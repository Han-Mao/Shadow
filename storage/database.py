"""SQLite 连接、PRAGMA 与事务（V4 §二/§三）。

为什么把「连接 + PRAGMA + 事务」单独抽一层：审核那份十阶段计划的核心判断是
**存储要收敛到一个数据库上**（tasks / checkpoints / events / confirmations / leases）。
如果每个 store 自己 `sqlite3.connect` 一遍，就会各自配一套 PRAGMA、各自一套迁移版本——
那又回到「六个存储六套事实」。这个模块是那件事的地基。

三条 PRAGMA 都不是随手加的：

- `journal_mode=WAL`：读写并发（一个进程在写、另一个在查）不必互相阻塞。
- `synchronous=FULL`：**commit 返回即落盘**。这正是审核 §四 要的那条保证——
  它与 JSONL 那版的 `fsync` 是同一个语义，但由数据库保证，而且天然覆盖
  「一个事务里的多条写入」，不再需要我们自己记住哪条路径该 fsync。
- `busy_timeout`：多进程同时写时**等待**，而不是立刻抛 `database is locked`
  （默认 0 秒就是不等待）。5 秒足够：正常写入是毫秒级的，等不到说明有长事务。

## 为什么要有 `Database` 对象

V4 §三 要求「确认票据 + Task 状态 + 事件」在**同一个事务**里提交。三个 store 各自
connect 自己的文件时，这在物理上就做不到。所以连接由 `Database` 持有，store 接受
`Database | 路径`：多个 store 拿到同一个 `Database` 就共享同一个连接与事务。

## 并发模型（说清就够，不需要更复杂的东西）

- **本进程内**：一把可重入锁守卫**所有**访问（读也包括）。为什么读也要进锁——
  共享一个连接时，A 线程处在事务中间，B 线程的读会看到**未提交**的数据。
  把读也纳入同一把锁，就不会出现「读到半个事务」。
- **跨进程**：SQLite 自己管（`BEGIN IMMEDIATE` + `busy_timeout` + `synchronous=FULL`）。
- **事务可重入**：`transaction()` 用深度计数，内层**加入**外层而不是再 `BEGIN`
  （嵌套 BEGIN 会直接报错）。这正是 §三 需要的：`/confirm` 开一个事务，
  里面调的 `TaskStore.save()` 自然成为它的一部分。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

# 连接级的 PRAGMA。放在这里而不是散在各 store：它们是**整个数据库**的属性，
# 不是某个表的属性。
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=FULL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)

# 所有表都放在这一个文件名下（V4 §二）。传目录时用它；传 `.db` 文件时用那个文件——
# 于是「指向同一个目录的多个 store」天然共享一个库，不必让每个调用点记住传对路径。
DB_FILENAME = "shadow.db"


def resolve_db_path(target: "str | Path") -> Path:
    """把「目录或 .db 路径」统一成一个数据库文件路径。

    - 传目录（历史用法，如 `artifacts/state`）→ `<目录>/shadow.db`；
    - 传 `*.db` → 就用它。

    这样 `TaskStore(STORAGE_DIR)` / `EventLog(STORAGE_DIR)` / `CheckpointStore(STORAGE_DIR)`
    会落到**同一个文件**上——V4 §三 的「同一事务」才有可能。
    """
    path = Path(target)
    if path.suffix == ".db":
        return path
    return path / DB_FILENAME


class Database:
    """一个数据库文件的连接 + 迁移 + 事务。"""

    def __init__(self, target: "str | Path") -> None:
        self.path = resolve_db_path(target)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        for pragma in _PRAGMAS:
            self.conn.execute(pragma)

        # 延迟导入：避免 migrations 反过来 import 本模块时形成环
        from .migrations import apply_migrations

        self.applied = apply_migrations(self.conn)

    # ---- 路径约定 ----

    def legacy_dir(self, name: str) -> Path:
        """旧版「一个 store 一个目录」时的目录（V4 迁移时按它导入旧数据）。

        V4 之前是 `artifacts/state/tasks/*.json`、`artifacts/state/events/*.jsonl`。
        两种传法都必须能用，否则升级时导不到数据：

        - 传**存储根目录**（`artifacts/state`，新代码的推荐写法）→ `<库所在目录>/tasks`；
        - 传**该 store 自己的目录**（`artifacts/state/tasks`，几十处历史调用点）→
          那个目录本身就是旧数据的家（此时库落在 `.../tasks/shadow.db`）。

        少了第二条，`TaskStore(STORAGE_DIR / "tasks")` 会去找
        `.../tasks/tasks/*.json`——静默导不到任何东西，而这正是迁移最怕的失败方式。
        """
        parent = self.path.parent
        if parent.name == name:
            return parent
        return parent / name

    # ---- 访问原语 ----

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行一条写语句，返回受影响行数（`rowcount`）。"""
        with self._lock:
            return self.conn.execute(sql, tuple(params)).rowcount

    @contextmanager
    def transaction(self) -> Iterator["Database"]:
        """一个写事务：要么全成功，要么全回滚（**可重入**）。

        可重入是 V4 §三 的关键：`/confirm` 要在一个事务里做完「校验票据 → 改 Task
        状态 → 写事件 → 作废票据」，而「改 Task 状态」会调到 `TaskStore.save()`——
        它自己也想开事务。深度计数让内层**加入**外层（只有最外层真正 BEGIN/COMMIT），
        既不会嵌套 BEGIN 报错，也不会出现「内层先提交、外层再失败」的半成品。
        """
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self
                finally:
                    self._depth -= 1
                return

            self.conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self
            except BaseException:
                self._depth = 0
                self.conn.execute("ROLLBACK")
                raise
            self._depth = 0
            self.conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self.conn.close()


def connect(target: "str | Path") -> Database:
    """打开（必要时创建）一个数据库：连接 + PRAGMA + 迁移都已完成。

    新代码直接用 `Database(...)`，或在多处复用**同一个** `Database` 以共享事务。
    """
    return Database(target)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """兼容早期调用点的自由函数：对**裸连接**开一个事务（不重入）。

    新代码应当用 `Database.transaction()`（可重入、与共享连接配套）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
