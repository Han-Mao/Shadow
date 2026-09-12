"""Task 模型（§3.1）。"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _new_task_id() -> str:
    """时间戳 + 随机后缀。纯时间戳在同一时刻并发建任务时会撞 id，进而互相覆盖状态。"""
    return f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"


class Task(BaseModel):
    id: str = Field(default_factory=_new_task_id)
    instruction: str
    context: str = ""
    status: TaskStatus = TaskStatus.PENDING
    max_steps: int = 10
    plan: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)

    def mark(self, status: TaskStatus) -> None:
        self.status = status
