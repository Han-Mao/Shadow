"""UI 树获取：返回与 `uiautomator dump` 兼容的 XML 字符串（路线 B 的基础，§6.2）。

走端口 `dump_ui()`：ADB 后端是 `uiautomator dump`，Android 后端把 AccessibilityNodeInfo
树序列化成**同一个结构**——格式契约与「读不到要抛异常」都写在
`device/controller.py` 的 `dump_ui` 里。

这一层刻意做得这么薄（只做转发）是有意的：格式统一是**后端**的责任，
在这一层加转换只会多出一处需要与 vision 对齐的地方。
"""
from __future__ import annotations

from .controller import DeviceController


def dump_ui_tree(controller: DeviceController) -> str:
    return controller.dump_ui()
