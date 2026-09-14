"""对象级授权与状态机一致性（V2.2 §一 / §二 / §三 / §四 / §五）。

这一组用例对应审核给出的 P0 三项 + P1 一项，每一项都是**真实存在过**的行为：

    P0-1  `/inject` 先改任务、再检查权限 → Authorization after side effect
    P0-2  不指定 device_serial 就能绕过设备限制（调度器自动挑到别人的设备）
    P0-3  GET /tasks/* 没有对象级授权 → 受限令牌能读到别人任务的放行令牌
    P1-3  否决危险动作时 API 直接 FAILED → 与 Runtime「换策略继续」的设计相反

测试全部走 HTTP，断言的是**外部可观察的行为**，而不是内部实现。
"""
from __future__ import annotations

import time

import pytest

TOKEN = "op-token"
EMU_1, EMU_2 = "emu-1", "emu-2"


@pytest.fixture
def authz_api(tmp_path, monkeypatch):
    """两台设备 + 一个只能操作 emu-1 的令牌。"""
    from fastapi.testclient import TestClient

    import agent.runtime as runtime_mod
    from agent.classifier import TaskClassifier
    from agent.runtime import AgentRuntime
    from agent.scheduler import TaskScheduler
    from agent.task_manager import TaskManager
    from api import server
    from device.pool import DevicePool
    from device.session import DeviceSession
    from fakes import FakeDevice
    from models.state import Observation
    from storage import CheckpointStore, EventLog, TaskStore, TrajectoryStore
    from storage.audit_log import AuditLog

    monkeypatch.setenv("SHADOW_API_TOKEN", TOKEN)
    monkeypatch.setenv("SHADOW_API_DEVICE_ALLOW", EMU_1)
    monkeypatch.delenv("SHADOW_API_READONLY_TOKEN", raising=False)

    sessions = {
        serial: DeviceSession(FakeDevice(), serial=serial) for serial in (EMU_1, EMU_2)
    }
    pool = DevicePool(list(sessions.values()))
    task_store = TaskStore(tmp_path / "tasks")
    checkpoint_store = CheckpointStore(tmp_path / "checkpoints")
    trajectory = TrajectoryStore(root=tmp_path / "trajectories")
    event_log = EventLog(tmp_path / "events")
    audit = AuditLog(tmp_path / "audit")

    runtime = AgentRuntime(
        pool,
        artifact_dir=tmp_path,
        trajectory=trajectory,
        checkpoints=checkpoint_store,
        task_store=task_store,
        event_log=event_log,
    )
    scheduler = TaskScheduler(
        runtime, pool, task_store=task_store, idle_poll_seconds=0.01, event_log=event_log
    )
    manager = TaskManager(
        store=task_store, scheduler=scheduler, classifier=TaskClassifier(), runtime=runtime
    )

    for name, value in (
        ("session", sessions[EMU_1]),
        ("device_pool", pool),
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

    monkeypatch.setattr(
        runtime_mod.observer,
        "observe",
        lambda adb, artifact_dir, step, suffix="": Observation(
            step=step,
            screenshot_path=str(tmp_path / f"s{step}{suffix}.png"),
            package="com.android.settings",
            activity=".Main",
            ui_tree='<hierarchy><node class="android.widget.TextView" text="设置"/></hierarchy>',
        ),
    )
    monkeypatch.setattr(runtime_mod.planner, "generate_plan", lambda *a, **k: ["一步"])

    client = TestClient(server.app, raise_server_exceptions=False)
    scheduler.start()
    try:
        yield client, server, pool, sessions, manager, runtime, runtime_mod, scheduler
    finally:
        scheduler.stop()


def hdr(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def running_on(scheduler, pool, task, serial: str) -> None:
    from models.task import TaskStatus

    task.mark(TaskStatus.RUNNING)
    if not pool.require(serial).owned_by(task.id):
        pool.require(serial).acquire(task.id)
    with scheduler._cond:  # noqa: SLF001 - 与既有测试一致的场景摆法
        scheduler._lanes[serial].running = task


class SuperTaskClassifier:
    """把所有注入都判成 SUPER_TASK —— 也就是破坏性最强的那条路径。"""

    def classify(self, instruction, *, current=None, candidates=()):
        from models.task_relation import TaskRelation, TaskRelationResult

        return TaskRelationResult(
            relation=TaskRelation.SUPER_TASK,
            confidence=0.95,
            affected_task_id=current.id if current else None,
            reason="改成搜索高铁",
        )


# ---------------------------------------------------------------- P0-1


def test_inject_authorizes_before_touching_the_task(authz_api, monkeypatch):
    """越权的 `/inject` 必须**一个字段都不改**（V2.2 §一）。

    旧实现的顺序是：先 `manager.inject()`（改 instruction / version / plan /
    priority，SUPER_TASK 还会请求抢占），再检查设备权限返回 403。
    调用方拿到的是失败响应，副作用却已经发生了——Authorization after side effect。
    """
    client, _, pool, _, manager, _, _, scheduler = authz_api
    monkeypatch.setattr(manager, "_classifier", SuperTaskClassifier())

    task = manager.create("在淘宝搜索运动鞋", device_serial=EMU_2, submit=False)
    task.set_plan(["搜索运动鞋", "加入购物车"])
    version_before, plan_before = task.version, [s.goal for s in task.plan]
    instruction_before = task.instruction
    manager._store.save(task)
    running_on(scheduler, pool, task, EMU_2)

    resp = client.post(
        f"/tasks/{task.id}/inject",
        json={"instruction": "改成搜索高铁", "allow_disruptive": True},
        headers=hdr(),
    )

    assert resp.status_code == 403
    assert "无权访问任务" in resp.json()["detail"]

    reloaded = manager.get(task.id)
    assert reloaded.instruction == instruction_before, "越权注入不得改写目标"
    assert reloaded.version == version_before, "越权注入不得动版本号"
    assert [s.goal for s in reloaded.plan] == plan_before, "越权注入不得改计划"
    assert reloaded.checkpoint_id == task.checkpoint_id, "越权注入不得清恢复点"
    assert not pool.require(EMU_2).should_yield(task.id), "更不该因此请求抢占"


# ---------------------------------------------------------------- P0-2


def test_unbound_task_cannot_escape_the_device_scope(authz_api):
    """不指定设备时，只能从**被允许的**设备里挑（V2.2 §二）。

    旧实现 `may_use_device(None) -> True`，于是「不填 serial」就是绕过设备限制的后门：
    调度器会自动挑一台最闲的，很可能就是别人的设备。
    """
    client, _, _, _, manager, _, _, scheduler = authz_api

    # 让 emu-2 明显更闲（emu-1 上挂一条在跑的任务），旧逻辑一定会挑 emu-2
    other = manager.create("占住 emu-1", device_serial=EMU_1, submit=False)
    running_on(scheduler, authz_api[2], other, EMU_1)

    resp = client.post("/tasks", json={"instruction": "打开微信"}, headers=hdr())

    assert resp.status_code == 200
    task = manager.get(resp.json()["id"])
    assert task.device_serial == EMU_1, "未指定设备也只能落在授权设备上"


def test_unavailable_allowed_device_returns_403_not_500(authz_api, monkeypatch):
    """授权设备一台都不在池里 → 403，而不是 500。"""
    client, _, _, _, _, _, _, _ = authz_api
    monkeypatch.setenv("SHADOW_API_DEVICE_ALLOW", "emu-9")

    resp = client.post("/tasks", json={"instruction": "打开微信"}, headers=hdr())

    assert resp.status_code == 403
    assert "emu-9" in resp.json()["detail"]


# ---------------------------------------------------------------- P0-3


def test_task_reads_are_scoped_to_allowed_devices(authz_api):
    """对象级读权限：读不到别人设备上的任务（V2.2 §四）。

    读 `/tasks/{id}` 会带出 `pending_confirmation.token`——读到就等于拿到了
    放行危险动作的凭据，所以读取侧必须同样过设备检查。
    """
    client, _, _, _, manager, runtime, _, _ = authz_api

    foreign = manager.create("别人的任务", device_serial=EMU_2, submit=False)
    # 让它有东西可泄露：伪造一个待确认的危险动作
    from models.action import Action, ActionType

    runtime._state_for(foreign).pending_confirmation = Action(
        type=ActionType.TAP, value="确认付款"
    )

    assert client.get(f"/tasks/{foreign.id}", headers=hdr()).status_code == 403
    assert client.get(f"/tasks/{foreign.id}/events", headers=hdr()).status_code == 403
    assert client.get(f"/tasks/{foreign.id}/replay", headers=hdr()).status_code == 403
    assert client.get(f"/tasks/{foreign.id}/checkpoint", headers=hdr()).status_code == 403
    assert client.get(f"/tasks/{foreign.id}/history", headers=hdr()).status_code == 403
    assert client.get(f"/tasks/{foreign.id}/shots/1", headers=hdr()).status_code == 403

    listed = client.get("/tasks", headers=hdr()).json()["tasks"]
    assert foreign.id not in [t["id"] for t in listed], "列表同样要过滤"


def test_own_task_is_still_readable(authz_api):
    """别把授权做过头：自己设备上的任务必须照常可读。"""
    client, _, _, _, manager, _, _, _ = authz_api

    mine = manager.create("我的任务", device_serial=EMU_1, submit=False)

    resp = client.get(f"/tasks/{mine.id}", headers=hdr())

    assert resp.status_code == 200
    assert resp.json()["id"] == mine.id


def test_scheduler_snapshot_is_scoped(authz_api):
    """/scheduler 只暴露授权设备的状态（V2.2 §四）。"""
    client, _, _, _, _, _, _, _ = authz_api

    body = client.get("/scheduler", headers=hdr()).json()

    assert set(body["devices"]) == {EMU_1}
    assert body["scoped_to"] == [EMU_1]


# ---------------------------------------------------------------- P1-3


def test_denying_a_dangerous_action_does_not_kill_the_task(authz_api, monkeypatch):
    """否决危险动作 = 换策略继续，**不是**把任务判死（V2.2 §三）。

    旧实现：Runtime 把动作拉黑并准备换策略，API 却 `task.mark(FAILED)`——
    两个模块的状态机在打架，实际行为是任务直接结束。
    只有 `POST /tasks/{id}/cancel` 才该结束任务。
    """
    from models.action import Action, ActionType, Decision

    client, _, pool, sessions, manager, _, runtime_mod, scheduler = authz_api
    monkeypatch.setattr(
        runtime_mod.planner,
        "plan_next_action",
        lambda *a, **k: Decision(
            action=Action(type=ActionType.TAP, value="确认付款", target="确认付款")
        ),
    )

    # 先让它跑到 waiting（此时 worker 已释放设备）
    created = client.post("/tasks", json={"instruction": "付款"}, headers=hdr()).json()
    task_id = created["id"]

    detail = {}
    for _ in range(300):
        detail = client.get(f"/tasks/{task_id}", headers=hdr()).json()
        if detail["status"] == "waiting":
            break
        time.sleep(0.02)
    assert detail["status"] == "waiting"
    assert detail["confirmation_kind"] == "dangerous_action"
    token = detail["pending_confirmation"]["token"]

    # 再占住设备，让「否决后重新入队」的状态不被 worker 立刻改掉，断言才稳定
    sessions[EMU_1].acquire("__manual__")
    try:
        denied = client.post(
            f"/tasks/{task_id}/confirm",
            json={"approved": False, "token": token},
            headers=hdr(),
        )
    finally:
        sessions[EMU_1].release("__manual__")

    assert denied.status_code == 200
    body = denied.json()
    assert body["approved"] is False
    assert body["kind"] == "dangerous_action"
    assert body["task"]["status"] != "failed", "否决一个动作不等于放弃整个任务"
    assert body["task"]["status"] in {"queued", "running", "waiting"}


def test_confirm_without_token_is_rejected_and_audited(authz_api):
    from models.action import Action, ActionType

    client, server, _, _, manager, runtime, _, _ = authz_api

    task = manager.create("付款", device_serial=EMU_1, submit=False)
    runtime._state_for(task).pending_confirmation = Action(
        type=ActionType.TAP, value="确认付款"
    )

    resp = client.post(
        f"/tasks/{task.id}/confirm", json={"approved": True}, headers=hdr()
    )

    assert resp.status_code == 403
    assert "确认未通过" in resp.json()["detail"]
    assert any("confirmation denied" in str(r.get("note", "")) for r in server.audit_log.read())


def test_confirmation_token_is_scoped_to_the_operator(authz_api, monkeypatch):
    """把令牌交给另一个人提交 → 拒绝（V2.2 §五）。"""
    from models.action import Action, ActionType

    client, server, _, _, manager, runtime, _, _ = authz_api

    task = manager.create("付款", device_serial=EMU_1, submit=False)
    runtime._state_for(task).pending_confirmation = Action(
        type=ActionType.TAP, value="确认付款"
    )
    # A 拿到的令牌
    token = server.auth.issue_confirmation_token(
        "operator-A", task.id, runtime.pending_confirmation(task.id).fingerprint, task.version
    )

    # B 用同一个令牌提交：身份不匹配 → 403
    monkeypatch.setenv("SHADOW_API_TOKEN_NAME", "operator-B")
    resp = client.post(
        f"/tasks/{task.id}/confirm", json={"approved": True, "token": token}, headers=hdr()
    )

    assert resp.status_code == 403


# ---------------------------------------------------------------- P2：手动 API 多设备


def test_manual_api_targets_the_requested_device(authz_api):
    """`/tap` 等手动端点支持指定设备，并按授权过滤（V2.2 §八）。"""
    client, _, _, sessions, _, _, _, _ = authz_api

    ok = client.post("/tap", json={"x": 1, "y": 2, "device_serial": EMU_1}, headers=hdr())
    denied = client.post("/tap", json={"x": 1, "y": 2, "device_serial": EMU_2}, headers=hdr())

    assert ok.status_code == 200
    assert ok.json()["device"] == EMU_1
    assert denied.status_code == 403
    assert sessions[EMU_1].controller.events.count(("tap", 1, 2)) == 1
    assert ("tap", 1, 2) not in sessions[EMU_2].controller.events


def test_devices_endpoint_lists_only_allowed(authz_api):
    client, _, _, _, _, _, _, _ = authz_api

    body = client.get("/devices", headers=hdr()).json()

    assert [item["serial"] for item in body["devices"]] == [EMU_1]


def test_observe_reports_generation_and_stability(authz_api, monkeypatch):
    """只读观察要能分辨「稳定页」与「跨了一次写入的中间态」（V2.2 §九）。"""
    client, server, _, sessions, _, _, _, _ = authz_api

    first = client.post("/observe?device_serial=emu-1", headers=hdr()).json()
    assert first["stable"] is True
    assert first["device"] == EMU_1
    assert first["generation"] == sessions[EMU_1].generation

    # 模拟「观察期间设备被写了」：observer 返回后代次变了
    def observe_then_write(adb, artifact_dir, step, suffix=""):
        from models.state import Observation

        sessions[EMU_1].bump_generation()
        return Observation(step=step, screenshot_path="x.png", package="com.x", activity=".A")

    monkeypatch.setattr(server.observer, "observe", observe_then_write)

    second = client.post("/observe?device_serial=emu-1", headers=hdr()).json()
    assert second["stable"] is False, "跨了写入的观察不能算稳定态"
