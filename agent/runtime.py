"""AgentRuntime（V2 §九）：只回答「怎么完成这个任务」。

调度不在这一层——排队、抢占、恢复由 `agent.scheduler.TaskScheduler` 负责。
Runtime 只做 Observe → Think → Act → Verify → Checkpoint 的闭环，
并在每个循环安全点检查「是否被取消 / 是否该让出设备」。

V5 §十二 起闭环里多了一层**执行策略**：

    Observe → Think → **ExecutionPolicy** → Act → Verify → Checkpoint

`ExecutionPolicy` 的落点是 `safe_point()`——它把「被取消 / 该让设备 / **用户在用手机** /
执行平面是否可用」这些**与页面无关的中断条件**收敛到一处判断（见该方法的 docstring）。
拆出去的价值在文档 §十二 说得很清楚：planner 只需要回答「下一步做什么」，
「在哪里做、现在能不能做」是执行侧的事。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from device.pool import DevicePool, storage_hint
from device.session import DeviceBusyError, DeviceSession
from models.action import Action, ActionType, ActionRisk, ActionEffectStatus, Decision
from models.checkpoint import Checkpoint
from models.exceptions import DeviceUnavailableError, PersistenceError
from models.execution_mode import ExecutionMode
from models.retry import (
    DEFAULT_POLICY,
    ErrorClass,
    RetryAction,
    classify_error,
    classify_result,
)
from models.state import Observation, ObservationEpoch, StepOutcome
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



@dataclass(frozen=True)
class SafePoint:
    """循环安全点的一次判定结论（V5 §十二）。

    `stop=False` 表示「可以继续」。停下时带 `outcome`（该以什么结果退出本轮）
    与 `kind`（结构化原因，用于发事件与统计）——**不发散成自由文本**，
    因为「任务为什么停」是要能被程序区分、被 `/health` 聚合的，
    不能只靠人去读日志（[66]：结构化优先，文本兜底）。
    """

    stop: bool
    outcome: "RunOutcome | None" = None
    reason: str = ""
    kind: str = ""


class AgentRuntime(
    ExecutionMixin,
    ConfirmationMixin,
    GoalControllerMixin,
    ReconcilerMixin,
    RecoveryMixin,
):
    # 因「用户正在用手机」而暂停的**次数上界**（V5 §十四）。
    #
    # 为什么必须有上界：用户长时间连续操作手机时，任务会陷入
    # 「用户在场 → 暂停 → 用户停手 → 恢复 → 用户又碰 → 又暂停」的循环，
    # 表现为「任务永远在 running、什么也不做、日志里全是用户活动暂停」。
    # 用户看到的是「它卡住了」，而他没法知道原因是自己在用手机。
    #
    # 到上界就明确失败（附上「可稍后重试或改用 shadow 模式」），
    # 把决定权交还给人——这比无限期等下去诚实。
    MAX_USER_PAUSES = 5

    DEFAULT_USER_IDLE_SECONDS = 2.0
    """用户停手多少秒后认为「他不用了」，可以恢复（V5 §十四）。

    2 秒是 §十四 给的默认值。**不能太短**：用户点完一下到点下一下之间常有一两秒的
    阅读停顿，按 0.5 秒判「停手了」会表现为「任务和用户抢屏幕」——每次用户刚点完
    任务就插进来，页面被抢走，用户下一个动作落在错误的页面上。
    **也不能太长**：用户在等电梯时把手机放到一边，任务要等 10 秒才能继续，
    用户感受到的是「它反应好慢」。这个值做成类属性便于测试与按设备调整。
    """

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

    def safe_point(self, task: Task, state: "RuntimeState", session: DeviceSession) -> "SafePoint":
        """循环安全点：**统一判断「现在还能不能继续往下做」**（V5 §十二 / §十六）。

        ━━━ 为什么要有这个方法 ━━━

        以前这些检查散在 `_run_loop` 里逐条 if（取消、抢占、预算）。再往里加一条
        「用户在不在用手机」时，最省事的写法是在那个函数里再插一个 if——但那样会带来
        两个问题：

        1. **顺序敏感**：这些条件互相不是独立的（用户在用手机 ⇒ 不该走«执行»；
           被抢占 ⇒ 无论用户在不在都该让位）。散着写时，谁先谁后靠读代码的人自己
           推，而新加的一条极容易被放到错误的位置。收敛到一处之后，
           顺序**显式、可测、只有一处**。
        2. **不可测**：安全点判定需要「造出被抢占/用户在场/预算耗尽」这些状态。
           收成一个纯函数式的入口后，用例可以直接构造 `Task` + `RuntimeState` +
           `DeviceSession` 调它，不必跑完整个 loop。

        返回 `SafePoint` 而不是 bool：调用方需要知道**为什么**停下（好发对事件、
        落对状态、选对 `RunOutcome`）。返回 bool 的话调用方只能再猜一次原因，
        而那正是「Runtime 说暂停了、Scheduler 说还在跑」这类语义冲突的来源。

        ━━━ 判定顺序与理由 ━━━

        1. **终态**——最高优先。任务已经是 DONE/FAILED/CANCELLED 时不该再动设备
           （V2.5 §六，这是最后一道闸）。
        2. **取消请求**——用户明确要求停，优先于其它一切让位理由。
        3. **用户暂停 / 抢占让位**——外部要求停。
        4. **用户在场**（V5 §十）——**人**在用手机，让开（§十四 第一阶段）。
           放在抢占之后：被抢占是硬让位（另一个 Agent 排队等设备），
           语义更强；而用户在场是可以「等几秒再看看」的软让位。
        5. **预算**——自己跑不动了。
        """
        # 1) 终态：不该再产生任何副作用
        if task.status in TERMINAL_STATUSES:
            return SafePoint(
                stop=True,
                outcome=RunOutcome.CANCELLED,
                reason=f"任务已被置为终态（{task.status.value}）",
                kind="terminal",
            )

        # 2) 取消请求：走到安全点才算真停（V2.5 §七）
        if task.status is TaskStatus.CANCEL_REQUESTED:
            return SafePoint(
                stop=True, outcome=RunOutcome.CANCELLED, reason="已收到取消请求", kind="cancel"
            )

        # 3) 用户暂停 / 被抢占让位
        if task.status is TaskStatus.PAUSED:
            return SafePoint(stop=True, outcome=RunOutcome.SUSPENDED, reason="任务已暂停", kind="paused")
        if session.should_yield(task.id):
            return SafePoint(
                stop=True, outcome=RunOutcome.SUSPENDED, reason="被抢占，让出设备", kind="preemption"
            )

        # 4) 用户在场（V5 §十）——本方法新增的那一条。
        verdict = self._user_presence_verdict(task, state)
        if verdict is not None:
            return verdict

        # 5) 预算
        if state.observation_count >= task.budget.max_observations:
            return SafePoint(
                stop=True,
                outcome=RunOutcome.FAILED,
                reason=f"已达观察次数上限 {task.budget.max_observations}",
                kind="budget_observations",
            )
        if state.model_call_count >= task.budget.max_model_calls:
            return SafePoint(
                stop=True,
                outcome=RunOutcome.FAILED,
                reason=f"已达模型调用上限 {task.budget.max_model_calls}",
                kind="budget_model_calls",
            )

        return SafePoint(stop=False)

    def _user_presence_verdict(
        self, task: Task, state: "RuntimeState"
    ) -> "SafePoint | None":
        """用户在场时要不要让开（V5 §十 / §十四）。

        只对**会占用用户屏幕**的任务生效：`execution_mode=shadow` 的任务本来就
        在影子平面跑（§十五），用户在用 Display 0 与它无关——这时因为「用户在操作」
        而暂停它，等于把用户自己的活动变成了阻断 Agent 的理由，与 §九
        「用户打开微信 → 无需抢占 A → A 继续执行」正好相反。

        ━━━ 「读不到」怎么办 ━━━

        `UserContext.confirmed=False`（探测失败 / 本端不支持）时**不暂停**。
        这里刻意与 `device/user_activity.py` 的保守取值方向不同，理由是同一条原则
        在两个位置上的**代价不对称**：

        - 在**动作层面**（`UserContext` 内部）：宁可说「用户在操作」。
          代价是任务慢一点。
        - 在**任务层面**（这里）：宁可继续跑。因为「探测能力缺失」是一个**恒定**条件
          （本端永远不支持），若把它当「用户在场」，任务会被永久暂停——
          不是慢一点，而是**完全不能跑**。那比「打扰用户」更糟，而且用户无法修复。

        所以判定要求 `confirmed=True` 且 `active=True`：前者保证「我们确实知道用户
        在操作」，后者是那个事实。
        """
        if not state.user_confirmed or not state.user_active:
            return None

        mode = state.effective_execution_mode(task.execution_mode.value)
        if mode != ExecutionMode.FOREGROUND.value:
            # 影子模式且**确实**跑在影子平面上：用户在用 Display 0 与它无关
            # （§九 / §十五），不该因为「用户在操作」而暂停它。
            # 需要用户在场的动作另由风险门禁处置，不在这里拦整条任务。
            #
            # ⚠️ V5 P0③（审查 §四）：这里读的必须是 **resolved** 平面。
            # 旧实现读 `state.execution_mode`，而它被写成 `task.execution_mode`
            # ——于是 HYBRID 在影子不可用、实际回落到 Display 0 时，这里仍然
            # 返回 None（不暂停），Agent 就继续往用户正在用的屏幕上点。
            # `effective_execution_mode()` 在解析不出来时退到 `foreground`，
            # 也就是「宁可多让一次」的保守侧。
            return None

        # 用户在场 → 让开。但**必须**有上界：用户一直在操作时不能无限期地
        # 「暂停→恢复→又暂停」，那会让任务永远推进不了，而用户看到的是
        # 「任务一直卡在 running」。到达上界就转人工，把决定权交还给人。
        if state.user_pauses >= self.MAX_USER_PAUSES:
            return SafePoint(
                stop=True,
                outcome=RunOutcome.FAILED,
                reason=(
                    f"用户已连续占用设备 {state.user_pauses} 次，"
                    "无法在不打扰用户的前提下继续（可稍后重试或改用 execution_mode=shadow）"
                ),
                kind="user_pause_limit",
            )
        return SafePoint(
            stop=True,
            outcome=RunOutcome.SUSPENDED_BY_USER,
            reason=f"用户正在使用设备（前台应用：{state.foreground_package or '未知'}）",
            kind="user_active",
        )

    def user_resume_ready(
        self,
        task: Task,
        state: "RuntimeState",
        session: DeviceSession,
        observation: "Observation | None",
        *,
        idle_seconds: float | None = None,
    ) -> bool:
        """判断「因为用户在场而挂起的任务」现在能不能接着跑（V5 §十四）。

        §十四 给的恢复条件是三条，缺一不可：

        1. 用户已经停手 **N 秒**（默认 `DEFAULT_USER_IDLE_SECONDS`）；
        2. 重新观察过一次页面；
        3. 页面指纹与暂停前**一致**。

        第 3 条最容易被漏掉，但它是这个方案唯一的安全支柱。用户在场期间
        页面可能被他自己换掉了（点开通知进了另一个 App、退出了当前页面），
        这时「继续执行」= 拿 A 屏的坐标点 B 屏。所以恢复**不靠**「用户停手了」，
        而靠「停手了 **且** 那一屏还是我认识的那一屏」。

        ━━━ 与 [80]/[92]「读不到 ≠ 空」的关系 ━━━

        指纹比对要求**两帧都有指纹**。任何一帧读不到（`None` / 空串）时不判「一致」，
        而是**不恢复**——注意这里的保守方向与 `_user_presence_verdict` 相反，
        因为代价又不一样了：这里若把「读不到」当「一致」，就是用不可知的页面
        去执行动作，代价是**点错地方**；不恢复的代价只是任务多等一轮。
        """
        if state.last_user_pause_at is None:
            # 不是「因用户活动」挂起的，本方法不管——抢占挂起走设备可用性判定。
            return False

        if not self._user_idle_long_enough(state, session, idle_seconds):
            return False

        if observation is None:
            # 还没重新观察过：不能凭「用户停手了」就恢复（第 2 条）。
            return False

        return self._page_fingerprint_matches(state, observation)

    def _user_idle_long_enough(
        self,
        state: "RuntimeState",
        session: DeviceSession,
        idle_seconds: float | None,
    ) -> bool:
        """用户停手够久了没（第 1 条）。

        优先用**设备侧的实测空闲时长**（`UserContext.idle_seconds`）——它比
        「距离我们上次暂停过了多久」准确得多：`last_user_pause_at` 里还混着
        「暂停之后我们自己又观察了一轮」的时间，用户其实早停手了，
        我们却因为轮询间隔而多等一次。

        设备侧拿不到时退回本地计时，且**只在当前这一轮确实测到用户不活跃时**才算数：
        拿不到 `confirmed=True` 的「用户不在」就一律不恢复。
        """
        threshold = self.DEFAULT_USER_IDLE_SECONDS if idle_seconds is None else idle_seconds

        probe = getattr(session.controller, "user_context", None)
        if callable(probe):
            try:
                context = probe()
            except Exception as exc:  # noqa: BLE001 —— 探测失败退回本地计时
                logger.debug("恢复判定读用户活动失败：%s", exc)
            else:
                measured = getattr(context, "idle_seconds", None)
                confirmed = bool(getattr(context, "confirmed", False))
                active = bool(getattr(context, "active", False))
                if confirmed and not active and isinstance(measured, (int, float)):
                    return float(measured) >= threshold

        # 本地计时：只在 state 里最新的探测结论是「确实不在操作」时采用。
        if not state.user_confirmed or state.user_active:
            return False
        return (time.monotonic() - state.last_user_pause_at) >= threshold

    @staticmethod
    def _page_fingerprint_matches(state: "RuntimeState", observation: "Observation") -> bool:
        """当前这一屏与暂停前那一屏是不是同一屏（第 3 条）。

        用 `ObservationEpoch`（[81] 决策快照那套）而不是重写一份比对逻辑——
        「页面还是不是那一屏」在本项目里只有一个权威答案，多写一份就多一个
        会和它结论不一致的地方。
        """
        expected = state.pause_epoch
        if expected is None:
            # 暂停时没能记下页面身份（观察缺失 / epoch 读不到）。
            # 不拿「没记录」当「没变化」——那是把证据缺口当证据（[80]）。
            return False
        current = ObservationEpoch.capture(observation, expected.generation)
        if not current.package and not current.activity:
            # 重新观察这一帧读不到页面身份 → 同样按「读不到」处理，不判一致。
            return False
        return current.stale_reason(expected) is None

    def refresh_user_context(self, task: Task, state: "RuntimeState", session: DeviceSession) -> None:
        """刷新「用户此刻在不在用手机」，写进 `state`（V5 §十）。

        与 `safe_point` 分开是因为它们的**调用时机不同**：本方法在每轮循环的
        开始调一次（读完喂给 state），而 `safe_point` 在多个位置被调（每个动作前）。
        分成两个之后，`safe_point` 是纯判定、不碰设备，可以随便调；
        探测（会碰设备、可能超时）只在明确的那一个点发生。

        探测失败**不抛异常、不影响任务**：拿不到就当「不知道」
        （`user_confirmed=False`），由 `_user_presence_verdict` 决定不暂停。
        """
        monitor = getattr(session.controller, "user_context", None)
        if not callable(monitor):
            # 后端没实现探测（老替身 / 老后端）：如实记「不知道」。
            state.user_confirmed = False
            state.user_active = False
            return
        try:
            context = monitor()
        except Exception as exc:  # noqa: BLE001 —— 探测失败不该弄挂任务
            logger.debug("读取用户活动失败（按不知道处理）：%s", exc)
            state.user_confirmed = False
            state.user_active = False
            return

        state.user_confirmed = bool(getattr(context, "confirmed", False))
        state.user_active = bool(getattr(context, "active", False))
        package = getattr(context, "foreground_package", None)
        if package:
            state.foreground_package = str(package)
        # V5 P0③（审查 §四/§五）：写入**解析后**的平面，不是 `task.execution_mode`。
        #
        # 旧实现直接写 `task.execution_mode.value`，于是 HYBRID 在影子不可用、
        # 实际回落到 Display 0 时，state 里仍然写着 "hybrid"——`_user_presence_verdict`
        # 据此认为「它是影子、用户操作与它无关」，**继续和用户抢同一块屏**。
        # 这正是审查 §四 指出的第二个严重问题。
        self._refresh_execution_plane(task, state, session)
        # V5 §十三：顺手把「这次执行落在哪块屏幕上」也记下来（恢复点要存它）。
        # 从设备侧读而不是从任务声明读——`display_id` 是**实际**用的那块，
        # 而后端已经知道这个事实（ADB 恒为默认显示，Android 侧将来可能是影子显示）。
        display_id = getattr(session.controller, "display_id", None)
        if isinstance(display_id, int) and not isinstance(display_id, bool):
            state.display_id = display_id

    def _refresh_execution_plane(
        self, task: Task, state: "RuntimeState", session: DeviceSession
    ) -> None:
        """把「任务要求的平面」解析成「实际平面」写进 `state`（V5 P0③）。

        ━━━ 与 `ExecutionSession.resolve_plane()` 的分工 ━━━

        `resolve_plane()` 回答的是「**这个动作**落在哪」（会把 `USER_REQUIRED`
        的动作单独拎出来判前台）；这里回答的是「**这个任务整体**在哪」——
        用户暂停判定关心的是后者：整条任务会不会占用用户的屏幕。

        影子可用性取自**设备侧的能力**而不是任务声明：`ExecutionSession` 在本
        路径上还不存在（那是 §八 的会话层，Real 里由调度器持有），而设备控制器
        已经知道本端有没有影子平面。

        ━━━ 解析失败不抛异常 ━━━

        `SHADOW` 任务在严格路径下会抛 `ShadowSessionUnavailable`——那是
        `resolve_plane()` 在**执行**路径上的职责（要么做到、要么明确失败）。
        这里是**观测**路径：拿不到结论就记「保守＝前台」并继续，
        因为用户暂停判定宁可按「会占用屏幕」处理（代价=多让一次），
        也不能因为解析不出平面就把任务弄挂。
        """
        from device.session import resolve_task_plane

        source = self._shadow_plane_probe(session)
        try:
            plane = resolve_task_plane(
                task.execution_mode,
                shadow_available=source,
                task_id=task.id,
                strict=False,
            )
        except Exception as exc:  # noqa: BLE001 —— 观测路径不该弄挂任务
            logger.debug("解析执行平面失败（按前台保守处理）：%s", exc)
            state.requested_execution_mode = str(task.execution_mode.value)
            state.resolved_execution_mode = ExecutionMode.FOREGROUND.value
            state.execution_mode = ExecutionMode.FOREGROUND.value
            state.plane_degraded = True
            state.plane_reason = f"平面解析失败：{exc}"
            return

        state.requested_execution_mode = plane.requested.value
        state.resolved_execution_mode = plane.resolved.value
        # 兼容旧调用点：`execution_mode` 的语义已统一为 resolved（见 _runtime_types）。
        state.execution_mode = plane.resolved.value
        state.plane_degraded = plane.degraded
        state.plane_reason = plane.reason
        if plane.degraded:
            # 降级是**用户可感知**的事实（他的 hybrid 其实在前台跑），
            # 第一次发生时报一次，别每轮刷屏。
            if not state.plane_degraded_reported:
                logger.info(
                    "任务 %s 的执行平面发生降级：%s → %s（%s）",
                    task.id,
                    plane.requested.value,
                    plane.resolved.value,
                    plane.reason,
                )
                state.plane_degraded_reported = True

    def _shadow_plane_probe(self, session: DeviceSession) -> bool | None:
        """影子平面可用吗（三态）。**不建连接**——只读后端已有的能力位。

        `True` / `False` / `None`（不知道）。之所以能直接说 `True`：设备侧只有在
        真的握有一个可用影子会话时才会报这个值；当前 Android 实现恒报 `False`
        （`ShadowDisplayManager` 如实声明做不到），所以实际路径上永远是
        `False` 或 `None`——两条都会让 `HYBRID` 保守地算作前台。
        """
        probe = getattr(session.controller, "shadow_plane_available", None)
        if not callable(probe):
            return None
        try:
            value = probe()
        except Exception as exc:  # noqa: BLE001 —— 可选能力，探测失败按未知
            logger.debug("读取影子平面可用性失败（按未知处理）：%s", exc)
            return None
        return value if isinstance(value, bool) else None

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
            # V5 §十三（+ P0③ 修正）：把「这个任务实际跑在哪块屏幕上」一起存下来。
            #
            # 存的是 `state.resolved_execution_mode`（**解析后**的平面）而不是
            # `task.execution_mode`（任务的**声明**）：两者在 hybrid 降级、
            # 影子平面中途不可用时就会分叉，而恢复时要知道的是真相。
            # 旧实现存 `state.execution_mode`，而那个字段被写成 task 的声明值——
            # 于是 hybrid 降级后恢复点里依然写着 "hybrid"，恢复语义错误（审查 §五）。
            execution_mode=state.effective_execution_mode(task.execution_mode.value),
            # 用户**要求**的平面也一并留下：恢复出来才知道它本来申请的是什么，
            # 否则「降级过」这个事实在恢复后就永久消失了。
            requested_execution_mode=state.requested_execution_mode,
            plane_degraded=state.plane_degraded,
            plane_reason=state.plane_reason,
            session_id=state.session_id,
            display_id=state.display_id,
            shadow_state_id=state.shadow_state_id,
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
