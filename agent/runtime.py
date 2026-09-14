"""AgentRuntime（V2 §九）：只回答「怎么完成这个任务」。

调度不在这一层——排队、抢占、恢复由 `agent.scheduler.TaskScheduler` 负责。
Runtime 只做 Observe → Think → Act → Verify → Checkpoint 的闭环，
并在每个循环安全点检查「是否被取消 / 是否该让出设备」。
"""
from __future__ import annotations

import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from device.pool import DevicePool, storage_hint
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
from models.verification import ActionDispatch, ActionEffect, DispatchStatus, GoalVerification
from storage.event_log import (
    ACTION_DISPATCHED,
    ACTION_VERIFIED,
    CHECKPOINT_SAVED,
    CONFIRMED,
    DONE,
    EFFECT_UNKNOWN,
    FAILED,
    GOAL_CONFIRMED,
    GOAL_REJECTED,
    GOAL_REQUESTED,
    RECONCILED,
    RISK_ASSESSED,
    STARTED,
    SUSPENDED,
    WAITING,
    EventLog,
)

from . import executor, goal_verifier, observer, planner, reconciliation, verifier
from .planner import ReplanContext
from .risk_gate import ActionRiskGate, RiskContext

logger = logging.getLogger(__name__)

# 与 api/server.py 读同一个环境变量，否则 /screenshot 与 Agent 截图会落到不同目录
ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "artifacts/shots"))

# 重试行为只由这一份策略决定（V2.1 §十二），不再在这里硬编码次数
RETRY_POLICY = DEFAULT_POLICY
# 最近 LOOP_WINDOW 个动作里，同一个指纹出现 LOOP_REPEAT_THRESHOLD 次即判定死循环
LOOP_WINDOW = 4
LOOP_REPEAT_THRESHOLD = 3
# 同一个任务最多做几次「效果未知」对账。对账会重做动作，无限对账等于无限重做，
# 所以给一个上界，超了就交给人（V2.2 §三）。
MAX_EFFECT_RECONCILIATIONS = 2


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

    page_seen_changed: bool = False
    """本次执行期间页面是否**真的**推进过（导航或结构变化）。

    V2.2 §四：GoalVerifier 判「任务是否真的完成」时，这是最要紧的一条独立证据——
    如果整个任务跑下来页面一次都没变，却声称完成，就该被质疑。
    """

    pending_effect_reconcile: bool = False
    """上一个动作发出了但效果未知，下一个安全点要拿新观察去对账（V2.2 §三）。"""

    effect_reconciliations: int = 0
    """对账次数。对账可能重做动作，所以必须有上界。"""

    goal_rejections: int = 0
    """完成申请被独立验证驳回了几次。连续驳回会转人工，避免昂贵空转。"""

    awaiting_goal_decision: bool = False
    """正在等人裁定「任务是否算完成」（完成申请被驳回太多次之后的兜底）。"""

    goal_approved_by_human: bool = False
    """人工已经认定任务完成：下一次完成申请直接放行，不再走自动验证。"""

    pending_replan_reason: str = ""
    """下一轮 Think 必须走 Re-plan 而不是常规决策的原因（如完成申请被驳回）。"""

    last_goal_check: object | None = None
    """最近一次完成裁定的结论（`goal_verifier.GoalCheck`），供 API / 审计查询。"""

    retry_count: int = 0
    prepared: bool = False
    forced_action: Action | None = None
    """对账判定「上次动作未生效」后要强制重做的动作（跳过一次模型决策）。"""

    attempt_seq: int = 0
    current_attempt_id: str | None = None
    """当前动作尝试的唯一 id（V2.1 §二十一）：同一步骤的每次重试都不同。"""

    recent_actions: deque[Action] = field(default_factory=lambda: deque(maxlen=LOOP_WINDOW))
    failed_strategies: list[str] = field(default_factory=list)
    denied_fingerprints: set[str] = field(default_factory=set)
    pending_confirmation: Action | None = None
    approved_dangerous: bool = False


