"""DeviceController 接口的 ADB 实现（§6.1 / §6.2）。不暴露给 Agent。"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SERIAL = "emulator-5554"
REMOTE_UI_DUMP = "/sdcard/window_dump.xml"
# 校验的是「用户原始输入」：空格允许，% 禁止（% 是 ADB input text 的转义引导符，
# 放行会让 "100%" 这类输入被设备当成非法转义）
TYPE_SAFE_PATTERN = re.compile(r"^[a-zA-Z0-9_.@,/?! ]+$")
# 时长类参数的合理区间：下限取 1ms 而非 0——wait(0) 是无意义空转，未来若出现轮询
# 容易演变成忙循环；上限防 VLM 返回天文数字把任务卡死
DURATION_RANGE_MS = (1, 60_000)


class AdbError(RuntimeError):
    pass


def escape_type_text(value: str) -> str:
    """校验并转义 `input text` 的输入，不合法时抛 AdbError。

    抽成独立函数是为了让假设备/单测能复用同一套校验规则——测试替身一旦绕过校验，
    "非法输入被拦截"这条路径就永远测不到。
    """
    if not TYPE_SAFE_PATTERN.match(value):
        raise AdbError("type 输入包含不安全字符，仅支持 ASCII 字母数字及 _ . @ , / ? ! 与空格")
    return value.replace(" ", "%s")


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
        self.shell("input", "text", escape_type_text(value))

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
        # 先清掉上一帧：dump 失败时若残留旧文件，会静默读到已经过期的页面
        self.shell("rm", "-f", REMOTE_UI_DUMP)

        output = self.shell("uiautomator", "dump", REMOTE_UI_DUMP)
        # uiautomator dump 失败时退出码仍可能为 0，只能靠输出判断
        if "ERROR" in output.upper():
            raise AdbError(f"uiautomator dump 失败: {output.strip()}")

        xml = self._run(["exec-out", "cat", REMOTE_UI_DUMP], text=False).stdout.decode(
            "utf-8", errors="replace"
        )
        # 设备端偶尔只回一句提示（例如 "UI hierchary dumped to"）而没写出完整 XML。
        # 用根节点做内容校验，避免把空内容当成「页面没有可点击元素」而误导决策。
        if "<hierarchy" not in xml:
            raise AdbError(f"uiautomator dump 输出异常，未取到 hierarchy 根节点: {xml[:200]!r}")
        return xml

    def screen_size(self) -> tuple[int, int]:
        """返回 (width, height)，即**实际渲染尺寸**。

        `wm size` 在设置了 override 的设备上会输出两行：Physical size 与 Override size。
        归一化坐标必须按 Override 换算，否则坐标会整体偏移（模拟器改过分辨率时必现）。
        """
        output = self.shell("wm", "size")
        physical: tuple[int, int] | None = None
        override: tuple[int, int] | None = None
        for line in output.splitlines():
            key, sep, value = line.partition(":")
            if not sep:
                continue
            size = self._parse_size(value)
            if size is None:
                continue
            if "override" in key.lower():
                override = size
            elif "physical" in key.lower():
                physical = size

        resolved = override or physical
        if resolved is None:
            raise AdbError(f"无法解析屏幕尺寸: {output}")
        return resolved

    @staticmethod
    def _parse_size(value: str) -> tuple[int, int] | None:
        """解析 "1080x2400" → (1080, 2400)；非尺寸文本返回 None。"""
        left, sep, right = value.strip().lower().partition("x")
        if not sep:
            return None
        try:
            return int(left.strip()), int(right.strip())
        except ValueError:
            return None

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
