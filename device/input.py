"""输入提供者（V2 §十八 · V3.3 §5）：让 Agent 只说「输入这段文字」，不关心底层用什么通道。

三个实现，按后端选：

- `AdbInputProvider`      ASCII → `input text`（快，但设备端会吞掉非 ASCII）
- `BroadcastInputProvider` 中文等非 ASCII → ADB Keyboard 广播（`com.android.adbkeyboard`）
- `AndroidInputProvider`  手机本机 → Accessibility `ACTION_SET_TEXT`

前两个属于 ADB 后端，第三个属于 Android 后端。**由控制器自己声明用哪个**
（`DeviceController.build_input_provider`），而不是在这里 if-else 判断后端类型——
手机自己跑的时候没有 adb，写死 ADB 通道会直接失败。方案文档 §5 说的
`InputProvider { AdbInputProvider, AndroidInputProvider }` 就是下面这份列表。
"""
from __future__ import annotations

import base64
import logging
from typing import Protocol

from .adb import TYPE_SAFE_PATTERN, AdbController, AdbError
from .controller import DeviceError

logger = logging.getLogger(__name__)

# ADB Keyboard 的包名与广播协议
ADB_KEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
ADB_KEYBOARD_PACKAGE = "com.android.adbkeyboard"
BROADCAST_ACTION_TEXT = "ADB_INPUT_TEXT"
BROADCAST_ACTION_B64 = "ADB_INPUT_B64"


class InputProvider(Protocol):
    """输入通道。"""

    name: str

    def available(self) -> bool: ...

    def input(self, text: str) -> None: ...


def is_plain_ascii(text: str) -> bool:
    """是否能用 `input text` 安全送达。"""
    return bool(text) and all(ord(ch) < 128 for ch in text) and bool(TYPE_SAFE_PATTERN.match(text))


class AdbInputProvider:
    """`input text` 通道：仅安全 ASCII，空格转义为 %s。"""

    name = "adb"

    def __init__(self, adb: AdbController) -> None:
        self._adb = adb

    def available(self) -> bool:
        return True

    def input(self, text: str) -> None:
        from .adb import escape_type_text

        self._adb.shell("input", "text", escape_type_text(text))


class BroadcastInputProvider:
    """ADB Keyboard 广播通道：支持中文等任意 Unicode。

    用 base64 传输，绕开 `am broadcast --es` 对引号、空格、换行的转义地狱。
    """

    name = "broadcast"

    def __init__(self, adb: AdbController, *, auto_enable: bool = True) -> None:
        self._adb = adb
        self._auto_enable = auto_enable
        self._enabled = False

    def _installed_imes(self) -> list[str]:
        try:
            return [line.strip() for line in self._adb.shell("ime", "list", "-s").splitlines() if line.strip()]
        except AdbError:
            return []

    def available(self) -> bool:
        return any(ADB_KEYBOARD_PACKAGE in ime for ime in self._installed_imes())

    def ensure_enabled(self) -> None:
        """启用并切到 ADB Keyboard。幂等，只在第一次真正执行。"""
        if self._enabled:
            return
        if not self.available():
            raise AdbError(
                f"设备未安装 ADB Keyboard（{ADB_KEYBOARD_PACKAGE}），无法输入非 ASCII 文本；"
                "请先安装该输入法 APK"
            )
        self._adb.shell("ime", "enable", ADB_KEYBOARD_IME)
        self._adb.shell("ime", "set", ADB_KEYBOARD_IME)
        self._enabled = True

    def input(self, text: str) -> None:
        if self._auto_enable:
            self.ensure_enabled()
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        self._adb.shell("am", "broadcast", "-a", BROADCAST_ACTION_B64, "--es", "msg", payload)

    def restore_ime(self, ime_id: str) -> None:
        """显式把输入法切回去。不在 input() 里自动做——每步切一次会让屏幕闪。"""
        self._adb.shell("ime", "set", ime_id)
        self._enabled = False


class AutoInputProvider:
    """按内容自动选通道：能走 adb 就走 adb，否则回退广播。"""

    name = "auto"

    def __init__(self, ascii_provider: InputProvider, broadcast_provider: InputProvider) -> None:
        self._ascii = ascii_provider
        self._broadcast = broadcast_provider

    def available(self) -> bool:
        return True

    def input(self, text: str) -> None:
        if is_plain_ascii(text):
            try:
                self._ascii.input(text)
                return
            except AdbError as exc:
                logger.warning("ASCII 通道输入失败（%s），回退到广播通道", exc)
        self._broadcast.input(text)


class AndroidInputProvider:
    """Accessibility `ACTION_SET_TEXT` 通道（V3.3 §5）：手机本机运行时的输入。

    **为什么手机侧不需要「ASCII / 非 ASCII 分流」**：`ACTION_SET_TEXT` 是把整段文本
    直接写进焦点节点，不经过输入法，所以中文和英文走同一条路。ADB 侧之所以要分流，
    是因为 `input text` 在设备端会被 IME 吞掉非 ASCII——那是 **ADB 通道的限制**，
    不该让它变成「所有后端都得遵守的规则」。

    这也顺手解决了一个老麻烦：ADB 侧输中文要先装并切到 ADB Keyboard
    （`BroadcastInputProvider.ensure_enabled`），在手机上跑根本不该有这一步。
    """

    name = "android_accessibility"

    def __init__(self, bridge) -> None:
        self._bridge = bridge

    def available(self) -> bool:
        """始终可用：桥连不上时会在 `input` 里抛 `AndroidBridgeError`。

        不像广播通道那样需要「先检查 ADB Keyboard 装没装」——辅助功能是系统组件。
        """
        return True

    def input(self, text: str) -> None:
        if not text:
            # 空输入是上游传参问题，不是设备问题；但要**报出来**，别静默成功——
            # 静默成功会让「我以为输入了」这种误判一路传到完成判定里。
            raise DeviceError("输入文本为空")
        self._bridge.set_text(text)


def build_default_input(controller) -> InputProvider:
    """按**控制器自己声明的**输入通道构造 provider（V3.3 §5）。

    以前这个函数写死了「ADB 双通道」——于是 Android 后端在手机上运行时仍会去调
    `adb shell input text`，而手机上根本没有 adb。现在改成问控制器要：
    新增后端（云手机、iOS、Android App 内嵌）不需要改这里，也不需要改 `executor`。

    没有声明该方法的对象（历史替身、简单假设备）退回 ADB 双通道，
    行为与升级前**完全一致**——这是刻意留的兼容口。
    """
    declared = getattr(controller, "build_input_provider", None)
    if callable(declared):
        return declared()
    return AutoInputProvider(AdbInputProvider(controller), BroadcastInputProvider(controller))
