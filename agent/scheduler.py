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
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

from device.pool import DevicePool
from device.session import DeviceSession
from models.exceptions import DeviceUnavailableError, PersistenceError
from models.execution_mode import ExecutionMode
from models.task import (
    PAUSED_BY_PREEMPTION,
    PAUSED_BY_USER,
    Task,
    TaskEvent,
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


# ---- 执行平面（V5 §九，审查 P0① 修正）----
#
# 抢占是**同一个平面内**的仲裁：两个任务都想在 Display 0 上点，才需要分先后。
# 影子任务跑在另一块屏幕上，与 Display 0 的用户/任务没有资源冲突——让它去抢占
# 前台任务（或被前台任务抢占）是**把两个不同平面的任务当成在争同一块屏幕**。
#
# ━━━ 这里曾经有一个真实的并发缺陷（审查 §三）━━━
#
# 旧实现按 `task.execution_mode` 判定，把 `SHADOW` 与 `HYBRID` 一起当成影子。
# 但 `HYBRID` 在影子**不可用**时会回落前台（`resolve_task_plane`）：
#
#     A = HYBRID，影子不可用 → 实际 FOREGROUND（打 Display 0）
#     B = FOREGROUND                        → 打 Display 0
#
# 旧代码认为「A 是影子 → 不抢占」，于是两个任务同时往 Display 0 发动作。
# 判定必须依据**实际平面**，不是任务声明。所以现在一律走
# `_occupies_user_display()`，它内部用与 Runtime 同一个解析入口。
SHADOW_MODES = {ExecutionMode.SHADOW.value, ExecutionMode.HYBRID.value}


def _occupies_user_display(task: Task, shadow_available: bool | None = None) -> bool:
    """这个任务**实际**会不会占用用户那块屏（Display 0）。

    影子平面不可用时 `SHADOW`/`HYBRID` 都会回落到 Display 0，因此这里不能用
    `task.execution_mode` 直接判断——那正是审查 §三 指出的缺陷。

    ━━━ `shadow_available` 为什么默认是 `None` ━━━

    `None` = 「不知道本端有没有影子平面」，`resolve_task_plane` 会把它按
    **不可用**处置，于是 `HYBRID` 保守地算作**占用 Display 0**。这个方向是对的：

    - 说「占用」而其实在影子 → 多一次不必要的抢占（任务慢一点，可接受）
    - 说「不占用」而其实在前台 → **两个任务同时点同一块屏**（不可接受）

    调用方（`TaskScheduler._occupies_user_display`）会传一个零 I/O 的缓存快照；
    本函数本身**不碰设备、不建连接**，因为它是在设备锁内被调用的
    （`_maybe_preempt_by_id` 持有 `self._cond`），引入 I/O 会把整个队列堵住。
    """
    from device.session import resolve_task_plane

    return resolve_task_plane(
        task.execution_mode, shadow_available=shadow_available, task_id=task.id
    ).occupies_user_display


def _is_shadow(task: Task) -> bool:
    """任务是否**声明**了影子平面（仅用于诊断，**不用于**抢占判定）。

    读 `task.execution_mode.value` 而不是直接比枚举：`Task.execution_mode` 是
    **宽松**字段（`LooseExecutionMode`，老记录 / 未知值会降级成 FOREGROUND），
    这里保持与它一致的读法，避免两处判定口径不一致。

    ⚠️ 回答的是「用户要求什么」，不是「实际在哪跑」。抢占一律用
    `_occupies_user_display()`——用本方法判抢占就是审查 §三 那个并发缺陷。
    """
    return task.execution_mode.value in SHADOW_MODES


class RuntimeLike(Protocol):
    """Scheduler 只依赖 runtime 的这一个方法，方便测试替换。"""

    def run(self, task: Task) -> Any: ...


class DeviceNotAllowedError(RuntimeError):
    """调用方被允许使用的设备里，没有一台可用（或全部不在池中）。

    这是**对象级设备权限的最后一道防线**（V2.2 §二）：API 层已经检查过
    「指定的 serial 在不在允许列表里」，但如果调用方根本不指定 serial，
    光在 API 层检查 `None` 是没用的——调度器会自动挑一台最闲的设备，
    于是「未指定」就成了绕过设备限制的后门。所以强制点必须落在选设备这一处。
    """

    def __init__(self, allowed: Any, known: list[str]) -> None:
        allowed_text = ", ".join(sorted(allowed)) if allowed else "（无）"
        known_text = ", ".join(known) if known else "（空）"
        super().__init__(
            f"没有可用的授权设备：允许 {allowed_text}；池中设备 {known_text}"
        )


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
        lease_store: Any | None = None,
        shadow_available: Any | None = None,
    ) -> None:
        self._runtime = runtime
        # 向后兼容：老调用方传的是单个 DeviceSession（单设备场景）
        self._pool = session if isinstance(session, DevicePool) else DevicePool([session])
        if len(self._pool) == 0:
            raise ValueError("至少要注册一台设备，否则任务永远拿不到设备")
        self._task_store = task_store
        self._idle_poll = idle_poll_seconds
        self._event_log = event_log
        # V3 M3：跨进程任务租约。None = 关闭（单进程部署，保持旧行为，向后兼容）。
        # 多进程/多实例部署时传入 LeaseStore，`_execute` / `recover` 会在真正执行前
        # 先 claim，抢不到就不执行——「一个任务同一时刻最多一个执行者」从「单进程内
        # 靠 lane.running」升级为「跨进程靠 SQLite 租约」。
        self._lease_store = lease_store
        # V5 §九（审查 P0①）：影子平面可用性的**零 I/O 快照来源**。
        #
        # 抢占判定在 `self._cond` 内执行，绝不能在里面建连接 / 探测设备。
        # 所以这里收一个「无参调用、返回 bool | None」的 provider（或直接给
        # `None`/`True`/`False`），由调用方（`api/server.py`）在装配时注入一个
        # **读缓存**的实现；未接线时取「未知」，`resolve_task_plane` 会把未知
        # 按「不可用」保守处置（HYBRID 算作占用 Display 0 → 正常参与抢占）。
        #
        # 这个方向是刻意的：说「不占用」而其实在前台 → 两个任务同时点同一块屏，
        # 后果比「多一次不必要的抢占」严重得多。
        self._shadow_available = shadow_available

        self._lanes: dict[str, _DeviceLane] = {
            device.serial: _DeviceLane(serial=device.serial, session=device)
            for device in self._pool
        }

        # 这几个是**全局**的：暂停区不属于任何设备（任务还可能被改派），
        # 完成记录只是审计，用一把 Condition 统管也省得死锁
        self._cond = threading.Condition(threading.RLock())
        self._paused: dict[str, Task] = {}
        # V2.3：绑定设备不可用、等待原设备恢复的任务，不进入任何车道。
        self._device_unavailable: dict[str, Task] = {}
        # V2.5 §十：完成记录必须是**去重 + 限长**的容器。同一条任务会从多条路径走到
        # 「我结束了」（worker 清理 / 失败分流 / 持久化降级 / 恢复落定），用 list 会
        # 出现 [A, A, A] 这种假数据，而且会无限增长。deque 限长 + `_mark_completed`
        # 去重，两者都要。
        self._completed: deque[str] = deque(maxlen=200)
        # V4 §8：终态任务的**对象**留档（等 `wait_terminal` 返回它）。deque 只存 id、
        # 会丢对象；这里存一份终态对象，同样限长避免无限增长。
        self._terminal: dict[str, Task] = {}
        self._counter = itertools.count()
        self._stopping = False

        # 抢占延迟观测（V2.1 §十四）。key 是 task_id（全局唯一），
        # 所以不必下沉到车道；结果也保留"最近一次"给 /scheduler 看
        self._preempt_request_at: dict[str, float] = {}
        self._last_preemption_latency: float | None = None

        # V2.5 §八：订阅设备池的「设备可用」事件——设备掉线不再静默改派之后，
        # 这是动态设备池下等待中的任务唯一的自动恢复触发点。
        self._pool.subscribe(self.on_device_available)

    def _shadow_plane_available(self) -> bool | None:
        """影子平面可用吗（零 I/O，供锁内调用）。

        三态：`True` / `False` / `None`（不知道）。provider 抛异常时按「不知道」
        处理——抢占判定不能因为一个可选能力的探测失败而崩掉，而「不知道」在
        `resolve_task_plane` 那里已经等价于保守的「不可用」。
        """
        source = self._shadow_available
        if source is None:
            return None
        if callable(source):
            try:
                value = source()
            except Exception as exc:  # noqa: BLE001 —— 可选能力，探测失败不该影响调度
                logger.debug("读取影子平面可用性失败（按未知处理）：%s", exc)
                return None
            return value if isinstance(value, bool) else None
        return source if isinstance(source, bool) else None

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

    def tracked_tasks(self) -> list[Task]:
        """当前进程内存里跟踪的全部任务（V2.5 §五）。

        这是「live 状态」的唯一出口：就绪队列 / 挂起区 / 正在跑 / 暂停区 / 等待设备
        五处容器里还没被丢弃的任务都在这儿。`TaskManager.list_all()` 需要它——否则
        列表只能读磁盘，和 `GET /tasks/{id}`（优先内存）的答案会打架。
        """
        with self._cond:
            seen: dict[str, Task] = {}
            for lane in self._lanes.values():
                for entry in lane.ready:
                    seen[entry[3].id] = entry[3]
                for task in lane.suspended:
                    seen[task.id] = task
                if lane.running is not None:
                    seen[lane.running.id] = lane.running
            for task in self._paused.values():
                seen[task.id] = task
            for task in self._device_unavailable.values():
                seen[task.id] = task
            return list(seen.values())

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

        restored = {
            "queued": 0,
            "resuming": 0,
            "paused": 0,
            "device_unavailable": 0,
            # V2.5 §七：重启前收到的取消请求要在恢复时直接落定，不能回到队列
            "cancelled": 0,
        }
        with self._cond:
            for task in self._task_store.list_active():
                if self._find_locked(task.id) is not None:
                    continue

                restored_as = ""
                try:
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
                    elif task.status is TaskStatus.CANCEL_REQUESTED:
                        # V2.5 §七：重启前收到的取消请求——现在没有 runtime 在跑，直接落定。
                        # 绝不能把一条「已请求取消」的任务重新投递出去执行。
                        task.apply_event(TaskEvent.CANCELLED, source="scheduler")
                        self._mark_completed(task.id)
                        restored["cancelled"] += 1
                        restored_as = "cancelled"
                    elif task.status is TaskStatus.RUNNING:
                        # V2.6 §七：进程被杀时任务停在 RUNNING——上一个动作到底发出去
                        # 没有是未知的。标上 recovery_required，让 runtime 在真正跑之前
                        # 先处理（有恢复点就对账，没有就转人工），而不是当普通任务重跑。
                        #
                        # V3 M3：跨进程部署下，RUNNING 可能不是「本进程留下的」，而是
                        # **另一个进程正在跑**（它还没落盘成终态）。此时绝不能再抢来恢复
                        # ——两个进程同时执行同一个任务就是 v2.9 的 P0。先 claim，抢不到
                        # 说明别的进程活着，本进程**跳过这条**，由持有者完成或租约过期后
                        # 下一轮恢复再接管。
                        if self._lease_store is not None and self._lease_store.claim(task.id) is None:
                            logger.info(
                                "任务 %s 的租约被其他进程持有，恢复时跳过", task.id
                            )
                            restored["skipped_leased"] = restored.get("skipped_leased", 0) + 1
                            restored_as = "skipped(leased by other worker)"
                        else:
                            task.recovery_required = True
                            task.apply_event(TaskEvent.RESUMED, source="scheduler")
                            lane = self._lane_for(task)
                            lane.push_ready(task, self._counter)
                            restored["queued"] += 1
                            restored_as = "queued(from running, recovery_required)"
                    else:
                        # created / queued / waiting
                        # waiting 说明在等人工确认。**确认上下文（危险动作待确认、人工完成
                        # 认定）不跨重启**（V2.7 P0-1）：确认是针对「当时那一屏」给的，
                        # 重启后重新决策一次比沿用旧确认更安全。这里把这件事写进恢复记录，
                        # 免得后人把「确认没了」当成 bug。
                        previous = task.status.value
                        task.apply_event(TaskEvent.RESUMED, source="scheduler")
                        lane = self._lane_for(task)
                        lane.push_ready(task, self._counter)
                        restored["queued"] += 1
                        restored_as = (
                            "queued(from waiting, confirmation_reset)"
                            if previous == TaskStatus.WAITING.value
                            else f"queued(from {previous})"
                        )
                except DeviceUnavailableError:
                    task.apply_event(TaskEvent.DEVICE_LOST, source="scheduler")
                    self._device_unavailable[task.id] = task
                    restored["device_unavailable"] += 1
                    restored_as = "device_unavailable"

                if not self._persist_or_degrade(task):
                    # 已降级并移出队列，不计入恢复成功
                    if restored_as.startswith("queued"):
                        restored["queued"] -= 1
                    elif restored_as == "resuming":
                        restored["resuming"] -= 1
                self._emit(task.id, RECOVERED, restored_as=restored_as)

            self._cond.notify_all()

        return restored

    # ---- 提交与控制 ----

    def _emit(self, task_id: str, kind: str, **data) -> None:
        if self._event_log is not None:
            self._event_log.emit(task_id, kind, **data)

    def submit(self, task: Task, *, allow_preempt: bool = True, allowed_devices: Any = None) -> Task:
        """把任务放进就绪队列；若正在跑的任务优先级更低且可打断，则请求抢占。

        `allowed_devices` 是调用方的设备授权范围（V2.2 §二）：未绑定设备的任务
        只能从这里面挑。授权不足时抛 `DeviceNotAllowedError`，**不会**留下
        半提交的任务——这个异常发生在 push_ready/落盘之前。
        """
        # 先解析车道（授权不足会在这里抛），**再**改状态与入队。
        # 反过来的话，被拒绝的任务会被改成 queued 却从没进过队列——变成僵尸。
        try:
            with self._cond:
                lane = self._lane_for(task, allowed_devices)
                task.apply_event(TaskEvent.SUBMITTED, source="scheduler")
                lane.push_ready(task, self._counter)
        except DeviceUnavailableError:
            with self._cond:
                task.apply_event(TaskEvent.DEVICE_LOST, source="scheduler")
                self._device_unavailable[task.id] = task
            self._persist_or_degrade(task)
            return task

        # **先落盘、再唤醒 worker**。反过来的话，worker 可能在任务还没持久化时
        # 就开始跑，进程恰在这一刻崩溃就会把任务整个丢掉——
        # 队列是内存的，磁盘上没记就等于没提交过。
        if not self._persist_or_degrade(task):
            return task
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
            task.apply_event(TaskEvent.PAUSED_BY_USER, source="scheduler")
        if not self._persist_or_degrade(task):
            return False
        self._emit(task_id, SUSPENDED, reason=PAUSED_BY_USER)
        return True

    def resume(self, task_id: str, *, allowed_devices: Any = None) -> bool:
        with self._cond:
            task = self._paused.pop(task_id, None)
            if task is None or task.is_terminal:
                return False
            if not task.resumable:
                self._paused[task_id] = task
                return False
            try:
                lane = self._lane_for(task, allowed_devices)
            except DeviceUnavailableError:
                self._paused[task_id] = task
                return False
            task.apply_event(TaskEvent.RESUMED, source="scheduler")
            lane.push_ready(task, self._counter)
        # V2.7 P1-6：**先落盘、再唤醒 worker**（与 submit 同一条纪律，见 submit 注释）。
        # 原来 notify_all 在锁内、persist 在锁外——worker 可能在任务还没持久化时就被唤醒
        # 取走，进程恰在那一刻崩溃，磁盘上仍是 PAUSED，而内存队列里它已经进过 ready，
        # recover() 会重复投递。把 persist 提到 notify 之前，先让「已恢复」这个事实落盘。
        if not self._persist_or_degrade(task):
            return False
        with self._cond:
            self._cond.notify_all()
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
                # V2.5 §七：**CANCELLED ≠ 副作用已经停止**。正在跑的任务先落
                # CANCEL_REQUESTED（请求已收到、设备侧可能还在动），由 Runtime 在下一个
                # 安全点退出、经 `_handle_outcome` 落成 CANCELLED。否则「点击发送 →
                # 立刻取消」会显示 cancelled，而消息其实已经发出去了。
                task.apply_event(TaskEvent.CANCEL_REQUESTED, source="scheduler")
                if not self._persist_or_degrade(task):
                    return False
                logger.info("任务 %s 已记录取消请求，等待安全点退出", task_id)
                return True

            if lane is not None:
                lane.drop(task_id)
            self._paused.pop(task_id, None)
            self._device_unavailable.pop(task_id, None)
            # 没在跑的任务：设备侧本来就没有动作在途，直接落定
            task.apply_event(TaskEvent.CANCELLED, source="scheduler")
        if not self._persist_or_degrade(task):
            return False
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
                "device_unavailable": sorted(self._device_unavailable),
                "completed": list(self._completed)[-20:],
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

    def wait_terminal(self, task_id: str, timeout: float) -> Task | None:
        """**事件驱动地**等一个任务到终态（V4 §8）。

        取代 `time.sleep` 轮询：`/wait` 以前每 100ms 起来查一次，绝大多数查询都空手而归。
        现在用 `_cond.wait` 阻塞——任务到终态（`_mark_completed` 之后）worker 会
        `notify_all`，等待者被唤醒时**先查一遍**，还没到就继续等，直到超时。

        返回终态任务；超时返回 None（调用方据此回 504）。

        **为什么还是要查而不是只靠通知**（与 `freshest` 的分工）：
        - `_cond` 是**进程内**的，只能收到本进程 worker 的通知；
        - 多进程部署（TaskLease）下任务可能被**别的进程**推进，本进程收不到通知，
          所以 `wait` 里带着一个「兜底重查」——超时参数同时是「多久回查一次磁盘」
          的节拍。进程内用事件、跨进程用 `freshest`，两条腿都在（V3.3 §四）。
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                task = self._find_locked(task_id)
                if task is not None and task.is_terminal:
                    return task
                # 终态任务已从 lane 移出（_execute 的 finally 清 running），
                # 但要等它终态的等待者得能拿到「那个已经结束的对象」——查终态留档
                finished = self._terminal.get(task_id)
                if finished is not None and finished.is_terminal:
                    return finished
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                # 兜底重查间隔取 `remaining` 与一个上界的小者：进程内通知会提前打断
                # 这次 wait；跨进程（收不到通知）时最多每 0.5s 起来重查一次磁盘。
                self._cond.wait(timeout=min(remaining, 0.5))

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
        unavailable = self._device_unavailable.get(task_id)
        if unavailable is not None:
            return None, unavailable
        return None, None

    def _pick_lane(self, allowed_devices: Any = None) -> _DeviceLane:
        """挑一条**被授权**的车道：优先没在跑、队列最短的。

        这是「未绑定任务」的落点，也是对象级设备权限真正生效的地方（V2.2 §二）：
        调用方不指定设备时，只能从它被允许的那些设备里挑，绝不能挑到池里最闲的那台。

        `allowed_devices` 为 None 表示不限（单用户本地场景）。
        """
        candidates = [
            self._lanes[serial]
            for serial in sorted(self._lanes)
            if allowed_devices is None or serial in allowed_devices
        ]
        if not candidates:
            raise DeviceNotAllowedError(allowed_devices, sorted(self._lanes))
        return min(
            candidates,
            key=lambda lane: (lane.running is not None, len(lane.ready), lane.serial),
        )

    def pick_device(self, allowed_devices: Any = None) -> str | None:
        """按授权范围挑一台设备，只回答 serial（不绑定、不排队）。

        给上层「先决定这台任务该跑在哪，再去做权限判断」用的。
        """
        with self._cond:
            if len(self._lanes) == 0:
                return None
            return self._pick_lane(allowed_devices).serial

    def _lane_for(self, task: Task, allowed_devices: Any = None) -> _DeviceLane:
        """任务该投到哪条车道；未绑定就挑一条**被授权的**并绑定（V2.1 §十三）。

        绑定发生在提交/恢复时，之后任务就固定在设备上——中途换设备会让
        页面上下文对不上，比排队等待更糟。

        V2.3：绑定设备不在池中时不静默改派，而是抛 DeviceUnavailableError，
        由调用方把任务置为 DEVICE_UNAVAILABLE 等待原设备恢复。
        """
        if task.device_serial:
            lane = self._lanes.get(task.device_serial)
            if lane is not None:
                if allowed_devices is not None and lane.serial not in allowed_devices:
                    raise DeviceNotAllowedError(allowed_devices, sorted(self._lanes))
                return lane
            raise DeviceUnavailableError(task.id, task.device_serial)

        lane = self._pick_lane(allowed_devices)
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
            # V5 §九（审查 P0① 修正）：跨平面的抢占不成立——但这里的「平面」必须是
            # **实际**平面。`HYBRID` 在影子不可用时会回落前台，此时它与前台任务
            # 争的正是同一块 Display 0，**必须**照常抢占。
            # 旧实现按 `task.execution_mode` 判断，把回落的 HYBRID 误当成影子，
            # 于是两个任务会同时往 Display 0 发动作。
            shadow_ok = self._shadow_plane_available()
            if not _occupies_user_display(pending, shadow_ok) or not _occupies_user_display(
                running, shadow_ok
            ):
                logger.debug(
                    "任务 %s 与 %s 不在同一执行平面，不触发抢占",
                    by_task_id,
                    running.id,
                )
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
        """关键持久化：失败时抛 PersistenceError，由调用方决定如何降级。"""
        if self._task_store is None:
            return
        try:
            # 与 runtime._persist 同一条约定（V2.6 §八）：**刻意不带** `expected_revision`。
            # 单进程内三个模块共享内存 Task 实例，revision 随任一写者推进，磁盘与内存一致，
            # 这里加 CAS 只会把「内存比磁盘新」的正常情形误判成冲突。
            # 跨进程保护要靠文件锁 / 数据库事务，届时统一收口，而不是逐点补参数。
            self._task_store.save(task)
        except Exception as exc:  # noqa: BLE001 - 转换后重新抛出
            logger.exception("保存任务 %s 失败", task.id)
            raise PersistenceError(task.id, str(exc)) from exc

    def _persist_or_degrade(self, task: Task) -> bool:
        """尽力持久化；失败时将任务降级并移出队列。

        返回 True 表示持久化成功，False 表示已降级。
        """
        try:
            self._persist(task)
            return True
        except PersistenceError:
            logger.error("任务 %s 持久化失败，从调度队列降级", task.id)
            task.apply_event(TaskEvent.DEGRADED, source="scheduler")
            try:
                self._persist(task)
            except PersistenceError:
                logger.exception("任务 %s 降级状态也无法持久化", task.id)
            with self._cond:
                self._drop_from_queues(task.id)
                self._mark_completed(task.id)
            return False

    def _drop_from_queues(self, task_id: str) -> None:
        """把任务从所有车道的就绪队列、挂起区、暂停区和等待设备区移除。

        **但不释放正在执行的车道**（V2.6 §四）：cancel、持久化降级都可能发生在
        Runtime 仍在跑的时候。`lane.running` 一旦被清掉，调度器就以为设备空了，
        而真实世界里那条任务还在操作手机——`running_tasks()`、抢占判断、设备所有权
        认知会同时失真。运行槽只由 worker 自己（`_execute` 的 finally）释放。
        """
        for lane in self._lanes.values():
            lane.drop(task_id)
            if (
                lane.running is not None
                and lane.running.id == task_id
                and not lane.executing
            ):
                lane.running = None
        self._paused.pop(task_id, None)
        # V2.5 §九：等待设备的容器也要清——否则 cancel 之后它还攥着一个 CANCELLED
        # 的旧对象，将来设备恢复时会被当成「待恢复任务」捞出来，容器本身就脏了。
        self._device_unavailable.pop(task_id, None)

    def _mark_completed(self, task_id: str, task: Task | None = None) -> None:
        """把任务记进「最近结束」列表（去重，V2.5 §十）。调用方需持有 `self._cond`。

        `task` 可选：给了就把**终态对象**也留一份（`_terminal`，V4 §8）。终态任务会从
        lane 里移出（`_execute` 的 finally 清 `lane.running`），所以「等它终态」的
        等待者不能再靠 `_find_locked` 找到它；留下终态对象，`wait_terminal` 才能返回
        「那个已经结束的任务」而不是返回 None 让调用方再去查磁盘。
        """
        if task_id not in self._completed:
            self._completed.append(task_id)
        if task is not None:
            self._terminal[task_id] = task
            # 与 `_completed` 的 deque 限长对齐：终态对象只留最近 200 条，
            # 更早的被等待者早该走了（它们要么已返回、要么已超时）。
            if len(self._terminal) > 200:
                oldest = next(iter(self._terminal))
                del self._terminal[oldest]

    def on_device_available(self, serial: str) -> list[str]:
        """设备重新可用：把它名下等待的任务放回对应车道（V2.5 §八）。

        这是 `DEVICE_UNAVAILABLE` 的**唯一正规恢复入口**，由 `DevicePool` 的注册事件
        驱动（`__init__` 里已订阅）。刻意不做成「重启时顺便扫一遍」——设备池是动态的，
        那样会让任务永久卡在等待态。

        只恢复 `device_serial == serial` 且仍非终态的任务；终态的一律顺手清出容器
        （它不该继续占着「等待设备」这个位置）。返回本次恢复的任务 id 列表。
        """
        recovered: list[Task] = []
        with self._cond:
            lane = self._ensure_lane(serial)
            if lane is None:
                return []  # 池里没有这台设备：交给调用方，调度器不猜
            for task_id, task in list(self._device_unavailable.items()):
                if task.device_serial != serial:
                    continue
                if task.is_terminal:
                    # 容器要跟着任务状态收敛，别留脏数据
                    self._device_unavailable.pop(task_id, None)
                    continue
                self._device_unavailable.pop(task_id, None)
                task.apply_event(TaskEvent.RESUMED, source="scheduler")
                lane.push_ready(task, self._counter)
                recovered.append(task)
        # V2.7 P1-6：**先落盘、再唤醒 worker**（与 submit / resume 同一条纪律）。
        for task in recovered:
            self._persist_or_degrade(task)
            logger.info("设备 %s 恢复，任务 %s 重新入队", serial, task.id)
        if recovered:
            with self._cond:
                self._cond.notify_all()
        return [task.id for task in recovered]

    def _ensure_lane(self, serial: str) -> _DeviceLane | None:
        """确保这台设备有车道，没有就补建（V2.5 §八）。

        车道原本在构造时按池一次性建好，动态设备池下这就成了「设备回来了、却没有
        车道」，任务永远回不去队列。补建时如果其他车道已在跑（调度器已 start），
        新车道也要起 worker——否则任务入了队也没人取。返回 None 表示池里没有这台设备。
        """
        lane = self._lanes.get(serial)
        if lane is not None:
            return lane
        device = self._pool.get(serial)
        if device is None:
            return None
        lane = _DeviceLane(serial=serial, session=device)
        self._lanes[serial] = lane
        others_running = any(
            other.worker is not None and other.worker.is_alive()
            for other in self._lanes.values()
        )
        if others_running and not self._stopping:
            lane.worker = threading.Thread(
                target=self._lane_loop,
                args=(lane,),
                name=f"shadow-device-{serial}",
                daemon=True,
            )
            lane.worker.start()
        logger.info("设备 %s 上线，已补建调度车道", serial)
        return lane

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
                    # V2.8 §四：与 suspended 分支对齐——取出时若已终态 / 已取消，跳过并
                    # 继续取下一个。持久化降级可能在「入队之后、worker 取出之前」把任务
                    # 从队列清掉，但 ready 堆是惰性清理的，这里兜底防「取到一个 DEGRADED
                    # / CANCELLED 的任务去执行」。
                    if task.is_terminal or task.status is TaskStatus.CANCELLED:
                        continue
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
            except PersistenceError as exc:
                # V2.4 §七：持久化失败 ≠ 任务失败。前者必须停在 DEGRADED——
                # 内存状态已经领先 durable state，继续跑会在崩溃后重复副作用。
                logger.error("任务 %s 因关键持久化失败降级：%s", task.id, exc)
                self._reap_worker_failure(task, lane, TaskEvent.DEGRADED)
            except DeviceUnavailableError as exc:
                # 设备掉线同样不是任务失败：原设备回来还能接着做。
                logger.warning("任务 %s 绑定的设备不可用，转入等待：%s", task.id, exc)
                with self._cond:
                    self._device_unavailable[task.id] = task
                self._reap_worker_failure(task, lane, TaskEvent.DEVICE_LOST)
            except Exception:  # noqa: BLE001 - Worker 线程绝不能因单个任务而退出
                logger.exception("任务 %s 执行时发生未捕获异常（ERROR_CLASS=INTERNAL）", task.id)
                self._reap_worker_failure(task, lane, TaskEvent.FAILED)

    def _reap_worker_failure(self, task: Task, lane: _DeviceLane, event: TaskEvent) -> None:
        """Worker 兜底：按异常性质落状态、释放车道占用、尽力持久化（V2.4 §七）。

        原来的兜底是 `except Exception: mark(FAILED)` —— 又放了一个垃圾桶，
        把 PersistenceError / DeviceUnavailableError 一律压成「任务失败」。
        分类之后，「这条任务为什么停下来」不用再从日志里猜：
        DEGRADED = 状态落不了盘、别再产生副作用；DEVICE_UNAVAILABLE = 等设备回来；
        只有真正的未知异常才是 FAILED。
        """
        with self._cond:
            task.apply_event(event, source="scheduler")
            # 只有终态才算「这条任务结束了」；DEVICE_UNAVAILABLE 还会回来，
            # 记进完成列表会让 snapshot 误报。
            if task.is_terminal:
                self._mark_completed(task.id)
            if lane.running is not None and lane.running.id == task.id:
                lane.running = None
        self._persist_or_degrade(task)

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
        lease_token: str | None = None
        try:
            # V3 M3：跨进程租约。执行前先 claim——抢不到说明另一个进程正在跑这个
            # 任务（或它的租约还没过期），把它放回队列等重试，而不是硬抢。
            # 这一层补齐了「单进程内靠 lane.running 唯一」之外、跨进程的缺口。
            if self._lease_store is not None:
                lease = self._lease_store.claim(task.id)
                if lease is None:
                    logger.info(
                        "任务 %s 的租约被其他进程持有，暂不执行（设备 %s）",
                        task.id,
                        lane.serial,
                    )
                    with self._cond:
                        lane.push_ready(task, self._counter)
                        self._cond.wait(timeout=self._idle_poll)
                    return
                lease_token = lease.token

            # 设备可能被单步调试端点（/tap、/actions）临时占着，拿不到就稍后重试
            if not lane.session.acquire(task.id):
                logger.info("设备 %s 忙，任务 %s 稍后重试", lane.serial, task.id)
                with self._cond:
                    lane.push_ready(task, self._counter)
                    self._cond.wait(timeout=self._idle_poll)
                return

            acquired = True
            if task.status is not TaskStatus.RUNNING:
                task.apply_event(TaskEvent.DISPATCHED, source="scheduler")
                if not self._persist_or_degrade(task):
                    return
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
            if lease_token is not None and self._lease_store is not None:
                self._lease_store.release(task.id, lease_token)

    def _handle_outcome(self, task: Task, outcome: Any, lane: _DeviceLane) -> None:
        """把 runtime 的结果映射回任务状态。

        SUSPENDED 的任务进 `_suspended` 而不是就绪队列：被抢占的任务要**优先恢复**，
        否则不断到来的新任务会让它永远等不到设备。
        """
        name = getattr(outcome, "value", str(outcome))

        if task.is_terminal:
            # V2.5 §六：任务在执行期间被（并发）置成了终态。终态不可逆，这里**不再改写它**
            # —— 硬 mark 会抛 InvalidTransitionError，把一次普通收尾变成本不该有的 worker
            # 异常。只补持久化与完成记录，让容器收敛。
            logger.warning(
                "任务 %s 已是终态（%s），runtime 结论 %s 不再改写状态",
                task.id,
                task.status.value,
                name,
            )
            # 先落盘、再通知（与下面终态分支同一条纪律，V4 §8）
            self._persist_or_degrade(task)
            with self._cond:
                self._mark_completed(task.id, task)
                self._cond.notify_all()
            return

        if name == "done":
            task.apply_event(TaskEvent.COMPLETED, source="scheduler")
        elif name == "cancelled":
            task.apply_event(TaskEvent.CANCELLED, source="scheduler")
        elif name == "suspended":
            task.apply_event(TaskEvent.PAUSED_BY_PREEMPTION, source="scheduler")
            with self._cond:
                lane.suspended.append(task)
            self._record_preemption_latency(task.id)
            logger.info("任务 %s 已挂起（让出设备 %s），等待恢复", task.id, lane.serial)
        elif name == "suspended_by_user":
            # V5 §十四：**因为用户正在用手机**而主动让开。
            #
            # 与上面的抢占挂起刻意分开处理：这里的「让位」不是设备争用的结果，
            # 而是人的存在本身。所以：
            #   - **不进 `_suspended` 队列**：那条队列的恢复由「设备空出来」驱动，
            #     而这里要等的是「用户停手 + 页面还是那一屏」。放进去会让调度器
            #     一有空设备就立刻把任务捞起来重跑，下一轮又撞上用户 → 空转。
            #     任务状态已经是 PAUSED(user)，由 `resume` / `/tasks/{id}/resume`
            #     显式拉起，或者由调度器的空闲探测决定（见 `_user_resume_scan`）。
            #   - **不记抢占延迟**：`_record_preemption_latency` 里面靠
            #     `_preempt_request_at` 判来源，本来就不会记到这一条，此处显式说明。
            logger.info("任务 %s 因用户正在使用设备而暂停（原因=user）", task.id)
        elif name == "awaiting_confirmation":
            # 等人工确认：既不算完成也不算失败，**不能**放进任何队列——
            # 放进去会被 worker 立刻取出重跑，再次撞上同一个危险动作，变成死循环。
            task.apply_event(TaskEvent.AWAITING_CONFIRMATION, source="scheduler")
            logger.info("任务 %s 等待人工确认危险动作", task.id)
        else:
            task.apply_event(TaskEvent.FAILED, source="scheduler")

        if task.is_terminal:
            # V4 §8：**先落盘、再通知**。`/wait` 的事件驱动等待者会在被唤醒后立刻
            # 查 `freshest`（磁盘）——若落盘晚于 notify，等待者会看到「还在跑」而空转。
            # 与 submit / resume / recover 的「先落盘、再 notify_all」是同一条纪律
            # （V2.7 P1-6）。
            self._persist_or_degrade(task)
            with self._cond:
                self._mark_completed(task.id, task)
                self._cond.notify_all()

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
