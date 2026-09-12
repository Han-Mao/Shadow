"""AgentRuntime：闭环执行、异常收敛、死循环检测、危险动作门禁、Checkpoint 恢复。"""
from __future__ import annotations

import pytest

import agent.runtime as runtime_mod
from agent.runtime import AgentRuntime, RunOutcome
from agent.verifier import Verification
from device.adb import AdbError
from device.session import DeviceSession
from fakes import FakeDevice
from models.action import Action, ActionType, Decision, Point
from models.state import Observation, StepOutcome
from models.task import Task, TaskPriority, TaskStatus
from models.task_step import StepStatus
from storage import CheckpointStore, TaskStore, TrajectoryStore


def observation(step: int, tmp_path, package: str = "com.android.settings") -> Observation:
    return Observation(
        step=step,
        screenshot_path=str(tmp_path / f"step_{step:03d}.png"),
        package=package,
        activity=".MainActivity",
        ui_tree='<hierarchy><node bounds="[0,0][10,10]" clickable="true" text="确定"/></hierarchy>',
    )


def patch_observe(monkeypatch, tmp_path, package: str = "com.android.settings"):
    def fake_observe(adb, artifact_dir, step, suffix=""):
        return observation(step, tmp_path, package)

    monkeypatch.setattr(runtime_mod.observer, "observe", fake_observe)


def patch_planner(monkeypatch, *, decisions, goals=None, replans=None):
    """把规划层换成脚本化的返回序列。"""
    decision_iter = iter(decisions)
    replan_iter = iter(replans or [])

    monkeypatch.setattr(runtime_mod.planner, "generate_plan", lambda *a, **k: list(goals or []))

    def fake_plan_next(*args, **kwargs):
        try:
            return next(decision_iter)
        except StopIteration:
            # 脚本耗尽等价于「VLM 彻底拿不出决策」，比悄悄返回 DONE 更贴近现实
            raise RuntimeError("决策脚本已耗尽") from None

    def fake_replan(*args, **kwargs):
        try:
            return next(replan_iter)
        except StopIteration:
            raise RuntimeError("没有更多 Re-plan 脚本了")

    monkeypatch.setattr(runtime_mod.planner, "plan_next_action", fake_plan_next)
    monkeypatch.setattr(runtime_mod.planner, "replan", fake_replan)


def patch_verifier(monkeypatch, outcome: StepOutcome = StepOutcome.OK, message: str = "stub"):
    monkeypatch.setattr(
        runtime_mod.verifier,
        "verify_action",
        lambda *a, **k: Verification(outcome=outcome, layer="stub", message=message),
    )


def build(tmp_path, *, session: DeviceSession | None = None, checkpoints=None, trajectory=None):
    session = session or DeviceSession(FakeDevice())
    runtime = AgentRuntime(
        session,
        artifact_dir=tmp_path,
        trajectory=trajectory if trajectory is not None else TrajectoryStore(),
        checkpoints=checkpoints,
        task_store=None,
    )
    return session, runtime


def tap(x: int = 10, y: int = 20) -> Decision:
    return Decision(action=Action(type=ActionType.TAP, target=Point(x=x, y=y), reason="点一下"))


# ---------------------------------------------------------------- 正常路径


def test_runtime_completes_task_and_closes_steps(monkeypatch, tmp_path):
    patch_observe(monkeypatch, tmp_path)
    patch_planner(monkeypatch, decisions=[Decision(action=Action(type=ActionType.DONE, reason="到站"))], goals=["打开设置"])
    patch_verifier(monkeypatch)

    session, runtime = build(tmp_path)
    task = Task(instruction="打开设置", max_steps=5)
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.DONE
    assert task.status is TaskStatus.DONE
    assert task.plan and all(s.status is StepStatus.DONE for s in task.plan)


def test_runtime_advances_step_when_model_reports_step_done(monkeypatch, tmp_path):
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["第一步", "第二步"],
        decisions=[
            Decision(action=Action(type=ActionType.TAP, target=Point(x=1, y=1)), step_done=True),
            Decision(action=Action(type=ActionType.DONE, reason="完成")),
        ],
    )
    patch_verifier(monkeypatch)

    session, runtime = build(tmp_path)
    task = Task(instruction="两步任务", max_steps=5)
    session.acquire(task.id)
    try:
        runtime.run(task)
    finally:
        session.release(task.id)

    assert task.plan[0].status is StepStatus.DONE
    assert task.plan[0].last_action is not None
    assert task.plan[1].status is StepStatus.DONE


# ---------------------------------------------------------------- 异常收敛


def test_runtime_fails_after_repeated_observation_errors(monkeypatch, tmp_path):
    """回归（V1 遗留）：截图/dump 失败不能 500，也不能让任务卡在 running。"""

    def broken_observe(*args, **kwargs):
        raise AdbError("模拟器连接中断")

    monkeypatch.setattr(runtime_mod.observer, "observe", broken_observe)
    patch_planner(monkeypatch, decisions=[])

    session, runtime = build(tmp_path)
    task = Task(instruction="观察必失败", max_steps=5)
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.FAILED
    assert task.status is TaskStatus.FAILED


