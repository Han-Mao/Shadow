"""`DeviceController` 端口的 **ADB 实现**（PC 侧）。不暴露给 Agent。

端口契约见 `device/controller.py`；手机本机运行时的对等实现见 `device/android.py`。
两者共用同一套 TaskManager / Scheduler / AgentRuntime / RiskGate / Checkpoint——
这个文件与那个文件之间的差别，就是「PC 控制手机」和「手机自己跑」的全部差别。
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from models.action import DURATION_RANGE_MS  # noqa: F401 - re-export，老导入路径继续有效

from .controller import (
    READ_ONLY_OPERATIONS,
    DeviceBudgetExhausted,
    DeviceError,
    is_read_only,
)
from .session import ShadowActionUnsupported
from .user_activity import DEFAULT_IDLE_THRESHOLD, UserContext

DEFAULT_SERIAL = "emulator-5554"
REMOTE_UI_DUMP = "/sdcard/window_dump.xml"
# 校验的是「用户原始输入」：空格允许，% 禁止（% 是 ADB input text 的转义引导符，
# 放行会让 "100%" 这类输入被设备当成非法转义）
TYPE_SAFE_PATTERN = re.compile(r"^[a-zA-Z0-9_.@,/?! ]+$")

# `shadow_*` 动作在 ADB 后端上统一的拒绝理由（审查 P1⑤）。
#
# 抽成一个常量是为了让**九条拒绝路径说同一句话**：理由只该有一份，
# 否则很容易出现「三处说了原理、六处只说未实现」，而用户读到的恰好是后者，
# 于是他会去等一个永远不会到来的更新。
_ADB_SHADOW_REFUSAL = (
    "ADB 后端没有影子平面：input tap/swipe/text 与 screencap 都只作用于当前显示"
    "（Display 0），命令本身没有任何参数能指向另一块屏幕"
)

# 当前上下文的命令总预算截止时刻（monotonic 秒）。见 `deadline_budget`。
_DEADLINE: ContextVar[float | None] = ContextVar("adb_deadline", default=None)


class AdbError(DeviceError):
    """ADB 相关失败。

    继承 `DeviceError`（V3.3 §1）而不是直接继承 `RuntimeError`：核心的失败收敛
    只写一条 `except DeviceError` 就能同时接住 ADB 与 Android 两种后端。
    `AdbError` 这个名字与原有捕获点全部保留，换基类对上层透明。
    """


class AdbBudgetExhausted(AdbError, DeviceBudgetExhausted):
    """总预算用完——**不是**设备坏了，而是我们主动不再往下等。

    单独一个类型是为了让上层能区分「设备真的出错」和「这次采集超预算了」：
    后者重试一次往往就好了，前者重试没用。

    V3.3 起它同时是 `DeviceBudgetExhausted`，于是上层可以不分后端地接住
    「预算耗尽」（Android 侧对应 `AndroidBudgetExhausted`）。
    """


# 只读操作声明与 `is_read_only` 已挪到 `device/controller.py`（V3.3 §1）——
# 那是「Shadow 认为什么算只读」的**约定**，不是 ADB 的实现细节，两种后端共用一份。
# 这里 re-export，老导入路径（`from device.adb import is_read_only`）继续有效。
__all__ = [
    "AdbBudgetExhausted",
    "AdbController",
    "AdbError",
    "DURATION_RANGE_MS",
    "READ_ONLY_OPERATIONS",
    "TYPE_SAFE_PATTERN",
    "escape_type_text",
    "is_read_only",
]


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

    # 按操作类型细分的超时（V2.7 P1-1）：审查指出「写类 15s 一档」太粗——
    # 点一下（tap）和拉冷启动（am start）根本不是一个量级，前者 2-5s 就该够。
    # 细分之后，「让出设备」这个安全点的等待上界进一步收紧：tap 卡住不用干等 15s。
    _INPUT_TIMEOUT = 5.0      # tap / swipe / long_press / back / home / keyevent
    _LAUNCH_TIMEOUT = 15.0    # am start / monkey 冷启动

    # ---- 用户活动探测的采样基线（V5 §十）----
    #
    # 存在**实例**上而不是模块级：它是「每台设备」的事实，两台设备的前台窗口变化
    # 互不相关，共用一个槽位会互相污染。
    #
    # 这三个字段必须写成带 `field(default_factory=…)` 的 dataclass 字段，
    # **不能**用手写 `__init__` 塞进去：本类是个 `@dataclass`，而 dataclass 生成的
    # `__init__` 认的是**类级字段**（`serial` / `timeout` / `read_timeout`）。
    # 手写一个 `__init__(self, serial=...)` 会把它整个顶掉，于是
    # `AdbController(serial="x", timeout=15.0)` 变成 `TypeError: unexpected keyword
    # argument 'timeout'` —— 两个既有用例当场挂掉（V5 第一轮真踩到了）。
    #
    # `repr=False`：锁对象进 repr 没有意义，反而让报错信息变长。
    _activity_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_activity_probe: tuple[str | None, float | None] = (None, None)
    _last_user_change_at: float | None = None

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

    def shell(self, *args: str, timeout: float | None = None) -> str:
        return self._run(["shell", *args], timeout=timeout).stdout.strip()

    def read_shell(self, *args: str) -> str:
        """采集类 shell 命令（`dumpsys` / `wm size` 等），走更紧的 `read_timeout`。"""
        return self._run(["shell", *args], timeout=self.read_timeout).stdout.strip()

    def state(self) -> str:
        return self._run(["get-state"]).stdout.strip()

    # ---- 输入 ----

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y), timeout=self._INPUT_TIMEOUT)

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self.shell(
            "input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms),
            timeout=self._INPUT_TIMEOUT,
        )

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.shell(
            "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms),
            timeout=self._INPUT_TIMEOUT,
        )

    def type_text(self, value: str) -> None:
        """仅支持安全 ASCII；空格转义为 %s；中文需 ADB Keyboard 广播方案（§6.3，M4 处理）。"""
        self.shell("input", "text", escape_type_text(value))

    def keyevent(self, code: str) -> None:
        self.shell("input", "keyevent", code, timeout=self._INPUT_TIMEOUT)

    def back(self) -> None:
        self.keyevent("KEYCODE_BACK")

    def home(self) -> None:
        self.keyevent("KEYCODE_HOME")

    def launch(self, package: str, activity: str | None = None) -> None:
        if activity:
            self.shell("am", "start", "-n", f"{package}/{activity}", timeout=self._LAUNCH_TIMEOUT)
        else:
            self.shell("monkey", "-p", package, "1", timeout=self._LAUNCH_TIMEOUT)

    def launch_app(self, package: str) -> None:
        """按包名启动主界面（= `launch(package, None)`）。

        端口（`device/controller.py`）显式列出这个方法，因为它是最高频的调用形态。
        Android 侧对应 `PackageManager.getLaunchIntentForPackage` + `startActivity`。
        """
        self.launch(package)

    def wait(self, duration_ms: int = 1000) -> None:
        time.sleep(duration_ms / 1000.0)

    def build_input_provider(self):
        """本后端的输入通道：ASCII 走 `input text`，其余走 ADB Keyboard 广播（V2 §十八）。

        延迟 import：`device.input` 会 import 本模块（要 `TYPE_SAFE_PATTERN`），
        模块级互相 import 会成环。端口要求实现这个方法，见 `device/controller.py`。
        """
        from .input import AutoInputProvider, AdbInputProvider, BroadcastInputProvider

        return AutoInputProvider(AdbInputProvider(self), BroadcastInputProvider(self))

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

    # ---- 用户活动（V5 §十）----

    def supports_user_activity(self) -> bool:
        return True

    def shadow_plane_available(self) -> bool | None:
        """ADB 后端**永远**给不出影子平面（V5 P0③）。

        返回 `False` 而不是 `None`（不知道）：这是一个**确定的事实**，不是探测
        失败。ADB 的全部能力是「往这台手机的一块屏幕上发命令」——`input tap`、
        `screencap` 都作用在当前显示（默认 Display 0）上，没有任何参数能指向
        另一块屏幕。所以它不可能承载一个「用户看不见的独立执行平面」。

        说 `False` 的后果对所有 `SHADOW`/`HYBRID` 任务是一样的（都会落到前台），
        与说 `None` 等价；区别只在日志与 `/health` 里能说清「确定没有」还是
        「没上报」，排查时这个区别有用。
        """
        return False

    def user_context(
        self, *, idle_threshold: float = DEFAULT_IDLE_THRESHOLD
    ) -> UserContext:
        """探测用户是不是正在用这台手机（V5 §十）。

        **ADB 侧能做到什么、做不到什么，要说清楚**：

        可以**确实**拿到的是：
        - 锁屏状态（`dumpsys window` / `dumpsys power` 里的 mDreamingLockscreen /
          mScreenOnFully）—— 这是可靠的布尔量。
        - 当前前台应用（复用 `current_focus`）。

        **拿不到**的是「最后一次用户输入的精确时刻」：Android 没有给出一个
        「用户最后一次触摸的 monotonic 时间戳」的公开 adb 接口
        （`dumpsys input` 里有 event 计数，但那是**自开机累计值**，
        不是时间戳；用它做差需要两次采样 + 知道屏幕刷新率，不可靠）。

        所以这里的策略是：**用「这一次采样与上一次采样的前台窗口/事件计数是否变化」
        来近似「用户最近有没有动过」**，并且——这是关键——当**第一次**采样时
        没有基线可比，返回 `confirmed=False`（不知道）而不是「用户不在」。

        这个取舍值得写下来，因为它是本实现最容易被误读的地方：
        它给的是**下限**证据（"我看到变化了" ⇒ 用户在动），不是上限证据
        （"我没看到变化" ⇏ 用户没动——可能只是他没切应用）。
        所以 `active=True` 是可信的，`active=False` 只在
        `confirmed=True` 且确实经历了「变化 → 静默超过阈值」两个阶段时才给出。

        触发条件（什么时候该换掉这个近似）：真机上出现「Agent 在用户翻页时误判空闲」
        的报告，或 Android 侧 `UserActivityMonitor.kt`（有 AccessibilityService 事件流，
        能拿到精确触摸时间）成为主路径时——那时 ADB 侧只作为调试后端的降级实现。
        """
        now = time.monotonic()

        # 1) 锁屏与前台应用：两个可靠的读数，先取。
        locked = self._read_screen_locked()
        package, _activity = self.current_focus()
        fg = package or None

        # 2) 与上一次采样比对：变了 = 用户动过（下限证据）。
        with self._activity_lock:
            prev_fg, prev_at = self._last_activity_probe
            self._last_activity_probe = (fg, now)

        if locked:
            # 锁屏：用户不在操作。这是**有证据**的结论，而且它**先于**基线判断——
            # 「屏幕锁着」这件事不需要历史采样来支撑，什么时候读到都成立。
            # 放在基线判断之后会让首次采样在锁屏时返回「不知道」（已踩，见本文件
            # 的历史注释），而锁屏恰恰是最该果断说「用户不在」的情形。
            return UserContext(
                active=False,
                confirmed=True,
                foreground_package=fg,
                screen_locked=True,
                idle_seconds=None,
                reason="",
            )

        if prev_at is None:
            # 第一次采样，没有基线。**不能**说「用户不在」——那是编造证据。
            return UserContext(
                active=True,
                confirmed=False,
                foreground_package=fg,
                screen_locked=locked,
                reason="首次采样无基线，无法判断用户是否在操作",
            )

        changed = fg != prev_fg
        if changed:
            with self._activity_lock:
                self._last_user_change_at = now
        # 没有变化不等于用户没动——只说明他可能没切应用。
        # 所以这个分支不更新 `_last_user_change_at`，让上一次的变化时间继续计时。

        with self._activity_lock:
            last_change = self._last_user_change_at

        if last_change is None:
            # 经历了至少两次采样、但前台窗口一直没变过。这不是「用户空闲」的证据，
            # 只是「我没观察到变化」——保守当作不确定。
            return UserContext(
                active=True,
                confirmed=False,
                foreground_package=fg,
                screen_locked=locked,
                reason="未观察到前台窗口变化，无法确证用户已停手",
            )

        idle = max(0.0, now - last_change)
        return UserContext(
            active=idle < idle_threshold,
            confirmed=True,
            foreground_package=fg,
            last_user_input_at=last_change,
            screen_locked=locked,
            idle_seconds=idle,
        )

    def _read_screen_locked(self) -> bool | None:
        """读锁屏状态。读不到返回 `None`（**不是** False）。"""
        try:
            output = self.read_shell("dumpsys", "window")
        except Exception as exc:  # noqa: BLE001 —— 探测失败不能拖垮调用方
            logger.debug("读锁屏状态失败：%s", exc)
            return None
        for line in output.splitlines():
            low = line.strip().lower()
            # 两种 ROM 的写法都认：AOSP 用 mDreamingLockscreen，部分厂商用 mShowingLockscreen
            if low.startswith("mdreaminglockscreen") or low.startswith("mshowinglockscreen"):
                return "true" in low
        return None

    # ---- 影子平面动作（审查 P1⑤）----
    #
    # 这一组在 ADB 后端上**永远不会成功**，而这是如实的结论，不是还没做：
    #
    #   `input tap/swipe/text` 与 `screencap` 作用于当前显示（硬件上就是 Display 0），
    #   命令本身**没有任何参数**能指向另一块屏幕。ADB 的全部能力就是
    #   「往这台手机的那一块屏幕上发命令」。
    #
    # 所以这里显式实现它们并抛 `ShadowActionUnsupported`（而不是靠协议基类的
    # `NotImplementedError`），差别在**错误信息**：从错误信息里能直接读出
    # 「ADB 后端原理上做不到，换 Android 后端也一样是另一回事」，
    # 而不是让人以为「这个后端还没实现，将来会补」。
    #
    # 注意它们**不**回落到 `self.tap()`。那正是 `ShadowActionRouter` 花整段注释
    # 在防的事：一次静默回落，用户的屏幕就被点了，而且日志里看不出是影子任务干的。

    def shadow_screenshot_bytes(self) -> bytes:
        raise ShadowActionUnsupported(f"{_ADB_SHADOW_REFUSAL}（无法在影子平面截图）")

    def shadow_dump_ui(self):
        raise ShadowActionUnsupported(f"{_ADB_SHADOW_REFUSAL}（无法读影子平面的 UI 树）")

    def shadow_tap(self, x: int, y: int, *, duration_ms: int = 0) -> None:
        raise ShadowActionUnsupported(f"{_ADB_SHADOW_REFUSAL}（无法在影子平面点击）")

    def shadow_long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        raise ShadowActionUnsupported(f"{_ADB_SHADOW_REFUSAL}（无法在影子平面长按）")

    def shadow_swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        raise ShadowActionUnsupported(f"{_ADB_SHADOW_REFUSAL}（无法在影子平面滑动）")

    def shadow_set_text(self, value: str) -> None:
        raise ShadowActionUnsupported(f"{_ADB_SHADOW_REFUSAL}（无法在影子平面输入文本）")

    def shadow_back(self) -> None:
        raise ShadowActionUnsupported(f"{_ADB_SHADOW_REFUSAL}（无法在影子平面返回）")

    def shadow_home(self) -> None:
        raise ShadowActionUnsupported(f"{_ADB_SHADOW_REFUSAL}（无法在影子平面回主屏）")

    def shadow_launch(self, package: str, activity: str | None = None) -> None:
        raise ShadowActionUnsupported(f"{_ADB_SHADOW_REFUSAL}（无法在影子平面启动应用）")
