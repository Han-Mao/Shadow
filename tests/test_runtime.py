"""AgentRuntime：闭环执行、异常收敛、死循环检测、危险动作门禁、Checkpoint 恢复。"""
from __future__ import annotations

import pytest

import agent.replay as replay_mod
import agent.runtime as runtime_mod
from agent.runtime import AgentRuntime, RunOutcome
from agent.verifier import Verification
from device.adb import AdbError
from device.pool import DevicePool
from device.session import DeviceSession
from fakes import FakeDevice
from models.action import Action, ActionEffectStatus, ActionRisk, ActionType, Decision, Point
from models.budget import TaskBudget
from models.checkpoint import Checkpoint
from models.retry import ErrorClass
from models.state import Observation, StepOutcome
from models.task import Task, TaskPriority, TaskStatus
from models.task_step import StepStatus
from storage import CheckpointStore, EventLog, TaskStore, TrajectoryStore


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


def patch_observe_ui(monkeypatch, tmp_path, ui_tree: str, package: str = "com.android.settings"):
    """UI 树可控的观察替身。

    对账靠的是比对 UI 结构指纹，所以必须能指定「崩溃前那一屏」和「现在这一屏」
    分别是哪棵树，否则测不出「页面变了 / 没变」两种分支。
    """

    def fake_observe(adb, artifact_dir, step, suffix=""):
        return Observation(
            step=step,
            screenshot_path=str(tmp_path / f"step_{step:03d}.png"),
            package=package,
            activity=".MainActivity",
            ui_tree=ui_tree,
        )

    monkeypatch.setattr(runtime_mod.observer, "observe", fake_observe)


PATCHED_UI_BEFORE = '<hierarchy><node class="android.widget.Button" text="确定"/></hierarchy>'
PATCHED_UI_AFTER = (
    '<hierarchy><node class="android.widget.Button" text="已提交"/>'
    '<node class="android.widget.TextView" text="提交成功"/></hierarchy>'
)


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


def build(
    tmp_path,
    *,
    session: DeviceSession | None = None,
    checkpoints=None,
    trajectory=None,
    event_log=None,
):
    session = session or DeviceSession(FakeDevice())
    runtime = AgentRuntime(
        session,
        artifact_dir=tmp_path,
        trajectory=trajectory if trajectory is not None else TrajectoryStore(),
        checkpoints=checkpoints,
        task_store=None,
        event_log=event_log,
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
    task = Task(instruction="打开设置", budget=TaskBudget(max_action_steps=5))
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
    task = Task(instruction="两步任务", budget=TaskBudget(max_action_steps=5))
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
    task = Task(instruction="观察必失败", budget=TaskBudget(max_action_steps=5))
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
    task = Task(instruction="重试", budget=TaskBudget(max_action_steps=5))
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.DONE


def test_runtime_gives_up_after_transient_retries(monkeypatch, tmp_path):
    """瞬时错误（ADB 超时这类设备抖动）重试到策略上限后必须收口为失败。

    保证仍然是「有限步内收敛」：不会因为引入重试策略就变成无限循环。
    """
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["做事"],
        decisions=[Decision(action=Action(type=ActionType.WAIT, value="马上")) for _ in range(10)],
        replans=[],
    )
    patch_verifier(monkeypatch, StepOutcome.ERROR, "ADB 超时")

    session, runtime = build(tmp_path)
    task = Task(instruction="一直失败", budget=TaskBudget(max_action_steps=20))
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.FAILED
    assert task.status is TaskStatus.FAILED


