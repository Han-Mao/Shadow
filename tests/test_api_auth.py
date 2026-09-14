"""API 访问控制、设备级权限、人工确认令牌与请求审计（V2.2 §九）。

审核的判断：

> API 提供的东西非常敏感（`/actions`、`/tasks`、`confirm`、`inject` 都能直接
> 影响真实手机）。现在虽然监听 localhost，但只要改成 0.0.0.0 / Docker /
> 反向代理，它就会立即变成高风险入口。尤其是 `/confirm`：
> 不能仅靠知道 task_id 就确认危险动作。

设计上刻意保住「本地开发零配置」：没配令牌 = 不鉴权（其余 300 多个用例照常跑）；
一旦配了令牌，则鉴权、只读、设备范围、确认令牌四层同时生效。
"""
from __future__ import annotations

import pytest

from api import auth

TOKEN = "s3cret-operator-token"
READONLY = "view-only-token"


# ---------------------------------------------------------------- 纯函数部分


def test_loopback_is_recognized():
    for host in ("127.0.0.1", "localhost", "::1"):
        assert auth.loopback_only(host)
    assert not auth.loopback_only("0.0.0.0")


def test_bare_bind_is_refused_without_token(monkeypatch):
    """非回环绑定 + 没有令牌 → 拒绝启动。这是防「图省事改个 HOST 就裸奔上线」的兜底。"""
    monkeypatch.delenv("SHADOW_API_TOKEN", raising=False)
    monkeypatch.delenv("SHADOW_API_READONLY_TOKEN", raising=False)

    refusal = auth.bare_bind_refused("0.0.0.0")

    assert refusal is not None
    assert "SHADOW_API_TOKEN" in refusal
    assert auth.bare_bind_refused("127.0.0.1") is None, "本机使用不该被拦"


def test_bare_bind_allowed_when_token_configured(monkeypatch):
    monkeypatch.setenv("SHADOW_API_TOKEN", TOKEN)

    assert auth.bare_bind_refused("0.0.0.0") is None


def test_token_extraction_accepts_both_forms():
    assert auth.extract_token("Bearer abc", None) == "abc"
    assert auth.extract_token("bearer abc", None) == "abc"
    assert auth.extract_token(None, "abc") == "abc"
    assert auth.extract_token("abc", None) == "abc"
    assert auth.extract_token(None, None) == ""


def test_authentication(monkeypatch):
    monkeypatch.setenv("SHADOW_API_TOKEN", TOKEN)
    monkeypatch.setenv("SHADOW_API_READONLY_TOKEN", READONLY)

    operator = auth.authenticate(f"Bearer {TOKEN}", None)
    viewer = auth.authenticate(None, READONLY)

    assert operator is not None and not operator.read_only
    assert viewer is not None and viewer.read_only
    assert auth.authenticate("Bearer wrong", None) is None
    assert auth.authenticate(None, None) is None


def test_device_scope(monkeypatch):
    monkeypatch.setenv("SHADOW_API_DEVICE_ALLOW", "emu-1,emu-2")
    principal = auth.authenticate(f"Bearer {TOKEN}", None) if False else auth.Principal(
        name="op", devices=frozenset({"emu-1", "emu-2"})
    )

    assert principal.may_use_device("emu-1")
    assert not principal.may_use_device("emu-9")
    assert principal.may_use_device(None), "不指定设备时不做限制"


def test_confirmation_token_is_bound_to_the_action():
    """令牌绑定「任务 + 具体动作 + 任务版本」——换个动作或目标被改写后旧令牌即失效。"""
    token = auth.issue_confirmation_token("t1", "fp-aaa", 1)

    assert auth.verify_confirmation_token(token, "t1", "fp-aaa", 1)
    assert not auth.verify_confirmation_token(token, "t1", "fp-bbb", 1), "换了动作必须失效"
    assert not auth.verify_confirmation_token(token, "t2", "fp-aaa", 1)
    assert not auth.verify_confirmation_token(token, "t1", "fp-aaa", 2), "任务改写后必须失效"
    assert not auth.verify_confirmation_token(None, "t1", "fp-aaa", 1)


# ---------------------------------------------------------------- 端到端部分


