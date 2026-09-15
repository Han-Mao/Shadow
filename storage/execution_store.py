"""执行记录的存储（V4 §一 · v4.1 §二/§三）。

`ExecutionStore` 是「**这次执行**是谁、哪台设备、什么动作、什么风险、结果如何」的落点。
接口沿用 V4 §一 那一版（`create` / `save` / `load` / `finish` / `recent`），
**换掉的只是实现**：一执行一个 JSON 文件 → SQLite 表（v4.1 §二）。

## 为什么必须搬（不是「整齐」）

V4 结束时，任务 / 恢复点 / 事件 / 确认票据 / 租约都在 `shadow.db`，只有执行记录还是文件。
那版实现有一个它自己在 docstring 里就承认的短板：

- **没有天然的查询面**：`recent()` 是「遍历目录 + 逐个反序列化 + 内存排序」。
  「现在有哪些执行不是终态」这种问题更是只能全表扫。
- **状态迁移无法受保护**：文件版的「读 → 判断 → 写」之间有窗口。而 v4.1 §七 要的
  「同一条执行不能被派发两次」正是「读-判断-写」必须原子才能给出的保证。
  表上的 `UPDATE ... WHERE execution_id=? AND status=?` 一条语句就能回答它，
  而且 `BEGIN IMMEDIATE` 起手就拿了写锁——判断与写入之间没有别人能插进来。
- **与事件不同库**：§八 要「一次执行 → 它的完整事件轨迹」，`events.execution_id`
  已经是那一链的入口（迁移 v1），但两份事实在两个存储里就没有共同事务。

## 一条纪律：状态是**派生列**，不是权威

`status` / `risk_level` / `action_type` 这些列只服务于「查询」与「受保护迁移」。
读出来仍然以 payload 列为准（`action_payload` / `result`）。所以 `load()` 永远走
`_row_to_execution()`，而不是直接拿列拼一个对象——否则列与 payload 一旦不一致，
「读出来的事实」会取决于你走了哪条路径。

## 与另外两个存储的分工（V4 §五 要求它们能串成一条链）

    ExecutionStore  →  「**这次执行**是谁、哪台设备、什么动作、什么风险、结果如何」
    EventLog        →  「执行**过程**里发生了什么」（逐条事件）
    AuditLog        →  「HTTP 层谁调了什么接口」（含被拒的请求）

三者靠 `execution_id` 关联：`GET /executions/{id}` 会把执行记录与它的事件流拼在一起。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Collection
from pathlib import Path

from models.exceptions import PersistenceError
from models.execution import (
    NON_TERMINAL_EXECUTION_STATUSES,
    TERMINAL_EXECUTION_STATUSES,
    ActionExecution,
)

from .database import Database

logger = logging.getLogger(__name__)

# 写一条执行记录要落的所有列。与 `migrations` 里的 `executions` 表一一对应。
_INSERT_COLUMNS = (
    "execution_id",
    "task_id",
    "principal",
    "device_id",
    "channel",
    "action_type",
    "action_payload",
    "risk_level",
    "status",
    "request_id",
    "created_at",
    "started_at",
    "finished_at",
    "result",
    "note",
    "revision",
)

_INSERT_SQL = (
    f"INSERT INTO executions ({', '.join(_INSERT_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_INSERT_COLUMNS))})"
)

# `save` 的「写入或覆盖」语义（与文件版的 `JsonStore.write` 对齐）：
# 除主键外全部覆盖，`revision` 由数据库递增——这样「覆盖过一次」在记录里留得下痕迹。
_UPSERT_SQL = (
    _INSERT_SQL
    + " ON CONFLICT(execution_id) DO UPDATE SET "
    + ", ".join(
        f"{name}=excluded.{name}" for name in _INSERT_COLUMNS if name not in {"execution_id", "revision"}
    )
    + ", revision=executions.revision+1"
)


def _params(execution: ActionExecution, *, revision: int | None = None) -> tuple:
    """把领域对象摊成参数元组。**派生列从领域对象算**，调用方不必多传一遍。

    `revision` 可以显式覆盖：`create` 落库的永远是 0（新建的记录没被改过），
    而不是把调用方对象上那个可能从别处带过来的计数原样写进去。
    """
    return (
        execution.execution_id,
        execution.task_id,
        execution.principal,
        execution.device_id,
        execution.channel,
        str(execution.action.get("type") or ""),
        json.dumps(execution.action or {}, ensure_ascii=False),
        execution.risk,
        execution.status,
        execution.request_id,
        execution.created_at,
        execution.started_at or "",
        execution.finished_at or "",
        json.dumps(execution.result or {}, ensure_ascii=False),
        execution.note,
        0 if revision is None else int(revision),
    )


def _row_to_execution(row) -> ActionExecution | None:
    """行 → 领域对象。payload 坏掉时返回 None（读路径告警，不抛）。"""
    def _load(raw, what: str):
        try:
            value = json.loads(raw) if raw else {}
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{what} 不是合法 JSON：{exc}") from exc
        return value if isinstance(value, dict) else {}

    try:
        action = _load(row["action_payload"], "action_payload")
        result = _load(row["result"], "result")
    except ValueError as exc:
        logger.warning("执行记录结构异常（%s）：%s", row["execution_id"], exc)
        return None

    return ActionExecution(
        execution_id=row["execution_id"],
        task_id=row["task_id"],
        principal=row["principal"],
        device_id=row["device_id"],
        action=action,
        channel=row["channel"],
        status=row["status"],
        risk=row["risk_level"],
        request_id=row["request_id"],
        created_at=row["created_at"],
        # 落盘时空串 = 「没有这一时刻」。空串与 None 在 SQLite 里都表达不了，
        # 所以约定用一个哨兵值，读的一侧在这里还原——只在这一处做转换。
        started_at=row["started_at"] or None,
        finished_at=row["finished_at"] or None,
        result=result,
        note=row["note"],
        revision=int(row["revision"]),
    )


class ExecutionStore:
    """执行记录（SQLite 表 `executions`）。

    `target` 可以是 `Database`（**推荐**：与 TaskStore / EventLog / CheckpointStore
    共享一个库与事务）或一个路径：目录 → `<目录>/shadow.db`，`*.db` → 用它本身。
    """

    def __init__(self, target) -> None:
        self._db = target if isinstance(target, Database) else Database(target)
        self.path = str(self._db.path)
        # v4.1 §二：旧的 `executions/*.json` 首次打开时一次性导入
        self._legacy_root: Path = self._db.legacy_dir("executions")
        imported = self._import_legacy_json()
        if imported:
            logger.info("已把 %d 条历史执行记录导入 SQLite（%s）", imported, self.path)

    # ---- 写 ----

    def transaction(self):
        """一个跨「执行记录 + 事件」的事务（v4.1 §四/§九）。

        `ExecutionService` 用它把「状态迁移」与「它的事件」绑在一起：事件写失败时
        状态也回滚到迁移前那一格——于是「读状态」就足以判断事件有没有留下。
        可重入由 `Database.transaction()` 保证（内层加入外层）。
        """
        return self._db.transaction()

    def create(self, execution: ActionExecution) -> ActionExecution:
        """**准备**阶段：插入一条新执行记录（v4.1 §三/§四）。

        为什么它与 `save` 分开：`create` 是「这条执行从此刻起存在」——
        重复的 `execution_id` 是**事实冲突**（不可能是同一条执行被创建两次），
        所以它抛 `PersistenceError` 而不是静默覆盖。`save` 保留 V4 §一 的
        「写入或覆盖」语义给兼容调用点。
        """
        try:
            with self._db.transaction():
                self._db.execute(_INSERT_SQL, _params(execution, revision=0))
        except sqlite3.IntegrityError as exc:
            raise PersistenceError(
                execution.execution_id, f"执行记录已存在（拒绝覆盖）：{exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 统一翻译成持久化失败
            raise PersistenceError(execution.execution_id, f"执行记录写盘失败：{exc}") from exc
        execution.revision = 0
        return execution

    def save(self, execution: ActionExecution) -> None:
        """写入（或覆盖）一条执行记录。失败抛 `PersistenceError`（V4 §一 的语义，保留）。

        为什么**不吞异常**：这条记录是「手机被操作过」的唯一凭据。
        手工路径在动作发出前必须先把它落盘——写不进去就不该执行，
        否则会出现「真的点了付款，但查不到是谁点的」。

        覆盖时 `revision` 由数据库 +1，并把落库后的值同步回传进来的对象
        （否则内存里的 `revision` 会是一个永远不动的 0，看起来像「从没改过」）。
        """
        try:
            with self._db.transaction():
                self._db.execute(_UPSERT_SQL, _params(execution))
                row = self._db.query_one(
                    "SELECT revision FROM executions WHERE execution_id = ?",
                    (execution.execution_id,),
                )
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(execution.execution_id, f"执行记录写盘失败：{exc}") from exc
        if row is not None:
            execution.revision = int(row["revision"])

    def transition(
        self,
        execution_id: str,
        *,
        expect: Collection[str],
        to: str,
        result: dict | None = None,
        note: str = "",
        risk: str = "",
    ) -> ActionExecution | None:
        """**受保护的状态迁移**（v4.1 §四/§五）：只有当前状态在 `expect` 里才迁移。

        返回迁移后的记录；**没迁成返回 `None`**（记录不存在，或当前状态不在 `expect` 里）。
        这是 `EXECUTION_DISPATCHED → EXECUTION_RUNNING` 这类«只允许发生一次»的守卫，
        也就是审核 §十 测试4 要的「同一个 execution_id 不能被派发两次」。

        为什么不是「先 `load` 再 `save`」：那两步之间隔着一次释放锁，
        两个并发请求都能读到 `DISPATCHED`，然后各写一次——两次设备调用。
        这里读、判断、写都在**同一个 `BEGIN IMMEDIATE`** 里（起手即写锁），
        并且 `UPDATE` 自己再比一次 `status`：读判断给出的是「为什么拒绝」，
        `WHERE` 给出的是**原子性**。两者都要，缺了前者报错难懂，缺了后者正确性靠不住。
        """
        try:
            with self._db.transaction():
                row = self._db.query_one(
                    "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
                )
                if row is None:
                    return None
                record = _row_to_execution(row)
                if record is None:
                    return None
                current = record.status
                if current not in expect:
                    logger.warning(
                        "执行 %s 的状态迁移被拒：当前 %s 不在期望集合 %s 内",
                        execution_id,
                        current,
                        sorted(expect),
                    )
                    return None

                record.mark(to)
                record.revision += 1
                if result is not None:
                    record.result = result
                if note:
                    record.note = note
                if risk:
                    record.risk = risk
                changed = self._db.execute(
                    "UPDATE executions SET status=?, started_at=?, finished_at=?, result=?, "
                    "note=?, risk_level=?, revision=? "
                    "WHERE execution_id=? AND status=?",
                    (
                        record.status,
                        record.started_at or "",
                        record.finished_at or "",
                        json.dumps(record.result or {}, ensure_ascii=False),
                        record.note,
                        record.risk,
                        record.revision,
                        execution_id,
                        current,
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(execution_id, f"执行状态迁移失败：{exc}") from exc

        if not changed:
            # 唯一可能：同一毫秒里有别人先迁走了（`WHERE status=?` 落空）。
            # 这时**不能**把内存里的 record 当成事实返回——它没写进去。
            logger.warning("执行 %s 的状态迁移落空（已被并发改写）", execution_id)
            return None
        return record

    def finish(
        self,
        execution_id: str,
        status: str,
        *,
        result: dict | None = None,
        note: str = "",
        risk: str = "",
    ) -> ActionExecution | None:
        """把一条执行落定为终态。找不到那条记录时返回 `None`（**不新建**，V4 §一）。

        与文件版的差别只有一处：**已经是终态的记录不再被改写**。
        以前它是「读-改-写」，第二次落定会覆盖第一次——而 `UNKNOWN`（进程死在设备调用
        之后）最怕的就是被后来的一次调用顺手改成 `FAILED`：那会把「可能已经付款了」
        抹成「失败了，重试吧」。终态是事实，事实之间不互相覆盖。

        落定失败抛 `PersistenceError`——上层要能区分「动作失败」与「记录失败」。
        """
        record = self.transition(
            execution_id,
            expect=set(NON_TERMINAL_EXECUTION_STATUSES),
            to=status,
            result=result,
            note=note,
            risk=risk,
        )
        if record is not None:
            return record
        # 没迁成：要么记录不存在（返回 None），要么它已经是终态（返回现状 + 告警）
        existing = self.load(execution_id)
        if existing is not None and existing.status in TERMINAL_EXECUTION_STATUSES:
            logger.warning(
                "执行 %s 已是终态 %s，拒绝把它改写为 %s",
                execution_id,
                existing.status,
                status,
            )
        return existing

    # ---- 读 ----

    def load(self, execution_id: str) -> ActionExecution | None:
        """读一条。损坏的记录返回 None 并告警——**不抛**，读路径不该把 500 带给调用方。"""
        try:
            row = self._db.query_one(
                "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读执行记录失败（%s）：%s", execution_id, exc)
            return None
        if row is None:
            return None
        return _row_to_execution(row)

    def recent(self, limit: int = 50) -> list[ActionExecution]:
        """最近的执行，新的在前。

        `created_at` 是**记录自己的**时间戳（可能是补录的），所以并列时用 `rowid`
        兜底定序——同一毫秒创建的两条记录否则谁先谁后不确定，而「最近的执行」
        这种给人看的列表必须是稳定顺序。
        """
        return self._query_recent(limit=limit, statuses=None)

    def by_status(self, statuses: Collection[str], *, limit: int = 50) -> list[ActionExecution]:
        """按状态取最近的若干条（新的在前）。

        `GET /executions?status=UNKNOWN` 用它回答 §七 的那个操作性问题：
        「现在有哪些执行是效果未知的」——不知道有哪些，就没法对账。
        """
        return self._query_recent(limit=limit, statuses=list(statuses))

    def unfinished(self, *, limit: int = 500) -> list[ActionExecution]:
        """**非终态**的执行，旧的在前（v4.1 §六 的恢复扫描）。

        旧的在前是有意的：恢复要先处理最早那批（它们最可能是上一个进程留下的），
        上限 `limit` 防的是「异常积累了几万条非终态记录」把启动拖死——
        真出现那种情况，先恢复一部分、下次启动接着扫，比卡在启动阶段好。
        """
        placeholders = ", ".join("?" * len(NON_TERMINAL_EXECUTION_STATUSES))
        rows = self._db.query(
            f"SELECT * FROM executions WHERE status IN ({placeholders}) "
            "ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (*sorted(NON_TERMINAL_EXECUTION_STATUSES), max(0, int(limit))),
        )
        return [record for record in (_row_to_execution(row) for row in rows) if record is not None]

    def _query_recent(self, *, limit: int, statuses: list[str] | None) -> list[ActionExecution]:
        clause = ""
        params: tuple = ()
        if statuses:
            clause = f"WHERE status IN ({', '.join('?' * len(statuses))}) "
            params = tuple(statuses)
        try:
            rows = self._db.query(
                f"SELECT * FROM executions {clause}"
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (*params, max(0, int(limit))),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("列执行记录失败：%s", exc)
            return []
        return [record for record in (_row_to_execution(row) for row in rows) if record is not None]

    def keys(self) -> list[str]:
        """全部执行 id（不保证顺序）——测试与运维清点用。"""
        return [row["execution_id"] for row in self._db.query("SELECT execution_id FROM executions")]

    def count(self, statuses: Collection[str] | None = None) -> int:
        """记录总数；给了 `statuses` 就只数这些状态的行（`/health/detail` 用它报
        「有多少次执行的效果还没交代清楚」）。"""
        if statuses is None:
            return int(self._db.query_one("SELECT COUNT(*) AS n FROM executions")["n"])
        wanted = list(statuses)
        if not wanted:
            return 0
        placeholders = ", ".join("?" * len(wanted))
        row = self._db.query_one(
            f"SELECT COUNT(*) AS n FROM executions WHERE status IN ({placeholders})",
            tuple(wanted),
        )
        return int(row["n"])

    def close(self) -> None:
        """只关连接——**共享 `Database` 时不要调它**（别的 store 还在用同一个连接）。"""
        self._db.close()

    # ---- 内部 ----

    def _import_legacy_json(self) -> int:
        """把旧的 `executions/*.json` 一次性搬进表里，返回导入条数（v4.1 §二）。

        跳过条件与 `EventLog._import_legacy_jsonl` 一致：目录里没有 `.json`，
        或表里已经有记录（说明已经在用新存储，不该再往里灌历史）。

        导入保留**原来的 execution_id、时间戳与状态**：否则历史执行会看起来
        「全都发生在升级那一刻」，而审计与 §六 的恢复扫描正是要看它们的先后与状态。
        """
        if not self._legacy_root.exists():
            return 0
        files = sorted(self._legacy_root.glob("*.json"))
        if not files or self.count() > 0:
            return 0

        imported = 0
        for path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    continue
                record = ActionExecution.from_dict(payload)
                # 老记录可能没有这些字段（dataclass 默认值兜住了），补上主键来源
                if not record.execution_id:
                    record.execution_id = path.stem
                with self._db.transaction():
                    self._db.execute(_INSERT_SQL, _params(record, revision=record.revision))
            except sqlite3.IntegrityError:
                # 同一个 id 已经在表里（重复导入）——跳过，不动已有事实
                continue
            except Exception as exc:  # noqa: BLE001 - 一条坏记录不该挡住其余导入
                logger.warning("导入历史执行记录失败（%s）：%s", path.name, exc)
                continue
            imported += 1
        return imported
