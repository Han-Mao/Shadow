"""多设备 + 子任务注入 + 崩溃恢复的端到端用例（V2.2 §二 / §十一 / §十二）。

审核对测试体系的判断：

> 目前很多代码看起来正确，真正一起跑起来很可能出状态机问题。
> 尤其要覆盖：双设备、双任务、同时注入、HITL、进程崩溃、恢复。

这一组用例专门补两类缺口：
1. **跨设备干扰**：改写 A 设备上的任务目标，绝不能波及 B 设备上毫不相干的任务。
2. **运行中改计划 + 崩溃**：SUBTASK 插入改了计划，版本号必须跟着动，
   否则「这条恢复点属于哪一版计划」永远说不清；而且崩溃重启后插入的步骤不能丢。
"""
from __future__ import annotations

from agent.runtime import RunOutcome
from agent.scheduler import TaskScheduler
from agent.task_manager import InjectAction, TaskManager
from device.pool import DevicePool
from device.session import DeviceSession
from fakes import FakeDevice
from models.budget import TaskBudget
from models.checkpoint import Checkpoint
from models.state import Observation
from models.task import Task, TaskStatus
from models.task_relation import TaskRelation, TaskRelationResult
from models.task_step import StepStatus
from storage import CheckpointStore, TaskStore


class DoneRuntime:
    """只回答「完成」的 runtime 替身——这里测的是调度与任务管理，不是页面操作。

    顺手把步骤标成 DONE：真实 runtime 收尾时也会这么做，替身不做的话
    「计划跑完了」这个断言就永远不成立。
    """

    def run(self, task: Task) -> RunOutcome:
        for step in task.plan:
            step.mark(StepStatus.DONE)
        task.sync_current_step()
        return RunOutcome.DONE


class SuperTaskClassifier:
    def classify(self, instruction, *, current=None, candidates=()):
        return TaskRelationResult(
            relation=TaskRelation.SUPER_TASK,
            confidence=0.9,
            affected_task_id=current.id if current else None,
            reason="改成搜索高铁",
        )


class SubtaskClassifier:
    def classify(self, instruction, *, current=None, candidates=()):
        return TaskRelationResult(
            relation=TaskRelation.SUBTASK,
            confidence=0.9,
            affected_task_id=current.id if current else None,
            reason="前置步骤",
        )


def build_pool(*serials: str):
    sessions = {serial: DeviceSession(FakeDevice(), serial=serial) for serial in serials}
    return DevicePool(list(sessions.values())), sessions


def running_on(scheduler: TaskScheduler, pool: DevicePool, task: Task, serial: str) -> Task:
    """把任务直接摆成「正在这台设备上执行」的状态（避免线程时序不确定）。"""
    task.mark(TaskStatus.RUNNING)
    pool.require(serial).acquire(task.id)
    with scheduler._cond:  # noqa: SLF001 - 与既有测试一致的场景摆法
        scheduler._lanes[serial].running = task
    return task


# ---------------------------------------------------------------- 跨设备干扰


def test_super_task_preempts_only_its_own_device(tmp_path):
    """改写 A 上的任务目标，绝不能打断 B 上毫不相干的任务（V2.2 §二）。

    旧实现调的是 `preempt_running()`（不带 task_id），在多设备下会退化成
    「所有车道都让出」——A 被改写目标，B 却在旁边陪着一起挂起。
    """
    store = TaskStore(tmp_path / "tasks")
    pool, sessions = build_pool("emu-1", "emu-2")
    scheduler = TaskScheduler(DoneRuntime(), pool, task_store=store, idle_poll_seconds=0.01)
    manager = TaskManager(store=store, scheduler=scheduler, classifier=SuperTaskClassifier())

    task_a = Task(instruction="在淘宝搜索运动鞋", device_serial="emu-1")
    task_b = Task(instruction="看一部电影", device_serial="emu-2")
    store.save(task_a)
    store.save(task_b)
    running_on(scheduler, pool, task_a, "emu-1")
    running_on(scheduler, pool, task_b, "emu-2")

    result = manager.inject("改成搜索高铁", current_task_id=task_a.id, allow_disruptive=True)

    assert result.action is InjectAction.SUPERSEDED
    assert sessions["emu-1"].should_yield(task_a.id), "被改写的任务应当让位"
    assert not sessions["emu-2"].should_yield(task_b.id), "另一台设备上的任务不该被牵连"
    assert sessions["emu-2"].snapshot()["preempt_requested_for"] is None


def test_running_tasks_exposes_every_device(tmp_path):
    """多设备下必须能拿到**全部**在跑的任务。

    旧的单设备视图（`_running`）只返回第一台，上层据此会以为系统里只跑了一个任务。
    """
    pool, _ = build_pool("emu-1", "emu-2")
    scheduler = TaskScheduler(DoneRuntime(), pool, idle_poll_seconds=0.01)

    task_a = Task(instruction="A", device_serial="emu-1")
    task_b = Task(instruction="B", device_serial="emu-2")
    running_on(scheduler, pool, task_a, "emu-1")
    running_on(scheduler, pool, task_b, "emu-2")

    running = scheduler.running_tasks()
    snapshot = scheduler.snapshot()

    assert {task.id for task in running} == {task_a.id, task_b.id}
    assert snapshot["running_tasks"] == {"emu-1": task_a.id, "emu-2": task_b.id}
    assert scheduler._running is not None, "兼容视图仍然可用（但不该被业务逻辑使用）"


# ---------------------------------------------------------------- 运行中插入子任务


