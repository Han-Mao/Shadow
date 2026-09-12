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
from models.task import TaskStatus
from storage import CheckpointStore, TaskStore, TrajectoryStore


@pytest.fixture
def api(tmp_path, monkeypatch):
    """重建 API 的依赖装配，全部指向临时目录与假设备。"""
    from fastapi.testclient import TestClient

    from api import server

    session = DeviceSession(FakeDevice(), serial="fake-serial")
    task_store = TaskStore(tmp_path / "tasks")
    checkpoint_store = CheckpointStore(tmp_path / "checkpoints")
    trajectory = TrajectoryStore()

    runtime = AgentRuntime(
        session,
        artifact_dir=tmp_path,
        trajectory=trajectory,
        checkpoints=checkpoint_store,
        task_store=task_store,
    )
    scheduler = TaskScheduler(runtime, session, task_store=task_store, idle_poll_seconds=0.01)
    manager = TaskManager(store=task_store, scheduler=scheduler, classifier=TaskClassifier())

    monkeypatch.setattr(server, "session", session)
    monkeypatch.setattr(server, "task_store", task_store)
    monkeypatch.setattr(server, "checkpoint_store", checkpoint_store)
    monkeypatch.setattr(server, "trajectory", trajectory)
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
    assert body["status"] == "queued"
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


def test_inject_subtask_merges_into_running_task(api):
    client, _, session, *_ = api
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

        detail = client.get(f"/tasks/{created['id']}").json()
        goals = [step["goal"] for step in detail["plan"]]
        assert "先帮我查东京酒店" in goals
    finally:
        session.release("__manual__")


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
