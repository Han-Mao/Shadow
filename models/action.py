"""Action Schema（§3.2）+ 动作风险等级与指纹（V2 §十四 / §十七 / V2.2 §一）。

风险这件事有**两个来源**，本模块只负责其中「动作自身能算出来的那一半」：

    policy_risk()  ——  服务端策略（动作类型 + 文本关键词）
    model_risk()   ——  模型声明（`risk` 权威标注 / `risk_hint` 仅建议）

真正的合议在 `agent.risk_gate.ActionRiskGate`：它还能拿到 UI 树、当前页面等
上下文，因此能判出「点击红色按钮」这种本模块看不见的风险。**任何会改设备的动作
都必须过那道门禁**，本模块的 `resolved_risk()` 只是没有上下文时的退化版本。
"""
from __future__ import annotations

import hashlib
from enum import Enum

from pydantic import BaseModel, Field


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
    DONE_REQUEST = "done_request"
    """模型**申请**完成（V2.2 §四）。

    语义上与 DONE 等价，改这个名字是为了让「申请」这个性质在数据里显式可见：
    模型说 done 不等于任务 done，最终由 `agent.goal_verifier` 依据独立证据裁定。
    """


# 所有「宣告完成」的动作类型。判断完成申请一律用 `is_completion_request`，
# 不要到处写 `type is ActionType.DONE`——那样新增别名时必漏。
COMPLETION_ACTION_TYPES = frozenset({ActionType.DONE, ActionType.DONE_REQUEST})


class ActionRisk(str, Enum):
    """动作风险等级。用于在 Executor 之前插入确认门禁（HITL），而不是事后补救。"""

    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"


# effective_risk 取「服务端策略」与「模型建议」中更严格的一个：DANGEROUS > CAUTION > SAFE
RISK_ORDER = {ActionRisk.SAFE: 0, ActionRisk.CAUTION: 1, ActionRisk.DANGEROUS: 2}


def risk_rank(risk: "ActionRisk") -> int:
    """风险的严重度数值（越大越严格）。"""
    return RISK_ORDER[risk]


def strictest(*risks: "ActionRisk") -> "ActionRisk":
    """取最严格的一个。空的输入按 SAFE 处理（没有风险信息 ≠ 有风险）。"""
    return max(risks or (ActionRisk.SAFE,), key=risk_rank)


class ActionEffectStatus(str, Enum):
    """一次动作「到底有没有在设备上生效」的判定（V2.1 §五）。

    手机 Agent 最大的恢复难题不是「当前页面是什么」，而是「上一次动作到底执行没执行」。
    例如进程在「点击提交订单」之后、拿到验证截图之前崩溃：重启时只知道 Step = 提交订单，
    却不知道订单到底提交没提交——若直接重试就会重复下单。

    - NOT_STARTED      ：还没发出去
    - DISPATCHED       ：ADB 命令已发出（executor 返回 ok），但还没验证页面变化
    - EFFECT_UNKNOWN   ：dispatch 后拿不到验证观察 → 效果未知，绝不能默认 retry
    - VERIFIED_SUCCESS ：验证通过（页面确实按预期变化）
    - VERIFIED_FAILED  ：验证失败
    """

    NOT_STARTED = "not_started"
    DISPATCHED = "dispatched"
    EFFECT_UNKNOWN = "effect_unknown"
    VERIFIED_SUCCESS = "verified_success"
    VERIFIED_FAILED = "verified_failed"


# 不改设备状态、或可轻易撤销的动作
SAFE_ACTION_TYPES = frozenset(
    {ActionType.BACK, ActionType.HOME, ActionType.WAIT, ActionType.DONE, ActionType.DONE_REQUEST}
)
# 会改变设备状态，但通常可撤销
CAUTION_ACTION_TYPES = frozenset(
    {ActionType.TAP, ActionType.LONG_PRESS, ActionType.TYPE, ActionType.SWIPE, ActionType.LAUNCH}
)
# 理应让页面或控件状态发生变化的动作（UIA 层验证只对这类动作有意义）
MUTATING_ACTION_TYPES = frozenset(
    {ActionType.TAP, ActionType.LONG_PRESS, ActionType.TYPE, ActionType.SWIPE, ActionType.LAUNCH}
)

# 命中即升级为 DANGEROUS：这类动作在真实 App 里往往不可撤销（下单 / 转账 / 删除）
DANGEROUS_KEYWORDS = (
    # 交易与资金
    "支付", "付款", "下单", "购买", "结算", "转账", "汇款", "提现", "充值", "退款", "扣款",
    "开通", "订购", "续费", "订阅", "免密",
    # 内容与关系不可逆
    "发送", "提交", "确认", "删除", "移除", "解绑", "注销", "解约", "退订", "清空", "格式化",
    # 授权与协议
    "同意", "授权", "允许访问", "获取验证码",
    # English
    "send", "pay", "purchase", "checkout", "check out", "delete", "remove", "confirm",
    "submit", "transfer", "unbind", "withdraw", "top up", "recharge", "subscribe",
    "authorize", "agree", "accept", "reset",
)