def test_runtime_asks_human_when_error_cannot_be_classified(monkeypatch, tmp_path):
    """错误类别不明、试探也无效时，转人工确认而不是继续瞎试（V2.1 §十三）。

    旧行为是「不分青红皂白重试到 3 次然后判失败」。现在先分类：
    判不出类别的只给 `max_unknown` 次试探，用尽后挂起等人——
    在一件说不清的事情上空转重试，比停下来问人更糟。
    """
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["做事"],
        decisions=[Decision(action=Action(type=ActionType.WAIT, value="马上")) for _ in range(10)],
        replans=[],
    )
    patch_verifier(monkeypatch, StepOutcome.ERROR, "页面没变")

    session, runtime = build(tmp_path)
    task = Task(instruction="一直失败", budget=TaskBudget(max_action_steps=20))
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.AWAITING_CONFIRMATION
    assert task.status is TaskStatus.WAITING


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
    task = Task(instruction="循环任务", budget=TaskBudget(max_action_steps=20))
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
    task = Task(instruction="抖动循环", budget=TaskBudget(max_action_steps=20))
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
    task = Task(instruction="给张三发消息", budget=TaskBudget(max_action_steps=5))
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
    task = Task(instruction="下单", budget=TaskBudget(max_action_steps=10))
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
    task = Task(instruction="逛淘宝", budget=TaskBudget(max_action_steps=5))
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
    task = Task(instruction="长任务", budget=TaskBudget(max_action_steps=3))
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

    task = Task(instruction="原本的任务", budget=TaskBudget(max_action_steps=5))
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

    task = Task(instruction="原本的任务", budget=TaskBudget(max_action_steps=5))
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
    task = Task(instruction="两步任务", budget=TaskBudget(max_action_steps=5))
    session.acquire(task.id)
    try:
        runtime.run(task)
    finally:
        session.release(task.id)

    entries = trajectory.history(task.id)
    assert entries, "执行轨迹必须被记录下来，否则决策没有上下文"
    assert entries[-1].action is not None


# ---------------------------------------------------------------- V2.1：Action Effect 恢复对账


