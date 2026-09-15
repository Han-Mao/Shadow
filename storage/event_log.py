"""事件日志（V2.1 §二十三）。

轨迹（`TrajectoryStore`）记的是「每一步看到了什么」——为**下一步决策**服务，
所以只保留最近几条，而且会被裁剪。

事件日志记录的是「这个任务发生过什么」——为**审计与重放**服务，
所以只追加、不裁剪，并且要能事后回答「它为什么会被抢占」「为什么会失败」。
两者用途不同，不要合并成一个。

格式选 JSONL 而不是单个 JSON 数组：追加不需要「读出来改完再整个写回」，
进程在写一半时被杀也只丢最后一行，不会损坏已有记录。
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from models.exceptions import PersistenceError

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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "kind": self.kind,
            "at": self.at,
            "data": self.data,
        }


class EventLog:
    """追加式事件日志，每个任务一个 `.jsonl` 文件。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

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

        报错方式（`PersistenceError`）与 `Runtime._degrade` / TaskManager 的失败分流
        完全一致：安全事件写不进 = durable state 落后于现实，任务停在 DEGRADED，
        而不是带着「转账了但没有授权记录」继续跑。
        """
        if is_safety_critical(kind):
            return self.emit_critical(task_id, kind, **data)

        event = Event(task_id=task_id, kind=kind, data=data)
        try:
            path = self._root / f"{task_id}.jsonl"
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - 普通事件是旁路，不能成为故障源
            logger.warning("写事件日志失败（任务 %s / %s）：%s", task_id, kind, exc)
        return event

    def emit_critical(self, task_id: str, kind: str, **data) -> Event:
        """写一条**安全关键**事件。写失败抛 `PersistenceError`（V3 M4）。

        这是 `emit` 在 `is_safety_critical(kind)` 为真时的实际执行体；
        也可以被显式调用，用来强制让一个**不在** `SAFETY_CRITICAL_KINDS` 里的事件
        走 fail-closed（例如未来新增的审计关键事件先上线、再补进常量表）。

        与 `emit` 的区别：`emit` 是旁路（fail-open，写不进不挡执行）；
        安全关键事件（危险动作已 dispatch、风险判定、人工批准、完成认定）一旦
        丢失，审计链就断了——「手机转账了但没记录」不可接受。所以这里写失败要
        **抛出去**，由调用方（runtime）把任务降级、副作用不继续。

        注意：普通事件仍走 `emit`（fail-open），只有明确的安全关键事件才走这里。
        分级而不是一刀切，避免「审计日志抖动就把所有任务都降级」。
        """
        event = Event(task_id=task_id, kind=kind, data=data)
        path = self._root / f"{task_id}.jsonl"
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - 转换后重新抛出
            raise PersistenceError(task_id, f"安全关键事件 {kind} 写盘失败：{exc}") from exc
        return event

    def read(self, task_id: str, limit: int = 200) -> list[Event]:
        """按时间顺序读取最近的事件。"""
        path = self._root / f"{task_id}.jsonl"
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读事件日志失败（任务 %s）：%s", task_id, exc)
            return []

        events: list[Event] = []
        for line in lines[-limit:]:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue  # 被截断的最后一行，跳过即可
            events.append(
                Event(
                    task_id=raw.get("task_id", task_id),
                    kind=raw.get("kind", ""),
                    data=raw.get("data", {}),
                    at=raw.get("at", ""),
                    id=raw.get("id", ""),
                )
            )
        return events

    def kinds(self, task_id: str) -> list[str]:
        return [event.kind for event in self.read(task_id)]
