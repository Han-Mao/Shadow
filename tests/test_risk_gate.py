"""Risk Gate（V2.2 §一 / §八）：策略风险与模型风险的合议，以及上下文对风险的抬升。

这一组用例锁定的是审核点名的那条底线：

> 模型在 `action.risk` 上的声明只是建议，不能把危险动作降为 safe。

以及 V2.2 新增的能力：**风险判定要看得到 UI 树**。否则
「点击红色按钮」这种描述永远查不出来它其实是「立即购买」。
"""
from __future__ import annotations

from agent.risk_gate import ActionRiskGate, RiskContext
from models.action import Action, ActionRisk, ActionType, Point
from models.state import Observation

# 坐标落在「立即购买」这个按钮内（bounds 100,200 → 300,300）
UI_WITH_BUY = (
    '<hierarchy>'
    '<node class="android.widget.Button" text="立即购买" bounds="[100,200][300,300]" '
    'clickable="true" resource-id="com.taobao:id/buy"/>'
    "</hierarchy>"
)


def tap_at(x: float, y: float, **kwargs) -> Action:
    return Action(type=ActionType.TAP, target=Point(x=x, y=y), **kwargs)


# ---------------------------------------------------------------- 策略 vs 模型


def test_model_hint_cannot_downgrade_policy_risk():
    """模型写 risk_hint=safe，不能把「确认付款」说成安全。"""
    assessment = ActionRiskGate.assess(
        Action(type=ActionType.TAP, target="确认付款", risk_hint=ActionRisk.SAFE)
    )

    assert assessment.policy is ActionRisk.DANGEROUS
    assert assessment.model is ActionRisk.SAFE
    assert assessment.effective is ActionRisk.DANGEROUS
    assert assessment.downgrade_blocked, "模型试图降级必须被记录，审计要看得见"
    assert assessment.requires_confirmation


def test_model_hint_can_raise_policy_risk():
    """反过来是允许的：模型可以把风险说高（要求更严格的确认）。"""
    assessment = ActionRiskGate.assess(
        tap_at(1, 1, risk_hint=ActionRisk.DANGEROUS)
    )

    assert assessment.policy is ActionRisk.CAUTION
    assert assessment.effective is ActionRisk.DANGEROUS
    assert not assessment.downgrade_blocked


def test_silence_is_not_a_downgrade_attempt():
    """模型没表态 ≠ 模型说 safe。

    分不清这两者的话，每个动作都会被记成「试图降级」，告警立刻变噪声。
    """
    assessment = ActionRiskGate.assess(tap_at(1, 1))

    assert assessment.model is ActionRisk.SAFE
    assert not assessment.downgrade_blocked
    assert assessment.effective is ActionRisk.CAUTION


def test_policy_and_model_risk_are_separately_readable():
    """两个来源必须在数据上分得开——否则审计答不出「这级风险是谁定的」。"""
    action = Action(type=ActionType.TAP, target=Point(x=1, y=1), risk_hint=ActionRisk.CAUTION)

    assert action.policy_risk() is ActionRisk.CAUTION
    assert action.model_risk() is ActionRisk.CAUTION
    assert action.resolved_risk() is ActionRisk.CAUTION


# ---------------------------------------------------------------- 上下文抬升风险


def test_target_node_text_reveals_the_real_action():
    """动作描述里一个危险词都没有，但它点的其实是「立即购买」。

    这正是只包一层 `resolved_risk()` 时漏掉的场景——信息在 UI 树里，不在动作里。
    """
    action = tap_at(200, 250, reason="点击红色按钮")
    context = RiskContext(ui_tree=UI_WITH_BUY, screen_size=(1000, 2000))

    assert ActionRiskGate.assess(action).effective is ActionRisk.CAUTION, "没有上下文时看不出来"

    assessment = ActionRiskGate.assess(action, context=context)
    assert assessment.effective is ActionRisk.DANGEROUS
    assert any("立即购买" in reason for reason in assessment.reasons)


def test_normalized_coordinates_resolve_the_same_node():
    """归一化坐标（0~1）与像素坐标必须解析到同一个元素，否则风险会算到别人头上。"""
    action = tap_at(0.2, 0.125)  # 1000x2000 屏幕上的 (200, 250)
    assessment = ActionRiskGate.assess(
        action, context=RiskContext(ui_tree=UI_WITH_BUY, screen_size=(1000, 2000))
    )

    assert assessment.effective is ActionRisk.DANGEROUS


def test_sensitive_app_raises_the_floor_but_not_to_dangerous():
    """支付类 App 里会改页面的动作至少 CAUTION，但不直接判危险——否则什么都拦不住。"""
    assessment = ActionRiskGate.assess(
        tap_at(1, 1), context=RiskContext(package="com.eg.android.AlipayGphone")
    )

    assert assessment.effective is ActionRisk.CAUTION
    assert any("敏感" in reason for reason in assessment.reasons)


def test_task_instruction_does_not_inflate_risk():
    """任务原文不参与关键词匹配。

    否则「帮我在淘宝下单」这个任务里**每一个**点击都会变成 DANGEROUS，
    门禁随即失效：什么都拦 = 什么都不拦。
    """
    assessment = ActionRiskGate.assess(
        tap_at(1, 1), context=RiskContext(instruction="帮我在淘宝下单买手机")
    )

    assert assessment.effective is ActionRisk.CAUTION


def test_broken_ui_tree_degrades_to_text_policy():
    """UI 树坏了不该让门禁整体失效，退化为「只看动作文本」。"""
    action = Action(type=ActionType.TAP, value="发送", target=Point(x=1, y=1))
    assessment = ActionRiskGate.assess(
        action, context=RiskContext(ui_tree="<hierarchy><node", screen_size=(1000, 2000))
    )

    assert assessment.effective is ActionRisk.DANGEROUS


# ---------------------------------------------------------------- 其它


def test_completion_request_is_safe():
    """「申请完成」不碰设备，不该因为敏感页面被判成需要确认。"""
    for action_type in (ActionType.DONE, ActionType.DONE_REQUEST):
        assessment = ActionRiskGate.assess(
            Action(type=action_type), context=RiskContext(package="com.eg.android.AlipayGphone")
        )
        assert assessment.effective is ActionRisk.SAFE


def test_risk_context_from_observation():
    observation = Observation(
        step=1,
        screenshot_path="/tmp/a.png",
        package="com.android.settings",
        activity=".Main",
        ui_tree=UI_WITH_BUY,
        screen_size=(1000, 2000),
    )

    context = RiskContext.from_observation(observation, instruction="改设置")

    assert context.package == "com.android.settings"
    assert context.activity == ".Main"
    assert context.ui_tree == UI_WITH_BUY
    assert context.instruction == "改设置"