def test_resume_replans_when_last_action_only_dispatched(monkeypatch, tmp_path):
    """恢复点记录上次动作只 dispatch 未验证（EFFECT_UNKNOWN，V2.1 §五）：

    绝不能基于旧 action 盲目续跑，否则「提交订单」可能被重复执行。
    正确做法：清空计划，让 planner 基于当前页面重新判断。
    """
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

    task = Task(instruction="原本的任务", budget=TaskBudget(max_action_steps=5))
    task.set_plan(["第一步", "第二步"])
    obs = observation(1, tmp_path, package="com.android.settings")
    checkpoint = runtime_mod.Checkpoint.capture(
        task_id=task.id,
        step=1,
        step_states=task.step_states(),
        observation=obs,
        action_effect=ActionEffectStatus.DISPATCHED,
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
    assert called["generate"] == 1, "DISPATCHED 恢复点必须重新规划，不能盲目续跑旧计划"


# ---------------------------------------------------------------- V2.1：预算拆分（§二 / §25 scenario 12）


def test_runtime_stops_at_action_budget_not_observation_budget(monkeypatch, tmp_path):
    """达到动作步数上限即终止，而非观察次数上限。

    旧实现里 max_steps 实际统计的是 Observe 次数；V2.1 拆成三个独立预算，
    这里把观察预算给足，只压动作预算，确认是「动作数」先触顶（scenario 12）。
    """
    patch_observe(monkeypatch, tmp_path)
    patch_planner(monkeypatch, goals=["长任务"], decisions=[tap(i * 40, i * 40) for i in range(50)])
    patch_verifier(monkeypatch)

    trajectory = TrajectoryStore()
    session, runtime = build(tmp_path, trajectory=trajectory)
    task = Task(
        instruction="长任务",
        budget=TaskBudget(max_action_steps=3, max_observations=500, max_model_calls=500),
    )
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.FAILED
    executed = [e for e in trajectory.history(task.id) if e.action is not None]
    assert len(executed) == 3, "动作预算为 3，应恰好执行 3 个动作后终止"


# ---------------------------------------------------------------- V2.1：动作对账（§五 / §25 scenario 5、6）


def _dispatched_checkpoint(tmp_path, task, ui_tree, action):
    """造一个「动作已 dispatch、还没验证」的恢复点（模拟进程在这瞬间崩溃）。"""
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    before = Observation(
        step=1,
        screenshot_path=str(tmp_path / "before.png"),
        package="com.android.settings",
        activity=".MainActivity",
        ui_tree=ui_tree,
    )
    checkpoint = Checkpoint.capture(
        task_id=task.id,
        step=1,
        step_states=task.step_states(),
        observation=before,
        action_effect=ActionEffectStatus.DISPATCHED,
        last_action=action,
    )
    checkpoints.save(checkpoint)
    task.checkpoint_id = checkpoint.id
    return checkpoints


def test_reconcile_continues_plan_when_dispatched_action_took_effect(monkeypatch, tmp_path):
    """Scenario 5：对账判定「已生效」→ 接着原计划跑，不推倒重来。

    旧实现只有「清空计划重新规划」一条路——动作其实已经生效了也要重来一遍，
    既浪费又可能把已经完成的事再做一次。
    """
    patch_observe_ui(monkeypatch, tmp_path, PATCHED_UI_AFTER)

    called = {"generate": 0}

    def counting_generate(*args, **kwargs):
        called["generate"] += 1
        return ["重规划出来的步骤"]

    monkeypatch.setattr(runtime_mod.planner, "generate_plan", counting_generate)
    monkeypatch.setattr(
        runtime_mod.planner,
        "plan_next_action",
        lambda *a, **k: Decision(action=Action(type=ActionType.DONE, reason="完成")),
    )
    patch_verifier(monkeypatch)

    task = Task(instruction="点确定并提交", budget=TaskBudget(max_action_steps=5))
    task.set_plan(["第一步", "第二步"])
    checkpoints = _dispatched_checkpoint(tmp_path, task, PATCHED_UI_BEFORE, tap(100, 200).action)

    session, runtime = build(tmp_path, checkpoints=checkpoints)
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.DONE
    assert called["generate"] == 0, "已确认生效就该接着原计划跑，不该再规划一次"
    assert [s.goal for s in task.plan] == ["第一步", "第二步"], "原计划不能被清空"


def test_reconcile_retries_action_when_it_never_took_effect(monkeypatch, tmp_path):
    """Scenario 6：对账判定「未生效」→ 把那个动作重做一次，而不是换条路走。

    页面结构与崩溃前完全一致，说明上次那个动作根本没执行成功。
    """
    patch_observe_ui(monkeypatch, tmp_path, PATCHED_UI_BEFORE)  # 与崩溃前同一屏

    executed: list[Action] = []

    def fake_execute(device, action, ui_tree):
        executed.append(action)
        return {"ok": True}

    monkeypatch.setattr(runtime_mod.executor, "execute", fake_execute)

    called = {"generate": 0}

    def counting_generate(*args, **kwargs):
        called["generate"] += 1
        return ["重规划出来的步骤"]

    monkeypatch.setattr(runtime_mod.planner, "generate_plan", counting_generate)
    monkeypatch.setattr(
        runtime_mod.planner,
        "plan_next_action",
        lambda *a, **k: Decision(action=Action(type=ActionType.DONE, reason="完成")),
    )
    patch_verifier(monkeypatch)

    retried = tap(100, 200).action
    task = Task(instruction="点确定并提交", budget=TaskBudget(max_action_steps=5))
    task.set_plan(["第一步"])
    checkpoints = _dispatched_checkpoint(tmp_path, task, PATCHED_UI_BEFORE, retried)

    session, runtime = build(tmp_path, checkpoints=checkpoints)
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.DONE
    assert len(executed) == 1, "只重做一次，不要无限重试"
    assert executed[0].is_same_as(retried), "重做的必须是恢复点里记录的那个动作"
    assert called["generate"] == 0, "只是重做动作，不需要重新规划"


# ---------------------------------------------------------------- V2.1：执行记录（§二十）


def test_runtime_records_failed_attempt_with_action_and_error_class(monkeypatch, tmp_path):
    """失败要留下「试的是哪个动作、属于哪类错误」，而不只是一句错误文本。

    这条是拆分的直接收益：以前失败只写 `last_error`，动作和错误分类都丢了，
    事后没法回答「它到底试过什么、这种错该不该重试」。
    """
    patch_observe(monkeypatch, tmp_path)
    failing = tap(100, 200).action
    patch_planner(
        monkeypatch, goals=["做事"], decisions=[Decision(action=failing) for _ in range(5)]
    )
    patch_verifier(monkeypatch, StepOutcome.ERROR, "ADB 超时")

    session, runtime = build(tmp_path)
    task = Task(instruction="做事", budget=TaskBudget(max_action_steps=10))
    task.set_plan(["第一步"])
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.FAILED
    step = task.plan[0]
    assert step.attempt_count >= 1, "失败必须留下尝试记录"

    first = step.attempts[0]
    assert first.action is not None and first.action.is_same_as(failing)
    assert first.error == "ADB 超时"
    assert first.error_class is ErrorClass.TRANSIENT, "「ADB 超时」属于设备抖动"
    assert first.number == 1


# ---------------------------------------------------------------- V2.1：事件日志（§二十三）


def test_runtime_emits_lifecycle_events(monkeypatch, tmp_path):
    """一次执行会留下完整的事件流：启动 → 动作发出 → 验证 → 完成。

    事件日志与轨迹分开的意义就在这里：轨迹只留最近几步给下一步决策，
    而事件流能事后回答「它到底做过什么、什么时候做的」。
    """
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["做事"],
        decisions=[tap(100, 200), Decision(action=Action(type=ActionType.DONE, reason="完成"))],
    )
    patch_verifier(monkeypatch)

    event_log = EventLog(tmp_path / "events")
    session, runtime = build(tmp_path, event_log=event_log)
    task = Task(instruction="点一下", budget=TaskBudget(max_action_steps=5))
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.DONE
    kinds = event_log.kinds(task.id)
    assert kinds[0] == "started"
    assert "action_dispatched" in kinds
    assert "action_verified" in kinds
    assert kinds[-1] == "done"

    dispatched = [e for e in event_log.read(task.id) if e.kind == "action_dispatched"][0]
    assert dispatched.data["action"] == "tap"
    assert dispatched.data["attempt_id"], "每次尝试都要有自己的 attempt id"


