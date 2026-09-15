"""存储层：TaskStore / CheckpointStore / TrajectoryStore / EventLog / ExecutionStore。

V4 §二 起**任务、恢复点、事件在同一个 SQLite 库**（`<存储目录>/shadow.db`），由
`Database` 持有连接与事务；v4.1 §二 把**执行记录**也搬了进来（它此前是最后一个
「一执行一个 JSON 文件」的存储）。至此所有**需要查询、需要受保护迁移、需要与别的
事实同事务**的东西都在表里。

仍为文件的只有轨迹（会被裁剪、按任务追加）、审计（HTTP 层旁路）——它们是
「一个 store 一份事实」的典型，没有跨 store 事务的需求。原先为它们准备的通用
`JsonStore` 已随最后一个使用者（执行记录）一起移除：留着一个只有测试认识的
通用原语，只会让下一轮读代码的人以为还有人在用它。
接口刻意做成窄方法集，后续要换 PostgreSQL / Redis 只需替换实现类。
"""
from .checkpoint_store import CheckpointStore, RestoreVerdict
from .confirmation_store import ConfirmationConsumptionStore
from .database import Database
from .event_log import Event, EventLog
from .execution_store import ExecutionStore
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
    "LeaseStore",
    "RestoreVerdict",
    "TaskStore",
    "TrajectoryStore",
]
