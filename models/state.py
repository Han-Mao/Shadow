"""Observation、AgentState 与执行结果（§3.3 / §3.4）。"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .action import Action
from .task import Task, TaskStatus


class StepStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    DONE = "done"


class Observation(BaseModel):
    step: int
    screenshot_path: str
    screen_size: tuple[int, int] | None = None
    package: str = ""
    activity: str = ""
    ui_tree: str | None = None
    action: Action | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = StepStatus.OK
    message: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class AgentState(BaseModel):
    task: Task
    history: list[Observation] = Field(default_factory=list)
    current_step: int = 0
    retry_count: int = 0

    def compact_history(self, last_n: int = 5) -> list[dict[str, Any]]:
        return [o.model_dump(exclude={"ui_tree"}) for o in self.history[-last_n:]]