# ---------------------------------------------------------------- V2.1：HITL（§25 scenario 10）


def test_runtime_events_are_self_sufficient_for_replay(monkeypatch, tmp_path):
    """事件流要「自足」到可以回放：动作细节都在事件里，不依赖内存态（§二十三）。

    TrajectoryStore 是内存态、会被裁剪；而最需要回放的时刻恰恰是任务崩溃之后。
    所以事件里必须带上「点在哪、输入了什么」——只记动作类型的话，回放看不出名堂。
    """
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["做事"],
        decisions=[tap(120, 340), Decision(action=Action(type=ActionType.DONE, reason="完成"))],
    )
    patch_verifier(monkeypatch)

    event_log = EventLog(tmp_path / "events")
    session, runtime = build(tmp_path, event_log=event_log)
    task = Task(instruction="点一下", budget=TaskBudget(max_action_steps=5))
    session.acquire(task.id)
    try:
        assert runtime.run(task) is RunOutcome.DONE
    finally:
        session.release(task.id)

    timeline = replay_mod.load_timeline(event_log, task.id)
    plan = replay_mod.build_plan(timeline)

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.action == "tap"
    assert step.target == {"x": 120.0, "y": 340.0}, "坐标必须落在事件里"
    assert step.attempt_id, "要能对应回是哪一次尝试"

    report = timeline.render_markdown()
    assert "(120,340)" in report
    assert "## 时间轴" in report


def test_runtime_persists_trajectory_so_restart_keeps_context(monkeypatch, tmp_path):
    """跑完后轨迹要落盘：重启后模型还能拿到前面的上下文（V2.1）。

    否则恢复点虽然在，模型却「失忆」了——只能盯着当前一屏重新猜，
    跟从零开始差不多。长跑任务上这个差别很大。
    """
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["做事"],
        decisions=[tap(1, 2), Decision(action=Action(type=ActionType.DONE, reason="完成"))],
    )
    patch_verifier(monkeypatch)

    root = tmp_path / "trajectories"
    session, runtime = build(tmp_path, trajectory=TrajectoryStore(root=root))
    task = Task(instruction="点一下", budget=TaskBudget(max_action_steps=5))
    session.acquire(task.id)
    try:
        assert runtime.run(task) is RunOutcome.DONE
    finally:
        session.release(task.id)

    restarted = TrajectoryStore(root=root)

    assert restarted.last_step(task.id) >= 1, "轨迹必须落盘"
    assert restarted.prompt_context(task.id), "重启后仍要能拼出决策上下文"


