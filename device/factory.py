"""设备后端装配（V3.3 §1）：按部署形态选择 `DeviceController` 实现。

方案文档 §10 描述的两个产品形态，差别就落在这一个函数上：

    开发模式   PC  ──ADB──▶  Android      `SHADOW_DEVICE_BACKEND=adb`（默认）
    产品模式   Android 上跑 Shadow 自己   `SHADOW_DEVICE_BACKEND=android`

**上层拿到的是同一个 `DeviceController`**，所以 `TaskManager` / `Scheduler` /
`AgentRuntime` / `RiskGate` / `Checkpoint` 一行都不用改——这正是方案文档 §1
「基本都不用重写」那句话的落点。

android 后端下，桥本身还有两条承载方式（见 `device/remote.py` 与 `android/README.md`）：

    路线 A  同进程（Chaquopy）   Kotlin 对象直接注册 → `register_android_bridge`
    路线 B  远程（设备端点）     手机开一个 HTTP 端点 ↔ Core 跑在 PC/局域网

选哪条由 `resolve_android_bridge()` 决定：**先同进程、后远程**，两条都没有就报错。
两条路线共用同一份 Kotlin 设备层，所以这个选择不改变手机端要写什么。

选错后端一律**当场报错**，不静默退回默认值：PC 上误开 android 会一路跑到第一次观察
才失败，而那时候任务已经在跑了；手机上误开 adb 同理。与 V3.2 §六「配置错误必须
fail-closed」是同一条原则——**配置错误要在启动期可见**。
"""
from __future__ import annotations

import logging
import os

from .controller import DeviceController, assert_implements

logger = logging.getLogger(__name__)

BACKEND_ADB = "adb"
BACKEND_ANDROID = "android"
SUPPORTED_BACKENDS = (BACKEND_ADB, BACKEND_ANDROID)

ENV_BACKEND = "SHADOW_DEVICE_BACKEND"


class UnknownDeviceBackend(ValueError):
    """`SHADOW_DEVICE_BACKEND` 写了不认识的值。"""


def selected_backend() -> str:
    """当前选定的设备后端。"""
    raw = (os.getenv(ENV_BACKEND) or BACKEND_ADB).strip().lower()
    if raw not in SUPPORTED_BACKENDS:
        raise UnknownDeviceBackend(
            f"{ENV_BACKEND}={raw!r} 不是支持的设备后端；"
            f"可选：{'、'.join(SUPPORTED_BACKENDS)}"
            f"（不设置则用 {BACKEND_ADB}，即 PC 通过 ADB 控制手机）"
        )
    return raw


def is_android_backend() -> bool:
    """当前是否「手机自己跑」。"""
    return selected_backend() == BACKEND_ANDROID


def resolve_android_bridge():
    """在 android 后端下拿到 `AndroidBridge`——**两条路线，一个选择顺序**。

        路线 A  同进程：Android 应用启动时 register_android_bridge(工厂)（Chaquopy）
        路线 B  远程：  配了 SHADOW_ANDROID_BRIDGE_URL → 手机上的设备端点（HTTP）

    顺序固定为「先同进程、后远程」，理由是**就近优先**：同进程没有网络这一跳，
    也不需要在手机上开监听端口（更少的攻击面）。两条都不可用时报错，
    并把两条路线的做法都写在错误信息里——这条错误信息是运维第一次部署时唯一会看到的东西，
    「配置错误要在启动期可见」（V3.2 §六、[93]）的价值全在这里。
    """
    from .android import AndroidServiceUnavailable, bridge_factory, require_android_bridge
    from .remote import (
        ENV_BRIDGE_TOKEN,
        ENV_BRIDGE_URL,
        bridge_url_from_env,
        build_remote_bridge,
    )

    if bridge_factory() is not None:
        return require_android_bridge()

    url = bridge_url_from_env()
    if url:
        logger.info("android 后端使用远程设备端点：%s", url)
        return build_remote_bridge(url)

    # 刻意不「退回 adb」：选了 android 后端说明部署者认为手机是本机/端点，
    # 静默换成 adb 会让整条链路去操作一个完全不同的设备（PC 上的 adb 目标）。
    raise AndroidServiceUnavailable(
        "已选择 android 设备后端，但既没有注册同进程桥，也没有配置远程设备端点。二选一：\n"
        "  ① 同进程：Android 应用启动时调用 device.android.register_android_bridge(工厂函数)；\n"
        f"  ② 远程：设置 {ENV_BRIDGE_URL}=http://<手机IP>:8765"
        f"（可选 {ENV_BRIDGE_TOKEN} 与手机端一致）。\n"
        "若只是想在 PC 上用 ADB 控制手机，请把 SHADOW_DEVICE_BACKEND 设为 adb（或不设置）。"
    )


