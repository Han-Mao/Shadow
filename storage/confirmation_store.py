"""ConfirmationConsumptionStore（V3.2 §二）：确认令牌的「已消费」记录必须跨重启存活。

审核指出的是一个**真实的语义缺口**，而且代码注释自己就承认了：

    _consumed_confirmations: dict[str, int] = {}      # 进程内存

而确认令牌的签名密钥在配置了 `SHADOW_API_TOKEN` / `SHADOW_CONFIRM_SECRET` 时是**稳定**的。
两者相加的后果是「一次性」只在进程生命周期内成立：

    09:00  签发令牌 A
    09:01  用它确认了一个危险动作
    09:02  服务重启 —— 记录清空，但签名密钥没变
    09:03  令牌 A 仍在 TTL 内，签名依然有效 → 它又「可用」了

所以现状是「**进程生命周期内**一次性」，不是「一次性」。这里把它搬到 SQLite：
`jti` 做主键，消费 = 一条 INSERT，唯一约束由数据库保证——天然是**跨进程 + 跨重启**
的原子 consume。不需要额外加锁，也不可能出现「两个进程同时通过同一个检查」
（这正是审核要求的 `consume = CAS / unique constraint`）。

为什么用独立的 `confirmations.db` 而不是复用 `lease.db`：
两者的运维语义是**相反**的。租约在「想让任务重新排队」时**可以**整体清空；
而已消费的审批票据**绝不能**清空——那等于把用过的票据又变成可用的。
放在同一个文件里，迟早有人顺手 `DELETE FROM` 全部。分开更安全。

`sqlite3` 是标准库，零额外依赖（与 V3 M3 的 `lease_store` 同一取舍）。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class ConfirmationConsumptionStore:
    """基于 SQLite 的确认令牌消费表。

    表结构（审核建议的字段都在，方便事后回答「这张票据是谁在用、用在哪）：

        CREATE TABLE consumed_confirmation (
            jti         TEXT PRIMARY KEY,
            task_id     TEXT NOT NULL,
            principal   TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            consumed_at REAL NOT NULL,
            expires_at  REAL NOT NULL
        )

    只有 `jti` 是主键、其余都是证据字段：**判重只靠主键**，任何字段缺失都不影响
    「同一张票据不能用第二次」这条语义。
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
                expires_at  REAL NOT NULL
            )
            """
        )
        self._conn.commit()

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
        """原子消费这个 jti。返回 False 表示**之前已经被消费过**。

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
                        (jti, task_id, principal, fingerprint, consumed_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (jti, task_id, principal, fingerprint, current, expires_at),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                return False
            # 顺手清掉过期记录，避免这张表无限增长。放在消费之后：先保证「判重」，
            # 清理只是维护动作，失败也不该让消费失败。
            try:
                self.prune_expired(now=current)
            except sqlite3.Error as exc:  # pragma: no cover - 维护动作
                logger.warning("清理过期确认记录失败（不影响判重）：%s", exc)
            return True

    def is_consumed(self, jti: str) -> bool:
        """只读查询：这张票据消费过吗（供审计/排查用，不消费）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM consumed_confirmation WHERE jti = ?", (jti,)
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

    def record(self, jti: str) -> dict | None:
        """取一条记录（排查「这张票据被谁在什么时候用掉了」）。"""
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
