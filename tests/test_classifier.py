"""TaskClassifier：规则 + 相似度 + LLM 三层融合。"""
from __future__ import annotations

import pytest

from agent.classifier import RULE_MIN_SCORE, TaskClassifier, instruction_similarity
from models.task import Task, TaskPriority, TaskStatus
from models.task_relation import TaskRelation, TaskRelationResult


def task(instruction: str, **kwargs) -> Task:
    return Task(instruction=instruction, **kwargs)


# ---------------------------------------------------------------- 相似度


def test_instruction_similarity_is_jaccard_over_mixed_tokens():
    assert instruction_similarity("打开设置", "打开设置") == pytest.approx(1.0)
    assert instruction_similarity("打开设置", "打开微信") > 0
    assert instruction_similarity("打开设置", "buy milk online") == 0.0
    assert instruction_similarity("", "任意") == 0.0


def test_similarity_handles_english_words():
    assert instruction_similarity("open settings now", "open settings") > 0.5


# ---------------------------------------------------------------- 规则层


def test_leading_marker_is_strong_subtask_signal():
    """「先……」是前置子步骤的典型说法，句首命中给高分。"""
    clf = TaskClassifier()
    result = clf.classify("先帮我查东京酒店", current=task("帮我规划东京三日游"))

    assert result.relation is TaskRelation.SUBTASK
    assert result.confidence >= 0.5
    assert result.signals["rule.subtask"] > 0


def test_subtask_below_threshold_is_not_actionable():
    """够不到 SUBTASK 专属门槛（0.65）就不并入计划，改为各跑各的（V2.1 §九）。

    纯规则的融合上限是 (0.3*0.75 + 0.1*sim)/0.4，这条样本约 0.60——
    「像一个子步骤」不等于「确实是该任务的一部分」，证据不足就不动用户的计划。
    """
    clf = TaskClassifier()
    result = clf.classify("先帮我查东京酒店", current=task("帮我规划东京三日游"))

    assert result.threshold == 0.65
    assert result.confidence < result.threshold
    assert not result.is_actionable


def test_llm_support_makes_subtask_actionable():
    """LLM 认同后融合分越过 0.65，才真正具备并入资格。"""
    clf = TaskClassifier(
        llm_judge=lambda instruction, current: TaskRelationResult(
            relation=TaskRelation.SUBTASK, confidence=0.9, reason="东京行程的一部分"
        )
    )
    result = clf.classify("先帮我查东京酒店", current=task("帮我规划东京三日游"))

    assert result.relation is TaskRelation.SUBTASK
    assert result.is_actionable
    assert result.confidence >= 0.65


def test_inline_marker_is_weaker_than_leading_marker():
    clf = TaskClassifier()
    leading = clf.classify("先帮我订机票", current=task("规划行程"))
    inline = clf.classify("规划行程，顺便带上酒店", current=task("规划行程"))
    assert leading.confidence > inline.confidence


def test_interrupt_markers_win():
    clf = TaskClassifier()
    result = clf.classify("马上打开微信给张三发消息", current=task("在淘宝搜运动鞋"))
    assert result.relation is TaskRelation.INTERRUPT
    assert result.confidence >= 0.5
    assert result.threshold == 0.80
    # INTERRUPT 的语义是「急」，内容往往与当前任务无关（sim≈0），
    # 纯规则融合只到 0.60，够不到 0.80 门槛。
    # 注意：这不影响抢占——抢占由优先级决定，不由 is_actionable 决定。
    assert not result.is_actionable


def test_llm_support_makes_interrupt_actionable():
    """LLM 明确判定为打断后，融合分越过 0.80 门槛。"""
    clf = TaskClassifier(
        llm_judge=lambda instruction, current: TaskRelationResult(
            relation=TaskRelation.INTERRUPT, confidence=0.95, reason="用户要求马上处理"
        )
    )
    result = clf.classify("马上打开微信给张三发消息", current=task("在淘宝搜运动鞋"))

    assert result.relation is TaskRelation.INTERRUPT
    assert result.is_actionable
    assert result.requires_second_confirmation


def test_semantic_similarity_is_off_by_default():
    """不注入 embedder 时行为完全不变——离线环境不该依赖任何模型或 Key。"""
    clf = TaskClassifier()
    result = clf.classify("先帮我订酒店", current=task("帮我规划东京三日游"))

    assert "semantic_similarity" not in result.signals
    assert "relevance" in result.signals
    assert result.signals["relevance"] == result.signals["similarity"]


def test_embedder_rescues_synonym_rewording():
    """同义改写：字面判不出关系，语义相似度能把相关性补上来（V2.1 §八）。

    「订酒店」与「找住宿」字面几乎不重叠（Jaccard 约 0.12），纯规则只到约 0.59，
    够不到 SUBTASK 的 0.65 门槛；注入 embedder 后相关性接近 1，判定成立。
    这正是上一轮留下的「无 LLM 时 subtask 并入过严」的补丁。
    """
    def embed(text: str) -> list[float]:
        # 同义词映射到同一维度：住宿/酒店 → 0，机票/航班 → 1，东京/大阪 → 2
        vec = [0.0, 0.0, 0.0, 0.1]
        for word, idx in (
            ("住宿", 0), ("酒店", 0), ("机票", 1), ("航班", 1), ("东京", 2), ("大阪", 2)
        ):
            if word in text:
                vec[idx] += 1.0
        return vec

    current = task("帮我订酒店")
    literal_only = TaskClassifier().classify("先帮我找住宿", current=current)
    with_semantic = TaskClassifier(embedder=embed).classify("先帮我找住宿", current=current)

    assert literal_only.relation is TaskRelation.SUBTASK
    assert not literal_only.is_actionable, "字面不重叠时够不到门槛"

    assert with_semantic.relation is TaskRelation.SUBTASK
    assert with_semantic.signals["semantic_similarity"] > 0.9
    assert with_semantic.is_actionable, "语义相关性把置信度抬过门槛"


