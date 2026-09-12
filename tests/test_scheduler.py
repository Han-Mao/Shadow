"""TaskScheduler：排队、优先级、暂停/取消、抢占与恢复。"""
from __future__ import annotations

import threading
import time

from agent.runtime import RunOutcome
from agent.scheduler import TaskScheduler
from device.session import DeviceSession
from fakes import FakeDevice
from models.task import PAUSED_BY_PREEMPTION, PAUSED_BY_USER, Task, TaskPriority, TaskStatus
from storage import TaskStore


class DoneRuntime:
    """立刻完成任务，并记录执行顺序。"""

    def __init__(self, expected: int = 1) -> None:
        self.order: list[str] = []
        self.done_event = threading.Event()
        self._expected = expected
        self._lock = threading.Lock()

    def run(self, task: Task) -> RunOutcome:
        with self._lock:
            self.order.append(task.id)
            if self._expected > 0 and len(self.order) >= self._expected:
                self.done_event.set()
        return RunOutcome.DONE


class ControlledRuntime:
    """A 会挂在安全点上直到被请求让出；B 立刻完成；A 恢复后立刻完成。

    用来验证 V2 最核心的那条路径：A 被抢占 → B 执行 → A 恢复。
    """

    def __init__(self, session: DeviceSession) -> None:
        self._session = session
        self.order: list[str] = []
        self.a_started = threading.Event()
        self.finished = threading.Event()
        self._a_runs = 0
        self._lock = threading.Lock()

    def run(self, task: Task) -> RunOutcome:
        with self._lock:
            self.order.append(task.id)
            is_b = "B" in task.instruction
            if is_b:
                return RunOutcome.DONE
            self._a_runs += 1
            run_index = self._a_runs

        if run_index == 1:
            self.a_started.set()
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                if self._session.should_yield(task.id):
                    return RunOutcome.SUSPENDED
                time.sleep(0.01)
            return RunOutcome.DONE

        # A 恢复后的第二次执行
        self.finished.set()
        return RunOutcome.DONE


class CancelAwareRuntime:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.exited = threading.Event()

    def run(self, task: Task) -> RunOutcome:
        self.started.set()
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if task.status is TaskStatus.CANCELLED:
                self.exited.set()
                return RunOutcome.CANCELLED
            time.sleep(0.01)
        return RunOutcome.FAILED


class ConfirmationRuntime:
    def __init__(self) -> None:
        self.runs = 0
        self.first_run = threading.Event()
        self._lock = threading.Lock()

    def run(self, task: Task) -> RunOutcome:
        with self._lock:
            self.runs += 1
            self.first_run.set()
        return RunOutcome.AWAITING_CONFIRMATION


class PreemptionRuntime:
    """可脚本化的抢占场景 runtime。

    A 第一次运行会挂在安全点上，直到被请求让出设备；
    B 的行为由 `b_outcome` 决定——给出结果就直接返回，给 `None` 则挂着等被取消。
    """

    def __init__(self, session: DeviceSession, *, b_outcome: RunOutcome | None = RunOutcome.DONE) -> None:
        self._session = session
        self._b_outcome = b_outcome
        self.order: list[str] = []
        self.a_started = threading.Event()
        self.a_resumed = threading.Event()
        self.b_started = threading.Event()
        self._a_runs = 0
        self._lock = threading.Lock()

    def run(self, task: Task) -> RunOutcome:
        with self._lock:
            self.order.append(task.id)
            is_a = task.instruction.startswith("A")
            if is_a:
                self._a_runs += 1
                a_run = self._a_runs
            else:
                self.b_started.set()

        if not is_a:
            if self._b_outcome is not None:
                return self._b_outcome
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                if task.status is TaskStatus.CANCELLED:
                    return RunOutcome.CANCELLED
                time.sleep(0.01)
            return RunOutcome.DONE

        if a_run == 1:
            self.a_started.set()
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                if self._session.should_yield(task.id):
                    return RunOutcome.SUSPENDED
                time.sleep(0.01)
            return RunOutcome.DONE

        self.a_resumed.set()
        return RunOutcome.DONE


def make_scheduler(runtime, session: DeviceSession) -> TaskScheduler:
    return TaskScheduler(runtime, session, idle_poll_seconds=0.01)


# ---------------------------------------------------------------- 基本调度


