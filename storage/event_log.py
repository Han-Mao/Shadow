"""事件日志（V2.1 §二十三 · V4 §四）。

轨迹（`TrajectoryStore`）记的是「每一步看到了什么」——为**下一步决策**服务，
所以只保留最近几条，而且会被裁剪。

事件日志记录的是「这个任务发生过什么」——为**审计与重放**服务，
所以只追加、不裁剪，并且要能事后回答「它为什么会被抢占」「为什么会失败」。
两者用途不同，不要合并成一个。

## 为什么从 JSONL 换到 SQLite（V4 §四）

V3.3 §一 给 JSONL 补上了 `write → fsync`（**落盘可靠性**），但审核列的那一串它给不了：

    多进程全序 · 查询 · 筛选 · 分页 · 关联 · 事务

现在这些由表结构与数据库本身提供：

| 保证 | 靠什么 |
|---|---|
| 每条事件有**每任务连续**的序号（1、2、3…） | `UNIQUE(task_id, sequence)` + 同事务内算序号 |
| 多进程同时写不丢、序号不重 | `BEGIN IMMEDIATE` + `busy_timeout` + 唯一约束 |
| commit 返回即落盘 | `PRAGMA synchronous=FULL`（替代手写 fsync） |
| 按 `execution_id` 串起一次执行的全链条（V4 §五） | 索引 `idx_events_execution` |

**接口刻意保持不变**（`emit` / `emit_critical` / `read` / `kinds`）：它有三四十个调用点，
这次要换的是**存储**而不是用法。分级（安全关键事件 fail-closed）也原样保留——
那是 V3.1 P0-1 的成果，与存储实现无关。

旧的 `<task_id>.jsonl` 会在首次打开时**一次性导入**（见 `_import_legacy_jsonl`），
所以升级不会丢掉已有的事件流。
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from models.exceptions import PersistenceError

from .event_store import EventStore

logger = logging.getLogger(__name__)

# 事件类型集中定义，避免字符串散落各处后拼写漂移、事后查不到
CREATED = "created"
QUEUED = "queued"
STARTED = "started"
ACTION_DISPATCHED = "action_dispatched"
ACTION_VERIFIED = "action_verified"
CHECKPOINT_SAVED = "checkpoint_saved"
RECONCILED = "reconciled"
WAITING = "waiting"
CONFIRMED = "confirmed"
PREEMPT_REQUESTED = "preempt_requested"
SUSPENDED = "suspended"
RESUMED = "resumed"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
RECOVERED = "recovered"

# V2.3：数据损坏必须被看见，而不是 404。
TASK_CORRUPTED = "task_corrupted"

# ---- V2.2 新增：把「谁做的决定」也变成可追溯事实 ----

GOAL_REQUESTED = "goal_requested"
"""模型申请完成（DONE_REQUEST）。申请 ≠ 完成。"""

GOAL_CONFIRMED = "goal_confirmed"
"""目标验证器确认完成（附独立证据）。"""

GOAL_REJECTED = "goal_rejected"
"""目标验证器**驳回**完成申请——声称完成与可核验事实矛盾。"""

EFFECT_UNKNOWN = "effect_unknown"
"""动作已发出但效果无法确认（拿不到验证观察）。下一个安全点要对账。"""

EXECUTION_RECOVERED = "execution_recovered"
"""启动恢复把一条**进程死后留下来的**执行落定为终态（v4.1 §六）。

刻意**不**放进 `SAFETY_CRITICAL_KINDS`：这条事件丢失不会让审计链断掉——
「它为什么被改成了 UNKNOWN / FAILED」这个理由同时写在执行记录的 `note` 字段里，
而记录本身是权威事实。事件在这里是**解释**，不是凭据。
"""

RISK_ASSESSED = "risk_assessed"
"""风险门禁的判定结论，含「模型试图降级被拒」这种要留痕的情况。"""

OBSERVATION_STALE = "observation_stale"
"""V3.1 P1-6：决策所依据的那一屏在执行前已经变了，本次动作被放弃、改为重新观察。

