"""DeviceSession（V2 §十三）：设备所有权与会话。

原来的 `_DEVICE_LOCK` 只表达「别同时点」，拿不到锁就 409。
DeviceSession 进一步回答三个问题：**谁在占用设备、能不能被抢占、抢占后怎么交接**。

V5 §八 起这个文件里住着**两个不同层次的概念**，它们以前混在一起：

    DeviceSession     「这台手机」——所有权、抢占、交接（设备身份）
    ExecutionSession  「这个任务在哪里运行」——执行平面 + 影子会话（会话身份）

混在一起的代价是：`DeviceSession.owner` 回答的是「哪个 Agent 拿设备控制权」，
而它在架构上被误用成「用户和 Agent 谁在用手机」的答案。当用户拿起手机时，
`owner` 依然是 Agent 的任务 id——因为设备所有权确实还在 Agent 手上，
只是**用户也在用这块屏幕**。两件事共用一个字段，就没有地方表达
「Agent 持有设备，但必须让开用户的屏幕」。

`ExecutionSession` 就是补上这个缺口的那层：它把「在哪里执行」（`ExecutionMode`）
与「设备的所有权」（`DeviceSession`）分开表达，见 `models/execution_mode.py`。
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from models.execution_mode import (
    ExecutionMode,
    ExecutionRequirement,
    ExecutionTarget,
    normalize_mode,
)

logger = logging.getLogger(__name__)


class DeviceBusyError(RuntimeError):
    """设备被别的任务占用。"""


class ShadowSessionUnavailable(RuntimeError):
    """影子执行平面不可用，而这个任务又**不能**退回前台执行。

    单列一个类型是刻意的：它与 `DeviceBusyError`（等一下就好）和
    `DeviceError`（设备坏了）都不同——它是**结构性缺失**（本端没有影子平面），
    重试和等待都不会让它变好，要么换后端、要么请用户改 `execution_mode`、
    要么把这个任务转人工。上层据此选择正确的处置，而不是无脑重试。
    """


class DeviceSession:
    """单设备的会话所有权。

    所有权用一把 Lock 表达：拿到锁 = 持有设备；`owner` 只是给状态接口看的注解。
    """

    def __init__(self, controller: Any, serial: str = "") -> None:
        self._controller = controller
        self._serial = serial
        self._device_lock = threading.Lock()
        self._meta_lock = threading.RLock()
        self._owner: str | None = None
        self._preempt_for: str | None = None
        # 设备状态的「代次」（V2.2 §九）：每次有人拿到设备、或每发出一个动作就 +1。
        # 观察这类只读接口不加锁（否则任务跑起来连设备都查不到），但消费者必须能
        # 分辨「这是一次稳定观察」还是「中途读到动画/半更新的一屏」——
        # 拿观察前后的代次一比就知道。
        self._generation = 0

    # ---- 只读状态 ----

    @property
    def controller(self) -> Any:
        return self._controller

    @property
    def serial(self) -> str:
        return self._serial

    @property
    def generation(self) -> int:
        """设备状态的代次。同一代次内的观察是稳定的。"""
        with self._meta_lock:
            return self._generation

    def bump_generation(self) -> int:
        """标记「设备状态可能变了」。"""
        with self._meta_lock:
            self._generation += 1
            return self._generation

    @property
    def owner(self) -> str | None:
        with self._meta_lock:
            return self._owner

    @property
    def busy(self) -> bool:
        return self.owner is not None

    def owned_by(self, task_id: str) -> bool:
        return self.owner == task_id

    def snapshot(self) -> dict[str, Any]:
        with self._meta_lock:
            return {
                "serial": self._serial,
                "owner": self._owner,
                "busy": self._owner is not None,
                "preempt_requested_for": self._preempt_for,
                "generation": self._generation,
            }

    # ---- 所有权 ----

    def acquire(self, task_id: str, timeout: float = 0.0) -> bool:
        """取得设备。timeout=0 表示不等待，拿不到立刻返回 False。"""
        acquired = (
            self._device_lock.acquire(timeout=timeout)
            if timeout > 0
            else self._device_lock.acquire(blocking=False)
        )
        if acquired:
            with self._meta_lock:
                self._owner = task_id
                self._preempt_for = None
                # 换了持有者 = 设备状态可能变 → 代次 +1
                self._generation += 1
            logger.debug("任务 %s 取得设备 %s", task_id, self._serial)
        return acquired

    def release(self, task_id: str) -> bool:
        """交还设备。

        非持有者调用只记警告并返回 False —— 释放动作经常写在 finally 里，
        这里抛异常会把真正的失败原因盖掉。
        """
        with self._meta_lock:
            if self._owner != task_id:
                logger.warning(
                    "任务 %s 试图释放设备，但当前持有者是 %s，已忽略", task_id, self._owner
                )
                return False
            self._owner = None
            self._preempt_for = None
        self._device_lock.release()
        logger.debug("任务 %s 交还设备 %s", task_id, self._serial)
        return True

    @contextmanager
    def owned(self, task_id: str) -> Iterator[Any]:
        """在执行动作前确认自己仍持有设备。

        抢占是异步发生的：Scheduler 只会给当前持有者打标记，真正让出发生在
        持有者自己的安全点上。因此每一步执行前都要复查一次所有权。
        """
        if not self.owned_by(task_id):
            raise DeviceBusyError(f"任务 {task_id} 当前不持有设备（持有者：{self.owner}）")
        # 一个动作就要发出去了 → 设备状态即将改变，代次 +1。
        # 这样并发的只读观察能看出「我这次观察跨越了一个动作」
        self.bump_generation()
        yield self._controller

    # ---- 抢占 ----

    def request_preempt(self, by_task_id: str) -> bool:
        """请求当前持有者让出设备（不强制中断，等它在安全点交接）。"""
        with self._meta_lock:
            if self._owner is None:
                return False
            if self._preempt_for == by_task_id:
                return True  # 同一个等待方重复请求，不必再记一遍
            self._preempt_for = by_task_id
            logger.info("请求任务 %s 让出设备，等待方 %s", self._owner, by_task_id)
            return True

    def should_yield(self, task_id: str) -> bool:
        """当前持有者是否应该让出（供 runtime 在循环安全点检查）。"""
        with self._meta_lock:
            return self._owner == task_id and self._preempt_for is not None

    def clear_preempt(self) -> None:
        with self._meta_lock:
            self._preempt_for = None


# ---------------------------------------------------------------- 执行会话（V5 §八）


@dataclass
class ShadowSession:
    """一个任务在影子平面里的执行环境（V5 §五 / §八）。

    ━━━ 它是什么、不是什么 ━━━

    **是**：任务与「影子执行环境」之间的绑定关系（`task_id` ↔ `session_id` ↔
    `display_id`），以及这个环境当前可用性的记账。

    **不是**：一个已经实现的虚拟显示。文档 §五 建议的实现是
    `ShadowDisplayManager.createSession(taskId)` 真正创建一个独立显示并启动淘宝实例；
    那要求 Android 侧的虚拟显示能力，而 §六 已经明确指出
    **`MediaProjection` ≠ 后台独立屏幕、`AccessibilityService` ≠ 后台独立 App 实例**。

    所以这里的立场是：**对象与语义先立起来，实现如实报告自己做不到**。
    `create_shadow_session()` 在缺乏真实影子平面时返回一个
    `available=False` 的会话，任何试图在它上面执行的动作都会拿到
    `ShadowSessionUnavailable` —— 而**不会**静默退回到用户的屏幕。
    这是这一层最重要的性质：宁可不做，也不要偷偷打扰用户。

    （为什么「先建对象再等实现」不算过度设计：`execution_mode=shadow` 这个 API
    从第一天起就要能被提交、被持久化、被 `/tasks` 读出来、被调度器区分对待。
    这些都不依赖底层虚拟显示存在，而它们决定了以后接上真实现时会不会要重写一遍上层。）
    """

    session_id: str
    task_id: str
    display_id: int | None = None
    available: bool = False
    """影子平面是否**真的可用**。

    `False` 表示「语义上有这个会话，但本端还没有能力创建独立显示」。
    消费方必须据此拒绝执行，而不是当作「等同于前台」。
    """

    reason: str = ""
    """`available=False` 时说明为什么不可用（进日志与 `/tasks/{id}`）。"""

    @property
    def is_usable(self) -> bool:
        return self.available and self.display_id is not None

    def require(self) -> None:
        """在执行任何影子动作前调用；不可用就抛。

        **为什么必须抛而不是返回 bool**：返回 bool 会在调用点被写成
        `if not usable: 走前台吧` —— 那就正好是这一层要防的事
        （把「影子做不到」翻译成「那就打扰用户」）。
        让调用方必须显式处理这个异常，它才会去考虑「这个动作到底该不该落到
        Display 0 上」。
        """
        if not self.is_usable:
            raise ShadowSessionUnavailable(
                f"影子执行平面不可用（session={self.session_id}）：{self.reason or '本端未实现独立显示'}"
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "display_id": self.display_id,
            "available": self.available,
            "usable": self.is_usable,
            "reason": self.reason,
        }


def new_session_id(task_id: str) -> str:
    """影子会话 id（与 Checkpoint 的 id 规则同源：任务 id + 随机后缀）。

    带随机后缀是因为同一任务可以被重新派到影子平面（恢复时重建会话），
    而「重建」必须得到一个**新** id —— 老 id 上可能还挂着上一个环境的残留状态，
    复用它会让「这个 display 是不是我上次那个」变成无法回答的问题。
    """
    import uuid

    return f"shadow_{task_id}_{uuid.uuid4().hex[:8]}"


@dataclass
class ExecutionSession:
    """「这个任务在哪里运行」（V5 §八）。

    ━━━ 与 `DeviceSession` 的分工（这两个概念以前是混的）━━━

        DeviceSession     描述「这台手机」：谁持有、能不能抢占、怎么交接
        ExecutionSession  描述「这个任务在哪里运行」：前台平面，还是影子平面

    它们**不是包含关系**而是正交的：影子平面同样跑在同一台手机上，同样要经过
    `DeviceSession` 的设备端口去读 UI、发手势。区别只在于**打到哪个显示上**。

    所以 `ExecutionSession` 持有 `device`（设备所有权句柄）与 `shadow`
    （影子环境句柄，可能不可用），由 `ExecutionTarget` 把两者合成一个
    「往哪走」的路标交给执行器（见 `models/execution_mode.py` 的职责拆分说明：
    planner 只回答「下一步做什么」，`ExecutionTarget` 回答「在哪里做」）。
    """

    task_id: str
    mode: ExecutionMode = ExecutionMode.FOREGROUND
    device: DeviceSession | None = None
    shadow: ShadowSession | None = None
    # 用户此刻在不在用手机（V5 §十）。由 runtime 的安全点每次刷新，
    # 放这里是为了让「执行决策」有一个统一的读取点，而不是到处去问监测器。
    user_active: bool = False
    user_confirmed: bool = False
    """用户活动读数的可信度。`False` = 不知道（不是「用户不在」）。

    这两个字段必须**同时**看：`user_active=False, user_confirmed=False` 的含义是
    「我们不知道用户在哪」，处置上应当向「用户在操作」靠（保守）。
    只读 `user_active` 就会把「不知道」当成「用户不在」——那正是模块
    `device/user_activity.py` 花整段 docstring 在防的事。
    """

    foreground_package: str | None = None
    requirement: ExecutionRequirement = ExecutionRequirement.SHADOW_PREFERRED

    @property
    def display_id(self) -> int | None:
        """当前执行落在哪个显示上。

        前台平面恒为 **0**（真实 Display 0，这是 Android 的约定）；
        影子平面取会话里记录的 display id（拿不到就是 `None`，**不是** 0——
        「不知道影子显示是几号」与「就打到用户屏幕上」是两件完全不同的事）。
        """
        if self.mode is ExecutionMode.FOREGROUND:
            return 0
        return self.shadow.display_id if self.shadow else None

    def target(self, *, requirement: ExecutionRequirement | None = None) -> ExecutionTarget:
        """合成交给执行器的执行路标。"""
        return ExecutionTarget(
            mode=self.mode,
            session_id=self.shadow.session_id if self.shadow else "",
            device_serial=self.device.serial if self.device else "",
            display_id=self.display_id,
            requirement=requirement or self.requirement,
        )

    def resolve_plane(self, *, requirement: ExecutionRequirement | None = None) -> ExecutionMode:
        """这个动作最终落在哪个平面。

        三档 `ExecutionMode` 的收窄逻辑（文档 §三 / §十一）：

        - `FOREGROUND` → 恒前台（用户自己选的，不改）。
        - `USER_REQUIRED` 的动作 → 恒前台：它要真人在场，影子绕不过（§十一 的支付宝例子）。
        - `SHADOW` 任务 + 影子可用 → 影子。
        - `SHADOW` 任务 + 影子**不可用** → 抛 `ShadowSessionUnavailable`。
          不降级是这一层的核心决定：用户明确说了「别打扰我」，我们就要么做到、
          要么明确失败，不能悄悄改成打扰他。
        - `HYBRID` + 影子可用 → 影子；不可用 → 回落前台。
          `HYBRID` 的定义就是「能后台的后台，不能后台的才申请前台」，
          所以这里的回落是**用户预期内**的（§三），不算失约。
        """
        req = requirement or self.requirement
        if self.mode is ExecutionMode.FOREGROUND:
            return ExecutionMode.FOREGROUND
        if req is ExecutionRequirement.USER_REQUIRED:
            # 需要真人的动作无论什么模式都要转人工；返回 FOREGROUND 表示
            # 「必须占用真实用户在场的那个平面」，由风险门禁去走确认流程。
            return ExecutionMode.FOREGROUND
        if self.shadow and self.shadow.is_usable:
            return ExecutionMode.SHADOW
        if self.mode is ExecutionMode.SHADOW or req is ExecutionRequirement.SHADOW_ONLY:
            if self.shadow:
                self.shadow.require()  # 抛出带原因的异常
            raise ShadowSessionUnavailable(
                f"任务 {self.task_id} 声明了影子执行，但本端没有可用的影子平面"
                "（见 device/session.py 的 ShadowSession 说明）"
            )
        return ExecutionMode.FOREGROUND

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "mode": self.mode.value,
            "display_id": self.display_id,
            "planar": {
                "user_active": self.user_active,
                "user_confirmed": self.user_confirmed,
                "foreground_package": self.foreground_package,
            },
            "shadow": self.shadow.snapshot() if self.shadow else None,
            "device": self.device.snapshot() if self.device else None,
        }


def create_execution_session(
    task_id: str,
    *,
    mode: object = ExecutionMode.FOREGROUND,
    device: DeviceSession | None = None,
    shadow: ShadowSession | None = None,
) -> ExecutionSession:
    """按任务的 `execution_mode` 造一个执行会话。

    `mode` 用 `normalize_mode` 收敛（认不出来时回退 `FOREGROUND`，见
    `models/execution_mode.py` 里对这个方向的解释：回退到前台最坏是打扰用户，
    回退到影子最坏是动作不知道打到哪去了）。
    """
    resolved = normalize_mode(mode)
    if resolved is ExecutionMode.FOREGROUND:
        # 前台平面不需要影子会话；即使调用方给了也丢掉，避免「前台任务
        # 意外拿到一个影子 display」这种自相矛盾的状态。
        shadow = None
    return ExecutionSession(task_id=task_id, mode=resolved, device=device, shadow=shadow)


def create_shadow_session(
    task_id: str,
    *,
    display_id: int | None = None,
    available: bool = False,
    reason: str = "",
) -> ShadowSession:
    """建一个影子会话（V5 §五）。

    **默认 `available=False`**，这不是偷懒而是当前的事实：真实创建独立显示需要
    Android 侧实现 `ShadowDisplayManager`，而 §六 已经说明现有权限组合
    （AccessibilityService + MediaProjection + ForegroundService）**不能**凭空
    产生一个独立的 App/UI 执行空间。

    哪一天 Android 侧真能创建虚拟显示，调用点把它改为 `available=True` 并传上
    `display_id` 即可——上层（调度器、runtime、Checkpoint）不需要改，
    因为它们读的是 `is_usable` 而不是「有没有这个对象」。
    """
    return ShadowSession(
        session_id=new_session_id(task_id),
        task_id=task_id,
        display_id=display_id,
        available=available,
        reason=reason or "本端尚未实现影子显示（Android 侧 ShadowDisplayManager）",
    )


__all__ = [
    "DeviceBusyError",
    "DeviceSession",
    "ExecutionSession",
    "ShadowSession",
    "ShadowSessionUnavailable",
    "create_execution_session",
    "create_shadow_session",
    "new_session_id",
]
