"""UserActivityMonitor：用户是不是正在用手机（V5 §十 / §十一，依据 `问题修复.md`）。

**为什么 Agent 需要知道用户在不在用手机**

Shadow 原本的假设是「这台手机只归 Agent 用」——`DeviceSession` 拿锁、执行、还锁。
可交付到真机上，用户和 Agent 是**共用同一块屏幕**的两个人。于是有两个必须回答的问题：

1. **能不能现在做？**（§十四 第一阶段）用户手指正在屏幕上，Agent 再发一个
   `tap()` 就是打到用户正在看的页面上——轻则两边互相打断，重则 Agent 在用户
   翻看聊天记录时点了个「发送」。所以要在动作发出前知道「用户是不是刚碰过屏幕」。

2. **这件事能不能背着人做？**（§十一）有些动作物理上就要真人：
   支付确认、OTP、生物识别、系统授权、相机、NFC。这些不是「危险所以拦」，
   是「程序根本替不了」。影子平面也绕不过——所以用户活动与 `ExecutionRequirement`
   是**两件事**，不要混成一个判断。

------
**这份接口最要紧的一条约定：读不到 ≠ 用户不在。**

`UserContext.active` 的三态语义必须区分清楚：

    active=True    有证据表明用户刚操作过（触摸/切应用/解锁）
    active=False   **有证据**表明用户没在操作

而「设备不支持探测」「命令超时」「后端没实现」这三种情况，**既不是 True 也不是
简单的 False**——它们是「不知道」。把它当成 `active=False` 就等于把
「我不知道用户在不在」翻译成「用户不在，agent 可以随便点」，而这个错误的代价是
动作打到了用户脸上。所以：

- `UserContext.confirmed` 标记「这次读数是不是有依据的」；
- 读不到时 `confirmed=False`，`active` 取**保守值 True**（当作用户在场），
  并带上 `reason` 说明为什么不知道；
- 需要「宁可暂停也别乱点」的调用方看 `active`；需要「区分不知道与不在」的调用方看
  `confirmed`。

这条与 [80]/[92]（「读不到」≠「空」，证据缺口是一等事实）是同一条纪律，
只是换了一个对象——那里是 UI 树，这里是用户在场与否。

------
**放在端口层（`device/`）而不是新的 Android 目录**：文档 §十 画的是
`android/device/user_activity_monitor.kt`，但探测手段**每个后端都不一样**——
ADB 侧要靠 `dumpsys input` / `power` 之类的只读命令，Android 侧要靠进程内的
`AccessibilityService` 事件或 `PowerManager`。这与 `build_input_provider` 是同一类
问题（「输入通道是后端自己的事」），所以放在 `DeviceController` 旁边，由各后端实现。
Android 侧的具体实现见 `android/.../device/UserActivityMonitor.kt`。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# 「用户刚操作过」的时间窗（秒）。默认 2 秒，与文档 §十四 的
# 「用户停止操作 2 秒 → 重新 Observe → 恢复」对齐。
DEFAULT_IDLE_THRESHOLD = 2.0

# 探测命令的默认超时。用户活动探测是**安全关键**的一次读，不能因为一次 dumpsys
# 卡住就把整个任务拖住——超时后返回「不知道」（保守态），由调用方决定要不要暂停。
DEFAULT_PROBE_TIMEOUT = 1.5


@dataclass(frozen=True)
class UserContext:
    """用户在不在用手机的一次快照（V5 §十）。

    与文档 §十 给的 `UserContext{active, foreground_package, last_user_input_at}`
    相比多了一个 `confirmed`：那是这份接口的核心（见模块 docstring 的三态约定）。
    `active` 在 `confirmed=False` 时是**保守取值**，不是事实。
    """

    active: bool = False
    """用户是否正在操作（或刚操作过，在 `idle_threshold` 窗口内）。

    注意：`confirmed=False` 时这个值是保守的 `True`，读它之前先看 `confirmed`。
    """

    confirmed: bool = False
    """这次读数**有没有依据**。

    `False` 表示「探测失败 / 本端不支持 / 命令超时」——此时 `active` 是保守值，
    不代表观测到的事实。需要区分「用户真的不在」和「我们不知道」时必须看这个字段。
    """

    foreground_package: str | None = None
    """当前前台应用。拿不到时 `None`（不是空串——空串是「查到了，但没有应用」）。"""

    last_user_input_at: float | None = None
    """最后一次用户输入的时间戳（`time.monotonic()` 基准）。

    `None` = 不知道。**不要**用 `0.0` 表示「很久没输入」——那是编造的证据。
    """

    screen_locked: bool | None = None
    """锁屏状态。`None` = 不知道（与「没锁屏」不是一回事）。"""

    reason: str = ""
    """`confirmed=False` 时说明为什么不知道，进日志与审计。"""

    idle_seconds: float | None = None
    """距离最后一次用户输入过了多久。`None` = 不知道。"""

    def is_idle(self, threshold: float = DEFAULT_IDLE_THRESHOLD) -> bool:
        """用户是否**确实**已经停手超过 `threshold` 秒。

        判定要求 `confirmed=True`：拿不准的时候**不**返回 True。
        这个方法是「可以安全恢复执行了吗」这个问题的答案，而它必须建立在
        有证据的基础上——`not active` 在未确认时也成立，但它只说明「没观测到用户」。
        """
        if not self.confirmed:
            return False
        if self.screen_locked:
            # 锁屏时用户显然不在操作，但截图/手势在锁屏下也不该做——交给上层判断，
            # 这里只回答「用户停手了没有」，所以锁屏算空闲。
            return True
        if self.idle_seconds is None:
            return False
        return self.idle_seconds >= threshold

    def describe(self) -> str:
        lock = "" if self.screen_locked is None else f" locked={self.screen_locked}"
        pkg = f" fg={self.foreground_package}" if self.foreground_package else ""
        if not self.confirmed:
            return f"unknown({self.reason or 'no evidence'})"
        return f"active={self.active} idle={self.idle_seconds}{pkg}{lock}"


class UnsupportedUserActivityError(RuntimeError):
    """本后端不支持用户活动探测。

    单独一个类型而不是让实现返回「空上下文」：不支持与「查了但不知道」在运维上
    是完全不同的两件事——前者要在装配期就知道（好决定这个后端能不能跑
    HYBRID/SHADOW 模式），后者只是某一次读数失败。
    """


@runtime_checkable
class UserActivityMonitor(Protocol):
    """用户活动探测端口（V5 §十）。

    实现必须**永不抛异常**（除非本端完全不支持，那就抛 `UnsupportedUserActivityError`）：
    探测失败要以 `UserContext(confirmed=False, reason=…)` 的形式返回。
    理由与 `executor` 的「永不抛异常」相同——用户活动探测挂在执行循环的安全点上，
    让它抛异常会把「探测失败」升级成「任务失败」，而正确的处置是「保守暂停」。
    """

    def user_context(self, *, idle_threshold: float = DEFAULT_IDLE_THRESHOLD) -> UserContext:
        """读一次用户活动快照。**不改变设备状态**（这是只读操作）。"""
        ...

    def supports_user_activity(self) -> bool:
        """本后端能否探测用户活动。

        装配期问一次，用来决定 `SHADOW` / `HYBRID` 模式的任务能不能在这个后端上跑
        （见 `device/factory.py` 与 `agent/scheduler.py`）。
        """
        ...


class NullUserActivityMonitor:
    """默认实现：**不知道**（不是「用户不在」）。

    它给所有还不支持探测的后端兜底，让「用户活动」这件事从第一天起就在类型系统里，
    而不是等某个后端实现了才出现在调用点上。返回 `confirmed=False` 保证
    「不支持」不会被误读成「用户空闲」——保守方向是「当作用户在操作」。
    """

    def __init__(self, reason: str = "本后端未实现用户活动探测") -> None:
        self._reason = reason

    def user_context(self, *, idle_threshold: float = DEFAULT_IDLE_THRESHOLD) -> UserContext:
        return UserContext(
            # 保守：当作用户在场。调用方若据此暂停，代价是「任务慢了一点」；
            # 若反过来当作空闲，代价是「动作打到用户脸上」。
            active=True,
            confirmed=False,
            reason=self._reason,
        )

    def supports_user_activity(self) -> bool:
        return False


class ScriptedUserActivityMonitor:
    """把「最后一次用户输入时间」当成构造参数的活动监测器。

    存在的理由是**让探测这件事可以被离线测试**：ADB 与 Android 两个真实现都是
    「查一次、拿一个时间戳」，所以只要把「查」这一步抽成可注入的回调，
    「2 秒内用户碰过 → 暂停」「3 秒没碰 → 恢复」这些**策略**就能全离线验证。
    策略本身（在 `agent/runtime.py` 的 `safe_point`）才是这一轮要保护的东西。

    `now` 可注入是为了让用例不用真的 `sleep(2)` ——挂钟依赖的测试既慢又不稳定。
    """

    def __init__(
        self,
        *,
        last_input_at: float | None = None,
        foreground_package: str | None = None,
        screen_locked: bool | None = False,
        clock=time.monotonic,
        read_error: str = "",
    ) -> None:
        self.last_input_at = last_input_at
        self.foreground_package = foreground_package
        self.screen_locked = screen_locked
        self._clock = clock
        self._read_error = read_error
        self._lock = threading.Lock()

    def touch(self, *, at: float | None = None) -> None:
        """模拟用户刚操作了一下。"""
        with self._lock:
            self.last_input_at = self._clock() if at is None else at

    def user_context(self, *, idle_threshold: float = DEFAULT_IDLE_THRESHOLD) -> UserContext:
        if self._read_error:
            # 模拟真实后端的读失败：**不抛异常**，而是返回「不知道」。
            # 这条路径必须存在，因为它是本模块最核心的约定（读不到 ≠ 用户不在）。
            return UserContext(active=True, confirmed=False, reason=self._read_error)
        now = self._clock()
        with self._lock:
            last = self.last_input_at
        if last is None:
            return UserContext(
                active=True,
                confirmed=False,
                foreground_package=self.foreground_package,
                screen_locked=self.screen_locked,
                reason="没有用户输入时间戳",
            )
        idle = max(0.0, now - last)
        return UserContext(
            active=idle < idle_threshold,
            confirmed=True,
            foreground_package=self.foreground_package,
            last_user_input_at=last,
            screen_locked=self.screen_locked,
            idle_seconds=idle,
        )

    def supports_user_activity(self) -> bool:
        return True


__all__ = [
    "DEFAULT_IDLE_THRESHOLD",
    "DEFAULT_PROBE_TIMEOUT",
    "NullUserActivityMonitor",
    "ScriptedUserActivityMonitor",
    "UnsupportedUserActivityError",
    "UserActivityMonitor",
    "UserContext",
]
