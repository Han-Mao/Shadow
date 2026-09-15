"""ConfirmationConsumptionStore：确认令牌的「已消费」记录（V3.2 §二 · V3.3 §六 · V4 §三）。

三件事在这里叠起来，缺一件这套「一次性审批」就不成立：

1. **跨重启**（V3.2 §二）：记录的签名密钥是稳定的，所以「一次性」若只活在进程内存里，
   重启后一张仍在 TTL 内的旧票据会复活。判定靠 `jti` 主键 + 原子 INSERT。
2. **两阶段**（V3.3 §六）：`reserve`（预占，不作废）→ 改业务状态 → `commit`（作废）；
   业务失败则 `release` 退回。中间那步失败时不该把用户的票据烧掉。
3. **同一事务**（V4 §三）：现在这张表与 `tasks` / `events` 在**同一个库**里，
   于是 `/confirm` 能做到「校验并预占票据 → 改 Task 状态 → 写 CONFIRMED 事件 → 作废票据」
   一次提交。跨文件没有事务——这也是它从独立 `confirmations.db` 搬进来的原因。

为什么仍然保留「表」这层隔离而不是和 tasks 混一张表：两者的清空语义相反。
租约/票据类数据在运维上**可以**整体清空（那个任务重新排队就行），
而审批记录**绝不能**清——清了等于把用过的票据又变成可用的。
表分开 + 注释写明，比放在不同文件里更容易被正确对待。

`sqlite3` 是标准库，零额外依赖。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

from .database import Database

logger = logging.getLogger(__name__)

# 预占的有效期（秒）。崩溃后留下的 `reserved` 行超过它就允许被重新预占——
# 它**不是**令牌 TTL（那是签名里的 expires_at），只决定「崩溃后多久能重试同一张票据」。
# 60 秒足够覆盖一次确认请求的正常耗时（改状态是本地操作），又短到不至于让人等。
RESERVE_TTL_SECONDS = float(os.getenv("SHADOW_CONFIRM_RESERVE_TTL_SECONDS", "60"))

_INSERT_FIELDS = (
    "jti",
    "task_id",
    "principal",
    "fingerprint",
    "consumed_at",
    "expires_at",
    "state",
    "reserved_at",
)


class ConfirmationConsumptionStore:
    """确认票据消费表（表名 `consumed_confirmation`）。"""

    def __init__(self, target, *, legacy_path: str | Path | None = None) -> None:
        """`target` 可以是 `Database`（推荐：与 tasks / events 共享库与事务，§三 需要它）
        或一个路径（目录 → `<目录>/shadow.db`，`*.db` → 用它本身）。"""
        self._owns_database = not isinstance(target, Database)
        self._db = target if isinstance(target, Database) else Database(target)
        # V3.2 时期的独立文件（`<存储目录>/confirmations.db`）。显式传 `legacy_path`
        # （`SHADOW_CONFIRM_DB` 指向老文件的场景）时优先用它。
        self._legacy_path = Path(legacy_path) if legacy_path else self._db.legacy_dir("confirmations.db")
        self._ensure_columns()
        imported = self._import_legacy_rows()
        if imported:
            logger.info("已把 %d 条历史确认票据记录导入 SQLite（%s）", imported, self._db.path)

    # ---- 迁移 ----

    def _ensure_columns(self) -> None:
        """补齐 `state` / `reserved_at` 两列。

        主库里的表由迁移 v3 建好，这里其实是**给老文件兜底**：`SHADOW_CONFIRM_DB`
        指向一个 V3.2 时期建的表（6 列）时，`CREATE TABLE IF NOT EXISTS` 是空操作，
        少了两列会让所有读写直接报错。判断列存在必须靠 `PRAGMA table_info`——
        直接 `ALTER TABLE ADD COLUMN` 在第二次启动时会抛 `duplicate column`。
        """
        columns = {
            row["name"]
            for row in self._db.query("PRAGMA table_info(consumed_confirmation)")
        }
        if not columns:
            return
        if "state" not in columns:
            self._db.execute(
                "ALTER TABLE consumed_confirmation ADD COLUMN state TEXT NOT NULL DEFAULT 'consumed'"
            )
        if "reserved_at" not in columns:
            self._db.execute(
                "ALTER TABLE consumed_confirmation ADD COLUMN reserved_at REAL NOT NULL DEFAULT 0"
            )
        # 索引在这里建（不在迁移里）：见 `storage/migrations/__init__.py` 的说明——
        # 老表缺列时迁移里的索引语句会让「升级」变成「起不来」。
        # 放在补齐列之后，它就永远是安全的，而且对已经跑过迁移的老库也是自愈的。
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_confirmation_state ON consumed_confirmation(state)"
        )

    def _import_legacy_rows(self) -> int:
        """把旧的独立 `confirmations.db` 里已消费的 jti 搬进主库（只做一次）。

        **这一步必须做**：「一次性」完全依赖这张表。升级时把它丢在一边，
        那些**升级前用过、仍在 TTL 内**的票据就会重新可用——而那正是 V3.2 §二 修掉的洞。
        旧文件保持不动（它是证据），只在表空且文件存在时搬。
        """
        legacy = self._legacy_path
        if legacy is None or not legacy.exists() or self.count() > 0:
            return 0
        if Path(self._db.path).resolve() == legacy.resolve():
            return 0  # 就是同一个文件（老部署直接把它当主库用），没什么可搬

        try:
            source = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
            source.row_factory = sqlite3.Row
            rows = source.execute("SELECT * FROM consumed_confirmation").fetchall()
            source.close()
        except sqlite3.Error as exc:
            logger.warning("读取旧确认票据库失败（%s）：%s", legacy, exc)
            return 0

        imported = 0
        for row in rows:
            keys = set(row.keys())
            self._db.execute(
                "INSERT OR IGNORE INTO consumed_confirmation "
                "(jti, task_id, principal, fingerprint, consumed_at, expires_at, state, reserved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["jti"],
                    row["task_id"] if "task_id" in keys else "",
                    row["principal"] if "principal" in keys else "",
                    row["fingerprint"] if "fingerprint" in keys else "",
                    row["consumed_at"] if "consumed_at" in keys else 0.0,
                    row["expires_at"] if "expires_at" in keys else 0.0,
                    row["state"] if "state" in keys else "consumed",
                    row["reserved_at"] if "reserved_at" in keys else 0.0,
                ),
            )
            imported += 1
        return imported

    # ---- 一步式（V3.2 语义，保留给不需要「改状态」的调用方与老测试）----

    def consume(
        self,
        jti: str,
        *,
        task_id: str = "",
        principal: str = "",
        fingerprint: str = "",
        expires_at: float = 0.0,
        now: float | None = None,
    ) -> bool:
        """原子消费这个 jti。返回 False 表示**之前已经被消费（或占着）**。

        实现是「同一事务里先查再插」：事务一开始就拿了写锁（`BEGIN IMMEDIATE`），
        所以「查」与「插」之间没有窗口——跨进程的第二个请求会先等锁，
        等到了就已经能看见第一个请求提交的行。

        为什么不写成单条 `INSERT` 靠主键冲突判重（V3.3 之前那样）：`reserve` 留下的
        **`reserved` 行也要挡住 `consume`**（预占中的票据不能被另一条路径消费掉），
        而单条 INSERT 只看主键冲突、分不清状态。`IntegrityError` 仍然兜着——
        它是最后一道保险，不是主判据。
        """
        if not jti:
            return False
        current = time.time() if now is None else now
        try:
            with self._db.transaction():
                if self._reserved_or_consumed(jti):
                    return False
                self._db.execute(
                    "INSERT INTO consumed_confirmation "
                    "(jti, task_id, principal, fingerprint, consumed_at, expires_at, state, reserved_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'consumed', ?)",
                    (jti, task_id, principal, fingerprint, current, expires_at, current),
                )
        except sqlite3.IntegrityError:
            return False
        self._prune_quietly(now=current)
        return True

    # ---- 两步式（V3.3 §六）----

    def reserve(
        self,
        jti: str,
        *,
        task_id: str = "",
        principal: str = "",
        fingerprint: str = "",
        expires_at: float = 0.0,
        now: float | None = None,
    ) -> bool:
        """**预占**这个 jti（尚未消费）。False = 已被消费，或已被别人预占。

        一条 SQL 完成「插入或接管过期预占」：

        - 没有这个 jti → 插入 `reserved` 行 → 成功；
        - 已有 `consumed` 行 → `WHERE` 不成立，更新被跳过 → False（用过就是用过）；
        - 已有**新鲜的** `reserved` 行 → 同样跳过 → False（另一个请求正在处理它）；
        - 已有**过期**的 `reserved` 行 → 接管（崩在中间的预占不该把票据永久卡死）。

        `cursor.rowcount` 是判据：0 表示那条 `WHERE` 挡住了更新，也就是「没占上」。
        它可以**加入外层事务**（§三）：`/confirm` 里预占、提交、退回都在同一个事务里，
        任何一步失败都会把它们一起回滚。
        """
        if not jti:
            return False
        current = time.time() if now is None else now
        stale_before = current - RESERVE_TTL_SECONDS
        with self._db.transaction():
            affected = self._db.execute(
                """
                INSERT INTO consumed_confirmation
                    (jti, task_id, principal, fingerprint, consumed_at, expires_at, state, reserved_at)
                VALUES (?, ?, ?, ?, 0, ?, 'reserved', ?)
                ON CONFLICT(jti) DO UPDATE SET
                    state       = 'reserved',
                    reserved_at = excluded.reserved_at,
                    expires_at  = excluded.expires_at,
                    task_id     = excluded.task_id,
                    principal   = excluded.principal,
                    fingerprint = excluded.fingerprint
                WHERE consumed_confirmation.state = 'reserved'
                  AND consumed_confirmation.reserved_at < ?
                """,
                (jti, task_id, principal, fingerprint, expires_at, current, stale_before),
            )
        reserved = affected == 1
        if reserved:
            self._prune_quietly(now=current)
        return reserved

    def commit(
        self,
        jti: str,
        *,
        expires_at: float | None = None,
        now: float | None = None,
        **_evidence,
    ) -> bool:
        """把预占转成**已消费**（「一次性」就此永久生效）。False = 没有可提交的预占。

        `expires_at` 只为与内存守卫的签名保持一致而接受——过期时刻在 `reserve`
        那一刻已经记在这行里了。`**_evidence` 同理（证据字段已在 reserve 时落库）。
        """
        if not jti:
            return False
        current = time.time() if now is None else now
        affected = self._db.execute(
            "UPDATE consumed_confirmation SET state = 'consumed', consumed_at = ? "
            "WHERE jti = ? AND state = 'reserved'",
            (current, jti),
        )
        return affected == 1

    def release(self, jti: str, **_evidence) -> bool:
        """退回预占，让同一张票据可以重试。**已消费的记录退不掉**（`WHERE` 挡住）。

        V3.3 §六 的关键一步：业务没做成时票据会被退回——毕竟危险动作没有发生，
        把票据烧掉只是让用户多点一次。
        V4 §三 之后还有一条更强的路径：整个 `/confirm` 在一个事务里，
        中途失败由**回滚**把预占一起撤掉（不需要显式 release）。
        这里保留它，给「事务之外」的调用方与测试用。
        """
        if not jti:
            return False
        return (
            self._db.execute(
                "DELETE FROM consumed_confirmation WHERE jti = ? AND state = 'reserved'", (jti,)
            )
            == 1
        )

    # ---- 查询与维护 ----

    def _reserved_or_consumed(self, jti: str) -> bool:
        row = self._db.query_one(
            "SELECT 1 FROM consumed_confirmation WHERE jti = ?", (jti,)
        )
        return row is not None

    def is_consumed(self, jti: str) -> bool:
        """只读查询：这张票据**已经用掉了**吗（预占中的不算）。

        预占中的票据还没有产生任何业务效果，所以它在审计上不该显示成「已使用」——
        要不要区分这两者，正是 V3.3 §六 那个问题的核心。
        """
        row = self._db.query_one(
            "SELECT 1 FROM consumed_confirmation WHERE jti = ? AND state = 'consumed'", (jti,)
        )
        return row is not None

    def is_reserved(self, jti: str) -> bool:
        """只读查询：这张票据正在被某次请求处理（预占中）。"""
        row = self._db.query_one(
            "SELECT 1 FROM consumed_confirmation WHERE jti = ? AND state = 'reserved'", (jti,)
        )
        return row is not None

    def prune_expired(self, *, now: float | None = None) -> int:
        """删掉已过期的记录，返回删除条数。

        为什么可以删：过了 `expires_at` 的令牌在签名校验那一关就已经失效
        （`_verify_and_parse` 会先判过期），留着记录不再提供任何保护，只是占空间。
        """
        current = time.time() if now is None else now
        return self._db.execute(
            "DELETE FROM consumed_confirmation WHERE expires_at < ?", (current,)
        )

    def _prune_quietly(self, *, now: float) -> None:
        try:
            self.prune_expired(now=now)
        except sqlite3.Error as exc:  # pragma: no cover - 维护动作
            logger.warning("清理过期确认记录失败（不影响判重）：%s", exc)

    def record(self, jti: str) -> dict | None:
        """取一条记录（排查「这张票据被谁在什么时候用掉了 / 正被谁占着」）。"""
        row = self._db.query_one(
            "SELECT * FROM consumed_confirmation WHERE jti = ?", (jti,)
        )
        return dict(row) if row is not None else None

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM consumed_confirmation")
        return int(row["n"])

    def clear(self) -> None:
        """清空全部记录。**只给测试用**——生产上清空等于让用过的票据复活。"""
        self._db.execute("DELETE FROM consumed_confirmation")

    def close(self) -> None:
        """只关自己开的连接；共享 `Database` 时不动它（别的 store 还在用）。"""
        if self._owns_database:
            self._db.close()
