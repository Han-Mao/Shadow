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
        # V2.5 §三：索引必须在启动时重建。隔离是**惰性**发生的（下次读到才登记），
        # 而 `_corrupt_ids` 是内存集合、重启即空——于是会出现「第一次请求 404、
        # 第二次才 recovery_error」这种前后不一致，正是 README 声称要避免的行为。
        self._restore_corrupt_index()

    def _restore_corrupt_index(self) -> None:
        """从 `quarantine/` 目录重建损坏索引（V2.5 §三）。

        文件名约定是 `{task_id}.corrupt.json`（见 `_quarantine`），据此反推 id。
        """
        try:
            for path in self._quarantine_root.glob("*.corrupt.json"):
                task_id = path.name[: -len(".corrupt.json")]
                if task_id:
                    self._corrupt_ids.add(task_id)
        except OSError as exc:  # noqa: BLE001 - 索引不完整也要能启动
            logger.warning("扫描隔离目录失败（损坏索引可能不完整）：%s", exc)
        if self._corrupt_ids:
            logger.warning(
                "从隔离目录恢复了 %d 条损坏任务：%s",
                len(self._corrupt_ids),
                sorted(self._corrupt_ids),
            )

    def save(self, task: Task, *, expected_revision: int | None = None) -> None:
        """写入任务；每次写入把 `revision` 推进一格（V2.4 §二 / V2.5 §一）。

        传 `expected_revision` 即启用乐观并发校验（CAS）：磁盘上的 revision 与期望值
        不符时抛 `ConcurrentModificationError`，**且不做任何写入**。TaskManager 改写
        「当前任务」时必须走这条路——否则 Runtime 刚判定的 DONE 会被随后的改写覆盖成
        「done + 新目标」。

        **读 - 比较 - 递增 - 写必须在同一个临界区**（V2.5 §一）：拆成
        「read() → 比较 → write()」的话，两次加锁之间仍有窗口，两个写者可以都通过
        比较，后写的静默覆盖先写的。所以这里交给 `JsonStore.update_atomic`。
        """

        def _mutate(current: object) -> dict:
            raw = current.get("revision") if isinstance(current, dict) else None
            actual = raw if isinstance(raw, int) else 0
            if expected_revision is not None and actual != expected_revision:
                raise ConcurrentModificationError(task.id, expected_revision, actual)
            task.revision = actual + 1
            return task.model_dump(mode="json")

        self._store.update_atomic(task.id, _mutate)

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
