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
        with self._states_lock:
            return self._recovery_notes.get(task_id)

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
            if task_id in self._recovery_notes:
                return self._confirm_recovery_locked(task_id, approved)
            return False

        if state.awaiting_goal_decision:
            return self._confirm_goal_locked(task_id, state, approved)

        if not approved:
            logger.info("任务 %s 的危险动作被人工否决", task_id)
            denied = state.pending_confirmation
            state.pending_confirmation = None
            # 记住这个动作被否决过：下次决策再给出它就直接换策略，
            # 否则任务会在「请求确认 → 被否决 → 再次请求确认」之间空转
            state.denied_fingerprints.add(denied.fingerprint)
            state.failed_strategies.append(f"危险动作被人工否决：{self._describe_action(denied)}")
            self._emit(
                task_id,
                CONFIRMED,
                approved=False,
                action=denied.type.value,
                risk=denied.resolved_risk().value,
            )
            return True
        pending = state.pending_confirmation
        if pending is None:
            return False
        # 记下「批准的到底是哪一个任务、哪一个动作、哪一版目标与计划、哪一次尝试」
        # （V2.7 P0-2）：放行时逐项比对，任务 / 动作 / 目标 / 计划 / 执行序号任一变过
        # 就作废——绝不拿来放行别的危险动作。
        state.approval = ApprovalGrant(
            task_id=task_id,
            action_fingerprint=pending.fingerprint,
            task_version=state.pending_task_version,
            plan_version=state.pending_plan_version,
            attempt_seq=state.attempt_seq,
        )
        self._emit(
            task_id,
            CONFIRMED,
            approved=True,
            action=pending.type.value,
            risk=pending.resolved_risk().value,
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
        self._recovery_notes.pop(task_id, None)
        logger.info("任务 %s 的崩溃恢复待办已被人工处理（approved=%s）", task_id, approved)
        self._emit(
            task_id, CONFIRMED, approved=approved, action="recovery", risk="", reason="recovery"
        )
        return True

    def forget(self, task_id: str) -> None:
        with self._states_lock:
            self._states.pop(task_id, None)
            self._recovery_notes.pop(task_id, None)
