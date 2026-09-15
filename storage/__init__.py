"""存储层：TaskStore / CheckpointStore / TrajectoryStore / EventLog / ExecutionStore。

V4 §二 起**任务、恢复点、事件在同一个 SQLite 库**（`<存储目录>/shadow.db`），由
`Database` 持有连接与事务。其余（轨迹 / 审计 / 执行记录）仍是 JSON 文件实现——
它们是「一个 store 一份事实」的典型，没有跨 store 事务的需求。
接口刻意做成窄方法集，后续要换 PostgreSQL / Redis 只需替换实现类。
"""
from .checkpoint_store import CheckpointStore, RestoreVerdict
from .confirmation_store import ConfirmationConsumptionStore
from .database import Database
from .event_log import Event, EventLog
from .execution_store import ExecutionStore
from .json_store import JsonStore
from .lease_store import LeaseStore
from .task_store import TaskStore
from .trajectory_store import TrajectoryStore

__all__ = [
    "CheckpointStore",
    "ConfirmationConsumptionStore",
    "Database",
    "Event",
    "EventLog",
    "ExecutionStore",
    "JsonStore",
    "LeaseStore",
    "RestoreVerdict",
    "TaskStore",
    "TrajectoryStore",
]
