"""Observation 与 prompt 摘要。

V2 起 history 归 `storage.trajectory_store.TrajectoryStore` 管理，不再有 AgentState：
任务状态在 `models.task.Task`，恢复点状态在 `models.checkpoint.Checkpoint`。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .action import Action


class StepOutcome(str, Enum):
    """单次观察 / 执行的结果判定。

    与 `models.task_step.StepStatus` 区分：那个描述「计划步骤走到哪了」。
    """

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
    status: StepOutcome = StepOutcome.OK
    message: str = ""
    created_at: datetime = Field(default_factory=datetime.now)

    def to_prompt_dict(self) -> dict[str, Any]:
        """产出进 prompt 的精简表示。

        mode="json" 让枚举序列化成 "ok" 而不是 StepOutcome.OK；result 走白名单裁剪。
        """
        data = self.model_dump(mode="json", include=PROMPT_FIELDS)
        result = self.result or {}
        data["result"] = {k: v for k, v in result.items() if k in PROMPT_RESULT_FIELDS}
        return data


def compact_observations(history: list[Observation], last_n: int = 5) -> list[dict[str, Any]]:
    """取最近 N 条观察的精简表示，用于拼 prompt。"""
    return [o.to_prompt_dict() for o in history[-last_n:]]
