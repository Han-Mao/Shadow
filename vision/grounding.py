"""元素 → 屏幕坐标（bbox 中心 / UI 树 bounds）。不做决策。

坐标换算是**纯几何**，与后端无关——所以这里只依赖 `DeviceController` 端口
（唯一用到设备的地方是「归一化坐标要查屏幕尺寸」），ADB 与 Android 后端共用同一份逻辑。
"""
from __future__ import annotations

import re

from device.controller import DeviceController
from models.action import Point

from . import parser

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ALPHA_RE = re.compile(r"[a-zA-Z]")


class GroundingError(RuntimeError):
    pass


def screen_size(controller: DeviceController) -> tuple[int, int]:
    return controller.screen_size()


def _is_fraction(value: float) -> bool:
    """仅把 (0,1) 的分数视为归一化比例；整数与 >=1 的浮点一律视为像素坐标。"""
    return 0 < value < 1


def _norm_to_pixel(value: float, size: int) -> int:
    if _is_fraction(value):
        return int(round(value * size))
    return int(round(value))


def _numbers_of(text: str) -> list[float] | None:
    """恰好 4 个数字 → bbox；恰好 2 个且不含字母 → 点坐标；否则 None。"""
    numbers = [float(n) for n in _NUMBER_RE.findall(text)]
    if len(numbers) == 4:
        return numbers
    if len(numbers) == 2 and not _ALPHA_RE.search(text):
        return numbers
    return None


# planner 的候选列表格式是 `{label} [{x},{y}]`（见 `vlm._compact_ui_tree`），
# 模型会把整行**原样回填**成 target。麻烦在于 label 里经常自带数字
# （"工具,文件夹,9个应用"、"11个应用，10条通知"），于是「数一数有几个数字」
# 这条判据全线失效：凑够 4 个就被当成 bbox，静默点到 label 数字算出来的位置。
# 方括号结构比数字个数可靠得多，所以先认它。
_BRACKET_BBOX_RE = re.compile(
    r"\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]"
    r"\s*"
    r"\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]"
)
_BRACKET_POINT_RE = re.compile(r"\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]")


def _numbers_from_brackets(text: str) -> list[float] | None:
    """方括号坐标：`[x1,y1][x2,y2]` → bbox；`[x,y]` → 点。认不出返回 None。

    **必须在 `_numbers_of` 之前判**——两者的判据会打架，而这里更可靠：
    `生活,文件夹,11个应用，10条通知 [773,361]` 按数字个数算是 4 个（11/10/773/361），
    会得出一个完全错误的 bbox（中心落在屏幕左上角附近，**静默点错地方**）；
    按方括号结构算只有一个 `[773,361]`，那正是要点的图标。
    """
    bbox = _BRACKET_BBOX_RE.search(text)
    if bbox:
        return [float(group) for group in bbox.groups()]
    point = _BRACKET_POINT_RE.search(text)
    if point:
        return [float(point.group(1)), float(point.group(2))]
    return None


def _extract_numbers(text: str) -> list[float] | None:
    """从 target 字符串里提取坐标数字，非坐标描述返回 None。

    三道守卫避免把普通描述误判成坐标：
    ① 方括号结构优先 —— planner 候选行 `{label} [{x},{y}]` 的直接产物；
    ② 含拉丁字母的字符串不做 2 数提取 —— 否则 "微信 v8.0.32" 会变成坐标 (8, 32)；
    ③ 先原样匹配，再剥离中文引导词后重试 —— 兼顾 "点击(540, 1200)" 这类带前缀的写法。
    """
    if not text:
        return None
    if numbers := _numbers_from_brackets(text):
        return numbers
    if numbers := _numbers_of(text):
        return numbers
    return _numbers_of(_CJK_RE.sub("", text))


