"""uiautomator dump 封装：返回 UI 树 XML 字符串（路线 B 的基础，§6.2）。"""
from __future__ import annotations

from .adb import AdbController


def dump_ui_tree(adb: AdbController) -> str:
    return adb.dump_ui()
