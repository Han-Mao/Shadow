"""Checkpoint（V2 §八）：保存「恢复这个任务所需的最小状态」，而不是全部历史。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .action import Action, ActionEffectStatus
from .state import Observation
from .task_step import StepStatus
from vision.fingerprint import ui_fingerprint

# UI 树快照只用于「页面是否还是当初那一屏」的比对，没必要留全量
UI_SNAPSHOT_LIMIT = 20_000
# 恢复时需要重放的最近轨迹条数
HISTORY_TAIL_SIZE = 5


def new_checkpoint_id(task_id: str, step: int) -> str:
    """形如 `20260912_0952ab_s003`，人眼可读且带随机后缀防撞。"""
    return f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}_s{step:03d}"


class Checkpoint(BaseModel):
    id: str
    task_id: str

    # 关联的 Task 版本（V2.1 §十八）。恢复时若与当前 task.version 不一致，
    # 说明任务目标已变（SUPER_TASK / re-plan 等），旧恢复点必须作废，不能盲续。
    task_version: int = 1

    # 关联的计划版本（V2.2 §十一）。与 task_version 分开放：
    #   task_version 变了 → 旧恢复点**绝对不能**用
    #   plan_version 变了 → 目标没变、只是计划被改过（插入子任务 / Re-plan），
    #                       恢复点仍然可用，但要能看出「它对应的是第几版计划」
    # 只做记录与展示，不做门控——门控会误伤「目标没变、页面也没变」的安全续跑。
    plan_version: int = 0

    current_step: int = 0
    step_states: dict[str, StepStatus] = Field(default_factory=dict)

    # 恢复时用来判断「当前页面是否还符合当时的状态」
    screenshot_path: str | None = None
    package: str = ""
    activity: str = ""
    ui_snapshot: str | None = None

    # 关于「环境状态」（电池 / 网络）的**有理由延期**（v4.4 §2 P1）：
    #
    # 审核建议恢复点带上 `device_state{battery, network, foreground_app, login_state}`。
    # 其中 `foreground_app` 已经被 `package`/`activity` + `same_screen_as()` 覆盖——
    # 那正是「恢复时判断当前是不是微信聊天页」用的东西。还缺的电池/网络要
    # `DeviceController` 新增接口，横跨 ADB + Android + 桥 + 静态契约测试四方，
    # 而收益只是「恢复时多一条环境信息」。
    #
    # 触发条件：真机任务频繁因「低电量 / 网络切换」而非「页面不对」失败时，
    # 再给 `DeviceController` 加 `battery_level()` / `network_type()` 并在
    # `Checkpoint.capture` 里落一条 `device_state`。在那之前不加——空接口是负债。

    history_tail: list[Observation] = Field(default_factory=list)

    # 上次动作的执行效果判定（V2.1 §五）。DISPATCHED 时进程崩溃 → 恢复即 EFFECT_UNKNOWN，
    # 绝不能默认重试，必须重新观察对账，否则「提交订单」这类动作可能被重复执行。
    action_effect: ActionEffectStatus = ActionEffectStatus.NOT_STARTED
    last_action: Action | None = None

    # 本次动作的唯一尝试 id（V2.1 §二十一）：同一步骤的每次重试各有自己的 attempt，
    # 对账时能精确知道「恢复的是哪一次尝试」，而不是笼统的「第几步」。
    action_attempt_id: str | None = None

    # L2 结构指纹（V2.1 §四）：存指纹而不是整棵 UI 树——
    # 省空间，而且恢复时可以直接比对，不必再把几万字符的 XML 解析一遍。
    screen_fingerprint: str = ""

    # 语义状态摘要（V2.1 §二十一）：当前页大致在做什么，恢复时快速重建上下文。
    semantic_state: str = ""

    # 已消耗预算快照（V2.1 §二十一）：恢复后才知道还剩多少动作 / 观察 / 模型调用。
    budget_used: dict[str, int] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    def capture(
        cls,
        *,
        task_id: str,
        step: int,
        step_states: dict[str, StepStatus],
        observation: Observation | None,
        history_tail: list[Observation] | None = None,
        task_version: int = 1,
        plan_version: int = 0,
        action_effect: ActionEffectStatus = ActionEffectStatus.NOT_STARTED,
        last_action: Action | None = None,
        action_attempt_id: str | None = None,
        semantic_state: str = "",
        budget_used: dict[str, int] | None = None,
    ) -> "Checkpoint":
        """从一次观察中提取恢复所需的最小状态。"""
        snapshot = None
        fingerprint = ""
        if observation is not None and observation.ui_tree:
            snapshot = observation.ui_tree[:UI_SNAPSHOT_LIMIT]
            fingerprint = ui_fingerprint(observation.ui_tree)

        return cls(
            id=new_checkpoint_id(task_id, step),
            task_id=task_id,
            task_version=task_version,
            plan_version=plan_version,
            current_step=step,
            step_states=dict(step_states),
            screenshot_path=observation.screenshot_path if observation else None,
            package=observation.package if observation else "",
            activity=observation.activity if observation else "",
            ui_snapshot=snapshot,
            history_tail=list(history_tail or [])[-HISTORY_TAIL_SIZE:],
            action_effect=action_effect,
            last_action=last_action,
            action_attempt_id=action_attempt_id,
            screen_fingerprint=fingerprint,
            semantic_state=semantic_state,
            budget_used=dict(budget_used or {}),
        )

    def same_screen_as(self, observation: Observation) -> bool:
        """判断当前页面是否仍在恢复点所在的那一屏。

        只比 package/activity 这类廉价信号——UI 树快照仅作参考，
        因为同一页面重绘后 bounds 会有细微差异，硬比会永远判为「不一致」。
        """
        if not self.package:
            return False
        if observation.package != self.package:
            return False
        if self.activity and observation.activity != self.activity:
            return False
        return True

    def summary(self) -> dict[str, Any]:
        """给人看的精简描述（API 返回 / 日志用）。"""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "current_step": self.current_step,
            "task_version": self.task_version,
            "plan_version": self.plan_version,
            "package": self.package,
            "activity": self.activity,
            "step_states": {k: v.value for k, v in self.step_states.items()},
            "action_effect": self.action_effect.value,
            "screen_fingerprint": self.screen_fingerprint[:12],
            "budget_used": dict(self.budget_used),
            "created_at": self.created_at.isoformat(),
        }
