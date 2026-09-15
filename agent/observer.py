"""采集 Observation（截图 + UI 树 + 上下文）。

这条链走 `DeviceController` **端口**，不认后端：截图/屏幕尺寸/焦点/UI 树四类设备操作，
PC 后端是 adb，Android 后端是 Accessibility + MediaProjection。
`observer` 是「一次采集」的唯一入口，所以它必须端口化——
这里若写死 ADB，手机部署就必然要 fork 一份 runtime（V3.3 §1）。
"""
from __future__ import annotations

import os
from pathlib import Path

from device import accessibility, screenshot
from device.controller import DeviceController
from models.state import Observation

# 一次采集的**总**预算（秒）。
#
# 为什么要有总预算：采集要连着做若干设备操作（截图 + 屏幕尺寸 + 焦点 + UI 树，
# ADB 后端实际是 6 条命令：截图 / wm size / dumpsys window / rm / uiautomator dump / cat），
# 每条操作自己是有超时的（采集类 6s），但逐条各自卡住就是累加——
# 而「采集」是 runtime 循环里最容易卡住的一步，
# 它的耗时直接决定了高优先级任务要等多久才能拿到设备（V2.1 §十四）。
#
# 给了总预算之后：每条操作的超时取 `min(自己的超时, 剩余预算)`，
# 预算用完就直接报错退出，不再开始下一条。于是**一次采集的耗时有上界**，
# 抢占延迟也才有可论证的上界——从「逐条累加」降到「预算 + 1 条」。
#
# 预算对两个后端都生效，但落实方式不同（见 `device/controller.py` 的协议说明）：
# ADB 是给子进程加超时；Android 是**准入控制**（预算耗尽不再发起新桥调用，
# 已经在飞的那次只能等它返回）——严格上界都是「预算 + 单次调用耗时」。
OBSERVE_BUDGET_SECONDS = float(os.getenv("OBSERVE_BUDGET_SECONDS", "12"))
# ADB 采集类命令的单条超时（截图 / dump / dumpsys）。**仅 ADB 后端**读它。
ADB_READ_TIMEOUT_SECONDS = float(os.getenv("ADB_READ_TIMEOUT_SECONDS", "6"))


def observe(
    controller: DeviceController,
    artifact_dir: str | Path,
    step: int,
    suffix: str = "",
    *,
    budget_seconds: float = OBSERVE_BUDGET_SECONDS,
) -> Observation:
    """采集当前设备状态。suffix 用于区分同一 step 前后的多次采集，避免截图互相覆盖。"""
    name = f"step_{step:03d}" + (f"_{suffix}" if suffix else "") + ".png"
    with controller.deadline_budget(budget_seconds):
        path = screenshot.capture(controller, artifact_dir, name=name)
        screen_size = controller.screen_size()
        package, activity = controller.current_focus()
        ui_tree = accessibility.dump_ui_tree(controller)
    return Observation(
        step=step,
        screenshot_path=str(path),
        screen_size=screen_size,
        package=package,
        activity=activity,
        ui_tree=ui_tree,
    )
