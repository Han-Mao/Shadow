"""元素 → 屏幕坐标（bbox 中心 / UI 树 bounds）。不做决策。"""
from __future__ import annotations

import re

from device.adb import AdbController
from models.action import Point

from . import parser


class GroundingError(RuntimeError):
    pass


def screen_size(adb: AdbController) -> tuple[int, int]:
    return adb.screen_size()


def _norm_to_pixel(value: int | float, size: int) -> int:
    """仅把 (0,1) 的分数视为归一化比例；整数与 >=1 的浮点一律视为像素坐标。"""
    if isinstance(value, float) and 0 < value < 1:
        return int(round(value * size))
    return int(value)


def _parse_bbox(text: str) -> tuple[float, float, float, float] | None:
    """从字符串中解析 [x1,y1,x2,y2] 或 x1,y1,x2,y2。保留小数以支持归一化坐标。"""
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    if len(numbers) == 4:
        return tuple(float(n) for n in numbers)
    return None


def resolve_target(
    adb: AdbController,
    target: Point | str | None,
    ui_tree: str | None = None,
) -> tuple[int, int]:
    """把 target 解析成屏幕坐标。支持：Point、描述字符串、bbox 字符串。

    解析失败时抛出 GroundingError，不静默回退到屏幕中心。
    """
    if isinstance(target, Point):
        return target.x, target.y

    if target is None:
        raise GroundingError("target 为空，无法解析坐标")

    if isinstance(target, str):
        bbox = _parse_bbox(target)
        if bbox:
            width, height = screen_size(adb)
            x1, y1, x2, y2 = bbox
            x1 = _norm_to_pixel(x1, width)
            x2 = _norm_to_pixel(x2, width)
            y1 = _norm_to_pixel(y1, height)
            y2 = _norm_to_pixel(y2, height)
            return round((x1 + x2) / 2), round((y1 + y2) / 2)

        if ui_tree:
            try:
                root = parser.parse(ui_tree)
            except Exception as exc:
                raise GroundingError(f"UI 树解析失败: {exc}") from exc
            nodes = parser.find_clickable(root)
            node = parser.match_by_text(nodes, target)
            if node:
                return node.center
            raise GroundingError(f"未在 UI 树中找到可点击元素: {target!r}")

    raise GroundingError(f"不支持的 target 类型: {type(target).__name__}")
