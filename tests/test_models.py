"""V2 数据模型：任务状态机、步骤依赖、动作风险与指纹、Checkpoint。"""
from __future__ import annotations

import pytest

from models.action import (
    Action,
    ActionEffectStatus,
    ActionRisk,
    ActionType,
    Decision,
    Point,
)
from models.checkpoint import Checkpoint
from models.retry import ErrorClass
from models.state import Observation, StepOutcome, compact_observations
from models.step_attempt import AttemptOutcome
from models.exceptions import InvalidTransitionError
from models.task import Task, TaskPriority, TaskStatus, priority_rank
from models.task_relation import TaskRelation, TaskRelationResult
from models.task_step import StepStatus, TaskStep, build_steps


# ---------------------------------------------------------------- Task / Step


def test_task_starts_created_and_is_not_terminal():
    task = Task(instruction="打开设置")
    assert task.status is TaskStatus.CREATED
    assert not task.is_terminal
    assert not task.is_active


def test_task_status_coverage():
    """V2.3 十态：新增 degraded / device_unavailable。"""
    values = {s.value for s in TaskStatus}
    assert values == {
        "created",
        "queued",
        "running",
        "paused",
        "waiting",
        "degraded",
        "device_unavailable",
        "done",
        "failed",
        "cancelled",
    }
    for status in (
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.DEGRADED,
    ):
        assert Task(instruction="x", status=status).is_terminal
    for status in (TaskStatus.DEVICE_UNAVAILABLE,):
        assert Task(instruction="x", status=status).is_active


def test_priority_rank_orders_correctly():
    assert priority_rank(TaskPriority.CRITICAL) > priority_rank(TaskPriority.HIGH)
    assert priority_rank(TaskPriority.HIGH) > priority_rank(TaskPriority.NORMAL)
    assert priority_rank(TaskPriority.NORMAL) > priority_rank(TaskPriority.LOW)


def test_set_plan_builds_serial_dependencies():
    task = Task(instruction="x")
    task.set_plan(["打开设置", "找飞行模式", "开启"])

    assert [s.id for s in task.plan] == ["s1", "s2", "s3"]
    assert task.plan[0].depends_on == []
    assert task.plan[1].depends_on == ["s1"]
    assert all(s.status is StepStatus.PENDING for s in task.plan)


def test_next_pending_step_respects_dependencies():
    task = Task(instruction="x")
    task.set_plan(["a", "b", "c"])

    first = task.next_pending_step()
    assert first is not None and first.id == "s1"

    # s2 虽然先完成了，但 s1 还没做 → 仍然先做 s1（这就是依赖的意义）
    task.plan[1].mark(StepStatus.DONE)
    still_first = task.next_pending_step()
    assert still_first is not None and still_first.id == "s1"

    task.plan[0].mark(StepStatus.DONE)
    assert task.next_pending_step().id == "s3"


def test_skipped_step_counts_as_satisfied():
    task = Task(instruction="x")
    task.set_plan(["a", "b"])
    task.plan[0].mark(StepStatus.SKIPPED)
    assert task.next_pending_step().id == "s2"


def test_task_progress_and_current_step_sync():
    task = Task(instruction="x")
    task.set_plan(["a", "b", "c"])
    assert task.plan_progress() == "0/3 done"

    task.plan[0].mark(StepStatus.DONE)
    task.plan[1].mark(StepStatus.RUNNING)
    task.sync_current_step()

    assert task.current_step == 1
    assert task.plan_progress() == "1/3 done · s2 running"


def test_step_retry_budget():
    step = TaskStep(id="s1", goal="点一下", max_retries=2)
    assert step.retryable
    step.record_failure("第一次失败")
    step.record_failure("第二次失败")
    assert not step.retryable
    assert step.last_error == "第二次失败"

    step.record_action(Action(type=ActionType.BACK))
    assert step.last_error is None  # 成功执行后清掉历史错误


# ---------------------------------------------------------------- 计划 / 尝试拆分（§二十）