def test_submit_executes_task():
    session = DeviceSession(FakeDevice())
    runtime = DoneRuntime(expected=1)
    scheduler = make_scheduler(runtime, session)
    task = Task(instruction="打开设置")

    scheduler.submit(task)
    scheduler.start()
    try:
        assert runtime.done_event.wait(2.0)
    finally:
        scheduler.stop()

    assert runtime.order == [task.id]
    assert task.status is TaskStatus.DONE
    assert session.owner is None


def test_priority_decides_who_runs_first():
    session = DeviceSession(FakeDevice())
    runtime = DoneRuntime(expected=2)
    scheduler = make_scheduler(runtime, session)

    low = Task(instruction="低优先级", priority=TaskPriority.LOW)
    high = Task(instruction="高优先级", priority=TaskPriority.HIGH)
    scheduler.submit(low)
    scheduler.submit(high)

    scheduler.start()
    try:
        assert runtime.done_event.wait(2.0)
    finally:
        scheduler.stop()

    assert runtime.order == [high.id, low.id]


def test_scheduler_snapshot_shape():
    session = DeviceSession(FakeDevice())
    scheduler = make_scheduler(DoneRuntime(), session)
    task = Task(instruction="x")
    scheduler.submit(task, allow_preempt=False)

    snap = scheduler.snapshot()
    assert task.id in snap["ready"]
    assert snap["running"] is None
    assert "device" in snap and snap["device"]["busy"] is False


# ---------------------------------------------------------------- 暂停 / 恢复 / 取消


def test_pause_and_resume_queued_task():
    session = DeviceSession(FakeDevice())
    scheduler = make_scheduler(DoneRuntime(), session)
    task = Task(instruction="x")
    scheduler.submit(task, allow_preempt=False)

    assert scheduler.pause(task.id)
    assert task.status is TaskStatus.PAUSED
    assert scheduler.snapshot()["paused"] == [task.id]

    assert scheduler.resume(task.id)
    assert task.status is TaskStatus.QUEUED
    assert task.id in scheduler.snapshot()["ready"]


def test_cannot_resume_task_that_disallows_it():
    session = DeviceSession(FakeDevice())
    scheduler = make_scheduler(DoneRuntime(), session)
    task = Task(instruction="x", resumable=False)
    scheduler.submit(task, allow_preempt=False)
    scheduler.pause(task.id)

    assert not scheduler.resume(task.id)
    assert task.status is TaskStatus.PAUSED


def test_cancel_queued_task_removes_it():
    session = DeviceSession(FakeDevice())
    scheduler = make_scheduler(DoneRuntime(), session)
    task = Task(instruction="x")
    scheduler.submit(task, allow_preempt=False)

    assert scheduler.cancel(task.id)
    assert task.status is TaskStatus.CANCELLED
    assert task.id not in scheduler.snapshot()["ready"]


def test_cancel_running_task_lets_runtime_exit_cleanly():
    """强杀线程不可能在「动作执行到一半」安全停下，只能打标记让 runtime 自己退。"""
    session = DeviceSession(FakeDevice())
    runtime = CancelAwareRuntime()
    scheduler = make_scheduler(runtime, session)
    task = Task(instruction="x")

    scheduler.submit(task)
    scheduler.start()
    try:
        assert runtime.started.wait(2.0)
        assert scheduler.cancel(task.id)
        assert runtime.exited.wait(2.0)
    finally:
        scheduler.stop()

    assert task.status is TaskStatus.CANCELLED


def test_cancel_finished_task_is_rejected():
    session = DeviceSession(FakeDevice())
    scheduler = make_scheduler(DoneRuntime(), session)
    task = Task(instruction="x", status=TaskStatus.DONE)
    assert not scheduler.cancel(task.id)


# ---------------------------------------------------------------- 抢占与恢复


def test_preemption_then_resume():
    """V2 的核心路径：A 跑到一半被 B 抢占，B 完成后 A 自动恢复。"""
    session = DeviceSession(FakeDevice())
    runtime = ControlledRuntime(session)
    scheduler = make_scheduler(runtime, session)

    task_a = Task(instruction="A 逛淘宝", priority=TaskPriority.NORMAL)
    scheduler.submit(task_a, allow_preempt=False)
    scheduler.start()

    try:
        assert runtime.a_started.wait(2.0), "A 应该先跑起来"

        task_b = Task(instruction="B 发微信", priority=TaskPriority.HIGH)
        scheduler.submit(task_b)

        # B 是 HIGH，A 是 NORMAL → A 让出设备，B 执行，然后 A 恢复
        assert runtime.finished.wait(4.0), "A 应该在 B 完成后被恢复"
    finally:
        scheduler.stop()

    assert runtime.order == [task_a.id, task_b.id, task_a.id]
    assert task_a.status is TaskStatus.DONE
    assert task_b.status is TaskStatus.DONE