def test_runtime_replans_after_failed_action(monkeypatch, tmp_path):
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["做事"],
        # 第一次给出非法 value（执行必失败），Re-plan 直接宣告完成
        decisions=[Decision(action=Action(type=ActionType.WAIT, value="立刻"))],
        replans=[Decision(action=Action(type=ActionType.DONE, reason="换路子后完成"))],
    )
    patch_verifier(monkeypatch)

    session, runtime = build(tmp_path)
    task = Task(instruction="重试", max_steps=5)
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.DONE


def test_runtime_gives_up_after_max_retries(monkeypatch, tmp_path):
    """执行一直失败且 Re-plan 也救不回来时，必须在有限步内收口。"""
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["做事"],
        decisions=[Decision(action=Action(type=ActionType.WAIT, value="马上")) for _ in range(10)],
        replans=[],
    )
    patch_verifier(monkeypatch, StepOutcome.ERROR, "页面没变")

    session, runtime = build(tmp_path)
    task = Task(instruction="一直失败", max_steps=20)
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.FAILED
    assert task.status is TaskStatus.FAILED


# ---------------------------------------------------------------- 死循环


def test_runtime_detects_loop_and_forces_replan(monkeypatch, tmp_path):
    """连续做同一个动作时必须换策略，而不是无限重试同一个动作。"""
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["循环任务"],
        decisions=[tap(500, 1200) for _ in range(10)],
        replans=[Decision(action=Action(type=ActionType.DONE, reason="换路子成功"))],
    )
    patch_verifier(monkeypatch)

    session, runtime = build(tmp_path)
    task = Task(instruction="循环任务", max_steps=20)
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.DONE


def test_loop_detection_counts_jittered_coordinates_as_same_action(monkeypatch, tmp_path):
    """坐标抖几像素不能把「同一个动作」洗成三个不同动作。"""
    patch_observe(monkeypatch, tmp_path)
    jittered = [Decision(action=Action(type=ActionType.TAP, target=Point(x=500 + i, y=1200 - i))) for i in range(4)]
    patch_planner(
        monkeypatch,
        goals=["循环任务"],
        decisions=jittered,
        replans=[Decision(action=Action(type=ActionType.DONE, reason="换路子成功"))],
    )
    patch_verifier(monkeypatch)

    session, runtime = build(tmp_path)
    task = Task(instruction="抖动循环", max_steps=20)
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.DONE


# ---------------------------------------------------------------- 危险动作门禁


def test_dangerous_action_waits_for_confirmation(monkeypatch, tmp_path):
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["发消息"],
        decisions=[
            Decision(action=Action(type=ActionType.TAP, value="发送", target=Point(x=5, y=5))),
            Decision(action=Action(type=ActionType.DONE, reason="发送完成")),
        ],
    )
    patch_verifier(monkeypatch)

    session, runtime = build(tmp_path)
    task = Task(instruction="给张三发消息", max_steps=5)
    session.acquire(task.id)

    outcome = runtime.run(task)
    assert outcome is RunOutcome.AWAITING_CONFIRMATION
    assert task.status is TaskStatus.WAITING
    assert runtime.pending_confirmation(task.id) is not None

    # 人工批准后该动作放行一次
    assert runtime.confirm(task.id, approved=True)
    outcome = runtime.run(task)
    session.release(task.id)

    assert outcome is RunOutcome.DONE
    assert task.status is TaskStatus.DONE


def test_rejected_dangerous_action_does_not_run(monkeypatch, tmp_path):
    """被否决的动作不能再放行，也不能让任务在「请求确认→否决→再请求」之间空转。"""
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["下单"],
        decisions=[
            Decision(action=Action(type=ActionType.TAP, value="确认下单", target=Point(x=5, y=5))),
            Decision(action=Action(type=ActionType.TAP, value="确认下单", target=Point(x=5, y=5))),
        ],
        replans=[],
    )
    patch_verifier(monkeypatch)

    session, runtime = build(tmp_path)
    task = Task(instruction="下单", max_steps=10)
    session.acquire(task.id)

    assert runtime.run(task) is RunOutcome.AWAITING_CONFIRMATION
    assert runtime.confirm(task.id, approved=False)
    assert runtime.pending_confirmation(task.id) is None

    # 否决后模型又给出同一个动作：必须转去换策略，而不是再次弹确认
    outcome = runtime.run(task)
    session.release(task.id)

    assert outcome is RunOutcome.FAILED
    assert task.status is TaskStatus.FAILED


def test_confirm_without_pending_action_is_rejected():
    session = DeviceSession(FakeDevice())
    runtime = AgentRuntime(session)
    assert not runtime.confirm("不存在的任务", approved=True)


# ---------------------------------------------------------------- 抢占与取消