# 敏感 App 的 package 特征：在这些应用里，任何会改页面的动作至少按 CAUTION 对待。
# 只抬高到 CAUTION 而不是 DANGEROUS——否则一个「返回上一页」都会把任务卡进 HITL。
SENSITIVE_PACKAGE_MARKERS = (
    "pay", "bank", "wallet", "alipay", "tenpay", "unionpay", "credit", "money",
    "securities", "stock", "insurance", "billing",
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

    # 权威风险标注：由人 / 服务端 / 已确认的策略写入，参与 effective_risk 计算。
    risk: ActionRisk | None = None

    # 模型建议（V2.2 §一）：模型**没有**决定风险的权力，最多给个提示。
    # 单独一个字段的好处是「模型说的」与「人定的」在数据里分得开：
    # 审计时能一眼看出这级风险是抬上去的还是策略判出来的。
    risk_hint: ActionRisk | None = None

    # 模型对「目标已达成」的可核验声明（V2.2 §四）：形如
    #   {"package": "com.taobao.taobao", "activity": "DetailActivity", "text": "立即购买"}
    # 有它就由 goal_verifier 逐条与真实 Observation 比对，比对不过直接驳回完成申请。
    goal_evidence: dict[str, str] = Field(default_factory=dict)

    # ---- 风险 ----

    def _policy_haystack(self) -> str:
        return " ".join(
            str(part).lower() for part in (self.value, self.target, self.reason) if part is not None
        )

    def policy_risk_hits(self) -> list[str]:
        """命中的危险关键词。日志与审计要能回答「为什么这步被判成危险」。"""
        haystack = self._policy_haystack()
        return [keyword for keyword in DANGEROUS_KEYWORDS if keyword in haystack]

    def policy_risk(self) -> ActionRisk:
        """服务端规则推断的风险等级（类型 + 文本关键词），**不采纳**模型声明。

        这是「没有上下文时」的版本：只看动作自身。完整策略风险（含 UI 节点文本、
        当前页面）在 `ActionRiskGate.policy_risk`——它会把这些信息一起算进来。
        """
        if self.policy_risk_hits():
            return ActionRisk.DANGEROUS
        if self.type in SAFE_ACTION_TYPES:
            return ActionRisk.SAFE
        if self.type in CAUTION_ACTION_TYPES:
            return ActionRisk.CAUTION
        return ActionRisk.CAUTION

    def model_risk(self) -> ActionRisk:
        """模型声明的风险（用于参与 effective_risk 计算）。

        取 `risk` 与 `risk_hint` 中更严格的一个：历史调用方与人工标注写 `risk`，
        VLM 走 `risk_hint`。两者都为空时按 SAFE 处理——**这不等于降级**，
        因为 `strictest(policy, SAFE)` 恒等于 policy。要判断「模型有没有试图降级」，
        必须看 `declared_risk()`，而不是这个方法的结果。
        """
        return strictest(self.risk or ActionRisk.SAFE, self.risk_hint or ActionRisk.SAFE)

    def declared_risk(self) -> ActionRisk | None:
        """模型/人工**明确**声明的风险；没表态时返回 None。

        与 `model_risk()` 的区别很关键：没表态 ≠ 说了 safe。
        分不清这两者的话，每一个动作都会被记成「模型试图把风险降级成 safe」，
        告警立刻变成噪声，真正的降级尝试反而看不见了。
        """
        return strictest(self.risk or ActionRisk.SAFE, self.risk_hint or ActionRisk.SAFE) if (
            self.risk is not None or self.risk_hint is not None
        ) else None

    def resolved_risk(self) -> ActionRisk:
        """effective_risk = max(policy_risk, model_risk)。

        模型声明的风险只是「建议」：它可以把风险**说高**（要求更严格确认），
        但**不能把危险动作说低**。例如 `{"type":"tap","target":"确认付款","risk_hint":"safe"}`
        仍会被判定为 DANGEROUS——这是 HITL 门禁不被绕过的底线。
        """
        return strictest(self.policy_risk(), self.model_risk())

    @property
    def is_completion_request(self) -> bool:
        """这是不是一个「申请完成」的动作（DONE / DONE_REQUEST）。"""
        return self.type in COMPLETION_ACTION_TYPES

    @property
    def is_mutating(self) -> bool:
        """这个动作**理应**改变页面/控件状态吗。"""
        return self.type in MUTATING_ACTION_TYPES

    # ---- 指纹 ----

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