def build_controller(serial: str) -> DeviceController:
    """按后端构造控制器，并**在装配期校验端口完整性**。

    `serial` 在 android 后端下只是一个标识（默认 `android-local`）——
    手机侧的「设备」就是手机自己，没有 serial 的概念；保留参数是为了让
    `DevicePool` / `DeviceSession` 那套多设备抽象不用为单机形态开特例。
    """
    backend = selected_backend()

    if backend == BACKEND_ANDROID:
        # 延迟 import：PC 侧部署不需要碰 android / remote 模块（它们 import 的东西
        # 在 PC 上也在，但保持「用不到就不加载」能让依赖边界更清楚）
        from .android import AndroidDeviceController

        controller = AndroidDeviceController(resolve_android_bridge(), serial=serial)
    else:
        from .adb import AdbController

        controller = AdbController(serial=serial)

    assert_implements(controller, backend=backend)
    return controller


def describe_backend(serial: str = "") -> str:
    """给启动日志/`/health` 用的一句话，说清「现在是谁在控制设备」。"""
    backend = selected_backend()
    if backend == BACKEND_ANDROID:
        return f"android（手机本机运行，设备标识 {serial or 'android-local'}）"
    return f"adb（PC 端控制，设备 {serial or '未指定'}）"


ANDROID_DEFAULT_SERIAL = "android-local"


def resolve_device_serial(preferred: str | None = None) -> str:
    """当前后端下「主设备」的标识。

    为什么按后端分：`device.emulator.resolve_serial` 会去跑 `adb devices` 做自动发现
    ——手机上根本没有 adb（方案文档 §1 的整个前提），也不该为了拿一个标识去启动子进程。
    Android 后端的「设备」就是这台手机本身，用一个固定标识即可；
    保留 `ADB_SERIAL` 覆盖能力是为了让多实例部署（手机 A / 手机 B）在日志里能区分。
    """
    if is_android_backend():
        raw = (preferred or os.getenv("ADB_SERIAL") or "").strip()
        first = raw.split(",")[0].strip() if raw else ""
        return first or ANDROID_DEFAULT_SERIAL

    from .emulator import resolve_serial

    return resolve_serial(preferred)


def resolve_device_serials(preferred: str | None = None) -> list[str]:
    """当前后端下要拉起的设备标识列表。

    Android 后端恒为**一台**：一台手机就是一个 Agent 主机。
    方案文档 §10 那种「手机 A / B / C 都连 Shadow Cloud」的形态里，
    **每台手机各跑一份 Shadow**，由云端汇总——不是在单份实例里挂多个设备。
    """
    if is_android_backend():
        return [resolve_device_serial(preferred)]

    from .emulator import resolve_serials

    return resolve_serials(preferred)


__all__ = [
    "ANDROID_DEFAULT_SERIAL",
    "BACKEND_ADB",
    "BACKEND_ANDROID",
    "ENV_BACKEND",
    "SUPPORTED_BACKENDS",
    "UnknownDeviceBackend",
    "build_controller",
    "describe_backend",
    "is_android_backend",
    "resolve_android_bridge",
    "resolve_device_serial",
    "resolve_device_serials",
    "selected_backend",
]
