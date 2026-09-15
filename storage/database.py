"""SQLite 连接、PRAGMA 与事务（V4 §二/§四）。

为什么把「连接 + PRAGMA + 事务」单独抽一层：审核那份十阶段计划的核心判断是
**存储要收敛到一个数据库上**（tasks / checkpoints / events / confirmations / leases）。
如果每个 store 自己 `sqlite3.connect` 一遍，就会各自配一套 PRAGMA、各自一套迁移版本——
那又回到「六个存储六套事实」。这个模块是那件事的地基：本轮先让 `events` 用上它，
其余 store 按同一套模式迁。

三条 PRAGMA 都不是随手加的：

- `journal_mode=WAL`：读写并发（一个进程在写、另一个在查）不必互相阻塞。
- `synchronous=FULL`：**commit 返回即落盘**。这正是审核 §四 要的那条保证——
  它与 JSONL 那版的 `fsync` 是同一个语义，但由数据库保证，而且天然覆盖
  「一个事务里的多条写入」，不再需要我们自己记住哪条路径该 fsync。
- `busy_timeout`：多进程同时写时**等待**，而不是立刻抛 `database is locked`
  （默认 0 秒就是不等待）。等待时长给 5 秒：正常写入是毫秒级的，等不到就说明有长事务。

事务语义：`transaction()` 用 `BEGIN IMMEDIATE` 而不是默认的延迟 BEGIN。
原因很实际——延迟事务在「读之后才升级为写」时可能拿到 `SQLITE_BUSY` 且**无法重试**
（快照已经过期）。既然我们的写几乎都是「先读最大值再写」，一开始就拿写锁更省事。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# 连接级的 PRAGMA。放在这里而不是散在各 store：它们是**整个数据库**的属性，
# 不是某个表的属性。
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=FULL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)


def connect(path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）一个数据库连接，并检查表结构是否已迁移。

    连接是长生命周期的：本仓库的 store 都是进程内长期持有的对象，
    每个请求开一次连接会把 WAL 的好处抵消掉大半。
    `check_same_thread=False` 是必须的——后台 worker 与 API 线程会共用它，
    真正的并发保护由 `transaction()`（写）与 SQLite 自身（读）提供。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)

    # 延迟导入：避免 migrations 反过来 import 本模块时形成环
    from .migrations import apply_migrations

    apply_migrations(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """一个写事务：要么全成功，要么全回滚。

    用 `BEGIN IMMEDIATE`（见模块 docstring）。异常一律回滚后原样抛出——
    调用方（各 store）负责把异常翻译成自己的领域异常（如 `PersistenceError`）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
