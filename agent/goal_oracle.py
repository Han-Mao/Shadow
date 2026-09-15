"""GoalOracle（V3 M1）：把「计划跑完」与「目标达成」彻底分开。

v2.9 审核点出的「闭环自证」：

    Plan 本身就是模型生成的
        ↓
    模型执行自己制定的计划
        ↓
    计划跑完（pending_steps == 0）
        ↓
    GoalVerifier 判 CONFIRMED

这里的问题不是「计划状态算不算事实」——计划跑没跑完确实是本地事实，不是模型自述。
问题是它属于**执行系统内部状态**，而不是**真实世界目标状态**。模型漏了「进入商品详情页」
这一步、只做了「打开淘宝」「搜索」，计划照样能「跑完」，但目标（进入详情页）没达成。

所以这里把两个概念拆成两个函数：

    plan_finished(plan)     →  计划还有没有 pending 步骤（本地事实，弱证据）
    goal_achieved(evidence) →  独立于模型计划的世界证据（可核验声明命中 / 页面推进）

裁定规则：**「计划跑完」是完成的门槛，不是完成的依据。** 真正的完成信号是
「独立于模型计划的世界状态证据」。

按画像分层（strict = navigation / side_effect，advisory = read_only）：

    strict   计划跑完 + 页面推进过     → 完成（计划跑完 + 世界状态确实变了）
    strict   计划跑完 + 无页面推进     → 不确定（只有内部状态，没有世界证据）
    advisory 计划跑完                  → 完成（纯查询，计划跑完就够了）
"""
from __future__ import annotations

from dataclasses import dataclass

from . import goal_policy
from .goal_policy import TaskProfile


@dataclass(frozen=True)
class GoalEvidence:
    """一次完成申请能拿到的全部独立证据（都不依赖模型自述）。"""

    pending_steps: int = 0
    """计划里还有多少步未完成。0 表示计划跑完（本地事实，弱证据）。"""

    page_seen_changed: bool = False
    """本次任务是否观察到过页面推进（世界状态变过，中等证据）。"""

    declared_matched: bool = False
    """模型是否给出了**命中**的可核验声明（package/activity/text 与真实页面相符，强证据）。"""

    @property
    def plan_finished(self) -> bool:
        return self.pending_steps == 0

    @property
    def goal_achieved(self) -> bool:
        """目标达成的世界证据：可核验声明命中，或页面真的推进过。"""
        return self.declared_matched or self.page_seen_changed


def plan_finished(pending_steps: int) -> bool:
    """计划还有没有未完成步骤。这是本地事实，不是模型自述——但它**不**等于目标达成。"""
    return pending_steps == 0


def goal_achieved(*, declared_matched: bool, page_seen_changed: bool) -> bool:
    """独立于模型计划的世界证据是否成立。

    两条路：要么模型给出了命中真实页面的可核验声明（强），要么本次任务观察到过
    页面推进（中）。两者都独立于「模型怎么拆分计划」。
    """
    return declared_matched or page_seen_changed


def achieved_by_plan_alone(profile: TaskProfile) -> bool:
    """「计划跑完」单独能否构成完成。

    只有纯查询（advisory 画像）可以——查一下天气，计划跑完就等于查到答案了。
    有副作用 / 导航（strict 画像）不行：计划是模型自己拆的，跑完不代表目标达成，
    必须有独立的世界证据（页面推进 / 可核验声明）。
    """
    return profile is TaskProfile.READ_ONLY


def requires_world_evidence(profile: TaskProfile) -> bool:
    """是否需要「独立于计划的世界证据」才能确认完成。"""
    return profile in (TaskProfile.NAVIGATION, TaskProfile.SIDE_EFFECT)


def describe_rule(profile: TaskProfile) -> str:
    """给 reason 用的一句话，说清楚「这个画像下，计划跑完够不够」。"""
    if achieved_by_plan_alone(profile):
        return "计划跑完即视为完成（纯查询任务）"
    return "计划跑完不单独构成完成，需叠加世界证据（页面推进 / 可核验声明）"
