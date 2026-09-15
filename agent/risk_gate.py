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
    SENSITIVE_SCREEN_MARKERS,
    Action,
    ActionRisk,
    ActionType,
    risk_rank,
    strictest,
)
from models.semantic import SemanticRole, infer_role, semantic_for, sensitive_content
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

    target_resolution: str = "no_target"
    """本次判定的**目标证据状态**（`vision.target.TargetResolution` 的值）。

    V3.1 P1-4：门禁必须能回答「这次点击是有目标证据、没有目标证据、还是本来就不需要
    目标证据」。把三种情况都混成「没有节点」，后果是审计看不出「我们其实是在盲点」，
    而盲点时对危险的估算是偏低的。
    """

    @property
    def unresolved_target(self) -> bool:
        """本该有目标证据、但没拿到（UI 树缺失 / 解析失败 / 匹配不到）。"""
        return self.target_resolution in ("no_tree", "parse_error", "not_found")

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
        if self.unresolved_target:
            parts.append(f"目标证据缺失（{self.target_resolution}）")
        if self.reasons:
            parts.append("；".join(self.reasons))
        return " · ".join(parts)


class ActionRiskGate:
    """统一的风险评估入口。"""

    # ---- 判定 ----

    @staticmethod
    def assess(action: Action, *, context: RiskContext | None = None) -> RiskAssessment:
        """算出动作的有效风险（effective_risk = max(policy_risk, model_risk)）。"""
        policy, reasons, resolution = ActionRiskGate.policy_risk(action, context=context)
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
            target_resolution=resolution.value,
        )

    @staticmethod
    def policy_risk(
        action: Action, *, context: RiskContext | None = None
    ) -> tuple[ActionRisk, list[str], "target_evidence.TargetResolution"]:
        """服务端策略风险（含上下文），返回 (等级, 依据列表, 目标解析结果)。"""
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

        if role is not SemanticRole.UNKNOWN:
            text_risk = semantic_for(role).risk
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
        elif action.type in SAFE_ACTION_TYPES:
            # V3.1 P0-3：认不出语义时的保守下限**只对会碰设备的动作成立**。
            #
            # `DONE` / `DONE_REQUEST` 根本不发 adb 命令，`BACK` / `HOME` / `WAIT` 的
            # 效果由动作类型就完全确定。对它们说「不知道这是什么动作」是不成立的——
            # 按类型判定才是对的，抬到 CAUTION 只会让「申请完成」看起来像个危险动作。
            text_risk = ActionRisk.SAFE
            reasons.append("语义角色未知，但该动作类型本身不改设备（按类型判 SAFE）")
        else:
            # 会改页面的动作认不出语义 → 保守下限（V3.1 P0-3）。
            # 以前这种情况 `reasons` 是空的，审计看到的是一条「没有任何理由」的判定，
            # 而它当时的结论是 SAFE——「不知道这是什么按钮」被当成了「它是安全的」。
            text_risk = semantic_for(role).risk
            reasons.append(f"语义角色未知（按保守下限 {text_risk.value} 处理）")

        # 3) 目标证据缺失的下限（V3.1 P1-4 · V3.3 §七）
        #
        # 「没有证据」不等于「没有反证」。UI 树读不到 / 解析失败 / 目标匹配不到时，
        # 我们对这次点击其实一无所知——那就不该因为「没抓到危险词、也没抓到目标文本」
        # 而把它留在 SAFE。
        #
        # 这里分两档，因为两种情形的**代价**差了一个数量级（V3.3 §七 的盲区）：
        #
        #   - 普通应用：抬到 CAUTION。不动它现在的等级，但审计里必须看得出「这是盲点」，
        #     而且它不能再被当成「命中文案之外的 SAFE」静默放行。
        #   - **敏感应用 / 敏感屏**（支付/银行/证券…，或这一屏就是支付确认/转账/验证码）：
        #     直接抬到 DANGEROUS → 转人工。
        #     审核给的例子正是最坏那个：「付款 App + 目标找不到 + TAP」——
        #     我们不知道该点在哪、也不知道那底下是什么按钮，而这一下可能就是付款。
        #     这种不确定性在支付页上不该由系统自己承担。
        #     V4 起「敏感」不只认包名，也认**屏幕内容**（`_screen_sensitivity_hint`）：
        #     普通 App 里弹出的收银台页，同样满足「代价不对称」。
        #
        # 为什么不在所有应用上转人工：那会让门禁变成噪声（每次「按钮没文字/树读不到」
        # 都要问人），然后被人绕过——V3.1 P0-3 的教训。敏感侧是「代价不对称」的那一侧，
        # 所以只在那一侧取最保守的判断。
        evidence_risk = ActionRisk.SAFE
        if action.is_mutating and resolved.resolution.is_evidence_gap:
            screen_hint = _screen_sensitivity_hint(context)
            if _is_sensitive_package(context) or screen_hint:
                evidence_risk = ActionRisk.DANGEROUS
                scope = (
                    f"敏感应用（{context.package if context else ''}）"
                    if _is_sensitive_package(context)
                    else f"敏感屏（命中特征「{screen_hint}」）"
                )
                reasons.append(
                    f"{scope}里目标解析失败（{resolved.resolution.value}）："
                    "点在哪、点的是什么都不知道，不能自动执行"
                )
            else:
                evidence_risk = ActionRisk.CAUTION
                reasons.append(
                    f"目标解析失败（{resolved.resolution.value}）："
                    f"{_TARGET_GAP_HINTS[resolved.resolution]}，按最坏情况对待"
                )

        # 说清楚这两档实际拦住了什么，别把功劳记错地方：
        # TAP / LONG_PRESS / TYPE / SWIPE / LAUNCH 的动作类型下限本来就是 CAUTION，
        # 所以对它们来说上面只是**不改动现状**（敏感应用那一档是新增的真拦截）。
        # 它的结构性价值在于：任何将来被标成「类型安全」的会改页面动作（或新增类型
        # 忘了归类），只要目标解析不出来就会被抬起来，而不会因为「没命中文案」静默放行。
        #
        # 另外两件让「按钮无文字 + 解析不到节点 → tap(540,1600)」不再危险的事，
        # 与这一行无关：
        #   - `models.semantic` 的 UNKNOWN 已改成 CAUTION + 非幂等（V3.1 P0-3）；
        #   - `Action.side_effect()` 对 TAP/LONG_PRESS/TYPE 的类型兜底已改成非幂等，
        #     所以 EFFECT_UNKNOWN 之后**不会自动再点一次**，而是走对账 / 人工。

        # 4) 页面敏感度下限：支付/银行类 App（或敏感屏）里会改页面的动作至少 CAUTION
        page_risk = ActionRisk.SAFE
        if action.is_mutating:
            screen_hint = _screen_sensitivity_hint(context)
            if _is_sensitive_package(context):
                page_risk = ActionRisk.CAUTION
                reasons.append(f"当前页面属敏感应用（{context.package if context else ''}）")
            elif screen_hint:
                page_risk = ActionRisk.CAUTION
                reasons.append(f"当前屏幕属敏感屏（命中特征「{screen_hint}」）")

        # 5) 输入内容里的身份/资金凭据（v4.2 §三 P2：Content Risk）
        #
        # 前四项看的都是「动作是什么」（类型 / 语义角色 / 目标证据 / 页面敏感度），
        # 这一项看的是「动作里带着什么内容」。审核给的那个缺口是准确的：
        # `TYPE` 的 value 是「要写进设备的东西」，而卡号 / 身份证号在文案上
        # 一个危险词都没有，语义层永远认不出它。
        #
        # 两档的原因仍然是「代价不对称」：
        #   - 敏感屏 / 敏感应用里写身份凭据 → DANGEROUS。这正是绑卡、实名、转账收款人
        #     那类页面：写进去就可能被提交，而且不可撤销。
        #   - 普通屏上写 → CAUTION。`TYPE` 的类型下限本来就是 CAUTION，所以这一档
        #     是**不改现状**、只让审计看得见。不去全量转人工的理由：随手把卡号记进
        #     备忘录也该由用户自己决定，全拦会变成噪声，然后被绕过（V3.1 P0-3）。
        content_risk = ActionRisk.SAFE
        if action.type is ActionType.TYPE:
            content_hint = sensitive_content(str(action.value or ""))
            if content_hint:
                screen_hint = _screen_sensitivity_hint(context)
                if _is_sensitive_package(context) or screen_hint:
                    content_risk = ActionRisk.DANGEROUS
                    scope = (
                        f"敏感应用（{context.package if context else ''}）"
                        if _is_sensitive_package(context)
                        else f"敏感屏（命中特征「{screen_hint}」）"
                    )
                    reasons.append(
                        f"输入内容像{content_hint}，且当前是{scope}："
                        "写进去就可能被提交，且不可撤销，转人工确认"
                    )
                else:
                    content_risk = ActionRisk.CAUTION
                    reasons.append(f"输入内容像{content_hint}：写进设备的内容不可撤销，按保守处理")

        return (
            strictest(type_risk, text_risk, evidence_risk, page_risk, content_risk),
            reasons,
            resolved.resolution,
        )

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


