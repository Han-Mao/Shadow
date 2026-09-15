"""LeaseStore（V3 M3）：跨进程的任务租约，保证「一个任务同一时刻最多一个执行者」。

为什么必须用 SQLite 而不是继续用 JSON 文件 + `threading.RLock`：

    v2.9 的 P0 明确指出：`JsonStore` 的锁只保护**当前 Python 进程内的线程**，
    不跨进程。两个进程（uvicorn worker 1/2、Docker replica A/B）看到同一份
    TaskStore，都能 `list_active()` 发现同一个 RUNNING 任务、都恢复它 → 双执行。

    `threading.RLock` 锁不住跨进程；`revision` CAS（`JsonStore.update_atomic`）同样
    只在进程内原子。而 SQLite 的 `INSERT ... ON CONFLICT ... DO UPDATE ... WHERE`
    是数据库事务里的原子原语，天然跨进程。

为什么只用**独立一个 `lease.db`**、不把整个存储层换成 SQLite：

    这是 M3 刻意收敛的范围。TaskLease 真正需要的只是一个「跨进程原子 claim」，
    一个独立的 SQLite 文件就够；Task / Checkpoint / Trajectory / Event 的 JSON
    存储**保持不变**（单进程内仍闭环）。这样回归面最小——只新增 Lease 这一层，
    而 SQLite 作为「跨进程原子原语」已经落地，将来要整体换存储时这里就是底子。

`sqlite3` 是 Python 标准库，零额外依赖。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# 租约默认时长：worker 心跳间隔必须显著小于 TTL，否则正常的 GC 停顿都会被误判成死亡
DEFAULT_LEASE_TTL_SECONDS = 30.0
DEFAULT_HEARTBEAT_SECONDS = 5.0


class Lease:
    """一条租约的当前状态。"""

    def __init__(
        self,
        task_id: str,
        worker_id: str,
        token: str,
        expires_at: float,
        heartbeat_at: float,
    ) -> None:
        self.task_id = task_id
        self.worker_id = worker_id
        self.token = token
        self.expires_at = expires_at
        self.heartbeat_at = heartbeat_at

    @property
    def expired(self, now: float | None = None) -> bool:
        return self.expires_at <= (now if now is not None else time.time())


class LeaseStore:
    """基于 SQLite 的租约表：`claim` / `heartbeat` / `release` / `peek`。

    表结构：

        CREATE TABLE lease (
            task_id      TEXT PRIMARY KEY,
            worker_id    TEXT NOT NULL,
            token        TEXT NOT NULL,
            acquired_at  REAL NOT NULL,
            expires_at   REAL NOT NULL,
            heartbeat_at REAL NOT NULL
        )

    并发语义（关键）：
    - `claim` 用 `INSERT ... ON CONFLICT(task_id) DO UPDATE SET ... WHERE expires_at < ?`。
      这个「已存在且未过期 → 不动；不存在或已过期 → 抢到」的判断在**单条 SQL 事务**
      里完成，SQLite 的写锁保证同一时刻只有一个进程能通过。
    - `heartbeat` 用 `UPDATE ... WHERE task_id=? AND token=?`：token 不匹配说明租约
      已被别人接管（我们的租约过期了），续租失败 → 调用方必须停止执行。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lease (
                task_id      TEXT PRIMARY KEY,
                worker_id    TEXT NOT NULL,
                token        TEXT NOT NULL,
                acquired_at  REAL NOT NULL,
                expires_at   REAL NOT NULL,
                heartbeat_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    @staticmethod
    def _worker_id() -> str:
        """本进程的身份：host + pid + 启动随机串（同 host 多实例也能区分）。"""
        host = os.getenv("HOSTNAME", "unknown")
        return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

    def claim(
        self,
        task_id: str,
        *,
        worker_id: str | None = None,
        ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> Lease | None:
        """原子地抢一条租约。成功返回 Lease，失败（别人持有且未过期）返回 None。

        返回值 `None` 表示**不能执行**：要么是另一个 worker 正在跑它，要么是它的
        租约还没过期（原 worker 可能只是心跳慢了）。调用方应把任务放回队列等重试，
        而不是硬抢——这正是「一个任务最多一个执行者」的兜底。
        """
        worker = worker_id or self._worker_id()
        token = uuid.uuid4().hex
        now = time.time()
        expires = now + ttl_seconds
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO lease (task_id, worker_id, token, acquired_at, expires_at, heartbeat_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    worker_id = excluded.worker_id,
                    token = excluded.token,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at,
                    heartbeat_at = excluded.heartbeat_at
                WHERE lease.expires_at <= excluded.acquired_at
                """,
                (task_id, worker, token, now, expires, now),
            )
            self._conn.commit()
            # 影响行数 == 1 表示「插入成功」或「接管了过期租约」；== 0 表示
            # 别人持有且未过期，冲突被 WHERE 挡住。
            if cursor.rowcount == 0:
                return None
            return Lease(task_id, worker, token, expires, now)

    def heartbeat(self, task_id: str, token: str, *, ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS) -> bool:
        """续租。返回 False 表示租约已不属于我们（被接管/已释放），必须停止执行。"""
        now = time.time()
        expires = now + ttl_seconds
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE lease SET heartbeat_at = ?, expires_at = ?
                WHERE task_id = ? AND token = ?
                """,
                (now, expires, task_id, token),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def release(self, task_id: str, token: str) -> bool:
        """释放租约。只有持有者（token 匹配）才能释放。返回是否真的删掉了。"""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM lease WHERE task_id = ? AND token = ?", (task_id, token)
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def peek(self, task_id: str) -> Lease | None:
        """只读地看一条租约现状（不抢、不续）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lease WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return Lease(
            row["task_id"],
            row["worker_id"],
            row["token"],
            row["expires_at"],
            row["heartbeat_at"],
        )

    def expired(self, task_id: str, now: float | None = None) -> bool:
        """这条租约是否已过期（可用于判断「别人家的 worker 是不是死了」）。"""
        lease = self.peek(task_id)
        if lease is None:
            return True
        return lease.expires_at <= (now if now is not None else time.time())

    def close(self) -> None:
        with self._lock:
            self._conn.close()