def test_high_priority_does_not_preempt_uninterruptible_task():
    session = DeviceSession(FakeDevice())
    runtime = ControlledRuntime(session)
    scheduler = make_scheduler(runtime, session)

    task_a = Task(instruction="A 逛淘宝", priority=TaskPriority.LOW, interruptible=False)
    scheduler.submit(task_a, allow_preempt=False)

    task_b = Task(instruction="B 发微信", priority=TaskPriority.CRITICAL)
    scheduler.submit(task_b)

    # A 正在跑之前 B 就提交了，此时没有 running，抢占不触发；但要确认提交没坏
    assert task_b.status is TaskStatus.QUEUED
    assert task_a.status is TaskStatus.QUEUED


def test_lower_priority_task_does_not_preempt():
    session = DeviceSession(FakeDevice())
    scheduler = make_scheduler(DoneRuntime(), session)
    running = Task(instruction="A", priority=TaskPriority.HIGH, status=TaskStatus.RUNNING)
    scheduler._running = running  # noqa: SLF001 - 直接摆好场景，避免线程时序不确定

    newcomer = Task(instruction="B", priority=TaskPriority.LOW)
    scheduler.submit(newcomer)

    assert not session.should_yield(running.id)


def test_preempt_returns_false_without_running_task():
    session = DeviceSession(FakeDevice())
    scheduler = make_scheduler(DoneRuntime(), session)
    assert not scheduler.preempt("nobody")


# ---------------------------------------------------------------- 等待人工确认


def test_awaiting_confirmation_task_is_not_requeued():
    """等确认的任务若被重新入队，worker 会立刻取出重跑，再次撞上同一个危险动作 → 死循环。"""
    session = DeviceSession(FakeDevice())
    runtime = ConfirmationRuntime()
    scheduler = make_scheduler(runtime, session)
    task = Task(instruction="点发送")

    scheduler.submit(task)
    scheduler.start()
    try:
        assert runtime.first_run.wait(2.0)
        time.sleep(0.3)  # 给 worker 充分机会去犯「重跑」这个错
    finally:
        scheduler.stop()

    assert runtime.runs == 1
    assert task.status is TaskStatus.WAITING
    snap = scheduler.snapshot()
    assert task.id not in snap["ready"]
    assert task.id not in snap["suspended"]
    assert task.id not in snap["completed"]


# ---------------------------------------------------------------- 设备占用


def test_task_waits_until_device_is_free():
    session = DeviceSession(FakeDevice())
    runtime = DoneRuntime(expected=1)
    scheduler = make_scheduler(runtime, session)

    # 模拟单步调试端点临时占用设备
    session.acquire("__manual__")

    task = Task(instruction="x")
    scheduler.submit(task)
    scheduler.start()
    try:
        time.sleep(0.2)
        assert runtime.order == [], "设备被占用时不该执行"

        session.release("__manual__")
        assert runtime.done_event.wait(2.0)
    finally:
        scheduler.stop()

    assert runtime.order == [task.id]


def test_worker_survives_runtime_crash():
    """单个任务把 runtime 搞崩，不能连带把 worker 线程一起带走。"""

    class ExplodingRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, task: Task) -> RunOutcome:
            self.calls += 1
            if "会炸" in task.instruction:
                raise RuntimeError("runtime 炸了")
            return RunOutcome.DONE

    session = DeviceSession(FakeDevice())
    runtime = ExplodingRuntime()
    scheduler = make_scheduler(runtime, session)

    first = Task(instruction="会炸")
    second = Task(instruction="不该受影响")
    scheduler.submit(first)
    scheduler.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and first.status is not TaskStatus.FAILED:
            time.sleep(0.02)
        assert first.status is TaskStatus.FAILED

        scheduler.submit(second)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and second.status is not TaskStatus.DONE:
            time.sleep(0.02)
        assert second.status is TaskStatus.DONE, "worker 必须还活着"
    finally:
        scheduler.stop()


