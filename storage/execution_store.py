"""执行记录的存储（V4 §一）。

**一个执行一个 JSON 文件**，而不是一份共享的 JSONL。理由不是偏好：

- 执行记录会被**并发创建**（两个手工请求同时进来，或 Agent 与手工路径交错）；
  共享文件就需要跨请求的「读-改-写」，那正是本仓库花了好几轮才收敛掉的坑；
- 一执行一文件天然隔离，而且写走 `JsonStore`（tmp + fsync + os.replace），
  半截记录不可能被读到；
- `finish()` 只改自己那条记录，不需要和任何别的写者协调。

与另外两个存储的分工（V4 §五 要求它们能串成一条链）：

    ExecutionStore  →  「**这次执行**是谁、哪台设备、什么动作、什么风险、结果如何」
    EventLog        →  「执行**过程**里发生了什么」（逐条事件）
    AuditLog        →  「HTTP 层谁调了什么接口」（含被拒的请求）

三者靠 `execution_id` 关联：`GET /executions/{id}` 会把执行记录与它的事件流拼在一起。
"""
from __future__ import annotations

import logging
import threading

from models.exceptions import PersistenceError
from models.execution import ActionExecution

from .json_store import JsonStore

logger = logging.getLogger(__name__)


class ExecutionStore:
    """执行记录的窄接口存储：`save` / `load` / `finish` / `recent`。"""

    def __init__(self, root) -> None:
        self._store = JsonStore(root)
        self._lock = threading.RLock()

    def save(self, execution: ActionExecution) -> None:
        """写入（或覆盖）一条执行记录。失败抛 `PersistenceError`。

        为什么**不吞异常**：这条记录是「手机被操作过」的唯一凭据。
        手工路径在动作发出前必须先把它落盘——写不进去就不该执行，
        否则会出现「真的点了付款，但查不到是谁点的」。
        """
        try:
            with self._lock:
                self._store.write(execution.execution_id, execution.to_dict())
        except Exception as exc:  # noqa: BLE001 - 统一翻译成持久化失败
            raise PersistenceError(execution.execution_id, f"执行记录写盘失败：{exc}") from exc

    def load(self, execution_id: str) -> ActionExecution | None:
        """读一条。损坏的记录返回 None 并告警——**不抛**，读路径不该把 500 带给调用方。"""
        try:
            with self._lock:
                payload = self._store.read(execution_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读执行记录失败（%s）：%s", execution_id, exc)
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return ActionExecution.from_dict(payload)
        except Exception as exc:  # noqa: BLE001 - 结构不认识的按损坏处理
            logger.warning("执行记录结构异常（%s）：%s", execution_id, exc)
            return None

    def finish(
        self,
        execution_id: str,
        status: str,
        *,
        result: dict | None = None,
        note: str = "",
        risk: str = "",
    ) -> ActionExecution | None:
        """把一条执行落定为终态。找不到那条记录时返回 None（**不新建**）。

        为什么用 `update_atomic` 而不是「load → 改 → save」：后者在并发下会丢字段
        （两个请求先后落定同一条记录时，后写的会覆盖先写的）。`update_atomic` 把
        「读-改-写」放进同一把锁里——这是本仓库唯一的读改写原语（约定 [47]）。
        """
        def _mutate(current):
            if not isinstance(current, dict):
                return current  # 记录不存在：不凭空造一条终态记录
            record = ActionExecution.from_dict(current)
            record.finish(status, result=result, note=note, risk=risk)
            return record.to_dict()

        try:
            with self._lock:
                payload = self._store.update_atomic(execution_id, _mutate)
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(execution_id, f"执行记录落定失败：{exc}") from exc
        return ActionExecution.from_dict(payload) if isinstance(payload, dict) else None

    def recent(self, limit: int = 50) -> list[ActionExecution]:
        """最近的执行，新的在前。

        读全部再排序：执行记录的量级是「人手点几下 + Agent 每步一条」，
        与轨迹（会被裁剪）不同，它**不会被裁剪**——所以这里的成本可控。
        真要分页时，正解是 SQLite（见 README 的 V4 Storage Refactor）。
        """
        with self._lock:
            keys = list(self._store.keys())
        records = [record for record in (self.load(key) for key in keys) if record is not None]
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records[:limit]

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._store.keys())
