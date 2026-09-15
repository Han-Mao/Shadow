"""DeviceController：Shadow 核心依赖的**设备端口**（V3.3 §1，依据 `手机部署方案.md`）。

这个文件回答一个问题：**核心到底依赖设备的哪些能力？**

在它存在之前，答案散落在 `observer` / `executor` / `vision.grounding` / `device.screenshot`
里——任何人想换一种设备后端（Android 本机 Accessibility、云手机、模拟器集群）都得先把
那几个模块读一遍，才知道自己要实现什么。现在它是显式的：**下面这一组方法就是全部**。

    Observe（只读）  screen_size / current_focus / dump_ui / screenshot / screenshot_bytes / state
    Act（改设备）    tap / long_press / swipe / back / home / launch / launch_app / wait
    基础设施         deadline_budget / build_input_provider

为什么签名写得这么死：**换后端不该改核心**。所以「手机上要改什么」这个问题的答案应当只有
两个——在 `device/android.py` 里实现这个协议，或者什么都不用改。

三处刻意保留的「非纯设备」能力，都写在协议里而不是散在外面：

- `deadline_budget`：核心用它给「一次采集」加**总耗时上界**（抢占延迟上界靠它）。
  后端自己决定怎么落实（ADB 是给子进程加超时；Android 是给服务调用加超时）。
- `build_input_provider`：输入通道是后端**自己**的事——ADB 侧是 `input text` +
  ADB Keyboard 广播，Android 侧是 Accessibility `ACTION_SET_TEXT`。
  问控制器要，而不是在 `executor` 里 if-else 判断后端类型：新增后端时不用改 executor。
  这就是方案文档 §5 说的 `InputProvider { AdbInputProvider, AndroidInputProvider }`。
- 只读声明（`is_read_only`）：`/screenshot`、`/observe` 这类「纯读」端点要能**结构化地**
  证明自己只读。声明放在这里，因为它是「Shadow 认为什么算只读」的约定，不是某个后端的细节。

已知实现：

    AdbDeviceController      device/adb.py      PC 侧：`adb -s <serial> ...`
    AndroidDeviceController  device/android.py  手机侧：Accessibility + MediaProjection

两者共用同一套 TaskManager / Scheduler / AgentRuntime / RiskGate / Checkpoint——
这里就是那个「共用」的接缝（方案文档 §1「TaskManager 到 Verifier 基本都不用重写」）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

# 只读操作（V2.7 P1-7）：这些方法只**读取**设备状态，不改变设备上的任何东西。
# API 的只读端点（/screenshot、/observe）只能调用这里面的方法；会改设备的
# tap / text / swipe / keyevent / am start 一律不在此列。
# 之前「只读」只是端点名约定，没有结构化声明——加了这个集合之后，
# 「只读端点是否真的只读」可以从代码里查证，而不是靠人记住约定。
#
# V3.3 起它从 `device/adb.py` 挪到端口层：这是**Shadow 的约定**，不是 ADB 的细节。
# `device.adb` 仍然 re-export，老导入路径继续有效。
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


class DeviceError(RuntimeError):
    """设备操作失败的**公共基类**（V3.3 §1）。

    存在的唯一理由是让核心能写一条 `except DeviceError` 就接住所有后端——
    `AdbDeviceController` 抛 `AdbError`，`AndroidDeviceController` 抛
    `AndroidBridgeError`，两者都是它的子类。

    没有它的话，「换后端不改核心」这句话在**错误处理**这条路径上就不成立了：
    `executor` 得逐个列出各后端的异常类型，加一个后端就改一次核心。

    注意：本类**刻意不声明 `error_class`**。设备错误该归 transient 还是 fatal
    取决于具体原因（设备掉线是 transient、「未授权」近似 fatal），
    而 `models/retry.classify_exception` 一旦读到 `error_class` 就**不再回退**到按类型/文本判断——
    给它填一个笼统的值会把 `models/retry` 里已有的文本规则（`_FATAL` 里的
    「设备丢失 / unauthorized」等）全部屏蔽掉。
    给设备错误配正确的 `error_class` 是一次会影响重试策略的独立改动（见 README 延期说明）。
    """


class DeviceBudgetExhausted(DeviceError):
    """采集总预算耗尽。

    单独一个类型（而不是只靠消息文本）是因为它的含义与真故障不同：
    **设备没坏，是我们主动不再往下等**。上层可以据此选择「换个时机重试」
    而不是「放弃这台设备」。
    """


def is_read_only(operation: str) -> bool:
    """这次设备操作会不会改变设备状态（V2.7 P1-7）。

    只读封装（screenshot / dump_ui / screen_size / current_focus / state）返回 True；
    `shell` / `read_shell` 是万能口、无法保证只读，保守返回 False；
    其余（tap / text / swipe / keyevent / launch 等）都是改设备的，返回 False。

    注意这是**按操作名**保守判定，不看后端。Android 后端里 `dump_ui` 同样是只读
    （读 AccessibilityNodeInfo 树），所以两边可以共用同一份声明——真要出现
    「同一个名字在 A 后端只读、在 B 后端会写」的情况，那说明名字起错了。
    """
    return operation in _READ_ONLY_SAFE


@runtime_checkable
class DeviceController(Protocol):
    """设备端口（V3.3 §1）。核心只认这个协议，不认具体后端。

    标注「前端不可见」：这个方法只有设备后端实现，Agent 侧不直接调——
    Agent 走 `Action` → `executor`，`executor` 才碰设备。
    """

    # ---- Observe：只读 ----

    def screen_size(self) -> tuple[int, int]:
        """返回 (width, height)，必须是**实际渲染尺寸**。

        ADB 取 `wm size` 的 Override size；Android 取 `WindowMetrics`。
        坐标归一化以它为基准（见 `vision.grounding`），取错了坐标会整体偏移。
        """
        ...

    def current_focus(self) -> tuple[str, str]:
        """返回 (package, activity)。拿不到时返回 ("", "")，**不要抛异常**。

        上层用它做「恢复点是否还成立」「决策所依据的那一屏是否还在」的判断，
        「读不到」和「读到了但为空」在那些判断里的处置不同，所以不能靠异常区分。
        """
        ...

    def dump_ui(self) -> str:
        """返回 UI 树，格式**必须与 `uiautomator dump` 兼容的 XML**。

        这是整个改造里最要紧的一条约束：`vision/parser.py`、`vision/target.py`、
        `vision/grounding.py`、`agent/evidence.py` 全都按 uiautomator 的 XML 约定解析
        （`class` / `text` / `content-desc` / `resource-id` / `bounds` / `clickable` …）。
        Android 后端把 AccessibilityNodeInfo 树序列化成**同样的结构**，
        这些模块就一行都不用改——这比「再写一套 Android 专用的 UI 解析」省掉一整层。

        读取失败（服务没连上、树取不到）**要抛异常**：调用方需要区分
        「页面确实没有可点击元素」和「我们没读到树」（V3.1 P1-4 的目标证据缺口）。
        """
        ...

    def screenshot_bytes(self) -> bytes:
        """返回 PNG 字节。ADB 走 `exec-out screencap`，Android 走 MediaProjection。"""
        ...

    def screenshot(self, path: str | Path) -> Path:
        """截图落盘并返回路径。实现应与 `screenshot_bytes` 保持同一来源。"""
        ...

    def state(self) -> str:
        """设备可用状态，给 `/devices` 展示与运维排查用。

        **`"device"` 表示「就绪、可以执行动作」**，其余字符串（`"offline"`、
        `"unauthorized"`、`"service_disabled"` …）都表示不可用。约定一个最小词表，
        因为调用方（`GET /devices`）只做展示、不做分支判断。
        """
        ...

    # ---- Act：会改变设备状态 ----

    def tap(self, x: int, y: int) -> None: ...
    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None: ...
    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None: ...
    def back(self) -> None: ...
    def home(self) -> None: ...

    def launch(self, package: str, activity: str | None = None) -> None:
        """启动应用。`activity` 缺省时按包名启动主界面。

        ADB：`am start -n pkg/act`，缺 activity 时退化 `monkey -p pkg 1`。
        Android：`PackageManager.getLaunchIntentForPackage` + `startActivity`。
        两个后端都必须支持「只给包名」——模型经常只给得出包名。
        """
        ...

    def launch_app(self, package: str) -> None:
        """按包名启动主界面（= `launch(package, None)`）。

        单独留一个名字是因为它是**最高频的调用形态**，而 `launch(pkg, None)` 读起来
        像「少传了参数」。协议里显式列出，实现方不容易漏。
        """
        ...

    def wait(self, duration_ms: int = 1000) -> None: ...

    # ---- 基础设施 ----

    def deadline_budget(self, seconds: float):
        """给这一段内的设备操作加一个**总**预算（上下文管理器）。

        核心（`agent.observer`）用它保证「一次采集的耗时有上界」，抢占延迟才有上界。
        ADB 实现是给每条子进程命令加 `min(自己的超时, 剩余预算)`；
        Android 实现应当把预算透传给桥（由桥决定能否中断采集）。
        """
        ...

    def build_input_provider(self):
        """返回本后端的输入通道（`device.input.InputProvider`）。

        ADB：`input text`（安全 ASCII）+ ADB Keyboard 广播（任意 Unicode）自动二选一。
        Android：Accessibility `ACTION_SET_TEXT`（方案文档 §5），必要时回退 IME。
        """
        ...


# 协议要求实现的方法。`assert_implements` 用它做装配期检查。
_REQUIRED_METHODS: tuple[str, ...] = (
    "screen_size",
    "current_focus",
    "dump_ui",
    "screenshot_bytes",
    "screenshot",
    "state",
    "tap",
    "long_press",
    "swipe",
    "back",
    "home",
    "launch",
    "launch_app",
    "wait",
    "deadline_budget",
    "build_input_provider",
)


class IncompleteDeviceController(TypeError):
    """后端没有实现完整的设备端口。"""


def assert_implements(controller: object, *, backend: str = "") -> None:
    """装配期检查后端是否实现完整端口（V3.3 §1）。

    为什么值得单独做一次检查：换后端最容易出的错是「少实现了一个方法」，
    而它在运行期的表现形式是**任务跑到某一阶段突然 AttributeError** ——
    那是最难定位的一类失败（要知道任务跑到第几步才用到 `dump_ui`）。
    所以在装配处一次性问清楚，把失败前移到启动阶段。

    `backend` 只用于错误信息，让「是哪个后端缺了哪个方法」一眼可见。
    """
    missing = [name for name in _REQUIRED_METHODS if not callable(getattr(controller, name, None))]
    if missing:
        who = backend or type(controller).__name__
        raise IncompleteDeviceController(
            f"设备后端 {who} 没有实现完整的 DeviceController 端口，缺少："
            + "、".join(missing)
            + "（契约见 device/controller.py；Android 侧见 device/android.py 的 AndroidBridge）"
        )
