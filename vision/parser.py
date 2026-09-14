"""Accessibility XML 解析。"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass

# 真实设备的 UI 树里 bounds 可能是负数（离屏 / 不可见元素，如 [-1,-1][-1,-1]
# 或 [-2147483648,-2147483648][2147483647,2147483647]），必须允许负号
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass
class UiNode:
    index: str
    text: str
    resource_id: str
    class_name: str
    package: str
    content_desc: str
    clickable: bool
    enabled: bool
    bounds: tuple[int, int, int, int]
    children: list["UiNode"]

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2


def _parse_bounds(value: str) -> tuple[int, int, int, int]:
    """解析 "[x1,y1][x2,y2]"。

    单个节点的异常 bounds 不应让整棵树解析失败（那会让路线 B 直接降级为纯 VLM），
    因此这里降级返回零矩形，而不是抛异常。
    """
    m = _BOUNDS_RE.search(value or "")
    if not m:
        return (0, 0, 0, 0)
    x1, y1, x2, y2 = (int(g) for g in m.groups())
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return x1, y1, x2, y2


def _build(node: ET.Element) -> UiNode:
    def attr(name: str, default: str = "") -> str:
        return node.get(name, default)

    return UiNode(
        index=attr("index"),
        text=attr("text"),
        resource_id=attr("resource-id"),
        class_name=attr("class"),
        package=attr("package"),
        content_desc=attr("content-desc"),
        clickable=attr("clickable", "false").lower() == "true",
        enabled=attr("enabled", "true").lower() == "true",
        bounds=_parse_bounds(attr("bounds")),
        children=[_build(child) for child in node],
    )


def parse(xml: str) -> UiNode:
    root = ET.fromstring(xml)
    return _build(root)


def find_clickable(root: UiNode) -> list[UiNode]:
    result: list[UiNode] = []
    if root.clickable and root.enabled:
        result.append(root)
    for child in root.children:
        result.extend(find_clickable(child))
    return result


def iter_nodes(root: UiNode) -> Iterator[UiNode]:
    """深度优先遍历全部节点（含不可点击的容器）。

    与 `find_clickable` 的区别：那个只回答「能点什么」，这个回答「屏幕上有什么」。
    验证「目标元素还在不在」时需要后者——元素点完可能变成不可点击，但它是否消失
    才是关键信号。
    """
    yield root
    for child in root.children:
        yield from iter_nodes(child)


def node_at(root: UiNode, x: float, y: float) -> UiNode | None:
    """找出坐标落在哪个节点上（取**最小**的那个，即最深/最具体的节点）。

    为什么不能取「第一个包含该点的节点」：根节点几乎总是包含整个屏幕，
    先命中它的话，风险判定永远只能看到空文本——「点击红色按钮」就查不到
    那个按钮其实叫「立即购买」。
    """
    best: UiNode | None = None
    best_area: int | None = None
    for node in iter_nodes(root):
        x1, y1, x2, y2 = node.bounds
        if x2 <= x1 or y2 <= y1:  # 零矩形 / 离屏节点：不参与命中
            continue
        if not (x1 <= x <= x2 and y1 <= y <= y2):
            continue
        area = (x2 - x1) * (y2 - y1)
        if best_area is None or area < best_area:
            best, best_area = node, area
    return best


def node_label(node: UiNode) -> str:
    """节点的可读文本：text → content-desc → resource-id。

    与风险判定共用同一套「这个节点叫什么」的定义，避免两处各判各的。
    """
    for candidate in (node.text, node.content_desc, node.resource_id):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def node_identity(node: UiNode) -> str:
    """节点身份键：只取结构性属性，不含 bounds。

    bounds 会随重绘抖动，用它做身份键会让「同一个按钮」在前后两屏里认不出来。
    """
    return "|".join(
        part or "" for part in (node.class_name, node.text, node.content_desc, node.resource_id)
    )


def match_by_text(nodes: list[UiNode], description: str) -> UiNode | None:
    """按描述匹配可点击节点。精确匹配优先，其次包含匹配。

    按分数取最优而不是「首个命中」：否则通用父容器（如空的 FrameLayout）
    会先于真正带文字的按钮被选中，落点偏到容器中心。
    """
    desc = (description or "").strip().lower()
    if not desc:
        return None

    best: UiNode | None = None
    best_score = 0
    for node in nodes:
        score = 0
        for field_value in (node.text, node.content_desc, node.resource_id):
            candidate = (field_value or "").strip().lower()
            if not candidate:
                continue
            if candidate == desc:
                score = max(score, 3)
            elif desc in candidate:
                score = max(score, 2)
        if score > best_score:
            best, best_score = node, score
            if score == 3:
                break
    return best
