"""多级验证（V2 §十五）：Device → UI Tree → VLM，逐级加码。

只用 VLM 判「成功没成功」有两个问题：它贵、慢，而且会为一次根本没生效的点击
编出合理化的理由。先在廉价且确定的信号上过一遍，VLM 只负责最后那层语义判断。
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from models.action import Action, ActionType
from models.state import Observation, StepOutcome
from vision import parser, vlm

logger = logging.getLogger(__name__)

# 这些动作理应让页面或控件状态发生变化；UI 树完全没动就是可疑信号
MUTATING_ACTION_TYPES = frozenset(
    {ActionType.TAP, ActionType.LONG_PRESS, ActionType.TYPE, ActionType.SWIPE, ActionType.LAUNCH}
)


class Verification(BaseModel):
    """一次执行的验证结论。"""

    outcome: StepOutcome = StepOutcome.OK
    done: bool = False
    should_retry: bool = False
    should_replan: bool = False
    layer: str = ""
    message: str = ""


def _clickable_labels(ui_tree: str | None) -> set[str] | None:
    if not ui_tree:
        return None
    try:
        root = parser.parse(ui_tree)
    except Exception:  # noqa: BLE001 - 树坏了就当作「无法比较」，不要影响判定
        return None
    labels: set[str] = set()
    for node in parser.find_clickable(root):
        label = (node.text or node.content_desc or node.resource_id).strip()
        if label:
            labels.add(label)
    return labels


def ui_tree_signal(pre: Observation, post: Observation) -> str:
    """返回 `changed` / `same` / `unknown`。"""
    before = _clickable_labels(pre.ui_tree)
    after = _clickable_labels(post.ui_tree)
    if before is None or after is None:
        return "unknown"
    return "same" if before == after else "changed"


def verify_action(
    instruction: str,
    pre_observation: Observation,
    action: Action,
    post_observation: Observation,
    result: dict | None = None,
) -> Verification:
    """逐级验证一次执行。"""
    execution = result if result is not None else (post_observation.result or {})

    # 规划器主动宣告完成，不需要再验证
    if action.type is ActionType.DONE:
        return Verification(
            outcome=StepOutcome.DONE,
            done=True,
            layer="planner",
            message=action.reason or "任务完成",
        )

    # ---- L1 设备层：动作到底发出去没有 ----
    if not execution.get("ok"):
        return Verification(
            outcome=StepOutcome.ERROR,
            should_retry=True,
            should_replan=True,
            layer="device",
            message=execution.get("error") or "设备执行失败",
        )

    # ---- L2 UI 树层：控件状态有没有真的变化 ----
    signal = ui_tree_signal(pre_observation, post_observation)

    # ---- L3 VLM 层：语义上是否达成目标 ----
    try:
        verdict = str(
            vlm.verify_transition(
                pre_observation.screenshot_path,
                post_observation.screenshot_path,
                instruction,
                action.model_dump(),
            )
        ).lower()
    except Exception as exc:  # noqa: BLE001 - VLM 不可用时降级，不能阻塞任务
        logger.warning("VLM 验证不可用: %s", exc)
        if signal == "same" and action.type in MUTATING_ACTION_TYPES:
            return Verification(
                outcome=StepOutcome.OK,
                should_retry=True,
                layer="device+ui_tree",
                message=f"VLM 不可用（{exc}）；且 UI 树与执行前完全一致，疑似无效操作",
            )
        return Verification(
            outcome=StepOutcome.OK,
            layer="device+ui_tree",
            message=f"执行成功（VLM 验证不可用：{exc}）",
        )

    if verdict == "done":
        return Verification(
            outcome=StepOutcome.DONE, done=True, layer="vlm", message="VLM 判定任务已完成"
        )
    if verdict == "error":
        return Verification(
            outcome=StepOutcome.ERROR,
            should_replan=True,
            layer="vlm",
            message="VLM 判定页面未进入预期状态",
        )

    if signal == "same" and action.type in MUTATING_ACTION_TYPES:
        return Verification(
            outcome=StepOutcome.OK,
            should_retry=True,
            layer="vlm+ui_tree",
            message="VLM 判定成功，但 UI 树与执行前完全一致，疑似无效操作",
        )

    return Verification(outcome=StepOutcome.OK, layer="vlm", message="VLM 判定执行成功")
