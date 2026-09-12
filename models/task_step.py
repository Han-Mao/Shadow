"""任务步骤（V2 §三）：计划不再是字符串列表，而是可追踪的执行状态机。"""
from __future__ import annotations

import uuid
from enum import Enum

from pydantic import BaseModel, Field

from .action import Action


class StepStatus(str, Enum):
    """计划步骤的生命周期状态。

    注意与 `models.state.StepOutcome` 的区别：StepOutcome 描述「一次观察/执行的结果」，
    StepStatus 描述「计划里某个步骤走到哪了」。两者语义不同，不要混用。
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


def new_step_id(index: int) -> str:
    return f"s{index}"


def build_steps(goals: list[str], *, max_retries: int = 2) -> list["TaskStep"]:
    """把语义级目标列表转成带状态与依赖的步骤序列。

    默认串行依赖（s2 依赖 s1）：手机 GUI 任务的步骤几乎都是顺序的，
    并行依赖关系留给后续按需声明。
    """
    steps: list[TaskStep] = []
    for i, goal in enumerate(goals, start=1):
        steps.append(
            TaskStep(
                id=new_step_id(i),
                goal=str(goal).strip(),
                depends_on=[new_step_id(i - 1)] if i > 1 else [],
                max_retries=max_retries,
            )
        )
    return steps


class TaskStep(BaseModel):
    id: str = Field(default_factory=lambda: f"s{uuid.uuid4().hex[:4]}")
    goal: str

    status: StepStatus = StepStatus.PENDING

    depends_on: list[str] = Field(default_factory=list)

    retry_count: int = 0
    max_retries: int = 2

    last_action: Action | None = None
    last_error: str | None = None

    @property
    def retryable(self) -> bool:
        return self.retry_count < self.max_retries

    def mark(self, status: StepStatus) -> None:
        self.status = status

    def record_action(self, action: Action) -> None:
        self.last_action = action
        self.last_error = None

    def record_failure(self, error: str) -> None:
        self.retry_count += 1
        self.last_error = error
