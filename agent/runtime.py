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
from models.exceptions import DeviceUnavailableError, PersistenceError
from models.retry import (
    DEFAULT_POLICY,
    ErrorClass,
    RetryAction,
    classify_error,
    classify_result,
)
from models.state import Observation, StepOutcome
from models.task import TERMINAL_STATUSES, Task, TaskStatus
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

# 注：`SHADOW_TOCTOU_GUARD` 与 `MAX_STALE_OBSERVATIONS` 定义在 `_execution.py`
# ——它们只在执行循环里用，放在 mixin 同一模块可避免 `runtime` ↔ `_execution` 循环导入。



from ._confirm import ConfirmationMixin
from ._execution import ExecutionMixin
from ._goal import GoalControllerMixin
from ._reconcile import ReconcilerMixin
from ._recovery import RecoveryMixin
from ._runtime_types import ApprovalGrant, RunOutcome, RuntimeState



class AgentRuntime(
    ExecutionMixin,
    ConfirmationMixin,
    GoalControllerMixin,
    ReconcilerMixin,
    RecoveryMixin,
):
    def __init__(
        self,
        session: DeviceSession | DevicePool,
        *,
        artifact_dir: str | Path = ARTIFACT_DIR,
        trajectory: object | None = None,
        checkpoints: object | None = None,
        task_store: object | None = None,
        event_log: EventLog | None = None,
        executions: object | None = None,
    ) -> None:
        # 向后兼容：老调用方传单个 DeviceSession（单设备场景）
        self._pool = session if isinstance(session, DevicePool) else DevicePool([session])
        self._default_session = self._pool.first()
        self._artifact_dir = Path(artifact_dir)
        self._trajectory = trajectory
        self._checkpoints = checkpoints
        self._task_store = task_store
        self._event_log = event_log
        # v4.1 §八/§九：动作级执行记录的唯一写入口（`agent.execution.ExecutionService`）。
        #
        # 可以**不传**：不传时行为与 V4.1 之前完全一致（事件照发，只是不带 execution_id），
        # 单测与脚本里的 runtime 大多属于这一类。线上一律由 `api/server.py` 注入。
        self._executions = executions
        self._states: dict[str, RuntimeState] = {}
        # 多设备 = 多个 worker 线程并发调用 runtime，运行时状态必须加锁
        self._states_lock = threading.RLock()
        # 崩溃恢复的备注容器（V2.6 §七）。职责与实现都在 `_recovery.py`
        # ——那是唯一一条会把任务交给人的路，单独一个文件讲清楚。
        self._init_recovery()


    def _session_for(self, task: Task) -> DeviceSession:
        """按任务绑定的设备取会话（V2.1 §十三 / V2.7 P0-3）。

        刻意**不做缓存**：多设备下 runtime 会被多个 worker 线程并发调用，
        缓存就是共享可变状态，而共享状态正是并发 bug 的来源。
        从池里按 serial 查一下的成本可以忽略。

        **已绑定的任务绝不回退到别的设备**（V2.7 P0-3）。原来的
        `self._pool.get(...) or self._default_session` 在「绑定设备不在池里」时会悄悄换成
        默认设备——那是跨设备上下文污染：A 任务停在微信页面、B 任务停在支付页面，
        把 A 的恢复点拿到 B 上接着点，等于把任务丢进别人的手机。
        已绑定的任务只有两种结局：**用原设备**，或 `DEVICE_UNAVAILABLE`
        （异常抛给调度器的失败分流，落 `DEVICE_UNAVAILABLE` 等原设备回来）。
        只有**尚未绑定**的任务才允许分配默认设备。
        """
        if task.device_serial:
            session = self._pool.get(task.device_serial)
            if session is None:
                raise DeviceUnavailableError(task.id, task.device_serial)
            return session
        if self._default_session is None:
            # 池里一台设备都没有：同样是「没有可用设备」，而不是「随便找一台」
            raise DeviceUnavailableError(task.id, "（未绑定且设备池为空）")
        return self._default_session

    # ---- 对外 ----

    def _emit(self, task_id: str, kind: str, **data) -> None:
        """写一条事件。**按 kind 自动分级**（V3.1 P0）。

        安全关键事件（`ACTION_DISPATCHED` / `RISK_ASSESSED` / `CONFIRMED` /
        `GOAL_CONFIRMED`）会**抛出** `PersistenceError`——这是刻意的，调用方必须
        决定「记不下这条，副作用还能不能继续」。要写成「失败返回原因」的形式，
        用 `_emit_critical_or`；要自己 try/except 也行，但别把安全事件当旁路吞掉。
        """
        if self._event_log is not None:
            self._event_log.emit(task_id, kind, **data)

    def _emit_critical(self, task_id: str, kind: str, **data) -> None:
        """**强制** fail-closed 地写一条事件，失败抛 `PersistenceError`（V3 M4）。

        `_event_log.emit` 已经会按 `is_safety_critical(kind)` 自动分派，所以这里
        主要是把「我要求这条必须落盘」这个意图写在调用点——同时它也是给未来
        不在 `SAFETY_CRITICAL_KINDS` 里的关键事件留的显式入口。
        """
        if self._event_log is not None:
            self._event_log.emit_critical(task_id, kind, **data)

    def _emit_critical_or(self, task_id: str, kind: str, **data) -> str | None:
        """写安全关键事件，把「失败」变成可判定的返回值（V3.1 P0）。

        成功返回 None；失败返回原因字符串，调用方据此**拒绝放行**副作用。
        危险动作 dispatch、人工批准、完成认定这三处的共同点是：它们一旦没被记录，
        就不能让对应的事情真的发生——所以需要的是「先写、再决定」，而不是
        「先做、顺手记一下」。
        """
        try:
            self._emit_critical(task_id, kind, **data)
        except PersistenceError as exc:
            logger.error("任务 %s 的安全关键事件 %s 写盘失败：%s", task_id, kind, exc.reason)
            return exc.reason
        return None

    # ---- 阶段 ----


    def _state_for(self, task: Task) -> RuntimeState:
        # 多设备下每台设备的 worker 都会走这里，必须加锁
        with self._states_lock:
            state = self._states.get(task.id)
            if state is None:
                state = RuntimeState()
                self._states[task.id] = state
            return state


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
        # V2.3 这里写的是「checkpoint 与 task pointer 必须一次提交」——**那句话说过头了**。
        # 这是两个文件、两次写入，没有跨文件事务（V3.2 §三）。准确的说法是下面两条，
        # 它们才是真正成立的：
        #
        #   ① **顺序**：先写 checkpoint、后写 task。所以任务**永远**不会指向一个
        #      不存在或没写完的恢复点——这是崩溃恢复最怕的那个方向。
        #   ② **单文件原子可见**：`JsonStore` 走 tmp + fsync + os.replace，任何一刻
        #      读到的新名字下面，内容都已经完整落盘。
        #
        # 代价是反方向仍然存在：进程在第 ③ 步之前崩溃 → 恢复点文件在盘上、任务指针
        # 没被提交 → 留下一个**孤儿恢复点**。它是无害的（没有任何代码路径会读
        # 「非指针指向的恢复点」），由启动时的 `CheckpointStore.prune_orphans()` 清掉。
        #
        # 要真正的跨文件事务就得上 SQLite（`BEGIN; INSERT checkpoint; UPDATE task; COMMIT;`）
        # ——依赖的是同一个触发条件：多进程部署或整体换存储（见 MEMORY [59]）。
        self._persist(task)
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
        """关键持久化：任务状态、checkpoint_id、version 等必须落盘。

        V2.3：这是关键持久化，失败不再被吞掉——内存状态继续领先 durable state
        会导致崩溃恢复后重复执行副作用。失败时抛 PersistenceError，由调用方
        把任务降级并停止产生新的 side effect。
        """
        if self._task_store is None:
            return
        try:
            # 这里**刻意不带** `expected_revision`（V2.6 §八，有理由的延期）：
            # 本进程内 Runtime / Scheduler / TaskManager 持有的是同一批内存 Task 实例，
            # `task.revision` 随任一写者推进，磁盘序号与内存天然一致——再加 CAS 不增加
            # 保护，反而会把「内存比磁盘新」（调度器先改内存、稍后统一落盘）这种**正常**
            # 情形误判成冲突。
            # 真正需要 CAS 的是**跨进程**写入，那得靠文件锁或数据库事务；到时候统一收口到
            # 一个 TaskMutationService，而不是在这里逐点补 expected_revision。
            self._task_store.save(task)
        except Exception as exc:  # noqa: BLE001 - 转换后重新抛出
            logger.exception("保存任务 %s 失败", task.id)
            raise PersistenceError(task.id, str(exc)) from exc
