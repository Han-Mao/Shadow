"""版本化迁移（V4 §二）。

审核要求 `storage/migrations/` 这个目录，理由是「存储要能演进」：现在
`confirmations` 表加一列（V3.3 §六）靠的是 `PRAGMA table_info` 里手工判断，
一次两次还行，十次之后就没人说得清线上库到底是什么形状。

这里用 SQLite 自带的 `PRAGMA user_version` 做版本号——不引入额外表，
也不会和业务表混在一起。规矩：

- 每个迁移是 `(版本号, SQL 列表)`，**只增不改**：已经发出去的版本号内容不能再动，
  否则新旧库的形状会分叉。
- `apply_migrations()` 是幂等的：已经应用过的直接跳过（靠 user_version 比大小）。
"""
from __future__ import annotations

import sqlite3

# 事件表（V4 §四）。字段与审核给的建表语句一致，另外多了两条索引与一个唯一约束：
#
#   UNIQUE(task_id, sequence) —— 一个任务的事件天然有序号 1、2、3…
#   而不是靠 `datetime.now()` 去猜先后。多进程同时写时，唯一约束是最后一道保险：
#   序号算重了会被数据库直接拒掉，不会静默写出两条同样的序号。
#
#   execution_id 上的索引 —— V4 §五 要「一次执行串起完整链条」，
#   按 execution_id 查事件是那条链的入口。
_EVENTS_V1 = [
    """
    CREATE TABLE IF NOT EXISTS events (
        event_id     TEXT PRIMARY KEY,
        task_id      TEXT NOT NULL,
        execution_id TEXT NOT NULL DEFAULT '',
        principal    TEXT NOT NULL DEFAULT '',
        device_id    TEXT NOT NULL DEFAULT '',
        kind         TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        sequence     INTEGER NOT NULL,
        payload      TEXT NOT NULL,
        UNIQUE (task_id, sequence)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, sequence)",
    "CREATE INDEX IF NOT EXISTS idx_events_execution ON events(execution_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)",
]

MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = ((1, tuple(_EVENTS_V1)),)


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """把未应用的迁移依次执行，返回本次应用的版本号列表。

    每个版本包在一个事务里：迁移中途失败就整体回滚，不会留下半张表。
    （`executescript` 会隐式提交，所以这里手写 BEGIN/COMMIT 而不是用它。）
    """
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    applied: list[int] = []

    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                conn.execute(statement)
            # PRAGMA 不支持参数绑定，版本号来自本模块的常量，不是外部输入
            conn.execute(f"PRAGMA user_version={int(version)}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        applied.append(version)

    return applied
