"""共用测试替身：全部离线，不需要 adb / 模拟器 / API Key。"""
from __future__ import annotations

import httpx

from device.adb import AdbController


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
        from device.adb import escape_type_text

        # 复用真实校验规则——替身一旦绕过校验，这条路径就永远测不到
        self.events.append(("type", escape_type_text(value)))

    def back(self) -> None:
        self.events.append(("back",))

    def home(self) -> None:
        self.events.append(("home",))

    def launch(self, package: str, activity: str | None = None) -> None:
        self.events.append(("launch", package, activity))

    def wait(self, duration_ms: int = 1000) -> None:
        self.events.append(("wait", duration_ms))

    def shell(self, *args: str) -> str:
        self.events.append(("shell", *args))
        return ""


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
