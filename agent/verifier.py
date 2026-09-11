"""执行后重新截图，由 VLM 判断页面是否进入预期状态（§4.3）。"""
from __future__ import annotations

from models.action import Action, ActionType
from models.state import Observation, StepStatus
from vision import vlm


def verify_action(
    instruction: str,
    pre_observation: Observation,
    action: Action,
    post_observation: Observation,
) -> tuple[StepStatus, str]:
    """比较执行前后页面状态，返回 (StepStatus, message)。"""
    if action.type == ActionType.DONE:
        return StepStatus.DONE, action.reason or "任务完成"

    if not post_observation.result.get("ok"):
        return StepStatus.ERROR, post_observation.result.get("error", "执行失败")

    try:
        status = vlm.verify_transition(
            pre_observation.screenshot_path,
            post_observation.screenshot_path,
            instruction,
            action.model_dump(),
        )
    except Exception as exc:
        # VLM 验证失败时退化为执行结果校验，避免阻塞任务
        return StepStatus.OK, f"执行成功但 VLM 验证未通过: {exc}"

    if status == "done":
        return StepStatus.DONE, "VLM 判定任务已完成"
    if status == "error":
        return StepStatus.ERROR, "VLM 判定页面未进入预期状态"
    return StepStatus.OK, "VLM 判定执行成功"