def test_runtime_targets_the_bound_device_not_the_first_one(monkeypatch, tmp_path):
    """任务绑在 emu-2，动作就必须发到 emu-2 上（V2.1 §十三）。

    不按绑定取会话的话，两台设备的任务会全跑到第一台上——「多设备」就成了摆设，
    而真实场景下这意味着**去操作了错误的手机**。
    """
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["做事"],
        decisions=[tap(10, 20), Decision(action=Action(type=ActionType.DONE, reason="完成"))],
    )
    patch_verifier(monkeypatch)

    first_device, second_device = FakeDevice(), FakeDevice()
    pool = DevicePool(
        [
            DeviceSession(first_device, serial="emu-1"),
            DeviceSession(second_device, serial="emu-2"),
        ]
    )
    runtime = AgentRuntime(pool, artifact_dir=tmp_path, trajectory=TrajectoryStore())

    task = Task(
        instruction="点一下",
        device_serial="emu-2",
        budget=TaskBudget(max_action_steps=5),
    )
    pool.require("emu-2").acquire(task.id)  # runtime 假设调用方（调度器）已持有设备
    try:
        assert runtime.run(task) is RunOutcome.DONE
    finally:
        pool.require("emu-2").release(task.id)

    assert ("tap", 10, 20) in second_device.events, "动作应发到绑定的 emu-2"
    assert first_device.events == [], "绝不能碰到 emu-1"


def test_runtime_falls_back_to_the_default_device_when_unbound(monkeypatch, tmp_path):
    """任务还没绑设备时用默认（第一台），保证单设备路径行为不变。"""
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["做事"],
        decisions=[Decision(action=Action(type=ActionType.DONE, reason="完成"))],
    )
    patch_verifier(monkeypatch)

    first_device = FakeDevice()
    pool = DevicePool(
        [
            DeviceSession(first_device, serial="emu-1"),
            DeviceSession(FakeDevice(), serial="emu-2"),
        ]
    )
    runtime = AgentRuntime(pool, artifact_dir=tmp_path)

    task = Task(instruction="做事", budget=TaskBudget(max_action_steps=5))
    pool.require("emu-1").acquire(task.id)
    try:
        assert runtime.run(task) is RunOutcome.DONE
    finally:
        pool.require("emu-1").release(task.id)


def test_dangerous_approval_is_consumed_after_one_use(monkeypatch, tmp_path):
    """Scenario 10：人工批准只对**这一次**动作生效，用完立即失效。

    否则用户批准过一次「付款」，之后所有危险动作都会被自动放行——
    一次批准变成永久授权，门禁形同虚设。
    """
    patch_observe(monkeypatch, tmp_path)
    danger = Action(type=ActionType.TAP, value="确认付款", reason="下单")

    executed: list[Action] = []

    def fake_execute(device, action, ui_tree):
        executed.append(action)
        return {"ok": True}

    monkeypatch.setattr(runtime_mod.executor, "execute", fake_execute)
    monkeypatch.setattr(runtime_mod.planner, "generate_plan", lambda *a, **k: ["付款"])
    monkeypatch.setattr(
        runtime_mod.planner, "plan_next_action", lambda *a, **k: Decision(action=danger)
    )
    patch_verifier(monkeypatch)

    session, runtime = build(tmp_path)
    task = Task(instruction="去付款", budget=TaskBudget(max_action_steps=5))
    session.acquire(task.id)
    try:
        first = runtime.run(task)
        assert first is RunOutcome.AWAITING_CONFIRMATION
        assert runtime.pending_confirmation(task.id) is not None
        assert executed == [], "批准之前绝不能执行"

        assert runtime.confirm(task.id, approved=True)

        second = runtime.run(task)
        assert second is RunOutcome.AWAITING_CONFIRMATION, (
            "批准后只放行一次，再出现危险动作必须重新确认"
        )
        assert len(executed) == 1, "一次批准只对一次动作生效"
    finally:
        session.release(task.id)


