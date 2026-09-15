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

# 任务与恢复点（V4 §二 —— 审核「unify task/checkpoint/event storage」那一步）。
#
# 为什么把它们从「一个实体一个 JSON 文件」搬进表里：
# - **跨文件一致性**：Checkpoint 与 Task 指针此前是两个文件两次写入，中间崩溃就留下
#   孤儿恢复点（靠启动清理事后补救）。同一个库里它们可以在**一个事务**里提交（V4 §三）。
# - **查询**：`status` / `created_at` 是真列，于是「现在有多少 running」「谁最新」不必
#   解析每一份 JSON。
# - **CAS 更直接**：原来靠 `JsonStore.update_atomic` 把「读-比较-递增-写」塞进同一把锁；
#   现在是 `BEGIN IMMEDIATE` 里读一次 revision 再写，锁由数据库给（跨进程也成立）。
#
# `payload` 是权威内容，`status` / `created_at` / `revision` 是**派生列**（与 events 的
# 关联列同一个口径）：它们只服务于查询与 CAS，读出来仍然以 payload 为准。
_TASKS_V2 = [
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id    TEXT PRIMARY KEY,
        revision   INTEGER NOT NULL,
        status     TEXT NOT NULL,
        created_at TEXT NOT NULL,
        payload    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at)",
    """
    CREATE TABLE IF NOT EXISTS checkpoints (
        task_id       TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL,
        created_at    TEXT NOT NULL DEFAULT '',
        payload       TEXT NOT NULL,
        PRIMARY KEY (task_id, checkpoint_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id, checkpoint_id)",
]

# 确认票据的消费记录（V4 §三 —— 审核「confirmation 必须进入同一事务」那一步）。
#
# V3.2 起它已经在 SQLite 上（`jti` 主键 + 原子 INSERT），但放在**另一个文件**里
# （`confirmations.db`）。审核 §三 要的是：「校验票据 → 改 Task 状态 → 写事件 → 作废票据」
# 在一个事务里。跨文件没有事务，所以表搬进主库——于是 §三 才有可能成立。
#
# 表结构沿用 V3.3 §六 的两阶段（`state` = reserved | consumed），
# 因为那套语义（预占 → 提交 / 退回）正是 §三 事务里要用的东西。
_CONFIRMATIONS_V3 = [
    """
    CREATE TABLE IF NOT EXISTS consumed_confirmation (
        jti         TEXT PRIMARY KEY,
        task_id     TEXT NOT NULL DEFAULT '',
        principal   TEXT NOT NULL DEFAULT '',
        fingerprint TEXT NOT NULL DEFAULT '',
        consumed_at REAL NOT NULL DEFAULT 0,
        expires_at  REAL NOT NULL DEFAULT 0,
        state       TEXT NOT NULL DEFAULT 'consumed',
        reserved_at REAL NOT NULL DEFAULT 0
    )
    """,
    # 索引**故意不在这里**建：`SHADOW_CONFIRM_DB` 指向一个 V3.2 时期的老表（6 列、没有
    # `state`）时，`CREATE TABLE IF NOT EXISTS` 是空操作，而这条索引会因缺列直接报错——
    # 那会把「升级老部署」变成「起不来」。索引改由
    # `ConfirmationConsumptionStore._ensure_columns()` 在补齐列之后创建（它是自愈的）。
]

# 动作执行记录（v4.1 §一 —— 审核那十阶段计划的第 1 步）。
#
# 到 V4 为止，任务 / 恢复点 / 事件 / 确认票据 / 租约都在这个库里，**只有执行记录还是
# 一执行一个 JSON 文件**——它是最后一个异类。搬进来的理由不是「整齐」，是三条具体的：
#
# - **状态迁移要受保护**：一条执行「还能不能派发」只能由一条 `UPDATE ... WHERE status=?`
#   来回答（见 `ExecutionStore.transition`）。这正是 §十 测试4 要的「同一个 execution_id
#   不能 tap 两次」——文件存储做不到这件事，因为「读-判断-写」之间有窗口。
# - **恢复要能扫**：进程被杀之后要能一句话问出「现在有哪些执行不是终态」。
#   一执行一文件时那是一次目录遍历 + 逐个反序列化，`status` 也不是可查询的列。
# - **要与事件同库**：§八 要「一条执行 → 它的完整事件轨迹」，跨文件没有事务，
#   而 `events.execution_id` 已经是那一链的入口（迁移 v1 就建好了）。
#
# 列的口径与 `events` / `tasks` 一致：**权威内容在 payload 列**（`action_payload` /
# `result`），`status` / `risk_level` / `action_type` 是**派生列**，只服务于查询与
# 受保护迁移，读出来仍然以 payload 为准。文本列一律 `NOT NULL DEFAULT ''` 而不是
# 允许 NULL：读的一侧就少一个 `or ""`，而 NULL 与「确实没有」在本表里没有区别。
#
# 一条**刻意没做**的事：不建 `execution_attempts` 表。审核建议的
# `UNIQUE(execution_id, action_attempt)` 想拦的是「同一条执行被派发两次」，
# 而 `status` 本身就是那个唯一约束——一条执行只允许 `DISPATCHED → RUNNING` 迁一次，
# 第二次派发的 `UPDATE` 会 miss（rowcount=0）而被拒。多一张表只会多一份可能与状态
# 不一致的事实。它**拦不住**的是「同一意图被执行两次」（两个不同的 execution_id），
# 那属于上层职责（`reconciliation` / `is_safe_to_retry`），不在本表范围内。
_EXECUTIONS_V4 = [
    """
    CREATE TABLE IF NOT EXISTS executions (
        execution_id   TEXT PRIMARY KEY,
        task_id        TEXT NOT NULL DEFAULT '',
        principal      TEXT NOT NULL DEFAULT '',
        device_id      TEXT NOT NULL DEFAULT '',
        channel        TEXT NOT NULL DEFAULT 'manual',
        action_type    TEXT NOT NULL DEFAULT '',
        action_payload TEXT NOT NULL DEFAULT '{}',
        risk_level     TEXT NOT NULL DEFAULT '',
        status         TEXT NOT NULL,
        request_id     TEXT NOT NULL DEFAULT '',
        created_at     TEXT NOT NULL,
        started_at     TEXT NOT NULL DEFAULT '',
        finished_at    TEXT NOT NULL DEFAULT '',
        result         TEXT NOT NULL DEFAULT '{}',
        note           TEXT NOT NULL DEFAULT '',
        revision       INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_execution_task ON executions(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_execution_principal ON executions(principal)",
    "CREATE INDEX IF NOT EXISTS idx_execution_device ON executions(device_id)",
    # 恢复扫描的入口（§六）：`WHERE status NOT IN (终态集合)`。
    # 这张表是**新建**的，所以索引可以直接写在迁移里——不会出现 v4 §三 那种
    # 「老表缺列 → CREATE INDEX 报 no such column → 升级变起不来」。
    "CREATE INDEX IF NOT EXISTS idx_execution_status ON executions(status)",
]

MIGRATIONS = (
    (1, tuple(_EVENTS_V1)),
    (2, tuple(_TASKS_V2)),
    (3, tuple(_CONFIRMATIONS_V3)),
    (4, tuple(_EXECUTIONS_V4)),
)


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
