"""模拟器设备发现与 serial 解析。"""
from __future__ import annotations

import os
import subprocess

from .adb import DEFAULT_SERIAL


def _list_devices() -> list[tuple[str, str]]:
    """adb devices → [(serial, state), ...]"""
    proc = subprocess.run(["adb", "devices"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "adb devices 失败")
    devices = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2:
            devices.append((parts[0], parts[1]))
    return devices


def resolve_serial(preferred: str | None = None) -> str:
    """优先级：显式参数 > 环境变量 ADB_SERIAL > 单一可用设备 > 默认 emulator-5554。"""
    if preferred:
        return preferred
    if env_serial := os.getenv("ADB_SERIAL"):
        return env_serial
    try:
        devices = [s for s, st in _list_devices() if st == "device"]
        if len(devices) == 1:
            return devices[0]
    except Exception:
        pass
    return DEFAULT_SERIAL
