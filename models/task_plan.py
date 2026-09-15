"""一次规划的完整结果：目标 + 约束 + 成功条件 + 步骤（v4.3 §1 / v4.4 §五）。

v4.3 的判断是「Think 阶段太薄，是 Reactive Agent 而不是 Deliberative Agent」，
建议把 `generate_plan()` 的返回值从 `list[str]` 升级成：

    TaskPlan { goal, steps, constraints, success_condition }

v4.2 已经补了每个步骤的 `expected_state`（"这一步做完页面该是什么样"）。
这一层补的是**任务级**的三样——它们解决的是「长任务会漂移」：

    goal               这一趟到底要干什么（模型自己的复述，用来发现跑偏）
    constraints        不许做什么（「不要点广告」「只用微信不要跳浏览器」）
    success_condition  什么算完成——**给人和审计看的**，不是裁决依据

**为什么它不进裁定**（与 `expected_state` 同一条纪律）：裁决仍然只认独立证据
（`agent/goal_verifier` 用页面结构 / 目标元素 / 导航变化裁定）。模型的自我复述
一旦成为证据，「模型说它成了」就等于「它成了」——V2.2 §四 整轮在拆的就是这个。
所以这一层的产物是：进决策 prompt（让模型每一步都看得到目标与约束）、
进 `/tasks/{id}` 与审计（让人看得见它当时以为在干什么）。

`TaskPlan` 是**传输形状**，不是持久化形状：落盘的仍然是 `Task.plan`
（`list[TaskStep]`）+ 任务上的三个标量字段。理由：步骤是**被反复推进的状态机**，
持久化形状不该在外面再包一层（那会出现「两个 plan」的经典分叉）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .task_step import TaskStep, build_steps


@dataclass(frozen=True)
class TaskPlan:
    """一份计划：任务级三要素 + 步骤序列。"""

    goal: str = ""
    constraints: tuple[str, ...] = ()
    success_condition: str = ""
    steps: tuple[TaskStep, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.steps and not self.goal and not self.success_condition

    def context_lines(self) -> list[str]:
        """渲染成给模型（与给人）看的三行。

        空的不渲染——「约束：无」这种行只会占 prompt 的篇幅，
        而模型对「没写约束」和「约束是空」的理解是一样的。
        """
        lines: list[str] = []
        if self.goal:
            lines.append(f"目标：{self.goal}")
        if self.constraints:
            lines.append("约束：" + "；".join(self.constraints))
        if self.success_condition:
            lines.append(f"完成条件：{self.success_condition}")
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "constraints": list(self.constraints),
            "success_condition": self.success_condition,
            "steps": [step.model_dump(mode="json") for step in self.steps],
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "TaskPlan":
        """把「模型/调用方给的东西」归一成 `TaskPlan`。

        收四种形状，因为这条链上真的会出现四种：
          - `TaskPlan`    —— 已经归一过（`planner.generate_plan` 的返回值）
          - `list`        —— 纯步骤列表（**历史形状**，几十个用例与脚本都在用）
          - `dict`        —— 模型按新 prompt 返回的 `{goal, constraints, success_condition, steps|plan}`
          - `None` / 别的 —— 空计划（生成失败时上游就是这么传的）

        步骤的归一（字符串 / `{"goal", "expected_state"}`）统一交给 `build_steps`，
        这里不重复一套解析——两份解析迟早会分叉。
        """
        if isinstance(payload, cls):
            return payload
        if payload is None:
            return cls()
        if isinstance(payload, (list, tuple)):
            return cls(steps=tuple(build_steps(list(payload))))
        if isinstance(payload, dict):
            raw_steps = payload.get("steps")
            if raw_steps is None:
                raw_steps = payload.get("plan") or []
            if not isinstance(raw_steps, (list, tuple)):
                raw_steps = []
            return cls(
                goal=str(payload.get("goal") or "").strip(),
                constraints=_clean_list(payload.get("constraints")),
                success_condition=str(
                    payload.get("success_condition") or payload.get("success") or ""
                ).strip(),
                steps=tuple(build_steps(list(raw_steps))),
            )
        return cls()


def _clean_list(value: Any) -> tuple[str, ...]:
    """约束列表：字符串当一条，可迭代的逐条去空，其余忽略。"""
    if value is None:
        return ()
    if isinstance(value, str):
        items: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return ()
    return tuple(item for item in (str(raw).strip() for raw in items) if item)
