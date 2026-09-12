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

from models.task import Task, TaskPriority, TaskStatus, priority_rank
from models.task_relation import TaskRelation, TaskRelationResult
from models.task_step import StepStatus, TaskStep

from .classifier import TaskClassifier

logger = logging.getLogger(__name__)


class InjectAction(str, Enum):
    MERGED = "merged"
    SPAWNED = "spawned"
    PREEMPTED = "preempted"
    DUPLICATE_IGNORED = "duplicate_ignored"


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
        max_steps: int = 10,
        priority: TaskPriority = TaskPriority.NORMAL,
        parent_task_id: str | None = None,
        submit: bool = True,
    ) -> Task:
        task = Task(
            instruction=instruction,
            context=context,
            max_steps=max_steps,
            priority=priority,
            parent_task_id=parent_task_id,
            root_task_id=parent_task_id,
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
        running = self._scheduler.snapshot().get("running")
        return self.get(running) if running else None

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
        max_steps: int = 10,
    ) -> InjectResult:
        """执行过程中插入新指令，由任务关系决定落点。"""
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

        new_priority = priority or (
            TaskPriority.HIGH
            if relation.relation in {TaskRelation.INTERRUPT, TaskRelation.SUPER_TASK}
            else TaskPriority.NORMAL
        )
        new_task = self.create(
            instruction,
            max_steps=max_steps,
            priority=new_priority,
            parent_task_id=current.id if relation.relation is TaskRelation.SUBTASK else None,
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
        """
        index = next(
            (i for i, s in enumerate(task.plan) if s.status is StepStatus.PENDING),
            len(task.plan),
        )
        step = TaskStep(id=f"s{index + 1}_{uuid.uuid4().hex[:3]}", goal=instruction, max_retries=2)

        if index < len(task.plan):
            following = task.plan[index]
            step.depends_on = list(following.depends_on)
            # 原步骤改为等新步骤做完，串行链保持完整
            following.depends_on = [step.id]

        task.plan.insert(index, step)
        task.updated_at = datetime.now()
        return step

    def _maybe_preempt(self, current: Task, newcomer: Task) -> bool:
        if not current.interruptible:
            return False
        if priority_rank(newcomer.priority) <= priority_rank(current.priority):
            return False
        return self._scheduler.preempt(newcomer.id)
