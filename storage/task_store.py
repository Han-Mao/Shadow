"""TaskStore：任务的生命周期持久化。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from models.exceptions import ConcurrentModificationError, CorruptDataError
from models.task import Task, TaskStatus

from .json_store import JsonStore

logger = logging.getLogger(__name__)


class TaskStore:
    def __init__(self, root: str | Path) -> None:
        self._store = JsonStore(root)
        self._quarantine_root = Path(root) / "quarantine"
        self._quarantine_root.mkdir(parents=True, exist_ok=True)
        # V2.3：损坏的任务 id 集合，list_all 时可以暴露它们而不是假装不存在。
        self._corrupt_ids: set[str] = set()

    def save(self, task: Task, *, expected_revision: int | None = None) -> None:
        """写入任务；每次写入把 `revision` 推进一格（V2.4 §二）。

        传 `expected_revision` 即启用乐观并发校验（CAS）：磁盘上的 revision
        与期望值不符时抛 `ConcurrentModificationError`，**且不做任何写入**。
        TaskManager 改写「当前任务」时必须走这条路——否则 Runtime 刚判定的
        DONE 会被随后的改写覆盖成「done + 新目标」。

        不传就是普通写入（Runtime / Scheduler 推进自身状态走这条），
        只额外做一次 revision 自增。
        """
        current = self._revision_on_disk(task.id)
        if expected_revision is not None and current is not None and current != expected_revision:
            raise ConcurrentModificationError(task.id, expected_revision, current)
        task.revision = (current or 0) + 1
        self._store.write(task.id, task.model_dump(mode="json"))

    def _revision_on_disk(self, task_id: str) -> int | None:
        """只读磁盘上的写入序号；文件不存在 / 读不出 / 没有该字段都返回 None。

        刻意不复用 `load()`：它会把损坏条目移进隔离区，而「校验 revision」
        是一次纯读取，不该带副作用。
        """
        try:
            payload = self._store.read(task_id)
        except CorruptDataError:
            return None
        if not isinstance(payload, dict):
            return None
        raw = payload.get("revision")
        return raw if isinstance(raw, int) else None

    def load(self, task_id: str) -> Task | None:
        try:
            payload = self._store.read(task_id)
        except CorruptDataError as exc:
            self._quarantine(task_id, exc.path)
            return None
        if payload is None:
            return None
        try:
            return Task.model_validate(payload)
        except Exception:
            # 老版本写下的结构可能在升级后校验失败，移到隔离区并暴露。
            path = self._store._path(task_id)
            self._quarantine(task_id, str(path))
            return None

    def _quarantine(self, task_id: str, path: str) -> None:
        """把损坏文件移到隔离区，并在内存中记录，避免它从恢复列表里消失。"""
        src = Path(path)
        dst = self._quarantine_root / f"{task_id}.corrupt.json"
        try:
            if src.exists():
                shutil.move(str(src), str(dst))
        except Exception as exc:  # noqa: BLE001 - 隔离失败不该中断服务
            logger.warning("任务 %s 损坏文件隔离失败：%s", task_id, exc)
        self._corrupt_ids.add(task_id)
        logger.error("任务 %s 数据损坏，已隔离到 %s", task_id, dst)

    def list_all(self) -> list[Task]:
        tasks = [t for t in (self.load(key) for key in self._store.keys()) if t is not None]
        return sorted(tasks, key=lambda t: t.created_at)

    def list_active(self) -> list[Task]:
        return [t for t in self.list_all() if not t.is_terminal]

    def corrupt_ids(self) -> set[str]:
        """返回本次进程生命周期内发现的损坏任务 id。"""
        return set(self._corrupt_ids)

    def delete(self, task_id: str) -> None:
        self._store.delete(task_id)

    def mark_cancelled(self, task_id: str) -> Task | None:
        task = self.load(task_id)
        if task is None:
            return None
        task.mark(TaskStatus.CANCELLED, source="task_store")
        self.save(task)
        return task
