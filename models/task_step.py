"""任务步骤（V2 §三）：计划不再是字符串列表，而是可追踪的执行状态机。

V2.1 §二十 把「计划」与「执行记录」拆开：
- 本模块的 `TaskStep` 描述**计划**（目标 / 依赖 / 重试上限 / 状态 / 尝试历史）
- `StepAttempt`（`models/step_attempt.py`）描述**一次尝试**的经过

`retry_count` / `last_action` / `last_error` 保留原名，但改成从 `attempts` 派生的
只读属性——调用方不用改，而执行历史不再被后来的尝试覆盖掉。
"""
from __future__ import annotations

import uuid
from enum import Enum

from pydantic import BaseModel, Field

from .action import Action, ActionEffectStatus
from .retry import DEFAULT_POLICY, ErrorClass
from .step_attempt import AttemptOutcome, StepAttempt


class StepStatus(str, Enum):
    """计划步骤的生命周期状态。

    注意与 `models.state.StepOutcome` 的区别：StepOutcome 描述「一次观察/执行的结果」，
    StepStatus 描述「计划里某个步骤走到哪了」。两者语义不同，不要混用。
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


def new_step_id(index: int) -> str:
    return f"s{index}"


def _normalize_goal(item: "str | dict") -> tuple[str, str]:
    """把「计划里的一项」摊成 `(目标, 预期状态)`。

    两种写法都要收：
      - 纯字符串（模型最常给的形状，也是所有历史用例的形状）→ 预期状态为空；
      - 对象 `{"goal": …, "expected_state": …}`（v4.2 §三 P1 起模型可以给）。

    `expected_state` 只是一个**说明性字段**，不参与完成裁定——裁定仍然由
    `agent/goal_verifier` 用**独立证据**（页面结构 / 目标元素 / 导航变化）作出。
    把模型自己写的「预期」当证据，等于让「模型说它成了」变成「它成了」，
    那正是这个仓库花了 V2.2 一整轮拆掉的东西。
    """
    if isinstance(item, dict):
        goal = str(item.get("goal") or item.get("description") or "").strip()
        expected = str(item.get("expected_state") or item.get("expect") or "").strip()
        return goal, expected
    return str(item).strip(), ""


def build_steps(
    goals: list, *, max_retries: int | None = None
) -> list["TaskStep"]:
    """把语义级目标列表转成带状态与依赖的步骤序列。

    默认串行依赖（s2 依赖 s1）：手机 GUI 任务的步骤几乎都是顺序的，
    并行依赖关系留给后续按需声明。

    ``max_retries`` 不传时取 `DEFAULT_POLICY.step_max_retries`——步骤级重试上限
    与 Runtime 共用同一份策略，不再各处硬编码（V2.1 §十二）。

    列表元素可以是字符串，也可以是 `{"goal": …, "expected_state": …}`
    （见 `_normalize_goal`）。**空目标的项会被跳过**：给不出目标的「步骤」
    进了计划只会变成一条永远卡住的 pending。
    """
    retries = DEFAULT_POLICY.step_max_retries if max_retries is None else max_retries
    steps: list[TaskStep] = []
    for raw in goals:
        goal, expected = _normalize_goal(raw)
        if not goal:
            continue
        index = len(steps) + 1
        steps.append(
            TaskStep(
                id=new_step_id(index),
                goal=goal,
                expected_state=expected,
                depends_on=[new_step_id(index - 1)] if index > 1 else [],
                max_retries=retries,
            )
        )
    return steps


class TaskStep(BaseModel):
    """计划里的一个步骤。执行记录全部在 `attempts` 里，这里不再存副本。"""

    id: str = Field(default_factory=lambda: f"s{uuid.uuid4().hex[:4]}")
    goal: str

    expected_state: str = ""
    """**这一步做完之后，页面应该是什么样**（v4.2 §三 P1，模型可选填）。

    补的是「计划只有步骤名、没有成功标准」这个缺口。它与 `depends_on` 一起让计划
    从「一串待办」变成「一张有依赖、有验收口径的清单」，也让人在 `/tasks/{id}`
    里一眼看出模型当时以为自己在干什么。

    **刻意不参与裁定**：`agent/goal_verifier` 仍然只认独立证据（页面结构变了没、
    目标元素还在不在、有没有成功标志）。把模型写的预期当证据，
    会让「模型说它成了」等价于「它成了」——V2.2 §四 整轮就是在拆这个。
    真要让它参与，前提是它能被验成机器可判的谓词（`agent/goal_oracle` 那套），
    那是独立一轮的工作量。
    """

    status: StepStatus = StepStatus.PENDING

    depends_on: list[str] = Field(default_factory=list)

    max_retries: int = DEFAULT_POLICY.step_max_retries
    """允许重试几次。属于**计划**（是策略配置），不是执行记录。"""

    attempts: list[StepAttempt] = Field(default_factory=list)
    """每一次尝试都留痕，按时间顺序。历史不会被后来者覆盖。"""

    # ---- 从 attempts 派生的只读视图（保持旧调用方的写法）----

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def retry_count(self) -> int:
        """失败过的次数。"""
        return sum(1 for attempt in self.attempts if attempt.outcome is AttemptOutcome.ERROR)

    @property
    def retryable(self) -> bool:
        return self.retry_count < self.max_retries

    @property
    def last_attempt(self) -> StepAttempt | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def last_action(self) -> Action | None:
        attempt = self.last_attempt
        return attempt.action if attempt else None

    @property
    def last_error(self) -> str | None:
        """只看**最后一次**尝试的错误 —— 成功之后就该是 None，不翻旧账。"""
        attempt = self.last_attempt
        return attempt.error if attempt else None

    # ---- 写入 ----

    def mark(self, status: StepStatus) -> None:
        self.status = status

    def next_attempt_number(self) -> int:
        return len(self.attempts) + 1

    def record_attempt(
        self,
        *,
        action: Action | None = None,
        outcome: AttemptOutcome = AttemptOutcome.UNKNOWN,
        effect: ActionEffectStatus = ActionEffectStatus.NOT_STARTED,
        layer: str = "",
        error: str | None = None,
        error_class: ErrorClass | None = None,
    ) -> StepAttempt:
        """记一次尝试。这是唯一的写入口，`record_action` / `record_failure` 都走它。"""
        attempt = StepAttempt(
            step_id=self.id,
            number=self.next_attempt_number(),
            action=action,
            outcome=outcome,
            effect=effect,
            layer=layer,
            error=error,
            error_class=error_class,
        )
        self.attempts.append(attempt)
        return attempt

    def record_action(
        self,
        action: Action,
        *,
        outcome: AttemptOutcome = AttemptOutcome.OK,
        effect: ActionEffectStatus = ActionEffectStatus.NOT_STARTED,
        layer: str = "",
    ) -> StepAttempt:
        """动作执行成功。成功之后 `last_error` 自然是 None，不需要额外清理。"""
        return self.record_attempt(
            action=action, outcome=outcome, effect=effect, layer=layer
        )

    def record_failure(
        self,
        error: str,
        *,
        action: Action | None = None,
        error_class: ErrorClass | None = None,
        effect: ActionEffectStatus = ActionEffectStatus.VERIFIED_FAILED,
        layer: str = "",
    ) -> StepAttempt:
        """动作失败。带上动作与错误分类，事后才查得出「试了什么、为什么不行」。"""
        return self.record_attempt(
            action=action,
            outcome=AttemptOutcome.ERROR,
            effect=effect,
            layer=layer,
            error=error,
            error_class=error_class,
        )
