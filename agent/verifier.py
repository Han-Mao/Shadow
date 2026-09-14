"""多级验证（V2 §十五 · V2.2 §六/§七）：Device → 导航 → 结构 → 目标元素 → VLM，逐级加码。

只用 VLM 判「成功没成功」有三个问题：它贵、慢，而且会为一次根本没生效的点击
编出合理化的理由。所以先在廉价且确定的信号上过一遍，VLM 只负责最后那层语义判断。

V2.2 补掉的两个洞：

1. **不再把「认不出的 VLM 结论」当成功**（§七）。以前 `{"result": "banana"}` 会被
   原样传下去，匹配不上 done / error 之后落到默认分支 `outcome=OK`——
   「模型胡说 = 这步过了」。现在 VLM 必须给出合法枚举，给不出就走「证据不足」分支。
2. **「页面变没变」不再只比可点击 label 集合**（§六）。见 `agent/evidence.py`：
   导航层 / 结构层 / 目标元素层三条独立证据，并且记录结论**依据的是哪一层**。

另一条贯穿全文的原则：**危险动作在缺少独立证据时，绝不按成功处理**。
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from models.action import (
    MUTATING_ACTION_TYPES,
    Action,
    ActionEffectStatus,
    ActionRisk,
)
from models.state import Observation, StepOutcome
from models.verification import ActionDispatch, ActionEffect, DispatchStatus, GoalVerification
from vision import vlm
from vision.vlm import VerifyResult, VlmError

from . import evidence

logger = logging.getLogger(__name__)

__all__ = [
    "MUTATING_ACTION_TYPES",
    "Verification",
    "screen_delta",
    "ui_tree_signal",
    "verify_action",
]


class Verification(BaseModel):
    """一次执行的验证结论。

    `outcome` / `should_retry` / `should_replan` 是给 Runtime 的**处置建议**；
    `dispatch` / `effect` / `goal` 是**事实分层**（V2.1 §十九）——
    前者说「接下来怎么办」，后者说「到底发生了什么」。
    分开之后，「命令没发出去」和「发出去了但没效果」才不会再共用一句「失败」。
    """

    outcome: StepOutcome = StepOutcome.OK
    done: bool = False
    should_retry: bool = False
    should_replan: bool = False
    layer: str = ""
    message: str = ""

    dispatch: ActionDispatch = ActionDispatch()
    effect: ActionEffect = ActionEffect()
    goal: GoalVerification = GoalVerification()


def screen_delta(pre: Observation, post: Observation, action: Action | None = None):
    """比对前后两次观察（`agent.evidence` 的入口，供 Runtime 复用）。"""
    return evidence.screen_delta(pre, post, action)


def ui_tree_signal(pre: Observation, post: Observation) -> str:
    """返回 `changed` / `same` / `unknown`。

    保留这个旧接口给外部调用方，但内部判定已经换成结构指纹（比 label 集合细）。
    """
    delta = evidence.screen_delta(pre, post)
    if not delta.known:
        return "unknown"
    return "changed" if delta.changed else "same"


def verify_action(
    instruction: str,
    pre_observation: Observation,
    action: Action,
    post_observation: Observation,
    result: dict | None = None,
) -> Verification:
    """逐级验证一次执行，并把结论拆成「发出 / 效果 / 目标」三层（V2.1 §十九）。"""
    execution = result if result is not None else (post_observation.result or {})
    delta = evidence.screen_delta(pre_observation, post_observation, action)
    independent = delta.changed or delta.target.is_positive_evidence
    evidence_layer = delta.strongest_layer.value

    # 规划器申请完成：这一步没有动作可发
    if action.is_completion_request:
        return _completion_verification(action, delta, independent, evidence_layer)

    # ---- L1 设备层：动作到底发出去没有 ----
    if not execution.get("ok"):
        error = execution.get("error") or "设备执行失败"
        error_class = str(execution.get("error_class") or "")
        return Verification(
            outcome=StepOutcome.ERROR,
            should_retry=True,
            should_replan=True,
            layer="device",
            message=error,
            dispatch=ActionDispatch(
                action=action,
                status=DispatchStatus.FAILED,
                transport_error=error,
                error_class=error_class,
            ),
            effect=ActionEffect(
                status=ActionEffectStatus.VERIFIED_FAILED, evidence="device", message=error
            ),
            goal=GoalVerification(layer="device", message=error),
        )

    dispatch = ActionDispatch(action=action, status=DispatchStatus.SENT)
    dangerous = action.resolved_risk() is ActionRisk.DANGEROUS

    # ---- L5 VLM 层：语义上是否达成目标（结论必须可解读，否则视为无此证据）----
    verdict, vlm_error = _vlm_verdict(instruction, pre_observation, post_observation, action)
    if vlm_error:
        logger.warning("VLM 验证不可用: %s", vlm_error)

    if verdict is VerifyResult.ERROR:
        return Verification(
            outcome=StepOutcome.ERROR,
            should_replan=True,
            layer="vlm",
            message="VLM 判定页面未进入预期状态",
            # V2.7 P2-2：动作已经发出去、但页面没到预期——这是「被 App 拒绝 / 没达到效果」，
            # 不是设备抖动。给结构化 error_class=action_rejected，让下游重试策略
            # 直接判「换策略」而不是「把同一动作再发一遍」（那只会重复触发副作用）。
            dispatch=ActionDispatch(
                action=action,
                status=DispatchStatus.SENT,
                error_class="action_rejected",
            ),
            effect=ActionEffect(
                status=ActionEffectStatus.VERIFIED_FAILED,
                evidence="vlm",
                changed=delta.changed,
                message="VLM 判定页面未进入预期状态",
            ),
            goal=GoalVerification(layer="vlm", message="VLM 判定页面未进入预期状态"),
        )

    if verdict is VerifyResult.DONE:
        return Verification(
            outcome=StepOutcome.DONE,
            done=True,
            layer="vlm",
            message="VLM 判定任务已完成"
            + ("" if independent else "（无独立证据支持，交由目标验证复核）"),
            dispatch=dispatch,
            effect=ActionEffect(
                status=ActionEffectStatus.VERIFIED_SUCCESS,
                evidence="vlm",
                changed=delta.changed,
            ),
            goal=GoalVerification(
                achieved=True,
                done=True,
                layer="vlm",
                message="VLM 判定任务已完成",
                independent_evidence=independent,
            ),
        )

    # ---- 无独立正向证据时的处置（这里是最容易出事的地方）----
    if not independent:
        return _no_independent_evidence(
            action=action,
            dispatch=dispatch,
            delta=delta,
            verdict=verdict,
            vlm_error=vlm_error,
            dangerous=dangerous,
        )

    # ---- 有独立证据：按最强的那一层记录 ----
    supported_by_vlm = verdict is VerifyResult.OK
    message = "执行成功"
    if supported_by_vlm:
        message = f"VLM 判定执行成功；{delta.describe()}"
    elif vlm_error:
        # VLM 不可用（或结论不可解读），但本地证据独立成立——这不是「默认成功」，
        # 而是「有据可依」，且依据哪一层被明确记录
        message = f"VLM 未提供有效判定（{vlm_error}）；本地独立证据：{delta.describe()}"

    return Verification(
        outcome=StepOutcome.OK,
        layer=evidence_layer if not supported_by_vlm else f"vlm+{evidence_layer}",
        message=message,
        dispatch=dispatch,
        effect=ActionEffect(
            status=ActionEffectStatus.VERIFIED_SUCCESS,
            evidence=evidence_layer,
            changed=delta.changed,
            target=delta.target,
            message=delta.describe(),
        ),
        goal=GoalVerification(
            achieved=True,
            layer=evidence_layer,
            message=delta.describe(),
            independent_evidence=True,
        ),
    )


def _completion_verification(
    action: Action, delta, independent: bool, evidence_layer: str
) -> Verification:
    """「申请完成」动作的验证记录。

    这里**不裁定**任务是否真的完成——那由 `agent.goal_verifier` 用计划、页面推进、
    模型声明的可核验证据独立裁定（V2.2 §四）。本函数只负责如实记下：
    这次申请有没有得到本地证据的支持。
    """
    reason = action.reason or "任务完成"
    return Verification(
        outcome=StepOutcome.DONE,
        done=True,
        layer="planner",
        message=reason + ("" if independent else "（未附独立证据）"),
        dispatch=ActionDispatch(action=action, status=DispatchStatus.SKIPPED),
        effect=ActionEffect(
            status=ActionEffectStatus.VERIFIED_SUCCESS,
            evidence=evidence_layer if independent else "planner",
            changed=delta.changed,
            target=delta.target,
            message=delta.describe(),
        ),
        goal=GoalVerification(
            achieved=True,
            done=True,
            layer="planner",
            message=reason,
            independent_evidence=independent,
        ),
    )


def _no_independent_evidence(
    *,
    action: Action,
    dispatch: ActionDispatch,
    delta,
    verdict: VerifyResult | None,
    vlm_error: str,
    dangerous: bool,
) -> Verification:
    """没有任何独立正向证据时的结论。

    三条路：危险动作绝不放过、会改页面的动作判「效果未知」、其余动作按设备层 ACK 收尾。
    """
    if dangerous:
        # 这是 V2.2 §七 的关键修补：以前 VLM 返回未知值会落到默认分支 outcome=OK，
        # 于是一个「确认付款」可能就这么过了。现在它必须是「效果未知」，
        # 而且要 is_mutating 也不重试——交由 Runtime 在线对账或人工确认。
        return Verification(
            outcome=StepOutcome.OK,
            should_retry=False,
            layer="l1_device",
            message=(
                "动作不可撤销，但没有任何独立证据能确认其效果"
                f"（{vlm_error or 'VLM 判定为 ' + str(verdict and verdict.value)}）"
                "，按「效果未知」处理，需人工确认"
            ),
            dispatch=dispatch,
            effect=ActionEffect(
                status=ActionEffectStatus.EFFECT_UNKNOWN,
                evidence="none",
                changed=delta.changed,
                target=delta.target,
                ambiguous=True,
                message="不可撤销动作缺少独立证据",
            ),
            goal=GoalVerification(
                achieved=False, layer="l1_device", message="效果未经独立确认"
            ),
        )

    if action.is_mutating:
        message = f"UI 树与执行前完全一致，疑似无效操作（{delta.describe()}）"
        if vlm_error:
            message = f"VLM 未提供有效判定（{vlm_error}）；且{message}"
        return Verification(
            outcome=StepOutcome.OK,
            should_retry=True,
            layer="device+evidence",
            message=message,
            dispatch=dispatch,
            effect=ActionEffect(
                status=ActionEffectStatus.EFFECT_UNKNOWN,
                evidence="none",
                changed=False,
                target=delta.target,
                ambiguous=True,
                message="页面无变化且无 VLM 判定，效果不明",
            ),
            goal=GoalVerification(achieved=False, layer="device+evidence"),
        )

    # BACK / HOME / WAIT 这类动作本来就不保证页面变化：
    # 设备层 ACK 已经是能拿到的最强证据，按成功收尾是合理的（不是「默认成功」）
    return Verification(
        outcome=StepOutcome.OK,
        layer="l1_device",
        message=f"设备层确认执行成功（{action.type.value} 不保证页面变化）",
        dispatch=dispatch,
        effect=ActionEffect(
            status=ActionEffectStatus.VERIFIED_SUCCESS,
            evidence="l1_device",
            changed=delta.changed,
            target=delta.target,
        ),
        goal=GoalVerification(achieved=True, layer="l1_device"),
    )


def _vlm_verdict(
    instruction: str, pre: Observation, post: Observation, action: Action
) -> tuple[VerifyResult | None, str]:
    """调用 VLM 并把它返回的东西收敛成严格枚举；不可用时返回 (None, 原因)。

    返回值里的原因字符串会一路进事件流——审计要能回答
    「这步到底是模型说过关，还是本地证据说过关」。
    """
    try:
        raw = vlm.verify_transition(
            pre.screenshot_path, post.screenshot_path, instruction, action.model_dump()
        )
    except VlmError as exc:
        # 包括 VlmVerifyError（结论不可解读）——两者都归为「没有语义证据」
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - VLM 不可用时降级，不能阻塞任务
        return None, f"{type(exc).__name__}: {exc}"

    if isinstance(raw, VerifyResult):
        return raw, ""
    # 兼容测试替身 / 旧调用方直接返回字符串
    try:
        return VerifyResult(str(raw).strip().lower()), ""
    except ValueError:
        return None, f"VLM 返回未知验证结论：{raw!r}"
