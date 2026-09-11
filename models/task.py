"""Task 模型（§3.1）。"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Task(BaseModel):
    id: str = Field(default_factory=lambda: datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    instruction: str
    context: str = ""
    status: TaskStatus = TaskStatus.PENDING
    max_steps: int = 10
    plan: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)

    def mark(self, status: TaskStatus) -> None:
        self.status = status
