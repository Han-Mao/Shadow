"""UI 结构指纹（V2.1 §四 L2）。

恢复校验原来只有两档：
    L1 版本 / package+activity  —— 太粗，同一页面内的内容变化完全看不见
    L3 语义（VLM）              —— 太贵，每次恢复都调模型不现实

中间缺的 L2 就是结构指纹：比 package/activity **细**（能看到页面内容变了），
比 VLM **便宜且不依赖网络**（纯本地解析）。

关键取舍：**指纹里不放 bounds**。
UI 树快照是逐字节存的字符串，同一页面重绘后 bounds 几乎必然有像素级差异，
硬比会永远判成「页面变了」，反而让 L2 失效。指纹只取结构性属性
（class / text / content-desc / resource-id），这些才是「是不是同一屏内容」的判据。
"""
from __future__ import annotations

import hashlib
import re

# uiautomator dump 的节点属性。bounds 刻意不参与（见模块 docstring）。
_NODE_ATTRS = ("class", "text", "content-desc", "resource-id")
_ATTR_PATTERNS = tuple(
    re.compile(rf'{name}="([^"]*)"', re.IGNORECASE) for name in _NODE_ATTRS
)

# 兼容非 XML 输入（测试替身、裁剪过的树）：按行归一化后取非空行
_WS = re.compile(r"\s+")


def ui_fingerprint(ui_tree: str | None) -> str:
    """把 UI 树压成一个稳定的结构指纹；空输入返回空串（表示「没有基线」）。"""
    if not ui_tree:
        return ""

    text = ui_tree.strip()
    if not text:
        return ""

    if "<" in text:
        signature = _signature_from_xml(text)
    else:
        signature = _signature_from_lines(text)

    if not signature:
        return ""
    return hashlib.sha1("\n".join(signature).encode("utf-8")).hexdigest()


def _signature_from_xml(xml: str) -> list[str]:
    """逐节点抽取结构属性。节点顺序也是结构的一部分，所以要保序。"""
    signature: list[str] = []
    for chunk in re.finditer(r"<node\b[^>]*>", xml, re.IGNORECASE):
        attrs = [pattern.search(chunk.group(0)) for pattern in _ATTR_PATTERNS]
        parts = [attr.group(1).strip() if attr else "" for attr in attrs]
        # 全空节点（纯布局容器）不进指纹，否则布局微调就会误判页面变化
        if any(parts):
            signature.append("|".join(parts))
    return signature


def _signature_from_lines(text: str) -> list[str]:
    return [_WS.sub(" ", line).strip() for line in text.splitlines() if line.strip()]
