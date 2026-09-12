"""TaskScheduler（V2 §六 / §七）：决定「谁现在用设备」。

与 `_DEVICE_LOCK` 的区别：那把锁只保证不并发点击，拿不到就 409；
Scheduler 负责排队、按优先级挑任务、必要时让当前任务让位（抢占），
并在高优先级任务跑完后把被抢占的任务**恢复**回来。

线程模型：单 Worker 线程 + 单设备。刻意不上 asyncio/Celery ——
下层 observer / executor 都是阻塞的 subprocess 调用，异步化会把整条链路都拖下水。
"""
from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from typing import Any, Protocol

from device.session import DeviceSession
from models.task import (
    PAUSED_BY_PREEMPTION,
    PAUSED_BY_USER,
    Task,
    TaskStatus,
    priority_rank,
)

logger = logging.getLogger(__name__)


class RuntimeLike(Protocol):
    """Scheduler 只依赖 runtime 的这一个方法，方便测试替换。"""

    def run(self, task: Task) -> Any: ...


class TaskScheduler:
    def __init__(
        self,
        runtime: RuntimeLike,
        session: DeviceSession,
        *,
        task_store: Any | None = None,
        idle_poll_seconds: float = 0.2,
    ) -> None:
        self._runtime = runtime
        self._session = session
        self._task_store = task_store
        self._idle_poll = idle_poll_seconds

        self._cond = threading.Condition(threading.RLock())
        self._ready: list[tuple[int, float, int, Task]] = []
        self._paused: dict[str, Task] = {}
        self._suspended: list[Task] = []
        self._completed: list[str] = []
        self._running: Task | None = None
        # 区分「runtime 真的在跑」和「worker 正拿着它等设备」——
        # 后者只能直接切状态，请求让出是没有意义的
        self._executing = False
        self._counter = itertools.count()
        self._stopping = False
        self._worker: threading.Thread | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stopping = False

        # 先重建队列再起 worker：反过来的话 worker 会对着空队列空转，
        # 恢复出来的任务也可能和刚提交的任务抢设备
        restored = self.recover()
        if any(restored.values()):
            logger.info("从磁盘恢复任务：%s", restored)

        self._worker = threading.Thread(target=self._worker_loop, name="shadow-scheduler", daemon=True)
        self._worker.start()
        logger.info("调度器已启动（单设备 / 单 Worker）")

    def stop(self, timeout: float = 5.0) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=timeout)

    # ---- 启动恢复 ----

    def recover(self) -> dict[str, int]:
        """从持久化层重建内存队列（V2 §23 第七阶段：A → B → 重启 → A）。

        内存队列随进程一起消失，但 TaskStore 里还留着未完成的任务。不重建的话，
        这些任务会变成「从 `/tasks/{id}` 看还活着、但永远没人执行」的僵尸——
        比直接失败更难排查。

        返回各类任务的恢复数量，方便启动日志与测试断言。幂等：已在内存队列里的不会重复投递。
        """
        if self._task_store is None:
            return {}

        restored = {"queued": 0, "resuming": 0, "paused": 0}
        with self._cond:
            for task in self._task_store.list_active():
                if self._find_locked(task.id) is not None:
                    continue

                if task.status is TaskStatus.PAUSED and task.paused_reason == PAUSED_BY_USER:
                    # 用户主动暂停的：重启后保持暂停，不替他做决定
                    self._paused[task.id] = task
                    restored["paused"] += 1
                elif task.status is TaskStatus.PAUSED:
                    # 被抢占挂起的：自动恢复，但仍要排在抢占者之后（_pop_next 会做优先级比较）
                    self._suspended.append(task)
                    restored["resuming"] += 1
                else:
                    # created / queued / running / waiting
                    # running 说明上次是进程被杀、动作已中断 —— 交给 Checkpoint 校验后重跑；
                    # waiting 说明在等人工确认，重启后重新决策一次比沿用旧确认更安全
                    task.mark(TaskStatus.QUEUED)
                    self._push_ready(task)
                    restored["queued"] += 1

                self._persist(task)

            self._cond.notify_all()

        return restored

    def _push_ready(self, task: Task) -> None:
        """把任务放进就绪队列。调用方需持有 `self._cond`。"""
        heapq.heappush(
            self._ready,
            (-priority_rank(task.priority), task.created_at.timestamp(), next(self._counter), task),
        )

    # ---- 提交与状态变更 ----

    def submit(self, task: Task, *, allow_preempt: bool = True) -> Task:
        """把任务放进就绪队列；若正在跑的任务优先级更低且可打断，则请求抢占。"""
        task.mark(TaskStatus.QUEUED)
        self._persist(task)

        with self._cond:
            self._push_ready(task)
            self._cond.notify_all()

        if allow_preempt:
            self._maybe_preempt(task)
        return task

    def pause(self, task_id: str) -> bool:
        """用户显式暂停。

        只有 runtime 真的在跑时才需要「请求让出」；任务还在排队、或正被 worker
        拿捏着等设备时，直接切到暂停即可——那时发让出请求没人会响应。
        """
        with self._cond:
            task = self._find_locked(task_id)
            if task is None or task.is_terminal:
                return False

            if self._executing and self._running is not None and self._running.id == task_id:
                self._session.request_preempt(task_id)
                return True

            self._paused[task_id] = task
            self._ready = [entry for entry in self._ready if entry[3].id != task_id]
            self._suspended = [t for t in self._suspended if t.id != task_id]
            task.mark(TaskStatus.PAUSED, paused_reason=PAUSED_BY_USER)
        self._persist(task)
        return True

    def resume(self, task_id: str) -> bool:
        with self._cond:
            task = self._paused.pop(task_id, None)
            if task is None or task.is_terminal:
                return False
            if not task.resumable:
                self._paused[task_id] = task
                return False
            task.mark(TaskStatus.QUEUED)
            self._push_ready(task)
            self._cond.notify_all()
        self._persist(task)
        return True

    def cancel(self, task_id: str) -> bool:
        """取消任务。运行中的只是打标记，由 runtime 在下一个安全点退出——
        强杀线程不可能安全地「在动作执行到一半」停下。"""
        with self._cond:
            task = self._find_locked(task_id)
            if task is None or task.is_terminal:
                return False

            if self._executing and self._running is not None and self._running.id == task_id:
                task.mark(TaskStatus.CANCELLED)
                self._persist(task)
                return True

            self._paused.pop(task_id, None)
            self._suspended = [t for t in self._suspended if t.id != task_id]
            self._ready = [entry for entry in self._ready if entry[3].id != task_id]
            task.mark(TaskStatus.CANCELLED)
        self._persist(task)
        return True

    def preempt(self, by_task_id: str) -> bool:
        """让当前任务让位给指定任务。"""
        return self._maybe_preempt_by_id(by_task_id)

    def preempt_running(self) -> bool:
        """让当前正在运行的任务在下一个安全点让位。

        用于 SUPER_TASK：任务目标已被改写，需要让出设备、从新目标重新规划（V2.1 §七）。
        与 `preempt(by_task_id)` 不同——这里没有「优先级比较」，被抢占的就是 running 自身。
        """
        with self._cond:
            running = self._running
        if running is None or not running.interruptible:
            return False
        return self._session.request_preempt(running.id)

    # ---- 查询 ----

    def snapshot(self) -> dict[str, Any]:
        with self._cond:
            return {
                "running": self._running.id if self._running else None,
                "running_instruction": self._running.instruction if self._running else None,
                "ready": [entry[3].id for entry in sorted(self._ready)],
                "paused": sorted(self._paused),
                "suspended": [t.id for t in self._suspended],
                "completed": list(self._completed[-20:]),
                "device": self._session.snapshot(),
            }

    def get(self, task_id: str) -> Task | None:
        with self._cond:
            return self._find_locked(task_id)

    # ---- 内部 ----

    def _find_locked(self, task_id: str) -> Task | None:
        if self._running is not None and self._running.id == task_id:
            return self._running
        if task_id in self._paused:
            return self._paused[task_id]
        for task in self._suspended:
            if task.id == task_id:
                return task
        for entry in self._ready:
            if entry[3].id == task_id:
                return entry[3]
        return None

    def _maybe_preempt(self, newcomer: Task) -> bool:
        return self._maybe_preempt_by_id(newcomer.id)

    def _maybe_preempt_by_id(self, by_task_id: str) -> bool:
        with self._cond:
            running = self._running
        if running is None or running.id == by_task_id:
            return False
        if not running.interruptible:
            logger.info("任务 %s 不可打断，%s 只能排队", running.id, by_task_id)
            return False

        with self._cond:
            pending = next((entry[3] for entry in self._ready if entry[3].id == by_task_id), None)
        if pending is None:
            return False
        if priority_rank(pending.priority) <= priority_rank(running.priority):
            return False

        return self._session.request_preempt(by_task_id)

    def _persist(self, task: Task) -> None:
        if self._task_store is not None:
            try:
                self._task_store.save(task)
            except Exception:  # noqa: BLE001 - 持久化失败不应中断调度
                logger.exception("保存任务 %s 失败", task.id)

    def _pop_next(self) -> Task | None:
        """取下一个任务：**恢复优先，但不能压过抢占者**。

        如果直接「有挂起就先恢复」，被抢占的 A 会在抢占者 B 开跑之前就抢回设备，
        抢占就永远生效不了。所以这里比较两者的优先级再决定。
        """
        with self._cond:
            while not self._stopping:
                self._suspended = [
                    t
                    for t in self._suspended
                    if not t.is_terminal and t.status is not TaskStatus.CANCELLED
                ]

                suspended_top = self._suspended[0] if self._suspended else None
                ready_top_rank = -self._ready[0][0] if self._ready else None

                if suspended_top is not None and (
                    ready_top_rank is None or priority_rank(suspended_top.priority) >= ready_top_rank
                ):
                    self._suspended.pop(0)
                    # 取出即登记，不留「已出队但还没标记为运行中」的空窗——
                    # 那段时间里任务不属于任何容器，pause/cancel 会找不到它
                    self._running = suspended_top
                    return suspended_top

                if self._ready:
                    task = heapq.heappop(self._ready)[3]
                    self._running = task
                    return task

                self._cond.wait(timeout=self._idle_poll)
            return None

    def _worker_loop(self) -> None:
        while True:
            task = self._pop_next()
            if task is None:
                return
            try:
                self._execute(task)
            except Exception:  # noqa: BLE001 - Worker 线程绝不能因单个任务而退出
                logger.exception("任务 %s 执行时发生未捕获异常", task.id)
                with self._cond:
                    task.mark(TaskStatus.FAILED)
                    self._completed.append(task.id)
                self._persist(task)

    def _execute(self, task: Task) -> None:
        if task.is_terminal:
            # 出队到执行之间任务可能已被取消；_pop_next 已把 _running 指向它，这里要还回去
            with self._cond:
                if self._running is not None and self._running.id == task.id:
                    self._running = None
                self._cond.notify_all()
            return

        # _running 已由 _pop_next 在出队时设置（消除空窗），这里只是重申一次语义
        with self._cond:
            self._running = task

        acquired = False
        try:
            # 设备可能被单步调试端点（/tap、/actions）临时占着，拿不到就稍后重试
            if not self._session.acquire(task.id):
                logger.info("设备忙，任务 %s 稍后重试", task.id)
                with self._cond:
                    self._push_ready(task)
                    self._cond.wait(timeout=self._idle_poll)
                return

            acquired = True
            if task.status is not TaskStatus.RUNNING:
                task.mark(TaskStatus.RUNNING)
                self._persist(task)
            logger.info("开始执行任务 %s：%s", task.id, task.instruction)

            with self._cond:
                self._executing = True
            try:
                outcome = self._runtime.run(task)
            finally:
                with self._cond:
                    self._executing = False
            self._handle_outcome(task, outcome)
        finally:
            with self._cond:
                self._running = None
            if acquired:
                self._session.release(task.id)

    def _handle_outcome(self, task: Task, outcome: Any) -> None:
        """把 runtime 的结果映射回任务状态。

        SUSPENDED 的任务进 `_suspended` 而不是就绪队列：被抢占的任务要**优先恢复**，
        否则不断到来的新任务会让它永远等不到设备。
        """
        name = getattr(outcome, "value", str(outcome))

        if name == "done":
            task.mark(TaskStatus.DONE)
        elif name == "cancelled":
            task.mark(TaskStatus.CANCELLED)
        elif name == "suspended":
            task.mark(TaskStatus.PAUSED, paused_reason=PAUSED_BY_PREEMPTION)
            with self._cond:
                self._suspended.append(task)
            logger.info("任务 %s 已挂起（让出设备），等待恢复", task.id)
        elif name == "awaiting_confirmation":
            # 等人工确认：既不算完成也不算失败，**不能**放进任何队列——
            # 放进去会被 worker 立刻取出重跑，再次撞上同一个危险动作，变成死循环。
            task.mark(TaskStatus.WAITING)
            logger.info("任务 %s 等待人工确认危险动作", task.id)
        else:
            task.mark(TaskStatus.FAILED)

        if task.is_terminal:
            with self._cond:
                self._completed.append(task.id)
        self._persist(task)
