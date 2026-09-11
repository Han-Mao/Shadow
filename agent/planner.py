"""任务 → 步骤计划，失败时 Re-plan。不产出坐标。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from models.action import Action
from vision import vlm


def generate_plan(
    instruction: str,
    screenshot_path: str | Path,
    ui_tree: str | None = None,
) -> list[str]:
    """根据首屏生成语义级步骤计划（§4.2）。"""
    return vlm.generate_plan(instruction, str(screenshot_path), ui_tree)


def plan_next_action(
    instruction: str,
    screenshot_path: str | Path,
    ui_tree: str | None = None,
    history: list[dict[str, Any]] | None = None,
    plan: list[str] | None = None,
) -> Action:
    """纯 VLM 路线 A + 路线 B：由 VLM 根据截图与 UI 树决定下一步 Action。"""
    return vlm.decide_next_action(
        instruction,
        str(screenshot_path),
        ui_tree,
        history,
        plan,
    )


def replan(
    instruction: str,
    screenshot_path: str | Path,
    ui_tree: str | None,
    history: list[dict[str, Any]],
    last_error: str,
) -> Action:
    """失败时基于错误信息重新决策。"""
    augmented_instruction = (
        f"{instruction}\n（上一步执行失败：{last_error}，请尝试替代方案）"
    )
    return plan_next_action(augmented_instruction, screenshot_path, ui_tree, history)
