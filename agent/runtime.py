"""AgentRuntime（V2 §九）：只回答「怎么完成这个任务」。

调度不在这一层——排队、抢占、恢复由 `agent.scheduler.TaskScheduler` 负责。
Runtime 只做 Observe → Think → Act → Verify → Checkpoint 的闭环，
并在每个循环安全点检查「是否被取消 / 是否该让出设备」。
"""
from __future__ import annotations

import logging
import os
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from device.session import DeviceBusyError, DeviceSession
from models.action import Action, ActionType, ActionRisk, ActionEffectStatus, Decision
from models.checkpoint import Checkpoint
from models.retry import (
    DEFAULT_POLICY,
    ErrorClass,
    RetryAction,
    classify_error,
)
from models.state import Observation, StepOutcome
from models.task import Task, TaskStatus
from models.task_step import StepStatus, TaskStep

from . import executor, observer, planner, reconciliation, verifier
from .planner import ReplanContext

logger = logging.getLogger(__name__)

# 与 api/server.py 读同一个环境变量，否则 /screenshot 与 Agent 截图会落到不同目录
ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "artifacts/shots"))

# 重试行为只由这一份策略决定（V2.1 §十二），不再在这里硬编码次数
RETRY_POLICY = DEFAULT_POLICY
# 最近 LOOP_WINDOW 个动作里，同一个指纹出现 LOOP_REPEAT_THRESHOLD 次即判定死循环
LOOP_WINDOW = 4
LOOP_REPEAT_THRESHOLD = 3


class RunOutcome(str, Enum):
    DONE = "done"
    FAILED = "failed"
    SUSPENDED = "suspended"
    """被抢占或用户暂停，任务保留状态等待恢复。"""

    CANCELLED = "cancelled"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    """撞上危险动作，等人确认（HITL）。"""


@dataclass
class RuntimeState:
    """单个任务的运行时状态。不落盘——恢复靠 Checkpoint，不靠这份内存。

    V2.1 §二：预算拆成三个互相独立的计数，不再用一个 step_index 包打天下。
    - execution_step   ：真正执行了多少个 Agent Action（Observe / DONE 都不算）
    - observation_count：观察了多少次（观察失败也消耗）
    - model_call_count ：调用了多少次 VLM
    """

    execution_step: int = 0
    observation_count: int = 0
    model_call_count: int = 0
    last_action_effect: ActionEffectStatus = ActionEffectStatus.NOT_STARTED

    retry_count: int = 0
    prepared: bool = False
    forced_action: Action | None = None
    """对账判定「上次动作未生效」后要强制重做的动作（跳过一次模型决策）。"""

    recent_actions: deque[Action] = field(default_factory=lambda: deque(maxlen=LOOP_WINDOW))
    failed_strategies: list[str] = field(default_factory=list)
    denied_fingerprints: set[str] = field(default_factory=set)
    pending_confirmation: Action | None = None
    approved_dangerous: bool = False


