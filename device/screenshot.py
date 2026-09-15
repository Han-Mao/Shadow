"""截图采集：落盘到 artifacts 目录并返回路径。

走端口 `screenshot(path)`（与 `screenshot_bytes()` 同一来源）：
ADB 后端是 `exec-out screencap`，Android 后端是 MediaProjection。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .controller import DeviceController


def capture(
    controller: DeviceController, artifact_dir: str | Path, name: str | None = None
) -> Path:
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    filename = name or f"shot_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
    return controller.screenshot(artifact_dir / filename)