def _checkpoint_for(task: Task, tmp_path) -> Checkpoint:
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    observation = Observation(
        step=1,
        screenshot_path=str(tmp_path / "s.png"),
        package="com.taobao.taobao",
        activity=".SearchActivity",
        ui_tree='<hierarchy><node class="android.widget.TextView" text="搜索"/></hierarchy>',
    )
    checkpoint = Checkpoint.capture(
        task_id=task.id,
        step=1,
        step_states=task.step_states(),
        observation=observation,
        task_version=task.version,
        plan_version=task.plan_version,
    )
    checkpoints.save(checkpoint)
    return checkpoint


def test_subtask_merge_bumps_plan_version(tmp_path):
    """计划被改过就必须动版本号（V2.2 §十一）。

    目标没变（version 不动），所以旧恢复点仍然可用；但 `plan_version` 必须递增，
    否则没有任何办法回答「这份计划是哪一版」。
    """
    store = TaskStore(tmp_path / "tasks")
    pool, _ = build_pool("emu-1")
    scheduler = TaskScheduler(DoneRuntime(), pool, task_store=store, idle_poll_seconds=0.01)
    manager = TaskManager(store=store, scheduler=scheduler, classifier=SubtaskClassifier())

    task = manager.create("逛淘宝", budget=TaskBudget(), submit=False)
    task.set_plan(["搜索运动鞋", "加入购物车"])
    store.save(task)
    version_before, plan_version_before = task.version, task.plan_version

    result = manager.inject("先打开微信", current_task_id=task.id)

    assert result.action is InjectAction.MERGED
    # 任务没进调度器时 inject 拿到的是从磁盘反序列化出来的副本，
    # 所以断言要看落盘后的状态，而不是手里那个旧对象
    reloaded = store.load(task.id)
    assert reloaded.plan_version == plan_version_before + 1, "插入步骤必须递增 plan_version"
    assert reloaded.version == version_before, "任务目标没变，version 不该动"
    goals = [step.goal for step in reloaded.plan]
    assert goals[0] == "先打开微信", "「先……」语义要求插到待执行首位"
    assert set(goals) == {"先打开微信", "搜索运动鞋", "加入购物车"}
    assert reloaded.plan[1].depends_on == [reloaded.plan[0].id], "串行依赖链要保持完整"


def test_subtask_inserted_while_running_survives_crash(tmp_path):
    """「运行中插入子任务 → 落检查点 → 进程崩溃 → 恢复」这条链必须走通（§十一/§十二）。

    审核点名要补的就是它：改计划的地方不更新版本号，崩溃恢复时就没有任何依据
    判断旧恢复点属于哪一版计划。
    """
    store = TaskStore(tmp_path / "tasks")
    pool, _ = build_pool("emu-1")
    scheduler = TaskScheduler(DoneRuntime(), pool, task_store=store, idle_poll_seconds=0.01)
    manager = TaskManager(store=store, scheduler=scheduler, classifier=SubtaskClassifier())

    task = manager.create("逛淘宝", budget=TaskBudget(), submit=False)
    task.set_plan(["搜索运动鞋", "加入购物车"])
    running_on(scheduler, pool, task, "emu-1")

    # 执行到一半，落了恢复点
    checkpoint = _checkpoint_for(task, tmp_path)
    task.checkpoint_id = checkpoint.id
    store.save(task)
    plan_version_at_checkpoint = task.plan_version

    # 用户插入前置步骤：计划被改写，版本号跟着动
    manager.inject("先打开微信", current_task_id=task.id)
    assert task.plan_version == plan_version_at_checkpoint + 1

    # ---- 模拟进程崩溃：内存队列全丢，调度器重建 ----
    revived = TaskScheduler(DoneRuntime(), pool, task_store=store, idle_poll_seconds=0.01)
    restored = revived.recover()

    reloaded = store.load(task.id)
    assert [step.goal for step in reloaded.plan][0] == "先打开微信"
    assert len(reloaded.plan) == 3, "崩溃不能把运行中插入的步骤丢掉"
    assert reloaded.plan_version == plan_version_at_checkpoint + 1, "版本号必须一起落盘"
    assert restored["queued"] == 1 or restored["resuming"] == 1, "任务必须被重新投递"
    assert reloaded.checkpoint_id == checkpoint.id, "恢复点仍在（目标没变，无需作废）"

    # 恢复点记录的计划版本能对上「它是改计划之前的那一版」
    saved = CheckpointStore(tmp_path / "checkpoints").load(task.id, checkpoint.id)
    assert saved is not None
    assert saved.plan_version == plan_version_at_checkpoint


def test_recovered_subtask_task_still_reaches_completion(tmp_path):
    """崩溃恢复之后，带插入步骤的计划要能被正常执行完，而不是卡住。"""
    store = TaskStore(tmp_path / "tasks")
    pool, _ = build_pool("emu-1")
    scheduler = TaskScheduler(DoneRuntime(), pool, task_store=store, idle_poll_seconds=0.01)
    manager = TaskManager(store=store, scheduler=scheduler, classifier=SubtaskClassifier())

    task = manager.create("逛淘宝", budget=TaskBudget(), submit=False)
    task.set_plan(["搜索运动鞋"])
    running_on(scheduler, pool, task, "emu-1")
    manager.inject("先打开微信", current_task_id=task.id)

    revived = TaskScheduler(DoneRuntime(), pool, task_store=store, idle_poll_seconds=0.01)
    revived.recover()
    # 测试替身把设备「借」给了这条任务；进程重启后设备当然是空闲的
    pool.require("emu-1").release(task.id)
    revived.start()
    try:
        import time

        for _ in range(200):
            if store.load(task.id).is_terminal:
                break
            time.sleep(0.02)
        final = store.load(task.id)
    finally:
        revived.stop()

    assert final.status is TaskStatus.DONE
    assert len(final.plan) == 2
    assert all(step.status is StepStatus.DONE for step in final.plan)
