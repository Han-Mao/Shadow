"""TaskStore：任务的生命周期持久化。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from models.exceptions import ConcurrentModificationError, CorruptDataError
from models.task import Task, TaskStatus

from .event_log import TASK_CORRUPTED
from .json_store import JsonStore

logger = logging.getLogger(__name__)


class TaskStore:
    def __init__(self, root: str | Path, *, event_log=None) -> None:
        self._store = JsonStore(root)
        self._quarantine_root = Path(root) / "quarantine"
        self._quarantine_root.mkdir(parents=True, exist_ok=True)
        # V2.3：损坏的任务 id 集合，list_all 时可以暴露它们而不是假装不存在。
        self._corrupt_ids: set[str] = set()
        # V2.4 §十：损坏必须进事件流。审核的原话是「不能让『数据损坏』表现成
        # 『不存在』」——隔离只是把文件挪走，如果没留下一条可追溯的事实，
        # 事后没人回答得了「这条任务去哪了」。可选依赖：不传就只记日志。
        self._event_log = event_log

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
            self._quarantine(task_id, exc.path, reason=exc.reason)
            return None
        if payload is None:
            return None
        try:
            return Task.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - 任何解析失败都按损坏处理
            # 老版本写下的结构可能在升级后校验失败，同样要隔离并暴露。
            path = self._store._path(task_id)
            self._quarantine(task_id, str(path), reason=f"反序列化失败：{exc}")
            return None

    def _quarantine(self, task_id: str, path: str, *, reason: str = "") -> None:
        """把损坏文件移到隔离区、记账、并留一条事件（V2.4 §十）。

        三件事缺一不可：
        - **移走文件**：否则下次 list_all 又会把它当正常条目读一遍
        - **记账**（`_corrupt_ids`）：让 list_all / API 能回答「它去哪了」
        - **留事件**（`TASK_CORRUPTED`）：写进只追加的事件流，事后可追溯
        """
        src = Path(path)
        dst = self._quarantine_root / f"{task_id}.corrupt.json"
        moved = False
        try:
            if src.exists():
                shutil.move(str(src), str(dst))
                moved = True
        except Exception as exc:  # noqa: BLE001 - 隔离失败不该中断服务
            logger.warning("任务 %s 损坏文件隔离失败：%s", task_id, exc)
        self._corrupt_ids.add(task_id)
        logger.error("任务 %s 数据损坏，已隔离到 %s（%s）", task_id, dst, reason or "原因未记录")
        if self._event_log is not None:
            # EventLog.emit 自身永不抛异常（审计是旁路），这里不必再包一层 try
            self._event_log.emit(
                task_id,
                TASK_CORRUPTED,
                reason=reason or "未知",
                quarantined_to=str(dst),
                moved=moved,
            )

    def is_corrupt(self, task_id: str) -> bool:
        """这条 id 是否已被判定为数据损坏（本次进程生命周期内）。"""
        return task_id in self._corrupt_ids

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
