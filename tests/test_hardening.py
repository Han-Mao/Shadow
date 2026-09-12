"""针对本轮审查修复的回归测试。全部使用假设备，不依赖 adb / 模拟器 / VLM。"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from agent import executor
from agent.memory import Memory
from device.adb import AdbController, AdbError, escape_type_text
from models.action import Action, ActionType, Point
from models.state import AgentState, Observation, StepStatus
from models.task import Task, TaskStatus
from vision import grounding, parser, vlm
from vision.grounding import GroundingError
from vision.vlm import VlmError, VlmParseError, _extract_json

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    """httpx.Response 替身，避免测试真的发网络请求。"""

    def __init__(self, status_code: int, payload: dict | None = None, raises: bool = True) -> None:
        self.status_code = status_code
        self._payload = payload or {"choices": [{"message": {"content": "{}"}}]}
        self._raises = raises

    def raise_for_status(self) -> None:
        if self._raises and self.status_code >= 400:
            raise httpx.HTTPStatusError("bad status", request=None, response=None)

    def json(self) -> dict:
        return self._payload



class FakeDevice:
    """DeviceController 替身：只记录调用，不碰真实设备。"""

    def __init__(self, size: tuple[int, int] = (1000, 2000)) -> None:
        self.size = size
        self.size_calls = 0
        self.events: list[tuple] = []

    def screen_size(self) -> tuple[int, int]:
        self.size_calls += 1
        return self.size

    def tap(self, x: int, y: int) -> None:
        self.events.append(("tap", x, y))

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self.events.append(("long_press", x, y, duration_ms))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.events.append(("swipe", x1, y1, x2, y2, duration_ms))

    def type_text(self, value: str) -> None:
        # 复用真实设备的校验/转义规则，避免替身把非法输入悄悄放行
        self.events.append(("type", escape_type_text(value)))

    def back(self) -> None:
        self.events.append(("back",))

    def home(self) -> None:
        self.events.append(("home",))

    def launch(self, package: str, activity: str | None = None) -> None:
        self.events.append(("launch", package, activity))

    def wait(self, duration_ms: int = 1000) -> None:
        self.events.append(("wait", duration_ms))


def make_adb(
    shell_outputs: dict[str, str],
    record: list | None = None,
    run_outputs: dict[str, bytes] | None = None,
) -> AdbController:
    """构造真实 AdbController，但把 shell() / _run() 换成预设输出。"""
    adb = AdbController(serial="fake-0001")

    def fake_shell(*args: str) -> str:
        if record is not None:
            record.append(list(args))
        return shell_outputs.get(" ".join(args), "")

    class _Completed:
        def __init__(self, stdout: bytes) -> None:
            self.stdout = stdout

    def fake_run(args: list[str], *, text: bool = True) -> "_Completed":
        if record is not None:
            record.append(list(args))
        data = (run_outputs or {}).get(" ".join(args), b"")
        return _Completed(data if isinstance(data, bytes) else data.encode())

    adb.shell = fake_shell  # type: ignore[method-assign]
    adb._run = fake_run  # type: ignore[method-assign]
    return adb


# ---------------------------------------------------------------- 设备层


def test_screen_size_prefers_override():
    """Override size 才是实际渲染尺寸，取 Physical 会让所有归一化坐标偏移。"""
    adb = make_adb({"wm size": "Physical size: 1080x2400\nOverride size: 720x1280"})
    assert adb.screen_size() == (720, 1280)


def test_screen_size_falls_back_to_physical():
    adb = make_adb({"wm size": "Physical size: 1080x2400"})
    assert adb.screen_size() == (1080, 2400)


def test_screen_size_rejects_garbage():
    adb = make_adb({"wm size": "unknown command"})
    with pytest.raises(AdbError):
        adb.screen_size()


def test_type_text_escapes_space_and_validates_input():
    record: list = []
    adb = make_adb({}, record)

    adb.type_text("hello world")
    assert record[-1] == ["input", "text", "hello%sworld"]

    # % 是 input text 的转义引导符，必须拒绝而不是放行
    with pytest.raises(AdbError):
        adb.type_text("100%")
    with pytest.raises(AdbError):
        adb.type_text("中文")
    with pytest.raises(AdbError):
        adb.type_text("a;rm -rf /")


def test_dump_ui_raises_on_error_output():
    adb = make_adb({"uiautomator dump /sdcard/window_dump.xml": "ERROR: could not get idle state."})
    with pytest.raises(AdbError):
        adb.dump_ui()


# ---------------------------------------------------------------- 感知层


def test_parse_bounds_accepts_negative_and_survives_bad_value():
    xml = (
        "<hierarchy>"
        '<node bounds="[-1,-1][-1,-1]" clickable="true"/>'
        '<node bounds="[10,20][110,220]" clickable="true" text="OK"/>'
        '<node bounds="oops" clickable="true" text="bad"/>'
        "</hierarchy>"
    )
    nodes = parser.find_clickable(parser.parse(xml))

    assert nodes[0].bounds == (-1, -1, -1, -1)
    assert nodes[1].bounds == (10, 20, 110, 220)
    assert nodes[1].center == (60, 120)
    # 单个坏节点降级为零矩形，而不是让整棵树解析失败
    assert nodes[2].bounds == (0, 0, 0, 0)


def test_match_by_text_prefers_exact_over_contains():
    xml = (
        "<hierarchy>"
        '<node bounds="[0,0][100,100]" clickable="true" text="搜索历史"/>'
        '<node bounds="[0,200][100,300]" clickable="true" text="搜索"/>'
        "</hierarchy>"
    )
    node = parser.match_by_text(parser.find_clickable(parser.parse(xml)), "搜索")
    assert node is not None
    assert node.center == (50, 250)


def test_resolve_target_pixel_point_skips_screen_query():
    """像素坐标不该触发 screen_size（每一步 tap 都多一次 dumpsys 是纯浪费）。"""
    fake = FakeDevice()
    assert grounding.resolve_target(fake, Point(x=540, y=1200)) == (540, 1200)
    assert fake.size_calls == 0


def test_resolve_target_normalized_point_uses_screen_size():
    fake = FakeDevice(size=(1000, 2000))
    assert grounding.resolve_target(fake, Point(x=0.5, y=0.25)) == (500, 500)
    assert fake.size_calls == 1


def test_resolve_target_string_forms():
    fake = FakeDevice(size=(1000, 2000))
    assert grounding.resolve_target(fake, "540,1200") == (540, 1200)
    assert grounding.resolve_target(fake, "[100,200,300,400]") == (200, 300)
    assert grounding.resolve_target(fake, "点击(540, 1200)") == (540, 1200)
    assert grounding.resolve_target(fake, "0.5,0.5") == (500, 1000)


def test_resolve_target_never_mistakes_text_for_coordinates():
    """「微信 v8.0.32」里有两个数字，绝不能被当成坐标 (8, 32)。"""
    fake = FakeDevice()
    with pytest.raises(GroundingError):
        grounding.resolve_target(fake, "微信 v8.0.32", ui_tree=None)


def test_resolve_target_matches_ui_tree_then_fails_loudly():
    xml = '<hierarchy><node bounds="[400,1000][600,1200]" clickable="true" text="搜索"/></hierarchy>'
    fake = FakeDevice()
    assert grounding.resolve_target(fake, "搜索", ui_tree=xml) == (500, 1100)
    with pytest.raises(GroundingError):
        grounding.resolve_target(fake, "不存在的元素", ui_tree=xml)


def test_extract_json_handles_fences_and_prose():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('{"a": 1}') == {"a": 1}
    # VLM 爱在 JSON 前后加解释，这类响应不能直接判失败
    assert _extract_json('好的，我的判断是：{"result": "ok", "reason": "已进入设置"} 以上。') == {
        "result": "ok",
        "reason": "已进入设置",
    }


def test_extract_json_raises_typed_error_without_json():
    with pytest.raises(VlmParseError):
        _extract_json("这次没有 JSON")
    with pytest.raises(VlmParseError):
        _extract_json("")


# ---------------------------------------------------------------- 执行层


def test_execute_never_raises_on_bad_parameters():
    """执行器是主循环的信任边界：任何入参都必须返回结构化结果，不能抛异常。"""
    fake = FakeDevice()

    bad_actions = [
        Action(type=ActionType.WAIT, value="马上"),
        Action(type=ActionType.WAIT, value="-5"),
        Action(type=ActionType.WAIT, value="99999999"),
        Action(type=ActionType.SWIPE, target="100,200"),
        Action(type=ActionType.SWIPE, target="a,b,c,d"),
        Action(type=ActionType.SWIPE, target=None),
        Action(type=ActionType.LONG_PRESS, target=Point(x=1, y=2), value="一秒"),
        Action(type=ActionType.TYPE, value="中文"),
        Action(type=ActionType.TYPE, value=None),
        Action(type=ActionType.LAUNCH, value=None),
        Action(type=ActionType.TAP, target="找不到的元素"),
    ]
    for action in bad_actions:
        result = executor.execute(fake, action)
        assert result["ok"] is False, action
        assert result["error"], action


def test_execute_happy_paths():
    fake = FakeDevice()

    assert executor.execute(fake, Action(type=ActionType.WAIT, value="500")) == {
        "ok": True,
        "duration": 500,
    }

    swipe = executor.execute(fake, Action(type=ActionType.SWIPE, target="100,200,300,400", value="250"))
    assert swipe == {"ok": True, "x1": 100, "y1": 200, "x2": 300, "y2": 400, "duration": 250}

    tap = executor.execute(fake, Action(type=ActionType.TAP, target=Point(x=10, y=20)))
    assert tap == {"ok": True, "x": 10, "y": 20}
    assert ("tap", 10, 20) in fake.events

    # 中文逗号也当分隔符，VLM 偶尔会输出全角
    full_width = executor.execute(fake, Action(type=ActionType.SWIPE, target="100，200，300，400"))
    assert full_width["ok"] is True


# ---------------------------------------------------------------- 模型与循环


def test_task_ids_are_unique():
    from models.task import Task

    ids = {Task(instruction="x").id for _ in range(200)}
    assert len(ids) == 200


def test_compact_history_excludes_noise():
    from models.state import AgentState, Observation
    from models.task import Task

    state = AgentState(task=Task(instruction="x"))
    state.history.append(
        Observation(step=1, screenshot_path="artifacts/shots/step_001.png", ui_tree="<xml/>", package="com.a")
    )
    entry = state.compact_history()[0]

    assert entry["step"] == 1
    assert entry["status"] == "ok"  # mode="json"：枚举序列化成字符串
    assert "screenshot_path" not in entry
    assert "ui_tree" not in entry


def test_loop_survives_invalid_action(monkeypatch, tmp_path):
    """回归：VLM 返回非法 value 时，主循环必须记错误并继续，而不是抛异常中断任务。"""
    import agent.loop as loop_mod
    from models.state import AgentState, Observation, StepStatus
    from models.task import Task, TaskStatus

    def fake_observe(adb, artifact_dir, step, suffix=""):
        return Observation(step=step, screenshot_path=str(tmp_path / f"step_{step:03d}.png"))

    def boom(*args, **kwargs):
        raise RuntimeError("replan 不可用")

    monkeypatch.setattr(loop_mod.observer, "observe", fake_observe)
    monkeypatch.setattr(loop_mod.planner, "generate_plan", lambda *a, **k: [])
    monkeypatch.setattr(
        loop_mod.planner,
        "plan_next_action",
        lambda *a, **k: Action(type=ActionType.WAIT, value="立刻"),
    )
    monkeypatch.setattr(loop_mod.planner, "replan", boom)

    state = AgentState(task=Task(instruction="测试非法 action", max_steps=2))
    result = loop_mod.AgentLoop(FakeDevice()).run(state)

    assert result.task.status == TaskStatus.FAILED
    # history = [计划阶段的初始观察(step 0)] + 两个失败步骤
    assert [o.step for o in result.history] == [0, 1, 2]
    assert result.history[0].action is None
    assert all(o.status == StepStatus.ERROR for o in result.history[1:])


def test_artifact_dir_follows_env(tmp_path):
    """loop 与 api 必须读同一个 ARTIFACT_DIR，否则截图落在两个目录。"""
    env = {**os.environ, "ARTIFACT_DIR": str(tmp_path)}
    code = "import agent.loop as m; print(m.ARTIFACT_DIR)"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == tmp_path


# ---------------------------------------------------------------- API 层


def api_client(raise_server_exceptions: bool = True):
    from fastapi.testclient import TestClient

    from api import server

    return TestClient(server.app, raise_server_exceptions=raise_server_exceptions), server


def test_api_rejects_unknown_action_type():
    client, _ = api_client()
    resp = client.post("/actions", json={"type": "fly"})
    assert resp.status_code == 400
    assert "fly" in resp.json()["detail"]


def test_api_rejects_invalid_target_before_touching_device():
    """非法 target 在 HTTP 契约层就被拒（422）。

    关键点：这条路径完全不碰设备——如果校验放在 Action 构造处，就会先截图、
    再报参数错；没有真机时更会变成 502，把客户端错误伪装成设备故障。
    """
    client, _ = api_client()
    resp = client.post("/actions", json={"type": "tap", "target": {"x": "abc", "y": 1}})
    assert resp.status_code == 422


def _boom(*args, **kwargs):
    raise RuntimeError("设备层炸了")


def test_api_unhandled_error_is_redacted_by_default(monkeypatch):
    """500 响应不能把内部异常细节回给客户端。"""
    client, server = api_client(raise_server_exceptions=False)
    monkeypatch.setattr(server, "_DEBUG_ERRORS", False)
    monkeypatch.setattr(server.adb, "state", _boom)

    resp = client.get("/devices")
    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]
    assert "设备层炸了" not in body["error"]


def test_api_unhandled_error_can_expose_detail_in_debug_mode(monkeypatch):
    """本机调试时可选择回传异常摘要。"""
    client, server = api_client(raise_server_exceptions=False)
    monkeypatch.setattr(server, "_DEBUG_ERRORS", True)
    monkeypatch.setattr(server.adb, "state", _boom)

    resp = client.get("/devices")
    assert resp.status_code == 500
    assert "设备层炸了" in resp.json()["error"]


def test_api_actions_end_to_end_with_fake_device(monkeypatch, tmp_path):
    """打通 API → AgentLoop → executor → 假设备，确认返回契约与落点都正确。"""
    from models.state import Observation, StepStatus
    import agent.loop as loop_mod

    client, server = api_client()
    fake = FakeDevice()
    monkeypatch.setattr(server, "adb", fake)

    def fake_observe(adb, artifact_dir, step, suffix=""):
        return Observation(step=step, screenshot_path=str(tmp_path / f"step_{step:03d}.png"))

    monkeypatch.setattr(loop_mod.observer, "observe", fake_observe)
    monkeypatch.setattr(loop_mod.verifier, "verify_action", lambda *a, **k: (StepStatus.OK, "stub"))

    resp = client.post("/actions", json={"type": "tap", "target": {"x": 540, "y": 1200}})
    assert resp.status_code == 200

    body = resp.json()
    assert body["status"] == "ok"
    assert body["result"] == {"ok": True, "x": 540, "y": 1200}
    assert ("tap", 540, 1200) in fake.events


def test_api_actions_normalized_target(monkeypatch, tmp_path):
    """归一化坐标经 API 进来也要按屏幕尺寸换算，而不是当成像素点。"""
    from models.state import Observation, StepStatus
    import agent.loop as loop_mod

    client, server = api_client()
    fake = FakeDevice(size=(1080, 2400))
    monkeypatch.setattr(server, "adb", fake)

    def fake_observe(adb, artifact_dir, step, suffix=""):
        return Observation(step=step, screenshot_path=str(tmp_path / f"step_{step:03d}.png"))

    monkeypatch.setattr(loop_mod.observer, "observe", fake_observe)
    monkeypatch.setattr(loop_mod.verifier, "verify_action", lambda *a, **k: (StepStatus.OK, "stub"))

    resp = client.post("/actions", json={"type": "tap", "target": {"x": 0.5, "y": 0.5}})
    assert resp.status_code == 200
    assert ("tap", 540, 1200) in fake.events


# ---------------------------------------------------------------- 观察阶段的异常收敛


def test_observation_failure_is_contained(monkeypatch):
    """回归：截图 / dump 失败不能让请求 500，任务也不能卡在 running。"""
    import agent.loop as loop_mod

    def broken_observe(*args, **kwargs):
        raise AdbError("模拟器连接中断")

    monkeypatch.setattr(loop_mod.observer, "observe", broken_observe)

    state = AgentState(task=Task(instruction="观察必失败", max_steps=2))
    result = loop_mod.AgentLoop(FakeDevice()).run(state)  # 不得抛异常

    assert result.task.status == TaskStatus.FAILED
    assert len(result.history) == 2
    assert all(o.status == StepStatus.ERROR for o in result.history)
    assert "观察失败" in result.history[0].message


def test_post_observation_failure_degrades_to_ok(monkeypatch, tmp_path):
    """动作已执行成功、只是拿不到新截图时，应降级为「未验证的 OK」，而不是触发重发动作。"""
    import agent.loop as loop_mod

    pre = Observation(step=1, screenshot_path=str(tmp_path / "pre.png"))

    def broken_observe(*args, **kwargs):
        # _verify_result 只做一次后置观察，这里直接失败即可
        raise AdbError("post 截图失败")

    monkeypatch.setattr(loop_mod.observer, "observe", broken_observe)

    state = AgentState(task=Task(instruction="点一下", max_steps=1))
    obs = loop_mod.AgentLoop(FakeDevice())._verify_result(
        state, pre, Action(type=ActionType.BACK), {"ok": True}
    )

    assert obs.status == StepStatus.OK
    assert "未验证" in obs.message
    assert obs.result == {"ok": True}


def test_run_marks_failed_on_unexpected_error(monkeypatch):
    """未预期异常必须先把状态落成 failed 再抛，不能留下永远 running 的僵尸任务。"""
    import agent.loop as loop_mod

    class ExplodingMemory(Memory):
        def append(self, task_id, observation):
            raise RuntimeError("存储层炸了")

    def broken_observe(*args, **kwargs):
        raise AdbError("设备不可用")

    monkeypatch.setattr(loop_mod.observer, "observe", broken_observe)

    state = AgentState(task=Task(instruction="x", max_steps=1))
    agent_loop = loop_mod.AgentLoop(FakeDevice(), ExplodingMemory())

    with pytest.raises(RuntimeError):
        agent_loop.run(state)
    assert state.task.status == TaskStatus.FAILED


def test_replan_retry_goes_through_vlm_verification(monkeypatch, tmp_path):
    """回归：重试路径也要过 VLM 验证，不能只看执行结果就判 OK。"""
    import agent.loop as loop_mod

    verified = []

    def fake_observe(adb, artifact_dir, step, suffix=""):
        return Observation(step=step, screenshot_path=str(tmp_path / f"s{step}{suffix}.png"))

    # 首次决策返回非法 value（执行必失败），Re-plan 返回合法 value（执行成功）
    monkeypatch.setattr(loop_mod.observer, "observe", fake_observe)
    monkeypatch.setattr(loop_mod.planner, "generate_plan", lambda *a, **k: [])
    monkeypatch.setattr(
        loop_mod.planner,
        "plan_next_action",
        lambda *a, **k: Action(type=ActionType.WAIT, value="立刻"),
    )
    monkeypatch.setattr(loop_mod.planner, "replan", lambda *a, **k: Action(type=ActionType.WAIT, value="10"))
    monkeypatch.setattr(
        loop_mod.verifier,
        "verify_action",
        lambda *a, **k: (verified.append(1), (StepStatus.OK, "VLM 验证通过"))[1],
    )

    state = AgentState(task=Task(instruction="重试", max_steps=1))
    result = loop_mod.AgentLoop(FakeDevice()).run(state)

    assert verified == [1], "Re-plan 成功执行后必须调用 VLM 验证"
    assert result.history[-1].status == StepStatus.OK


# ---------------------------------------------------------------- 并发与执行模型


def test_device_lock_rejects_concurrent_tasks():
    """全局单设备：已有任务在跑时，第二个任务应立刻 409，而不是排队抢设备。"""
    client, server = api_client()

    assert server._DEVICE_LOCK.acquire(blocking=False)
    try:
        resp = client.post("/tasks", json={"instruction": "第二个任务"})
        assert resp.status_code == 409
        assert "设备忙" in resp.json()["detail"]
    finally:
        server._DEVICE_LOCK.release()


def test_write_endpoints_share_device_lock():
    """会改变设备状态的端点共用一把锁，避免点击/输入互相交错。"""
    client, server = api_client()

    assert server._DEVICE_LOCK.acquire(blocking=False)
    try:
        assert client.post("/tap", json={"x": 1, "y": 2}).status_code == 409
        assert client.post("/back").status_code == 409
        assert client.post("/text", json={"value": "abc"}).status_code == 409
    finally:
        server._DEVICE_LOCK.release()


def test_read_only_endpoint_is_not_blocked_by_device_lock():
    """/devices 不改设备状态，不该被锁住——否则任务跑起来连设备信息都查不到。"""
    client, server = api_client(raise_server_exceptions=False)

    assert server._DEVICE_LOCK.acquire(blocking=False)
    try:
        resp = client.get("/devices")
        assert resp.status_code != 409
    finally:
        server._DEVICE_LOCK.release()


def _stub_loop_once(monkeypatch, tmp_path):
    """把主循环的观察 / 规划 / 验证全部打桩，让 /tasks 能脱机跑完。"""
    import agent.loop as loop_mod

    def fake_observe(adb, artifact_dir, step, suffix=""):
        path = tmp_path / f"step_{step:03d}{'_' + suffix if suffix else ''}.png"
        if not path.exists():
            path.write_bytes(b"\x89PNG\r\n\x1a\n")
        return Observation(step=step, screenshot_path=str(path))

    monkeypatch.setattr(loop_mod.observer, "observe", fake_observe)
    monkeypatch.setattr(loop_mod.planner, "generate_plan", lambda *a, **k: ["一步到位"])
    monkeypatch.setattr(
        loop_mod.planner, "plan_next_action", lambda *a, **k: Action(type=ActionType.DONE, reason="到站")
    )
    monkeypatch.setattr(loop_mod.verifier, "verify_action", lambda *a, **k: (StepStatus.OK, "stub"))


def test_tasks_default_to_background(monkeypatch, tmp_path):
    """默认后台执行：POST /tasks 立刻返回，不占用请求线程等整个 loop 跑完。"""
    client, server = api_client()
    fake = FakeDevice()
    monkeypatch.setattr(server, "adb", fake)
    monkeypatch.setattr(server.loop, "adb", fake)
    _stub_loop_once(monkeypatch, tmp_path)

    resp = client.post("/tasks", json={"instruction": "打开设置"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "background"
    assert body["task"]["status"] in {"pending", "running"}
    task_id = body["task"]["id"]

    detail = {}
    for _ in range(250):
        detail = client.get(f"/tasks/{task_id}").json()
        if detail["task"]["status"] in {"done", "failed"}:
            break
        time.sleep(0.02)

    assert detail["task"]["status"] == "done"
    assert detail["task"]["plan"] == ["一步到位"]


def test_tasks_wait_true_returns_final_state(monkeypatch, tmp_path):
    """演示便利：wait=true 时同步等任务结束并直接返回终态。"""
    client, server = api_client()
    fake = FakeDevice()
    monkeypatch.setattr(server, "adb", fake)
    monkeypatch.setattr(server.loop, "adb", fake)
    _stub_loop_once(monkeypatch, tmp_path)

    resp = client.post("/tasks", json={"instruction": "打开设置", "wait": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "sync"
    assert body["task"]["status"] == "done"


def test_plan_screenshot_is_served_at_shots_zero(monkeypatch, tmp_path):
    """回归：step 0 的截图已经落盘，必须能通过 /tasks/{id}/shots/0 取到。"""
    client, server = api_client()
    fake = FakeDevice()
    monkeypatch.setattr(server, "adb", fake)
    monkeypatch.setattr(server.loop, "adb", fake)
    _stub_loop_once(monkeypatch, tmp_path)

    created = client.post("/tasks", json={"instruction": "打开设置", "wait": True}).json()
    task_id = created["task"]["id"]

    resp = client.get(f"/tasks/{task_id}/shots/0")
    assert resp.status_code == 200


# ---------------------------------------------------------------- 设备层细节


def test_dump_ui_clears_stale_file_before_dump():
    record: list = []
    adb = make_adb(
        {"uiautomator dump /sdcard/window_dump.xml": "UI hierchary dumped to: /sdcard/window_dump.xml"},
        record=record,
        run_outputs={"exec-out cat /sdcard/window_dump.xml": b"<hierarchy></hierarchy>"},
    )

    assert adb.dump_ui() == "<hierarchy></hierarchy>"
    assert record[0] == ["rm", "-f", "/sdcard/window_dump.xml"]


def test_dump_ui_rejects_output_without_hierarchy_root():
    """dump 输出不含 hierarchy 时不能静默返回内容，否则会被当成「页面没有可点击元素」。"""
    adb = make_adb(
        {"uiautomator dump /sdcard/window_dump.xml": "UI hierchary dumped to: /sdcard/x.xml"},
        run_outputs={"exec-out cat /sdcard/window_dump.xml": b"<html>stale</html>"},
    )
    with pytest.raises(AdbError):
        adb.dump_ui()


def test_zero_duration_is_rejected():
    """wait(0) 是无意义空转，下限取 1ms。"""
    result = executor.execute(FakeDevice(), Action(type=ActionType.WAIT, value="0"))
    assert result["ok"] is False
    assert "范围" in result["error"]


# ---------------------------------------------------------------- VLM 重试与成本


def test_image_detail_is_stage_specific(monkeypatch):
    """决策需要 high（坐标精度），计划与验证用 low（省 token 与延迟）。"""
    assert vlm._image_detail("decide") == "high"
    assert vlm._image_detail("plan") == "low"
    assert vlm._image_detail("verify") == "low"

    monkeypatch.setenv("VLM_DETAIL_DECIDE", "low")
    assert vlm._image_detail("decide") == "low"


def test_vlm_retries_retryable_status(monkeypatch):
    """429 / 5xx 属瞬时故障，应带退避重试。"""
    calls: list = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        status = 503 if len(calls) < 3 else 200
        return FakeResponse(status, {"choices": [{"message": {"content": '{"ok": 1}'}}]})

    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.setattr(vlm.httpx, "post", fake_post)
    monkeypatch.setattr(vlm.time, "sleep", lambda _s: None)

    message = vlm._call_vlm([{"role": "user", "content": "hi"}])
    assert len(calls) == 3
    assert message["content"] == '{"ok": 1}'


def test_vlm_does_not_retry_client_errors(monkeypatch):
    """401 之类是请求本身的问题，重试只是浪费配额和时间。"""
    calls: list = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return FakeResponse(401)

    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.setattr(vlm.httpx, "post", fake_post)
    monkeypatch.setattr(vlm.time, "sleep", lambda _s: None)

    with pytest.raises(VlmError):
        vlm._call_vlm([{"role": "user", "content": "hi"}])
    assert len(calls) == 1


def test_vlm_retries_network_errors(monkeypatch):
    calls: list = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("连接被拒绝")
        return FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.setattr(vlm.httpx, "post", fake_post)
    monkeypatch.setattr(vlm.time, "sleep", lambda _s: None)

    vlm._call_vlm([{"role": "user", "content": "hi"}])
    assert len(calls) == 2


def test_vlm_raises_after_exhausting_retries(monkeypatch):
    calls: list = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return FakeResponse(429)

    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.setattr(vlm.httpx, "post", fake_post)
    monkeypatch.setattr(vlm.time, "sleep", lambda _s: None)

    with pytest.raises(VlmError, match="已重试"):
        vlm._call_vlm([{"role": "user", "content": "hi"}])
    assert len(calls) == vlm.VLM_MAX_ATTEMPTS


# ---------------------------------------------------------------- prompt 卫生


def test_decision_prompt_renders_observation_only_step():
    """action 为 None 的观察步应显示占位说明，而不是 "None"，否则模型会以为发生过动作。"""
    history = [
        {
            "step": 0,
            "action": None,
            "status": "ok",
            "message": "",
            "result": {},
            "package": "com.android.settings",
            "activity": ".Settings",
        },
        {
            "step": 1,
            "action": {"type": "tap", "target": {"x": 10.0, "y": 20.0}, "value": None, "reason": "点搜索"},
            "status": "ok",
            "message": "已点击",
            "result": {"ok": True, "x": 10, "y": 20},
            "package": "com.android.settings",
            "activity": ".Settings",
        },
    ]
    text = vlm.build_decision_prompt("打开设置", "", None, history, [])

    assert "（无动作，仅观察）" in text
    history_section = text.split("最近执行记录：")[1].split("当前页面")[0]
    assert "None" not in history_section
    assert "tap" in history_section and "x=10.0" in history_section
    assert "已点击" in history_section
