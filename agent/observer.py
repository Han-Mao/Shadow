"""采集 Observation（截图 + Accessibility UI 树 + 上下文）。"""
from __future__ import annotations

from pathlib import Path

from device import accessibility, screenshot
from device.adb import AdbController
from models.state import Observation


def observe(adb: AdbController, artifact_dir: str | Path, step: int, suffix: str = "") -> Observation:
    """采集当前设备状态。suffix 用于区分同一 step 前后的多次采集，避免截图互相覆盖。"""
    name = f"step_{step:03d}" + (f"_{suffix}" if suffix else "") + ".png"
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
