"""DeviceController 接口的 ADB 实现（§6.1 / §6.2）。不暴露给 Agent。"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SERIAL = "emulator-5554"
REMOTE_UI_DUMP = "/sdcard/window_dump.xml"
TYPE_SAFE_PATTERN = re.compile(r"^[a-zA-Z0-9_.@,/?!%s]+$")


class AdbError(RuntimeError):
    pass


@dataclass
class AdbController:
    # 所有命令统一携带 -s <serial>，从机制上保证不误触用户真机（§6.4）
    serial: str = DEFAULT_SERIAL
    timeout: float = 15.0

    def _base(self) -> list[str]:
        return ["adb", "-s", self.serial]

    def _run(self, args: list[str], *, text: bool = True) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                self._base() + args,
                capture_output=True,
                text=text,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AdbError("adb 未安装或不在 PATH 中") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb 命令超时: {' '.join(args)}") from exc
        if proc.returncode != 0:
            stderr = proc.stderr if text else proc.stderr.decode(errors="replace")
            raise AdbError(stderr.strip() or f"adb {' '.join(args)} 失败")
        return proc

    def shell(self, *args: str) -> str:
        return self._run(["shell", *args]).stdout.strip()

    def state(self) -> str:
        return self._run(["get-state"]).stdout.strip()

    # ---- 输入 ----

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y))

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self.shell("input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.shell("input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))

    def type_text(self, value: str) -> None:
        """仅支持安全 ASCII；空格转义为 %s；中文需 ADB Keyboard 广播方案（§6.3，M4 处理）。"""
        safe = value.replace(" ", "%s")
        if not TYPE_SAFE_PATTERN.match(safe):
            raise AdbError("type 输入包含不安全字符，仅支持 ASCII 字母数字及 _ . @ , / ? ! 与空格")
        self.shell("input", "text", safe)

    def keyevent(self, code: str) -> None:
        self.shell("input", "keyevent", code)

    def back(self) -> None:
        self.keyevent("KEYCODE_BACK")

    def home(self) -> None:
        self.keyevent("KEYCODE_HOME")

    def launch(self, package: str, activity: str | None = None) -> None:
        if activity:
            self.shell("am", "start", "-n", f"{package}/{activity}")
        else:
            self.shell("monkey", "-p", package, "1")

    def wait(self, duration_ms: int = 1000) -> None:
        time.sleep(duration_ms / 1000.0)

    # ---- 采集 ----

    def screenshot_bytes(self) -> bytes:
        return self._run(["exec-out", "screencap", "-p"], text=False).stdout

    def screenshot(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.screenshot_bytes())
        return path

    def dump_ui(self) -> str:
        self.shell("uiautomator", "dump", REMOTE_UI_DUMP)
        xml = self._run(["exec-out", "cat", REMOTE_UI_DUMP], text=False).stdout
        return xml.decode("utf-8", errors="replace")

    def screen_size(self) -> tuple[int, int]:
        """返回 (width, height)。"""
        output = self.shell("wm", "size")
        for line in output.splitlines():
            if "size" in line.lower():
                part = line.split(":")[-1].strip()
                w, h = part.split("x")
                return int(w), int(h)
        raise AdbError(f"无法解析屏幕尺寸: {output}")

    def current_focus(self) -> tuple[str, str]:
        """返回 (package, activity)。"""
        output = self.shell("dumpsys", "window")
        for line in output.splitlines():
            if "mCurrentFocus" in line and "/" in line:
                # Window{... com.example/.MainActivity}
                focus = line.split("/")
                package = focus[0].split()[-1]
                activity = focus[1].rstrip("}").strip()
                return package, activity
        return "", ""
