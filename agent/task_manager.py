"""TaskManager（V2 §十）：任务生命周期，以及「执行过程中插入新指令」的完整流程（V2 §二十一）。

关系判定的四种落点：
- SUBTASK     → 并入当前任务的计划，插入到下一个待执行步骤之前（「先……」语义）
- INTERRUPT   → 建新任务并抢占当前任务
- UNRELATED   → 建独立任务排队；优先级高于当前任务时同样触发抢占
- DUPLICATE   → 直接复用已有任务，不重复执行
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Callable

from models.budget import TaskBudget
from models.exceptions import ConcurrentModificationError
from models.retry import DEFAULT_POLICY
from models.task import Task, TaskEvent, TaskPriority, TaskStatus, priority_rank
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
    def __init__(
        self,
        *,
        store,
        scheduler,
        classifier: TaskClassifier | None = None,
        runtime=None,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._classifier = classifier or TaskClassifier()
        # 只有「人工处理待确认事项」需要 runtime（危险动作 / 完成裁定都在它那里）。
        # 拿它进来是有意的（V2.2 §九/§十）：以前 API 自己调 runtime.confirm 再自己改
        # task 状态，于是 Runtime 的语义（拒绝后换策略）和 API 的语义（直接 FAILED）
        # 各写各的，冲突不可避免。现在这条路径只有 TaskManager 一个出口。
        self._runtime = runtime

        # 改写「当前任务」时的互斥锁（V2.4 §三）：inject / complete / fail /
        # resolve_confirmation / pause / resume / cancel 都是「读出状态 → 判断能不能改
        # → 改 → 落盘」四步，任意两步之间被别的线程插进来，判断就失效了（TOCTOU）。
        #
        # 但它**不是万能的**：Runtime 与 Scheduler 拿着同一批 Task 实例，却不经过这把锁，
        # 所以互斥只在本模块内部成立。真正的兜底是 TaskStore 的 revision CAS
        # （见 `_rewrite`）——锁只是把窗口缩到可忽略。
        self._mutation_lock = threading.RLock()

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
        allowed_devices: frozenset[str] | None = None,
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
        task.apply_event(TaskEvent.CREATED, source="task_manager")
        self._store.save(task)
        if submit:
            self._scheduler.submit(task, allowed_devices=allowed_devices)
        return task

    def get(self, task_id: str) -> Task | None:
        # 调度器内存里有更实时的状态（队列/暂停区），持久化层是兜底
        task = self._scheduler.get(task_id)
        if task is not None:
            return task
        return self._store.load(task_id)

    def list_all(self) -> list[Task]:
        """列出全部任务，**live 状态优先**（V2.5 §五）。

        磁盘只是兜底，调度器内存才是当前进程的实时真相：任务刚被判 RUNNING 但还没
        落盘时，只读磁盘会让 `GET /tasks` 显示 queued，而 `GET /tasks/{id}`（走
        `get()`，优先内存）显示 running —— 同一时刻两个答案，排查 Agent 时最怕这个。
        所以这里跟 `get()` 用同一套优先级，并对齐 `revision` 较大的那份。
        """
        merged: dict[str, Task] = {t.id: t for t in self._scheduler.tracked_tasks()}
        for task in self._store.list_all():
            merged.setdefault(task.id, task)
        return sorted(merged.values(), key=lambda t: t.created_at)

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
        """把任务标记为完成（外部入口）。

        V2.4 §三：与 inject 共用 `_mutation_lock`。

        V2.5 §六：**不接受正在运行的任务**。Runtime 才是「任务做完了没有」的权威，
        从外部把 RUNNING 直接改成 DONE 的话，Runtime 下一轮仍会继续 Observe / Think /
        Act（`DONE → DONE` 是合法自迁移，终态硬闸拦不住），于是「DONE」不再意味着
        「已经停止」。要让运行中的任务结束，请走 `/cancel`（安全点让出）或让它跑完。
        返回 None 表示「这次调用什么都没做」。
        """
        with self._mutation_lock:
            task = self.get(task_id)
            if task is None:
                return None
            if task.status is TaskStatus.RUNNING:
                logger.warning("任务 %s 正在执行，拒绝从外部直接标记完成（V2.5 §六）", task_id)
                return None
            task.sync_current_step()
            task.apply_event(TaskEvent.COMPLETED, source="task_manager")
            self._store.save(task)
            return task

    def fail(self, task_id: str) -> Task | None:
        """把任务标记为失败（外部入口）。同样拒绝 RUNNING（V2.5 §六）。"""
        with self._mutation_lock:
            task = self.get(task_id)
            if task is None:
                return None
            if task.status is TaskStatus.RUNNING:
                logger.warning("任务 %s 正在执行，拒绝从外部直接标记失败（V2.5 §六）", task_id)
                return None
            task.apply_event(TaskEvent.FAILED, source="task_manager")
            self._store.save(task)
            return task

    def pause(self, task_id: str) -> bool:
        # 也走 _mutation_lock（V2.4 §三）：pause / cancel 与 inject 都想改同一个任务时，
        # 必须有个明确先后，不能各改一半。
        with self._mutation_lock:
            return self._scheduler.pause(task_id)

    def resume(self, task_id: str, *, allowed_devices: frozenset[str] | None = None) -> bool:
        with self._mutation_lock:
            return self._scheduler.resume(task_id, allowed_devices=allowed_devices)

    def cancel(self, task_id: str) -> bool:
        with self._mutation_lock:
            return self._scheduler.cancel(task_id)

    # ---- 人工处理待确认事项（V2.2 §三 / §十一）----

    def confirmation_kind(self, task_id: str) -> str:
        """这条任务在等什么：「dangerous_action」/「goal」/「recovery」/「none」。"""
        if self._runtime is None or not self.confirmation_pending(task_id):
            return "none"
        if self._runtime.recovery_pending(task_id) is not None:
            return "recovery"
        return "goal" if self._runtime.is_goal_decision(task_id) else "dangerous_action"

    def confirmation_pending(self, task_id: str) -> bool:
        """有没有待人工处理的事项（危险动作 / 完成裁定 / 崩溃恢复）。"""
        if self._runtime is None:
            return False
        return (
            self._runtime.pending_confirmation(task_id) is not None
            or self._runtime.is_goal_decision(task_id)
            or self._runtime.recovery_pending(task_id) is not None
        )

    def resolve_confirmation(self, task_id: str, *, approved: bool) -> Task | None:
        """处理一次人工确认，返回更新后的任务；没有待确认事项时返回 None。

        **两种确认的落点是一样的：都重新入队继续做。**

        这正是审核指出的那个跨模块冲突（V2.2 §三）：

            Runtime：危险动作被否决 → 拉黑该动作 → 换策略继续
            API    ：否决 → task.mark(FAILED)

        两套语义打架，实际行为是「任务被直接判死」，与设计文档相反。
        归拢之后只有一条规则——

            否决危险动作 = 这个动作不要了，换一种做法接着完成
            否决完成申请 = 还没做完，接着做
            只有 USER_CANCEL 才该让任务结束

        真正「放弃任务」的入口是 `/cancel`，不是「否决某个动作」。
        """
        if self._runtime is None:
            return None
        # V2.4 §三：确认也是「改当前任务」，与 inject 用同一把锁——
        # 否则「确认放行」与「SUPER_TASK 改写目标」可能同时生效。
        with self._mutation_lock:
            task = self.get(task_id)
            if task is None or not self.confirmation_pending(task_id):
                return None
            kind = self.confirmation_kind(task_id)

            if kind == "recovery" and not approved:
                # V2.6 §七：人判断「不要继续」——不再自动重跑，落在 DEGRADED 等人工处置
                if not self._runtime.confirm(task_id, approved):
                    return None
                task.apply_event(TaskEvent.DEGRADED, source="task_manager")
                self._store.save(task)
                logger.warning("任务 %s 的崩溃恢复被否决，转入 degraded 等人工处置", task_id)
                return task

            if not self._runtime.confirm(task_id, approved):
                return None

            if kind == "recovery":
                # 人确认「可以继续」→ 重新规划：旧的 plan 是在崩溃前那份状态上排的，
                # 沿用它等于假装那次中断没发生过（V2.6 §七）。
                # V2.8 §八：**但「继续」不等于「上次副作用已忽略」**——`recovery_note`
                # 里记着「上次动作效果未知」，这里**刻意不清空**，让它跟着任务进下一轮，
                # runtime 重新规划时会把它作为 Re-plan 上下文，让模型先核验当前状态
                # 而不是直接重复上次的动作（否则「发送/下单/支付/删除」可能做第二遍）。
                task.recovery_required = False
                task.plan = []
                task.plan_version += 1
                task.checkpoint_id = None

            # 无论批准还是否决，都要重新入队：
            #  批准 → 放行那一次危险动作 / 认定完成 / 放行崩溃恢复
            #  否决 → 按 runtime 记下的黑名单或 Re-plan 理由，换一种做法继续
            self._scheduler.submit(task, allow_preempt=False)
            logger.info(
                "任务 %s 的待确认事项已处理（approved=%s, kind=%s）", task_id, approved, kind
            )
            return task

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
        allowed_devices: frozenset[str] | None = None,
    ) -> InjectResult:
        """执行过程中插入新指令，由任务关系决定落点。

        SUPER_TASK 会**改写正在执行任务的目标**——这是不可逆操作（旧目标就丢了），
        所以除了要过专属置信度门槛，还必须由调用方显式传 ``allow_disruptive=True``
        放行；否则返回 ``NEEDS_CONFIRMATION``，什么都不会改（V2.1 §九）。
        """
        # V2.4 §三：整段「读当前任务 → 判关系 → 改写 → 落盘」都在同一把锁里。
        # 以前是「先读、放锁、过一会儿再改」，中间任何一步被并发写插进来，
        # 判断都会失效（TOCTOU）。
        with self._mutation_lock:
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
            ):
                merged = self._rewrite_authoritative(
                    current, lambda base: self._merge_subtask(base, instruction)
                )
                if merged is not None:
                    task_now, step = merged
                    return InjectResult(
                        relation=relation,
                        action=InjectAction.MERGED,
                        task=task_now,
                        message=f"并入任务 {task_now.id}，插入步骤 {step.id}：{instruction}",
                    )
                # 改写失败（任务已结束 / 期间被别人改过）就落到下面「另起任务」：
                # 用户说的事照样要做，只是不再往一条已经结束的任务里塞步骤。

            if (
                relation.relation is TaskRelation.SUPER_TASK
                and relation.is_actionable
                and current is not None
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
                # 改写落在**磁盘权威快照**上并用 revision 做 CAS（V2.4 §二）：
                # 若这期间 Runtime 已经把任务判成 DONE，这次改写会失败，而不是把
                # 「status=done + instruction=新目标」写进磁盘。
                def _supersede(base: Task) -> None:
                    base.instruction = instruction
                    base.version += 1             # 版本+1，旧 Checkpoint / 旧计划随之失效
                    base.plan = []                # 旧计划作废，恢复时重新规划新目标
                    base.plan_version += 1        # 计划也换版本，便于回溯恢复点属于哪版计划
                    base.checkpoint_id = None     # 旧 Checkpoint 因版本不匹配自动失效
                    base.priority = TaskPriority.HIGH

                rewritten = self._rewrite_authoritative(current, _supersede)
                if rewritten is not None:
                    task_now, _ = rewritten
                    if task_now.status is TaskStatus.RUNNING:
                        # 正在跑：请求**这一个任务**在下一个安全点让位（V2.2 §二）。
                        # 必须带 task_id —— 不带的话 scheduler 会退化成「所有车道都让出」，
                        # 多设备场景下 A 任务被改写会把 B 设备上毫不相干的任务也一起打断。
                        self._scheduler.preempt_running(task_now.id)
                    else:
                        self._scheduler.submit(
                            task_now, allow_preempt=False, allowed_devices=allowed_devices
                        )
                    return InjectResult(
                        relation=relation,
                        action=InjectAction.SUPERSEDED,
                        task=task_now,
                        message=f"任务 {task_now.id} 被重构为新目标（v{task_now.version}）：{instruction}",
                    )
                # 任务已经结束（或被并发改过）→ 不再改写，改为新建任务承接新目标。

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
                # 设备授权范围一路传下去（V2.2 §二）：未绑定的新任务只能落在被允许的设备上，
                # 绝不能因为「没指定 serial」就漂到别人的设备上
                allowed_devices=allowed_devices,
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

    def _rewrite_authoritative(
        self, current: Task, mutate: Callable[[Task], Any]
    ) -> tuple[Task, Any] | None:
        """改写「当前任务」：内存取最新真相，落盘用 revision 做 CAS（V2.4 §二）。

        为什么要 CAS，而不是「改完直接 save」：`current` 是调度器内存里那份**实时**
        对象，可能比磁盘新（比如刚被判为 RUNNING 还没落盘），所以改写必须作用在它
        身上；但「检查过不是终态」与「真正写入」之间隔着一段时间，Runtime 完全可能
        在这段时间里把任务判成 DONE 并落盘。于是写入时带上磁盘 revision 校验：
        期间被人写过就抛 ConcurrentModificationError，本次改写不生效，并把内存改动
        回滚掉（否则内存改了、磁盘没改，两边永久分叉）。

        返回 `(任务, mutate 的返回值)`；任务已结束 / 快照读不到 / revision 冲突时返回
        None，调用方据此退化成「另起一个新任务」，而不是去改写一条已结束的任务。
        """
        on_disk = self._store.load(current.id)
        if on_disk is None:
            logger.warning("任务 %s 的持久化快照不可用，本次改写不生效", current.id)
            return None
        if current.is_terminal or on_disk.is_terminal:
            logger.info(
                "任务 %s 已处于终态（内存 %s / 磁盘 %s），本次改写不生效",
                current.id,
                current.status.value,
                on_disk.status.value,
            )
            return None
        backup = current.model_copy(deep=True)

        def _rollback() -> None:
            # 回滚内存：否则内存已经改了、磁盘还是旧的，两边会永久分叉
            for name in type(backup).model_fields:
                setattr(current, name, getattr(backup, name))

        try:
            result = mutate(current)
            self._store.save(current, expected_revision=on_disk.revision)
        except ConcurrentModificationError as exc:
            _rollback()
            logger.warning("任务 %s 在改写期间被并发修改，本次改写不生效：%s", current.id, exc)
            return None
        except Exception:
            # V2.5 §二：**任何** save 失败都要回滚，不能只处理 CAS 冲突。
            # 磁盘满 / 权限 / 序列化失败时内存已经 mutate 过了（instruction 换了、
            # plan 清空了），若直接抛出去，调用方拿到异常、内存却停在「新目标」——
            # 正是我们一直在防的那条分叉。回滚之后再抛，让上层按普通故障处理。
            _rollback()
            logger.exception("任务 %s 改写后持久化失败，已回滚内存状态", current.id)
            raise
        return current, result

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
