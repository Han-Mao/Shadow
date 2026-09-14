"""Shadow 核心异常（V2.3 Reliability）。

把「状态机拒绝」「关键持久化失败」「绑定设备不可用」这类会改变任务命运的异常
集中定义，避免各个模块各自抛裸 RuntimeError。
"""
from __future__ import annotations


class ShadowError(RuntimeError):
    """Shadow 内部错误基类。"""


class InvalidTransitionError(ShadowError):
    """状态机拒绝的迁移，尤其是终态试图复活。"""

    def __init__(self, task_id: str, from_status: str, to_status: str, source: str = "") -> None:
        self.task_id = task_id
        self.from_status = from_status
        self.to_status = to_status
        self.source = source or "未标注"
        super().__init__(
            f"任务 {task_id} 不允许从 {from_status} 迁移到 {to_status}（来源 {self.source}）"
        )


class PersistenceError(ShadowError):
    """关键持久化失败：内存状态不能再继续领先于 durable state。"""

    def __init__(self, task_id: str, reason: str) -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"任务 {task_id} 持久化失败：{reason}")


class DeviceUnavailableError(ShadowError):
    """任务绑定的设备当前不在池中。"""

    def __init__(self, task_id: str, serial: str) -> None:
        self.task_id = task_id
        self.serial = serial
        super().__init__(f"任务 {task_id} 绑定的设备 {serial} 不可用")


class CorruptDataError(PersistenceError):
    """从持久化层读出的数据损坏或无法解析。"""

    def __init__(self, key: str, path: str, reason: str) -> None:
        self.key = key
        self.path = path
        self.reason = reason
        super().__init__(key, f"数据损坏 {path}：{reason}")
