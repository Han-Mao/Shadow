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
from .controller import ShadowActionUnsupported

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


@dataclass(frozen=True)
class TaskPlane:
    """一个任务**实际**会在哪个平面执行（V5 §三，审查 P0 ①②③）。

    ━━━ 为什么必须区分「声明」与「实际」━━━

    `Task.execution_mode` 是**用户的要求**（requested）。它不等于任务最终落在哪：

        requested = HYBRID
        影子不可用
            ↓
        resolved  = FOREGROUND   ← 真正会发生的事

    在 `TaskPlane` 出现之前，Scheduler 与 Runtime 都直接读 `task.execution_mode`，
    于是同一个 `HYBRID` 被两处读成两种东西：

    - Scheduler 认为「是影子」→ 不触发抢占 → **但两个任务其实都在 Display 0**
    - Runtime  认为「是影子」→ 不因用户操作暂停 → **但用户和 Agent 抢同一块屏**

    两者都是真实的并发/打扰缺陷。统一走这一个解析入口之后，下游读到的
    `resolved` 就是事实，不会再有第二种解释。

    ━━━ 为什么不直接改 `Task.execution_mode` ━━━

    因为「用户要求什么」和「实际发生什么」都要能用——`/tasks/{id}` 要显示用户
    提交的模式，Checkpoint 恢复时也要知道原始要求，否则恢复出来一个
    `foreground` 的任务，用户再也看不出它本来申请的是 `hybrid`。
    所以这里是**新增一列事实**，而不是改写用户输入。
    """

    requested: ExecutionMode
    """用户提交/持久化的模式。"""

    resolved: ExecutionMode
    """实际会在哪个平面执行。影子不可用时 `HYBRID` 在这里变成 `FOREGROUND`。"""

    degraded: bool = False
    """`resolved != requested` 时为 True（目前只有 HYBRID 回落这一种来源）。

    单独立一个字段而不是让调用方自己比，是因为「降级发生了」这个事实本身要
    进日志、进 `/tasks/{id}`、进 Checkpoint——让每个调用点各写一次比较，
    早晚会有一处漏掉。
    """

    reason: str = ""

    @property
    def occupies_user_display(self) -> bool:
        """这个任务会不会占用用户那块屏（Display 0）。

        名字刻意不叫 `requires_foreground`：它回答的不是「要不要真人」，
        而是「会不会和用户抢同一块屏幕」——这正是 Scheduler 判抢占与 Runtime
        判用户暂停**共同**需要的那个事实。两处若各写各的判断，就是审查里
        那两个 bug 的来源。
        """
        return self.resolved is ExecutionMode.FOREGROUND

    @property
    def is_shadow(self) -> bool:
        """实际跑在影子平面（用户与它互不相干）。"""
        return self.resolved is ExecutionMode.SHADOW

    def snapshot(self) -> dict[str, Any]:
        return {
            "requested": self.requested.value,
            "resolved": self.resolved.value,
            "degraded": self.degraded,
            "reason": self.reason,
        }


