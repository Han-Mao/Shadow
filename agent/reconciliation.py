"""动作效果对账（V2.1 §五）。

问题：进程在「动作已 dispatch、还没验证」之间崩溃，恢复时 `action_effect` 是
DISPATCHED → 判 EFFECT_UNKNOWN。此时「上次那个动作到底生效了没有」是未知的。

第一版只实现了一条路——「清空计划重新规划」。它安全，但明显不够：
    - 动作其实**已经生效**（页面已经变了）→ 重规划纯属浪费，还可能把已完成的事再做一遍
    - 动作其实**没生效**      → 应该把这个动作重做一次，而不是换一条路
    - 判断不了              → 应该交给人，而不是自己瞎猜

所以对账要把 EFFECT_UNKNOWN 拆成四条路：

    已成功  → CONTINUE   继续原计划
    未成功  → RETRY      重做该动作（危险动作除外）
    页面不符 → REPLAN     重新规划
    无法判断 → ASK_HUMAN  转人工确认

**关键安全约束**：危险动作（付款/发送/删除）在无法确认成功时，绝不 RETRY。
宁可多问一次人，也不能重复扣款、重复下单。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from models.action import Action, ActionEffectStatus, ActionRisk
from models.checkpoint import Checkpoint
from models.state import Observation
from vision.fingerprint import ui_fingerprint


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


def needs_reconciliation(checkpoint: Checkpoint | None) -> bool:
    """只有「发了但没验证」的恢复点才需要对账。"""
    if checkpoint is None:
        return False
    return checkpoint.action_effect in (
        ActionEffectStatus.DISPATCHED,
        ActionEffectStatus.EFFECT_UNKNOWN,
    )


def reconcile(checkpoint: Checkpoint, observation: Observation) -> ReconcileVerdict:
    """判断上次那个「只 dispatch 未验证」的动作到底生效了没有。

    判据是 L2 结构指纹比对（见 `vision.fingerprint`）：
    恢复点存的是**动作发出前**那一屏的 UI 树，与当前屏比对——
    变了说明动作生效，没变说明没生效。
    """
    action = checkpoint.last_action
    if action is None:
        return ReconcileVerdict(
            ReconcileAction.REPLAN, "恢复点没有记录上次动作，无从对账"
        )

    dangerous = action.resolved_risk() is ActionRisk.DANGEROUS

    # 页面已经切到别的 App：当初那一屏的上下文没了，只能重新规划
    if checkpoint.package and observation.package and observation.package != checkpoint.package:
        return ReconcileVerdict(
            ReconcileAction.REPLAN,
            f"页面已从 {checkpoint.package} 切到 {observation.package}，重新规划",
        )

    # 优先用恢复点存好的结构指纹，省掉把几万字符的 XML 再解析一遍；
    # 老恢复点没有这个字段时才回退到从 ui_snapshot 现算。
    before = checkpoint.screen_fingerprint or ui_fingerprint(checkpoint.ui_snapshot)
    after = ui_fingerprint(observation.ui_tree)

    if not before or not after:
        # 没有可比对的基线。REPLAN 不会重复执行那个存疑的动作，所以是安全的退路；
        # 但危险动作连「换个做法自动继续」都不该由机器决定，必须人来拍板。
        if dangerous:
            return ReconcileVerdict(
                ReconcileAction.ASK_HUMAN,
                "缺少 UI 基线，无法判断危险动作是否生效，需人工确认",
            )
        return ReconcileVerdict(
            ReconcileAction.REPLAN, "缺少 UI 基线，无法判断上次动作是否生效"
        )

    if before != after:
        return ReconcileVerdict(
            ReconcileAction.CONTINUE, "页面结构已变化，判定上次动作已生效，继续原计划"
        )

    # 页面结构完全没变 → 动作没生效
    if dangerous:
        return ReconcileVerdict(
            ReconcileAction.ASK_HUMAN,
            "页面无变化，危险动作可能未生效；为避免重复执行，转人工确认",
        )
    return ReconcileVerdict(
        ReconcileAction.RETRY,
        "页面无变化，判定上次动作未生效，重做该动作",
        retry_action=action,
    )
