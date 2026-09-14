"""效果对账（V2.7 P2-1 拆出的独立职责）。

「动作发出去了但效果未知」时，用下一次观察把上次动作对掉——是「接着跑」「重做」
「换策略」还是「问人」。这是独立于主循环的一段完整逻辑（V2.2 §三 / §五）。

方法通过 `self` 引用共享能力（`_emit` / `_load_checkpoint` / `_ask_human` /
`_plan_from_scratch`），用 mixin 拆解，零行为改动。
"""
from __future__ import annotations

import logging

from models.action import ActionEffectStatus
from models.checkpoint import Checkpoint
from models.state import Observation
from models.task import Task
from storage.event_log import RECONCILED

from . import reconciliation
from ._runtime_types import RunOutcome, RuntimeState

logger = logging.getLogger(__name__)

# 同一个任务最多做几次「效果未知」对账。对账会重做动作，无限对账等于无限重做，
# 所以给一个上界，超了就交给人（V2.2 §三）。
MAX_EFFECT_RECONCILIATIONS = 2


class ReconcilerMixin:
    """把「效果未知」的动作对掉。"""

    def _settle_reconciliation(
        self,
        task: Task,
        state: RuntimeState,
        checkpoint: Checkpoint,
        observation: Observation,
        *,
        online: bool,
    ):
        """对账一次「效果未知」的动作，返回 None 表示继续跑。

        `online=True` 表示这是**执行途中**的对账：动作是自己刚刚发出的，
        中间只隔了一次失败的观察，所以「页面切到别的 App」恰恰是动作生效的强证据。
        恢复路径（False）不能这么推断——进程死了多久、期间发生了什么都不知道，
        页面变了只能说明「上下文没了」，必须重新规划。
        """
        if (
            online
            and checkpoint.last_action is not None
            and state.effect_reconciliations >= MAX_EFFECT_RECONCILIATIONS
        ):
            return self._ask_human(
                task,
                state,
                checkpoint.last_action,
                reason="effect_unknown",
                message=(
                    f"连续 {state.effect_reconciliations} 次无法确认动作效果，"
                    "为避免重复执行，转人工确认"
                ),
            )

        settled = reconciliation.reconcile(checkpoint, observation, online=online)
        logger.warning("任务 %s 对账结果 %s：%s", task.id, settled.action.value, settled.reason)
        self._emit(
            task.id,
            RECONCILED,
            verdict=settled.action.value,
            reason=settled.reason,
            layer=settled.layer,
            attempt_id=checkpoint.action_attempt_id,
            online=online,
        )

        if settled.action is reconciliation.ReconcileAction.CONTINUE:
            # V2.3：Activity/结构变化只能证明「页面动了」，不等于原始动作的 side effect
            # 已经成立。按证据强度区分 effect 状态，避免把导航直接当成动作成功。
            if settled.layer == "l5_success_marker":
                state.last_action_effect = ActionEffectStatus.VERIFIED_SUCCESS
            elif settled.layer == "l2_navigation":
                state.last_action_effect = ActionEffectStatus.NAVIGATED
            else:
                state.last_action_effect = ActionEffectStatus.UI_CHANGED
            state.pending_effect_reconcile = False
            logger.info("任务 %s 确认上次动作已生效（%s），从恢复点继续", task.id, settled.layer)
            return None

        if settled.action is reconciliation.ReconcileAction.RETRY:
            action = settled.retry_action or checkpoint.last_action
            if action is not None and not action.is_safe_to_retry():
                # V2.7 P1-2：「重做」只对**重做等价**的动作安全。发消息 / 点赞 / 提交表单
                # 这类非幂等动作、以及支付 / 删除这类不可逆动作，页面没变并不代表没生效——
                # 重做就是第二条消息、第二笔订单。此时唯一正确的答案是问人。
                return self._ask_human(
                    task,
                    state,
                    action,
                    reason="effect_unknown",
                    message=(
                        f"动作（{action.type.value}）效果未知，且副作用类型为 "
                        f"{action.side_effect().value}——重做可能重复产生副作用，转人工确认"
                    ),
                )
            # 保留原计划，下一轮直接重做这个动作（不惊动模型）
            state.forced_action = settled.retry_action
            state.pending_effect_reconcile = False
            if online:
                state.effect_reconciliations += 1
            return None

        if settled.action is reconciliation.ReconcileAction.ASK_HUMAN:
            return self._ask_human(
                task, state, checkpoint.last_action, reason="effect_unknown", message=settled.reason
            )

        # REPLAN：目标没变，但现在在哪儿不清楚 → 清空计划重新规划
        state.pending_effect_reconcile = False
        state.last_action_effect = ActionEffectStatus.EFFECT_UNKNOWN
        task.plan = []
        self._plan_from_scratch(task, state, observation)
        return None

    def _reconcile_pending_effect(self, task: Task, state: RuntimeState, observation: Observation):
        """在线对账入口：上一个动作效果未知，用刚拿到的这一屏把它对掉。"""
        if not state.pending_effect_reconcile:
            return None
        state.pending_effect_reconcile = False
        checkpoint = self._load_checkpoint(task)
        if checkpoint is None or not reconciliation.needs_reconciliation(checkpoint):
            # 没有可用的基线（例如 checkpoint 没落盘）→ 不猜，交给人
            return self._ask_human(
                task,
                state,
                None,
                reason="effect_unknown",
                message="上一个动作效果未知，但缺少可对账的恢复点",
            )
        return self._settle_reconciliation(task, state, checkpoint, observation, online=True)
