"""执行循环（V2.7 P2-1 拆出的独立职责）。

Observe → Think → Act → Verify → Checkpoint 的主循环骨架，连同它直接调用的
阶段方法（观察 / 决策 / 执行 / 验证 / 收尾 / 失败结算）。这些方法通过 `self` 引用
主类的共享能力（`_emit` / `_persist` / `_save_checkpoint` / `_ask_human` 等），
所以用 mixin 拆解、共享同一实例，零行为改动。
"""
from __future__ import annotations

import logging

from device.pool import storage_hint
from device.session import DeviceBusyError, DeviceSession
from models.action import Action, ActionType, ActionRisk, ActionEffectStatus, Decision
from models.checkpoint import Checkpoint
from models.exceptions import PersistenceError
from models.retry import (
    DEFAULT_POLICY,
    ErrorClass,
    RetryAction,
    classify_error,
    classify_result,
)
from models.state import Observation, StepOutcome
from models.task import TERMINAL_STATUSES, Task, TaskEvent, TaskStatus
from models.task_step import StepStatus, TaskStep
from models.verification import ActionDispatch, ActionEffect, DispatchStatus, GoalVerification
from storage.event_log import (
    ACTION_DISPATCHED,
    ACTION_VERIFIED,
    DONE,
    EFFECT_UNKNOWN,
    FAILED,
    RISK_ASSESSED,
    STARTED,
    SUSPENDED,
    WAITING,
)

from . import executor, observer, planner, reconciliation, verifier
from .planner import ReplanContext
from .risk_gate import ActionRiskGate, RiskContext
from ._runtime_types import LOOP_REPEAT_THRESHOLD, RunOutcome, RuntimeState

logger = logging.getLogger(__name__)

# 重试行为只由这一份策略决定（V2.1 §十二），不再硬编码次数
RETRY_POLICY = DEFAULT_POLICY


