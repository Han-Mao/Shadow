"""Task 模型（V2 §二）：任务不再只是「一次请求」，而是可排队、可暂停、可恢复、可抢占的执行单元。"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from .budget import TaskBudget
from .task_step import StepStatus, TaskStep, build_steps


class TaskStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


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

TERMINAL_STATUSES = frozenset({TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED})
# 已进入调度视野、但尚未结束
ACTIVE_STATUSES = frozenset({TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.WAITING})

# 暂停原因。进程重启后只有「用户显式暂停」该继续保持暂停；
# 「被抢占挂起」是调度器临时让位，重启后必须自动恢复，否则任务就被永久搁置了。
PAUSED_BY_USER = "user"
PAUSED_BY_PREEMPTION = "preemption"



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

    # 计划版本（V2.1 §二十一）。与 version 的区别：
    #   version      = **任务目标**的版本（SUPER_TASK 改写目标才 +1）
    #   plan_version = **计划**的版本（每次重建/插入步骤都 +1）
    # 分开之后，「目标没变但计划被 Re-plan 过」这件事才有办法表达——
    # 否则旧 Checkpoint 只能一律作废，连「页面没变、计划也没变」的安全续跑都要放弃。
    plan_version: int = 0

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

    # ---- 状态 ----

    def mark(self, status: TaskStatus, *, paused_reason: str | None = None) -> None:
        """切换状态。

        `paused_reason` 只在 PAUSED 时有意义，切到其它状态会被自动清空，
        避免把「上次为什么暂停」的信息带到下一次运行里。
        """
        self.status = status
        self.paused_reason = paused_reason if status is TaskStatus.PAUSED else None
        self.updated_at = datetime.now()

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

    def set_plan(self, goals: list[str]) -> None:
        """用语义级目标重建计划。计划一变，plan_version 就 +1。"""
        self.plan = build_steps(goals)
        self.plan_version += 1
        self.active_step_id = None
        self.sync_current_step()
        self.updated_at = datetime.now()

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