# ---------------------------------------------------------------- V2.2 §三：效果未知与在线对账


def patch_observe_sequence(monkeypatch, tmp_path, trees, *, fail_post_times=0):
    """按调用序返回不同的 UI 树，并可以让「重新观察」失败若干次。

    这是 V2.2 §三 的测试基础：必须先能造出「动作发出去了、但拿不到验证观察」
    这个瞬间，才谈得上验证系统怎么处理它。
    """
    remaining = list(trees)
    failures = {"left": fail_post_times}
    last = {"tree": trees[-1] if trees else ""}

    def fake_observe(adb, artifact_dir, step, suffix=""):
        if suffix == "post" and failures["left"] > 0:
            failures["left"] -= 1
            raise AdbError("模拟：动作已发出，但重新观察失败")
        tree = remaining.pop(0) if remaining else last["tree"]
        last["tree"] = tree
        return Observation(
            step=step,
            screenshot_path=str(tmp_path / f"step_{step:03d}{suffix}.png"),
            package="com.android.settings",
            activity=".MainActivity",
            ui_tree=tree,
        )

    monkeypatch.setattr(runtime_mod.observer, "observe", fake_observe)


def test_reobserve_failure_is_recorded_as_effect_unknown(monkeypatch, tmp_path):
    """重新观察失败**不能**当成 OK 放行（V2.2 §三）。

    旧实现把它降级为「未验证的 OK」：点击发送 → 拿不到截图 → 记为成功 →
    下一轮模型看到页面没变又点一次 → 重复发送。现在它必须落成 EFFECT_UNKNOWN，
    并由下一个安全点的对账决定继续 / 重做 / 找人。
    """
    patch_observe_sequence(monkeypatch, tmp_path, [PATCHED_UI_BEFORE, PATCHED_UI_AFTER], fail_post_times=1)
    patch_planner(
        monkeypatch,
        goals=["发消息"],
        decisions=[tap(10, 20), Decision(action=Action(type=ActionType.DONE, reason="完成"))],
    )
    patch_verifier(monkeypatch)

    events = EventLog(tmp_path / "events")
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    session, runtime = build(tmp_path, checkpoints=checkpoints, event_log=events)
    task = Task(instruction="给张三发消息", budget=TaskBudget(max_action_steps=5))
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    kinds = events.kinds(task.id)
    assert "effect_unknown" in kinds, "拿不到验证观察必须落成「效果未知」"
    assert "reconciled" in kinds, "下一个安全点必须就地把它对掉"
    reconciled = [e for e in events.read(task.id) if e.kind == "reconciled"][-1]
    assert reconciled.data["verdict"] == "continue", "页面已变，应判定上次动作已生效"
    assert reconciled.data["online"] is True
    assert outcome is RunOutcome.DONE


def test_effect_unknown_reconcile_redoes_the_action_once(monkeypatch, tmp_path):
    """页面毫无变化 → 判定上次动作没生效 → 原样重做一次，而不是让模型另做别的。"""
    patch_observe_sequence(monkeypatch, tmp_path, [PATCHED_UI_BEFORE], fail_post_times=1)
    patch_planner(
        monkeypatch,
        goals=["点确定"],
        decisions=[tap(10, 20), Decision(action=Action(type=ActionType.DONE, reason="完成"))],
    )
    patch_verifier(monkeypatch)

    executed: list[Action] = []

    def fake_execute(device, action, ui_tree):
        executed.append(action)
        return {"ok": True}

    monkeypatch.setattr(runtime_mod.executor, "execute", fake_execute)

    events = EventLog(tmp_path / "events")
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    session, runtime = build(tmp_path, checkpoints=checkpoints, event_log=events)
    task = Task(instruction="点确定", budget=TaskBudget(max_action_steps=5))
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.DONE
    assert len(executed) == 2, "对账判定未生效后应重做一次"
    assert executed[0].is_same_as(executed[1])
    reconciled = [e for e in events.read(task.id) if e.kind == "reconciled"][-1]
    assert reconciled.data["verdict"] == "retry"


