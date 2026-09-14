"""采集 Observation（截图 + Accessibility UI 树 + 上下文）。"""
from __future__ import annotations

import os
from pathlib import Path

from device import accessibility, screenshot
from device.adb import AdbController
from models.state import Observation

# 一次采集的**总**预算（秒）。
#
# 为什么要有总预算：采集内部要跑 6 条 adb 命令（截图 + `wm size` + `dumpsys window`
# + rm + uiautomator dump + cat）。每条命令自己是有超时的（采集类 6s），
# 但 6 条各自卡住就是 36 秒——而「采集」是 runtime 循环里最容易卡住的一步，
# 它的耗时直接决定了高优先级任务要等多久才能拿到设备（V2.1 §十四）。
#
# 给了总预算之后：每条命令的超时取 `min(自己的超时, 剩余预算)`，
# 预算用完就直接报错退出，不再开始下一条。于是**一次采集的耗时有上界**，
# 抢占延迟也才有可论证的上界——从「6 条累加」降到「预算 + 1 条」。
OBSERVE_BUDGET_SECONDS = float(os.getenv("OBSERVE_BUDGET_SECONDS", "12"))
# adb 采集类命令的单条超时（截图 / dump / dumpsys）
ADB_READ_TIMEOUT_SECONDS = float(os.getenv("ADB_READ_TIMEOUT_SECONDS", "6"))


def observe(
    adb: AdbController,
    artifact_dir: str | Path,
    step: int,
    suffix: str = "",
    *,
    budget_seconds: float = OBSERVE_BUDGET_SECONDS,
) -> Observation:
    """采集当前设备状态。suffix 用于区分同一 step 前后的多次采集，避免截图互相覆盖。"""
    name = f"step_{step:03d}" + (f"_{suffix}" if suffix else "") + ".png"
    with adb.deadline_budget(budget_seconds):
        path = screenshot.capture(adb, artifact_dir, name=name)
        screen_size = adb.screen_size()
        package, activity = adb.current_focus()
        ui_tree = accessibility.dump_ui_tree(adb)
    return Observation(
        step=step,
        screenshot_path=str(path),
        screen_size=screen_size,
        package=package,
        activity=activity,
        ui_tree=ui_tree,
    )
