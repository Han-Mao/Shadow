"""API 契约：任务编排端点、状态码语义、错误脱敏。"""
from __future__ import annotations

import pytest

import agent.runtime as runtime_mod
from agent.classifier import TaskClassifier
from agent.runtime import AgentRuntime
from agent.scheduler import TaskScheduler
from agent.task_manager import TaskManager
from device.session import DeviceSession
from fakes import FakeDevice
from models.action import Action, ActionType, Decision
from models.state import Observation
from models.task import PAUSED_BY_PREEMPTION, PAUSED_BY_USER, Task, TaskStatus
from models.task_relation import TaskRelation, TaskRelationResult
from models.task_step import StepStatus
from storage import CheckpointStore, EventLog, TaskStore, TrajectoryStore


@pytest.fixture
def api(tmp_path, monkeypatch):
    """重建 API 的依赖装配，全部指向临时目录与假设备。"""
    from fastapi.testclient import TestClient

    from api import server

    session = DeviceSession(FakeDevice(), serial="fake-serial")
    task_store = TaskStore(tmp_path / "tasks")
    checkpoint_store = CheckpointStore(tmp_path / "checkpoints")
    trajectory = TrajectoryStore(root=tmp_path / "trajectories")

    event_log = EventLog(tmp_path / "events")

    runtime = AgentRuntime(
        session,
        artifact_dir=tmp_path,
        trajectory=trajectory,
        checkpoints=checkpoint_store,
        task_store=task_store,
        event_log=event_log,
    )
    scheduler = TaskScheduler(
        runtime, session, task_store=task_store, idle_poll_seconds=0.01, event_log=event_log
    )
    manager = TaskManager(store=task_store, scheduler=scheduler, classifier=TaskClassifier())

    monkeypatch.setattr(server, "session", session)
    monkeypatch.setattr(server, "task_store", task_store)
    monkeypatch.setattr(server, "checkpoint_store", checkpoint_store)
    monkeypatch.setattr(server, "trajectory", trajectory)
    monkeypatch.setattr(server, "event_log", event_log)
    monkeypatch.setattr(server, "runtime", runtime)
    monkeypatch.setattr(server, "scheduler", scheduler)
    monkeypatch.setattr(server, "manager", manager)

    client = TestClient(server.app, raise_server_exceptions=False)
    scheduler.start()
    try:
        yield client, server, session, scheduler, manager
    finally:
        scheduler.stop()


def stub_loop(monkeypatch, tmp_path, *, done_reason="完成"):
    """让 runtime 立刻完成任务：不碰设备、不调 VLM。"""

    def fake_observe(adb, artifact_dir, step, suffix=""):
        return Observation(
            step=step,
            screenshot_path=str(tmp_path / f"step_{step:03d}.png"),
            package="com.android.settings",
            activity=".Main",
        )

    monkeypatch.setattr(runtime_mod.observer, "observe", fake_observe)
    monkeypatch.setattr(runtime_mod.planner, "generate_plan", lambda *a, **k: ["一步到位"])
    monkeypatch.setattr(
        runtime_mod.planner,
        "plan_next_action",
        lambda *a, **k: Decision(action=Action(type=ActionType.DONE, reason=done_reason)),
    )


def wait_terminal(client, task_id: str, attempts: int = 300) -> dict:
    import time

    payload = {}
    for _ in range(attempts):
        payload = client.get(f"/tasks/{task_id}").json()
        if payload["status"] in {"done", "failed", "cancelled"}:
            return payload
        time.sleep(0.02)
    return payload


# ---------------------------------------------------------------- 创建与查询


