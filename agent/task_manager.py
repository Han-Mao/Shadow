"""TaskManager（V2 §十）：任务生命周期，以及「执行过程中插入新指令」的完整流程（V2 §二十一）。

关系判定的四种落点：
- SUBTASK     → 并入当前任务的计划，插入到下一个待执行步骤之前（「先……」语义）
- INTERRUPT   → 建新任务并抢占当前任务
- UNRELATED   → 建独立任务排队；优先级高于当前任务时同样触发抢占
- DUPLICATE   → 直接复用已有任务，不重复执行
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from enum import Enum

from models.budget import TaskBudget
from models.retry import DEFAULT_POLICY
from models.task import Task, TaskPriority, TaskStatus, priority_rank
from models.task_relation import TaskRelation, TaskRelationResult
from models.task_step import StepStatus, TaskStep

from .classifier import TaskClassifier

logger = logging.getLogger(__name__)


class InjectAction(str, Enum):
    MERGED = "merged"
    SPAWNED = "spawned"
    PREEMPTED = "preempted"
    SUPERSEDED = "superseded"
    """SUPER_TASK：新指令是父任务，重构了当前任务的目标（不另起任务）。"""
    DUPLICATE_IGNORED = "duplicate_ignored"
    NEEDS_CONFIRMATION = "needs_confirmation"
    """判定置信度达标，但该关系会改写/打断在跑任务，需调用方显式放行后才生效。"""


class InjectResult:
    def __init__(
        self,
        *,
        relation: TaskRelationResult,
        action: InjectAction,
        task: Task | None,
        message: str,
    ) -> None:
        self.relation = relation
        self.action = action
        self.task = task
        self.message = message

    def model_dump(self) -> dict:
        return {
            "relation": self.relation.relation.value,
            "confidence": self.relation.confidence,
            "reason": self.relation.reason,
            "signals": self.relation.signals,
            "action": self.action.value,
            "task_id": self.task.id if self.task else None,
            "message": self.message,
            "requires_confirmation": self.action is InjectAction.NEEDS_CONFIRMATION,
        }


class TaskManager:
    def __init__(self, *, store, scheduler, classifier: TaskClassifier | None = None) -> None:
        self._store = store
        self._scheduler = scheduler
        self._classifier = classifier or TaskClassifier()

    # ---- 创建与查询 ----

    def create(
        self,
        instruction: str,
        *,
        context: str = "",
        budget: TaskBudget | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        parent_task_id: str | None = None,
        submit: bool = True,
        relation_meta: dict | None = None,
        device_serial: str | None = None,
    ) -> Task:
        task = Task(
            instruction=instruction,
            context=context,
            budget=budget or TaskBudget(),
            priority=priority,
            parent_task_id=parent_task_id,
            root_task_id=parent_task_id,
            relation_meta=dict(relation_meta or {}),
            device_serial=device_serial,
        )
        task.mark(TaskStatus.CREATED)
        self._store.save(task)
        if submit:
            self._scheduler.submit(task)
        return task

    def get(self, task_id: str) -> Task | None:
        # 调度器内存里有更实时的状态（队列/暂停区），持久化层是兜底
        task = self._scheduler.get(task_id)
        if task is not None:
            return task
        return self._store.load(task_id)

    def list_all(self) -> list[Task]:
        return self._store.list_all()

    def active_task(self) -> Task | None:
        """当前正在执行的任务（多设备时返回优先级最高的那个）。

        旧的 `snapshot()["running"]` 在多设备下只返回**第一台**在跑的任务，
        上层据此以为「系统里只跑了这一个」——这正是审核点名的单设备残留（V2.2 §十）。
        """
        running = self.active_tasks()
        if not running:
            return None
        return max(running, key=lambda task: priority_rank(task.priority))

    def active_tasks(self) -> list[Task]:
        """所有设备上正在执行的任务。业务逻辑请用这个，不要用「唯一 running」。"""
        return [task for task in self._scheduler.running_tasks() if not task.is_terminal]

    # ---- 状态迁移 ----

    def complete(self, task_id: str) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        task.sync_current_step()
        task.mark(TaskStatus.DONE)
        self._store.save(task)
        return task

    def fail(self, task_id: str) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        task.mark(TaskStatus.FAILED)
        self._store.save(task)
        return task

    def pause(self, task_id: str) -> bool:
        return self._scheduler.pause(task_id)

    def resume(self, task_id: str) -> bool:
        return self._scheduler.resume(task_id)

    def cancel(self, task_id: str) -> bool:
        return self._scheduler.cancel(task_id)

    # ---- 注入 ----

    def inject(
        self,
        instruction: str,
        *,
        current_task_id: str | None = None,
        priority: TaskPriority | None = None,
        budget: TaskBudget | None = None,
        max_steps: int = 10,
        allow_disruptive: bool = False,
    ) -> InjectResult:
        """执行过程中插入新指令，由任务关系决定落点。

        SUPER_TASK 会**改写正在执行任务的目标**——这是不可逆操作（旧目标就丢了），
        所以除了要过专属置信度门槛，还必须由调用方显式传 ``allow_disruptive=True``
        放行；否则返回 ``NEEDS_CONFIRMATION``，什么都不会改（V2.1 §九）。
        """
        current = self.get(current_task_id) if current_task_id else self.active_task()

        # 重复检测要把当前任务也算进来：用户对同一个任务又说一遍同样的话，
        # 应当判为 duplicate 而不是当作新的子步骤再跑一遍
        candidates = [t for t in self.list_all() if not t.is_terminal]
        relation = self._classifier.classify(instruction, current=current, candidates=candidates)

        if relation.relation is TaskRelation.DUPLICATE:
            target = self.get(relation.affected_task_id) if relation.affected_task_id else None
            return InjectResult(
                relation=relation,
                action=InjectAction.DUPLICATE_IGNORED,
                task=target,
                message=f"与已有任务重复，不重复执行：{relation.reason}",
            )

        if (
            relation.relation is TaskRelation.SUBTASK
            and relation.is_actionable
            and current is not None
            and not current.is_terminal
        ):
            step = self._merge_subtask(current, instruction)
            self._store.save(current)
            return InjectResult(
                relation=relation,
                action=InjectAction.MERGED,
                task=current,
                message=f"并入任务 {current.id}，插入步骤 {step.id}：{instruction}",
            )

        if (
            relation.relation is TaskRelation.SUPER_TASK
            and relation.is_actionable
            and current is not None
            and not current.is_terminal
        ):
            if relation.requires_second_confirmation and not allow_disruptive:
                return InjectResult(
                    relation=relation,
                    action=InjectAction.NEEDS_CONFIRMATION,
                    task=current,
                    message=(
                        f"判定为父任务但改写目标不可逆，需二次确认后再执行"
                        f"（{relation.describe()}，任务 {current.id} 未被修改）"
                    ),
                )

            # 新指令是「父任务」：重构当前任务目标，而不是另起一个任务（V2.1 §六/§七）。
            current.instruction = instruction
            current.version += 1            # 版本+1，旧 Checkpoint / 旧计划随之失效
            current.plan = []               # 旧计划作废，恢复时重新规划新目标
            current.plan_version += 1       # 计划也换了版本，便于回溯「这条恢复点属于哪一版计划」
            current.checkpoint_id = None    # 旧 Checkpoint 因版本不匹配自动失效
            current.priority = TaskPriority.HIGH
            self._store.save(current)
            if current.status is TaskStatus.RUNNING:
                # 正在跑：请求**这一个任务**在下一个安全点让位（V2.2 §二）。
                # 必须带 task_id —— 不带的话 scheduler 会退化成「所有车道都让出」，
                # 多设备场景下 A 任务被改写会把 B 设备上毫不相干的任务也一起打断。
                self._scheduler.preempt_running(current.id)
            else:
                self._scheduler.submit(current, allow_preempt=False)
            return InjectResult(
                relation=relation,
                action=InjectAction.SUPERSEDED,
                task=current,
                message=f"任务 {current.id} 被重构为新目标（v{current.version}）：{instruction}",
            )

        new_priority = priority or (
            TaskPriority.HIGH
            if relation.relation in {TaskRelation.INTERRUPT, TaskRelation.SUPER_TASK}
            else TaskPriority.NORMAL
        )
        new_task = self.create(
            instruction,
            budget=budget or TaskBudget.from_max_steps(max_steps),
            priority=new_priority,
            parent_task_id=current.id if relation.relation is TaskRelation.SUBTASK else None,
            # 记下「它是被判成什么关系才产生的」，事后能回溯为什么它抢占了别人
            relation_meta={
                "relation": relation.relation.value,
                "confidence": relation.confidence,
                "reason": relation.reason,
                "signals": relation.signals,
                "against_task_id": current.id if current else None,
            },
        )

        preempted = (
            current is not None
            and not current.is_terminal
            and self._maybe_preempt(current, new_task)
        )

        return InjectResult(
            relation=relation,
            action=InjectAction.PREEMPTED if preempted else InjectAction.SPAWNED,
            task=new_task,
            message=(
                f"新建任务 {new_task.id}（{new_priority.value}）"
                + (f"，已请求打断 {current.id}" if preempted and current else "")
                + f"：{relation.reason}"
            ),
        )

    # ---- 内部 ----

    @staticmethod
    def _merge_subtask(task: Task, instruction: str) -> TaskStep:
        """把新目标插到「下一个待执行步骤」之前。

        插到正在执行的步骤之前会打断已发出的动作；插到待执行首位既满足「先……」的语义，
        又不动已经跑了一半的那一步。

        **计划一改就必须 `plan_version += 1`**（V2.2 §十一）：恢复点、Re-plan 上下文、
        审计里都带着这个版本号，不递增的话「这份计划是哪一版」就永远说不清，
        「运行中插入子任务 → 落检查点 → 进程崩溃 → 恢复」这条链尤其容易踩。
        """
        index = next(
            (i for i, s in enumerate(task.plan) if s.status is StepStatus.PENDING),
            len(task.plan),
        )
        step = TaskStep(
            id=f"s{index + 1}_{uuid.uuid4().hex[:3]}",
            goal=instruction,
            max_retries=DEFAULT_POLICY.step_max_retries,
        )

        if index < len(task.plan):
            following = task.plan[index]
            step.depends_on = list(following.depends_on)
            # 原步骤改为等新步骤做完，串行链保持完整
            following.depends_on = [step.id]

        task.plan.insert(index, step)
        # 目标没变（version 不动），但计划确实变了 → plan_version +1。
        # 旧恢复点仍然可用（页面还是那一屏），但能看出它属于上一版计划。
        task.plan_version += 1
        task.sync_current_step()
        task.updated_at = datetime.now()
        return step

    def _maybe_preempt(self, current: Task, newcomer: Task) -> bool:
        if not current.interruptible:
            return False
        if priority_rank(newcomer.priority) <= priority_rank(current.priority):
            return False
        return self._scheduler.preempt(newcomer.id)
