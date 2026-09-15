"""人工确认（V2.7 P2-1 拆出的独立职责）。

危险动作确认 / 完成裁定 / 崩溃恢复放行，三类「等人拍板」的入口都在这里。
它们共享同一个 `confirm` 入口，按「等的是什么」分流到三个处理函数。

方法通过 `self` 引用共享能力（`_emit` / `_describe_action` / `_confirm_goal_locked` /
`_confirm_recovery_locked`），用 mixin 拆解，零行为改动。
"""
from __future__ import annotations

import logging

from models.action import Action
from storage.event_log import CONFIRMED

from ._runtime_types import ApprovalGrant

logger = logging.getLogger(__name__)


class ConfirmationMixin:
    """危险动作 / 完成裁定 / 崩溃恢复的人工确认入口。"""

    def pending_confirmation(self, task_id: str) -> Action | None:
        with self._states_lock:
            state = self._states.get(task_id)
            return state.pending_confirmation if state else None

    def recovery_pending(self, task_id: str) -> str | None:
        """这条任务是不是在等「崩溃后状态不可信」的人工处置（V2.6 §七）。

        返回原因是给 API 与审计看的：它不是危险动作、也不是完成裁定，而是
        「上次那个动作到底生效没有，我们判断不了，交给人决定」。
        """
        return self._recovery_reason(task_id)

    def confirm(self, task_id: str, approved: bool) -> bool:
        """人工确认危险动作。批准后该动作会被放行一次。"""
        with self._states_lock:
            return self._confirm_locked(task_id, approved)

    def _confirm_locked(self, task_id: str, approved: bool) -> bool:
        state = self._states.get(task_id)
        if state is None or state.pending_confirmation is None:
            # 等人工裁定「任务是否完成」时，pending_confirmation 是空的（那不是某个动作）
            if state is not None and state.awaiting_goal_decision:
                return self._confirm_goal_locked(task_id, state, approved)
            # V2.6 §七：崩溃恢复门禁也在等人工，它同样不是某个动作
            if self._has_recovery_note(task_id):
                return self._confirm_recovery_locked(task_id, approved)
            return False

        if state.awaiting_goal_decision:
            return self._confirm_goal_locked(task_id, state, approved)

        if not approved:
            denied = state.pending_confirmation
            # V3.1 P0：先留痕，再改动内存状态。写不进去就什么都不做——
            # 「用户否决过某个危险动作」同样是必须可追溯的事实（它解释了后续策略为什么
            # 绕开这个动作），记不下来就别假装收到过。
            failed = self._emit_critical_or(
                task_id,
                CONFIRMED,
                approved=False,
                action=denied.type.value,
                risk=denied.resolved_risk().value,
            )
            if failed is not None:
                logger.error("任务 %s 的否决事件写盘失败，否决不生效：%s", task_id, failed)
                return False
            logger.info("任务 %s 的危险动作被人工否决", task_id)
            state.pending_confirmation = None
            # 记住这个动作被否决过：下次决策再给出它就直接换策略，
            # 否则任务会在「请求确认 → 被否决 → 再次请求确认」之间空转
            state.denied_fingerprints.add(denied.fingerprint)
            state.failed_strategies.append(f"危险动作被人工否决：{self._describe_action(denied)}")
            return True

        pending = state.pending_confirmation
        if pending is None:
            return False
        # V3.1 P0：这条 CONFIRMED 写不进去就**绝不能装出「已批准」**——批准会立刻
        # 放行一个真实副作用，而它没有对应的授权记录，正是「转账了但查不到谁批的」。
        # 严格先写后放行：`state.approval` 只在留痕成功之后才被设置。
        failed = self._emit_critical_or(
            task_id,
            CONFIRMED,
            approved=True,
            action=pending.type.value,
            risk=pending.resolved_risk().value,
            # 记下批准时所在的那一屏，放行时会再比对一次（V3.1 P2-9）
            page="/".join(part for part in state.pending_page if part),
        )
        if failed is not None:
            logger.error("任务 %s 的批准事件写盘失败，放行不生效：%s", task_id, failed)
            return False
        # 记下「批准的到底是哪一个任务、哪一个动作、哪一版目标与计划、哪一次尝试」
        # （V2.7 P0-2）：放行时逐项比对，任务 / 动作 / 目标 / 计划 / 执行序号任一变过
        # 就作废——绝不拿来放行别的危险动作。
        #
        # V3.1 P2-9：再加一层页面绑定。但**只在真的知道「在哪一屏」时才绑**：
        # `_ask_human` / `_settle_failure` 这两条转人工路径手上没有观察，页面是空的；
        # 那时如果拿空页面去算一个「绑定指纹」，放行时必然比对不上，凭据会被永久作废，
        # 任务就卡在「请求确认 → 凭据失效 → 再请求确认」的空转里。
        # 拿不到页面信息时不绑页面，而不是绑一个假页面。
        has_page = any(state.pending_page)
        state.approval = ApprovalGrant(
            task_id=task_id,
            action_fingerprint=pending.fingerprint,
            page_bound_fingerprint=(
                pending.page_bound_fingerprint(*state.pending_page) if has_page else ""
            ),
            task_version=state.pending_task_version,
            plan_version=state.pending_plan_version,
            attempt_seq=state.attempt_seq,
        )
        return True

    def _confirm_recovery_locked(self, task_id: str, approved: bool) -> bool:
        """人工处理「崩溃后状态不可信」（V2.6 §七）。

        与另外两种确认的区别：危险动作否决 = 放弃那个动作；完成裁定否决 = 接着做；
        而这里的批准 / 否决都不改变「要做什么」，只回答一个问题——
        **上次那个动作能不能当成没生效、放心重跑**。

        批准 = 人确认过没有不可挽回的副作用（TaskManager 会顺手清掉旧计划重新规划）；
        否决 = 不再自动重跑，落 DEGRADED 等人工处置。两边都不在这里改任务状态：
        状态统一由 TaskManager 落，避免又出现「两处各写一半」。
        """
        # V3.1 P0：同样是「先落盘、再生效」。清掉恢复待办 = 批准重跑，
        # 而重跑可能产生第二条消息 / 第二笔订单——没有裁决记录就不能放行。
        failed = self._emit_critical_or(
            task_id, CONFIRMED, approved=approved, action="recovery", risk="", reason="recovery"
        )
        if failed is not None:
            logger.error("任务 %s 的恢复裁定事件写盘失败，裁定不生效：%s", task_id, failed)
            return False
        self._clear_recovery_note(task_id)
        logger.info("任务 %s 的崩溃恢复待办已被人工处理（approved=%s）", task_id, approved)
        return True

    def forget(self, task_id: str) -> None:
        with self._states_lock:
            self._states.pop(task_id, None)
            self._clear_recovery_note(task_id)
