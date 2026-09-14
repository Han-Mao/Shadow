"""动作效果对账（V2.1 §五 · V2.2 §三/§六）。

问题：动作已经发出去了，但「它到底生效了没有」是未知的——进程在这一瞬间崩溃，
或者重新观察失败拿不到新截图。此时 `action_effect` 是 DISPATCHED / EFFECT_UNKNOWN。

对账要把这个未知拆成四条路：

    已成功  → CONTINUE   继续原计划
    未成功  → RETRY      重做该动作（危险动作除外）
    页面不符 → REPLAN     重新规划
    无法判断 → ASK_HUMAN  转人工确认

**关键安全约束**：危险动作（付款/发送/删除）在无法确认成功时，绝不 RETRY，
也不接受「页面变了」这种弱证据。宁可多问一次人，也不能重复扣款、重复下单。

V2.2 把判据从「UI 结构指纹变没变」升级成多级证据（`agent.evidence`）：

    同 package 内 activity 变了  → 强证据，动作确实生效
    结构指纹变了                → 一般证据（普通动作够了；危险动作还不够）
    目标元素消失/文本变了        → 强证据
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from models.action import Action, ActionEffectStatus, ActionRisk
from models.checkpoint import Checkpoint
from models.state import Observation
from vision.fingerprint import ui_fingerprint
from vision.target import target_state

from . import evidence


class ReconcileAction(str, Enum):
    CONTINUE = "continue"
    """已确认生效：接着原计划跑，不要重规划。"""

    RETRY = "retry"
    """确认没生效：把这个动作再执行一次。"""

    REPLAN = "replan"
    """页面已经不是当初那一屏，或没有动作可对账：重新规划。"""

    ASK_HUMAN = "ask_human"
    """自己判断不了：转人工确认（HITL）。"""


@dataclass(frozen=True)
class ReconcileVerdict:
    action: ReconcileAction
    reason: str
    retry_action: Action | None = None
    """仅 RETRY 时有值：要重做的那个动作。"""

    layer: str = ""
    """结论依据的是哪一层证据（V2.2 §六）。"""


def needs_reconciliation(checkpoint: Checkpoint | None) -> bool:
    """只有「发了但没验证」的恢复点才需要对账。"""
    if checkpoint is None:
        return False
    return checkpoint.action_effect in (
        ActionEffectStatus.DISPATCHED,
        ActionEffectStatus.EFFECT_UNKNOWN,
    )


def reconcile(
    checkpoint: Checkpoint,
    observation: Observation,
    *,
    action: Action | None = None,
    online: bool = False,
) -> ReconcileVerdict:
    """判断上次那个「只 dispatch 未验证」的动作到底生效了没有。

    ``online=True`` 用于**执行途中**的对账：动作是自己刚发出的，中间只隔了一次失败的
    观察，所以「页面切到了另一个 App」恰恰是动作生效的强证据。
    恢复路径（默认 False）不能这么推断——进程死了多久、期间发生过什么都不知道，
    页面变了只能说明「当初那一屏的上下文没了」，必须重新规划。
    """
    action = action if action is not None else checkpoint.last_action
    if action is None:
        return ReconcileVerdict(
            ReconcileAction.REPLAN, "恢复点没有记录上次动作，无从对账"
        )

    dangerous = action.resolved_risk() is ActionRisk.DANGEROUS
    delta = _delta_against(checkpoint, observation, action)

    # ---- 危险动作走单独一条通道：只认「明确的成功标志」（V2.2 §六）----
    #
    # 审核指出的假设问题：
    #   点击付款 → 打开支付 Activity → 旧逻辑判「page changed → CONTINUE」
    #   但那只能说明**进入了支付流程**，不等于付款成功。
    # 所以危险动作不看「页面变没变」（对这类动作它是弱证据），
    # 只看页面上有没有出现「支付成功 / 已发送」这种终态文案；没有就转人工。
    if dangerous:
        marker = evidence.success_evidence(observation.ui_tree)
        if marker:
            return ReconcileVerdict(
                ReconcileAction.CONTINUE,
                f"页面出现明确成功标志「{marker}」，判定不可撤销动作已生效",
                layer="l5_success_marker",
            )
        return ReconcileVerdict(
            ReconcileAction.ASK_HUMAN,
            "不可撤销动作：页面变化（含跳转）只能说明进入了下一屏，"
            "不能证明副作用已经成功，且页面上没有出现明确的成功标志，转人工确认",
            layer=delta.strongest_layer.value,
        )

    package_changed = bool(
        checkpoint.package and observation.package and observation.package != checkpoint.package
    )
    if package_changed:
        if online:
            # 就在我们眼皮底下切了 App，除了刚发的那个动作没有别的原因
            return ReconcileVerdict(
                ReconcileAction.CONTINUE,
                f"页面已从 {checkpoint.package} 切到 {observation.package}，"
                "判定上次动作已生效",
                layer="l2_navigation",
            )
        return ReconcileVerdict(
            ReconcileAction.REPLAN,
            f"页面已从 {checkpoint.package} 切到 {observation.package}，重新规划",
            layer="l2_navigation",
        )

    # 同 package 内 activity 变了 → 动作确实让页面跳转了
    if checkpoint.activity and observation.activity and observation.activity != checkpoint.activity:
        return ReconcileVerdict(
            ReconcileAction.CONTINUE,
            f"页面已从 {checkpoint.activity} 跳转到 {observation.activity}，判定上次动作已生效",
            layer="l2_navigation",
        )

    # 拿不到可比对的基线
    if not delta.known:
        return ReconcileVerdict(
            ReconcileAction.REPLAN, "缺少 UI 基线，无法判断上次动作是否生效"
        )

    # 目标元素自己变了（消失 / 文本变化）——比「整棵树变了」更贴近意图
    if delta.target.is_positive_evidence:
        return ReconcileVerdict(
            ReconcileAction.CONTINUE,
            f"目标元素{delta.target.value}，判定上次动作已生效",
            layer="l4_target",
        )

    if delta.changed:
        return ReconcileVerdict(
            ReconcileAction.CONTINUE,
            f"页面结构已变化（{delta.strongest_layer.value}），判定上次动作已生效，继续原计划",
            layer=delta.strongest_layer.value,
        )

    return ReconcileVerdict(
        ReconcileAction.RETRY,
        "页面无变化，判定上次动作未生效，重做该动作",
        retry_action=action,
        layer="l3_structure",
    )


def _delta_against(checkpoint: Checkpoint, observation: Observation, action: Action):
    """比对「动作发出前那一屏」与「现在这一屏」。

    刻意不用 `evidence.screen_delta(checkpoint→observation)`：恢复点里存的是
    **截断过的** UI 快照（`UI_SNAPSHOT_LIMIT`），重新算指纹会与当初存下的
    `screen_fingerprint` 不一致，把「同一屏」误判成「变了」。
    所以基线指纹优先用当初算好的那一份，快照只用于目标元素定位。
    """
    before_fp = fingerprint_of(checkpoint)
    after_fp = ui_fingerprint(observation.ui_tree)
    known = bool(before_fp and after_fp)
    state = target_state(
        action,
        checkpoint.ui_snapshot,
        observation.ui_tree,
        observation.screen_size,
    )
    return evidence.ScreenDelta(
        navigation_changed=bool(
            (checkpoint.activity and observation.activity and checkpoint.activity != observation.activity)
            or (checkpoint.package and observation.package and checkpoint.package != observation.package)
        ),
        structural_changed=known and before_fp != after_fp,
        target=state,
        fingerprint_before=before_fp,
        fingerprint_after=after_fp,
        known=known,
    )


def fingerprint_of(checkpoint: Checkpoint) -> str:
    """恢复点的结构指纹（优先用它存好的，老恢复点才现算）。"""
    return checkpoint.screen_fingerprint or ui_fingerprint(checkpoint.ui_snapshot)
