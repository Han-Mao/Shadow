"""Android 本机设备适配器（V3.3 §1/§3/§4/§5/§6，依据 `手机部署方案.md`）。

方案文档的核心判断：手机自己当 Agent 主机时，**不该再通过 ADB 控制自己**
（ADB 需要 PC 宿主、需要调试授权、还要装 ADB Keyboard）。手机侧应该走：

    AccessibilityService  →  UI 树（AccessibilityNodeInfo）/ 手势 / 全局动作
    MediaProjection       →  截图
    PackageManager        →  启动应用
    ACTION_SET_TEXT       →  输入文字

但这个 Python 进程**没法直接调 Android API**：Shadow 的核心是 Python，
而 AccessibilityService / MediaProjection 是 Kotlin/Java 侧的组件。所以这里定义一条**桥**：

    AgentRuntime → executor → AndroidDeviceController → AndroidBridge → Kotlin 侧
                                                            ↑
                                              你（Android 端）要实现的就这一个接口

Python 侧只做「协议翻译 + 语义补齐」；Android 侧负责真正碰系统。这样拆的好处是
**桥的实现可以被完整测**（`tests/test_android_adapter.py` 用一个假桥跑通整条任务链），
而不是靠「在真机上试一次能跑」来确认。

━━━ Android 侧必须实现的能力（`AndroidBridge`）━━━

| 桥方法 | Android 侧的对应实现 |
|---|---|
| `screen_size()` | `WindowManager.currentWindowMetrics.bounds`（**实际渲染尺寸**） |
| `current_focus()` | 最近一次 `TYPE_WINDOW_STATE_CHANGED` 事件的 packageName / className |
| `dump_ui()` | 把 `AccessibilityNodeInfo` 树序列化成**与 uiautomator 相同的 XML**（见下） |
| `screenshot_bytes()` | `MediaProjection` + `ImageReader` 抓一帧 PNG |
| `tap` / `long_press` / `swipe` | `AccessibilityService.dispatchGesture`（`GestureDescription`） |
| `set_text(value)` | 焦点节点的 `ACTION_SET_TEXT`（必要时 `InputMethodService` 兜底） |
| `press_back` / `press_home` | `performGlobalAction(GLOBAL_ACTION_BACK / HOME)` |
| `launch(pkg, act)` | `PackageManager.getLaunchIntentForPackage` + `startActivity` |
| `state()` | 服务是否已连上 + 辅助功能/投屏权限是否到位 |

━━━ 最要紧的一条约束：UI 树的格式 ━━━

`dump_ui()` 返回的 XML **必须与 `uiautomator dump` 兼容**：

```xml
<hierarchy>
  <node class="android.widget.Button" text="立即购买" content-desc=""
        resource-id="com.taobao.taobao:id/buy" clickable="true" scrollable="false"
        bounds="[600,1150][760,1250]"/>
</hierarchy>
```

守这条约定，`vision/parser.py`、`vision/target.py`、`vision/grounding.py`、
`agent/evidence.py`、`agent/risk_gate.py` **一行都不用改**——它们本来就按这套约定解析
（`resource-id` 甚至直接进风险判定的 haystack，见 V3 M2）。
如果 Android 侧另造一套格式，就得再写一整套 UI 解析与坐标映射，那是白付的成本。

反过来，`dump_ui` **读不到树时要抛异常**，不要返回空字符串：核心要区分
「页面确实没有可点击元素」和「我们没读到树」（V3.1 P1-4 的目标证据缺口），
返回空串会把后者伪装成前者。
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, runtime_checkable

from .controller import (
    DeviceBudgetExhausted,
    DeviceError,
    assert_implements,
    is_read_only,
)
from .session import ShadowActionUnsupported
from .user_activity import DEFAULT_IDLE_THRESHOLD, UserContext

logger = logging.getLogger(__name__)

DEFAULT_DISPLAY_ID = 0
"""Android 的 `Display.DEFAULT_DISPLAY`。

