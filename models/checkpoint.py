"""Checkpoint（V2 §八）：保存「恢复这个任务所需的最小状态」，而不是全部历史。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .state import Observation
from .task_step import StepStatus

# UI 树快照只用于「页面是否还是当初那一屏」的比对，没必要留全量
UI_SNAPSHOT_LIMIT = 20_000
# 恢复时需要重放的最近轨迹条数
HISTORY_TAIL_SIZE = 5


def new_checkpoint_id(task_id: str, step: int) -> str:
    """形如 `20260912_0952ab_s003`，人眼可读且带随机后缀防撞。"""
    return f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}_s{step:03d}"


class Checkpoint(BaseModel):
    id: str
    task_id: str

    current_step: int = 0
    step_states: dict[str, StepStatus] = Field(default_factory=dict)

    # 恢复时用来判断「当前页面是否还符合当时的状态」
    screenshot_path: str | None = None
    package: str = ""
    activity: str = ""
    ui_snapshot: str | None = None

    history_tail: list[Observation] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    def capture(
        cls,
        *,
        task_id: str,
        step: int,
        step_states: dict[str, StepStatus],
        observation: Observation | None,
        history_tail: list[Observation] | None = None,
    ) -> "Checkpoint":
        """从一次观察中提取恢复所需的最小状态。"""
        snapshot = None
        if observation is not None and observation.ui_tree:
            snapshot = observation.ui_tree[:UI_SNAPSHOT_LIMIT]

        return cls(
            id=new_checkpoint_id(task_id, step),
            task_id=task_id,
            current_step=step,
            step_states=dict(step_states),
            screenshot_path=observation.screenshot_path if observation else None,
            package=observation.package if observation else "",
            activity=observation.activity if observation else "",
            ui_snapshot=snapshot,
            history_tail=list(history_tail or [])[-HISTORY_TAIL_SIZE:],
        )

    def same_screen_as(self, observation: Observation) -> bool:
        """判断当前页面是否仍在恢复点所在的那一屏。

        只比 package/activity 这类廉价信号——UI 树快照仅作参考，
        因为同一页面重绘后 bounds 会有细微差异，硬比会永远判为「不一致」。
        """
        if not self.package:
            return False
        if observation.package != self.package:
            return False
        if self.activity and observation.activity != self.activity:
            return False
        return True

    def summary(self) -> dict[str, Any]:
        """给人看的精简描述（API 返回 / 日志用）。"""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "current_step": self.current_step,
            "package": self.package,
            "activity": self.activity,
            "step_states": {k: v.value for k, v in self.step_states.items()},
            "created_at": self.created_at.isoformat(),
        }