def test_attempts_preserve_every_try_instead_of_overwriting():
    """同一步骤试三次，三次的记录都要在。

    这正是拆分的意义：以前 `last_action` / `last_error` 是单值字段，
    第二次失败会把第一次的痕迹直接盖掉——而排查 Agent 的问题，
    最需要的恰恰是「它试过哪些没用的办法」。
    """
    step = TaskStep(id="s1", goal="提交订单", max_retries=3)

    first = step.record_failure("点错位置", action=Action(type=ActionType.TAP, value="提交"))
    second = step.record_failure("按钮置灰", action=Action(type=ActionType.TAP, value="确认"))
    third = step.record_failure("超时", action=Action(type=ActionType.TAP, value="再试"))

    assert [a.number for a in step.attempts] == [1, 2, 3]
    assert [a.error for a in step.attempts] == ["点错位置", "按钮置灰", "超时"]
    assert len({a.id for a in step.attempts}) == 3, "每次尝试都要有自己的 id"
    assert (first.id, second.id, third.id) != ("", "", "")

    # 派生视图只看最后一次（不翻旧账），但历史本身留着
    assert step.last_error == "超时"
    assert step.attempt_count == 3
    assert step.retry_count == 3


def test_retry_count_counts_only_failures():
    """只有失败才计入重试次数——成功的尝试不该消耗重试预算。"""
    step = TaskStep(id="s1", goal="做事", max_retries=3)

    step.record_action(Action(type=ActionType.BACK))
    step.record_action(Action(type=ActionType.HOME))
    assert step.attempt_count == 2
    assert step.retry_count == 0
    assert step.retryable

    step.record_failure("失败了")
    assert step.attempt_count == 3
    assert step.retry_count == 1


def test_success_after_failure_clears_last_error_but_keeps_the_record():
    step = TaskStep(id="s1", goal="做事", max_retries=3)
    step.record_failure("第一次失败")

    step.record_action(Action(type=ActionType.BACK))

    assert step.last_error is None, "成功之后就不该再报旧错"
    assert step.retry_count == 1, "但失败次数仍然记着（重试预算已消耗）"
    assert [a.outcome for a in step.attempts] == [AttemptOutcome.ERROR, AttemptOutcome.OK]


def test_attempt_carries_error_class_and_effect_for_postmortem():
    """事后要能回答「那次失败到底是设备抖动还是做法不对」。"""
    step = TaskStep(id="s1", goal="做事", max_retries=3)
    attempt = step.record_failure(
        "页面没变",
        action=Action(type=ActionType.TAP, value="提交"),
        error_class=ErrorClass.ACTION_REJECTED,
        effect=ActionEffectStatus.VERIFIED_FAILED,
        layer="vlm",
    )

    assert attempt.error_class is ErrorClass.ACTION_REJECTED
    assert attempt.effect is ActionEffectStatus.VERIFIED_FAILED
    assert attempt.layer == "vlm"
    assert attempt.action.value == "提交"
    assert not attempt.succeeded


def test_step_round_trips_through_json():
    """尝试历史要能落盘再读回来，否则重启后「它试过什么」就断了。"""
    step = TaskStep(id="s1", goal="做事", max_retries=3)
    step.record_failure("失败了", action=Action(type=ActionType.TAP, value="提交"))
    step.record_action(Action(type=ActionType.BACK), layer="vlm")

    restored = TaskStep.model_validate(step.model_dump(mode="json"))

    assert restored.attempt_count == 2
    assert restored.retry_count == 1
    assert restored.last_action.type is ActionType.BACK
    assert restored.attempts[0].error == "失败了"


def test_task_ids_are_unique():
    ids = {Task(instruction="x").id for _ in range(200)}
    assert len(ids) == 200


# ---------------------------------------------------------------- Action


def test_action_risk_inference():
    assert Action(type=ActionType.BACK).resolved_risk() is ActionRisk.SAFE
    assert Action(type=ActionType.WAIT, value="500").resolved_risk() is ActionRisk.SAFE
    assert Action(type=ActionType.TAP, target=Point(x=1, y=2)).resolved_risk() is ActionRisk.CAUTION
    # 命中危险关键词即升级——「发送」这类动作在真实 App 里往往不可撤销
    assert Action(type=ActionType.TAP, value="发送").resolved_risk() is ActionRisk.DANGEROUS
    assert Action(type=ActionType.TYPE, value="delete account").resolved_risk() is ActionRisk.DANGEROUS


