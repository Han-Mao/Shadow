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


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def resolve_serials(preferred: str | None = None) -> list[str]:
    """解析出**一批**设备 serial（V2.1 §十三）。

    优先级：显式参数 > 环境变量 ADB_SERIAL > 当前所有可用设备 > 默认 emulator-5554。
    支持逗号分隔，所以 `ADB_SERIAL="emu-1,emu-2"` 就能一次拉起两台。

    两处与 `resolve_serial` 有意的差别：
    - 显式指定时**不再自动发现**——用户说了用哪几台就只用哪几台，
      否则「我只想连 A」会被自动发现的 B 悄悄打破；
    - 自动发现时有多少用多少（而不是「恰好一台才用」），
      这正是多设备想要的行为。
    """
    if preferred:
        return _split(preferred) or [DEFAULT_SERIAL]
    if env_serial := os.getenv("ADB_SERIAL"):
        return _split(env_serial) or [DEFAULT_SERIAL]
    try:
        devices = [s for s, st in _list_devices() if st == "device"]
        if devices:
            return devices
    except Exception:
        pass
    return [DEFAULT_SERIAL]
