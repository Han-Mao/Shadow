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
        """写一条事件。**永不抛异常**——审计日志写失败不该影响任务执行。"""
        event = Event(task_id=task_id, kind=kind, data=data)
        try:
            path = self._root / f"{task_id}.jsonl"
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - 审计是旁路，不能成为故障源
            logger.warning("写事件日志失败（任务 %s / %s）：%s", task_id, kind, exc)
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
