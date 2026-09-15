"""Task 模型（V2 §二）：任务不再只是「一次请求」，而是可排队、可暂停、可恢复、可抢占的执行单元。"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from .budget import TaskBudget
from .exceptions import InvalidTransitionError
from .task_plan import TaskPlan
from .task_step import StepStatus, TaskStep, build_steps

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING = "waiting"
    # 关键持久化失败：任务不能再继续产生 side effect（V2.3）。
    DEGRADED = "degraded"
    # 任务绑定的设备当前不可用，等待原设备恢复而不是静默改派（V2.3）。
    DEVICE_UNAVAILABLE = "device_unavailable"
    # 取消请求已收到，但设备侧可能还在动（V2.5 §七）。**不是终态**：
    # 它表达的是「请求已下达」，真正的停止发生在 Runtime 的下一个安全点——
    # 强杀线程不可能安全地「在动作执行到一半」停下，所以必须把
    # 「请求取消」和「已经停止」分成两个状态，否则 CANCELLED 会被误读成
    # 「副作用已停止」，而点击「发送」之后立刻取消时消息其实已经发出去了。
    CANCEL_REQUESTED = "cancel_requested"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskEvent(str, Enum):
    """任务状态迁移的**语义事件**（V2.7 P1-5）。

    事件驱动的价值不在「换个写法」，而在于把「发生了什么 → 该变成什么状态」的映射
    **集中到一处**（`Task.apply_event`），而不是散落在 Runtime / Scheduler /
    TaskManager 的 33 处 `task.mark(TaskStatus.X)` 里各自拍脑袋。

    各模块不再直接指定目标状态，而是声明「发生了什么」；迁移目标由这里统一决定，
    于是「Runtime 说暂停了、Scheduler 说还在跑」这类跨模块语义冲突在源头就无从发生。
    `mark` / `transition_to` 仍是底层唯一原语（终态硬闸 + source 审计不变）。
    """

    # 生命周期
    CREATED = "created"              # 任务刚创建
    SUBMITTED = "submitted"          # 入队待执行
    DISPATCHED = "dispatched"        # worker 开始执行
    COMPLETED = "completed"          # 正常跑完
    FAILED = "failed"                # 执行失败
    CANCELLED = "cancelled"          # 已取消（副作用已停止）
    CANCEL_REQUESTED = "cancel_requested"  # 取消请求下达（副作用可能还在途）

    # 中断与恢复
    PAUSED_BY_USER = "paused_by_user"
    PAUSED_BY_PREEMPTION = "paused_by_preemption"
    RESUMED = "resumed"

    # 等待与故障
    AWAITING_CONFIRMATION = "awaiting_confirmation"  # 危险动作 / 完成裁定 / 崩溃恢复
    DEVICE_LOST = "device_lost"      # 绑定设备不可用
    DEGRADED = "degraded"            # 关键持久化失败


# 事件 → 目标状态的集中映射（V2.7 P1-5）。
# 这是「唯一」的语义裁决点：一个事件该落到哪个状态，只有这里说了算。
_EVENT_TO_STATUS: dict[TaskEvent, TaskStatus] = {
    TaskEvent.CREATED: TaskStatus.CREATED,
    TaskEvent.SUBMITTED: TaskStatus.QUEUED,
    TaskEvent.DISPATCHED: TaskStatus.RUNNING,
    TaskEvent.COMPLETED: TaskStatus.DONE,
    TaskEvent.FAILED: TaskStatus.FAILED,
    TaskEvent.CANCELLED: TaskStatus.CANCELLED,
    TaskEvent.CANCEL_REQUESTED: TaskStatus.CANCEL_REQUESTED,
    TaskEvent.PAUSED_BY_USER: TaskStatus.PAUSED,
    TaskEvent.PAUSED_BY_PREEMPTION: TaskStatus.PAUSED,
    TaskEvent.RESUMED: TaskStatus.QUEUED,
    TaskEvent.AWAITING_CONFIRMATION: TaskStatus.WAITING,
    TaskEvent.DEVICE_LOST: TaskStatus.DEVICE_UNAVAILABLE,
    TaskEvent.DEGRADED: TaskStatus.DEGRADED,
}


class TaskPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


_PRIORITY_RANK = {
    TaskPriority.LOW: 0,
    TaskPriority.NORMAL: 1,
    TaskPriority.HIGH: 2,
    TaskPriority.CRITICAL: 3,
}

TERMINAL_STATUSES = frozenset(
    {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.DEGRADED}
)
# 已进入调度视野、但尚未结束
ACTIVE_STATUSES = frozenset(
    {
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.PAUSED,
        TaskStatus.WAITING,
        TaskStatus.DEVICE_UNAVAILABLE,
        TaskStatus.CANCEL_REQUESTED,
    }
)

# 「数据损坏」不是任务状态，所以**不**放进 TaskStatus（V2.4 §十）：
# 任务记录已经反序列化不出来了，根本构造不出 Task 对象，谈不上它「处于哪个状态」。
# 这个常量只用于 API 呈现——GET /tasks/{id} 对已隔离的任务回 status=recovery_error
# 而不是 404：「不存在」与「数据坏了」是两种完全不同的故障，处置方式也不同。
RECOVERY_ERROR = "recovery_error"

# 暂停原因。进程重启后只有「用户显式暂停」该继续保持暂停；
# 「被抢占挂起」是调度器临时让位，重启后必须自动恢复，否则任务就被永久搁置了。
PAUSED_BY_USER = "user"
PAUSED_BY_PREEMPTION = "preemption"

# ---- 状态机（V2.2 §十/§十一）----
#
# 背景：TaskManager / Scheduler / Runtime / API 四层都能写 `task.status`，
# 于是「Runtime 想拒绝后继续、API 却直接 FAILED」这类**跨模块语义冲突**几乎必然发生
# （审核原话：不是功能不够，而是设计文档和实际状态迁移开始分叉）。
#
# 所以把合法迁移写成一张表，所有写入统一走 `Task.transition_to()`。
# 终态不可逆（DONE / FAILED / CANCELLED / DEGRADED 之后不能再动）：
# 再迁出直接抛 `InvalidTransitionError`（fail closed）。这是防止「已完成的任务被
# 复活、然后重新产生副作用」的最后一道闸，不能只记 warning。
#
# 非终态的非法迁移**仍然执行**，只记 warning 并返回 False。
# 这是刻意的——运行时最怕的是「状态卡住」，严格拒绝会让任务永远停在 running；
# 但要能被看见，而不是悄悄发生。
ALLOWED_TRANSITIONS: dict["TaskStatus", frozenset["TaskStatus"]] = {
    TaskStatus.CREATED: frozenset(
        {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.PAUSED,
         TaskStatus.WAITING, TaskStatus.DEGRADED, TaskStatus.DEVICE_UNAVAILABLE,
         TaskStatus.CANCEL_REQUESTED, TaskStatus.FAILED, TaskStatus.CANCELLED,
         TaskStatus.DONE, TaskStatus.CREATED}
    ),
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.WAITING,
         TaskStatus.DEGRADED, TaskStatus.DEVICE_UNAVAILABLE, TaskStatus.CANCEL_REQUESTED,
         TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.DONE, TaskStatus.QUEUED}
    ),
    TaskStatus.RUNNING: frozenset(
        {TaskStatus.QUEUED, TaskStatus.PAUSED, TaskStatus.WAITING,
         TaskStatus.DEGRADED, TaskStatus.DEVICE_UNAVAILABLE, TaskStatus.CANCEL_REQUESTED,
         TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.RUNNING}
    ),
    TaskStatus.PAUSED: frozenset(
        {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING,
         TaskStatus.DEGRADED, TaskStatus.DEVICE_UNAVAILABLE, TaskStatus.CANCEL_REQUESTED,
         TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.PAUSED}
    ),
    TaskStatus.WAITING: frozenset(
        {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.PAUSED,
         TaskStatus.DEGRADED, TaskStatus.DEVICE_UNAVAILABLE, TaskStatus.CANCEL_REQUESTED,
         TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.WAITING}
    ),
    TaskStatus.DEVICE_UNAVAILABLE: frozenset(
        {TaskStatus.QUEUED, TaskStatus.DEGRADED, TaskStatus.CANCEL_REQUESTED,
         TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.DEVICE_UNAVAILABLE}
    ),
    # 取消请求：等 Runtime 在安全点落定。正常落成 CANCELLED；退出途中出错可以是
    # FAILED / DEGRADED；设备重新可用时也允许回到 QUEUED（取消被打断）；
    # DONE 也是合法的——任务可能恰好抢在安全点之前跑完了，那比「已取消」更接近事实。
    TaskStatus.CANCEL_REQUESTED: frozenset(
        {TaskStatus.CANCELLED, TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.DEGRADED,
         TaskStatus.QUEUED, TaskStatus.CANCEL_REQUESTED}
    ),
    # 终态：不可逆
    TaskStatus.DONE: frozenset({TaskStatus.DONE}),
    TaskStatus.FAILED: frozenset({TaskStatus.FAILED}),
    TaskStatus.CANCELLED: frozenset({TaskStatus.CANCELLED}),
    TaskStatus.DEGRADED: frozenset({TaskStatus.DEGRADED}),
}

# 条件合法：从 PAUSED 恢复成 QUEUED 时，只有「被抢占挂起」才该自动恢复。
# 用户主动暂停的任务被调度器重新入队是错的——不替用户做决定。
RESUMMABLE_PAUSED_REASONS = frozenset({PAUSED_BY_PREEMPTION})



def priority_rank(priority: TaskPriority) -> int:
    """数值化优先级，便于排序（越大越先执行）。"""
    return _PRIORITY_RANK.get(priority, 1)


def _new_task_id() -> str:
    """时间戳 + 随机后缀。纯时间戳在同一时刻并发建任务时会撞 id，进而互相覆盖状态。"""
    return f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"


class Task(BaseModel):
    id: str = Field(default_factory=_new_task_id)
    instruction: str
    context: str = ""

    status: TaskStatus = TaskStatus.CREATED
    priority: TaskPriority = TaskPriority.NORMAL

    # 任务关系：子任务挂在父任务下，root 用于把一棵任务树归并到同一个调度单元
    parent_task_id: str | None = None
    root_task_id: str | None = None

    # 任务绑定到哪台设备（V2.1 §十三）。None = 尚未绑定，调度器可以派给任意空闲设备；
    # 一旦开始执行就固定下来——中途换设备会让页面上下文彻底对不上，
    # 相当于把任务丢到一个陌生手机上接着做。
    device_serial: str | None = None

    current_step: int = 0

    # 资源预算：动作 / 观察 / 模型调用三个独立上限（V2.1 §二），取代单一的 max_steps。
    # 旧代码里 max_steps 实际统计的是 Observe 次数，并非动作数，预算语义是错的。
    budget: TaskBudget = Field(default_factory=TaskBudget)

    # 版本号（V2.1 §十七）：SUPER_TASK / re-plan / 计划变更 / 优先级变更 / 人工干预时 +1。
    # 旧 Checkpoint / Decision 如果版本不匹配，一律作废，防止旧指令误执行。
    version: int = 1

    plan: list[TaskStep] = Field(default_factory=list)

    # 任务级的「计划三要素」（v4.3 §1 / v4.4 §五）：模型对这次任务的复述。
    # 它们与 plan 同生共死（都在 `set_plan` 里写），进决策 prompt 与 `/tasks/{id}`，
    # 但**不参与完成裁定**——裁决只认独立证据（`agent/goal_verifier`）。
    plan_goal: str = ""
    """这一趟到底要干什么。写在 prompt 里，让每一步决策都看得到「别跑偏」。"""

    plan_constraints: list[str] = Field(default_factory=list)
    """不许做什么（「不要点广告」「只用微信不要跳浏览器」）。"""

    success_condition: str = ""
    """什么算完成——**给人看**的验收口径，不是裁决依据（v4.3 §1）。"""

    # 计划版本（V2.1 §二十一）。与 version 的区别：
    #   version      = **任务目标**的版本（SUPER_TASK 改写目标才 +1）
    #   plan_version = **计划**的版本（每次重建/插入步骤都 +1）
    # 分开之后，「目标没变但计划被 Re-plan 过」这件事才有办法表达——
    # 否则旧 Checkpoint 只能一律作废，连「页面没变、计划也没变」的安全续跑都要放弃。
    plan_version: int = 0

    # 写入序号（V2.4 §二）：只由持久化层推进，每次成功 save 都 +1。
    # 三个版本号的分工不能混：
    #   version      = **目标**的版本（SUPER_TASK 改写目标才 +1）——语义信息
    #   plan_version = **计划**的版本（重建 / 插步骤 +1）——语义信息
    #   revision     = **这条记录**的写入序号——只服务于乐观并发校验（CAS）
    # 少了它就没法回答「我手里这份状态有没有被别的线程改过」，
    # 于是「Runtime 标记 DONE」和「TaskManager 改写目标」可以同时成功，
    # 落盘变成 done + 新目标（见 ConcurrentModificationError）。
    revision: int = 0

    # 崩溃 / 重启中断标记（V2.6 §七）：`recover()` 发现磁盘上停在 RUNNING 的任务时置位。
    # 含义是「上一次执行是在半途消失的，动作可能已经真的发出去过」——所以进入执行循环
    # 之前必须先把这件事处理掉（有恢复点就对账，没有就转人工），
    # 严禁默认「从 QUEUED 重新规划然后接着点手机」。
    recovery_required: bool = False

    # 崩溃恢复的**上下文说明**（V2.8 §八）：记录「上次动作效果未知」这类事实，
    # 跨重启存活。人工批准继续后**不清空**——「继续」不等于「上次副作用已忽略」，
    # runtime 重新规划时要把它作为 Re-plan 上下文传给 planner，让模型倾向于
    # 「先核验当前状态」而不是「直接重复上次的动作」。
    recovery_note: str = ""

    # 用户明确否决过的动作指纹（V2.7 P0-1）：必须**跨重启存活**。
    # 只存在内存里时，重启后系统会再问一遍用户已经拒绝过的动作——除了骚扰，更糟的是
    # 让人以为「系统没记住我说的话」。
    # 与之相对，**放行凭据（ApprovalGrant）刻意不落盘**：批准是针对当时那一屏给的，
    # 重启后页面可能早就变了，重新请人确认才是正确行为。
    denied_fingerprints: list[str] = Field(default_factory=list)

    # 当前聚焦的步骤（V2.1 §二十一）。由 next_pending_step 选出谁就记谁，
    # 便于 API / 日志直接回答「现在在干哪一步」，不用再从 plan 里推算一遍。
    active_step_id: str | None = None

    # 关系判定元数据（V2.1 §二十一）：这个任务是被判成什么关系才产生的。
    # 出问题时能回溯「它为什么会被并进来 / 为什么会抢占别人」。
    relation_meta: dict[str, object] = Field(default_factory=dict)

    # 最近一次 Checkpoint，用于暂停后恢复
    checkpoint_id: str | None = None

    # 仅当 status 为 PAUSED 时有意义：区分用户暂停与抢占挂起（见 PAUSED_BY_* 常量）
    paused_reason: str | None = None

    # 是否允许被打断 / 是否允许恢复，由调度器读取
    interruptible: bool = True
    resumable: bool = True

    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    # 非法状态迁移的累计次数（V2.2 §十）。> 0 就说明有调用方绕过了正确的状态机，
    # 放在 Task 上而不是只写日志，是因为它会被 /tasks/{id} 直接读出来。
    illegal_transition_count: int = 0

    # ---- 状态 ----

    def mark(
        self, status: TaskStatus, *, paused_reason: str | None = None, source: str = ""
    ) -> None:
        """切换状态（所有状态写入的统一入口）。

        `paused_reason` 只在 PAUSED 时有意义，切到其它状态会被自动清空，
        避免把「上次为什么暂停」的信息带到下一次运行里。

        `source` 回答「谁改了这个状态」，审计与排查时 indispensable。

        真正干活的是 `transition_to`——它按 `ALLOWED_TRANSITIONS` 校验。
        终态（DONE/FAILED/CANCELLED/DEGRADED）不允许再迁出；
        其它非法迁移仍先记 warning 并执行，避免运行时状态卡住，但要能被看见。
        """
        self.transition_to(status, paused_reason=paused_reason, source=source)

    def apply_event(self, event: TaskEvent, *, source: str = "") -> None:
        """按**语义事件**迁移状态（V2.7 P1-5）。

        这是事件驱动迁移的入口：调用方声明「发生了什么」，目标状态由 `_EVENT_TO_STATUS`
        统一裁决，而不是各自写 `TaskStatus.X`。`source` 仍会被带入 `transition_to`
        用于审计，终态硬闸与非法迁移告警的语义**完全不变**。

        两个暂停事件会带上 `paused_reason`，其余事件自动清空它。
        """
        paused_reason = None
        if event is TaskEvent.PAUSED_BY_USER:
            paused_reason = PAUSED_BY_USER
        elif event is TaskEvent.PAUSED_BY_PREEMPTION:
            paused_reason = PAUSED_BY_PREEMPTION
        self.transition_to(
            _EVENT_TO_STATUS[event], paused_reason=paused_reason, source=source
        )

    def transition_to(
        self,
        status: TaskStatus,
        *,
        paused_reason: str | None = None,
        source: str = "",
    ) -> bool:
        """状态迁移的唯一实现，返回「这次迁移是否合法」。

        V2.2 §十/§十一：以前 TaskManager / Scheduler / Runtime / API 各自写
        `task.status = x`，于是「Runtime 想拒绝后继续、API 却直接 FAILED」这类
        跨模块语义冲突几乎无法避免——因为没有一处能回答「这个迁移该不该发生」。
        `source` 让 warning 能指出是谁做的，排查时不用猜。

        V2.3：终态不可逆是安全底线。终态任务一旦再被迁出，直接抛
        `InvalidTransitionError`——这比「记 warning 但执行」更能防止副作用重复。
        """
        if self.is_terminal and status is not self.status:
            self.illegal_transition_count += 1
            raise InvalidTransitionError(
                self.id, self.status.value, status.value, source=source or "未标注"
            )

        legal = status in ALLOWED_TRANSITIONS.get(self.status, frozenset())
        if not legal:
            self.illegal_transition_count += 1
            logger.warning(
                "任务 %s 非法状态迁移 %s → %s（来源 %s），已执行但请检查调用方",
                self.id,
                self.status.value,
                status.value,
                source or "未标注",
            )
        self.status = status
        self.paused_reason = paused_reason if status is TaskStatus.PAUSED else None
        self.updated_at = datetime.now()
        return legal

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def rank(self) -> int:
        return priority_rank(self.priority)

    # ---- 步骤 ----

    def set_plan(self, plan) -> None:
        """用一份计划重建计划相关字段。计划一变，plan_version 就 +1。

        ``plan`` 收四种形状（归一化在 `TaskPlan.from_payload`）：
        `TaskPlan` / 步骤列表（历史形状：`["打开微信", …]` 或带
        `expected_state` 的 dict 列表）/ 模型返回的完整 dict（含任务级的
        `goal` / `constraints` / `success_condition`）/ `None`（空计划）。

        **任务级三要素只在这一处写入**：它们是「模型对这次任务的复述」，
        与 `plan` 同生共死（重新规划时一起换），否则会出现
        「新计划配旧约束」这种没人能解释的组合。
        """
        parsed = TaskPlan.from_payload(plan)
        self.plan = list(parsed.steps)
        self.plan_goal = parsed.goal
        self.plan_constraints = list(parsed.constraints)
        self.success_condition = parsed.success_condition
        self.plan_version += 1
        self.active_step_id = None
        self.sync_current_step()
        self.updated_at = datetime.now()

    def plan_context_lines(self) -> list[str]:
        """任务级计划上下文（目标 / 约束 / 完成条件），给决策 prompt 与界面用。"""
        return TaskPlan(
            goal=self.plan_goal,
            constraints=tuple(self.plan_constraints),
            success_condition=self.success_condition,
        ).context_lines()

    def step_states(self) -> dict[str, StepStatus]:
        return {step.id: step.status for step in self.plan}

    def get_step(self, step_id: str) -> TaskStep | None:
        for step in self.plan:
            if step.id == step_id:
                return step
        return None

    def next_pending_step(self) -> TaskStep | None:
        """下一个可执行的步骤。依赖未完成的步骤不会被选中。

        顺带把 active_step_id 更新为选中的步骤（V2.1 §二十一）。
        """
        done_ids = {s.id for s in self.plan if s.status in {StepStatus.DONE, StepStatus.SKIPPED}}
        for step in self.plan:
            if step.status is not StepStatus.PENDING:
                continue
            if all(dep in done_ids for dep in step.depends_on):
                self.active_step_id = step.id
                return step
        self.active_step_id = None
        return None

    def sync_current_step(self) -> None:
        """把已结束的步骤数写回 current_step，让进度可读。"""
        self.current_step = sum(
            1 for s in self.plan if s.status in {StepStatus.DONE, StepStatus.SKIPPED}
        )

    def plan_progress(self) -> str:
        """形如 `2/4 done · s3 running`，用于日志与 API 展示。"""
        if not self.plan:
            return "无计划"
        total = len(self.plan)
        finished = sum(1 for s in self.plan if s.status in {StepStatus.DONE, StepStatus.SKIPPED})
        running = next((s.id for s in self.plan if s.status is StepStatus.RUNNING), None)
        tail = f" · {running} running" if running else ""
        return f"{finished}/{total} done{tail}"