def _point_to_pixel(controller: DeviceController, x: float, y: float) -> tuple[int, int]:
    """像素坐标直接取整，不查设备；只有归一化坐标才问屏幕尺寸。

    「不查设备」是有意省掉一次设备往返（ADB 后端是 `dumpsys`，Android 后端是一次桥调用）——
    模型给像素坐标是常态，为它多跑一趟不划算。
    """
    if _is_fraction(x) or _is_fraction(y):
        width, height = screen_size(controller)
        return _norm_to_pixel(x, width), _norm_to_pixel(y, height)
    return int(round(x)), int(round(y))


def _bbox_center(controller: DeviceController, box: list[float]) -> tuple[int, int]:
    x1, y1, x2, y2 = box
    if any(_is_fraction(v) for v in box):
        width, height = screen_size(controller)
        x1, y1 = _norm_to_pixel(x1, width), _norm_to_pixel(y1, height)
        x2, y2 = _norm_to_pixel(x2, width), _norm_to_pixel(y2, height)
    return round((x1 + x2) / 2), round((y1 + y2) / 2)


# 模型给结构化 target 时，字段名的**优先级本身就是「该怎么匹配」的声明**：
# text / content-desc 是给人看的标签，resource-id 是给机器的标识。
# 顺序与 `parser._text_score` 取字段的顺序保持一致，免得两处各判各的。
_TARGET_LABEL_KEYS = (
    "text",
    "content-desc",
    "content_desc",
    "resource-id",
    "resource_id",
    "description",
    "desc",
)


def _point_from_mapping(target: dict) -> tuple[float, float] | None:
    """`{"x": 540, "y": 1200}` → 点；缺一个、或不是数字，返回 None。"""
    x, y = target.get("x"), target.get("y")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return float(x), float(y)
    return None


def _label_from_mapping(target: dict) -> str:
    """按字段优先级取出可匹配的标签；一个都没有就返回空串。"""
    for key in _TARGET_LABEL_KEYS:
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def resolve_target(
    controller: DeviceController,
    target: Point | str | dict | None,
    ui_tree: str | None = None,
) -> tuple[int, int]:
    """把 target 解析成屏幕坐标。支持：Point、坐标串/bbox 串、语义描述、结构化对象。

    解析失败时抛出 GroundingError，不静默回退到屏幕中心。
    """
    if isinstance(target, Point):
        return _point_to_pixel(controller, target.x, target.y)

    if target is None:
        raise GroundingError("target 为空，无法解析坐标")

    if isinstance(target, dict):
        # 模型有时把定位信息写成结构化对象，例如
        #     {"text": "耳机有线耳机"}   或   {"resource-id": "com.x.ui:id/search"}
        # 这比一串自由文本**更有信息量**（字段名直接说明该用哪个字段匹配），
        # 丢掉太可惜 —— 2026-09-18 真机上，一个任务里 4 次尝试全是这么被拒的，
        # 而报文只说「没找到元素」，完全看不出真正的原因是**类型不支持**。
        point = _point_from_mapping(target)
        if point is not None:
            return _point_to_pixel(controller, point[0], point[1])
        label = _label_from_mapping(target)
        if not label:
            raise GroundingError(f"target 是对象，但里面没有可用的定位信息: {target!r}")
        target = label  # 交给下面的文本匹配

    if isinstance(target, str):
        numbers = _extract_numbers(target)
        if numbers:
            if len(numbers) == 4:
                return _bbox_center(controller, numbers)
            return _point_to_pixel(controller, numbers[0], numbers[1])

        if not ui_tree:
            raise GroundingError(f"无法解析目标坐标，且没有可用的 UI 树: {target!r}")
        try:
            root = parser.parse(ui_tree)
        except Exception as exc:
            raise GroundingError(f"UI 树解析失败: {exc}") from exc
        node = parser.match_by_text(parser.find_clickable(root), target)
        if node is None:
            raise GroundingError(f"未在 UI 树中找到可点击元素: {target!r}")
        return node.center

    raise GroundingError(f"不支持的 target 类型: {type(target).__name__}")