@pytest.fixture
def secured_api(tmp_path, monkeypatch):
    """带令牌的 API 环境（依赖装配与 test_api 一致，只是多了鉴权配置）。"""
    from fastapi.testclient import TestClient

    import agent.runtime as runtime_mod
    from agent.classifier import TaskClassifier
    from agent.runtime import AgentRuntime
    from agent.scheduler import TaskScheduler
    from agent.task_manager import TaskManager
    from api import server
    from device.session import DeviceSession
    from fakes import FakeDevice
    from models.action import Action, ActionType, Decision
    from models.state import Observation
    from storage import CheckpointStore, EventLog, TaskStore, TrajectoryStore
    from storage.audit_log import AuditLog

    monkeypatch.setenv("SHADOW_API_TOKEN", TOKEN)
    monkeypatch.setenv("SHADOW_API_READONLY_TOKEN", READONLY)
    monkeypatch.setenv("SHADOW_API_DEVICE_ALLOW", "emu-1")

    session = DeviceSession(FakeDevice(), serial="emu-1")
    task_store = TaskStore(tmp_path / "tasks")
    checkpoint_store = CheckpointStore(tmp_path / "checkpoints")
    trajectory = TrajectoryStore(root=tmp_path / "trajectories")
    event_log = EventLog(tmp_path / "events")
    audit = AuditLog(tmp_path / "audit")

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

    for name, value in (
        ("session", session),
        ("task_store", task_store),
        ("checkpoint_store", checkpoint_store),
        ("trajectory", trajectory),
        ("event_log", event_log),
        ("audit_log", audit),
        ("runtime", runtime),
        ("scheduler", scheduler),
        ("manager", manager),
    ):
        monkeypatch.setattr(server, name, value)

    # 不碰设备：观察与决策都用替身
    monkeypatch.setattr(
        runtime_mod.observer,
        "observe",
        lambda adb, artifact_dir, step, suffix="": Observation(
            step=step,
            screenshot_path=str(tmp_path / f"s{step}.png"),
            package="com.android.settings",
            activity=".Main",
        ),
    )
    monkeypatch.setattr(runtime_mod.planner, "generate_plan", lambda *a, **k: ["一步到位"])
    monkeypatch.setattr(
        runtime_mod.planner,
        "plan_next_action",
        lambda *a, **k: Decision(action=Action(type=ActionType.DONE, reason="完成")),
    )

    client = TestClient(server.app, raise_server_exceptions=False)
    scheduler.start()
    try:
        yield client, server, session, scheduler, manager, runtime, audit, runtime_mod
    finally:
        scheduler.stop()


def _headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_request_without_token_is_rejected(secured_api):
    client, *_ = secured_api

    resp = client.get("/tasks")

    assert resp.status_code == 401
    assert "未授权" in resp.json()["error"]


def test_request_with_wrong_token_is_rejected(secured_api):
    client, *_ = secured_api

    resp = client.get("/tasks", headers=_headers("not-the-token"))

    assert resp.status_code == 401


def test_valid_token_is_accepted(secured_api):
    client, *_ = secured_api

    resp = client.get("/tasks", headers=_headers())

    assert resp.status_code == 200


def test_readonly_token_cannot_touch_the_device(secured_api):
    """只读令牌能看，不能点——否则「只读」两个字毫无意义。"""
    client, *_ = secured_api

    assert client.get("/tasks", headers=_headers(READONLY)).status_code == 200
    resp = client.post("/tap", json={"x": 1, "y": 2}, headers=_headers(READONLY))

    assert resp.status_code == 403
    assert "只读" in resp.json()["error"]


def test_health_is_public(secured_api):
    client, *_ = secured_api

    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["auth"] == "token"


def test_device_scope_is_enforced(secured_api):
    """令牌只能操作被授权的设备。"""
    client, *_ = secured_api

    ok = client.post("/tasks", json={"instruction": "x", "device_serial": "emu-1"}, headers=_headers())
    denied = client.post("/tasks", json={"instruction": "x", "device_serial": "emu-9"}, headers=_headers())

    assert ok.status_code == 200
    assert denied.status_code == 403
    assert "emu-9" in denied.json()["detail"]


def test_confirm_needs_a_confirmation_token(secured_api, monkeypatch):
    """知道 task_id 不等于有权放行危险动作（审核原话）。"""
    import time

    from models.action import Action, ActionType, Decision

    client, server, session, scheduler, manager, runtime, audit, runtime_mod = secured_api
    monkeypatch.setattr(
        runtime_mod.planner,
        "plan_next_action",
        lambda *a, **k: Decision(
            action=Action(type=ActionType.TAP, value="确认付款", target="确认付款")
        ),
    )

    created = client.post("/tasks", json={"instruction": "付款"}, headers=_headers()).json()
    task_id = created["id"]

    for _ in range(300):
        detail = client.get(f"/tasks/{task_id}", headers=_headers()).json()
        if detail["status"] == "waiting":
            break
        time.sleep(0.02)
    assert detail["status"] == "waiting"
    token = detail["pending_confirmation"]["token"]
    assert token

    no_token = client.post(f"/tasks/{task_id}/confirm", json={"approved": True}, headers=_headers())
    wrong = client.post(
        f"/tasks/{task_id}/confirm", json={"approved": True, "token": "x" * 32}, headers=_headers()
    )
    ok = client.post(
        f"/tasks/{task_id}/confirm", json={"approved": True, "token": token}, headers=_headers()
    )

    assert no_token.status_code == 403
    assert wrong.status_code == 403
    assert ok.status_code == 200


def test_requests_are_audited(secured_api):
    client, server, *_rest = secured_api
    audit = _rest[-2]

    client.get("/tasks", headers=_headers())
    client.get("/tasks")

    records = audit.read()
    assert any(r["path"] == "/tasks" and r["status"] == 200 for r in records)
    assert any(r["path"] == "/tasks" and r["status"] == 401 for r in records), "被拒绝的请求同样要留痕"