def resolve_task_plane(
    requested: object,
    *,
    shadow_available: bool | None = None,
    requirement: ExecutionRequirement | None = None,
    task_id: str = "",
    strict: bool = False,
) -> TaskPlane:
    """把「任务声明的模式」解析成「实际执行平面」（**唯一入口**）。

    下游（Scheduler 判抢占、Runtime 判用户暂停、Checkpoint 记录、`/tasks/{id}`）
    一律读这里的结果，不许再自己去比 `task.execution_mode`——那正是审查 §三/§四
    两个缺陷的根因。

    ━━━ 四个参数里最要紧的是 `shadow_available` ━━━

    它是**三态**（`True` / `False` / `None`），与 `UserContext.confirmed` 同一个
    纪律：`None` = 「不知道本端有没有影子平面」，不是「没有」。

    - `True`：有 → `SHADOW`/`HYBRID` 都进影子。
    - `False`：确认没有 → `SHADOW` 按 `strict` 处置，`HYBRID` 回落前台。
    - `None`：不知道 → **按 `False` 处理**，但 `reason` 里写明是「未知」而不是
      「不可用」，且 `strict=False` 时 `SHADOW` 也会回落而不是静默走前台。

    为什么 `None` 按「没有」处理：影子平面**不存在**时动作会落到用户的屏幕上，
    这个代价比「任务慢一点」重得多。而在 `strict=True`（Runtime 执行路径）下
    会直接抛 `ShadowSessionUnavailable`——`SHADOW` 是用户明确说「别打扰我」，
    要么做到、要么明确失败。

    ━━━ 为什么不做设备探测 ━━━

    本函数**不碰设备、不建连接**：Scheduler 在挑选任务时要逐任务判断平面，
    那里绝对不能引入 I/O（会在锁内调用）。所以「影子可不可用」由调用方查好后
    传进来；Runtime 侧有真实会话，直接从 `ShadowSession.is_usable` 取。
    """
    mode = normalize_mode(requested)
    req = requirement or ExecutionRequirement.SHADOW_PREFERRED

    if mode is ExecutionMode.FOREGROUND:
        return TaskPlane(requested=mode, resolved=mode)

    # 需要真人的动作恒前台：影子平面里没有真人，绕不过去（§十一）。
    if req is ExecutionRequirement.USER_REQUIRED:
        return TaskPlane(
            requested=mode,
            resolved=ExecutionMode.FOREGROUND,
            degraded=mode is not ExecutionMode.FOREGROUND,
            reason="动作需要真人在场，只能落在用户平面",
        )

    if shadow_available is True:
        return TaskPlane(requested=mode, resolved=ExecutionMode.SHADOW)

    # ---- 到这里：影子确认不可用，或者不知道 ----
    known_missing = shadow_available is False
    why = (
        "本端没有可用的影子平面"
        if known_missing
        else "无法确认本端是否有可用的影子平面（按不可用处置）"
    )

    if mode is ExecutionMode.SHADOW or req is ExecutionRequirement.SHADOW_ONLY:
        if strict:
            raise ShadowSessionUnavailable(
                f"任务 {task_id or '(未知)'} 声明了影子执行，但{why}"
                "（见 device/session.py 的 ShadowSession 说明）"
            )
        # 非严格路径（如调度器只想知道「会不会占 Display 0」）：如实回落并标注，
        # 让调用方据 `degraded` 决定要不要拒绝。**不抛异常**是因为调度器需要
        # 一个可用的答案来排序，抛在这里会让整个队列卡住。
        return TaskPlane(
            requested=mode,
            resolved=ExecutionMode.FOREGROUND,
            degraded=True,
            reason=f"{why}；该任务声明了影子执行，实际会占用用户平面",
        )

    # HYBRID：定义就是「能后台就后台，不能后台才申请前台」，回落是用户预期内的。
    return TaskPlane(
        requested=mode,
        resolved=ExecutionMode.FOREGROUND,
        degraded=True,
        reason=f"{why}；HYBRID 按定义回落到前台平面",
    )


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


# ---------------------------------------------------------------- 影子动作路由（审查 P1⑤）


#: 影子平面必须能路由的动作。**这个集合就是路由器的契约**。
#:
#: 为什么要有这么一个常量，而不是让 `ShadowActionRouter` 有哪些方法就算哪些：
#: 审查 §十 的原话是「不要只是 `shadow_session()`」——它要的是一**整套**动作路由。
#: 把清单显式写出来，才能在测试里对「有没有漏」做检查（见
#: `tests/test_device_port.py::test_shadow_actions_cover_the_foreground_port`）：
#: 前台端口有 `tap/swipe/...`，影子平面就都得有对应的一个，**一个都不能少**。
#: 否则会出现最坏的一种缺陷形状——某个动作只在影子路线上缺了一步，
#: 于是它默默落到了 `self._device`（用户屏幕）上。
SHADOW_ACTION_NAMES: tuple[str, ...] = (
    "screenshot_bytes",
    "dump_ui",
    "tap",
    "long_press",
    "swipe",
    "set_text",
    "back",
    "home",
    "launch",
)


