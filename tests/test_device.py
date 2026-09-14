"""设备层：ADB 封装、输入通道、设备会话所有权。"""
from __future__ import annotations

import base64
import threading

import pytest

import device.adb as adb_mod
from device.adb import AdbBudgetExhausted, AdbController, AdbError, escape_type_text
from device.input import (
    ADB_KEYBOARD_PACKAGE,
    AdbInputProvider,
    AutoInputProvider,
    BroadcastInputProvider,
    build_default_input,
    is_plain_ascii,
)
from device.session import DeviceBusyError, DeviceSession
from fakes import FakeDevice, make_adb


# ---------------------------------------------------------------- AdbController


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


def test_escape_type_text_is_shared_with_callers():
    assert escape_type_text("a b") == "a%sb"
    with pytest.raises(AdbError):
        escape_type_text("中文")


# ---------------------------------------------------------------- 命令总预算（§十四）


def _spy_subprocess(monkeypatch) -> list[tuple[list[str], float | None]]:
    """截住 subprocess.run，记录每条命令实际用的超时。"""
    captured: list[tuple[list[str], float | None]] = []

    class _Proc:
        returncode = 0
        stdout: str | bytes = ""
        stderr: str | bytes = ""

    def fake_run(cmd, **kwargs):
        captured.append((list(cmd), kwargs.get("timeout")))
        return _Proc()

    monkeypatch.setattr(adb_mod.subprocess, "run", fake_run)
    return captured


def test_read_commands_use_a_tighter_timeout_than_writes(monkeypatch):
    """采集类命令用 read_timeout，写类用 timeout。

    采集卡住时继续干等没有收益——任务不会因为多等几秒就拿到页面，
    只会把「让出设备」的安全点一直往后拖，堵住高优先级的任务。
    写类不同：`am start` 拉冷启动的 App 确实可能慢，等一等是有意义的。
    """
    captured = _spy_subprocess(monkeypatch)
    adb = AdbController(serial="x", timeout=15.0, read_timeout=3.0)

    adb.shell("input", "tap", "1", "2")
    adb.read_shell("wm", "size")
    adb.screenshot_bytes()

    assert captured[0][1] == 15.0, "写类命令用默认 timeout"
    assert captured[1][1] == 3.0, "dumpsys/wm size 属于采集"
    assert captured[2][1] == 3.0, "截图属于采集"


def test_deadline_budget_caps_each_command_to_remaining_budget(monkeypatch):
    """总预算把单条命令的超时压到「剩余预算」，而不是各自等满自己的超时。"""
    captured = _spy_subprocess(monkeypatch)
    adb = AdbController(serial="x", timeout=15.0)

    with adb.deadline_budget(2.0):
        adb.shell("input", "tap", "1", "2")

    assert captured[-1][1] is not None
    assert captured[-1][1] <= 2.0, "写类默认 15s，但预算只剩 2s"


def test_deadline_budget_exhaustion_refuses_to_start_new_command(monkeypatch):
    """预算耗尽后**不再启动**新命令——这正是「一次采集耗时有上界」的来源。"""
    captured = _spy_subprocess(monkeypatch)
    adb = AdbController(serial="x")

    with adb.deadline_budget(0.0):
        with pytest.raises(AdbBudgetExhausted):
            adb.shell("wm", "size")

    assert captured == [], "预算已耗尽，不该真的去跑命令"


def test_budget_exhaustion_is_an_adb_error_so_upper_layers_keep_working(monkeypatch):
    """预算耗尽继承自 AdbError，上层的「失败收敛」逻辑不用改就能接住它。"""
    _spy_subprocess(monkeypatch)
    adb = AdbController(serial="x")

    with adb.deadline_budget(0.0):
        with pytest.raises(AdbError):
            adb.read_shell("dumpsys", "window")


def test_observe_applies_one_total_budget_to_the_whole_capture(monkeypatch, tmp_path):
    """一次采集必须整体受预算约束。

    采集内部有 6 条 adb 命令（截图 + wm size + dumpsys + rm + dump + cat），
    每条各自有超时也挡不住「6 条累加」——高优任务就要等那么久。
    """
    from agent import observer as observer_mod

    adb = make_adb(
        {
            "wm size": "Physical size: 1080x2400",
            "dumpsys window": "mCurrentFocus=Window{abc com.demo/.Main}",
            "uiautomator dump /sdcard/window_dump.xml": "UI hierchary dumped to: ok",
        },
        run_outputs={"exec-out cat /sdcard/window_dump.xml": b"<hierarchy></hierarchy>"},
    )

    used: list[float] = []
    real_budget = adb.deadline_budget

    def spy(seconds: float):
        used.append(seconds)
        return real_budget(seconds)

    adb.deadline_budget = spy  # type: ignore[method-assign]
    monkeypatch.setattr(
        observer_mod.screenshot, "capture", lambda *a, **k: tmp_path / "step_001.png"
    )

    observation = observer_mod.observe(adb, tmp_path, 1, budget_seconds=4.0)

    assert used == [4.0], "整段采集必须包在同一个预算里"
    assert observation.package == "com.demo"
    assert observation.ui_tree == "<hierarchy></hierarchy>"


# ---------------------------------------------------------------- 输入通道（中文）