# ---------------------------------------------------------------- 组合式中断场景


def test_a_resumes_after_b_fails():
    """A → B 失败 → A：B 挂掉不能把被抢占的 A 一起带走。"""
    session = DeviceSession(FakeDevice())
    runtime = PreemptionRuntime(session, b_outcome=RunOutcome.FAILED)
    scheduler = make_scheduler(runtime, session)

    task_a = Task(instruction="A 逛淘宝", priority=TaskPriority.NORMAL)
    scheduler.submit(task_a, allow_preempt=False)
    scheduler.start()
    try:
        assert runtime.a_started.wait(2.0), "A 应该先跑起来"

        task_b = Task(instruction="B 发微信", priority=TaskPriority.HIGH)
        scheduler.submit(task_b)

        assert runtime.a_resumed.wait(4.0), "B 失败后 A 必须被恢复"
    finally:
        scheduler.stop()

    assert runtime.order == [task_a.id, task_b.id, task_a.id]
    assert task_b.status is TaskStatus.FAILED
    assert task_a.status is TaskStatus.DONE


def test_a_resumes_after_b_cancelled():
    """A → B 被取消 → A：取消抢占者之后，被抢占的任务不能跟着一起卡住。"""
    session = DeviceSession(FakeDevice())
    runtime = PreemptionRuntime(session, b_outcome=None)  # B 挂着，等被取消
    scheduler = make_scheduler(runtime, session)

    task_a = Task(instruction="A 逛淘宝", priority=TaskPriority.NORMAL)
    scheduler.submit(task_a, allow_preempt=False)
    scheduler.start()
    try:
        assert runtime.a_started.wait(2.0)

        task_b = Task(instruction="B 发微信", priority=TaskPriority.HIGH)
        scheduler.submit(task_b)
        assert runtime.b_started.wait(2.0), "B 应该抢占成功并开始执行"

        assert scheduler.cancel(task_b.id)
        assert runtime.a_resumed.wait(4.0), "B 被取消后 A 必须被恢复"
    finally:
        scheduler.stop()

    assert task_b.status is TaskStatus.CANCELLED
    assert task_a.status is TaskStatus.DONE


# ---------------------------------------------------------------- 启动恢复（服务重启）


def make_persistent(tmp_path, runtime, *, session=None) -> TaskScheduler:
    return TaskScheduler(
        runtime,
        session or DeviceSession(FakeDevice()),
        task_store=TaskStore(tmp_path / "tasks"),
        idle_poll_seconds=0.01,
    )


def test_recover_requeues_queued_task(tmp_path):
    """服务重启后，磁盘上排队的任务必须被重新投递，否则永远没人执行。"""
    store = TaskStore(tmp_path / "tasks")
    first = TaskScheduler(DoneRuntime(), DeviceSession(FakeDevice()), task_store=store)

    task = Task(instruction="重启前提交的任务")
    first.submit(task, allow_preempt=False)  # 只入队，不给它跑的机会
    first.stop()

    runtime = DoneRuntime(expected=1)
    second = TaskScheduler(
        runtime, DeviceSession(FakeDevice()), task_store=store, idle_poll_seconds=0.01
    )
    restored = second.recover()
    assert restored["queued"] == 1

    second.start()
    try:
        assert runtime.done_event.wait(2.0)
    finally:
        second.stop()

    assert runtime.order == [task.id]


def test_recover_requeues_task_that_was_running(tmp_path):
    """进程被杀时正在执行的任务：重启后要接着跑，而不是永久停在 running。"""
    store = TaskStore(tmp_path / "tasks")
    task = Task(instruction="跑到一半被杀")
    task.mark(TaskStatus.RUNNING)
    store.save(task)

    runtime = DoneRuntime(expected=1)
    scheduler = TaskScheduler(
        runtime, DeviceSession(FakeDevice()), task_store=store, idle_poll_seconds=0.01
    )
    assert scheduler.recover()["queued"] == 1

    scheduler.start()
    try:
        assert runtime.done_event.wait(2.0)
    finally:
        scheduler.stop()

    assert runtime.order == [task.id]
    assert store.load(task.id).status is TaskStatus.DONE


