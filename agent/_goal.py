"""完成申请与裁定（V2.7 P2-1 拆出的独立职责）。

`AgentRuntime` 原来把「模型申请完成 → GoalVerifier 独立裁定 → 人工兜底」整条链路
堆在主文件里。这是「怎么判定任务做完」的完整子域，独立成 mixin 之后，
`runtime.py` 只剩执行循环骨架与设备/持久化基础设施。

方法之间通过 `self` 互相引用（`self._emit` / `self._close_step` / `self._finish` /
`self._persist`），所以用 mixin 拆而不是抽成独立对象——共享同一实例，零行为改动。
"""
from __future__ import annotations

import logging

from models.state import Observation
from models.task import Task, TaskStatus
from models.task_step import StepStatus, TaskStep
from storage.event_log import CONFIRMED, GOAL_CONFIRMED, GOAL_REJECTED, GOAL_REQUESTED, WAITING

from . import goal_verifier
from ._runtime_types import RunOutcome, RuntimeState

logger = logging.getLogger(__name__)


class GoalControllerMixin:
    """完成申请与裁定的完整子域。"""

    def _request_finish(
        self,
        task: Task,
        state: RuntimeState,
        observation: Observation,
        action,
        step: TaskStep | None,
    ):
        """处理一次「申请完成」：交给 GoalVerifier 用独立证据裁定。

        返回 None 表示申请被驳回、任务应当继续跑（调用方 `continue`）。
        """
        pending_steps = sum(1 for item in task.plan if item.status is StepStatus.PENDING)

        if state.goal_approved_by_human:
            check = goal_verifier.GoalCheck(
                goal_verifier.GoalVerdict.CONFIRMED, "人工已认定任务完成"
            )
        else:
            check = goal_verifier.verify_goal(
                action=action,
                observation=observation,
                pending_steps=pending_steps,
                executed_steps=state.execution_step,
                page_seen_changed=state.page_seen_changed,
                # 严格度按任务画像自动选（V2.2 §六）：纯查询宽松、导航与副作用严格。
                # 所以必须把任务原文传进去，否则无法判断这是哪一类任务。
                instruction=task.instruction,
                context=task.context,
            )

        state.last_goal_check = check
        self._emit(
            task.id,
            GOAL_REQUESTED,
            reason=action.reason,
            pending_steps=pending_steps,
            executed_steps=state.execution_step,
            page_seen_changed=state.page_seen_changed,
            rejections=state.goal_rejections,
            mode=check.mode,
            profile=check.profile,
        )

        if not check.blocks_completion:
            self._emit(
                task.id,
                GOAL_CONFIRMED,
                verdict=check.verdict.value,
                reason=check.reason,
                mode=check.mode,
                profile=check.profile,
                layer=check.independent_evidence and "l6_goal" or "planner",
            )
            self._close_step(task, step)
            self._finish(task, state, observation, action)
            return RunOutcome.DONE

        # ---- 拿到反证：打回继续做 ----
        state.goal_rejections += 1
        self._emit(
            task.id,
            GOAL_REJECTED,
            reason=check.reason,
            checks=check.checks,
            mode=check.mode,
            profile=check.profile,
            rejections=state.goal_rejections,
        )
        logger.warning(
            "任务 %s 的完成申请被驳回（第 %d 次）：%s",
            task.id,
            state.goal_rejections,
            check.reason,
        )
        if state.goal_rejections > goal_verifier.MAX_GOAL_REJECTIONS:
            state.awaiting_goal_decision = True
            task.mark(TaskStatus.WAITING, source="runtime")
            self._persist(task)
            logger.warning(
                "任务 %s 完成申请连续被驳回 %d 次，转人工裁定", task.id, state.goal_rejections
            )
            self._emit(
                task.id,
                WAITING,
                reason="goal_unverified",
                action="done",
                risk="",
                detail=(
                    f"完成申请连续 {state.goal_rejections} 次未通过独立验证，"
                    "既不能确认也无法自行推翻，交给人裁定"
                ),
            )
            return RunOutcome.AWAITING_CONFIRMATION

        state.failed_strategies.append(f"完成申请被驳回：{check.reason}")
        # 下一轮必须走 Re-plan：常规决策会看到同一屏、又给出同一个 DONE
        state.pending_replan_reason = f"你声称任务已完成，但独立验证不通过：{check.reason}"
        return None

    def last_goal_check(self, task_id: str):
        """最近一次完成裁定的结论（供 API / 审计查询）。"""
        with self._states_lock:
            state = self._states.get(task_id)
            return state.last_goal_check if state else None

    def is_goal_decision(self, task_id: str) -> bool:
        """当前等待人工处理的，是不是「任务算不算完成」这件事（而非某个危险动作）。

        API 需要用这个区分两种确认的语义：危险动作被否决 = 放弃该动作；
        完成申请被否决 = 任务还没完，继续做。
        """
        with self._states_lock:
            state = self._states.get(task_id)
            return bool(state and state.awaiting_goal_decision)

    def _confirm_goal_locked(self, task_id: str, state: RuntimeState, approved: bool) -> bool:
        """人工裁定「任务是否算完成」。

        这里的否决语义与危险动作**完全不同**：危险动作被否决 = 放弃那个动作；
        完成申请被否决 = 「还没做完，接着做」。所以否决时不拉黑任何指纹，
        而是给下一轮塞一个 Re-plan 理由（V2.2 §四）。
        """
        state.awaiting_goal_decision = False
        state.pending_confirmation = None
        if approved:
            logger.info("任务 %s 的完成申请被人工批准", task_id)
            state.goal_approved_by_human = True
            self._emit(task_id, CONFIRMED, approved=True, action="done", risk="", reason="goal")
            return True

        logger.info("任务 %s 的完成申请被人工否决，任务继续执行", task_id)
        state.goal_approved_by_human = False
        state.goal_rejections = 0
        state.pending_replan_reason = "人工确认任务尚未完成，请基于当前页面继续实际执行"
        state.failed_strategies.append("人工否决了完成申请：任务尚未完成")
        self._emit(
            task_id,
            CONFIRMED,
            approved=False,
            action="done",
            risk="",
            reason="goal",
        )
        return True
