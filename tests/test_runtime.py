"""AgentRuntime：闭环执行、异常收敛、死循环检测、危险动作门禁、Checkpoint 恢复。"""
from __future__ import annotations

import pytest

import agent.runtime as runtime_mod
from agent.runtime import AgentRuntime, RunOutcome
from agent.verifier import Verification
from device.adb import AdbError
from device.session import DeviceSession
from fakes import FakeDevice
from models.action import Action, ActionEffectStatus, ActionType, Decision, Point
from models.budget import TaskBudget
from models.checkpoint import Checkpoint
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


# ---------------------------------------------------------------- V2.1：HITL（§25 scenario 10）


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