def test_runtime_suspends_when_asked_to_yield(monkeypatch, tmp_path):
    patch_observe(monkeypatch, tmp_path)
    patch_planner(monkeypatch, decisions=[tap(), tap()])
    patch_verifier(monkeypatch)

    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    session, runtime = build(tmp_path, checkpoints=checkpoints)
    task = Task(instruction="逛淘宝", max_steps=5)
    session.acquire(task.id)
    session.request_preempt("高优先级任务")

    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.SUSPENDED
    # 让出前必须落一个 Checkpoint，否则恢复时无从判断页面还对不对
    assert task.checkpoint_id is not None
    assert checkpoints.load(task.id, task.checkpoint_id) is not None


def test_runtime_exits_when_cancelled(monkeypatch, tmp_path):
    patch_observe(monkeypatch, tmp_path)
    patch_planner(monkeypatch, decisions=[tap()])

    session, runtime = build(tmp_path)
    task = Task(instruction="x", status=TaskStatus.CANCELLED)

    assert runtime.run(task) is RunOutcome.CANCELLED
    assert task.status is TaskStatus.CANCELLED


def test_runtime_stops_at_max_steps(monkeypatch, tmp_path):
    patch_observe(monkeypatch, tmp_path)
    patch_planner(monkeypatch, goals=["长任务"], decisions=[tap(i * 40, i * 40) for i in range(50)])
    patch_verifier(monkeypatch)

    session, runtime = build(tmp_path)
    task = Task(instruction="长任务", max_steps=3)
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.FAILED


# ---------------------------------------------------------------- Checkpoint 恢复


def test_resume_keeps_plan_when_screen_matches(monkeypatch, tmp_path):
    """页面还在原处时，不该丢掉已有计划重新规划。"""
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    patch_observe(monkeypatch, tmp_path, package="com.android.settings")

    called = {"generate": 0}

    def counting_generate(*args, **kwargs):
        called["generate"] += 1
        return ["重新规划出来的步骤"]

    monkeypatch.setattr(runtime_mod.planner, "generate_plan", counting_generate)
    monkeypatch.setattr(
        runtime_mod.planner,
        "plan_next_action",
        lambda *a, **k: Decision(action=Action(type=ActionType.DONE, reason="完成")),
    )
    patch_verifier(monkeypatch)

    task = Task(instruction="原本的任务", max_steps=5)
    task.set_plan(["第一步", "第二步"])
    checkpoint = runtime_mod.Checkpoint.capture(
        task_id=task.id,
        step=1,
        step_states=task.step_states(),
        observation=observation(1, tmp_path, package="com.android.settings"),
    )
    checkpoints.save(checkpoint)
    task.checkpoint_id = checkpoint.id

    session, runtime = build(tmp_path, checkpoints=checkpoints)
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.DONE
    assert called["generate"] == 0, "恢复点有效时不应该重新规划"


def test_resume_replans_when_screen_changed(monkeypatch, tmp_path):
    """页面已经不是当初那一屏（比如被抢占期间用户切了 App），必须重新规划。"""
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    patch_observe(monkeypatch, tmp_path, package="com.taobao.taobao")

    called = {"generate": 0}

    def counting_generate(*args, **kwargs):
        called["generate"] += 1
        return ["重新规划出来的步骤"]

    monkeypatch.setattr(runtime_mod.planner, "generate_plan", counting_generate)
    monkeypatch.setattr(
        runtime_mod.planner,
        "plan_next_action",
        lambda *a, **k: Decision(action=Action(type=ActionType.DONE, reason="完成")),
    )
    patch_verifier(monkeypatch)

    task = Task(instruction="原本的任务", max_steps=5)
    task.set_plan(["第一步", "第二步"])
    checkpoint = runtime_mod.Checkpoint.capture(
        task_id=task.id,
        step=1,
        step_states=task.step_states(),
        observation=observation(1, tmp_path, package="com.android.settings"),
    )
    checkpoints.save(checkpoint)
    task.checkpoint_id = checkpoint.id

    session, runtime = build(tmp_path, checkpoints=checkpoints)
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.DONE
    assert called["generate"] == 1, "恢复点失效时必须重新规划"


def test_trajectory_records_each_executed_step(monkeypatch, tmp_path):
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["两步"],
        decisions=[
            Decision(action=Action(type=ActionType.TAP, target=Point(x=1, y=1)), step_done=True),
            Decision(action=Action(type=ActionType.DONE, reason="完成")),
        ],
    )
    patch_verifier(monkeypatch)

    trajectory = TrajectoryStore()
    session, runtime = build(tmp_path, trajectory=trajectory)
    task = Task(instruction="两步任务", max_steps=5)
    session.acquire(task.id)
    try:
        runtime.run(task)
    finally:
        session.release(task.id)

    entries = trajectory.history(task.id)
    assert entries, "执行轨迹必须被记录下来，否则决策没有上下文"
    assert entries[-1].action is not None
