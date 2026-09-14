"""步骤的「尝试」记录（V2.1 §二十）。

`TaskStep` 原来是「计划 + 执行记录」的混合体：

    goal / depends_on / max_retries          ← 计划层：我要做什么、允许重试几次
    retry_count / last_action / last_error   ← 执行层：我试过什么、结果如何

混在一起的代价很实际：**执行历史会被覆盖**。
同一个步骤失败三次，事后只看得到第三次的动作和错误；
想知道「第一次试的是什么、为什么不行」——没地方可查。
而排查 Agent 的问题，恰恰最需要看「它试过哪些没用的办法」。

拆开之后各归各位：

    TaskStep     只描述计划：目标 + 依赖 + 重试上限 + 当前状态 + 尝试历史
    StepAttempt  描述**一次**尝试：第几次、什么动作、结果、错误分类、证据来自哪层

调用方（runtime / API）不用改写法：`retry_count` / `last_action` / `last_error`
保留原名，但改成从 `attempts` 派生的只读属性。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from .action import Action, ActionEffectStatus
from .retry import ErrorClass


class AttemptOutcome(str, Enum):
    """一次尝试的结果。"""

    OK = "ok"
    """动作执行并验证通过。"""

    DONE = "done"
    """这一步连同整个任务被判定完成。"""

    ERROR = "error"
    """动作失败（设备层报错、或验证判定没达成）。"""

    UNKNOWN = "unknown"
    """还没结算——动作已发出但结果未知。"""


class StepAttempt(BaseModel):
    """对一个计划的某一次尝试。"""

    id: str = Field(default_factory=lambda: f"a{uuid.uuid4().hex[:8]}")
    """尝试 id。与 `Checkpoint.action_attempt_id` 对齐，恢复时能精确定位到是哪一次。"""

    step_id: str

    number: int = 1
    """第几次尝试（1 起）。让「这是第一次还是第三次」变得可读。"""

    action: Action | None = None
    outcome: AttemptOutcome = AttemptOutcome.UNKNOWN

    effect: ActionEffectStatus = ActionEffectStatus.NOT_STARTED
    """这次尝试产生的效果（§十九的三概念之一）。"""

    layer: str = ""
    """结论来自哪一层验证：`device` / `device+ui_tree` / `vlm` / `planner`。"""

    error: str | None = None
    error_class: ErrorClass | None = None
    """错误分类（§十三）。有了它才能事后回答「到底是设备抖动还是做法不对」。"""

    at: datetime = Field(default_factory=datetime.now)

    @property
    def succeeded(self) -> bool:
        return self.outcome in (AttemptOutcome.OK, AttemptOutcome.DONE)