def test_is_plain_ascii():
    assert is_plain_ascii("hello.world")
    assert is_plain_ascii("a b")
    assert not is_plain_ascii("你好")
    assert not is_plain_ascii("")
    assert not is_plain_ascii("100%")


def test_adb_input_provider_uses_input_text():
    record: list = []
    adb = make_adb({}, record)
    AdbInputProvider(adb).input("hello world")
    assert record[-1] == ["input", "text", "hello%sworld"]


def test_broadcast_provider_sends_base64():
    record: list = []
    adb = make_adb({"ime list -s": f"{ADB_KEYBOARD_PACKAGE}/.AdbIME"}, record)
    provider = BroadcastInputProvider(adb)

    assert provider.available()
    provider.input("你好张三")

    broadcast = record[-1]
    assert broadcast[:4] == ["am", "broadcast", "-a", "ADB_INPUT_B64"]
    assert broadcast[4] == "--es" and broadcast[5] == "msg"
    assert base64.b64decode(broadcast[6]).decode("utf-8") == "你好张三"


def test_broadcast_provider_enables_ime_once():
    record: list = []
    adb = make_adb({"ime list -s": f"{ADB_KEYBOARD_PACKAGE}/.AdbIME"}, record)
    provider = BroadcastInputProvider(adb)

    provider.input("一")
    provider.input("二")

    sets = [call for call in record if call[:3] == ["ime", "set", "com.android.adbkeyboard/.AdbIME"]]
    assert len(sets) == 1, "每次输入都切输入法会让屏幕闪烁，只能切一次"


def test_broadcast_provider_reports_missing_ime():
    adb = make_adb({"ime list -s": "com.google.android.inputmethod.latin/.LatinIME"})
    provider = BroadcastInputProvider(adb)
    assert not provider.available()
    with pytest.raises(AdbError, match="ADB Keyboard"):
        provider.input("你好")


def test_auto_provider_routes_by_content():
    record: list = []
    adb = make_adb({"ime list -s": f"{ADB_KEYBOARD_PACKAGE}/.AdbIME"}, record)
    provider = build_default_input(adb)

    provider.input("hello")
    assert record[-1] == ["input", "text", "hello"]

    provider.input("你好")
    assert record[-1][:4] == ["am", "broadcast", "-a", "ADB_INPUT_B64"]


def test_auto_provider_falls_back_when_ascii_channel_rejects():
    """ASCII 通道抛错（例如设备拒绝）时必须回退广播，而不是把异常抛给上层。"""
    record: list = []
    adb = make_adb({"ime list -s": f"{ADB_KEYBOARD_PACKAGE}/.AdbIME"}, record)

    class Rejecting:
        name = "rejecting"

        def available(self) -> bool:
            return True

        def input(self, text: str) -> None:
            raise AdbError("设备拒绝了这次输入")

    provider = AutoInputProvider(Rejecting(), BroadcastInputProvider(adb))
    provider.input("hello")
    assert record[-1][:4] == ["am", "broadcast", "-a", "ADB_INPUT_B64"]


# ---------------------------------------------------------------- DeviceSession


def test_session_acquire_release_and_owner():
    session = DeviceSession(FakeDevice(), serial="emulator-5554")

    assert session.acquire("task-a")
    assert session.owned_by("task-a")
    assert session.busy
    assert not session.acquire("task-b", timeout=0.01)
    assert session.owner == "task-a"

    assert session.release("task-a")
    assert session.owner is None
    assert session.acquire("task-b")


def test_session_release_by_non_owner_is_ignored():
    """释放通常写在 finally 里，抛异常会把真正的失败原因盖掉。"""
    session = DeviceSession(FakeDevice())
    session.acquire("task-a")
    assert session.release("task-b") is False
    assert session.owner == "task-a"


def test_session_preempt_handshake():
    session = DeviceSession(FakeDevice())
    session.acquire("task-a")

    assert not session.should_yield("task-a")
    assert session.request_preempt("task-b")
    assert session.should_yield("task-a")
    assert not session.should_yield("task-c")

    session.release("task-a")
    assert not session.should_yield("task-a")


def test_session_owned_context_manager_rejects_non_owner():
    session = DeviceSession(FakeDevice())
    with pytest.raises(DeviceBusyError):
        with session.owned("nobody"):
            pass


def test_session_is_serialized_under_concurrency():
    """10 个线程抢设备，任何时刻只应有一个持有者。"""
    session = DeviceSession(FakeDevice())
    inside = 0
    max_inside = 0
    guard = threading.Lock()

    def worker(name: str) -> None:
        nonlocal inside, max_inside
        if not session.acquire(name, timeout=2.0):
            return
        try:
            with guard:
                inside += 1
                max_inside = max(max_inside, inside)
            with guard:
                inside -= 1
        finally:
            session.release(name)

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert max_inside == 1
    assert session.owner is None


def test_session_snapshot_exposes_state():
    session = DeviceSession(FakeDevice(), serial="emu-1")
    session.acquire("task-x")
    session.request_preempt("task-y")
    snap = session.snapshot()
    assert snap["owner"] == "task-x"
    assert snap["busy"] is True
    assert snap["preempt_requested_for"] == "task-y"


def test_adb_controller_default_serial_is_safe():
    """所有命令都带 -s，避免误触用户真机。"""
    adb = AdbController(serial="emulator-5556")
    assert adb._base() == ["adb", "-s", "emulator-5556"]
