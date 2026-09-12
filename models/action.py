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


# effective_risk 取「服务端规则」与「模型建议」中更严格的一个：DANGEROUS > CAUTION > SAFE
_RISK_RANK = {ActionRisk.SAFE: 0, ActionRisk.CAUTION: 1, ActionRisk.DANGEROUS: 2}


def _risk_rank(risk: "ActionRisk") -> int:
    return _RISK_RANK[risk]


class ActionEffectStatus(str, Enum):
    """一次动作「到底有没有在设备上生效」的判定（V2.1 §五）。

    手机 Agent 最大的恢复难题不是「当前页面是什么」，而是「上一次动作到底执行没执行」。
    例如进程在「点击提交订单」之后、拿到验证截图之前崩溃：重启时只知道 Step = 提交订单，
    却不知道订单到底提交没提交——若直接重试就会重复下单。

    - NOT_STARTED      ：还没发出去
    - DISPATCHED       ：ADB 命令已发出（executor 返回 ok），但还没验证页面变化
    - EFFECT_UNKNOWN   ：dispatch 后进程崩溃 / 拿不到验证观察 → 重启即落入此态，绝不能默认 retry
    - VERIFIED_SUCCESS ：验证通过（页面确实按预期变化）
    - VERIFIED_FAILED  ：验证失败
    """

    NOT_STARTED = "not_started"
    DISPATCHED = "dispatched"
    EFFECT_UNKNOWN = "effect_unknown"
    VERIFIED_SUCCESS = "verified_success"
    VERIFIED_FAILED = "verified_failed"


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

    def policy_risk(self) -> ActionRisk:
        """服务端规则推断的风险等级（类型 + 关键词），**不采纳**模型在 `risk` 上的声明。

        模型可以**建议**风险（在 action 上写 `risk="caution"`），但不能把危险动作声称为 safe——
        否则外部调用能静默执行「确认付款」之类动作（V2.1 §十一：Server Policy > Model Suggestion）。
        """
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

    def resolved_risk(self) -> ActionRisk:
        """effective_risk = max(policy_risk, model_risk)。

        模型声明的 `risk` 只是「建议」：它可以把风险**说高**（要求更严格确认），
        但**不能把危险动作说低**。例如 `{"type":"tap","target":"确认付款","risk":"safe"}`
        仍会被判定为 DANGEROUS——这是 HITL 门禁不被绕过的底线。
        """
        if self.risk is None:
            return self.policy_risk()
        return max(self.policy_risk(), self.risk, key=_risk_rank)

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