def test_dangerous_effect_unknown_never_auto_retries(monkeypatch, tmp_path):
    """危险动作效果未知时绝不自动重做——宁可多问一次人，也不能重复扣款。"""
    patch_observe_sequence(monkeypatch, tmp_path, [PATCHED_UI_BEFORE], fail_post_times=1)
    danger = Action(type=ActionType.TAP, value="确认付款", target=Point(x=10, y=20))
    # 两次给同一个动作：第一次撞 HITL，批准后第二次才真正执行
    patch_planner(
        monkeypatch, goals=["付款"], decisions=[Decision(action=danger), Decision(action=danger)]
    )
    patch_verifier(monkeypatch)

    executed: list[Action] = []

    def fake_execute(device, action, ui_tree):
        executed.append(action)
        return {"ok": True}

    monkeypatch.setattr(runtime_mod.executor, "execute", fake_execute)

    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    session, runtime = build(tmp_path, checkpoints=checkpoints)
    task = Task(instruction="去付款", budget=TaskBudget(max_action_steps=5))
    session.acquire(task.id)

    # 第一次 run：危险动作先要人工确认，批准后才执行
    assert runtime.run(task) is RunOutcome.AWAITING_CONFIRMATION
    assert runtime.confirm(task.id, approved=True)
    outcome = runtime.run(task)
    session.release(task.id)

    assert outcome is RunOutcome.AWAITING_CONFIRMATION, "页面无变化 + 危险动作 → 必须转人工"
    assert len(executed) == 1, "绝不能自动重做危险动作"


# ---------------------------------------------------------------- V2.2 §四：完成申请


def test_completion_claim_is_rejected_then_confirmed_with_evidence(monkeypatch, tmp_path):
    """模型的完成申请被独立验证驳回后，换一种方式继续做，最终凭可核验证据完成。"""
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["第一步", "第二步"],
        # 第一次什么都不说就想收工；被驳回后 Re-plan 给出可核验的完成声明
        decisions=[
            tap(1, 1),
            Decision(action=Action(type=ActionType.DONE_REQUEST, reason="", goal_evidence={})),
        ],
        replans=[
            tap(9, 9),
            Decision(
                action=Action(
                    type=ActionType.DONE_REQUEST,
                    reason="已回到设置页",
                    goal_evidence={"package": "com.android.settings"},
                )
            ),
        ],
    )
    patch_verifier(monkeypatch)

    events = EventLog(tmp_path / "events")
    session, runtime = build(tmp_path, event_log=events)
    task = Task(instruction="改设置", budget=TaskBudget(max_action_steps=10))
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    kinds = events.kinds(task.id)
    assert "goal_requested" in kinds
    assert "goal_rejected" in kinds, "既没走完计划、页面也没推进、还没给理由 → 必须驳回"
    assert "goal_confirmed" in kinds, "给出可核验且命中的声明后应当被确认"
    assert outcome is RunOutcome.DONE
    assert task.status is TaskStatus.DONE

    done_event = [e for e in events.read(task.id) if e.kind == "done"][-1]
    assert done_event.data["goal_independent_evidence"] is True
    assert done_event.data["goal_rejections"] == 1


def test_repeated_completion_rejection_goes_to_human(monkeypatch, tmp_path):
    """连续被驳回且无法自行推翻时交给人裁定，而不是无限重试。"""
    monkeypatch.setattr(runtime_mod.goal_verifier, "MAX_GOAL_REJECTIONS", 0)
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["第一步"],
        decisions=[
            tap(1, 1),
            Decision(action=Action(type=ActionType.DONE_REQUEST, reason="")),
            Decision(action=Action(type=ActionType.DONE_REQUEST, reason="人工已认定完成")),
        ],
    )
    patch_verifier(monkeypatch)

    session, runtime = build(tmp_path)
    task = Task(instruction="改设置", budget=TaskBudget(max_action_steps=10))
    session.acquire(task.id)

    assert runtime.run(task) is RunOutcome.AWAITING_CONFIRMATION
    assert runtime.is_goal_decision(task.id), "待处理的应当是「任务算不算完成」，不是某个动作"
    assert task.status is TaskStatus.WAITING

    # 人工认定完成 → 下一次完成申请直接放行
    assert runtime.confirm(task.id, approved=True)
    outcome = runtime.run(task)
    session.release(task.id)

    assert outcome is RunOutcome.DONE
    assert task.status is TaskStatus.DONE


