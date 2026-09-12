"""任务关系（V2 §四）：判断新任务与在跑任务是什么关系。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TaskRelation(str, Enum):
    UNRELATED = "unrelated"
    """互不相干，各自排队。"""

    SUBTASK = "subtask"
    """新任务是当前任务的子任务，应并入当前任务执行，不另起任务。"""

    SUPER_TASK = "super_task"
    """新任务是当前任务的父任务，需要重构当前任务的目标。"""

    DUPLICATE = "duplicate"
    """与已有任务重复，直接复用，不重复执行。"""

    INTERRUPT = "interrupt"
    """需要打断当前任务优先执行，执行完再恢复。"""


class TaskRelationResult(BaseModel):
    relation: TaskRelation = TaskRelation.UNRELATED

    confidence: float = 0.0
    reason: str = ""

    affected_task_id: str | None = None

    # 各信号源的原始分，便于排查「为什么判成了这个关系」
    signals: dict[str, float] = Field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        """置信度过低时不采信关系判定，退化为普通新任务。"""
        return self.confidence >= 0.5 and self.relation is not TaskRelation.UNRELATED

    def describe(self) -> str:
        target = f" → {self.affected_task_id}" if self.affected_task_id else ""
        return f"{self.relation.value}({self.confidence:.2f}){target}: {self.reason}"
