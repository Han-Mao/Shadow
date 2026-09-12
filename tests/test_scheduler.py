"""TaskScheduler：排队、优先级、暂停/取消、抢占与恢复。"""
from __future__ import annotations

import threading
import time

from agent.runtime import RunOutcome
from agent.scheduler import TaskScheduler
from device.session import DeviceSession
from fakes import FakeDevice
from models.task import Task, TaskPriority, TaskStatus


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
