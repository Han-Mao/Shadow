"""DevicePool（V2.1 §十三）：多设备注册表。

在这之前整个系统只有一台设备：`DeviceSession` 是全局单例，Scheduler 也只认一个
`_running`。要支持多设备，第一件事是把「设备」从「全局唯一」变成**可枚举的一等公民**。

职责刻意很窄：**serial → DeviceSession 的注册与查回**。
真正的调度决策（谁先用、怎么均衡）不在这一层——那是 Scheduler 的事。
放这里只会让两边都变糊涂。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator

if TYPE_CHECKING:  # 只用于类型标注，避免 device 包内部的循环导入
    from .session import DeviceSession

logger = logging.getLogger(__name__)


class UnknownDeviceError(KeyError):
    """请求了一台没注册过的设备。"""

    def __init__(self, serial: str, known: list[str]) -> None:
        known_text = ", ".join(known) if known else "（空）"
        super().__init__(f"未注册的设备 {serial!r}；已注册：{known_text}")


class DevicePool:
    """一批设备的注册表。

    线程模型：注册发生在启动装配阶段，之后基本只读；
    但 `snapshot()` 会被 API 线程调用，所以内部仍加锁。

    V2.5 §八 起还可以订阅「设备可用」事件：设备掉线不再静默改派之后，等待原设备
    回来的任务必须有**自动**恢复的触发点，否则会永久卡在 `DEVICE_UNAVAILABLE`。
    """

    def __init__(self, sessions: "Iterator[DeviceSession] | list[DeviceSession] | None" = None) -> None:
        self._sessions: dict[str, "DeviceSession"] = {}
        self._listeners: list[Callable[[str], None]] = []
        self._lock = threading.RLock()
        for session in sessions or []:
            self.register(session)

    def subscribe(self, listener: Callable[[str], None]) -> None:
        """订阅「某台设备变为可用」。`listener(serial)` 在注册该设备时被调用。"""
        with self._lock:
            self._listeners.append(listener)

    def register(self, session: "DeviceSession") -> "DeviceSession":
        serial = session.serial or f"device-{len(self._sessions) + 1}"
        with self._lock:
            self._sessions[serial] = session
        self._notify_available(serial)
        return session

    def _notify_available(self, serial: str) -> None:
        """通知监听器「这台设备可用了」。监听器出错不能影响设备注册本身。"""
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(serial)
            except Exception:  # noqa: BLE001 - 设备注册不能被调度器故障拖垮
                logger.warning("设备可用监听器处理 %s 时失败", serial, exc_info=True)

    def get(self, serial: str | None) -> "DeviceSession | None":
        if serial is None:
            return None
        return self._sessions.get(serial)

    def require(self, serial: str) -> "DeviceSession":
        session = self._sessions.get(serial)
        if session is None:
            raise UnknownDeviceError(serial, self.serials)
        return session

    def first(self) -> "DeviceSession | None":
        """任取一台（单设备场景的便捷入口）。没有设备时返回 None。"""
        return next(iter(self._sessions.values()), None)

    def idle(self) -> list["DeviceSession"]:
        """当前没被占用的设备。"""
        return [session for session in self._sessions.values() if not session.busy]

    @property
    def serials(self) -> list[str]:
        return sorted(self._sessions)

    def snapshot(self) -> dict:
        return {
            "count": len(self._sessions),
            "devices": {serial: session.snapshot() for serial, session in sorted(self._sessions.items())},
        }

    def __len__(self) -> int:
        return len(self._sessions)

    def __contains__(self, serial: object) -> bool:
        return serial in self._sessions

    def __iter__(self) -> Iterator["DeviceSession"]:
        return iter(self._sessions.values())


def build_pool(controller_factory, serials: list[str] | None = None):
    """按 serial 列表建一批会话。

    `controller_factory(serial)` 负责造出该设备的控制器（由
    `device.factory.build_controller` 按 `SHADOW_DEVICE_BACKEND` 决定是
    `AdbDeviceController` 还是 `AndroidDeviceController`，测试里是 `FakeDevice` 的封装）。
    没有给 serial 列表时返回空池——由调用方自己 register，保持「装配方式」开放。
    """
    from .session import DeviceSession

    pool = DevicePool()
    for serial in serials or []:
        pool.register(DeviceSession(controller_factory(serial), serial=serial))
    return pool


def storage_hint(root: str | Path, serial: str) -> Path:
    """给多设备留出「按设备分目录」的约定（截图 / 产物）。

    单设备时调用方一般仍然用统一目录；多设备混在一起会让产物无法归属。
    """
    safe = (serial or "default").replace("/", "_").replace("\\", "_")
    return Path(root) / safe
