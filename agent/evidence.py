"""多级证据（V2.2 §六）：判断「这一步到底发生了什么」的本地证据链。

审核指出原实现的问题很具体：

> 现在只比较「所有可点击控件的 label 集合」，然后 `same` / `changed` 二选一。
> 这是一个不错的廉价信号，但不能叫「UI 状态是否变化」。

三个反例都很真实——toast 弹出后 label 集合没变（漏判成功）、页面内部数据变了
label 集合也没变（漏判变化）、无关 Dialog 弹出却被判成 changed（误判成功）。

所以这里把「页面变了吗」拆成**四条互相独立的证据**，并按可靠度排序：

    L2 导航层    package / activity 变了        —— 最硬，几乎不可能误判
    L3 结构层    UI 结构指纹变了（class/text/content-desc/resource-id）
    L4 目标层    被操作的那个元素消失了 / 文本变了 —— 最贴近「这个动作有没有用」
    L5 语义层    VLM 判断（贵、慢、会编理由，只做最后一层）

`strongest_layer` 给出本次判定实际用到的**最强**证据来自哪一层，
落进事件流后，事后能回答「当时凭什么叫它成功」。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from models.action import Action
from models.state import Observation
from vision import parser
from vision.fingerprint import ui_fingerprint
from vision.target import TargetState, target_state


class EvidenceLevel(str, Enum):
    """证据来自哪一层。名字里的 L 与审核文档的编号一致，便于对照。"""

    L1_DEVICE = "l1_device"
    """设备层：ADB 命令送达并执行成功（executor 返回 ok）。"""

    L2_NAVIGATION = "l2_navigation"
    """导航层：package / activity 变化。"""

    L3_STRUCTURE = "l3_structure"
    """结构层：UI 结构指纹变化。"""

    L4_TARGET = "l4_target"
    """目标层：被操作元素的自身状态变化。"""

    L5_VLM = "l5_vlm"
    """语义层：VLM 对前后截图的判定。"""

    L6_GOAL = "l6_goal"
    """目标层（任务级）：GoalVerifier 对「任务是否完成」的独立裁定。"""

    NONE = "none"
    """没有可用证据。"""

    @property
    def is_independent(self) -> bool:
        """是不是**不依赖模型**的独立证据（V2.2 §四：完成判定需要它）。"""
        return self in (
            EvidenceLevel.L1_DEVICE,
            EvidenceLevel.L2_NAVIGATION,
            EvidenceLevel.L3_STRUCTURE,
            EvidenceLevel.L4_TARGET,
            EvidenceLevel.L6_GOAL,
        )


@dataclass(frozen=True)
class ScreenDelta:
    """动作前后「屏幕发生了什么」的结论，以及这个结论来自哪一层证据。"""

    navigation_changed: bool = False
    structural_changed: bool = False
    label_changed: bool = False
    target: TargetState = TargetState.UNKNOWN
    fingerprint_before: str = ""
    fingerprint_after: str = ""
    known: bool = True
    """两侧 UI 树都可解析、可比对。False 表示「无从比较」，不是「没变化」。"""

    @property
    def changed(self) -> bool:
        """页面**确实**变了。

        与「label 集合变了」的区别：指纹包含 class / content-desc / resource-id，
        所以「提交 → 已提交」这类同集合不同内容的场景也能识别；
        而结构完全一致时不会被无关的 layout 抖动误判。
        """
        return self.navigation_changed or self.structural_changed

    @property
    def strongest_layer(self) -> EvidenceLevel:
        if not self.known:
            return EvidenceLevel.NONE
        if self.navigation_changed:
            return EvidenceLevel.L2_NAVIGATION
        if self.structural_changed:
            return EvidenceLevel.L3_STRUCTURE
        if self.target.is_positive_evidence:
            return EvidenceLevel.L4_TARGET
        if self.fingerprint_before and self.fingerprint_after:
            return EvidenceLevel.L1_DEVICE  # 结构可比且一致：只剩设备层那一句 ok
        return EvidenceLevel.NONE

    def describe(self) -> str:
        parts: list[str] = []
        if not self.known:
            parts.append("UI 树不可比")
        else:
            parts.append("页面结构已变化" if self.structural_changed else "页面结构无变化")
        if self.navigation_changed:
            parts.append("已发生页面跳转")
        if self.label_changed:
            parts.append("可点击元素集合有变化")
        if self.target is not TargetState.UNKNOWN:
            parts.append(f"目标元素{_TARGET_TEXT[self.target]}")
        return "；".join(parts)

    def to_dict(self) -> dict:
        return {
            "navigation_changed": self.navigation_changed,
            "structural_changed": self.structural_changed,
            "label_changed": self.label_changed,
            "target": self.target.value,
            "layer": self.strongest_layer.value,
            "describe": self.describe(),
        }


_TARGET_TEXT = {
    TargetState.GONE: "已消失",
    TargetState.CHANGED: "文本/描述已变化",
    TargetState.UNCHANGED: "原样还在",
    TargetState.ABSENT_BEFORE: "动作前就找不到",
    TargetState.UNKNOWN: "无法定位",
}


def screen_delta(pre: Observation, post: Observation, action: Action | None = None) -> ScreenDelta:
    """比对前后两次观察，产出多级证据。"""
    fingerprint_before = ui_fingerprint(pre.ui_tree)
    fingerprint_after = ui_fingerprint(post.ui_tree)
    known = bool(fingerprint_before and fingerprint_after)

    navigation_changed = bool(
        (pre.package and post.package and pre.package != post.package)
        or (pre.activity and post.activity and pre.activity != post.activity)
    )

    state = TargetState.UNKNOWN
    if action is not None:
        state = target_state(action, pre.ui_tree, post.ui_tree, post.screen_size or pre.screen_size)

    return ScreenDelta(
        navigation_changed=navigation_changed,
        structural_changed=known and fingerprint_before != fingerprint_after,
        label_changed=_label_set_changed(pre.ui_tree, post.ui_tree),
        target=state,
        fingerprint_before=fingerprint_before,
        fingerprint_after=fingerprint_after,
        known=known,
    )


def _label_set_changed(before_tree: str | None, after_tree: str | None) -> bool:
    """「可点击元素 label 集合」这个旧信号仍然保留——它便宜，且能捕捉指纹抓不到的变化。

    但它**不再**被当作「页面变没变」的全部依据，只作为一条辅助证据。
    """
    before = _clickable_labels(before_tree)
    after = _clickable_labels(after_tree)
    if before is None or after is None:
        return False
    return before != after


def _clickable_labels(ui_tree: str | None) -> set[str] | None:
    if not ui_tree:
        return None
    try:
        root = parser.parse(ui_tree)
    except Exception:  # noqa: BLE001 - 树坏了就当作「无法比较」，不要影响判定
        return None
    labels: set[str] = set()
    for node in parser.find_clickable(root):
        label = parser.node_label(node)
        if label:
            labels.add(label)
    return labels
