"""V2 数据模型：任务状态机、步骤依赖、动作风险与指纹、Checkpoint。"""
from __future__ import annotations

from models.action import Action, ActionRisk, ActionType, Decision, Point
from models.checkpoint import Checkpoint
from models.state import Observation, StepOutcome, compact_observations
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
    """V2 的八态：单任务 MVP 的 pending/running/done/failed 不够描述排队与暂停。"""
    values = {s.value for s in TaskStatus}
    assert values == {
        "created",
        "queued",
        "running",
        "paused",
        "waiting",
        "done",
        "failed",
        "cancelled",
    }
    for status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
        assert Task(instruction="x", status=status).is_terminal


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
