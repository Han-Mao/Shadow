"""输入提供者（V2 §十八）：让 Agent 只说「输入这段文字」，不关心底层用什么通道。

- ASCII → `input text`（快，但设备端会吞掉非 ASCII）
- 中文等非 ASCII → ADB Keyboard 广播（`com.android.adbkeyboard`）
"""
from __future__ import annotations

import base64
import logging
from typing import Protocol

from .adb import TYPE_SAFE_PATTERN, AdbController, AdbError

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


def build_default_input(adb: AdbController) -> AutoInputProvider:
    """默认组合：ASCII 走 adb，其余走 ADB Keyboard 广播。"""
    return AutoInputProvider(AdbInputProvider(adb), BroadcastInputProvider(adb))