它记录的是**一次被避免的 TOCTOU**：拿 A 屏算出来的坐标去点 B 屏。
刻意**不**放进 `SAFETY_CRITICAL_KINDS`——这条事件丢失只会少一条解释，
而它对应的行为（拒绝执行）本身仍然是最安全的那一侧。
"""


# ---- V3 M4：安全关键事件 fail-safe ----
#
# v2.9 P1 §八：EventLog 是 fail-open——写失败只 warning 后继续执行。
# 对普通业务日志没问题，但这个日志同时承担「审计 + 回放 + 安全决策追踪」。
# 最坏情形是：手机实际发生了转账，但 EventLog 没记录，事后「发生了什么 / 谁批准」无法追溯。
#
# 所以安全关键事件要 fail-safe：写不进 durable store，副作用就不该继续。
# 这些事件一旦丢失，审计链就断了：
SAFETY_CRITICAL_KINDS = frozenset(
    {
        ACTION_DISPATCHED,  # 动作已经要发出去了，这是「发生了什么」的最后一处记录点
        RISK_ASSESSED,      # 风险怎么判的、模型有没有试图降级
        CONFIRMED,          # 谁批准了危险动作 / 恢复 / 完成
        GOAL_CONFIRMED,     # 谁认定任务完成
    }
)


def is_safety_critical(kind: str) -> bool:
    """这条事件是否属于「丢失即审计链断裂」的安全关键事件。"""
    return kind in SAFETY_CRITICAL_KINDS


@dataclass
class Event:
    task_id: str
    kind: str
    data: dict = field(default_factory=dict)
    # 毫秒精度：回放要看「两个事件之间隔了多久」（比如抢占请求到真正让出），
    # 秒级粒度会把这类分析糊掉
    at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="milliseconds"))
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    # V4 §四：每个任务独立的序号。它才是「先后」的权威依据——
    # 毫秒时间戳在两个进程同时写时可能相同，序号不会。
    sequence: int | None = None

    def to_dict(self) -> dict:
        payload = {
            "id": self.id,
            "task_id": self.task_id,
            "kind": self.kind,
            "at": self.at,
            "data": self.data,
        }
        if self.sequence is not None:
            payload["sequence"] = self.sequence
        return payload


class EventLog:
    """追加式事件日志（SQLite 表 `events`）。"""

    def __init__(self, target) -> None:
        """`target` 可以是 `Database`（推荐：与 TaskStore / CheckpointStore 共享一个库与
        事务，§三 的「同一事务」就靠它）或一个路径：目录 → `<目录>/shadow.db`，
        `*.db` → 用它本身。

        保留「传目录」这种用法是有意的：调用点都在传 `STORAGE_DIR`，
        而这次改的是存储不是用法。
        """
        self._store = EventStore(target)
        self.path = self._store.path
        # V4 之前事件写在 `<存储目录>/events/*.jsonl`；迁移时按这个约定导入
        self._legacy_root = self._store.legacy_dir("events")
        imported = self._import_legacy_jsonl()
        if imported:
            logger.info("已把 %d 条历史 JSONL 事件导入 SQLite（%s）", imported, self.path)

    # ---- 写 ----

    def emit(self, task_id: str, kind: str, **data) -> Event:
        """写一条事件。**按 kind 自动分级**（V3.1 P0）。

        - 普通事件 → fail-open：审计是旁路，写失败只 warning，不挡执行。
        - 安全关键事件（`is_safety_critical(kind)` 为真）→ fail-closed：
          写失败抛 `PersistenceError`，调用方必须中止后续副作用。

        分派刻意放在 `emit` **内部**，而不是靠调用方自觉去选 `emit_critical`。
        V3 M4 定义了 `SAFETY_CRITICAL_KINDS` 却只让 `ACTION_DISPATCHED` 一处走
        `emit_critical`，`RISK_ASSESSED` / `CONFIRMED` / `GOAL_CONFIRMED` 仍走这里
        的 fail-open 分支——常量表说它们是「丢失即审计链断裂」，真实行为却是
        「写不进去也照跑」。「约定调用方记得选对方法」这种事早晚会漏，
        所以让唯一的写入口自己按 kind 决定。
        """
        if is_safety_critical(kind):
            return self.emit_critical(task_id, kind, **data)

        event = Event(task_id=task_id, kind=kind, data=data)
        try:
            row = self._store.append(task_id, kind, data)
            event.sequence = int(row["sequence"])
        except Exception as exc:  # noqa: BLE001 - 普通事件是旁路，不能成为故障源
            logger.warning("写事件日志失败（任务 %s / %s）：%s", task_id, kind, exc)
        return event

    def emit_critical(self, task_id: str, kind: str, **data) -> Event:
        """写一条**安全关键**事件。写失败抛 `PersistenceError`（V3 M4）。

        这是 `emit` 在 `is_safety_critical(kind)` 为真时的实际执行体；
        也可以被显式调用，用来强制让一个**不在** `SAFETY_CRITICAL_KINDS` 里的事件
        走 fail-closed（例如未来新增的审计关键事件先上线、再补进常量表）。

        为什么写失败要**抛出去**：安全关键事件（危险动作已 dispatch、风险判定、
        人工批准、完成认定）一旦丢失，审计链就断了——「手机转账了但没记录」不可接受。
        调用方（runtime / API 的手工路径）据此把任务降级、副作用不继续。

        V3.3 §一 补的落盘语义在 V4 §四 之后由数据库给：`synchronous=FULL` 之下
        **commit 返回即落盘**，所以「写成功」这次真的等于 durable——
        不再需要我们逐条记住该不该 fsync。
        """
        event = Event(task_id=task_id, kind=kind, data=data)
        try:
            row = self._store.append(task_id, kind, data)
            event.sequence = int(row["sequence"])
        except Exception as exc:  # noqa: BLE001 - 转换后重新抛出
            raise PersistenceError(task_id, f"安全关键事件 {kind} 写盘失败：{exc}") from exc
        return event

    # ---- 读 ----

    def read(self, task_id: str, limit: int = 200) -> list[Event]:
        """按顺序读取最近 `limit` 条事件。"""
        try:
            rows = self._store.read(task_id, limit=limit)
        except Exception as exc:  # noqa: BLE001 - 读失败不该把 500 带给调用方
            logger.warning("读事件日志失败（任务 %s）：%s", task_id, exc)
            return []
        return [self._to_event(row) for row in rows]

    def kinds(self, task_id: str) -> list[str]:
        try:
            return self._store.kinds(task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读事件类型失败（任务 %s）：%s", task_id, exc)
            return []

    def read_by_execution(self, execution_id: str, limit: int = 200) -> list[Event]:
        """按 `execution_id` 取事件（V4 §五：一次执行的完整链条）。"""
        try:
            rows = self._store.read_by_execution(execution_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("按执行读事件失败（%s）：%s", execution_id, exc)
            return []
        return [self._to_event(row) for row in rows]

    def task_ids(self) -> list[str]:
        """出现过事件的任务 id（回放脚本用它列出可回放的对象）。"""
        try:
            return self._store.task_ids()
        except Exception as exc:  # noqa: BLE001
            logger.warning("列出事件任务失败：%s", exc)
            return []

    def count(self, task_id: str | None = None) -> int:
        return self._store.count(task_id)

    def close(self) -> None:
        self._store.close()

    # ---- 内部 ----

    @staticmethod
    def _to_event(row: dict) -> Event:
        try:
            data = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            # 结构坏掉的 payload 不让整条读路径失败：留一个空 dict，事件本身还在
            data = {}
        return Event(
            task_id=row["task_id"],
            kind=row["kind"],
            data=data if isinstance(data, dict) else {},
            at=row["created_at"],
            id=row["event_id"],
            sequence=int(row["sequence"]),
        )

    def _import_legacy_jsonl(self) -> int:
        """把旧的 `<task_id>.jsonl` 一次性搬进表里，返回导入条数（V4 §四）。

        两种情况直接跳过：
        - 表里已经有事件（说明已经在用新存储，不该再往里灌历史）；
        - 目录里没有 `.jsonl`。

        导入保留**原来的 event_id 与时间戳**：否则搬过来的事件会看起来
        「全都发生在升级那一刻」，而回放与审计正是要看它们之间隔了多久。
        被截断的坏行照旧跳过（与 JSONL 版的行为一致）。
        """
        if not self._legacy_root.exists():
            return 0
        legacy_files = sorted(self._legacy_root.glob("*.jsonl"))
        if not legacy_files or self._store.count() > 0:
            return 0

        imported = 0
        for path in legacy_files:
            task_id = path.stem
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 被截断的最后一行
                kind = raw.get("kind") or ""
                if not kind:
                    continue
                data = raw.get("data")
                self._store.append(
                    raw.get("task_id") or task_id,
                    kind,
                    data if isinstance(data, dict) else {},
                    event_id=raw.get("id") or None,
                    created_at=raw.get("at") or None,
                )
                imported += 1
        return imported