def test_create_task_defaults_to_background(api):
    client, *_ = api
    resp = client.post("/tasks", json={"instruction": "打开设置"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "background"
    assert body["id"]
    # 后台模式不等结果，任务立刻入队——而 worker 可能已经把它取走开跑了。
    # 所以这里只断言「已进入调度」，不去赌线程时序：
    # 真想要终态请查 /tasks/{id}，或用 wait=True。
    assert body["status"] in {"queued", "running"}
    assert body["priority"] == "normal"


def test_create_task_wait_returns_terminal_state(api, monkeypatch, tmp_path):
    client, *_ = api
    stub_loop(monkeypatch, tmp_path)

    resp = client.post("/tasks", json={"instruction": "打开设置", "wait": True, "max_steps": 3})

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "sync"
    assert body["status"] == "done"
    assert body["plan"][0]["goal"] == "一步到位"
    assert body["plan"][0]["status"] == "done"


def test_task_response_includes_progress_and_plan_states(api, monkeypatch, tmp_path):
    client, *_ = api
    stub_loop(monkeypatch, tmp_path)

    created = client.post("/tasks", json={"instruction": "打开设置", "wait": True}).json()
    detail = client.get(f"/tasks/{created['id']}").json()

    assert detail["plan_progress"] == "1/1 done"
    assert detail["plan"][0]["status"] == "done"


def test_get_unknown_task_returns_404(api):
    client, *_ = api
    resp = client.get("/tasks/does-not-exist")
    assert resp.status_code == 404


def test_list_tasks(api):
    client, *_ = api
    client.post("/tasks", json={"instruction": "任务一"})
    client.post("/tasks", json={"instruction": "任务二"})

    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert len(resp.json()["tasks"]) == 2


# ---------------------------------------------------------------- 生命周期


def test_pause_resume_cancel_flow(api):
    client, server, session, scheduler, manager = api

    # 先占住设备再建任务：任务会停在队列/等设备状态，pause 可以立即生效。
    # （对真正执行中的任务，暂停是异步的——要等 runtime 跑到下一个安全点）
    session.acquire("__manual__")
    try:
        created = client.post("/tasks", json={"instruction": "长任务"}).json()
        task_id = created["id"]

        assert client.post(f"/tasks/{task_id}/pause").status_code == 200
        assert client.get(f"/tasks/{task_id}").json()["status"] == "paused"

        assert client.post(f"/tasks/{task_id}/resume").status_code == 200
        assert client.get(f"/tasks/{task_id}").json()["status"] == "queued"

        assert client.post(f"/tasks/{task_id}/cancel").status_code == 200
        assert client.get(f"/tasks/{task_id}").json()["status"] == "cancelled"
    finally:
        session.release("__manual__")


def test_lifecycle_on_unknown_task_returns_404(api):
    client, *_ = api
    for action in ("pause", "resume", "cancel"):
        assert client.post(f"/tasks/nope/{action}").status_code == 404


def test_cancel_finished_task_returns_409(api, monkeypatch, tmp_path):
    client, *_ = api
    stub_loop(monkeypatch, tmp_path)
    created = client.post("/tasks", json={"instruction": "打开设置", "wait": True}).json()

    assert client.post(f"/tasks/{created['id']}/cancel").status_code == 409


# ---------------------------------------------------------------- 指令注入


def test_inject_subtask_merges_into_running_task(api, monkeypatch):
    """SUBTASK 被并入当前任务计划。

    V2.1 §九：并入计划要求 0.65 的置信度。纯规则最高只到约 0.60
    （融合上限 = (0.3*0.75 + 0.1*sim)/0.4），所以这里点亮 LLM 层，
    对应生产环境配了 VLM 的情形。
    """
    client, _, session, _, manager = api
    monkeypatch.setattr(
        manager,
        "_classifier",
        TaskClassifier(
            llm_judge=lambda instruction, current: TaskRelationResult(
                relation=TaskRelation.SUBTASK, confidence=0.9, reason="东京三日游的一部分"
            )
        ),
    )
    # 占住设备，让任务停在队列里；否则 worker 立刻跑它，假设备必然观察失败 → 任务已终态
    session.acquire("__manual__")
    try:
        created = client.post("/tasks", json={"instruction": "帮我规划东京三日游"}).json()

        resp = client.post(
            f"/tasks/{created['id']}/inject", json={"instruction": "先帮我查东京酒店"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "merged"
        assert body["relation"] == "subtask"
        assert body["confidence"] >= 0.65

        detail = client.get(f"/tasks/{created['id']}").json()
        goals = [step["goal"] for step in detail["plan"]]
        assert "先帮我查东京酒店" in goals
    finally:
        session.release("__manual__")


def test_inject_subtask_migrates_dependency_chain(api, monkeypatch):
    """Scenario 9：SUBTASK 插入后，串行依赖链必须迁移到新步骤上。

    只断言「步骤被插进来了」是不够的——真正会出错的是依赖：
    后续步骤若仍依赖被新步骤绕开的旧前驱，计划要么卡死，要么执行顺序错乱。
    """
    client, server, session, _, manager = api
    monkeypatch.setattr(
        manager,
        "_classifier",
        TaskClassifier(
            llm_judge=lambda instruction, current: TaskRelationResult(
                relation=TaskRelation.SUBTASK, confidence=0.9, reason="东京行程的一部分"
            )
        ),
    )
    session.acquire("__manual__")
    try:
        created = client.post("/tasks", json={"instruction": "帮我规划东京三日游"}).json()
        task = manager.get(created["id"])
        task.set_plan(["查机票", "订酒店"])
        # 第一步已完成 → 新步骤插到「下一个待执行步骤」之前，也就是 index 1
        task.plan[0].mark(StepStatus.DONE)
        server.task_store.save(task)

        resp = client.post(
            f"/tasks/{created['id']}/inject", json={"instruction": "先帮我查东京酒店"}
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "merged"

        plan = manager.get(created["id"]).plan
        assert [s.goal for s in plan] == ["查机票", "先帮我查东京酒店", "订酒店"]

        inserted, following = plan[1], plan[2]
        assert inserted.depends_on == [plan[0].id], "新步骤必须接上原本的前驱"
        assert following.depends_on == [inserted.id], "原后续步骤必须改为依赖新插入的步骤"
    finally:
        session.release("__manual__")


def test_inject_subtask_below_threshold_does_not_touch_running_plan(api):
    """够不到 SUBTASK 门槛时，宁可另起任务，也不擅自改动正在跑的计划。

    没有 LLM 时纯规则融合约 0.60 < 0.65，属「证据不够」：
    悄悄往用户正在执行的计划里插一步，比各跑各的风险高。
    """
    client, _, session, *_ = api
    session.acquire("__manual__")
    try:
        created = client.post("/tasks", json={"instruction": "帮我规划东京三日游"}).json()

        resp = client.post(
            f"/tasks/{created['id']}/inject", json={"instruction": "先帮我查东京酒店"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["relation"] == "subtask"
        assert body["action"] != "merged"
        assert body["action"] in {"spawned", "preempted"}

        detail = client.get(f"/tasks/{created['id']}").json()
        goals = [step["goal"] for step in detail["plan"]]
        assert "先帮我查东京酒店" not in goals, "证据不足时不该动用户的计划"
    finally:
        session.release("__manual__")


def test_events_endpoint_returns_audit_stream(api):
    """/events 返回只追加的审计事件流（V2.1 §二十三）。

    与 /history 的区别：history 是给下一步决策看的观察轨迹（会被裁剪），
    events 是给事后追溯看的事件流（抢占、对账、失败原因都在里面）。
    """
    client, _, session, *_ = api
    session.acquire("__manual__")  # 占住设备，任务停在队列里，避免后台线程干扰断言
    try:
        created = client.post("/tasks", json={"instruction": "打开设置"}).json()

        resp = client.get(f"/tasks/{created['id']}/events")
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == created["id"]
        assert body["count"] >= 1
        kinds = [e["kind"] for e in body["events"]]
        assert "queued" in kinds
    finally:
        session.release("__manual__")


def test_events_endpoint_404_for_unknown_task(api):
    client, *_ = api
    assert client.get("/tasks/does-not-exist/events").status_code == 404


def test_replay_endpoint_returns_timeline_and_plan(api):
    """/replay 把事件流按时间轴读回来，并附带动作计划（V2.1 §二十三）。"""
    client, _, session, *_ = api
    session.acquire("__manual__")  # 占住设备，任务停在队列，保证事件稳定
    try:
        created = client.post("/tasks", json={"instruction": "打开设置"}).json()

        resp = client.get(f"/tasks/{created['id']}/replay")
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == created["id"]
        assert any(f["kind"] == "queued" for f in body["frames"])
        assert all("offset_seconds" in f for f in body["frames"])
        assert body["plan"]["count"] == 0, "还没跑动作，计划应为空"
    finally:
        session.release("__manual__")


def test_replay_endpoint_supports_markdown_report(api):
    client, _, session, *_ = api
    session.acquire("__manual__")
    try:
        created = client.post("/tasks", json={"instruction": "打开设置"}).json()

        resp = client.get(f"/tasks/{created['id']}/replay?format=markdown")
        assert resp.status_code == 200
        assert "## 时间轴" in resp.text
        assert "## 值得注意的地方" in resp.text
    finally:
        session.release("__manual__")


def test_replay_endpoint_404_for_unknown_task(api):
    client, *_ = api
    assert client.get("/tasks/does-not-exist/replay").status_code == 404


def test_inject_unrelated_spawns_new_task(api):
    client, *_ = api
    created = client.post("/tasks", json={"instruction": "在淘宝搜索黑色运动鞋"}).json()

    resp = client.post(
        f"/tasks/{created['id']}/inject", json={"instruction": "今天杭州天气怎么样"}
    )
    body = resp.json()
    assert body["action"] in {"spawned", "preempted"}
    assert body["task_id"] != created["id"]
    assert client.get(f"/tasks/{body['task_id']}").status_code == 200


def test_inject_duplicate_is_ignored(api):
    client, _, session, *_ = api
    session.acquire("__manual__")
    try:
        created = client.post("/tasks", json={"instruction": "在淘宝搜索黑色运动鞋"}).json()

        resp = client.post(
            f"/tasks/{created['id']}/inject", json={"instruction": "在淘宝搜索黑色运动鞋"}
        )
        body = resp.json()
        assert body["action"] == "duplicate_ignored"
        assert body["task_id"] == created["id"]
    finally:
        session.release("__manual__")


def test_inject_on_unknown_task_returns_404(api):
    client, *_ = api
    assert client.post("/tasks/nope/inject", json={"instruction": "x"}).status_code == 404


# ---------------------------------------------------------------- 调度与检查点


def test_scheduler_endpoint_shape(api):
    client, *_ = api
    resp = client.get("/scheduler")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"running", "ready", "paused", "suspended", "completed", "device"}
    assert "owner" in body["device"]


def test_checkpoint_endpoint_404_before_first_checkpoint(api):
    client, *_ = api
    created = client.post("/tasks", json={"instruction": "x"}).json()
    assert client.get(f"/tasks/{created['id']}/checkpoint").status_code == 404


def test_checkpoint_endpoint_after_run(api, monkeypatch, tmp_path):
    client, *_ = api
    stub_loop(monkeypatch, tmp_path)
    created = client.post("/tasks", json={"instruction": "打开设置", "wait": True}).json()

    resp = client.get(f"/tasks/{created['id']}/checkpoint")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == created["id"]
    assert "step_states" in body


def test_history_endpoint(api, monkeypatch, tmp_path):
    client, *_ = api
    stub_loop(monkeypatch, tmp_path)
    created = client.post("/tasks", json={"instruction": "打开设置", "wait": True}).json()

    resp = client.get(f"/tasks/{created['id']}/history")
    assert resp.status_code == 200
    assert resp.json()["count"] >= 0


# ---------------------------------------------------------------- 单步端点与状态码


def test_actions_rejects_unknown_type(api):
    client, *_ = api
    resp = client.post("/actions", json={"type": "fly"})
    assert resp.status_code == 400
    assert "fly" in resp.json()["detail"]


def test_actions_rejects_invalid_target_before_touching_device(api):
    """非法 target 在契约层就被拒（422），不会先拍截图、碰真机再报参数错。"""
    client, *_ = api
    resp = client.post("/actions", json={"type": "tap", "target": {"x": "abc", "y": 1}})
    assert resp.status_code == 422


def test_manual_endpoints_report_device_busy(api):
    client, _, session, *_ = api
    session.acquire("__manual__")
    try:
        assert client.post("/tap", json={"x": 1, "y": 2}).status_code == 409
        assert client.post("/back").status_code == 409
    finally:
        session.release("__manual__")


def test_unhandled_error_is_redacted_by_default(api, monkeypatch):
    client, server, *_ = api

    def boom(*args, **kwargs):
        raise RuntimeError("设备层炸了")

    monkeypatch.setattr(server, "_DEBUG_ERRORS", False)
    monkeypatch.setattr(server.adb, "state", boom)

    resp = client.get("/devices")
    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert "设备层炸了" not in body["error"]


def test_unhandled_error_can_expose_detail_in_debug(api, monkeypatch):
    client, server, *_ = api

    def boom(*args, **kwargs):
        raise RuntimeError("设备层炸了")

    monkeypatch.setattr(server, "_DEBUG_ERRORS", True)
    monkeypatch.setattr(server.adb, "state", boom)

    resp = client.get("/devices")
    assert "设备层炸了" in resp.json()["error"]


def test_wait_timeout_returns_504(api):
    """设备一直被占着 → 任务永远跑不完 → wait 必须超时返回，而不是无限等。"""
    client, _, session, *_ = api
    session.acquire("__manual__")
    try:
        resp = client.post(
            "/tasks",
            json={"instruction": "永远跑不完", "wait": True, "wait_timeout": 1},
        )
        assert resp.status_code == 504
        assert "轮询" in resp.json()["detail"]
    finally:
        session.release("__manual__")


# ---------------------------------------------------------------- 启动恢复


def test_lifespan_recovers_unfinished_tasks(tmp_path, monkeypatch):
    """服务重启：磁盘上留着未完成的任务时，lifespan 必须把它拉回队列并跑完。

    没有这段逻辑的话，任务会停在磁盘上没人执行——从 /tasks/{id} 看还「活着」，
    但永远不会有进展（§23 第七阶段）。
    """
    import time

    from fastapi.testclient import TestClient

    from api import server

    store = TaskStore(tmp_path / "tasks")
    task = Task(instruction="上次被抢占后没跑完")
    task.set_plan(["打开淘宝"])
    task.mark(TaskStatus.PAUSED, paused_reason=PAUSED_BY_PREEMPTION)
    store.save(task)

    session = DeviceSession(FakeDevice(), serial="fake-serial")
    runtime = AgentRuntime(
        session,
        artifact_dir=tmp_path,
        trajectory=TrajectoryStore(),
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        task_store=store,
    )
    scheduler = TaskScheduler(runtime, session, task_store=store, idle_poll_seconds=0.01)
    manager = TaskManager(store=store, scheduler=scheduler, classifier=TaskClassifier())

    monkeypatch.setattr(server, "session", session)
    monkeypatch.setattr(server, "task_store", store)
    monkeypatch.setattr(server, "runtime", runtime)
    monkeypatch.setattr(server, "scheduler", scheduler)
    monkeypatch.setattr(server, "manager", manager)
    stub_loop(monkeypatch, tmp_path)

    # 进入 context manager 才会触发 lifespan（scheduler.start → recover）
    with TestClient(server.app) as client:
        payload: dict = {}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            payload = client.get(f"/tasks/{task.id}").json()
            if payload["status"] in {"done", "failed"}:
                break
            time.sleep(0.05)

    assert payload["status"] == "done", "重启后未完成的任务必须被自动执行完"


def test_lifespan_does_not_resume_user_paused_tasks(tmp_path, monkeypatch):
    """用户显式暂停的任务，重启后应保持暂停。"""
    import time

    from fastapi.testclient import TestClient

    from api import server

    store = TaskStore(tmp_path / "tasks")
    task = Task(instruction="用户暂停的任务")
    task.mark(TaskStatus.PAUSED, paused_reason=PAUSED_BY_USER)
    store.save(task)

    session = DeviceSession(FakeDevice(), serial="fake-serial")
    runtime = AgentRuntime(session, artifact_dir=tmp_path, task_store=store)
    scheduler = TaskScheduler(runtime, session, task_store=store, idle_poll_seconds=0.01)
    manager = TaskManager(store=store, scheduler=scheduler, classifier=TaskClassifier())

    monkeypatch.setattr(server, "session", session)
    monkeypatch.setattr(server, "task_store", store)
    monkeypatch.setattr(server, "runtime", runtime)
    monkeypatch.setattr(server, "scheduler", scheduler)
    monkeypatch.setattr(server, "manager", manager)
    stub_loop(monkeypatch, tmp_path)

    with TestClient(server.app) as client:
        time.sleep(0.3)  # 给 worker 充分机会去「多管闲事」
        payload = client.get(f"/tasks/{task.id}").json()
        scheduler_state = client.get("/scheduler").json()

    assert payload["status"] == "paused"
    assert task.id in scheduler_state["paused"]
    assert scheduler_state["running"] is None


# ---------------------------------------------------------------- V2.1：Risk Gate 统一（§十 / §十一）


def test_actions_blocks_dangerous_without_confirmation(api):
    """/actions 直控端点也必须过 Risk Gate：危险动作不能绕过 HITL 直接执行。"""
    client, *_ = api
    resp = client.post("/actions", json={"type": "tap", "target": "确认付款"})
    assert resp.status_code == 403
    assert "人工确认" in resp.json()["detail"]


def test_model_cannot_downgrade_dangerous_risk():
    """模型在 action 上声明 risk=safe，不能把危险动作降权（V2.1 §十一）。"""
    from models.action import ActionRisk, Point

    action = Action(
        type=ActionType.TAP,
        target=Point(x=1, y=1),
        value="确认付款",
        risk=ActionRisk.SAFE,
    )
    assert action.resolved_risk() is ActionRisk.DANGEROUS


# ---------------------------------------------------------------- V2.1：SUPER_TASK 真正重构（§六/§七/§25 scenario 7·8）


def test_inject_super_task_rewrites_task_goal_and_bumps_version(api, monkeypatch):
    """SUPER_TASK 把新指令改写成当前任务目标，version+1，旧计划与旧 Checkpoint 失效。

    之前这里只是新建一个任务（create），根本没重构当前任务——正是建议指出的最大缺口。
    """
    client, server, session, scheduler, manager = api

    class _SuperTaskClassifier:
        def classify(self, instruction, *, current=None, candidates=()):
            return TaskRelationResult(
                relation=TaskRelation.SUPER_TASK,
                confidence=0.9,
                affected_task_id=current.id if current else None,
                reason="改成搜索高铁",
            )

    monkeypatch.setattr(manager, "_classifier", _SuperTaskClassifier())

    session.acquire("__manual__")  # 让任务停在队列，便于断言
    try:
        created = client.post("/tasks", json={"instruction": "在淘宝搜索北京到上海的机票"}).json()
        task = manager.get(created["id"])
        task.set_plan(["查机票", "比价格"])
        task.version = 1

        resp = client.post(
            f"/tasks/{created['id']}/inject",
            json={"instruction": "改成搜索高铁", "allow_disruptive": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "superseded"
        assert body["relation"] == "super_task"

        reloaded = manager.get(created["id"])
        assert reloaded.version == 2, "SUPER_TASK 必须 version+1"
        assert reloaded.plan == [], "旧计划必须失效"
        assert reloaded.checkpoint_id is None, "旧 Checkpoint 必须失效"
        assert reloaded.instruction == "改成搜索高铁", "目标必须被改写"
    finally:
        session.release("__manual__")


def test_inject_super_task_needs_confirmation_without_explicit_allow(api, monkeypatch):
    """改写别人正在跑的任务目标不可逆，必须调用方显式放行（V2.1 §九）。

    没带 allow_disruptive 时应当原样返回 needs_confirmation，
    **一个字都不能改**——否则「疑似父任务」就会把用户原来的目标冲掉。
    """
    client, _, session, _, manager = api

    class _SuperTaskClassifier:
        def classify(self, instruction, *, current=None, candidates=()):
            return TaskRelationResult(
                relation=TaskRelation.SUPER_TASK,
                confidence=0.9,
                affected_task_id=current.id if current else None,
                reason="改成搜索高铁",
            )

    monkeypatch.setattr(manager, "_classifier", _SuperTaskClassifier())

    session.acquire("__manual__")
    try:
        created = client.post("/tasks", json={"instruction": "在淘宝搜索机票"}).json()
        task = manager.get(created["id"])
        task.set_plan(["查机票"])
        task.version = 1

        resp = client.post(
            f"/tasks/{created['id']}/inject", json={"instruction": "改成搜索高铁"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "needs_confirmation"
        assert body["requires_confirmation"] is True

        reloaded = manager.get(created["id"])
        assert reloaded.instruction == "在淘宝搜索机票", "未放行时目标不得被改写"
        assert reloaded.version == 1, "未放行时不得 +1"
        assert [s.goal for s in reloaded.plan] == ["查机票"], "未放行时计划不得变动"
    finally:
        session.release("__manual__")


# ---------------------------------------------------------------- V2.1：Checkpoint 版本校验（§18 / §25 scenario 8）


def test_checkpoint_validate_rejects_stale_version(tmp_path):
    """Checkpoint 版本与当前任务版本不一致 → STALE，不能 resume（防旧指令误执行）。

    即便页面恰好没变，旧计划对应的旧恢复点也绝不能续跑（V2.1 §十八）。
    """
    from models.state import Observation
    from storage.checkpoint_store import CheckpointStore, RestoreVerdict

    store = CheckpointStore(tmp_path / "checkpoints")
    obs = Observation(
        step=1,
        screenshot_path=str(tmp_path / "s.png"),
        package="com.android.settings",
        activity=".Main",
    )
    cp = runtime_mod.Checkpoint.capture(
        task_id="t1", step=1, step_states={}, observation=obs, task_version=1
    )
    store.save(cp)

    # 版本一致、页面一致 → 可以续跑
    assert store.validate(cp, obs, task_version=1) is RestoreVerdict.RESUME
    # 任务已发生 SUPER_TASK（version 升到 2）→ 旧 checkpoint 即使页面相同也必须失效
    assert store.validate(cp, obs, task_version=2) is RestoreVerdict.STALE