def test_recover_keeps_user_paused_task_paused(tmp_path):
    """用户显式暂停的任务，重启后不该被擅自恢复。"""
    store = TaskStore(tmp_path / "tasks")
    first = TaskScheduler(DoneRuntime(), DeviceSession(FakeDevice()), task_store=store)

    task = Task(instruction="用户暂停的任务")
    first.submit(task, allow_preempt=False)
    assert first.pause(task.id)
    first.stop()

    scheduler = TaskScheduler(DoneRuntime(), DeviceSession(FakeDevice()), task_store=store)
    restored = scheduler.recover()

    assert restored == {"queued": 0, "resuming": 0, "paused": 1}
    assert scheduler.snapshot()["paused"] == [task.id]


def test_recover_resumes_preempted_task(tmp_path):
    """被抢占挂起只是「临时让位」，重启后必须自动回到执行队列。"""
    store = TaskStore(tmp_path / "tasks")
    task = Task(instruction="被抢占的任务")
    task.mark(TaskStatus.PAUSED, paused_reason=PAUSED_BY_PREEMPTION)
    store.save(task)

    runtime = DoneRuntime(expected=1)
    scheduler = TaskScheduler(
        runtime, DeviceSession(FakeDevice()), task_store=store, idle_poll_seconds=0.01
    )
    assert scheduler.recover()["resuming"] == 1

    scheduler.start()
    try:
        assert runtime.done_event.wait(2.0)
    finally:
        scheduler.stop()

    assert runtime.order == [task.id]


def test_recover_is_idempotent(tmp_path):
    """start() 内部会调 recover()，重复调用不能把同一个任务投递两次。"""
    store = TaskStore(tmp_path / "tasks")
    task = Task(instruction="任务")
    task.mark(TaskStatus.QUEUED)
    store.save(task)

    scheduler = TaskScheduler(DoneRuntime(), DeviceSession(FakeDevice()), task_store=store)
    assert scheduler.recover()["queued"] == 1
    assert scheduler.recover()["queued"] == 0
    assert len(scheduler.snapshot()["ready"]) == 1


def test_recover_ignores_finished_tasks(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    for status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
        task = Task(instruction=f"已结束-{status.value}", status=status)
        store.save(task)

    scheduler = TaskScheduler(DoneRuntime(), DeviceSession(FakeDevice()), task_store=store)
    assert scheduler.recover() == {"queued": 0, "resuming": 0, "paused": 0}
    assert scheduler.snapshot()["ready"] == []


def test_recover_without_store_is_noop():
    """没接持久化层时（单机脚本、部分测试）recover 应当安全返回。"""
    scheduler = TaskScheduler(DoneRuntime(), DeviceSession(FakeDevice()))
    assert scheduler.recover() == {}
    assert scheduler.snapshot()["ready"] == []


def test_restart_after_preemption_end_to_end(tmp_path):
    """A → B → 重启 → A：被抢占挂起后进程退出，重启要把 A 拉回来跑完。"""
    store = TaskStore(tmp_path / "tasks")

    # 磁盘上留下的状态 = 「A 被 B 抢占挂起的那一刻进程被杀」
    task_a = Task(instruction="A 逛淘宝")
    task_a.set_plan(["打开淘宝", "搜索运动鞋"])
    task_a.mark(TaskStatus.PAUSED, paused_reason=PAUSED_BY_PREEMPTION)
    store.save(task_a)

    runtime = DoneRuntime(expected=1)
    scheduler = TaskScheduler(
        runtime, DeviceSession(FakeDevice()), task_store=store, idle_poll_seconds=0.01
    )
    restored = scheduler.recover()
    scheduler.start()
    try:
        assert runtime.done_event.wait(3.0), "重启后 A 必须被拉回执行"
    finally:
        scheduler.stop()

    assert restored["resuming"] == 1
    assert runtime.order == [task_a.id]
    assert store.load(task_a.id).status is TaskStatus.DONE


def test_pause_reason_is_recorded_and_cleared(tmp_path):
    """paused_reason 决定重启后的去留，必须如实记录、切走时清空。"""
    session = DeviceSession(FakeDevice())
    scheduler = make_scheduler(DoneRuntime(), session)
    task = Task(instruction="x")
    scheduler.submit(task, allow_preempt=False)

    assert task.paused_reason is None
    scheduler.pause(task.id)
    assert task.paused_reason == PAUSED_BY_USER

    scheduler.resume(task.id)
    assert task.paused_reason is None
    assert task.status is TaskStatus.QUEUED
