"""Action Risk Gate（V2.1 §十 / §十一 · V2.2 §一）：所有改变设备的 Action 必经的单一风险门禁。

**为什么门禁不能只是 `Action.resolved_risk()` 的一层包装**

`resolved_risk()` 只看得到动作自己（类型 + 文本关键词）。审核指出这正是风险的来源：

    VLM 返回 {"action_type": "tap", "target": "点击红色按钮", "risk_hint": "safe"}
        ↓
    动作文本里一个危险词都没有 → 判 SAFE → 不触发 HITL → 真的点下去了

而那个红色按钮实际叫「立即购买」——**这个信息在 UI 树里，不在动作里**。
所以门禁必须拿到上下文，把三路证据合起来：

    policy_risk = max( 动作类型风险, 动作文本关键词风险,
                       **目标元素文本/resource-id 风险**, **当前页面敏感度下限** )
    model_risk  = max( 模型 risk 声明, 模型 risk_hint 建议 )
    effective   = max( policy_risk, model_risk )        # 模型只能抬，不能降

模型这边只有「建议权」（`risk_hint`），降级会被记录并忽略——审计时能回答
「这一级风险到底是谁定的、模型有没有试图把它说低」。

Runtime 与 API 共用 `ActionRiskGate.assess()`，保证
「任何会改变设备的 Action 都先过 RiskPolicy，再谈执行」（V2.1 §十 的统一链路）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from models.action import (
    SAFE_ACTION_TYPES,
    SENSITIVE_PACKAGE_MARKERS,
    Action,
    ActionRisk,
    risk_rank,
    strictest,
)
from models.semantic import SemanticRole, infer_role, semantic_for
from vision import target as target_evidence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskContext:
    """判定风险时能看到的「页面上下文」。

    刻意不包含任务指令：把任务原文塞进关键词匹配会让「帮我在淘宝下单」这种任务里
    **每一个**点击都变成 DANGEROUS，门禁随即失效（什么都拦 = 什么都不拦）。
    指令只用于日志说明「这是什么任务下的动作」。
    """

    package: str = ""
    activity: str = ""
    ui_tree: str | None = None
    screen_size: tuple[int, int] | None = None
    instruction: str = ""

    @classmethod
    def from_observation(cls, observation, *, instruction: str = "") -> "RiskContext":
        """从一次观察里取上下文（Runtime / API 都走这个入口）。"""
        if observation is None:
            return cls(instruction=instruction)
        return cls(
            package=getattr(observation, "package", "") or "",
            activity=getattr(observation, "activity", "") or "",
            ui_tree=getattr(observation, "ui_tree", None),
            screen_size=getattr(observation, "screen_size", None),
            instruction=instruction,
        )


@dataclass(frozen=True)
class RiskAssessment:
    """一次风险判定的完整结论（不只是那个最终等级）。"""

    effective: ActionRisk
    policy: ActionRisk
    model: ActionRisk
    downgrade_blocked: bool = False
    """模型试图把策略判定降级（model < policy），已被忽略。"""

    reasons: tuple[str, ...] = field(default_factory=tuple)
    """判定依据，逐条可读。事件流与日志直接用它回答「为什么拦」。"""

    @property
    def requires_confirmation(self) -> bool:
        """危险动作必须经人工确认，不能静默执行。"""
        return self.effective is ActionRisk.DANGEROUS

    def describe(self) -> str:
        parts = [f"effective={self.effective.value}", f"policy={self.policy.value}"]
        if self.model is not ActionRisk.SAFE:
            parts.append(f"model={self.model.value}")
        if self.downgrade_blocked:
            parts.append("模型降级被拒")
        if self.reasons:
            parts.append("；".join(self.reasons))
        return " · ".join(parts)


class ActionRiskGate:
    """统一的风险评估入口。"""

    # ---- 判定 ----

    @staticmethod
    def assess(action: Action, *, context: RiskContext | None = None) -> RiskAssessment:
        """算出动作的有效风险（effective_risk = max(policy_risk, model_risk)）。"""
        policy, reasons = ActionRiskGate.policy_risk(action, context=context)
        model = action.model_risk()
        declared = action.declared_risk()
        effective = strictest(policy, model)
        # 只有**明确声明**了更低的风险才算降级尝试。
        # 把「模型没表态」也算成降级的话，每个动作都会报一次警，
        # 真正的降级尝试就淹没在噪声里了。
        downgrade_blocked = declared is not None and risk_rank(declared) < risk_rank(policy)

        if downgrade_blocked:
            logger.warning(
                "模型试图把风险降级（model=%s < policy=%s），已忽略：action=%s target=%s"
                " 当前页面=%s",
                model.value,
                policy.value,
                action.type.value,
                action.target,
                (context.package if context else "") or "未知",
            )
            reasons = (*reasons, f"模型声明 {model.value}，低于策略下限 {policy.value}，已拒绝降级")

        if effective is ActionRisk.DANGEROUS:
            logger.info("判定为危险动作（需人工确认）：%s", "；".join(reasons) or "类型策略")

        return RiskAssessment(
            effective=effective,
            policy=policy,
            model=model,
            downgrade_blocked=downgrade_blocked,
            reasons=tuple(reasons),
        )

    @staticmethod
    def policy_risk(
        action: Action, *, context: RiskContext | None = None
    ) -> tuple[ActionRisk, list[str]]:
        """服务端策略风险（含上下文），返回 (等级, 依据列表)。"""
        reasons: list[str] = []

        # 1) 动作类型
        type_risk = ActionRisk.SAFE if action.type in SAFE_ACTION_TYPES else ActionRisk.CAUTION

        # 2) 语义角色 —— 动作自身 + **目标元素**在 UI 树里的真实文本，统一进
        #    `infer_role` 判「这个动作到底在做什么」，再从角色查表派生风险。
        #    V3 M2 起取代旧的关键词 grep：不再只认 DANGEROUS_KEYWORDS，而是
        #    语义角色（purchase / delete / authorize / submit / like / …）。
        resolved = target_evidence.resolve_target(
            action,
            context.ui_tree if context else None,
            context.screen_size if context else None,
        )
        node_text = resolved.label
        haystack = " ".join(
            part
            for part in (action._policy_haystack(), node_text.lower(), resolved.resource_id.lower())
            if part
        )
        role = infer_role(haystack)
        text_risk = semantic_for(role).risk

        if role is not SemanticRole.UNKNOWN:
            reasons.append(f"语义角色 {role.value}（判定风险 {text_risk.value}）")
            if node_text and role in (
                SemanticRole.PURCHASE,
                SemanticRole.DELETE,
                SemanticRole.AUTHORIZE,
                SemanticRole.SUBMIT,
            ):
                # 这一条是 V2.2 新增的关键能力：动作描述里没有危险词，
                # 但被点到的那个控件本身叫「立即购买」——只有查 UI 树才知道
                reasons.append(f"目标元素实为「{node_text}」")

        # 3) 页面敏感度下限：支付/银行类 App 里会改页面的动作至少 CAUTION
        page_risk = ActionRisk.SAFE
        if action.is_mutating and _is_sensitive_package(context):
            page_risk = ActionRisk.CAUTION
            reasons.append(f"当前页面属敏感应用（{context.package if context else ''}）")

        return strictest(type_risk, text_risk, page_risk), reasons

    @staticmethod
    def model_risk(action: Action) -> ActionRisk:
        """模型声明的风险（`risk` 权威标注 ⊕ `risk_hint` 建议）。"""
        return action.model_risk()

    @staticmethod
    def requires_confirmation(action: Action, *, context: RiskContext | None = None) -> bool:
        """危险动作必须经人工确认，不能静默执行。"""
        return ActionRiskGate.assess(action, context=context).requires_confirmation

    # ---- 上下文落地：把「动作的目标」解析成 UI 树里的真实节点 ----

    @staticmethod
    def resolve_target_node(action: Action, context: RiskContext | None = None):
        """把动作目标还原成 UI 树节点（委托 `vision.target`，全系统只此一份口径）。

        拿不到节点不是错误：LAUNCH / WAIT 这类动作本来就没有目标元素，
        风险判定退化为「动作类型 + 动作文本」。
        """
        resolved = target_evidence.resolve_target(
            action,
            context.ui_tree if context else None,
            context.screen_size if context else None,
        )
        return resolved.node


def _is_sensitive_package(context: RiskContext | None) -> bool:
    if context is None or not context.package:
        return False
    package = context.package.lower()
    return any(marker in package for marker in SENSITIVE_PACKAGE_MARKERS)
