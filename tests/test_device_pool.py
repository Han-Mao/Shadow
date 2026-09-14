"""DevicePool（V2.1 §十三）：把「设备」从全局唯一变成可枚举的一等公民。"""
from __future__ import annotations

import pytest

from device.pool import DevicePool, UnknownDeviceError, build_pool, storage_hint
from device.session import DeviceSession
from fakes import FakeDevice


def session(serial: str) -> DeviceSession:
    return DeviceSession(FakeDevice(), serial=serial)


def test_register_and_lookup_by_serial():
    pool = DevicePool()
    first = pool.register(session("emu-1"))

    assert pool.get("emu-1") is first
    assert pool.get("emu-9") is None
    assert pool.get(None) is None
    assert pool.serials == ["emu-1"]
    assert "emu-1" in pool
    assert len(pool) == 1


def test_require_raises_with_the_available_serials_in_the_message():
    """报错要顺带告诉调用方「有哪些设备可用」，否则排查还得回去翻配置。"""
    pool = DevicePool([session("emu-1")])

    with pytest.raises(UnknownDeviceError) as excinfo:
        pool.require("emu-9")

    assert "emu-9" in str(excinfo.value)
    assert "emu-1" in str(excinfo.value)


def test_idle_only_returns_unoccupied_devices():
    pool = DevicePool([session("emu-1"), session("emu-2")])
    pool.require("emu-1").acquire("t1")

    assert [item.serial for item in pool.idle()] == ["emu-2"]


def test_blank_serial_gets_a_fallback_name():
    """serial 为空时也要能在池里被寻址，否则任务永远派不到它。"""
    pool = DevicePool()
    pool.register(DeviceSession(FakeDevice(), serial=""))

    assert pool.serials == ["device-1"]


def test_snapshot_shape():
    pool = DevicePool([session("emu-1")])

    snap = pool.snapshot()

    assert snap["count"] == 1
    assert snap["devices"]["emu-1"]["busy"] is False
    assert snap["devices"]["emu-1"]["serial"] == "emu-1"


def test_build_pool_from_serial_list():
    pool = build_pool(lambda serial: FakeDevice(), serials=["a", "b"])

    assert pool.serials == ["a", "b"]
    assert build_pool(lambda serial: FakeDevice(), serials=None).serials == []


def test_storage_hint_keeps_devices_apart(tmp_path):
    """多设备的产物必须能归属到具体设备；混在一个目录里就分不清是谁的了。"""
    assert storage_hint(tmp_path, "emu-1") == tmp_path / "emu-1"
    assert storage_hint(tmp_path, "a/b") == tmp_path / "a_b"
    assert storage_hint(tmp_path, "") == tmp_path / "default"


def test_iteration_and_first():
    pool = DevicePool([session("a"), session("b")])

    assert [item.serial for item in pool] == ["a", "b"]
    assert pool.first() is pool.require("a")
    assert DevicePool().first() is None
