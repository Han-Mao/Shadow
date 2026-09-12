"""验证的三个概念（V2.1 §十九）。

原来只有一个 `Verification`，把三件性质完全不同的事揉在一起：

1. 动作**发出去**了吗？—— 设备层的确定信号（命令有没有送达）
2. 动作**产生了效果**吗？—— 页面/控件有没有真的变化
3. **目标**达成了吗？—— 语义层面这件事做完了没有

混在一起的后果很实际：日志里只看到一句「失败」，分不清到底是
「命令压根没发出去」（该重试）、「发出去了但页面没反应」（该换策略）、
还是「动作做完了但这个任务还没完」（该继续下一步）。
三者对应的处置完全不同。

分开之后，`ActionEffectStatus` 有了明确归属——它描述的是**效果**（第 2 层），
不再和「有没有发出去」（第 1 层）混用。
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from .action import Action, ActionEffectStatus


class DispatchStatus(str, Enum):
    """动作有没有真的送到设备上。"""

    SENT = "sent"
    """命令已送达并执行（不代表产生了预期效果）。"""

    FAILED = "failed"
    """没发出去：设备离线、ADB 报错、会话被抢占等。"""

    SKIPPED = "skipped"
    """没有动作可发：规划器直接宣告任务完成。"""


class ActionDispatch(BaseModel):
    """第 1 层：动作是否发出（L1 设备层）。"""

    action: Action | None = None
    status: DispatchStatus = DispatchStatus.SENT
    transport_error: str = ""
    """设备层返回的错误原文；成功时为空。"""

    @property
    def ok(self) -> bool:
        return self.status is not DispatchStatus.FAILED


class ActionEffect(BaseModel):
    """第 2 层：动作让页面发生了什么变化（L2 UI 树 + L3 VLM）。"""

    status: ActionEffectStatus = ActionEffectStatus.NOT_STARTED
    evidence: str = ""
    """效果判定的证据来源：`ui_tree` / `vlm` / `none`（没有可比对信号）。"""

    changed: bool = False
    """UI 结构是否发生了变化。没变而动作又是「会改页面」的类型，就是可疑信号。"""

    message: str = ""


class GoalVerification(BaseModel):
    """第 3 层：目标是否达成（语义层）。"""

    achieved: bool = False
    """这一步的目标是否达成。"""

    done: bool = False
    """整个任务是否可以判定完成。"""

    layer: str = ""
    message: str = ""
