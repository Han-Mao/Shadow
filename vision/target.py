"""把「动作目标」还原成 UI 树里的真实节点。

这是 V2.2 补上的一个关键缺口。在此之前，系统对动作目标的全部认知就是
`Action.target` 里那串文本或坐标——于是：

    动作说：tap (540, 1613)          → 看不出要干什么
    动作说：tap "点击红色按钮"        → 看不出那个按钮其实是「立即购买」

而这两件事对**风险判定**（要不要拦下来问人）和**效果验证**（点完那个元素还在不在）
都是决定性的。两处需求相同，所以解析逻辑只有这一份。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from models.action import Action, Point
from vision import parser


class TargetState(str, Enum):
    """目标元素在动作前后的存在状态（V2.2 §六 L4）。

    比「UI 树变没变」精确得多：一个「提交」按钮点完变成「已提交」，
    label 集合可能完全一致，但目标元素的状态确实变了。
    """

    UNKNOWN = "unknown"
    """动作没给出可定位的目标（LAUNCH / WAIT / HOME 等），或 UI 树不可用。"""

    ABSENT_BEFORE = "absent_before"
    """动作前就找不到这个元素，无从比较。"""

    GONE = "gone"
    """动作前在、动作后消失——跳转或提交成功最强的本地证据。"""

    CHANGED = "changed"
    """同一个元素还在，但文本/描述变了（「提交」→「已提交」）。"""

    UNCHANGED = "unchanged"
    """元素原样还在，说明这次点击很可能没有生效。"""

    @property
    def is_positive_evidence(self) -> bool:
        """是否构成「动作确实产生了效果」的正向证据。"""
        return self in (TargetState.GONE, TargetState.CHANGED)


@dataclass(frozen=True)
class ResolvedTarget:
    """动作目标在某一屏上的落地结果。"""

    node: parser.UiNode | None
    key: str = ""
    """节点身份键（class|text|content-desc|resource-id），不含 bounds。"""

    label: str = ""

    @property
    def found(self) -> bool:
        return self.node is not None

    @property
    def resource_id(self) -> str:
        return self.node.resource_id if self.node is not None else ""


def to_pixel(point: Point, size: tuple[int, int] | None) -> tuple[float, float]:
    """归一化坐标（0~1）按屏幕尺寸换算；像素坐标原样返回。

    口径必须与 `vision.grounding._point_to_pixel` 一致——否则风险判定看的位置
    和真正点下去的位置不是一个地方，会把 A 元素的风险算到 B 头上。
    """
    x, y = point.x, point.y
    if size:
        width, height = size
        if 0 < x < 1:
            x *= width
        if 0 < y < 1:
            y *= height
    return x, y


def resolve_target(
    action: Action,
    ui_tree: str | None,
    screen_size: tuple[int, int] | None = None,
) -> ResolvedTarget:
    """把动作目标还原成节点。拿不到不是错误——很多动作本来就没有目标元素。"""
    if not ui_tree:
        return ResolvedTarget(node=None)

    try:
        root = parser.parse(ui_tree)
    except Exception:  # noqa: BLE001 - 树坏了退化为「无目标信息」，不能让验证/门禁中断
        return ResolvedTarget(node=None)

    target = action.target
    if isinstance(target, Point):
        x, y = to_pixel(target, screen_size)
        node = parser.node_at(root, x, y)
    elif isinstance(target, str):
        node = parser.match_by_text(parser.find_clickable(root), target)
    else:
        node = None

    if node is None:
        return ResolvedTarget(node=None)
    return ResolvedTarget(
        node=node,
        key=parser.node_identity(node),
        label=parser.node_label(node),
    )


def target_state(action: Action, pre_tree: str | None, post_tree: str | None,
                 screen_size: tuple[int, int] | None = None) -> TargetState:
    """比较目标元素在动作前后的状态。

    判据是「**动作前那个元素**在后置树里还在不在、还是不是原来那个样子」——
    所以要在后置树里找的是 `before` 的身份键，而不是用同一坐标再查一次
    （再查一次必然查得到，那会让「提交 → 已提交」被误判成「没变化」）。
    """
    before = resolve_target(action, pre_tree, screen_size)
    if not before.found:
        return TargetState.UNKNOWN

    # 按身份键在整个后置树里找：元素可能因为点击从「可点击」变成「不可点击」，
    # 只用可点击集合去找会误判成消失
    after_nodes = _all_nodes(post_tree)
    if not after_nodes:
        return TargetState.UNKNOWN

    if before.key in after_nodes:
        return TargetState.UNCHANGED

    # 按 resource-id 兜底认亲：文案从「提交」变成「已提交」时身份键会变，
    # 但 resource-id 通常不变，能认出「还是那个元素，只是内容变了」
    if before.resource_id and any(
        node.resource_id == before.resource_id for node in after_nodes.values()
    ):
        return TargetState.CHANGED

    return TargetState.GONE


def _all_nodes(ui_tree: str | None) -> dict[str, parser.UiNode]:
    if not ui_tree:
        return {}
    try:
        root = parser.parse(ui_tree)
    except Exception:  # noqa: BLE001
        return {}
    return {parser.node_identity(node): node for node in parser.iter_nodes(root)}
