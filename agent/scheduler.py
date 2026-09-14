"""TaskScheduler（V2 §六 / §七 · V2.1 §十三）：决定「谁现在用哪台设备」。

与 `_DEVICE_LOCK` 的区别：那把锁只保证不并发点击，拿不到就 409；
Scheduler 负责排队、按优先级挑任务、必要时让当前任务让位（抢占），
并在高优先级任务跑完后把被抢占的任务**恢复**回来。

线程模型：**每台设备一个 Worker 线程**（V2.1 §十三）。单设备时就是一个线程，
与旧实现逐字等价；多设备时各设备并行，互不阻塞。
刻意不上 asyncio/Celery —— observer / executor 都是阻塞的 subprocess 调用，
异步化会把整条链路都拖下水。

车道（lane）是 V2.1 引入的核心抽象：

    一台设备 = 一条车道（`_DeviceLane`），各自持有就绪队列、挂起区和运行槽。

任务一旦开始执行就**绑定**到某台设备（`Task.device_serial`）——中途换设备会让
页面上下文彻底对不上，等于把任务丢到一台陌生手机上接着做。未绑定的任务由调度器
派给最闲的一台。
"""
from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from device.pool import DevicePool
from device.session import DeviceSession
from models.task import (
    PAUSED_BY_PREEMPTION,
    PAUSED_BY_USER,
    Task,
    TaskStatus,
    priority_rank,
)
from storage.event_log import EventLog, PREEMPT_REQUESTED, QUEUED, RECOVERED, RESUMED, SUSPENDED

logger = logging.getLogger(__name__)

# 抢占延迟的**告警阈值**（秒）。
#
# 为什么没有硬性上限：协作式抢占靠 runtime 在循环安全点自己让出，
# 而安全点之间的间隔取决于「一次 Observe 要多久」——命令已经在设备上跑起来了，
# Python 侧没有安全的方式把它掐断（强行杀 adb 子进程会留下半截设备状态，比多等一会儿更糟）。
#
# 所以延迟上界靠**两层**控制，这里只负责把超出预期的情形报出来：
#   1. `observer.OBSERVE_BUDGET_SECONDS`（12s）——一次采集的总预算，
#      给安全点间隔一个可论证的上界（从「6 条命令各自超时累加」降到「预算 + 1 条」）
#   2. 本条阈值（2s）——超过就 warning，让「高优任务被长命令堵住」能被看见
MAX_PREEMPTION_LATENCY_SECONDS = 2.0


class RuntimeLike(Protocol):
    """Scheduler 只依赖 runtime 的这一个方法，方便测试替换。"""

    def run(self, task: Task) -> Any: ...


@dataclass
class _DeviceLane:
    """一台设备一条车道：自己的就绪队列、挂起区、运行槽。

    把这三样从 Scheduler 的全局字段下沉下来，是多设备能成立的关键——
    否则两台设备会共用一个 `running`，互相把对方的状态覆盖掉。
    """

    serial: str
    session: DeviceSession
    ready: list[tuple[int, float, int, Task]] = field(default_factory=list)
    suspended: list[Task] = field(default_factory=list)
    running: Task | None = None
    # 区分「runtime 真的在跑」和「worker 正拿着它等设备」——
    # 后者只能直接切状态，请求让出是没有意义的
    executing: bool = False
    worker: threading.Thread | None = None

    def push_ready(self, task: Task, counter: itertools.count) -> None:
        heapq.heappush(
            self.ready,
            (-priority_rank(task.priority), task.created_at.timestamp(), next(counter), task),
        )

    def drop(self, task_id: str) -> None:
        """把任务从本车道的就绪队列与挂起区摘掉。"""
        self.ready = [entry for entry in self.ready if entry[3].id != task_id]
        self.suspended = [task for task in self.suspended if task.id != task_id]

    def snapshot(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "running": self.running.id if self.running else None,
            "ready": sorted(entry[3].id for entry in self.ready),
            "suspended": [task.id for task in self.suspended],
            "executing": self.executing,
            "device": self.session.snapshot(),
        }


