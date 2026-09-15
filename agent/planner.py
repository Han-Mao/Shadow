"""任务规划与 Re-plan（V2 §十六：Re-plan 不再拼字符串，而是结构化上下文）。不产出坐标。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from models.action import Action, Decision
from models.task_plan import TaskPlan
from models.task_step import TaskStep
from vision import vlm

# 执行方式失败时，提示模型可以换哪些路子——避免它把同一个动作原样重发一遍
DEFAULT_ALTERNATIVES = (
    "改用 UI 树里的元素文本定位",
    "改用 content-desc / resource-id 定位",
    "先返回上一页再重新进入",
    "滚动页面后再试",
    "改用文本输入替代逐级点击",
    "等待页面加载完成后重试",
)


def format_plan(plan: list[TaskStep] | None) -> list[str]:
    """把带状态的步骤渲染进 prompt。

    这是 V2 计划从 `list[str]` 升级成 `list[TaskStep]` 的实际收益：
    模型能看到「哪几步已经做完」，而不是每次都面对一份一模一样的静态清单。

    v4.2 §三 P1 起顺带把模型的 `expected_state` 也带进去：下一步决策时能读到
    「上一步本来预期看到什么」，而不是只看到一个步骤名。
    """
    if not plan:
        return []
    return [
        f"[{step.status.value}] {step.goal}"
        + (f"（预期：{step.expected_state}）" if step.expected_state else "")
        for step in plan
    ]


class ReplanContext(BaseModel):
    """结构化 Re-plan 输入。

    关键点：让模型明白「刚才是**这种执行方式**失败了」，而不是「任务失败了」。
    少了这层区分，模型往往会原样重发同一个动作。
    """

    task: str
    current_step_goal: str = ""
    previous_action: Action | None = None
    failure_reason: str = ""
    failed_strategies: list[str] = Field(default_factory=list)
    available_alternatives: list[str] = Field(default_factory=lambda: list(DEFAULT_ALTERNATIVES))

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "current_step": self.current_step_goal or "（尚未确定）",
            "previous_action": (
                self.previous_action.model_dump(mode="json") if self.previous_action else None
            ),
            "failure_reason": self.failure_reason,
            "failed_strategies": list(self.failed_strategies),
            "available_alternatives": list(self.available_alternatives),
        }


def generate_plan(
    instruction: str,
    screenshot_path: str | Path,
    ui_tree: str | None = None,
) -> TaskPlan:
    """根据首屏生成一份计划（`v4.3 §1`：goal + constraints + success_condition + steps）。

    返回 `TaskPlan`；调用方 `Task.set_plan` 会把它拆进
    `task.plan` / `plan_goal` / `plan_constraints` / `success_condition`。
    形状归一化在 `vision.vlm.generate_plan` 与 `TaskPlan.from_payload` 里做，
    这一层只负责透传。
    """
    return vlm.generate_plan(instruction, str(screenshot_path), ui_tree)


def plan_next_action(
    instruction: str,
    screenshot_path: str | Path,
    ui_tree: str | None = None,
    history: list[dict[str, Any]] | None = None,
    plan: list[TaskStep] | None = None,
    current_step: TaskStep | None = None,
    plan_context: str = "",
) -> Decision:
    """路线 A + 路线 B：由 VLM 根据截图、UI 树与当前步骤决定下一步做什么。

    `plan_context` 是任务级的目标 / 约束 / 完成条件（v4.3 §1 的 `TaskPlan` 三要素）
    ——步骤列表只回答「走到哪了」，这三样回答「为什么走、不许怎么走」。
    """
    return vlm.decide_next_action(
        instruction,
        str(screenshot_path),
        ui_tree,
        history,
        format_plan(plan),
        current_step.goal if current_step else "",
        plan_context,
    )


def format_plan_context(task) -> str:
    """把一个 Task 的任务级上下文渲染成一段可放进 prompt 的文字（空则返回空串）。"""
    lines = task.plan_context_lines() if hasattr(task, "plan_context_lines") else []
    return "\n".join(lines)


def replan(
    context: ReplanContext,
    screenshot_path: str | Path,
    ui_tree: str | None,
    history: list[dict[str, Any]] | None = None,
) -> Decision:
    """失败时基于结构化上下文重新决策。"""
    return vlm.replan_action(
        context.to_prompt_dict(),
        str(screenshot_path),
        ui_tree,
        history,
    )