影子平面的**否定判据**要用到它：`display_id == 0` 意味着「就是用户在看的那块屏幕」，
那就不构成影子平面。写在这里而不是从 Kotlin 侧取，是因为这个数字是 Android 平台
常量（`Display.DEFAULT_DISPLAY` 自 API 1 起就是 0），不是我们会变的东西。
"""

# 当前上下文的采集总预算截止时刻（monotonic 秒）。语义与 `device.adb` 里那个一致：
# 一次采集内多次桥调用共享同一个截止时刻，耗尽后**不再开始新的调用**。
_DEADLINE: ContextVar[float | None] = ContextVar("android_deadline", default=None)

# 采集类调用的单次超时（秒）。桥如果不支持超时，这个值只用于错误信息与自检。
DEFAULT_READ_TIMEOUT_SECONDS = 6.0


class AndroidBridgeError(DeviceError):
    """Android 桥侧操作失败。

    继承 `DeviceError`（而不是像 ADB 那样子类化 `RuntimeError`）是为了让
    `executor` 的「失败收敛」只写一条 `except DeviceError`——**换后端不改核心**，
    包括错误处理这条路径。
    """


class AndroidServiceUnavailable(AndroidBridgeError):
    """辅助功能服务没连上、或投屏/无障碍权限没给——设备暂时不可用。

    单列一个类型是因为它的处置与普通过程失败不同：这不是「这次没成功、重试一下」，
    而是「用户还没授权」，必须在界面上说清楚要开什么权限。
    """


class AndroidBudgetExhausted(AndroidBridgeError, DeviceBudgetExhausted):
    """采集总预算耗尽——**不是**设备坏了，是我们主动不再往下等。

    与 `AdbBudgetExhausted` 同义（那边继承 `AdbError`，这边继承 `AndroidBridgeError`），
    共同基类 `DeviceBudgetExhausted` 让上层可以不分后端地接住它。
    """


def _user_context_from_bridge(
    payload: dict, *, idle_threshold: float
) -> UserContext:
    """把桥返回的 dict 翻译成 `UserContext`（V5 §十）。

    单独抽成模块级函数（而不是塞进 `AndroidDeviceController.user_context`）是为了
    **可离线测试**：桥的返回形状是本后端最容易出错的地方（Kotlin 侧字段名写错、
    类型不是 JSON 原生类型、`confirmed` 漏传），而这些错误在真机上只会表现为
    「Agent 莫名其妙不暂停」。抽出来之后可以在 PC 上直接喂各种畸形 dict 验证。

    三条降级规则，都指向同一个方向——**宁可说「不知道」，不说「用户不在」**：

    1. `confirmed` 缺省为 **False**（而不是 True）。Kotlin 侧忘了传这个键时，
       我们不会假装这次读数有依据。
    2. `active` 缺省为 **True**（保守）。没有明确说「用户不在」时，当作用户在场。
    3. 类型不对（例如 `idle_seconds` 传了字符串）时按「不知道」丢弃该字段，
       不抛异常——探测失败不该升级成任务失败。
    """
    confirmed = bool(payload.get("confirmed", False))

    idle_raw = payload.get("idle_seconds")
    idle: float | None = None
    if isinstance(idle_raw, (int, float)) and not isinstance(idle_raw, bool):
        idle = float(idle_raw)

    locked_raw = payload.get("screen_locked")
    locked: bool | None = locked_raw if isinstance(locked_raw, bool) else None

    package = payload.get("foreground_package")
    fg = package if isinstance(package, str) and package else None

    reason = payload.get("reason")
    reason_text = reason if isinstance(reason, str) else ""

    if not confirmed:
        # 未确认时 active 一律保守取 True，即使桥传了 False——
        # 「没依据的 active=False」是最危险的组合（它会被读成「用户不在，可以点」）。
        if not reason_text:
            reason_text = "桥未确认这次读数"
        return UserContext(
            active=True,
            confirmed=False,
            foreground_package=fg,
            screen_locked=locked,
            idle_seconds=idle,
            reason=reason_text,
        )

    active_raw = payload.get("active")
    active = active_raw if isinstance(active_raw, bool) else True
    return UserContext(
        active=active,
        confirmed=True,
        foreground_package=fg,
        last_user_input_at=None,
        screen_locked=locked,
        idle_seconds=idle,
        reason=reason_text,
    )


def _shadow_session_from_bridge(
    bridge: object, session_id: str, *, adapter: str = "bridge"
) -> dict:
    """探测影子平面可用性，把结果规整成 `{available, reason, display_id}`（V5 §五）。

    单独抽成模块级函数，理由与 `_user_context_from_bridge` 完全相同：
    跨语言边界的形状错误在真机上只表现为「影子任务莫名失败」，抽出来才好在 PC 上
    直接喂畸形输入验证。

    ━━━ 与用户活动那个函数的方向**相反**，这是刻意的 ━━━

    `_user_context_from_bridge` 的降级方向是「保守地说用户在操作」（宁可慢）；
    这里的方向是「保守地说影子平面不可用」（宁可降级/拒绝）。两者都是「不确定时
    选安全的那一侧」，只是**安全侧在哪边不同**：

    - 用户活动判错 → Agent 在用户看屏幕时发手势，**碰了用户的手机**；
    - 影子平面判错 → 动作被发到一块我们以为独立的屏幕上，实际落在 Display 0，
      **同样碰了用户的手机**。

    两个错都指向「打扰/影响用户」，所以两者的保守方向一致：不确定就别动。

    四条降级规则：

    1. 桥没有这个方法 / 调用抛异常 → `available=False`，原因如实写明；
    2. 返回不是 dict → `available=False`（不是「当作可用」）；
    3. `available` 不是 bool → 取 **False**（不是取真值）；
    4. `available=True` 但 `display_id` 缺失或等于默认显示 → **仍判不可用**。
       第 4 条最容易被认为是多余的，却最要紧：影子平面的**定义**是「一块独立的
       显示」。声称可用却报默认显示，等于承认动作会落到用户那块屏幕上——
       这正是本能力要避免的事。宁可在这里把它拦下来。
    """
    probe = getattr(bridge, "shadow_session", None)
    if not callable(probe):
        return {
            "available": False,
            "reason": f"桥 {adapter} 没有 shadow_session 方法（APK 版本较旧）",
            "display_id": None,
        }

    try:
        payload = probe(str(session_id))
    except Exception as exc:  # noqa: BLE001 —— 探测失败不升级为任务失败
        logger.warning("探测影子平面失败：%s", exc)
        return {
            "available": False,
            "reason": f"桥调用失败：{exc}",
            "display_id": None,
        }

    if not isinstance(payload, dict):
        return {
            "available": False,
            "reason": f"桥返回了意外的类型 {type(payload).__name__}",
            "display_id": None,
        }

    # 严格取 bool：字符串 "true" / 数字 1 都不算「可用」。
    # 这里不学 Python 的 truthiness——`"false"` 在真值判断里是 True，
    # 而它会让我们把一块不存在的屏幕当成存在的。
    available_raw = payload.get("available")
    available = available_raw if isinstance(available_raw, bool) else False

    display_raw = payload.get("display_id")
    display_id = (
        int(display_raw)
        if isinstance(display_raw, int) and not isinstance(display_raw, bool)
        else None
    )

    reason_raw = payload.get("reason")
    reason = reason_raw if isinstance(reason_raw, str) else ""

    if available and (display_id is None or display_id == DEFAULT_DISPLAY_ID):
        # 声称可用但没给出**独立的** display id（`Display.DEFAULT_DISPLAY` 是 0）
        # → 按不可用处置，理由说清楚。
        #
        # `== DEFAULT_DISPLAY_ID` 那一半才是这条规则真正的用武之地：
        # 桥最容易犯的错不是「忘了传 display_id」，而是「传了 0」——因为
        # `ShadowDisplayManager` 在不可用时返回的正是 `Display.DEFAULT_DISPLAY`。
        # 只判 None 的话，「available=true + display_id=0」这个组合会顺利通过，
        # 而它字面意思就是「影子平面可用，用的还是用户那块屏幕」。
        return {
            "available": False,
            "reason": (
                "桥声称影子平面可用，但未给出独立的 display_id"
                "（动作会落到默认显示上，正是本能力要避免的）"
            ),
            "display_id": None,
        }

    return {"available": available, "reason": reason, "display_id": display_id}


@runtime_checkable
class AndroidBridge(Protocol):
    """Android 侧（Kotlin/Java）必须实现的能力。

    实现方式随宿主而定：Chaquopy 里直接暴露一个 Kotlin 对象、JNI 包装、
    或一个本地 IPC（AIDL / 本地 socket）都行——Python 侧只认这个结构。
    """

    def screen_size(self) -> tuple[int, int]: ...
    def current_focus(self) -> tuple[str, str]: ...
    def dump_ui(self) -> str: ...
    def screenshot_bytes(self) -> bytes: ...

    def tap(self, x: int, y: int) -> None: ...
    def long_press(self, x: int, y: int, duration_ms: int) -> None: ...
    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None: ...
    def set_text(self, value: str) -> None: ...
    def press_back(self) -> None: ...
    def press_home(self) -> None: ...
    def launch(self, package: str, activity: str | None) -> None: ...

    def state(self) -> str: ...

    # ---- 用户活动（V5 §十）----
    #
    # Kotlin 侧拿到的是**精确时间**（AccessibilityService 的 TYPE_VIEW_TOUCHED /
    # 输入事件流 + PowerManager 的锁屏状态），比 ADB 侧的「前后台窗口变化近似」
    # 强很多，所以两边返回同一个结构、但 `confirmed` 的可信度不同。
    #
    # `user_activity()` 返回一个 dict（跨语言边界最容易对齐的形状），键：
    #   {
    #     "confirmed": bool,        # 这次读数有没有依据
    #     "active": bool,           # confirmed=False 时是保守值 True
    #     "idle_seconds": float|None,
    #     "foreground_package": str|None,
    #     "screen_locked": bool|None,
    #     "reason": str,
    #   }
    # 用 dict 而不是让 Kotlin 构造 Python 对象：Chaquopy 下传 dict 是零成本且类型安全的，
    # 而传自定义类需要两侧同时改。返回 None 表示「本桥版本还不支持」——
    # 与 `confirmed=False` 是两回事，前者是「这个能力不存在」。
    def user_activity(self) -> dict | None: ...

    # ---- 影子执行平面（V5 §五 / §六）----
    #
    # `shadow_session()` 探测「影子平面现在能不能用」，键：
    #   {
    #     "available": bool,       # 能不能在影子平面执行动作
    #     "reason": str,            # 不可用时的结构化原因码
    #     "display_id": int|None,   # 影子平面的 display id；不可用时是默认显示
    #   }
    #
    # ━━━ 它当前**一定**返回 available=False ━━━
    #
    # 这不是「还没接完线」，而是 §六 的结论：`MediaProjection` 只能捕获显示、
    # `AccessibilityService` 无法在后台启动独立 App 实例，所以现有 API 造不出
    # 一块可独立操作的屏幕。桥如实说不支持，调用方据此 fail-closed
    # （`shadow` 任务）或降级到前台（`hybrid` 任务）。
    #
    # ━━━ 为什么不用「返回 None」表示不支持 ━━━
    #
    # `None` 在 `user_activity` 那里表示「这个桥版本没有这个能力」，用来驱动
    # `supports_*()` 判据。而影子探测的语义是「我探测过了，结论是不可用」——
    # 那是**正常的探测结果**，不是能力缺失。混用会让「老 APK」与
    # 「新 APK 探测后说不可用」这两件事无法区分，而它们的处置并不相同。
    #
    # 与 `user_activity` 的另一处一致：这两个方法都**不抛异常**，
    # 用返回值的字段表达结论（端口约定：`current_focus` 同源）。
    def shadow_session(self, session_id: str) -> dict | None: ...

    def shadow_release(self, session_id: str) -> None: ...


_BRIDGE_METHODS: tuple[str, ...] = (
    "screen_size",
    "current_focus",
    "dump_ui",
    "screenshot_bytes",
    "tap",
    "long_press",
    "swipe",
    "set_text",
    "press_back",
    "press_home",
    "launch",
    "state",
    "user_activity",
    # V5 §五：影子平面探测/释放。加进必需方法表是刻意的——影子任务的能力
    # 判断依赖它，若允许「桥没有这个方法」，那么它就是靠 `getattr` 兜底，
    # 于是**每个**调用点都得自己写一遍「拿不到怎么办」，而那些写法必然会分叉。
    "shadow_session",
    "shadow_release",
)


class IncompleteAndroidBridge(TypeError):
    """Android 桥少了方法。装配期就报出来，不要等任务跑到中途才 AttributeError。"""


def assert_bridge_complete(bridge: object) -> None:
    missing = [name for name in _BRIDGE_METHODS if not callable(getattr(bridge, name, None))]
    if missing:
        raise IncompleteAndroidBridge(
            f"AndroidBridge {type(bridge).__name__} 缺少："
            + "、".join(missing)
            + "（契约见 device/android.py 顶部表格）"
        )


# ---- 桥的注册（Android 应用启动时调用一次）----

_bridge_factory: Callable[[], AndroidBridge] | None = None


def register_android_bridge(factory: Callable[[], AndroidBridge]) -> None:
    """注册「怎么拿到桥」。

    典型用法（Chaquopy / 自带 Python 的 Android 应用，`Application.onCreate` 里）：

        Python.start(AndroidPlatform(this))
        py = Python.getInstance()
        py.getModule("device.android").callAttr("register_android_bridge", this.bridgeFactory)

    传工厂而不是桥实例：桥内部通常要等 `AccessibilityService` 连上才可用，
    而注册发生在服务之前。**延迟到真正构造控制器时再取**，能避免拿着一个半成品桥。
    """
    global _bridge_factory
    _bridge_factory = factory


def bridge_factory() -> Callable[[], AndroidBridge] | None:
    return _bridge_factory


def register_android_bridge_object(bridge: AndroidBridge) -> None:
    """注册一个**已经建好的**桥实例（V3.3 §1；Chaquopy 路线用）。

    与 `register_android_bridge(工厂)` 的区别只在「什么时候取桥」：

    - 工厂版本适合「桥要等辅助功能服务连上才能用」的场景；
    - 这个版本适合**桥本身是无状态的转发层**的场景——`android/` 里的
      `AndroidBridgeImpl` 就是这样：它自己什么都不存，全部委托给
      `ShadowAccessibilityService` / `ScreenCapture` / `AppLauncher` 三个单例。
      既然如此，就没有「拿到半成品桥」的风险，多一层工厂只是多一层间接。

    为什么需要这个函数：Chaquopy 把 Kotlin 对象转成 Python 对象后，那个对象
    **不是一个可调用的函数**——直接把它当工厂传给 `register_android_bridge`
    会在第一次取桥时抛 `TypeError: 'AndroidBridgeImpl' object is not callable`。
    而要在 Kotlin 侧造一个 Python 能调用的工厂又得额外包一层。
    在这里包最省事，也最不容易出错。
    """
    assert_bridge_complete(bridge)
    register_android_bridge(lambda: bridge)


def clear_android_bridge() -> None:
    """注销（测试用；生产上不该需要）。"""
    global _bridge_factory
    _bridge_factory = None


def require_android_bridge() -> AndroidBridge:
    """取桥实例；没注册就**响亮地失败**。

    这里刻意不「退化成空实现」：选定了 android 后端却没有桥，说明装配错了。
    给一个空桥的话，任务会跑到第一次观察才发现设备没反应——那是最难排查的一类失败
    （V3.2 §六「配置错误必须 fail-closed」同一原则）。
    """
    if _bridge_factory is None:
        raise AndroidServiceUnavailable(
            "已选择 android 设备后端，但没有注册 AndroidBridge。"
            "请在 Android 应用启动时调用 device.android.register_android_bridge(工厂函数)；"
            "若只是想用 PC 侧的 ADB，请把 SHADOW_DEVICE_BACKEND 设为 adb（或不设置）。"
        )
    bridge = _bridge_factory()
    assert_bridge_complete(bridge)
    return bridge


class AndroidDeviceController:
    """把 `AndroidBridge` 适配成 Shadow 的 `DeviceController` 端口（V3.3 §1）。

    这一层**只做协议翻译**，不含任何 Android API 调用——所有系统交互都在桥里。
    这样「Python 核心 + Android 桥」的边界是清晰的：本文件可以在 PC 上被完整单测。
    """

    backend_name = "android"

    def __init__(
        self,
        bridge: AndroidBridge,
        *,
        serial: str = "android-local",
        read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    ) -> None:
        assert_bridge_complete(bridge)
        self._bridge = bridge
        self._serial = serial
        self._read_timeout = read_timeout

    @property
    def serial(self) -> str:
        return self._serial

    @property
    def bridge(self) -> AndroidBridge:
        """桥本身（诊断/测试用）。核心不该碰它。"""
        return self._bridge

    # ---- 预算 ----

    def remaining_budget(self) -> float | None:
        deadline = _DEADLINE.get()
        if deadline is None:
            return None
        return deadline - time.monotonic()

    @contextmanager
    def deadline_budget(self, seconds: float) -> Iterator[None]:
        """给这一段内的桥调用加总预算（语义与 ADB 侧一致）。

        与 ADB 的差别：ADB 能给每个子进程加超时（`min(自己的超时, 剩余预算)`），
        而桥调用是**同进程的一次方法调用**，我们没法从外面掐断它。
        所以这里做的是**准入控制**——预算耗尽后不再开始新的采集调用，
        而已经在飞的那一次只能等它自己返回。严格上界因此是「预算 + 单次调用耗时」，
        与 ADB 侧「预算 + 单条命令超时」是同一个形状。

        要更强的保证，桥应当支持取消（方案文档 §8 的「模型服务挂掉时要能停下来」）：
        桥实现里把这次调用登记成可取消任务，Python 侧调用前查 `remaining_budget()`。
        """
        token = _DEADLINE.set(time.monotonic() + seconds)
        try:
            yield
        finally:
            _DEADLINE.reset(token)

    def _guarded(self, operation: str, call: Callable[[], object]):
        """预算准入 + 桥异常归一。

        归一这步是刻意的：桥是 Kotlin/Java 侧的对象，它可能抛出宿主语言的异常
        （Chaquopy 会包成 Python 异常，但类型五花八门）。核心只认 `DeviceError`，
        所以这里统一翻译，顺带把「哪一步采集失败」写进消息——否则上层只会看到
        一句来自 Java 的 NullPointerException。
        """
        remaining = self.remaining_budget()
        if remaining is not None and remaining <= 0:
            raise AndroidBudgetExhausted(f"采集总预算已耗尽，跳过 {operation}")
        try:
            return call()
        except (AndroidBridgeError, DeviceError):
            raise
        except Exception as exc:  # noqa: BLE001 - 桥是外部实现，什么异常都可能
            raise AndroidBridgeError(f"{operation} 失败（Android 桥抛出 {type(exc).__name__}）：{exc}") from exc

    # ---- Observe ----

    def screen_size(self) -> tuple[int, int]:
        size = self._guarded("读取屏幕尺寸", self._bridge.screen_size)
        if not size or len(size) != 2 or size[0] <= 0 or size[1] <= 0:
            raise AndroidBridgeError(f"屏幕尺寸异常: {size!r}（应为 (width, height) 正整数）")
        return int(size[0]), int(size[1])

    def current_focus(self) -> tuple[str, str]:
        """拿不到焦点时返回 `("", "")` 而不是抛异常。

        与端口约定一致：上层要区分「读不到」与「读到了但为空」，
        而这两者在桥侧都是「没有可用的窗口信息」，统一按「读不到」处理。
        """
        try:
            focus = self._guarded("读取当前焦点", self._bridge.current_focus)
        except AndroidBridgeError as exc:
            logger.warning("读取当前焦点失败，按「读不到」处理：%s", exc)
            return "", ""
        if not focus:
            return "", ""
        package, _, activity = (str(focus[0]), "", str(focus[1]) if len(focus) > 1 else "")
        return package, activity

    def dump_ui(self) -> str:
        """读 UI 树。**失败要抛**（见模块文档：空串会把「没读到」伪装成「页面为空」）。"""
        xml = self._guarded("采集 UI 树", self._bridge.dump_ui)
        if not xml or "<hierarchy" not in str(xml):
            raise AndroidBridgeError(
                "Android 桥返回的 UI 树不含 <hierarchy> 根节点——它必须与 uiautomator "
                "dump 的输出格式兼容（见 device/android.py 顶部说明）"
            )
        return str(xml)

    def screenshot_bytes(self) -> bytes:
        data = self._guarded("截图", self._bridge.screenshot_bytes)
        if not data:
            raise AndroidBridgeError("Android 桥返回了空截图（MediaProjection 可能还没就绪）")
        return bytes(data)

    def screenshot(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.screenshot_bytes())
        return target

    def state(self) -> str:
        """设备状态。`"device"` 表示就绪（端口约定）。

        桥返回空值/异常时返回 `"unknown"` 而不是抛——它只用于 `/devices` 展示与排查，
        为此把整个端点弄成 500 不值得。
        """
        try:
            reported = self._guarded("查询设备状态", self._bridge.state)
        except AndroidBridgeError as exc:
            logger.warning("查询设备状态失败：%s", exc)
            return "unknown"
        return str(reported) or "unknown"

    # ---- 用户活动（V5 §十）----

    def supports_user_activity(self) -> bool:
        """本桥能不能探测用户活动。

        判据是**桥有没有这个方法**——老版本 APK 的桥不带 `user_activity`，
        那时必须如实说「做不到」，好让 `SHADOW`/`HYBRID` 任务被拒绝或降级
        （见 `device/factory.py`），而不是让它以为「用户在不在都无所谓」。
        """
        return callable(getattr(self._bridge, "user_activity", None))

    def shadow_plane_available(self) -> bool | None:
        """桥那边有没有可用的影子平面（V5 P0③）。

        判据同样是**桥有没有这个方法**：老版本 APK 的桥不带 `shadow_session`，
        那时返回 `None`（不知道）——因为「桥没实现」与「桥实现了但创建不出独立
        显示」是两件事，日志上要能分开。

        新桥会真的调一次 `ShadowDisplayManager`，当前实现恒报 `available=false`：
        `MediaProjection` 只能捕获、`AccessibilityService` 无法后台起独立 App
        实例（§六）。所以这里的 `False` 是**如实**的现状，不是保守取值。

        **零 I/O**：读的是桥的**本地**能力位/缓存，不发 HTTP。
        真去创建虚拟显示是很重的操作，不能放在抢占判定（设备锁内）的路径上。
        """
        probe = getattr(self._bridge, "shadow_session", None)
        if not callable(probe):
            return None
        cached = getattr(self._bridge, "shadow_available", None)
        if isinstance(cached, bool):
            return cached
        return False

    def user_context(
        self, *, idle_threshold: float = DEFAULT_IDLE_THRESHOLD
    ) -> UserContext:
        """从桥读用户活动（V5 §十）。

        **永不抛异常**（协议要求）：桥挂了、返回的 dict 缺字段、类型不对，
        一律降级成 `confirmed=False`（不知道）——因为「探测用户失败」
        不该升级成「任务失败」，正确处置是保守暂停。这与 `state()` 的处理同源。
        """
        if not self.supports_user_activity():
            return UserContext(
                active=True,
                confirmed=False,
                reason="当前 Android 桥不支持 user_activity（APK 版本较旧）",
            )
        try:
            payload = self._bridge.user_activity()
        except Exception as exc:  # noqa: BLE001 —— 见 docstring：探测失败不升级为任务失败
            logger.warning("读取用户活动失败：%s", exc)
            return UserContext(active=True, confirmed=False, reason=f"桥调用失败：{exc}")

        if payload is None:
            # 与「读失败」分开：None 表示这个桥**明确声明**不支持这项能力。
            return UserContext(
                active=True,
                confirmed=False,
                reason="桥声明不支持用户活动探测",
            )
        if not isinstance(payload, dict):
            return UserContext(
                active=True,
                confirmed=False,
                reason=f"桥返回了意外的类型 {type(payload).__name__}",
            )

        return _user_context_from_bridge(payload, idle_threshold=idle_threshold)

    # ---- 影子执行平面（V5 §五 / §六）----

    def shadow_session(self, session_id: str) -> dict:
        """探测影子平面可用性（V5 §五）。

        **永不抛异常**，也**永不返回 None**——与 `user_activity` 的处理不同，
        因为这两件事的语义不同（见 `AndroidBridge.shadow_session` 的说明）：
        探测失败在这里也要给出一个 dict，只是 `available=False` 且 `reason`
        说明为什么——调用方需要「不可用 + 原因」这一对信息才能决定
        fail-closed 还是降级，光知道「没拿到」是不够的。
        """
        return _shadow_session_from_bridge(
            self._bridge, session_id, adapter=type(self).__name__
        )

    def shadow_release(self, session_id: str) -> None:
        """释放影子会话。失败不抛——释放是清理动作，它挂掉不该让任务失败。"""
        release = getattr(self._bridge, "shadow_release", None)
        if not callable(release):
            return
        try:
            release(str(session_id))
        except Exception as exc:  # noqa: BLE001 —— 清理失败不该升级成任务失败
            logger.warning("释放影子会话 %s 失败：%s", session_id, exc)

    # ---- 影子平面动作（审查 P1⑤）----
    #
    # 这一组转发给桥的 `shadow_*` 方法，**并且绝不回落到不带前缀的同名方法**。
    #
    # 三条硬规矩，每条都对应一种具体的失败形状：
    #
    # 1. **桥缺 `shadow_*` → 抛 `ShadowActionUnsupported`，不回落 `self.tap()`。**
    #    眼下真机上桥还没有这些方法（§六：造不出独立显示），所以这里全部会抛。
    #    那是如实的：影子动作失败会被记账、会出现在 `/executions` 里；
    #    而回落成 `self._bridge.tap(...)` 是**静默地**点在用户正看的屏幕上，
    #    日志上还看不出它来自影子任务——本能力唯一要防的事。
    #
    # 2. **桥的 `shadow_*` 抛异常 → 原样透出，不吞成「成功」。**
    #    这条与 `shadow_release` 的「失败不抛」**刻意相反**，因为两者性质不同：
    #    释放是清理（失败了任务照样算成功），动作是效果（失败了这一步就没做成）。
    #    把动作异常吞掉，会让 verifier 拿到一个「已执行」的假象。
    #
    # 3. **不给 display_id 参数。** 显示绑定在**会话**上（`shadow_session()` 时确定），
    #    不是每个动作各带一个。让动作自己传 display id，就会出现
    #    「会话绑在 display 7、这个动作传了 display 0」这种自相矛盾的组合，
    #    而那正好等于把动作打到用户屏幕上。

    def shadow_screenshot_bytes(self) -> bytes:
        result = self._shadow_action("screenshot_bytes", "shadow_screenshot_bytes")
        if not isinstance(result, (bytes, bytearray)):
            raise ShadowActionUnsupported(
                f"影子平面截图返回了意外类型 {type(result).__name__}（期望 bytes）"
            )
        return bytes(result)

    def shadow_dump_ui(self) -> str:
        """影子平面的 UI 树。

        **读不到要抛**（[80]/[92]）：这里把「空结果」判成错误而不是返回空串，
        因为空串在观察层会被解读成「这块屏幕是空的」——而真相往往是
        「桥不认这个 display」。两者的处置完全不同（前者是等，后者是修）。
        """
        result = self._shadow_action("dump_ui", "shadow_dump_ui")
        if not isinstance(result, str) or not result.strip():
            raise ShadowActionUnsupported(
                "影子平面 UI 树为空：读不到 ≠ 屏幕是空的，请检查桥是否支持 display 参数"
            )
        return result

    def shadow_tap(self, x: int, y: int, *, duration_ms: int = 0) -> None:
        self._shadow_action("tap", "shadow_tap", int(x), int(y), int(duration_ms))

    def shadow_long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self._shadow_action("long_press", "shadow_long_press", int(x), int(y), int(duration_ms))

    def shadow_swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._shadow_action(
            "swipe", "shadow_swipe", int(x1), int(y1), int(x2), int(y2), int(duration_ms)
        )

    def shadow_set_text(self, value: str) -> None:
        self._shadow_action("set_text", "shadow_set_text", str(value))

    def shadow_back(self) -> None:
        self._shadow_action("back", "shadow_back")

    def shadow_home(self) -> None:
        self._shadow_action("home", "shadow_home")

    def shadow_launch(self, package: str, activity: str | None = None) -> None:
        """在影子平面启动应用。P2 的核心难点（§十一），真机上当前做不到。"""
        self._shadow_action("launch", "shadow_launch", str(package), activity)

    def _shadow_action(self, action: str, bridge_method: str, *args) -> Any:
        """调用桥上的影子动作；任何「调不了」都抛，不回落、不吞异常。"""
        method = getattr(self._bridge, bridge_method, None)
        if not callable(method):
            raise ShadowActionUnsupported(
                f"影子动作 {action} 不可用：当前 Android 桥没有实现 {bridge_method}()。"
                "本端尚未实现「往独立显示发动作」（见 device/session.py 的 ShadowSession 说明），"
                "绝不回落到 Display 0"
            )
        return method(*args)

    # ---- Act ----

    def tap(self, x: int, y: int) -> None:
        self._guarded("tap", lambda: self._bridge.tap(int(x), int(y)))

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self._guarded(
            "long_press", lambda: self._bridge.long_press(int(x), int(y), int(duration_ms))
        )

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._guarded(
            "swipe",
            lambda: self._bridge.swipe(int(x1), int(y1), int(x2), int(y2), int(duration_ms)),
        )

    def back(self) -> None:
        self._guarded("back", self._bridge.press_back)

    def home(self) -> None:
        self._guarded("home", self._bridge.press_home)

    def launch(self, package: str, activity: str | None = None) -> None:
        """启动应用：桥走 `PackageManager` + `startActivity`（方案文档 §6）。

        语义与 ADB 的 `am start` / `monkey` 对齐：只给包名时启动主界面。
        """
        self._guarded("launch", lambda: self._bridge.launch(str(package), activity or None))

    def launch_app(self, package: str) -> None:
        self.launch(package, None)

    def wait(self, duration_ms: int = 1000) -> None:
        time.sleep(max(0, int(duration_ms)) / 1000.0)

    # ---- 输入通道 ----

    def build_input_provider(self):
        """返回 Accessibility 输入通道（方案文档 §5）。

        延迟 import 避免 `device.input` ↔ `device.android` 在模块加载期互相牵扯。
        """
        from .input import AndroidInputProvider

        return AndroidInputProvider(self._bridge)


# 装配期自检：这个适配器必须实现完整端口。
# 放在模块末尾而不是构造里——它是**代码**的自检，每次导入跑一次就够，
# 不需要每个实例付一次反射成本。
assert_implements(AndroidDeviceController, backend="android")

__all__ = [
    "AndroidBridge",
    "AndroidBridgeError",
    "AndroidBudgetExhausted",
    "AndroidDeviceController",
    "AndroidServiceUnavailable",
    "IncompleteAndroidBridge",
    "assert_bridge_complete",
    "bridge_factory",
    "clear_android_bridge",
    "register_android_bridge",
    "register_android_bridge_object",
    "require_android_bridge",
    "is_read_only",
]
