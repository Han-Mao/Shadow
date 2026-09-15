"""存储层：TaskStore / CheckpointStore / TrajectoryStore。

第一版用 JSON 文件实现——零依赖、人类可读、方便演示时直接翻看状态。
接口刻意做成窄方法集，后续要换 SQLite / PostgreSQL / Redis 只需替换实现类。
"""
from .checkpoint_store import CheckpointStore, RestoreVerdict
from .event_log import Event, EventLog
from .json_store import JsonStore
from .lease_store import LeaseStore
from .task_store import TaskStore
from .trajectory_store import TrajectoryStore

__all__ = [
    "CheckpointStore",
    "Event",
    "EventLog",
    "JsonStore",
    "LeaseStore",
    "RestoreVerdict",
    "TaskStore",
    "TrajectoryStore",
]
