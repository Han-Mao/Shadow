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


# 分关系置信度阈值（V2.1 §九）。
#
# 原来所有关系共用 0.5，问题是**不同关系判错的代价差了几个数量级**：
#   DUPLICATE  判错 → 用户的新指令被直接吞掉，什么都不会发生（最难被发现）
#   SUPER_TASK 判错 → 正在执行的任务目标被改写，不可恢复
#   INTERRUPT  判错 → 抢占并挂起别的任务（有 Checkpoint，可以恢复，代价中等）
#   SUBTASK    判错 → 只是往计划里多插一步（代价最小）
# 代价越高，要求越严。
RELATION_THRESHOLDS: dict[TaskRelation, float] = {
    TaskRelation.DUPLICATE: 0.90,
    TaskRelation.SUPER_TASK: 0.85,
    TaskRelation.INTERRUPT: 0.80,
    TaskRelation.SUBTASK: 0.65,
    TaskRelation.UNRELATED: 0.50,
}
DEFAULT_THRESHOLD = 0.5

# 这两类会改写或打断已经在跑的任务，判错不可逆，需要调用方显式放行（二次确认）
RELATIONS_NEEDING_CONFIRMATION = frozenset(
    {TaskRelation.INTERRUPT, TaskRelation.SUPER_TASK}
)


class TaskRelationResult(BaseModel):
    relation: TaskRelation = TaskRelation.UNRELATED

    confidence: float = 0.0
    reason: str = ""

    affected_task_id: str | None = None

    # 各信号源的原始分，便于排查「为什么判成了这个关系」
    signals: dict[str, float] = Field(default_factory=dict)

    @property
    def threshold(self) -> float:
        return RELATION_THRESHOLDS.get(self.relation, DEFAULT_THRESHOLD)

    @property
    def is_actionable(self) -> bool:
        """置信度是否达到**该关系专属**的门槛。

        UNRELATED 永远不 actionable——它不是「可以做某事」的关系，而是「各跑各的」。
        """
        if self.relation is TaskRelation.UNRELATED:
            return False
        return self.confidence >= self.threshold

    @property
    def requires_second_confirmation(self) -> bool:
        """该关系会改动在跑任务，采信前是否需要调用方显式确认。"""
        return self.relation in RELATIONS_NEEDING_CONFIRMATION and self.is_actionable

    def describe(self) -> str:
        target = f" → {self.affected_task_id}" if self.affected_task_id else ""
        return f"{self.relation.value}({self.confidence:.2f}){target}: {self.reason}"