def test_model_cannot_downgrade_policy_risk():
    """V2.1 §10/§11：策略风险是下限，模型声明的低风险不能把危险动作降级成安全。

    模型若乱标 SAFE，命中「发送」这类不可撤销关键词的策略动作仍须判为 DANGEROUS。
    """
    action = Action(type=ActionType.TAP, value="发送", risk=ActionRisk.SAFE)
    assert action.resolved_risk() is ActionRisk.DANGEROUS


def test_model_can_raise_policy_risk():
    """模型可以把策略判为 CAUTION/SAFE 的动作显式抬到 DANGEROUS（走人工确认路径）。"""
    action = Action(type=ActionType.TAP, target=Point(x=1, y=2), risk=ActionRisk.DANGEROUS)
    assert action.resolved_risk() is ActionRisk.DANGEROUS


def test_fingerprint_is_stable_for_identical_actions():
    a = Action(type=ActionType.TAP, target=Point(x=500, y=1200))
    b = Action(type=ActionType.TAP, target=Point(x=500, y=1200))
    assert a.fingerprint == b.fingerprint


def test_is_same_as_tolerates_coordinate_jitter():
    """指纹按固定网格量化，坐标跨网格边界时（1198 vs 1200）会算出不同指纹；
    所以死循环检测用带容差的 is_same_as，而不是比指纹字符串。"""
    a = Action(type=ActionType.TAP, target=Point(x=500, y=1200))
    b = Action(type=ActionType.TAP, target=Point(x=503, y=1198))
    assert a.is_same_as(b)


def test_is_same_as_rejects_real_differences():
    base = Action(type=ActionType.TAP, target=Point(x=100, y=100))
    assert not base.is_same_as(Action(type=ActionType.TAP, target=Point(x=900, y=100)))
    assert not base.is_same_as(Action(type=ActionType.LONG_PRESS, target=Point(x=100, y=100)))
    assert not base.is_same_as(Action(type=ActionType.TYPE, value="hello"))
    assert base.is_same_as(Action(type=ActionType.TAP, target=Point(x=100, y=100)))


def test_is_same_as_compares_string_targets_exactly():
    a = Action(type=ActionType.TAP, target="搜索按钮")
    assert a.is_same_as(Action(type=ActionType.TAP, target="搜索按钮"))
    assert not a.is_same_as(Action(type=ActionType.TAP, target="设置按钮"))


def test_decision_carries_step_done():
    decision = Decision(action=Action(type=ActionType.TAP), step_done=True)
    assert decision.step_done
    assert "tap" in decision.describe()


# ---------------------------------------------------------------- Observation / Checkpoint


def test_observation_prompt_dict_strips_noise():
    obs = Observation(
        step=3,
        screenshot_path="artifacts/shots/step_003.png",
        package="com.android.settings",
        activity=".Settings",
        ui_tree="<hierarchy/>",
        result={"ok": True, "x": 10, "y": 20, "internal_debug_blob": "x" * 500},
    )
    entry = obs.to_prompt_dict()

    assert entry["step"] == 3
    assert entry["status"] == "ok"  # mode="json"：枚举序列化成字符串
    assert "screenshot_path" not in entry
    assert "ui_tree" not in entry
    assert "internal_debug_blob" not in entry
    assert entry["result"] == {"ok": True, "x": 10, "y": 20}


def test_compact_observations_keeps_tail():
    history = [Observation(step=i, screenshot_path=f"s{i}.png") for i in range(1, 8)]
    entries = compact_observations(history, last_n=3)
    assert [e["step"] for e in entries] == [5, 6, 7]


def test_checkpoint_capture_and_screen_comparison():
    obs = Observation(
        step=2,
        screenshot_path="shot.png",
        package="com.tencent.mm",
        activity=".ui.SearchUI",
        ui_tree="<hierarchy>" + "x" * 30_000 + "</hierarchy>",
    )
    checkpoint = Checkpoint.capture(
        task_id="task-1",
        step=2,
        step_states={"s1": StepStatus.DONE, "s2": StepStatus.RUNNING},
        observation=obs,
        history_tail=[obs],
    )

    assert checkpoint.task_id == "task-1"
    assert checkpoint.current_step == 2
    assert checkpoint.step_states["s1"] is StepStatus.DONE
    # UI 快照要截断，否则每个 checkpoint 都会带上几百 KB 的 XML
    assert len(checkpoint.ui_snapshot) < 25_000

    same_screen = Observation(step=3, screenshot_path="s2.png", package="com.tencent.mm", activity=".ui.SearchUI")
    other_screen = Observation(step=3, screenshot_path="s3.png", package="com.taobao.taobao", activity=".Main")
    assert checkpoint.same_screen_as(same_screen)
    assert not checkpoint.same_screen_as(other_screen)

    summary = checkpoint.summary()
    assert summary["step_states"] == {"s1": "done", "s2": "running"}