# 目标证据缺口的可读解释（V3.1 P1-4）。审计要能区分「我们没读到树」和
# 「树读到了但没有这个节点」——前者是我们自己的观测问题，后者可能是页面已经变了。
_TARGET_GAP_HINTS = {
    target_evidence.TargetResolution.NO_TREE: "本次没有 UI 树，看不到点击落在哪个控件上",
    target_evidence.TargetResolution.PARSE_ERROR: "UI 树存在但解析失败，目标元素无法确认",
    target_evidence.TargetResolution.NOT_FOUND: "UI 树里找不到该目标（页面可能已变）",
}


def _is_sensitive_package(context: RiskContext | None) -> bool:
    if context is None or not context.package:
        return False
    package = context.package.lower()
    return any(marker in package for marker in SENSITIVE_PACKAGE_MARKERS)


def _screen_sensitivity_hint(context: RiskContext | None) -> str:
    """这一屏是不是「敏感屏」（支付确认 / 转账 / 绑卡 / 验证码……）（V4 · Policy Engine）。

    与 `_is_sensitive_package` 的分工：包名是**静态身份**，这个是**动态屏幕内容**——
    普通 App 里也能弹起收银台，支付 App 首页也不一定在敏感操作上。真正该抬级的是
    「这一屏正在做敏感事」。

    返回命中的特征文案（空串 = 不是敏感屏）。纯函数：只读 `context.ui_tree`，
    不破坏门禁无状态的判定性质。
    """
    if context is None or not context.ui_tree:
        return ""
    haystack = context.ui_tree.lower()
    for marker in SENSITIVE_SCREEN_MARKERS:
        if marker in haystack:
            return marker
    return ""