def test_embedder_failure_degrades_to_literal_similarity():
    """向量化挂掉不能影响注入流程，必须静默退回字面相似度。"""
    def broken(text: str) -> list[float]:
        raise RuntimeError("模型未加载")

    clf = TaskClassifier(embedder=broken)
    result = clf.classify("先帮我订酒店", current=task("帮我规划东京三日游"))

    assert result.relation is TaskRelation.SUBTASK
    assert result.signals["relevance"] == result.signals["similarity"]


def test_super_task_and_interrupt_are_flagged_for_second_confirmation():
    """会改写/打断在跑任务的关系，必须带二次确认标记；SAFE 关系不打扰用户。"""
    interrupt = TaskRelationResult(relation=TaskRelation.INTERRUPT, confidence=0.9)
    super_task = TaskRelationResult(relation=TaskRelation.SUPER_TASK, confidence=0.9)
    subtask = TaskRelationResult(relation=TaskRelation.SUBTASK, confidence=0.9)

    assert interrupt.requires_second_confirmation
    assert super_task.requires_second_confirmation
    assert not subtask.requires_second_confirmation


def test_super_task_markers():
    clf = TaskClassifier()
    result = clf.classify("改成查大阪的酒店", current=task("帮我规划东京三日游"))
    assert result.relation is TaskRelation.SUPER_TASK
    assert result.confidence >= 0.5


def test_unrelated_instruction_yields_unrelated():
    clf = TaskClassifier()
    result = clf.classify("今天杭州天气怎么样", current=task("在淘宝搜索黑色运动鞋"))
    assert result.relation is TaskRelation.UNRELATED
    assert not result.is_actionable
    assert "无强信号" in result.reason


def test_rule_min_score_boundary_is_exposed():
    """阈值是公开常量，避免测试与实现各写一份魔数。"""
    assert 0 < RULE_MIN_SCORE < 1


# ---------------------------------------------------------------- 重复检测


def test_duplicate_detected_against_candidates():
    clf = TaskClassifier()
    existing = task("在淘宝搜索黑色运动鞋")
    result = clf.classify("在淘宝搜索黑色运动鞋", current=None, candidates=[existing])

    assert result.relation is TaskRelation.DUPLICATE
    assert result.affected_task_id == existing.id
    assert "高度相似" in result.reason


def test_similar_but_not_identical_is_not_duplicate():
    clf = TaskClassifier()
    existing = task("在淘宝搜索黑色运动鞋")
    result = clf.classify("在京东搜索白色运动鞋", current=None, candidates=[existing])
    assert result.relation is not TaskRelation.DUPLICATE


# ---------------------------------------------------------------- LLM 融合


def test_llm_judgement_is_fused_with_rule_signal():
    calls: list[tuple[str, str]] = []

    def judge(instruction: str, current: str) -> TaskRelationResult:
        calls.append((instruction, current))
        return TaskRelationResult(relation=TaskRelation.UNRELATED, confidence=0.9, reason="两件事")

    clf = TaskClassifier(llm_judge=judge)
    result = clf.classify("先帮我查东京酒店", current=task("规划东京三日游"))

    assert calls, "注入了 llm_judge 就必须真的调用它"
    assert result.relation is TaskRelation.UNRELATED
    # 融合分 = 0.6*LLM + 0.3*规则 + 0.1*相似度，而不是直接采信 LLM
    assert "LLM 判定" in result.reason
    assert "rule.subtask" in result.signals


def test_llm_failure_degrades_to_rules():
    """LLM 挂掉时绝不能把整个注入流程带崩，必须退化为纯规则判定。"""

    def judge(instruction: str, current: str) -> TaskRelationResult:
        raise RuntimeError("VLM 超时")

    clf = TaskClassifier(llm_judge=judge)
    result = clf.classify("马上打开微信", current=task("在淘宝搜运动鞋"))

    assert result.relation is TaskRelation.INTERRUPT
    assert result.confidence > 0


def test_no_llm_means_pure_rule_mode():
    clf = TaskClassifier(llm_judge=None)
    result = clf.classify("先帮我查东京酒店", current=task("规划东京三日游"))
    assert result.relation is TaskRelation.SUBTASK
    assert "规则命中" in result.reason


def test_llm_not_called_without_current_task():
    def judge(instruction: str, current: str) -> TaskRelationResult:
        raise AssertionError("没有当前任务时不该调用 LLM")

    clf = TaskClassifier(llm_judge=judge)
    result = clf.classify("打开微信", current=None)
    assert result.relation is TaskRelation.UNRELATED


# ---------------------------------------------------------------- 边界


def test_empty_instruction():
    clf = TaskClassifier()
    result = clf.classify("   ", current=task("做点什么"))
    assert result.relation is TaskRelation.UNRELATED
    assert "空" in result.reason


def test_duplicate_check_runs_before_rule_layer():
    """重复判定优先于规则判定——重复任务不该因为措辞像子任务而被合并。"""
    clf = TaskClassifier()
    existing = task("先帮我查东京酒店")
    result = clf.classify("先帮我查东京酒店", current=task("规划行程"), candidates=[existing])
    assert result.relation is TaskRelation.DUPLICATE


def test_signals_are_exposed_for_debugging():
    clf = TaskClassifier()
    result = clf.classify("先打开设置", current=task("配置手机"))
    assert "rule.subtask" in result.signals
    assert "similarity" in result.signals
