"""V2 核心能力演示：任务抢占与恢复（对应《V2 审核建议》§二十二）。

剧本：

    用户：帮我在淘宝搜索一双黑色运动鞋          → 任务 A 开始执行
    ...A 跑到第 2 步...
    用户：先帮我打开微信给张三发"晚上开会"       → 任务 B（HIGH）插入
    Scheduler：A 落 Checkpoint → 让出设备 → B 执行 → B 完成 → A 从恢复点继续

离线运行，不需要 adb / 模拟器 / API Key：

    python scripts/demo_preemption.py
"""
from __future__ import annotations

import logging
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent.runtime as runtime_mod  # noqa: E402
from agent.runtime import AgentRuntime, RunOutcome  # noqa: E402
from agent.scheduler import TaskScheduler  # noqa: E402
from agent.task_manager import TaskManager  # noqa: E402
from agent.verifier import Verification  # noqa: E402
from agent.classifier import TaskClassifier  # noqa: E402
from device.session import DeviceSession  # noqa: E402
from models.action import Action, ActionType, Decision, Point  # noqa: E402
from models.state import Observation, StepOutcome  # noqa: E402
from models.task import Task, TaskPriority  # noqa: E402
from storage import CheckpointStore, TaskStore, TrajectoryStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("demo")


class PrintedDevice:
    """假设备：把每一次真实下发到「设备」的动作打印出来。"""

    def __init__(self) -> None:
        self.events: list[str] = []

    def tap(self, x: int, y: int) -> None:
        self.events.append(f"tap({x},{y})")
        log.info("   [设备] tap(%s, %s)", x, y)

    def screen_size(self) -> tuple[int, int]:
        return (1080, 2400)


def install_stubs(tmp: Path) -> None:
    """把「看页面 / 问模型 / 判断结果」换成脚本，只保留调度与运行时这条真实链路。"""
    counter = {"a": 0}

    def fake_observe(adb, artifact_dir, step, suffix=""):
        return Observation(
            step=step,
            screenshot_path=str(tmp / f"step_{step:03d}{'_' + suffix if suffix else ''}.png"),
            package="com.taobao.taobao",
            activity=".Main",
            ui_tree='<hierarchy><node bounds="[0,0][10,10]" clickable="true" text="搜索"/></hierarchy>',
        )

    def fake_generate_plan(instruction, screenshot_path, ui_tree=None):
        return ["打开 App", "进入搜索", "输入关键词", "完成"]

    def fake_plan_next(instruction, screenshot_path, ui_tree=None, history=None, plan=None, current_step=None):
        if "微信" in instruction:
            return Decision(action=Action(type=ActionType.DONE, reason="微信任务完成"))

        index = counter["a"]
        counter["a"] += 1
        time.sleep(0.4)  # 模拟一次 VLM 决策耗时，让抢占有机会发生
        if index >= 3:
            return Decision(action=Action(type=ActionType.DONE, reason="淘宝任务完成"))
        return Decision(
            action=Action(type=ActionType.TAP, target=Point(x=300 + index * 40, y=800), reason=f"第 {index + 1} 步"),
            step_done=True,
        )

    def fake_replan(context, screenshot_path, ui_tree, history=None):
        log.warning("   [换策略] %s", context.failure_reason)
        return Decision(action=Action(type=ActionType.DONE, reason="换策略后完成"))

    runtime_mod.observer.observe = fake_observe
    runtime_mod.planner.generate_plan = fake_generate_plan
    runtime_mod.planner.plan_next_action = fake_plan_next
    runtime_mod.planner.replan = fake_replan
    runtime_mod.verifier.verify_action = lambda *a, **k: Verification(
        outcome=StepOutcome.OK, layer="stub", message="页面已变化"
    )


def main() -> None:
    tmp = Path("artifacts/demo_state")
    if tmp.exists():
        shutil.rmtree(tmp)  # 每次从干净状态演示，避免上一轮的任务混进列表
    install_stubs(tmp)

    session = DeviceSession(PrintedDevice(), serial="emulator-demo")
    task_store = TaskStore(tmp / "tasks")
    checkpoints = CheckpointStore(tmp / "checkpoints")
    trajectory = TrajectoryStore()

    runtime = AgentRuntime(
        session,
        artifact_dir=Path("artifacts/shots"),
        trajectory=trajectory,
        checkpoints=checkpoints,
        task_store=task_store,
    )
    scheduler = TaskScheduler(runtime, session, task_store=task_store, idle_poll_seconds=0.05)
    manager = TaskManager(store=task_store, scheduler=scheduler, classifier=TaskClassifier())

    scheduler.start()

    try:
        log.info("")
        log.info("用户：#1 帮我在淘宝搜索一双黑色运动鞋")
        task_a = manager.create("帮我在淘宝搜索一双黑色运动鞋", max_steps=8, priority=TaskPriority.NORMAL)

        # 等 A 真的跑起来（拿到设备）
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not session.owned_by(task_a.id):
            time.sleep(0.05)
        log.info("A 已取得设备，执行中 …")
        time.sleep(0.6)

        log.info("")
        log.info('用户：#2 先帮我打开微信给张三发"晚上开会"')
        result = manager.inject(
            '先帮我打开微信给张三发"晚上开会"',
            current_task_id=task_a.id,
            priority=TaskPriority.HIGH,
        )
        log.info("TaskClassifier：%s", result.relation.describe())
        log.info("TaskManager：%s", result.message)

        # 等 A 恢复并跑完
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not task_a.is_terminal:
            time.sleep(0.1)

        log.info("")
        log.info("=========== 最终状态 ===========")
        for task in sorted(manager.list_all(), key=lambda t: t.created_at):
            log.info("任务 %s  [%s / %s]", task.instruction, task.status.value, task.priority.value)
            log.info("   计划：%s", task.plan_progress())
        log.info("调度器：%s", scheduler.snapshot())

        cp = checkpoints.latest_for_task(task_a.id)
        if cp is not None:
            log.info("A 的最新恢复点：%s（当时页面 %s/%s）", cp.id, cp.package, cp.activity)
    finally:
        scheduler.stop()


if __name__ == "__main__":
    main()
