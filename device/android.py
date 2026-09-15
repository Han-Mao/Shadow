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
from typing import Callable, Iterator, Protocol, runtime_checkable

from .controller import (
    DeviceBudgetExhausted,
    DeviceError,
    assert_implements,
    is_read_only,
)

logger = logging.getLogger(__name__)

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
