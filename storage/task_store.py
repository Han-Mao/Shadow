"""TaskStore：任务的生命周期持久化（V4 §二 起在 SQLite 上）。

改的是**存储**，不是语义。四条契约一个字都没动：

1. **损坏 ≠ 不存在**：坏记录被移出在用集合、原始内容留成证据文件、id 记进 `corrupt_ids`、
   事件流里一条 `TASK_CORRUPTED`（V2.4 §十）。
2. **CAS 是原子的**：`save(expected_revision=)` 要么推进一格、要么抛
   `ConcurrentModificationError` 且不写（V2.5 §一）。
3. **损坏记录不可被覆盖**：对坏记录调 `save` 一律抛 `CorruptDataError`——否则
   「隔离 → 事件 → recovery_error」这条链会被绕过（V2.6 §三）。
4. **索引跨重启可重建**：启动时扫 `quarantine/`（V2.5 §三）。

从「一个任务一个 JSON 文件」换到表，换来的是 V4 §二/§三 要的东西：

    BEGIN IMMEDIATE
      读 revision → 比较 → 递增 → 写 payload       ← CAS 的临界区由数据库给，跨进程成立
      （同一个事务里还能顺带写事件与恢复点指针 —— §三 要的「同一事务」）
    COMMIT

`payload` 是权威内容；`revision` / `status` / `created_at` 是**派生列**（与 events 的
关联列同一个口径），只为查询与 CAS 服务。

隔离目录仍在 `<存储目录>/tasks/quarantine/`（与升级前同一个路径），
所以 `GET /tasks` 的 `corrupt` 列表与运维脚本都不受影响。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from models.exceptions import ConcurrentModificationError, CorruptDataError
from models.task import Task, TaskStatus

from .database import Database
from .event_log import TASK_CORRUPTED

logger = logging.getLogger(__name__)


class TaskStore:
    def __init__(self, target, *, event_log=None) -> None:
        """`target` 可以是 `Database`（推荐：与 EventLog / CheckpointStore 共享一个库与事务）
        或一个路径（目录 → `<目录>/shadow.db`，`*.db` → 用它本身）。"""
        self._db = target if isinstance(target, Database) else Database(target)
        # V4 之前任务写在 `<存储目录>/tasks/*.json`；迁移时按这个约定导入，隔离目录也在这里
        self._legacy_root = self._db.legacy_dir("tasks")
        self._quarantine_root = self._legacy_root / "quarantine"
        self._quarantine_root.mkdir(parents=True, exist_ok=True)
        # V2.3：损坏的任务 id 集合，list_all 时可以暴露它们而不是假装不存在。
        self._corrupt_ids: set[str] = set()
        # V2.4 §十：损坏必须进事件流。审核的原话是「不能让『数据损坏』表现成
        # 『不存在』」——隔离只是把它移出在用集合，如果没留下可追溯的事实，
        # 事后没人回答得了「这条任务去哪了」。可选依赖：不传就只记日志。
        self._event_log = event_log
        # V2.5 §三：索引必须在启动时重建。隔离是**惰性**发生的（下次读到才登记），
        # 而 `_corrupt_ids` 是内存集合、重启即空——于是会出现「第一次请求 404、
        # 第二次才 recovery_error」这种前后不一致，正是 README 声称要避免的行为。
        self._restore_corrupt_index()
        imported = self._import_legacy_json()
        if imported:
            logger.info("已把 %d 个历史任务 JSON 导入 SQLite（%s）", imported, self._db.path)

    # ---- 迁移与损坏索引 ----

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

    def _import_legacy_json(self) -> int:
        """把旧版 `<存储目录>/tasks/*.json` 一次性搬进表（V4 §二）。

        内容**原样**搬（不做校验）：合法 JSON 正常使用；坏文件搬成一条 payload 坏掉的
        行，会在第一次读到它时被隔离并暴露——而不是在这里被静默丢掉。
        「数据损坏 ≠ 不存在」这条要求在**迁移**这一环同样成立。
        """
        root = self._legacy_root
        if not root.exists() or self._db.query_one("SELECT 1 FROM tasks LIMIT 1") is not None:
            return 0

        imported = 0
        for path in sorted(root.glob("*.json")):
            task_id = path.stem
            if not task_id:
                continue
            text = path.read_text(encoding="utf-8")
            revision = 0
            status = TaskStatus.CREATED.value
            created_at = ""
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                raw = None
            if isinstance(raw, dict):
                revision = raw.get("revision") if isinstance(raw.get("revision"), int) else 0
                status = str(raw.get("status") or status)
                created_at = str(raw.get("created_at") or "")
            self._db.execute(
                "INSERT OR IGNORE INTO tasks (task_id, revision, status, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, revision, status, created_at, text),
            )
            imported += 1
        return imported

    # ---- 写 ----

    def save(self, task: Task, *, expected_revision: int | None = None) -> None:
        """写入任务；每次写入把 `revision` 推进一格（V2.4 §二 / V2.5 §一）。

        传 `expected_revision` 即启用乐观并发校验（CAS）：库里的 revision 与期望值
        不符时抛 `ConcurrentModificationError`，**且不做任何写入**。

        **读 - 比较 - 递增 - 写必须在同一个临界区**（V2.5 §一）：以前靠
        `JsonStore.update_atomic` 把四步塞进同一把锁，现在是 `BEGIN IMMEDIATE`
        里先读再写——临界区由数据库给，**跨进程也成立**（这是升级前做不到的）。
        """
        with self._db.transaction():
            row = self._db.query_one(
                "SELECT revision, payload FROM tasks WHERE task_id = ?", (task.id,)
            )
            actual = 0
            if row is not None:
                if not _is_json(row["payload"]):
                    # V2.6 §三：损坏记录不能被任何 save 覆盖，否则隔离链路被绕过
                    raise CorruptDataError(
                        task.id, str(self._db.path), "payload 不是合法 JSON，拒绝覆盖"
                    )
                actual = int(row["revision"])
            if expected_revision is not None and actual != expected_revision:
                raise ConcurrentModificationError(task.id, expected_revision, actual)
            task.revision = actual + 1
            self._db.execute(
                "INSERT INTO tasks (task_id, revision, status, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "revision = excluded.revision, status = excluded.status, "
                "created_at = excluded.created_at, payload = excluded.payload",
                (
                    task.id,
                    task.revision,
                    task.status.value,
                    str(task.created_at),
                    json.dumps(task.model_dump(mode="json"), ensure_ascii=False),
                ),
            )

    # ---- 读 ----

    def load(self, task_id: str) -> Task | None:
        row = self._db.query_one("SELECT payload FROM tasks WHERE task_id = ?", (task_id,))
        if row is None:
            return None
        return self._load_payload(task_id, row["payload"])

    def _load_payload(self, task_id: str, payload: str) -> Task | None:
        """从 payload 还原任务；任何解析失败都按损坏处理（隔离 + 记账 + 留痕）。"""
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            self._quarantine(task_id, payload, reason=f"payload 不是合法 JSON：{exc}")
            return None
        try:
            return Task.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - 任何解析失败都按损坏处理
            # 老版本写下的结构可能在升级后校验失败，同样要隔离并暴露。
            self._quarantine(task_id, payload, reason=f"反序列化失败：{exc}")
            return None

    def _quarantine(self, task_id: str, payload: str | None, *, reason: str = "") -> None:
        """把损坏记录移出在用集合、留成证据文件、记账、并写一条事件（V2.4 §十）。

        V4 §二 之后「移走文件」变成了「删掉那行 + 把原始 payload 落成隔离文件」：
        隔离的价值在于「坏数据一旦被发现就是只读的、且留下可查的痕迹」，
        而不是它必须以文件形式留在原处。所以：

        - 原始内容**原样**写到 `quarantine/<id>.corrupt.json`（人要看的就是它）；
        - 表里的行**删掉**（否则 list_all / keys 每次都会再撞同一个坏行）；
        - `_corrupt_ids` 记账 + `TASK_CORRUPTED` 事件留痕（这两条完全没变）。

        事件里 `moved` 的含义从「文件被移走」变成「已从在用集合移出」——
        字段与语义仍是「有没有被清出活数据」，调用方的判断不受影响。
        """
        dst = self._quarantine_root / f"{task_id}.corrupt.json"
        moved = False
        try:
            dst.write_text(payload or "", encoding="utf-8")
            moved = True
        except OSError as exc:  # noqa: BLE001 - 隔离失败不该中断服务
            logger.warning("任务 %s 损坏内容落盘失败：%s", task_id, exc)
        try:
            self._db.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        except Exception as exc:  # noqa: BLE001
            logger.warning("任务 %s 的损坏行删除失败：%s", task_id, exc)
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
        rows = self._db.query("SELECT task_id, payload FROM tasks ORDER BY created_at")
        tasks = [t for t in (self._load_payload(r["task_id"], r["payload"]) for r in rows) if t]
        return sorted(tasks, key=lambda t: t.created_at)

    def list_active(self) -> list[Task]:
        return [t for t in self.list_all() if not t.is_terminal]

    def corrupt_ids(self) -> set[str]:
        """返回本次进程生命周期内发现的损坏任务 id。"""
        return set(self._corrupt_ids)

    def keys(self) -> list[str]:
        return [row["task_id"] for row in self._db.query("SELECT task_id FROM tasks")]

    def delete(self, task_id: str) -> None:
        self._db.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))

    def mark_cancelled(self, task_id: str) -> Task | None:
        task = self.load(task_id)
        if task is None:
            return None
        task.mark(TaskStatus.CANCELLED, source="task_store")
        self.save(task)
        return task


def _is_json(text: str | None) -> bool:
    """能不能解析成 JSON。**故意只做 JSON 这一层**：与升级前 `JsonStore` 的损坏判定一致
    （结构漂移由 `Task.model_validate` 在**读**的时候判，那时才隔离）。"""
    if not text:
        return False
    try:
        json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return False
    return True
