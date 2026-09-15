"""ConfirmationConsumptionStore（V3.2 §二 · V3.3 §六）：确认令牌的消费记录。

审核指出的是一个**真实的语义缺口**，而且代码注释自己就承认了：

    _consumed_confirmations: dict[str, int] = {}      # 进程内存

而确认令牌的签名密钥在配置了 `SHADOW_API_TOKEN` / `SHADOW_CONFIRM_SECRET` 时是**稳定**的。
两者相加的后果是「一次性」只在进程生命周期内成立：

    09:00  签发令牌 A
    09:01  用它确认了一个危险动作
    09:02  服务重启 —— 记录清空，但签名密钥没变
    09:03  令牌 A 仍在 TTL 内，签名依然有效 → 它又「可用」了

所以现状是「**进程生命周期内**一次性」，不是「一次性」。这里把它搬到 SQLite：
`jti` 做主键，判重 = 一条 INSERT，唯一约束由数据库保证——天然是**跨进程 + 跨重启**
的原子判定。不需要额外加锁，也不可能出现「两个进程同时通过同一个检查」。

## V3.3 §六：两步语义 —— 预占（reserve）与消费（commit）

V3.2 的实现只有一步 `consume`，而调用方（`POST /confirm`）的顺序是：

    消费令牌 → 改业务状态（resolve_confirmation）

于是出现一个**不好但不危险**的窗口：票据已经作废，可业务状态没改成（并发改写、
任务已结束……）→ 用户看到 409，还必须重新申请一张票据。审核原话是
「这不是安全漏洞，反而是 fail-safe，但用户体验会比较糟」。

现在拆成三步，把「不可逆」推迟到最后：

    reserve(jti)            # 预占：还没消费，只是先占住这个 jti（原子）
        ↓
    改业务状态               # 真正的副作用在这里发生
        ↓
    commit(jti)             # 一旦成功，这张票据就永久作废
    （业务失败则 release(jti) 退回预占，用户可以拿同一张票据重试）

为什么这样仍然安全：
- **判重语义没变弱**：`reserve` 与 `consume` 一样是主键 + 单条 SQL，两个并发请求
  不可能同时预占成功；`commit` 之后的行是 `consumed`，任何 `reserve` 都会失败。
- **`release` 只能退「预占」**，退不掉已消费的记录（`WHERE state='reserved'`）。
- **卡死的预占会自动过期**：进程在 `reserve` 之后崩溃会留下一个 `reserved` 行，
  超过 `RESERVE_TTL_SECONDS` 就允许被**同一个 jti** 重新预占（否则这张票据就永久
  卡住了）。这个 TTL 只影响「崩溃后重试要等多久」，不影响「用过就不能再用」。

为什么用独立的 `confirmations.db` 而不是复用 `lease.db`：
两者的运维语义是**相反**的。租约在「想让任务重新排队」时**可以**整体清空；
而已消费的审批票据**绝不能**清空——那等于把用过的票据又变成可用的。
放在同一个文件里，迟早有人顺手 `DELETE FROM` 全部。分开更安全。

`sqlite3` 是标准库，零额外依赖（与 V3 M3 的 `lease_store` 同一取舍）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 预占的有效期（秒）。崩溃后留下的 `reserved` 行超过它就允许被重新预占——
# 它**不是**令牌 TTL（那是签名里的 expires_at），只决定「崩溃后多久能重试同一张票据」。
# 60 秒足够覆盖一次确认请求的正常耗时（改状态是本地操作），又短到不至于让人等。
RESERVE_TTL_SECONDS = float(os.getenv("SHADOW_CONFIRM_RESERVE_TTL_SECONDS", "60"))


class ConfirmationConsumptionStore:
    """基于 SQLite 的确认令牌消费表。

    表结构（审核建议的字段都在，方便事后回答「这张票据是谁在用、用在哪」）：

        CREATE TABLE consumed_confirmation (
            jti         TEXT PRIMARY KEY,
            task_id     TEXT NOT NULL,
            principal   TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            consumed_at REAL NOT NULL,
            expires_at  REAL NOT NULL,
            state       TEXT NOT NULL DEFAULT 'consumed',   -- consumed | reserved
            reserved_at REAL NOT NULL DEFAULT 0
        )

    只有 `jti` 是主键、其余都是证据字段：**判重只靠主键**，任何字段缺失都不影响
    「同一张票据不能用第二次」这条语义。

    `state` / `reserved_at` 是 V3.3 §六 加的（两阶段：先预占、后消费）。
    老库（V3.2 建的、只有 6 列）在 `__init__` 里自动补列——历史行都是已消费的，
    所以默认值取 `'consumed'` 正是它们真实的状态。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS consumed_confirmation (
                jti         TEXT PRIMARY KEY,
                task_id     TEXT NOT NULL,
                principal   TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                consumed_at REAL NOT NULL,
                expires_at  REAL NOT NULL,
                state       TEXT NOT NULL DEFAULT 'consumed',
                reserved_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        self._migrate_legacy_columns()
        self._conn.commit()

    def _migrate_legacy_columns(self) -> None:
        """给 V3.2 建的老表补上 `state` / `reserved_at`。

        为什么值得写这段：直接 `ALTER TABLE ADD COLUMN` 只有在列不存在时才成功，
        所以先查 `PRAGMA table_info`；不查的话第二次启动就会抛 `duplicate column`。
        历史行没有 state 字段，而它们全部是**已消费**的（V3.2 只有那一种语义），
        因此默认值 `'consumed'` 不是「随便填的」。
        """
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(consumed_confirmation)")}
        if "state" not in columns:
            self._conn.execute(
                "ALTER TABLE consumed_confirmation ADD COLUMN state TEXT NOT NULL DEFAULT 'consumed'"
            )
        if "reserved_at" not in columns:
            self._conn.execute(
                "ALTER TABLE consumed_confirmation ADD COLUMN reserved_at REAL NOT NULL DEFAULT 0"
            )

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

        实现就是一条 INSERT：主键冲突由 SQLite 抛出 `IntegrityError`，我们把它翻译成
        「已消费」。这就是跨进程的 CAS——不需要先 SELECT 再判断，那两步之间永远有窗口。
        """
        if not jti:
            return False
        current = time.time() if now is None else now
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO consumed_confirmation
                        (jti, task_id, principal, fingerprint, consumed_at, expires_at, state, reserved_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'consumed', ?)
                    """,
                    (jti, task_id, principal, fingerprint, current, expires_at, current),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                return False
            # 顺手清掉过期记录，避免这张表无限增长。放在消费之后：先保证「判重」，
            # 清理只是维护动作，失败也不该让消费失败。
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
        - 已有 `consumed` 行 → `WHERE` 不成立，更新被跳过 → 返回 False（用过就是用过）；
        - 已有**新鲜的** `reserved` 行 → 同样跳过 → 返回 False（另一个请求正在处理它）；
        - 已有**过期**的 `reserved` 行 → 接管（崩在中间的预占不该把票据永久卡死）。

        `cursor.rowcount` 是这里的判据：0 表示那条 `WHERE` 挡住了更新，也就是「没占上」。
        """
        if not jti:
            return False
        current = time.time() if now is None else now
        stale_before = current - RESERVE_TTL_SECONDS
        with self._lock:
            cursor = self._conn.execute(
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
            self._conn.commit()
            reserved = cursor.rowcount == 1
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
        那一刻已经记在这行里了，这里不需要重写。`**_evidence` 同理（证据字段已在
        reserve 时落库）。
        """
        if not jti:
            return False
        current = time.time() if now is None else now
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE consumed_confirmation
                   SET state = 'consumed', consumed_at = ?
                 WHERE jti = ? AND state = 'reserved'
                """,
                (current, jti),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def release(self, jti: str, **_evidence) -> bool:
        """退回预占，让同一张票据可以重试。**已消费的记录退不掉**（`WHERE` 挡住）。

        这是 V3.3 §六 的关键一步：审核指出「票据已作废但业务没做成」时用户必须重新
        申请票据。有了它，那种情况下票据会被退回——毕竟**危险动作没有发生**，
        把票据烧掉只是让用户多点一次。
        """
        if not jti:
            return False
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM consumed_confirmation WHERE jti = ? AND state = 'reserved'",
                (jti,),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    # ---- 查询与维护 ----

    def is_consumed(self, jti: str) -> bool:
        """只读查询：这张票据**已经用掉了**吗（预占中的不算）。

        预占中的票据还没有产生任何业务效果，所以它在审计上不该显示成「已使用」——
        要不要区分这两者，正是 V3.3 §六 那个问题的核心。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM consumed_confirmation WHERE jti = ? AND state = 'consumed'",
                (jti,),
            ).fetchone()
        return row is not None

    def is_reserved(self, jti: str) -> bool:
        """只读查询：这张票据正在被某次请求处理（预占中）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM consumed_confirmation WHERE jti = ? AND state = 'reserved'",
                (jti,),
            ).fetchone()
        return row is not None

    def prune_expired(self, *, now: float | None = None) -> int:
        """删掉已过期的记录，返回删除条数。

        为什么可以删：过了 `expires_at` 的令牌在签名校验那一关就已经失效
        （`_verify_and_parse` 会先判过期），留着记录不再提供任何保护，只是占空间。
        """
        current = time.time() if now is None else now
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM consumed_confirmation WHERE expires_at < ?", (current,)
            )
            self._conn.commit()
            return cursor.rowcount

    def _prune_quietly(self, *, now: float) -> None:
        try:
            self.prune_expired(now=now)
        except sqlite3.Error as exc:  # pragma: no cover - 维护动作
            logger.warning("清理过期确认记录失败（不影响判重）：%s", exc)

    def record(self, jti: str) -> dict | None:
        """取一条记录（排查「这张票据被谁在什么时候用掉了 / 正被谁占着」）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM consumed_confirmation WHERE jti = ?", (jti,)
            ).fetchone()
        return dict(row) if row is not None else None

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM consumed_confirmation"
            ).fetchone()
        return int(row["n"])

    def clear(self) -> None:
        """清空全部记录。**只给测试用**——生产上清空等于让用过的票据复活。"""
        with self._lock:
            self._conn.execute("DELETE FROM consumed_confirmation")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