class ShadowActionRouter:
    """把动作路由到影子平面（审查 P1⑤，V5 §五 / §十六）。

    ━━━ 为什么要有这一层，而不是在 `executor` 里写 if ━━━

    上一版只有 `shadow_session()` 一个方法：能建出会话、能问「可不可用」，
    但**没有任何地方能真的往那个平面发一个动作**。于是所有动作仍然走
    `DeviceSession.controller`（= Display 0）。这个缺口很危险，因为它是**静默的**：
    接口看起来齐全（有 shadow 会话、有 display_id），跑起来却全打在用户屏幕上。

    所以这一层把「往哪走」变成一个**必须显式经过**的关卡：任何影子动作都要先
    过 `_shadow_or_fail()`，而它在路由不过去时**抛异常**。这比返回 `None`
    再让调用方 `or self._device` 要安全得多——后者是这一层唯一想禁止的写法。

    ━━━ fail-closed 的含义（本类最重要的性质）━━━

    「路由不过去」时**必须**抛 `ShadowActionUnsupported`，**绝不能**回落到
    `self._device`。理由：`self._device` 是用户的屏幕。一次静默回落 =
    用户正在看的那一屏被 Agent 点了一下，而且**没有任何日志能说明它来自影子任务**。

    这个取舍在当前的现实中尤其重要：真机上影子平面恒不可用（§六），
    所以「永不回落」意味着**所有影子动作都会失败**。那是**正确**的行为——
    失败会被记账、会进 `/executions`、会让用户看到「这个任务做不到」；
    而回落是无声的、不可观测的，且恰恰打扰了它承诺不打扰的人。

    ━━━ 与 `session.controller` 的关系 ━━━

    影子动作**同样**经由设备端口（`controller`）发出，因为跨到 Android 侧
    只有这一条通道。区别在于带上 `target`：`display_id` 告诉桥「往那块屏幕上打」。
    桥若还不支持带显示参数的动作，就该抛 `ShadowActionUnsupported` 而不是
    忽略这个参数——**静默忽略 display_id 等于把动作打到 Display 0 上**。
    """

    def __init__(
        self,
        session: ShadowSession,
        controller: Any | None = None,
        *,
        device_serial: str = "",
    ) -> None:
        self._session = session
        self._controller = controller
        self._device_serial = device_serial

    # ---- 只读 ----

    @property
    def session(self) -> ShadowSession:
        return self._session

    @property
    def display_id(self) -> int | None:
        return self._session.display_id

    @property
    def task_id(self) -> str:
        return self._session.task_id

    @property
    def is_usable(self) -> bool:
        return self._session.is_usable

    def target(self, *, requirement: ExecutionRequirement | None = None) -> ExecutionTarget:
        """影子平面的执行路标。

        `mode` 恒为 `SHADOW`（不是 `HYBRID`）：能走到这个路由器里，说明
        调用方已经**决定**这一步在影子平面做。`HYBRID` 是任务级声明，
        而这里回答的是「这个动作往哪走」——把任务级的模糊带进来，
        会让 `requires_foreground` 这类判断得出错误结论。
        """
        return ExecutionTarget(
            mode=ExecutionMode.SHADOW,
            session_id=self._session.session_id,
            device_serial=self._device_serial,
            display_id=self._session.display_id,
            requirement=requirement or ExecutionRequirement.SHADOW_ONLY,
        )

    # ---- 核心：路由关卡 ----

    def _shadow_or_fail(self, action: str) -> Any:
        """取出影子平面要用的控制器；任何一条不满足都抛。

        检查顺序刻意是「先会话、后控制器」：会话不可用是本端**结构性**缺失，
        它的原因码（`reason`）比「没有控制器」有用得多，用户看到它才知道
        该去开什么。反过来的话，一个不可用的会话会报出「控制器是 None」，
        排查方向被带偏。

        **不做任何回落**。这里一旦出现 `or self._device` 式的兜底，
        本类就退化成了「装作在影子平面执行的前台执行器」。
        """
        self._session.require()  # 不可用 → ShadowSessionUnavailable（带原因）
        if self._controller is None:
            raise ShadowActionUnsupported(
                f"影子动作 {action} 无法路由：会话 {self._session.session_id} 没有绑定控制器"
                "（设备端口缺失，绝不回落到用户屏幕）"
            )
        return self._controller

    def _call(self, action: str, method: str, *args: Any, **kwargs: Any) -> Any:
        """调控制器上一个影子版本的方法。

        **方法名必须带 `shadow_` 前缀**，这是刻意的硬要求而不是命名习惯：
        若允许调同名方法（`tap` → `controller.tap`），那这个类在控制器还没实现
        影子路由时会**静默地**把动作打到 Display 0 上——正是本类要防的唯一一件事。
        带前缀的名字在控制器上不存在时，`getattr` 直接给 `None`，
        于是我们抛 `ShadowActionUnsupported`。

        属性可能**存在但不可调用**（跨语言边界上 Chaquopy / HTTP 代理都给出过
        这种对象，`device/android.py` 的 `shadow_release` 注释里记过同一个坑），
        所以判据是 `callable()`，不是 `hasattr()`。
        """
        controller = self._shadow_or_fail(action)
        bound = getattr(controller, f"shadow_{method}", None)
        if not callable(bound):
            raise ShadowActionUnsupported(
                f"影子动作 {action} 无法路由：设备端口没有实现 shadow_{method}()。"
                f"当前 display={self._session.display_id} 的影子平面无法承载该动作"
                "（本端未实现 = 明确失败，不回落到 Display 0）"
            )
        return bound(*args, **kwargs)

    # ---- 动作（与前台端口一一对应）----

    def screenshot_bytes(self) -> bytes:
        """影子平面截图。"""
        return self._call("screenshot_bytes", "screenshot_bytes")

    def dump_ui(self) -> Any:
        """影子平面的 UI 树。

        与 `device/controller.py` 的同名方法同一条约定：**读不到要抛**，
        不能返回空树——`None`/空树在这里的含义是「这块屏幕是空的」，
        而真相往往是「读不到」。[80]/[92] 已经为此吃过一次亏。
        """
        return self._call("dump_ui", "dump_ui")

    def tap(self, x: int, y: int, *, duration_ms: int = 0) -> None:
        self._call("tap", "tap", x, y, duration_ms=duration_ms)

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self._call("long_press", "long_press", x, y, duration_ms=duration_ms)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._call("swipe", "swipe", x1, y1, x2, y2, duration_ms=duration_ms)

    def set_text(self, value: str) -> None:
        self._call("set_text", "set_text", value)

    def back(self) -> None:
        self._call("back", "back")

    def home(self) -> None:
        self._call("home", "home")

    def launch(self, package: str, activity: str | None = None) -> None:
        """在影子平面启动一个 App。

        这是 P2 的核心难点所在（§十一）：`AccessibilityService` 起不了
        后台独立 App 实例，所以在影子平面启动 App 是本端**真正做不到**的那一步。
        当前它必然抛 `ShadowActionUnsupported`——那是如实的，
        而不是把用户手机上的淘宝切到前台。
        """
        self._call("launch", "launch", package, activity)

    def snapshot(self) -> dict[str, Any]:
        return {
            "session": self._session.snapshot(),
            "display_id": self._session.display_id,
            "routable": self.is_usable and self._controller is not None,
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

    def action_router(self) -> ShadowActionRouter:
        """本会话的影子动作路由（审查 P1⑤）。

        **无论影子平面可不可用都返回一个路由器**，这是刻意的：调用方拿到它之后
        不需要再写 `if usable:` 分支，直接在路由上发动作即可——不可用时每一次
        调用都会抛 `ShadowSessionUnavailable`（带原因码）。

        反过来设计（不可用就返回 `None`）会诱导出
        `router = session.action_router() or foreground_executor` 这种写法，
        而那正是这一层禁止的静默回落。让「做不到」在**动作发生的那一刻**炸出来，
        比在构造路由器时就分开两条路更容易被正确使用。

        控制器取 `self.device.controller`：影子动作与前台动作走的是**同一个**
        设备端口，区别只在动作带不带 display 参数（见 `ShadowActionRouter`）。
        `device` 为空（还没有设备所有权）时给 `None`，路由时抛
        `ShadowActionUnsupported`。
        """
        controller = self.device.controller if self.device else None
        return ShadowActionRouter(
            self.shadow or ShadowSession(session_id="", task_id=self.task_id),
            controller,
            device_serial=self.device.serial if self.device else "",
        )

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

    def task_plane(self, *, requirement: ExecutionRequirement | None = None) -> TaskPlane:
        """本会话对应的 `TaskPlane`（含 requested/resolved 两列）。

        与 `resolve_plane()` 的区别：`resolve_plane()` 只回答「这个动作落在哪」，
        本方法回答「这个任务整体在哪」，并把**降级这件事**如实带出来。
        下游（Runtime 写 state、Checkpoint 落盘、`/tasks/{id}`）要的是后者——
        只拿到结果平面的话，「HYBRID 被降级了」这个事实就丢了。
        """
        usable = bool(self.shadow and self.shadow.is_usable)
        return resolve_task_plane(
            self.mode,
            shadow_available=usable,
            requirement=requirement or self.requirement,
            task_id=self.task_id,
        )

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
    "ShadowActionRouter",
    "ShadowActionUnsupported",
    "ShadowSession",
    "ShadowSessionUnavailable",
    "TaskPlane",
    "create_execution_session",
    "create_shadow_session",
    "resolve_task_plane",
    "new_session_id",
]