class AgentRuntime:
    def __init__(
        self,
        session: DeviceSession,
        *,
        artifact_dir: str | Path = ARTIFACT_DIR,
        trajectory: object | None = None,
        checkpoints: object | None = None,
        task_store: object | None = None,
    ) -> None:
        self._session = session
        self._artifact_dir = Path(artifact_dir)
        self._trajectory = trajectory
        self._checkpoints = checkpoints
        self._task_store = task_store
        self._states: dict[str, RuntimeState] = {}

    # ---- 对外 ----

    def run(self, task: Task) -> RunOutcome:
        """把一个任务跑到结束、失败或被挂起。"""
        if task.status is TaskStatus.CANCELLED:
            # 调度器已经取消了它，不要用 mark(RUNNING) 把状态又改回去
            logger.info("任务 %s 已取消，跳过执行", task.id)
            return RunOutcome.CANCELLED

        state = self._state_for(task)
        task.mark(TaskStatus.RUNNING)
        self._persist(task)

        checkpoint = self._load_checkpoint(task)
        observation: Observation | None = None
        last_observation: Observation | None = None

        while True:
            # ---- 安全点 ----
            if task.status is TaskStatus.CANCELLED:
                logger.info("任务 %s 已被取消，退出执行", task.id)
                return RunOutcome.CANCELLED
            if task.status is TaskStatus.PAUSED:
                self._save_checkpoint(task, state, last_observation)
                return RunOutcome.SUSPENDED
            if self._session.should_yield(task.id):
                self._save_checkpoint(task, state, last_observation)
                logger.info("任务 %s 让出设备（被抢占），等待稍后恢复", task.id)
                return RunOutcome.SUSPENDED
            if state.observation_count >= task.budget.max_observations:
                self._save_checkpoint(task, state, last_observation)
                return self._fail(task, f"已达观察次数上限 {task.budget.max_observations}")
            if state.model_call_count >= task.budget.max_model_calls:
                self._save_checkpoint(task, state, last_observation)
                return self._fail(task, f"已达模型调用上限 {task.budget.max_model_calls}")

            # ---- Observe ----
            observation = self._observe(task, state)
            if observation is None:
                # 观察失败基本都是设备/ADB 抖动，按瞬时错误处理
                outcome = self._settle_failure(task, state, ErrorClass.TRANSIENT, "观察失败")
                if outcome is not None:
                    return outcome
                continue
            last_observation = observation

            if not state.prepared:
                prepared_outcome = self._prepare(task, state, observation, checkpoint)
                state.prepared = True
                if prepared_outcome is not None:
                    return prepared_outcome

            # ---- Think ----
            step = task.next_pending_step()
            decision = self._forced_decision(state) or self._decide(task, state, observation, step)
            if decision is None:
                outcome = self._settle_failure(
                    task, state, ErrorClass.PARSE_ERROR, "规划失败，无法确定下一步"
                )
                if outcome is not None:
                    return outcome
                continue

            action = decision.action

            if action.type is ActionType.DONE:
                self._close_step(task, step)
                self._finish(task, state, observation, action)
                return RunOutcome.DONE

            # ---- 死循环检测：换策略，而不是继续重试 ----
            if self._note_action(state, action) or action.fingerprint in state.denied_fingerprints:
                reason = (
                    "该动作已被人工否决，必须换一种做法"
                    if action.fingerprint in state.denied_fingerprints
                    else "连续多次执行同一个动作，疑似死循环"
                )
                logger.warning("任务 %s %s", task.id, reason)
                state.failed_strategies.append(self._describe_action(action))
                state.recent_actions.clear()
                retried = self._replan(task, state, observation, step, reason)
                if retried is None:
                    outcome = self._settle_failure(
                        task, state, ErrorClass.PARSE_ERROR, "换策略失败"
                    )
                    if outcome is not None:
                        return outcome
                    continue
                decision, action = retried, retried.action
                if action.type is ActionType.DONE:
                    self._close_step(task, step)
                    self._finish(task, state, observation, action)
                    return RunOutcome.DONE

            # ---- 危险动作门禁（HITL）----
            if action.resolved_risk() is ActionRisk.DANGEROUS and not state.approved_dangerous:
                state.pending_confirmation = action
                self._save_checkpoint(task, state, observation)
                task.mark(TaskStatus.WAITING)
                self._persist(task)
                logger.warning("任务 %s 命中危险动作，等待人工确认：%s", task.id, action.type.value)
                return RunOutcome.AWAITING_CONFIRMATION

            # ---- Act ----
            state.execution_step += 1
            if state.execution_step > task.budget.max_action_steps:
                self._save_checkpoint(task, state, observation, action)
                return self._fail(task, f"已达动作步数上限 {task.budget.max_action_steps}")
            # 命令已发出（executor 永不抛异常，成功与否看 result），但还没验证页面变化。
            # 这中间若进程崩溃，恢复时就会落入 EFFECT_UNKNOWN（V2.1 §五）。
            state.last_action_effect = ActionEffectStatus.DISPATCHED
            result = self._execute(task, action, observation)
            if state.approved_dangerous:
                state.approved_dangerous = False
                state.pending_confirmation = None

            # ---- Verify ----
            post, verification = self._verify(task, state, observation, action, result)
            self._append_trajectory(task, post)

            if verification.outcome is StepOutcome.DONE:
                state.last_action_effect = ActionEffectStatus.VERIFIED_SUCCESS
                self._close_step(task, step)
                self._finish(task, state, post, action)
                return RunOutcome.DONE

            if verification.outcome is StepOutcome.OK:
                state.last_action_effect = ActionEffectStatus.VERIFIED_SUCCESS
                if decision.step_done:
                    self._close_step(task, step, action)
                state.retry_count = 0
                state.failed_strategies.clear()
                self._save_checkpoint(task, state, post, action)
                continue

            # ---- ERROR：先分类，再由策略决定重试 / 换策略 / 找人 / 放弃 ----
            state.last_action_effect = ActionEffectStatus.VERIFIED_FAILED
            state.failed_strategies.append(self._describe_action(action))
            if step is not None:
                step.record_failure(verification.message)
            error_class = classify_error(verification.message)
            logger.warning(
                "任务 %s 第 %d 步失败[%s]：%s",
                task.id,
                state.execution_step,
                error_class.value,
                verification.message,
            )
            self._save_checkpoint(task, state, post, action)
            outcome = self._settle_failure(
                task, state, error_class, verification.message, pending=action
            )
            if outcome is not None:
                return outcome
            continue

    def pending_confirmation(self, task_id: str) -> Action | None:
        state = self._states.get(task_id)
        return state.pending_confirmation if state else None

    def confirm(self, task_id: str, approved: bool) -> bool:
        """人工确认危险动作。批准后该动作会被放行一次。"""
        state = self._states.get(task_id)
        if state is None or state.pending_confirmation is None:
            return False
        if not approved:
            logger.info("任务 %s 的危险动作被人工否决", task_id)
            denied = state.pending_confirmation
            state.pending_confirmation = None
            # 记住这个动作被否决过：下次决策再给出它就直接换策略，
            # 否则任务会在「请求确认 → 被否决 → 再次请求确认」之间空转
            state.denied_fingerprints.add(denied.fingerprint)
            state.failed_strategies.append(f"危险动作被人工否决：{self._describe_action(denied)}")
            return True
        state.approved_dangerous = True
        return True

    def forget(self, task_id: str) -> None:
        self._states.pop(task_id, None)

    # ---- 阶段 ----

    def _state_for(self, task: Task) -> RuntimeState:
        state = self._states.get(task.id)
        if state is None:
            state = RuntimeState()
            self._states[task.id] = state
        return state

    def _prepare(
        self,
        task: Task,
        state: RuntimeState,
        observation: Observation,
        checkpoint: Checkpoint | None,
    ) -> RunOutcome | None:
        """首次进入循环时决定：接着旧计划跑、重做上次动作、找人确认，还是重新规划。

        有返回值时表示任务应当就此挂起（等人确认），调用方直接把它当作 run 的结论。
        """
        if task.plan and checkpoint is not None and self._checkpoints is not None:
            verdict = self._checkpoints.validate(checkpoint, observation, task_version=task.version)
            if verdict.value != "resume":
                logger.info("恢复点已失效（%s），任务 %s 重新规划", verdict.value, task.id)
                task.plan = []
            elif reconciliation.needs_reconciliation(checkpoint):
                # 上次动作「已 dispatch 未验证」→ EFFECT_UNKNOWN。
                # 先对账再决定，而不是一律清空计划重规划（V2.1 §五）。
                state.last_action_effect = ActionEffectStatus.EFFECT_UNKNOWN
                settled = reconciliation.reconcile(checkpoint, observation)
                logger.warning(
                    "任务 %s 对账结果 %s：%s", task.id, settled.action.value, settled.reason
                )
                if settled.action is reconciliation.ReconcileAction.CONTINUE:
                    state.last_action_effect = ActionEffectStatus.VERIFIED_SUCCESS
                    logger.info(
                        "任务 %s 确认上次动作已生效，从恢复点继续（%s）",
                        task.id,
                        task.plan_progress(),
                    )
                    return None
                if settled.action is reconciliation.ReconcileAction.RETRY:
                    # 保留原计划，下一轮直接重做这个动作（不惊动模型）
                    state.forced_action = settled.retry_action
                    return None
                if settled.action is reconciliation.ReconcileAction.ASK_HUMAN:
                    state.pending_confirmation = checkpoint.last_action
                    task.mark(TaskStatus.WAITING)
                    self._persist(task)
                    logger.warning("任务 %s 无法判断上次动作是否生效，等待人工确认", task.id)
                    return RunOutcome.AWAITING_CONFIRMATION
                task.plan = []
            else:
                logger.info("任务 %s 从恢复点继续（%s）", task.id, task.plan_progress())
                return None

        if task.plan:
            return None

        try:
            state.model_call_count += 1
            goals = planner.generate_plan(
                task.instruction, observation.screenshot_path, observation.ui_tree
            )
        except Exception as exc:  # noqa: BLE001 - 计划只是提示，失败不阻塞执行
            logger.warning("生成计划失败，按无计划执行: %s", exc)
            goals = []

        task.set_plan(goals)
        self._persist(task)
        logger.info("任务 %s 计划：%s", task.id, task.plan_progress())

    def _observe(self, task: Task, state: RuntimeState, suffix: str = "") -> Observation | None:
        state.observation_count += 1
        try:
            return observer.observe(
                self._session.controller, self._artifact_dir, state.observation_count, suffix=suffix
            )
        except Exception as exc:  # noqa: BLE001 - 设备抖动不能穿透到 API
            logger.warning("任务 %s 第 %d 次观察失败: %s", task.id, state.observation_count, exc)
            return None

    def _decide(
        self,
        task: Task,
        state: RuntimeState,
        observation: Observation,
        step: TaskStep | None,
    ) -> Decision | None:
        history = self._prompt_context(task)
        try:
            state.model_call_count += 1
            return planner.plan_next_action(
                task.instruction,
                observation.screenshot_path,
                observation.ui_tree,
                history,
                task.plan,
                step,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("任务 %s 规划失败: %s", task.id, exc)
            return self._replan(task, state, observation, step, f"规划失败: {exc}")

    def _replan(
        self,
        task: Task,
        state: RuntimeState,
        observation: Observation,
        step: TaskStep | None,
        reason: str,
    ) -> Decision | None:
        context = ReplanContext(
            task=task.instruction,
            current_step_goal=step.goal if step else "",
            previous_action=step.last_action if step else None,
            failure_reason=reason,
            failed_strategies=list(state.failed_strategies),
        )
        state.model_call_count += 1
        try:
            decision = planner.replan(
                context, observation.screenshot_path, observation.ui_tree, self._prompt_context(task)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("任务 %s Re-plan 失败: %s", task.id, exc)
            return None

        state.failed_strategies.append(self._describe_action(decision.action))
        return decision

    def _execute(self, task: Task, action: Action, observation: Observation) -> dict:
        try:
            with self._session.owned(task.id) as device:
                return executor.execute(device, action, observation.ui_tree)
        except DeviceBusyError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 执行器已兜底，这里防的是会话层意外
            return {"ok": False, "error": f"执行异常 {type(exc).__name__}: {exc}"}

    def _verify(
        self,
        task: Task,
        state: RuntimeState,
        pre: Observation,
        action: Action,
        result: dict,
    ) -> tuple[Observation, verifier.Verification]:
        post = self._observe(task, state, suffix="post")
        if post is None:
            # 动作已经发出去了，只是拿不到新截图：降级为「未验证的 OK」，
            # 否则一次设备抖动会被当成执行失败，触发无谓的重发。
            pre.action = action
            pre.result = result
            pre.status = StepOutcome.OK
            pre.message = "执行成功，但重新观察失败（未验证）"
            return pre, verifier.Verification(
                outcome=StepOutcome.OK, layer="device", message=pre.message
            )

        post.action = action
        post.result = result
        verdict = verifier.verify_action(task.instruction, pre, action, post, result)
        post.status = verdict.outcome
        post.message = verdict.message
        return post, verdict

    # ---- 步骤与收尾 ----

    @staticmethod
    def _close_step(task: Task, step: TaskStep | None, action: Action | None = None) -> None:
        if step is None:
            return
        step.mark(StepStatus.DONE)
        if action is not None:
            step.record_action(action)
        task.sync_current_step()

    def _finish(
        self, task: Task, state: RuntimeState, observation: Observation | None, action: Action
    ) -> None:
        for step in task.plan:
            if step.status is not StepStatus.SKIPPED:
                step.mark(StepStatus.DONE)
        task.sync_current_step()
        self._save_checkpoint(task, state, observation)
        task.mark(TaskStatus.DONE)
        self._persist(task)
        logger.info("任务 %s 完成：%s", task.id, action.reason or "模型判定已完成")

    def _fail(self, task: Task, reason: str) -> RunOutcome:
        """统一的失败收口：**必须同时改任务状态**。

        只返回 RunOutcome.FAILED 而不动 task.status，任务会永远停在 running；
        虽然调度器那边也会兜底标记，但 Runtime 自身不能依赖调用方补齐。
        """
        logger.warning("任务 %s 判定失败：%s", task.id, reason)
        task.mark(TaskStatus.FAILED)
        self._persist(task)
        return RunOutcome.FAILED

    # ---- 失败结算（V2.1 §十二 / §十三）----

    def _settle_failure(
        self,
        task: Task,
        state: RuntimeState,
        error_class: ErrorClass,
        message: str,
        *,
        pending: Action | None = None,
    ) -> RunOutcome | None:
        """按统一策略结算一次失败：返回 None 表示继续循环，否则是终止结论。

        重试预算只在**这一处**递增，不再有 RuntimeState（原 MAX=3）与
        TaskStep（原 max_retries=2）两套计数互相打架。
        """
        decision = RETRY_POLICY.decide(error_class, state.retry_count)
        state.retry_count += 1
        logger.info(
            "任务 %s 失败结算：%s → %s（第 %d 次）",
            task.id,
            error_class.value,
            decision.action.value,
            state.retry_count,
        )
        if decision.action is RetryAction.ABORT:
            return self._fail(task, f"{message}：{decision.reason}")
        if decision.action is RetryAction.ASK_HUMAN:
            if pending is not None:
                state.pending_confirmation = pending
            task.mark(TaskStatus.WAITING)
            self._persist(task)
            logger.warning("任务 %s 无法自行决断，转人工确认：%s", task.id, decision.reason)
            return RunOutcome.AWAITING_CONFIRMATION
        # RETRY / REPLAN 的具体动作由所在阶段执行，这里只记账与放行
        return None

    @staticmethod
    def _forced_decision(state: RuntimeState) -> Decision | None:
        """取出对账要求强制重做的动作（取完即清，避免无限重做同一个动作）。"""
        if state.forced_action is None:
            return None
        action = state.forced_action
        state.forced_action = None
        return Decision(action=action, step_done=False, thought="重试上次未生效的动作")

    # ---- 辅助 ----

    def _note_action(self, state: RuntimeState, action: Action) -> bool:
        """记录动作并判断是否陷入循环。

        比较用 `Action.is_same_as`（带容差）而不是指纹字符串：
        指纹把坐标量化到固定网格，坐标跨网格边界时会漏判。
        """
        state.recent_actions.append(action)
        recent = list(state.recent_actions)
        if len(recent) < LOOP_REPEAT_THRESHOLD:
            return False
        latest = recent[-1]
        return sum(1 for item in recent if item.is_same_as(latest)) >= LOOP_REPEAT_THRESHOLD

    @staticmethod
    def _describe_action(action: Action) -> str:
        target = action.target.model_dump(mode="json") if hasattr(action.target, "model_dump") else action.target
        return f"{action.type.value} target={target} value={action.value!r}"

    def _load_checkpoint(self, task: Task) -> Checkpoint | None:
        if self._checkpoints is None or not task.checkpoint_id:
            return None
        return self._checkpoints.load(task.id, task.checkpoint_id)

    def _save_checkpoint(
        self, task: Task, state: RuntimeState, observation: Observation | None, action: Action | None = None
    ) -> None:
        if self._checkpoints is None:
            return
        checkpoint = Checkpoint.capture(
            task_id=task.id,
            step=state.execution_step,
            step_states=task.step_states(),
            observation=observation,
            history_tail=self._trajectory.tail(task.id, 5) if self._trajectory else [],
            task_version=task.version,
            action_effect=state.last_action_effect,
            last_action=action,
        )
        self._checkpoints.save(checkpoint)
        task.checkpoint_id = checkpoint.id

    def _append_trajectory(self, task: Task, observation: Observation) -> None:
        if self._trajectory is not None:
            self._trajectory.append(task.id, observation)

    def _prompt_context(self, task: Task) -> list[dict]:
        if self._trajectory is None:
            return []
        return self._trajectory.prompt_context(task.id, 5)

    def _persist(self, task: Task) -> None:
        if self._task_store is None:
            return
        try:
            self._task_store.save(task)
        except Exception:  # noqa: BLE001 - 持久化失败不应中断执行
            logger.exception("保存任务 %s 失败", task.id)
