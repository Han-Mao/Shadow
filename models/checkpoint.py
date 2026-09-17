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

    # ---- 执行平面上下文（V5 §十三）----
    #
    # §十三 要求恢复点带上「这个任务当时跑在**哪块屏幕上**」。加这四个字段的理由
    # 不是「多存点信息总没坏处」，而是**恢复时无法从别处推出来**：
    #
    #   `Task.execution_mode` 是任务的**声明**（「我希望跑在影子平面」），
    #   而恢复点要回答的是「**我上次实际在哪跑的**」。两者在正常情况下一致，
    #   在 hybrid 降级、影子平面中途不可用、用户改了模式之后就会分叉——
    #   而恰恰是那些情况下的恢复最需要知道真相：如果上次跑在用户那块屏幕上，
    #   恢复时按「影子平面」去定位页面，就会对着一块空的显示找东西。
    #
    # 默认值全部取「前台 / 未指定」，因为这正是 V5 之前所有恢复点的真实情况：
    # 老记录反序列化后得到的就是这个组合，语义恰好等于「当时没有平面概念，
    # 就是那块默认屏幕」。**不能用 None 表示「不知道」**——那会让老记录和
    # 「真的不知道」混起来，而后者在恢复时应当更保守。

    execution_mode: str = "foreground"
    """实际执行时的平面（`ExecutionMode.value`），不是任务声明的那个。"""

    session_id: str = ""
    """执行会话 id（`ShadowSession.session_id`）。空串＝没有会话概念（V5 之前）。"""

    display_id: int | None = None
    """实际使用的 display id。`None` ＝当时没有记录。"""

    shadow_state_id: str = ""
    """影子平面上的状态标识（§十三）。

    留空是诚实的现状：真正的影子平面（§十五 第二阶段）尚未实现，
    所以现在**没有**这个东西可记。这个字段先占位，是为了让恢复逻辑写成
    「有就用、没有就按单平面处置」，而不是等实现时再回来加一个字段——
    那时所有既存恢复点又都要靠默认值兜底一次。
    """

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
        execution_mode: str = "foreground",
        session_id: str = "",
        display_id: int | None = None,
        shadow_state_id: str = "",
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
            execution_mode=execution_mode,
            session_id=session_id,
            display_id=display_id,
            shadow_state_id=shadow_state_id,
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
            # V5 §十三：执行平面也要能被查出来。「任务跑在哪块屏幕上」是个
            # 排查时的**第一个问题**（「为什么它点到了我的微信？」），
            # 而它不在 summary 里的话，只能去翻原始记录才能回答。
            "execution_mode": self.execution_mode,
            "session_id": self.session_id,
            "display_id": self.display_id,
            "created_at": self.created_at.isoformat(),
        }