def test_human_denial_of_completion_keeps_the_task_alive(monkeypatch, tmp_path):
    """人工说「还没做完」= 继续做，不能把任务判死。"""
    monkeypatch.setattr(runtime_mod.goal_verifier, "MAX_GOAL_REJECTIONS", 0)
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["第一步"],
        decisions=[
            tap(1, 1),
            Decision(action=Action(type=ActionType.DONE_REQUEST, reason="")),
            Decision(action=Action(type=ActionType.DONE_REQUEST, reason="这次真的做完了")),
        ],
        replans=[tap(7, 7)],
    )
    patch_verifier(monkeypatch)

    session, runtime = build(tmp_path)
    task = Task(instruction="改设置", budget=TaskBudget(max_action_steps=10))
    session.acquire(task.id)

    assert runtime.run(task) is RunOutcome.AWAITING_CONFIRMATION
    assert runtime.confirm(task.id, approved=False)
    assert not runtime.is_goal_decision(task.id)
    assert task.status is not TaskStatus.FAILED, "否决完成 ≠ 放弃任务"

    outcome = runtime.run(task)
    session.release(task.id)

    # 被塞了 Re-plan 理由，下一轮会先换一种做法——而不是原地重复同一个完成申请
    assert outcome is not RunOutcome.CANCELLED
    assert task.status is not TaskStatus.FAILED


# ---------------------------------------------------------------- V2.2 §一：风险门禁接入闭环


def test_runtime_blocks_action_whose_target_node_is_dangerous(monkeypatch, tmp_path):
    """动作描述人畜无害，但点到的控件是「立即购买」——门禁必须拦下来等人工确认。"""
    ui = (
        '<hierarchy><node class="android.widget.Button" text="立即购买" '
        'bounds="[100,200][300,300]" clickable="true"/></hierarchy>'
    )
    patch_observe_ui(monkeypatch, tmp_path, ui)
    patch_planner(
        monkeypatch,
        goals=["买东西"],
        decisions=[Decision(action=Action(type=ActionType.TAP, target=Point(x=200, y=250)))],
    )
    patch_verifier(monkeypatch)

    executed: list[Action] = []
    monkeypatch.setattr(
        runtime_mod.executor,
        "execute",
        lambda device, action, ui_tree: (executed.append(action), {"ok": True})[1],
    )

    events = EventLog(tmp_path / "events")
    session, runtime = build(tmp_path, event_log=events)
    task = Task(instruction="买东西", budget=TaskBudget(max_action_steps=5))
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.AWAITING_CONFIRMATION
    assert executed == [], "确认之前不得执行"
    assessed = [e for e in events.read(task.id) if e.kind == "risk_assessed"][-1]
    assert assessed.data["effective"] == "dangerous"
    assert "立即购买" in assessed.data["reason"]


def test_runtime_records_model_risk_downgrade_attempt(monkeypatch, tmp_path):
    """模型试图把危险动作说成 safe：既不生效，也要在事件流里留痕。"""
    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["付款"],
        decisions=[
            Decision(
                action=Action(
                    type=ActionType.TAP,
                    value="确认付款",
                    target=Point(x=5, y=5),
                    risk_hint=ActionRisk.SAFE,
                )
            )
        ],
    )
    patch_verifier(monkeypatch)

    events = EventLog(tmp_path / "events")
    session, runtime = build(tmp_path, event_log=events)
    task = Task(instruction="付款", budget=TaskBudget(max_action_steps=5))
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.AWAITING_CONFIRMATION, "降级声明不能让它静默执行"
    assessed = [e for e in events.read(task.id) if e.kind == "risk_assessed"][-1]
    assert assessed.data["downgrade_blocked"] is True
    assert assessed.data["policy"] == "dangerous"
    assert assessed.data["model"] == "safe"
