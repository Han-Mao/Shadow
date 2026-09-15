"""设备控制层（V3.3 §1：端口 + 多种后端）。

```
DeviceController           device/controller.py   核心只依赖这个协议
├── AdbDeviceController    device/adb.py          PC 侧：adb -s <serial> ...
└── AndroidDeviceController device/android.py     手机侧：Accessibility + MediaProjection
                               ↑
                        AndroidBridge（Kotlin 侧实现）
```

取实例请用 `device.factory.build_controller(serial)`，**不要直接 new 具体类**——
后端由部署形态决定（`SHADOW_DEVICE_BACKEND=adb|android`），
上层拿到的是同一个协议对象。
"""
from .controller import (
    READ_ONLY_OPERATIONS,
    DeviceBudgetExhausted,
    DeviceController,
    DeviceError,
    IncompleteDeviceController,
    assert_implements,
    is_read_only,
)

__all__ = [
    "READ_ONLY_OPERATIONS",
    "DeviceBudgetExhausted",
    "DeviceController",
    "DeviceError",
    "IncompleteDeviceController",
    "assert_implements",
    "is_read_only",
]