def test_checkpoint_without_observation_is_still_valid():
    checkpoint = Checkpoint.capture(
        task_id="t", step=0, step_states={}, observation=None
    )
    assert checkpoint.screenshot_path is None
    assert not checkpoint.same_screen_as(Observation(step=1, screenshot_path="x.png"))


# ---------------------------------------------------------------- TaskRelation


def test_relation_result_actionable_threshold():
    confident = TaskRelationResult(relation=TaskRelation.SUBTASK, confidence=0.8)
    unsure = TaskRelationResult(relation=TaskRelation.SUBTASK, confidence=0.3)
    unrelated = TaskRelationResult(relation=TaskRelation.UNRELATED, confidence=0.9)

    assert confident.is_actionable
    assert not unsure.is_actionable
    assert not unrelated.is_actionable
    assert "subtask" in confident.describe()


def test_step_outcome_values_are_distinct_from_step_status():
    """两个枚举语义不同，值域也不能撞。"""
    assert {o.value for o in StepOutcome} == {"ok", "error", "done"}
    assert {s.value for s in StepStatus} == {"pending", "running", "done", "failed", "skipped"}


def test_build_steps_trims_goals():
    steps = build_steps(["  打开设置  ", "开启飞行模式"])
    assert [s.goal for s in steps] == ["打开设置", "开启飞行模式"]


# ---------------------------------------------------------------- 任务状态机（V2.2 §十）


def test_legal_transitions_are_not_flagged():
    """正常生命周期不该产生任何「非法迁移」噪音。"""
    task = Task(instruction="打开设置")

    for status in (
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.WAITING,
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.DONE,
    ):
        assert task.transition_to(status) is True, f"{status} 应当是合法迁移"

    assert task.illegal_transition_count == 0


def test_terminal_transition_is_rejected():
    """V2.3：终态（DONE/FAILED/CANCELLED/DEGRADED）不允许再迁出。

    这是防止「任务复活并重复产生副作用」的最后一道防线，必须 fail closed。
    """
    task = Task(instruction="x")
    task.mark(TaskStatus.DONE)

    with pytest.raises(InvalidTransitionError):
        task.transition_to(TaskStatus.RUNNING, source="test_suite")

    assert task.illegal_transition_count == 1
    assert task.status is TaskStatus.DONE


def test_non_terminal_illegal_transition_is_recorded_but_still_applied():
    """非终态之间的非法迁移仍先执行，避免运行时状态卡住；但要被记账。"""
    task = Task(instruction="x")
    task.mark(TaskStatus.RUNNING)

    ok = task.transition_to(TaskStatus.CREATED, source="test_suite")

    assert ok is False
    assert task.illegal_transition_count == 1
    assert task.status is TaskStatus.CREATED


def test_paused_reason_cleared_when_leaving_paused():
    from models.task import PAUSED_BY_USER

    task = Task(instruction="x")
    task.mark(TaskStatus.PAUSED, paused_reason=PAUSED_BY_USER)
    assert task.paused_reason == PAUSED_BY_USER

    task.mark(TaskStatus.QUEUED)
    assert task.paused_reason is None, "「上次为什么暂停」不该带到下一次运行"


def test_every_status_has_a_transition_entry():
    """表要覆盖全部状态，漏一个就会让那个状态变成「随便怎么转都非法」。"""
    from models.task import ALLOWED_TRANSITIONS

    assert set(ALLOWED_TRANSITIONS) == set(TaskStatus)
    for status in (
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ):
        assert ALLOWED_TRANSITIONS[status] == frozenset({status}), "终态不可逆"