class ExecutionMixin:
    def run(self, task: Task) -> RunOutcome:
        """把一个任务跑到结束、失败或被挂起。"""
        if task.status in (TaskStatus.CANCELLED, TaskStatus.CANCEL_REQUESTED):
            # 调度器已经（请求）取消了它，不要用 mark(RUNNING) 把状态又改回去。
            # V2.5 §七：CANCEL_REQUESTED 只表示「请求已下达」；但本轮压根没开始执行，
            # 不存在副作用疑云，所以在这里直接落定 CANCELLED。
            if task.status is TaskStatus.CANCEL_REQUESTED:
                task.apply_event(TaskEvent.CANCELLED, source="runtime")
            logger.info("任务 %s 已取消，跳过执行", task.id)
            return RunOutcome.CANCELLED

        state = self._state_for(task)
        state.run_version = task.version
        # 载入跨重启存活的决策状态（V2.7 P0-1）：用户否决过的动作，重启之后依然算数
        state.denied_fingerprints.update(task.denied_fingerprints)
        # 本任务跑在哪台设备上，由绑定决定——多设备时 worker 线程各跑各的，
        # 所以这个 session 只作为**局部变量**贯穿本次 run，不进实例状态
        session = self._session_for(task)

        # V2.6 §七：崩溃恢复门禁要在 mark(RUNNING) 之前——先确认「上次那个动作到底
        # 发出去没有」。确认不了就不进入执行循环（返回 None 表示可以正常开跑）。
        if task.recovery_required:
            gate = self._gate_crash_recovery(task)
            if gate is not None:
                return gate

        task.apply_event(TaskEvent.DISPATCHED, source="runtime")
        try:
            self._persist(task)
        except PersistenceError as exc:
            # V2.4 §六：启动期的首次落盘同样属于「关键持久化」。
            # 放在 try 外面的话，这个异常会直接逃到调度器的兜底 except，
            # 最终被判成 FAILED —— 但「持久化失败」与「任务失败」是两回事：
            # 前者必须停在 DEGRADED（内存状态已经领先 durable state，
            # 继续跑会在崩溃恢复后重复产生副作用），而不是假装任务做完了。
            logger.error("任务 %s 启动期持久化失败，任务降级：%s", task.id, exc)
            return self._degrade(task, state, f"启动期持久化失败：{exc.reason}")
        self._emit(task.id, STARTED, version=task.version, instruction=task.instruction)

        checkpoint = self._load_checkpoint(task)
        observation: Observation | None = None
        last_observation: Observation | None = None

        try:
            outcome = self._run_loop(
                task, state, session, checkpoint, observation, last_observation
            )
        except PersistenceError as exc:
            # V2.3：关键持久化失败必须停下来，不能再继续产生 side effect。
            # 此时内存状态已经领先于磁盘，继续执行可能在崩溃后重复副作用。
            logger.error("任务 %s 关键持久化失败，任务降级：%s", task.id, exc)
            return self._degrade(task, state, f"持久化失败：{exc.reason}")
        return outcome


    def _run_loop(
        self,
        task: Task,
        state: RuntimeState,
        session: DeviceSession,
        checkpoint: Checkpoint | None,
        observation: Observation | None,
        last_observation: Observation | None,
    ) -> RunOutcome:
        while True:
            # V2.7 P0-1：把该跨重启存活的运行时状态同步到任务上（随下一次 persist 落盘）。
            # 只同步「否决黑名单」这类必须记住的东西，取舍见 `_sync_durable_state`。
            self._sync_durable_state(task, state)

            # ---- 安全点 ----
            if task.status in TERMINAL_STATUSES:
                # V2.5 §六：终态意味着「不该再产生任何副作用」。正常路径走不到这里
                # （外部已经不允许把 RUNNING 改成终态），但这是最后一道闸：万一有人绕过
                # 去，我们宁可在这里退出，也不要在一条已经结束的任务上继续点手机。
                logger.warning(
                    "任务 %s 在执行中被置为终态（%s），立即停止产生副作用",
                    task.id,
                    task.status.value,
                )
                return RunOutcome.CANCELLED
            if task.status is TaskStatus.CANCEL_REQUESTED:
                # V2.5 §七：CANCEL_REQUESTED 表示「请求已下达」，走到安全点才算真停。
                # 此刻设备侧可能刚 dispatch 过一个动作——所以退出路径要如实区分
                # 「动作发出前就停了」还是「动作发出后才发现要停」，审计才读得懂。
                task.apply_event(TaskEvent.CANCELLED, source="runtime")
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

            # ---- 版本围栏（V2.3）：目标/计划被外部改写后，旧决策上下文作废 ----
            if task.version != state.run_version:
                logger.info(
                    "任务 %s 版本已变化（%d → %d），作废当前决策上下文并重新规划",
                    task.id,
                    state.run_version,
                    task.version,
                )
                state.run_version = task.version
                task.plan = []
                state.prepared = False
                checkpoint = None
                self._save_checkpoint(task, state, last_observation)
                continue

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

            if assessment.requires_confirmation:
                if state.approval is not None and state.approval.matches(action, task):
                    # 一次性：放行即消费，绝不复用（V2.7 P0-2）
                    logger.info(
                        "任务 %s 的危险动作已获人工放行，本次放行即消费：%s",
                        task.id,
                        action.type.value,
                    )
                    state.approval = None
                else:
                    if state.approval is not None:
                        # 批准的不是这个动作（动作本身 / 目标版本 / 计划版本任一变过）
                        # → 凭证作废，重新请求确认（V2.7 P0-2）
                        logger.warning(
                            "任务 %s 的人工放行凭证与本动作不匹配（%s），作废并重新请求确认",
                            task.id,
                            state.approval.describe(),
                        )
                        state.approval = None
                    state.pending_confirmation = action
                    state.pending_task_version = task.version
                    state.pending_plan_version = task.plan_version
                    self._save_checkpoint(task, state, observation)
                    task.apply_event(TaskEvent.AWAITING_CONFIRMATION, source="runtime")
                    self._persist(task)
                    logger.warning(
                        "任务 %s 命中危险动作，等待人工确认：%s", task.id, action.type.value
                    )
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
            # 放行凭据已经在「放行那一刻」消费掉了（V2.7 P0-2），这里只需清掉待确认占位
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
            # V2.7 P2-2：结构化错误类别优先（device 层直接带了 executor 的分类），
            # 文本匹配只作兜底。
            error_class = classify_result(
                {"ok": False, "error_class": verification.dispatch.error_class}
            ) if verification.dispatch.error_class else classify_error(verification.message)
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


    def _ask_human(
        self, task: Task, state: RuntimeState, action: Action | None, *, reason: str, message: str
    ) -> RunOutcome:
        """把无法自行决断的事交给人，并把任务停在 WAITING。"""
        state.pending_confirmation = action
        task.apply_event(TaskEvent.AWAITING_CONFIRMATION, source="runtime")
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
        task.apply_event(TaskEvent.COMPLETED, source="runtime")
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
        task.apply_event(TaskEvent.FAILED, source="runtime")
        self._persist(task)
        self._emit(task.id, FAILED, reason=reason)
        return RunOutcome.FAILED


    def _degrade(self, task: Task, state: RuntimeState, reason: str) -> RunOutcome:
        """关键持久化失败后的安全态：任务不能再产生 side effect（V2.3）。

        与 _fail 不同：_fail 是「任务执行不下去」；_degrade 是「状态已经落不了盘，
        继续执行会在崩溃后丢失进度并可能重复副作用」。
        """
        logger.error("任务 %s 降级：%s", task.id, reason)
        task.apply_event(TaskEvent.DEGRADED, source="runtime")
        # 降级本身也要尽力落盘；再失败就无力回天了，但至少不会再产生新动作。
        try:
            self._persist(task)
        except PersistenceError:
            logger.exception("任务 %s 降级状态也无法持久化", task.id)
        self._emit(task.id, FAILED, reason=reason, degraded=True)
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
            task.apply_event(TaskEvent.AWAITING_CONFIRMATION, source="runtime")
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