class AgentRuntime:
    def __init__(
        self,
        session: DeviceSession | DevicePool,
        *,
        artifact_dir: str | Path = ARTIFACT_DIR,
        trajectory: object | None = None,
        checkpoints: object | None = None,
        task_store: object | None = None,
        event_log: EventLog | None = None,
    ) -> None:
        # 向后兼容：老调用方传单个 DeviceSession（单设备场景）
        self._pool = session if isinstance(session, DevicePool) else DevicePool([session])
        self._default_session = self._pool.first()
        self._artifact_dir = Path(artifact_dir)
        self._trajectory = trajectory
        self._checkpoints = checkpoints
        self._task_store = task_store
        self._event_log = event_log
        self._states: dict[str, RuntimeState] = {}
        # 多设备 = 多个 worker 线程并发调用 runtime，运行时状态必须加锁
        self._states_lock = threading.RLock()

    def _session_for(self, task: Task) -> DeviceSession:
        """按任务绑定的设备取会话（V2.1 §十三）。

        刻意**不做缓存**：多设备下 runtime 会被多个 worker 线程并发调用，
        缓存就是共享可变状态，而共享状态正是并发 bug 的来源。
        从池里按 serial 查一下的成本可以忽略。
        """
        return self._pool.get(task.device_serial) or self._default_session

    # ---- 对外 ----

    def run(self, task: Task) -> RunOutcome:
        """把一个任务跑到结束、失败或被挂起。"""
        if task.status is TaskStatus.CANCELLED:
            # 调度器已经取消了它，不要用 mark(RUNNING) 把状态又改回去
            logger.info("任务 %s 已取消，跳过执行", task.id)
            return RunOutcome.CANCELLED

        state = self._state_for(task)
        # 本任务跑在哪台设备上，由绑定决定——多设备时 worker 线程各跑各的，
        # 所以这个 session 只作为**局部变量**贯穿本次 run，不进实例状态
        session = self._session_for(task)
        task.mark(TaskStatus.RUNNING)
        self._persist(task)
        self._emit(task.id, STARTED, version=task.version, instruction=task.instruction)

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
            if session.should_yield(task.id):
                self._save_checkpoint(task, state, last_observation)
                logger.info("任务 %s 让出设备（被抢占），等待稍后恢复", task.id)
                self._emit(task.id, SUSPENDED, reason="preemption", step=state.execution_step)
                return RunOutcome.SUSPENDED
            if state.observation_count >= task.budget.max_observations:
                self._save_checkpoint(task, state, last_observation)
                return self._fail(task, f"已达观察次数上限 {task.budget.max_observations}")
            if state.model_call_count >= task.budget.max_model_calls:
                self._save_checkpoint(task, state, last_observation)
                return self._fail(task, f"已达模型调用上限 {task.budget.max_model_calls}")

            # ---- Observe ----
            observation = self._observe(task, state, session)
            if observation is None:
                # 观察失败基本都是设备/ADB 抖动，按瞬时错误处理
                outcome = self._settle_failure(task, state, ErrorClass.TRANSIENT, "观察失败")
                if outcome is not None:
                    return outcome
                continue
            last_observation = observation

            # ---- 未知效果对账（V2.2 §三）----
            # 上一步动作发出去了、但拿不到验证观察（EFFECT_UNKNOWN）。
            # 现在手上正好有一次新观察，就地把它对掉，而不是等进程崩溃后再补救。
            unresolved = self._reconcile_pending_effect(task, state, observation)
            if unresolved is not None:
                return unresolved

            if not state.prepared:
                prepared_outcome = self._prepare(task, state, observation, checkpoint)
                state.prepared = True
                if prepared_outcome is not None:
                    return prepared_outcome

            # ---- Think ----
            step = task.next_pending_step()
            decision = self._think(task, state, observation, step)
            if decision is None:
                outcome = self._settle_failure(
                    task, state, ErrorClass.PARSE_ERROR, "规划失败，无法确定下一步"
                )
                if outcome is not None:
                    return outcome
                continue

            action = decision.action

            # ---- 完成申请：模型说 done 只是申请，最终由 GoalVerifier 裁定（V2.2 §四）----
            if action.is_completion_request:
                outcome = self._request_finish(task, state, observation, action, step)
                if outcome is not None:
                    return outcome
                continue

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
                if action.is_completion_request:
                    outcome = self._request_finish(task, state, observation, action, step)
                    if outcome is not None:
                        return outcome
                    continue

            # ---- 危险动作门禁（HITL）----
            # 风险判定必须带上下文：模型完全可以把一次「点击立即购买按钮」描述成
            # 「点击红色按钮」，只有查 UI 树才知道那个控件到底叫什么（V2.2 §一）
            assessment = ActionRiskGate.assess(
                action, context=RiskContext.from_observation(observation, instruction=task.instruction)
            )
            if assessment.downgrade_blocked or assessment.effective is ActionRisk.DANGEROUS:
                self._emit(
                    task.id,
                    RISK_ASSESSED,
                    action=action.type.value,
                    effective=assessment.effective.value,
                    policy=assessment.policy.value,
                    model=assessment.model.value,
                    downgrade_blocked=assessment.downgrade_blocked,
                    reason=assessment.describe(),
                    target=self._action_target(action),
                )

            if assessment.requires_confirmation and not state.approved_dangerous:
                state.pending_confirmation = action
                self._save_checkpoint(task, state, observation)
                task.mark(TaskStatus.WAITING)
                self._persist(task)
                logger.warning("任务 %s 命中危险动作，等待人工确认：%s", task.id, action.type.value)
                self._emit(
                    task.id,
                    WAITING,
                    reason="dangerous_action",
                    action=action.type.value,
                    risk=assessment.effective.value,
                    why=assessment.describe(),
                )
                return RunOutcome.AWAITING_CONFIRMATION

            # ---- Act ----
            state.execution_step += 1
            if state.execution_step > task.budget.max_action_steps:
                self._save_checkpoint(task, state, observation, action)
                return self._fail(task, f"已达动作步数上限 {task.budget.max_action_steps}")
            # 命令已发出（executor 永不抛异常，成功与否看 result），但还没验证页面变化。
            # 这中间若进程崩溃，恢复时就会落入 EFFECT_UNKNOWN（V2.1 §五）。
            state.attempt_seq += 1
            state.current_attempt_id = f"{task.id}:a{state.attempt_seq}"
            state.last_action_effect = ActionEffectStatus.DISPATCHED
            self._emit(
                task.id,
                ACTION_DISPATCHED,
                attempt_id=state.current_attempt_id,
                action=action.type.value,
                risk=assessment.effective.value,
                step=state.execution_step,
                # 带上动作细节，事件流才「自足」到可以回放（V2.1 §二十三）：
                # 只记动作类型的话，回放时看不出它当时点在哪、输入了什么
                fingerprint=action.fingerprint,
                target=self._action_target(action),
                value=action.value,
                reason=action.reason,
            )
            result = self._execute(task, action, observation, session)
            if state.approved_dangerous:
                state.approved_dangerous = False
                state.pending_confirmation = None
            # 人工的完成认定只对「紧接着的那次完成申请」有效，
            # 一旦又去执行新动作，就说明任务其实还没结束
            state.goal_approved_by_human = False

            # ---- Verify ----
            post, verification = self._verify(task, state, observation, action, result, session)
            self._append_trajectory(task, post)
            self._emit(
                task.id,
                ACTION_VERIFIED,
                attempt_id=state.current_attempt_id,
                outcome=verification.outcome.value,
                dispatch=verification.dispatch.status.value,
                effect=verification.effect.status.value,
                target=verification.effect.target.value,
                goal_achieved=verification.goal.achieved,
                layer=verification.layer,
                message=verification.message,
                # 截图路径进事件流，回放时能直接点开看当时那一屏
                screenshot=post.screenshot_path,
            )

            # 效果由验证层判定（V2.1 §十九），Runtime 不再自己拍脑袋。
            # 关键差异：VLM 说「成功」但 UI 树没变时，这里落的是 EFFECT_UNKNOWN 而不是
            # VERIFIED_SUCCESS——下一轮若崩溃，恢复时会走对账而不是盲目续跑。
            state.last_action_effect = verification.effect.status

            # 页面真的推进过吗？这是完成裁定时最要紧的独立证据（V2.2 §四）
            if verification.effect.changed or verification.effect.target.is_positive_evidence:
                state.page_seen_changed = True

            if verification.outcome is StepOutcome.DONE:
                # VLM 在验证阶段直接判定完成，同样只是一次「申请」，仍要过目标验证
                outcome = self._request_finish(task, state, post, action, step)
                if outcome is not None:
                    return outcome
                continue

            if verification.outcome is StepOutcome.OK:
                if verification.effect.status is ActionEffectStatus.EFFECT_UNKNOWN:
                    # 「发出去了但不知道效果」——在线就该对账，而不是当成功继续（V2.2 §三）
                    state.pending_effect_reconcile = True
                    self._emit(
                        task.id,
                        EFFECT_UNKNOWN,
                        action=action.type.value,
                        reason=verification.message,
                        target=self._action_target(action),
                    )
                if decision.step_done:
                    self._close_step(
                        task,
                        step,
                        action,
                        effect=verification.effect.status,
                        layer=verification.layer,
                    )
                state.retry_count = 0
                state.failed_strategies.clear()
                self._save_checkpoint(task, state, post, action)
                continue

            # ---- ERROR：先分类，再由策略决定重试 / 换策略 / 找人 / 放弃 ----
            state.failed_strategies.append(self._describe_action(action))
            error_class = classify_error(verification.message)
            if step is not None:
                # 把动作、错误分类、效果、证据层一起写进 attempt（V2.1 §二十）：
                # 只记一句错误文本的话，事后查不出「试的是什么动作、属于哪类错误」
                step.record_failure(
                    verification.message,
                    action=action,
                    error_class=error_class,
                    effect=verification.effect.status,
                    layer=verification.layer,
                )
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
        with self._states_lock:
            state = self._states.get(task_id)
            return state.pending_confirmation if state else None

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
        state.approved_dangerous = True
        self._emit(
            task_id,
            CONFIRMED,
            approved=True,
            action=pending.type.value if pending else "",
            risk=pending.resolved_risk().value if pending else "",
        )
        return True

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

    def forget(self, task_id: str) -> None:
        with self._states_lock:
            self._states.pop(task_id, None)

    def _emit(self, task_id: str, kind: str, **data) -> None:
        if self._event_log is not None:
            self._event_log.emit(task_id, kind, **data)

    # ---- 阶段 ----

    def _state_for(self, task: Task) -> RuntimeState:
        # 多设备下每台设备的 worker 都会走这里，必须加锁
        with self._states_lock:
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
                resolved = self._settle_reconciliation(
                    task, state, checkpoint, observation, online=False
                )
                if resolved is not None:
                    return resolved
            else:
                logger.info("任务 %s 从恢复点继续（%s）", task.id, task.plan_progress())
                return None

        if task.plan:
            return None

        self._plan_from_scratch(task, state, observation)
        return None

    def _plan_from_scratch(
        self, task: Task, state: RuntimeState, observation: Observation
    ) -> None:
        """生成一份全新计划（首次执行、恢复点失效、对账要求重规划都会走到这里）。"""
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

    # ---- 未知效果对账（V2.2 §三）----

    def _settle_reconciliation(
        self,
        task: Task,
        state: RuntimeState,
        checkpoint: Checkpoint,
        observation: Observation,
        *,
        online: bool,
    ) -> RunOutcome | None:
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
            state.last_action_effect = ActionEffectStatus.VERIFIED_SUCCESS
            state.pending_effect_reconcile = False
            logger.info("任务 %s 确认上次动作已生效，从恢复点继续", task.id)
            return None

        if settled.action is reconciliation.ReconcileAction.RETRY:
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

    def _reconcile_pending_effect(
        self, task: Task, state: RuntimeState, observation: Observation
    ) -> RunOutcome | None:
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

    def _ask_human(
        self, task: Task, state: RuntimeState, action: Action | None, *, reason: str, message: str
    ) -> RunOutcome:
        """把无法自行决断的事交给人，并把任务停在 WAITING。"""
        state.pending_confirmation = action
        task.mark(TaskStatus.WAITING)
        self._persist(task)
        logger.warning("任务 %s 转人工确认：%s", task.id, message)
        self._emit(
            task.id,
            WAITING,
            reason=reason,
            action=action.type.value if action else "",
            risk=action.resolved_risk().value if action else "",
            detail=message,
        )
        return RunOutcome.AWAITING_CONFIRMATION

    # ---- 完成申请与目标验证（V2.2 §四）----

    def _request_finish(
        self,
        task: Task,
        state: RuntimeState,
        observation: Observation,
        action: Action,
        step: TaskStep | None,
    ) -> RunOutcome | None:
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
            task.mark(TaskStatus.WAITING)
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

    def last_goal_check(self, task_id: str) -> object | None:
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
        logger.info("任务 %s 计划：%s", task.id, task.plan_progress())

    def _observe(
        self,
        task: Task,
        state: RuntimeState,
        session: DeviceSession,
        suffix: str = "",
    ) -> Observation | None:
        state.observation_count += 1
        try:
            # 产物按设备分目录（V2.2 §七）：多设备共用一个目录时，
            # 两台设备的截图会互相覆盖、证据无法归属，Checkpoint / Replay / 审计
            # 都会指到错的那一张图上。
            return observer.observe(
                session.controller,
                storage_hint(self._artifact_dir, session.serial),
                state.observation_count,
                suffix=suffix,
            )
        except Exception as exc:  # noqa: BLE001 - 设备抖动不能穿透到 API
            logger.warning("任务 %s 第 %d 次观察失败: %s", task.id, state.observation_count, exc)
            return None

    def _think(
        self,
        task: Task,
        state: RuntimeState,
        observation: Observation,
        step: TaskStep | None,
    ) -> Decision | None:
        """决定下一步做什么。

        三条通道按优先级：对账指定的强制动作 → 必须换策略的 Re-plan → 常规决策。
        顺序不能换：强制动作是「已确认上一次没生效，重做」，再让模型自由发挥就白对了。
        """
        forced = self._forced_decision(state)
        if forced is not None:
            return forced

        if state.pending_replan_reason:
            reason = state.pending_replan_reason
            state.pending_replan_reason = ""
            return self._replan(task, state, observation, step, reason)

        return self._decide(task, state, observation, step)

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

    def _execute(
        self, task: Task, action: Action, observation: Observation, session: DeviceSession
    ) -> dict:
        try:
            with session.owned(task.id) as device:
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
        session: DeviceSession,
    ) -> tuple[Observation, verifier.Verification]:
        post = self._observe(task, state, session, suffix="post")
        if post is None:
            # 动作已经发出去了，只是拿不到新截图 —— 这是 V2.2 §三 的核心修补点。
            #
            # 旧实现把它降级成「未验证的 OK」直接放行，于是「点击发送」之后
            # 观察失败会被当成成功，下一轮模型看到页面还没变，就再点一次 → 重复发送。
            # 现在它被明确记为 **EFFECT_UNKNOWN**（效果未知），并交给在线对账处理：
            # 下一步拿到新观察后，比对该动作发出前的页面结构，判定「生效 / 未生效 / 找人」。
            pre.action = action
            pre.result = result
            pre.status = StepOutcome.OK
            pre.message = "动作已发出，但重新观察失败：效果未知"
            logger.warning(
                "任务 %s 动作已发出但重新观察失败，效果记为未知：%s",
                task.id,
                action.type.value,
            )
            return pre, verifier.Verification(
                outcome=StepOutcome.OK,
                should_retry=False,
                layer="l1_device",
                message=pre.message,
                dispatch=ActionDispatch(action=action, status=DispatchStatus.SENT),
                effect=ActionEffect(
                    status=ActionEffectStatus.EFFECT_UNKNOWN,
                    evidence="none",
                    changed=False,
                    ambiguous=True,
                    message="拿不到验证观察，无法判断动作是否生效",
                ),
                goal=GoalVerification(
                    achieved=False, layer="l1_device", message="效果未经确认"
                ),
            )

        post.action = action
        post.result = result
        verdict = verifier.verify_action(task.instruction, pre, action, post, result)
        post.status = verdict.outcome
        post.message = verdict.message
        return post, verdict

    # ---- 步骤与收尾 ----

    @staticmethod
    def _close_step(
        task: Task,
        step: TaskStep | None,
        action: Action | None = None,
        *,
        effect: ActionEffectStatus | None = None,
        layer: str = "",
    ) -> None:
        if step is None:
            return
        step.mark(StepStatus.DONE)
        if action is not None:
            step.record_action(
                action,
                effect=effect if effect is not None else ActionEffectStatus.VERIFIED_SUCCESS,
                layer=layer,
            )
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
        check = state.last_goal_check
        self._emit(
            task.id,
            DONE,
            steps=state.execution_step,
            reason=action.reason,
            # 完成这件事也必须可追溯：谁判的、凭什么、有没有独立证据（V2.2 §四）
            goal_verdict=getattr(getattr(check, "verdict", None), "value", ""),
            goal_reason=getattr(check, "reason", ""),
            goal_independent_evidence=bool(getattr(check, "independent_evidence", False)),
            goal_rejections=state.goal_rejections,
        )

    def _fail(self, task: Task, reason: str) -> RunOutcome:
        """统一的失败收口：**必须同时改任务状态**。

        只返回 RunOutcome.FAILED 而不动 task.status，任务会永远停在 running；
        虽然调度器那边也会兜底标记，但 Runtime 自身不能依赖调用方补齐。
        """
        logger.warning("任务 %s 判定失败：%s", task.id, reason)
        task.mark(TaskStatus.FAILED)
        self._persist(task)
        self._emit(task.id, FAILED, reason=reason)
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
    def _action_target(action: Action) -> object:
        """把动作目标序列化成 JSON 友好的形式（Point → {"x":..,"y":..}）。

        事件里要存得下、读得回来，回放时才能还原「它当时点在哪」。
        """
        target = action.target
        if hasattr(target, "model_dump"):
            return target.model_dump(mode="json")
        return target

    @staticmethod
    def _describe_action(action: Action) -> str:
        target = (
            action.target.model_dump(mode="json")
            if hasattr(action.target, "model_dump")
            else action.target
        )
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
            plan_version=task.plan_version,
            action_effect=state.last_action_effect,
            last_action=action,
            action_attempt_id=state.current_attempt_id,
            semantic_state=self._semantic_state(observation, task),
            budget_used={
                "action_steps": state.execution_step,
                "observations": state.observation_count,
                "model_calls": state.model_call_count,
            },
        )
        self._checkpoints.save(checkpoint)
        task.checkpoint_id = checkpoint.id
        self._emit(
            task.id,
            CHECKPOINT_SAVED,
            checkpoint_id=checkpoint.id,
            effect=checkpoint.action_effect.value,
            budget_used=checkpoint.budget_used,
            screenshot=checkpoint.screenshot_path,
        )

    @staticmethod
    def _semantic_state(observation: Observation | None, task: Task) -> str:
        """恢复点上的「当前在干什么」摘要。

        刻意只取廉价信号（页面 + 聚焦步骤），不调 VLM——
        恢复点每一步都要写，用模型既贵又会让写盘变成网络调用。
        """
        if observation is None:
            return ""
        page = f"{observation.package}/{observation.activity}".strip("/")
        step = f" · 步骤 {task.active_step_id}" if task.active_step_id else ""
        return f"{page}{step}"

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
