"""Action Schema（§3.2）+ 动作风险等级与指纹（V2 §十四 / §十七）。"""
from __future__ import annotations

import hashlib
from enum import Enum

from pydantic import BaseModel


class ActionType(str, Enum):
    TAP = "tap"
    LONG_PRESS = "long_press"
    TYPE = "type"
    SWIPE = "swipe"
    BACK = "back"
    HOME = "home"
    LAUNCH = "launch"
    WAIT = "wait"
    DONE = "done"


class ActionRisk(str, Enum):
    """动作风险等级。用于在 Executor 之前插入确认门禁（HITL），而不是事后补救。"""

    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"


# 不改设备状态、或可轻易撤销的动作
SAFE_ACTION_TYPES = frozenset({ActionType.BACK, ActionType.HOME, ActionType.WAIT, ActionType.DONE})
# 会改变设备状态，但通常可撤销
CAUTION_ACTION_TYPES = frozenset(
    {ActionType.TAP, ActionType.LONG_PRESS, ActionType.TYPE, ActionType.SWIPE, ActionType.LAUNCH}
)

# 命中即升级为 DANGEROUS：这类动作在真实 App 里往往不可撤销（下单 / 转账 / 删除）
DANGEROUS_KEYWORDS = (
    "发送", "支付", "付款", "下单", "购买", "确认", "提交", "删除", "转账", "汇款", "解绑", "注销",
    "send", "pay", "purchase", "checkout", "delete", "remove", "confirm", "submit", "transfer", "unbind",
)

# 坐标量化粒度：落在同一个 16px 网格里的点击视为同一个动作。
# 用向下取整而不是 round——round 在桶边界上会因 1px 抖动跳到相邻桶，让循环检测漏判。
_FINGERPRINT_QUANTUM = 16


class Point(BaseModel):
    x: float
    y: float


class Action(BaseModel):
    type: ActionType
    target: Point | str | None = None
    value: str | None = None
    reason: str = ""
    # 留空则由 resolved_risk 按类型 + 关键词推断；显式指定优先（人工标注的动作）
    risk: ActionRisk | None = None

    def resolved_risk(self) -> ActionRisk:
        """推断动作风险等级。"""
        if self.risk is not None:
            return self.risk

        haystack = " ".join(
            str(part).lower() for part in (self.value, self.target, self.reason) if part is not None
        )
        if any(keyword in haystack for keyword in DANGEROUS_KEYWORDS):
            return ActionRisk.DANGEROUS
        if self.type in SAFE_ACTION_TYPES:
            return ActionRisk.SAFE
        if self.type in CAUTION_ACTION_TYPES:
            return ActionRisk.CAUTION
        return ActionRisk.CAUTION

    @property
    def fingerprint(self) -> str:
        """动作指纹，用于检测「同一步骤反复做同一个动作」的死循环（V2 §十七）。

        坐标按 16px 网格向下取整：模型每步给出 (500,1200) 与 (503,1198) 这种抖动时，
        指纹保持一致，不会让死循环伪装成「每步都在做新动作」。
        """
        target = self.target
        if isinstance(target, Point):
            bucket_x = int(target.x) // _FINGERPRINT_QUANTUM
            bucket_y = int(target.y) // _FINGERPRINT_QUANTUM
            target_key = f"{bucket_x},{bucket_y}"
        else:
            target_key = str(target or "")

        raw = f"{self.type.value}|{target_key}|{self.value or ''}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def is_same_as(self, other: "Action", *, tolerance: float = 24.0) -> bool:
        """语义上是不是同一个动作：类型与取值一致，且落点足够接近。

        死循环检测用这个而不是比指纹字符串——指纹把坐标量化到固定网格，
        坐标恰好跨网格边界时（例如 1198 与 1200）会被判成两个动作，漏掉循环。
        这里直接比距离，没有边界问题。
        """
        if self.type is not other.type:
            return False
        if (self.value or "") != (other.value or ""):
            return False

        this_target, other_target = self.target, other.target
        if isinstance(this_target, Point) and isinstance(other_target, Point):
            return (
                abs(this_target.x - other_target.x) <= tolerance
                and abs(this_target.y - other_target.y) <= tolerance
            )
        return str(this_target or "") == str(other_target or "")


class Decision(BaseModel):
    """规划器的一次决策。

    除了动作本身，还带上「做完这个动作后，当前聚焦的步骤是否算完成」——
    这是步骤状态得以逐级推进、暂停后能从正确位置恢复的依据。
    """

    action: Action
    step_done: bool = False
    thought: str = ""

    def describe(self) -> str:
        return f"{self.action.type.value}({self.action.target}) step_done={self.step_done}"
