"""DeviceController 接口的 ADB 实现（§6.1 / §6.2）。不暴露给 Agent。"""
from __future__ import annotations

import re
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

DEFAULT_SERIAL = "emulator-5554"
REMOTE_UI_DUMP = "/sdcard/window_dump.xml"
# 校验的是「用户原始输入」：空格允许，% 禁止（% 是 ADB input text 的转义引导符，
# 放行会让 "100%" 这类输入被设备当成非法转义）
TYPE_SAFE_PATTERN = re.compile(r"^[a-zA-Z0-9_.@,/?! ]+$")
# 时长类参数的合理区间：下限取 1ms 而非 0——wait(0) 是无意义空转，未来若出现轮询
# 容易演变成忙循环；上限防 VLM 返回天文数字把任务卡死
DURATION_RANGE_MS = (1, 60_000)

# 当前上下文的命令总预算截止时刻（monotonic 秒）。见 `deadline_budget`。
_DEADLINE: ContextVar[float | None] = ContextVar("adb_deadline", default=None)


class AdbError(RuntimeError):
    pass


class AdbBudgetExhausted(AdbError):
    """总预算用完——**不是**设备坏了，而是我们主动不再往下等。

    单独一个类型是为了让上层能区分「设备真的出错」和「这次采集超预算了」：
    后者重试一次往往就好了，前者重试没用。
    """


# 只读操作（V2.7 P1-7）：这些方法只**读取**设备状态，不改变设备上的任何东西。
# API 的只读端点（/screenshot、/observe）只能调用这里面的方法；会改设备的
# tap / text / swipe / keyevent / am start 一律不在此列。
# 之前「只读」只是端点名约定，没有结构化声明——加了这个集合之后，
# 「只读端点是否真的只读」可以从代码里查证，而不是靠人记住约定。
READ_ONLY_OPERATIONS = frozenset(
    {
        "screenshot",
        "screenshot_bytes",
        "dump_ui",
        "screen_size",
        "current_focus",
        "state",
        "read_shell",
        "shell",  # 危险：shell 本身可以执行任意命令，见 is_read_only 的说明
    }
)

# 但 `shell` 是万能执行口，把它算「只读」只在**调用方只传只读命令**时成立。
# 这里保守处理：`shell` 单独列出来，`is_read_only` 默认对它返回 False，
# 只有明确的只读封装（screenshot / dump_ui / screen_size / current_focus）才算只读。
_READ_ONLY_SAFE = READ_ONLY_OPERATIONS - {"shell", "read_shell"}


def is_read_only(operation: str) -> bool:
    """这次设备操作会不会改变设备状态（V2.7 P1-7）。

    只读封装（screenshot / dump_ui / screen_size / current_focus / state）返回 True；
    `shell` / `read_shell` 是万能口、无法保证只读，保守返回 False；
    其余（tap / text / swipe / keyevent / launch 等）都是改设备的，返回 False。
    """
    return operation in _READ_ONLY_SAFE



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

    # 写类/交互类命令的超时（tap / text / am start …）
    timeout: float = 15.0

    # 采集类命令的超时（截图 / dump / dumpsys）。
    #
    # 刻意比写类更紧：采集卡住时**继续干等没有任何收益**——任务不会因为多等 10 秒
    # 就拿到页面，反而会把「让出设备」这个安全点一直往后拖，堵住高优先级任务。
    # 写类不同：`am start` 拉冷启动的 App 确实可能慢，等一等是有意义的。
    read_timeout: float = 6.0

    def _base(self) -> list[str]:
        return ["adb", "-s", self.serial]

    @contextmanager
    def deadline_budget(self, seconds: float) -> Iterator[None]:
        """给这一段内的**所有** adb 调用加一个总预算（V2.1 §十四）。

        为什么需要它：单个命令早就有超时（15s），但一次采集要跑好几个命令
        （截图 + dumpsys×2 + rm + uiautomator dump + cat = 6 条），
        各自等 15s 的话最坏就是 90 秒——高优任务要等 90 秒才能拿到设备。

        有了总预算后：每条命令的超时取 `min(命令自己的超时, 剩余预算)`，
        预算耗尽就直接 `AdbBudgetExhausted`，不再开始新命令。
        这样「一次采集」的耗时**真的有上界**，安全点间隔也就有上界了。

        注意：它管不了「已经在飞的那一条命令」——那条只能等它自己超时。
        所以严格上界是 `预算 + 单条命令超时`，但已经从"若干条累加"降到"一条"。
        """
        token = _DEADLINE.set(time.monotonic() + seconds)
        try:
            yield
        finally:
            _DEADLINE.reset(token)

    def _run(
        self,
        args: list[str],
        *,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        budget = self.timeout if timeout is None else timeout

        remaining_deadline = _DEADLINE.get()
        if remaining_deadline is not None:
            remaining = remaining_deadline - time.monotonic()
            if remaining <= 0:
                raise AdbBudgetExhausted(
                    f"adb 调用总预算已耗尽，跳过命令: {' '.join(args)}"
                )
            budget = min(budget, remaining)

        try:
            proc = subprocess.run(
                self._base() + args,
                capture_output=True,
                text=text,
                timeout=budget,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AdbError("adb 未安装或不在 PATH 中") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb 命令超时（{budget:.1f}s）: {' '.join(args)}") from exc
        if proc.returncode != 0:
            stderr = proc.stderr if text else proc.stderr.decode(errors="replace")
            raise AdbError(stderr.strip() or f"adb {' '.join(args)} 失败")
        return proc

    def shell(self, *args: str) -> str:
        return self._run(["shell", *args]).stdout.strip()

    def read_shell(self, *args: str) -> str:
        """采集类 shell 命令（`dumpsys` / `wm size` 等），走更紧的 `read_timeout`。"""
        return self._run(["shell", *args], timeout=self.read_timeout).stdout.strip()

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
        return self._run(
            ["exec-out", "screencap", "-p"], text=False, timeout=self.read_timeout
        ).stdout

    def screenshot(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.screenshot_bytes())
        return path

    def dump_ui(self) -> str:
        # 先清掉上一帧：dump 失败时若残留旧文件，会静默读到已经过期的页面
        self._run(["shell", "rm", "-f", REMOTE_UI_DUMP], timeout=self.read_timeout)

        output = self._run(
            ["shell", "uiautomator", "dump", REMOTE_UI_DUMP], timeout=self.read_timeout
        ).stdout.strip()
        # uiautomator dump 失败时退出码仍可能为 0，只能靠输出判断
        if "ERROR" in output.upper():
            raise AdbError(f"uiautomator dump 失败: {output}")

        xml = self._run(
            ["exec-out", "cat", REMOTE_UI_DUMP], text=False, timeout=self.read_timeout
        ).stdout.decode("utf-8", errors="replace")
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
        output = self.read_shell("wm", "size")
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
        output = self.read_shell("dumpsys", "window")
        for line in output.splitlines():
            if "mCurrentFocus" in line and "/" in line:
                # Window{... com.example/.MainActivity}
                focus = line.split("/")
                package = focus[0].split()[-1]
                activity = focus[1].rstrip("}").strip()
                return package, activity
        return "", ""
