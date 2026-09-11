"""截图采集：落盘到 artifacts 目录并返回路径。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .adb import AdbController


def capture(adb: AdbController, artifact_dir: str | Path, name: str | None = None) -> Path:
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    filename = name or f"shot_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
    return adb.screenshot(artifact_dir / filename)
