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


# 进 prompt 的字段白名单：截图路径、时间戳等噪声既费 token，也会让模型把本地文件路径当成页面信息
PROMPT_FIELDS = {"step", "action", "result", "status", "message", "package", "activity"}
# result 只保留决策真正用得上的键，避免 verbose 结果撑爆上下文
PROMPT_RESULT_FIELDS = {"ok", "error", "x", "y", "duration", "text"}


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

    def to_prompt_dict(self) -> dict[str, Any]:
        """产出进 prompt 的精简表示。

        mode="json" 让枚举序列化成 "ok" 而不是 StepStatus.OK；result 走白名单裁剪。
        """
        data = self.model_dump(mode="json", include=PROMPT_FIELDS)
        result = self.result or {}
        data["result"] = {k: v for k, v in result.items() if k in PROMPT_RESULT_FIELDS}
        return data


class AgentState(BaseModel):
    task: Task
    history: list[Observation] = Field(default_factory=list)
    current_step: int = 0
    retry_count: int = 0

    def compact_history(self, last_n: int = 5) -> list[dict[str, Any]]:
        return [o.to_prompt_dict() for o in self.history[-last_n:]]
