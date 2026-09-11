"""Accessibility XML 解析。"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass


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
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value)
    if not m:
        raise ValueError(f"无法解析 bounds: {value}")
    return tuple(int(g) for g in m.groups())


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
        bounds=_parse_bounds(attr("bounds", "[0,0][0,0]")),
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


def match_by_text(nodes: list[UiNode], description: str) -> UiNode | None:
    desc = description.lower()
    for node in nodes:
        if desc in node.text.lower() or desc in node.content_desc.lower() or desc in node.resource_id.lower():
            return node
    return None
