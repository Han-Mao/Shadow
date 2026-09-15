"""EventStore：SQLite 上的事件表（V4 §四）。

JSONL 那版（V3.3 §一/§二）解决的是**落盘可靠性**——`write → flush → fsync`，
以及「单次 write 不撕裂」。它解决不了的是审核列的那一串：

    多进程全序 · 查询 · 筛选 · 分页 · 关联 · 事务

现在这些由表结构本身提供：

    WHERE task_id = ? ORDER BY sequence      ← 每个任务的事件天然有序，不靠时间戳猜先后
    WHERE execution_id = ?                   ← V4 §五 那条链的入口
    WHERE kind = 'action_dispatched'         ← 审计按类型筛

`sequence` 是**每个任务独立**的序号（1、2、3…）。它比 `datetime.now()` 强的点很具体：
毫秒级时间戳在两个进程同时写时可能相同，而「谁先谁后」恰恰是回放与审计要回答的问题。
唯一约束 `UNIQUE(task_id, sequence)` 是最后一道保险——序号万一算重，数据库直接拒掉，
不会静默写出两条同样的序号。

落盘强度由 `database.connect()` 的 `synchronous=FULL` 给：**commit 返回即落盘**。
这与 JSONL 版 `fsync` 是同一个语义，但由数据库保证，而且天然覆盖「一个事务里的多条写入」。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import database


class EventStore:
    """事件表的窄接口：`append` / `read` / `kinds` / `read_by_execution`。

    返回的是**字典行**而不是领域对象：这一层只管持久化，
    「事件是什么」由 `storage/event_log.py` 定义（它再把行翻译成 `Event`）。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn = database.connect(self.path)
        # 进程内串行：SQLite 的连接对象不是线程安全的，而 API 线程与后台 worker 会共用它。
        # 跨进程的互斥由 BEGIN IMMEDIATE + busy_timeout 负责（见 database.py）。
        self._lock = threading.RLock()

    # ---- 写 ----

    def append(
        self,
        task_id: str,
        kind: str,
        data: dict | None = None,
        *,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> dict:
        """追加一条事件，返回落库后的行。

        序号在**同一个事务**里算：先 `MAX(sequence)+1` 再 `INSERT`，中间不会有别人插进来
        （`BEGIN IMMEDIATE` 一开始就拿写锁）。仍然保留一次 IntegrityError 重试——
        唯一约束是兜底，不是装饰；真撞上就重算一次，比抛出去让上层猜原因好。

        `event_id` / `created_at` 只在**导入历史数据**时显式传：那时要保留原来的身份与
        时间，否则搬过来的事件会看起来「全都发生在导入那一刻」。
        """
        payload = json.dumps(data or {}, ensure_ascii=False)
        event_id = event_id or uuid.uuid4().hex[:8]
        created_at = created_at or datetime.now().isoformat(timespec="milliseconds")
        # 关联列从 payload 里取：调用方不用为了「能被查到」而多传一遍参数。
        # 兼容 `device` 与 `device_id` 两种写法（历史事件用的是前者）。
        source = data or {}
        execution_id = str(source.get("execution_id") or "")
        principal = str(source.get("principal") or "")
        device_id = str(source.get("device_id") or source.get("device") or "")

        with self._lock:
            for attempt in (1, 2):
                try:
                    with database.transaction(self._conn) as conn:
                        row = conn.execute(
                            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next "
                            "FROM events WHERE task_id = ?",
                            (task_id,),
                        ).fetchone()
                        sequence = int(row["next"])
                        conn.execute(
                            "INSERT INTO events (event_id, task_id, execution_id, principal, "
                            "device_id, kind, created_at, sequence, payload) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                event_id,
                                task_id,
                                execution_id,
                                principal,
                                device_id,
                                kind,
                                created_at,
                                sequence,
                                payload,
                            ),
                        )
                    return {
                        "event_id": event_id,
                        "task_id": task_id,
                        "execution_id": execution_id,
                        "principal": principal,
                        "device_id": device_id,
                        "kind": kind,
                        "created_at": created_at,
                        "sequence": sequence,
                        "payload": payload,
                    }
                except sqlite3.IntegrityError:
                    if attempt == 2:
                        raise
                    continue
        raise AssertionError("不可达")  # pragma: no cover

    # ---- 读 ----

    def read(self, task_id: str, limit: int = 200) -> list[dict]:
        """某个任务**最近** `limit` 条事件，按序号升序返回（与旧 JSONL 版语义一致）。

        先按序号倒序取 limit 条、再翻回升序：要的是「最近的 N 条」，但读出来必须是
        时间顺序——反过来的话回放会把因果颠倒过来。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM (SELECT * FROM events WHERE task_id = ? "
                "ORDER BY sequence DESC LIMIT ?) ORDER BY sequence ASC",
                (task_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def kinds(self, task_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind FROM events WHERE task_id = ? ORDER BY sequence ASC", (task_id,)
            ).fetchall()
        return [row["kind"] for row in rows]

    def read_by_execution(self, execution_id: str, limit: int = 200) -> list[dict]:
        """按 `execution_id` 取事件——V4 §五 那条「一次执行串起全链条」的入口。"""
        if not execution_id:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM (SELECT * FROM events WHERE execution_id = ? "
                "ORDER BY created_at DESC, sequence DESC LIMIT ?) "
                "ORDER BY created_at ASC, sequence ASC",
                (execution_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 维护与自省 ----

    def task_ids(self) -> list[str]:
        """出现过事件的任务 id（`scripts/replay_task.py` 用它列出可回放的任务）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT task_id FROM events ORDER BY task_id"
            ).fetchall()
        return [row["task_id"] for row in rows]

    def count(self, task_id: str | None = None) -> int:
        with self._lock:
            if task_id is None:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM events WHERE task_id = ?", (task_id,)
                ).fetchone()
        return int(row["n"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