class TaskScheduler:
    def __init__(
        self,
        runtime: RuntimeLike,
        session: DeviceSession | DevicePool,
        *,
        task_store: Any | None = None,
        idle_poll_seconds: float = 0.2,
        event_log: EventLog | None = None,
    ) -> None:
        self._runtime = runtime
        # 向后兼容：老调用方传的是单个 DeviceSession（单设备场景）
        self._pool = session if isinstance(session, DevicePool) else DevicePool([session])
        if len(self._pool) == 0:
            raise ValueError("至少要注册一台设备，否则任务永远拿不到设备")
        self._task_store = task_store
        self._idle_poll = idle_poll_seconds
        self._event_log = event_log

        self._lanes: dict[str, _DeviceLane] = {
            device.serial: _DeviceLane(serial=device.serial, session=device)
            for device in self._pool
        }

        # 这几个是**全局**的：暂停区不属于任何设备（任务还可能被改派），
        # 完成记录只是审计，用一把 Condition 统管也省得死锁
        self._cond = threading.Condition(threading.RLock())
        self._paused: dict[str, Task] = {}
        self._completed: list[str] = []
        self._counter = itertools.count()
        self._stopping = False

        # 抢占延迟观测（V2.1 §十四）。key 是 task_id（全局唯一），
        # 所以不必下沉到车道；结果也保留"最近一次"给 /scheduler 看
        self._preempt_request_at: dict[str, float] = {}
        self._last_preemption_latency: float | None = None

    # ---- 设备 ----

    @property
    def pool(self) -> DevicePool:
        return self._pool

    def lanes(self) -> list[str]:
        return sorted(self._lanes)

    @property
    def _running(self) -> Task | None:
        """**仅为兼容旧调用方与测试保留**的单设备视图。

        V2.1 起真正的运行槽在 `_DeviceLane.running` 上，每台设备一个。
        这个 property 在多设备下只能返回第一台在跑的任务，用它做业务判断会得出
        「系统里只跑了一个任务」的错误结论——正是审核点名的单设备残留（V2.2 §十）。
        新代码请用 `running_tasks()` 或 `snapshot()["devices"]`。
        """
        for serial in sorted(self._lanes):
            running = self._lanes[serial].running
            if running is not None:
                return running
        return None

    @_running.setter
    def _running(self, task: Task | None) -> None:
        """兼容旧测试的直接赋值（只作用于第一台设备）。业务代码不要用。"""
        self._lanes[sorted(self._lanes)[0]].running = task

    def running_tasks(self) -> list[Task]:
        """所有设备上正在执行的任务（多设备下的正确读法）。"""
        with self._cond:
            return [
                self._lanes[serial].running
                for serial in sorted(self._lanes)
                if self._lanes[serial].running is not None
            ]

    # ---- 生命周期 ----

    def start(self) -> None:
        if any(lane.worker is not None and lane.worker.is_alive() for lane in self._lanes.values()):
            return
        self._stopping = False

        # 先重建队列再起 worker：反过来的话 worker 会对着空队列空转，
        # 恢复出来的任务也可能和刚提交的任务抢设备
        restored = self.recover()
        if any(restored.values()):
            logger.info("从磁盘恢复任务：%s", restored)

        for lane in self._lanes.values():
            lane.worker = threading.Thread(
                target=self._lane_loop,
                args=(lane,),
                name=f"shadow-device-{lane.serial}",
                daemon=True,
            )
            lane.worker.start()
        logger.info("调度器已启动（%d 台设备：%s）", len(self._lanes), ", ".join(self.lanes()))

    def stop(self, timeout: float = 5.0) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        for lane in self._lanes.values():
            if lane.worker is not None:
                lane.worker.join(timeout=timeout)

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

                # 上次绑定的设备可能已经不在池里（拔线/换机）——改派给现有设备，
                # 否则这条任务永远没人取走，会悄悄变成僵尸
                if task.device_serial and task.device_serial not in self._lanes:
                    logger.warning(
                        "任务 %s 原先绑定的设备 %s 不在池中，改派给可用设备",
                        task.id,
                        task.device_serial,
                    )
                    task.device_serial = None

                if task.status is TaskStatus.PAUSED and task.paused_reason == PAUSED_BY_USER:
                    # 用户主动暂停的：重启后保持暂停，不替他做决定
                    self._paused[task.id] = task
                    restored["paused"] += 1
                    restored_as = "paused"
                elif task.status is TaskStatus.PAUSED:
                    # 被抢占挂起的：自动恢复，但仍要排在抢占者之后（_pop_next 会做优先级比较）
                    lane = self._lane_for(task)
                    lane.suspended.append(task)
                    restored["resuming"] += 1
                    restored_as = "resuming"
                else:
                    # created / queued / running / waiting
                    # running 说明上次是进程被杀、动作已中断 —— 交给 Checkpoint 校验后重跑；
                    # waiting 说明在等人工确认，重启后重新决策一次比沿用旧确认更安全
                    previous = task.status.value
                    task.mark(TaskStatus.QUEUED)
                    lane = self._lane_for(task)
                    lane.push_ready(task, self._counter)
                    restored["queued"] += 1
                    restored_as = f"queued(from {previous})"

                self._persist(task)
                self._emit(task.id, RECOVERED, restored_as=restored_as)

            self._cond.notify_all()

        return restored

    # ---- 提交与控制 ----

    def _emit(self, task_id: str, kind: str, **data) -> None:
        if self._event_log is not None:
            self._event_log.emit(task_id, kind, **data)

    def submit(self, task: Task, *, allow_preempt: bool = True) -> Task:
        """把任务放进就绪队列；若正在跑的任务优先级更低且可打断，则请求抢占。"""
        task.mark(TaskStatus.QUEUED)
        with self._cond:
            lane = self._lane_for(task)
            lane.push_ready(task, self._counter)

        # **先落盘、再唤醒 worker**。反过来的话，worker 可能在任务还没持久化时
        # 就开始跑，进程恰在这一刻崩溃就会把任务整个丢掉——
        # 队列是内存的，磁盘上没记就等于没提交过。
        self._persist(task)
        self._emit(task.id, QUEUED, priority=task.priority.value, device=lane.serial)

        with self._cond:
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
            lane, task = self._locate(task_id)
            if task is None or task.is_terminal:
                return False

            if (
                lane is not None
                and lane.executing
                and lane.running is not None
                and lane.running.id == task_id
            ):
                lane.session.request_preempt(task_id)
                return True

            if lane is not None:
                lane.drop(task_id)
            self._paused[task_id] = task
            task.mark(TaskStatus.PAUSED, paused_reason=PAUSED_BY_USER)
        self._persist(task)
        self._emit(task_id, SUSPENDED, reason=PAUSED_BY_USER)
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
            lane = self._lane_for(task)
            lane.push_ready(task, self._counter)
            self._cond.notify_all()
        self._persist(task)
        self._emit(task_id, RESUMED)
        return True

    def cancel(self, task_id: str) -> bool:
        """取消任务。运行中的只是打标记，由 runtime 在下一个安全点退出——
        强杀线程不可能安全地「在动作执行到一半」停下。"""
        with self._cond:
            lane, task = self._locate(task_id)
            if task is None or task.is_terminal:
                return False

            if (
                lane is not None
                and lane.executing
                and lane.running is not None
                and lane.running.id == task_id
            ):
                task.mark(TaskStatus.CANCELLED)
                self._persist(task)
                return True

            if lane is not None:
                lane.drop(task_id)
            self._paused.pop(task_id, None)
            task.mark(TaskStatus.CANCELLED)
        self._persist(task)
        return True

    def preempt(self, by_task_id: str) -> bool:
        """让当前任务让位给指定任务。"""
        return self._maybe_preempt_by_id(by_task_id)

    def preempt_running(self, task_id: str | None = None) -> bool:
        """让正在运行的任务在下一个安全点让位。

        用于 SUPER_TASK：任务目标已被改写，需要让出设备、从新目标重新规划（V2.1 §七）。
        与 `preempt(by_task_id)` 不同——这里没有「优先级比较」，被抢占的就是 running 自身。

        多设备下**必须**指明是哪条任务（`task_id`）：不指定就退化为「所有车道都让出」，
        那会把无关设备上毫不相干的任务也一起打断（V2.2 §二）。
        不传参仍然可用（兼容旧调用方），但会打 warning——它几乎总是调用方写错了。
        """
        if task_id is None:
            logger.warning(
                "preempt_running() 未指定 task_id，将让所有设备上的在跑任务一起让出；"
                "多设备场景下请传入具体任务 id"
            )
        with self._cond:
            targets: list[tuple[_DeviceLane, Task]] = []
            if task_id is not None:
                lane, task = self._locate(task_id)
                if lane is not None and lane.running is not None and lane.running.id == task_id:
                    targets.append((lane, lane.running))
            else:
                targets = [
                    (lane, lane.running)
                    for lane in self._lanes.values()
                    if lane.running is not None
                ]

        granted = False
        for lane, running in targets:
            if not running.interruptible:
                continue
            if lane.session.request_preempt(running.id):
                granted = True
                # 自己抢占自己（SUPER_TASK 改写目标）也要计入延迟观测，
                # 否则「目标被改写 → 多久真正让出」这段耗时是盲区（V2.1 §十四）
                self._preempt_request_at[running.id] = time.monotonic()
                self._emit(
                    running.id,
                    PREEMPT_REQUESTED,
                    by_task_id=running.id,
                    by_priority=running.priority.value,
                    running_priority=running.priority.value,
                    device=lane.serial,
                    reason="self_preempt",
                )
        return granted

    # ---- 查询 ----

    def snapshot(self) -> dict[str, Any]:
        with self._cond:
            lanes = [self._lanes[serial] for serial in sorted(self._lanes)]
            running = next((lane.running for lane in lanes if lane.running is not None), None)
            return {
                # 顶层 running / device 是历史契约，多设备下只反映第一台——
                # 真正可信的是下面的 devices（V2.2 §十）
                "running": running.id if running else None,
                "running_instruction": running.instruction if running else None,
                "running_tasks": {
                    lane.serial: lane.running.id for lane in lanes if lane.running is not None
                },
                "ready": sorted(entry[3].id for lane in lanes for entry in lane.ready),
                "paused": sorted(self._paused),
                "suspended": [task.id for lane in lanes for task in lane.suspended],
                "completed": list(self._completed[-20:]),
                # 单设备时平铺成那台设备的状态（保持既有契约）
                "device": lanes[0].session.snapshot() if lanes else {},
                # 多设备详情
                "devices": {lane.serial: lane.snapshot() for lane in lanes},
                # 抢占延迟可观测（V2.1 §十四）：见 MAX_PREEMPTION_LATENCY_SECONDS 的说明
                "last_preemption_latency_seconds": self._last_preemption_latency,
            }

    def get(self, task_id: str) -> Task | None:
        with self._cond:
            return self._find_locked(task_id)

    # ---- 内部：定位与分派 ----

    def _find_locked(self, task_id: str) -> Task | None:
        return self._locate(task_id)[1]

    def _locate(self, task_id: str) -> tuple[_DeviceLane | None, Task | None]:
        """任务在哪条车道上。暂停区的任务不在任何车道上（lane 为 None）。"""
        for serial in sorted(self._lanes):
            lane = self._lanes[serial]
            if lane.running is not None and lane.running.id == task_id:
                return lane, lane.running
        for serial in sorted(self._lanes):
            lane = self._lanes[serial]
            for entry in lane.ready:
                if entry[3].id == task_id:
                    return lane, entry[3]
            for task in lane.suspended:
                if task.id == task_id:
                    return lane, task
        paused = self._paused.get(task_id)
        if paused is not None:
            return None, paused
        return None, None

    def _pick_lane(self) -> _DeviceLane:
        """挑一条最闲的车道：优先没在跑、队列最短的。

        这是「未绑定任务」的落点。简单策略就够——真正的负载均衡要看设备异构性，
        那是以后的事，现在先把「多设备能并行跑」这件事做对。
        """
        return min(
            (self._lanes[serial] for serial in sorted(self._lanes)),
            key=lambda lane: (lane.running is not None, len(lane.ready), lane.serial),
        )

    def _lane_for(self, task: Task) -> _DeviceLane:
        """任务该投到哪条车道；未绑定就挑一条**并绑定**（V2.1 §十三）。

        绑定发生在提交/恢复时，之后任务就固定在设备上——中途换设备会让
        页面上下文对不上，比排队等待更糟。
        """
        if task.device_serial:
            lane = self._lanes.get(task.device_serial)
            if lane is not None:
                return lane
            logger.warning(
                "任务 %s 绑定的设备 %s 不在池中，改派给可用设备", task.id, task.device_serial
            )
            task.device_serial = None

        lane = self._pick_lane()
        task.device_serial = lane.serial
        return lane

    def _maybe_preempt(self, newcomer: Task) -> bool:
        return self._maybe_preempt_by_id(newcomer.id)

    def _maybe_preempt_by_id(self, by_task_id: str) -> bool:
        with self._cond:
            lane, pending = self._locate(by_task_id)
            if pending is None or lane is None:
                # 找不到，或任务在暂停区（暂停中的任务不该参与抢占）
                return False
            running = lane.running
            if running is None or running.id == by_task_id:
                return False
            interruptible = running.interruptible
            running_priority = running.priority
            running_id = running.id

        if not interruptible:
            logger.info("任务 %s 不可打断，%s 只能排队", running_id, by_task_id)
            return False
        if priority_rank(pending.priority) <= priority_rank(running_priority):
            return False

        granted = lane.session.request_preempt(by_task_id)
        if granted:
            # 记下「请求让出」的时刻（V2.1 §十四），任务真正挂起时再算延迟
            self._preempt_request_at[running_id] = time.monotonic()
            self._emit(
                running_id,
                PREEMPT_REQUESTED,
                by_task_id=by_task_id,
                by_priority=pending.priority.value,
                running_priority=running_priority.value,
                device=lane.serial,
            )
        return granted

    def _persist(self, task: Task) -> None:
        if self._task_store is not None:
            try:
                self._task_store.save(task)
            except Exception:  # noqa: BLE001 - 持久化失败不应中断调度
                logger.exception("保存任务 %s 失败", task.id)

    # ---- 内部：取出与执行 ----

    def _pop_next(self, lane: _DeviceLane) -> Task | None:
        """从本车道取下一个任务：**恢复优先，但不能压过抢占者**。

        如果直接「有挂起就先恢复」，被抢占的 A 会在抢占者 B 开跑之前就抢回设备，
        抢占就永远生效不了。所以这里比较两者的优先级再决定。
        """
        with self._cond:
            while not self._stopping:
                lane.suspended = [
                    t
                    for t in lane.suspended
                    if not t.is_terminal and t.status is not TaskStatus.CANCELLED
                ]

                suspended_top = lane.suspended[0] if lane.suspended else None
                ready_top_rank = -lane.ready[0][0] if lane.ready else None

                if suspended_top is not None and (
                    ready_top_rank is None
                    or priority_rank(suspended_top.priority) >= ready_top_rank
                ):
                    lane.suspended.pop(0)
                    # 取出即登记，不留「已出队但还没标记为运行中」的空窗——
                    # 那段时间里任务不属于任何容器，pause/cancel 会找不到它
                    lane.running = suspended_top
                    return suspended_top

                if lane.ready:
                    task = heapq.heappop(lane.ready)[3]
                    lane.running = task
                    return task

                self._cond.wait(timeout=self._idle_poll)
            return None

    def _lane_loop(self, lane: _DeviceLane) -> None:
        """一条车道的 worker：不断取出并执行属于本设备的任务。"""
        while True:
            task = self._pop_next(lane)
            if task is None:
                return
            try:
                self._execute(task, lane)
            except Exception:  # noqa: BLE001 - Worker 线程绝不能因单个任务而退出
                logger.exception("任务 %s 执行时发生未捕获异常", task.id)
                with self._cond:
                    task.mark(TaskStatus.FAILED)
                    self._completed.append(task.id)
                    if lane.running is not None and lane.running.id == task.id:
                        lane.running = None
                self._persist(task)

    def _execute(self, task: Task, lane: _DeviceLane) -> None:
        if task.is_terminal:
            # 出队到执行之间任务可能已被取消；_pop_next 已把 running 指向它，这里要还回去
            with self._cond:
                if lane.running is not None and lane.running.id == task.id:
                    lane.running = None
                self._cond.notify_all()
            return

        # running 已由 _pop_next 在出队时设置（消除空窗），这里只是重申一次语义
        with self._cond:
            lane.running = task

        acquired = False
        try:
            # 设备可能被单步调试端点（/tap、/actions）临时占着，拿不到就稍后重试
            if not lane.session.acquire(task.id):
                logger.info("设备 %s 忙，任务 %s 稍后重试", lane.serial, task.id)
                with self._cond:
                    lane.push_ready(task, self._counter)
                    self._cond.wait(timeout=self._idle_poll)
                return

            acquired = True
            if task.status is not TaskStatus.RUNNING:
                task.mark(TaskStatus.RUNNING)
                self._persist(task)
            logger.info("开始执行任务 %s（设备 %s）：%s", task.id, lane.serial, task.instruction)

            with self._cond:
                lane.executing = True
            try:
                outcome = self._runtime.run(task)
            finally:
                with self._cond:
                    lane.executing = False
            self._handle_outcome(task, outcome, lane)
        finally:
            with self._cond:
                if lane.running is not None and lane.running.id == task.id:
                    lane.running = None
            if acquired:
                lane.session.release(task.id)

    def _handle_outcome(self, task: Task, outcome: Any, lane: _DeviceLane) -> None:
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
                lane.suspended.append(task)
            self._record_preemption_latency(task.id)
            logger.info("任务 %s 已挂起（让出设备 %s），等待恢复", task.id, lane.serial)
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

    def _record_preemption_latency(self, task_id: str) -> None:
        """结算一次抢占的真实延迟：从「请求让出」到「真的挂起」（V2.1 §十四）。"""
        requested_at = self._preempt_request_at.pop(task_id, None)
        if requested_at is None:
            return  # 这次挂起不是抢占引起的（用户暂停），不计入抢占延迟
        latency = time.monotonic() - requested_at
        self._last_preemption_latency = round(latency, 3)
        if latency > MAX_PREEMPTION_LATENCY_SECONDS:
            logger.warning(
                "任务 %s 抢占延迟 %.2fs，超过阈值 %.1fs——"
                "通常是单条 ADB 命令耗时过长，高优任务被堵在命令返回上",
                task_id,
                latency,
                MAX_PREEMPTION_LATENCY_SECONDS,
            )
