"""目标验证器（V2.2 §四）：把「模型说完成」降级为「模型申请完成」。

审核对原来那段逻辑的判断是准确的：

> `DONE` 是一个过于强的模型权限。模型一旦返回 `{"action_type": "done"}`，
> 整个任务直接完成，没有真正独立验证目标。

现实里的失败长这样——任务「在淘宝搜索 iPhone 17 Pro Max 并进入商品详情页」，
模型看到搜索结果页就说 done，系统照单全收。

所以这里把完成拆成两件事：

    VLM → DONE_REQUEST → GoalVerifier → 独立证据 → 真正完成 / 打回继续 / 转人工

**独立证据**（不依赖模型自述）按强度排序：

    L6-a 计划跑完了        task.plan 里没有未完成步骤
    L6-b 页面真的推进过    本次任务至少发生过一次导航或结构变化
    L6-c 可核验声明        模型给出了 package / activity / text 声明，且逐条与真实页面相符

有**反证**才驳回（REJECTED），只是「证据不足」就如实记为 UNCERTAIN 并放行——
证据不足不等于有反证，把没有证据的完成一律拦下来会让 Agent 变成不能用。
但反过来的错误（有反证还放行）是不可接受的，所以驳回必须是真的。

`GOAL_VERIFY_MODE` 控制严格度（**V2.2 §六 起默认按任务画像自动判定**）：

    auto / 未设置  **默认**。按任务画像决定（见 `agent/goal_policy.py`）：
                    纯查询 → advisory；导航/副作用/改设置 → strict
    advisory       全局宽松：只在拿到反证时驳回
    strict         全局严格：计划里还有未完成步骤就驳回
    off            不检查

显式配置仍然是**全局覆盖**——需要单点压过画像时用它（例如压测、回归）。
裁定结果里会记下「这次用了哪个画像、哪个策略」，所以「为什么这次被判严格」
事后查得出来，不用猜配置。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum

from models.action import Action
from models.state import Observation
from vision import parser

from . import goal_policy

logger = logging.getLogger(__name__)

# 连续被驳回多少次后转人工（V2.2 §四）。
# 不设上限的话，「模型坚持说完成、验证器坚持驳回」会变成一次昂贵的空转。
MAX_GOAL_REJECTIONS = 2

# 可核验声明的白名单键与它们对应的核验方式
_TEXT_KEYS = ("text",)
_PAGE_KEYS = ("package", "activity")


class GoalVerdict(str, Enum):
    CONFIRMED = "confirmed"
    """有独立证据支持，确认完成。"""

    REJECTED = "rejected"
    """拿到**反证**：声称完成与可核验事实矛盾。任务必须继续。"""

    UNCERTAIN = "uncertain"
    """证据不足，既不能确认也不能否证。默认放行，但如实留痕。"""


@dataclass(frozen=True)
class GoalCheck:
    """一次完成申请的裁定结果。"""

    verdict: GoalVerdict
    reason: str
    checks: dict[str, str] = field(default_factory=dict)
    """逐条核验明细（声明 → 实际），审计要看的就是它。"""

    independent_evidence: bool = False
    """是否拿到了不依赖模型的证据。"""

    mode: str = ""
    """本次实际生效的策略（off / advisory / strict）。"""

    profile: str = ""
    """本次判定的任务画像（read_only / navigation / side_effect / unknown）。

    记下来是为了能回答「为什么这条任务判得比别人严」——
    否则「画像 → 策略」这条链路只在代码里，出了事只能猜。
    """

    @property
    def blocks_completion(self) -> bool:
        return self.verdict is GoalVerdict.REJECTED

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "checks": dict(self.checks),
            "independent_evidence": self.independent_evidence,
            "mode": self.mode,
            "profile": self.profile,
        }


def mode() -> str:
    """当前生效的**全局**策略名（`auto` 表示按任务画像走）。

    保留这个函数给运维/自检看配置，内部判定请用 `goal_policy.resolve_mode`。
    """
    return os.getenv("GOAL_VERIFY_MODE", "").strip().lower() or goal_policy.AUTO


def verify_goal(
    *,
    action: Action,
    observation: Observation | None,
    pending_steps: int,
    executed_steps: int,
    page_seen_changed: bool,
    instruction: str = "",
    context: str = "",
) -> GoalCheck:
    """裁定一次完成申请。纯本地、无网络、无副作用。

    严格度按**任务画像**自动选择（V2.2 §六）：纯查询宽松、导航与副作用严格。
    """
    current_mode, profile = goal_policy.resolve_mode(instruction, context)
    policy_note = goal_policy.describe_policy(profile, current_mode)

    def decided(verdict: GoalVerdict, reason: str, **kwargs) -> GoalCheck:
        return GoalCheck(
            verdict=verdict,
            reason=f"{reason}［{policy_note}］",
            mode=current_mode,
            profile=profile.value,
            **kwargs,
        )

    if current_mode == goal_policy.OFF:
        return decided(GoalVerdict.UNCERTAIN, "目标验证已关闭（GOAL_VERIFY_MODE=off）")

    # ---- L6-c：模型给了可核验声明 → 逐条比对（这是唯一的强判定）----
    declared = action.goal_evidence or {}
    if declared:
        checks, mismatched, unverifiable = _check_declarations(declared, observation)
        if mismatched:
            return decided(
                GoalVerdict.REJECTED,
                "完成申请与可核验事实矛盾：" + "；".join(mismatched),
                checks=checks,
                independent_evidence=True,
            )
        if unverifiable:
            return decided(
                GoalVerdict.UNCERTAIN,
                "完成申请提供了声明，但其中部分无法核验：" + "；".join(unverifiable),
                checks=checks,
            )
        return decided(
            GoalVerdict.CONFIRMED,
            f"完成申请的可核验声明全部命中（{len(checks)} 项）",
            checks=checks,
            independent_evidence=True,
        )

    # ---- L6-a：计划跑完了（计划状态是本地事实，不是模型自述）----
    if pending_steps == 0:
        return decided(
            GoalVerdict.CONFIRMED,
            "计划中没有未完成步骤，独立证据支持完成",
            independent_evidence=True,
        )

    # ---- strict：计划没做完就不认 ----
    if current_mode == goal_policy.STRICT:
        return decided(
            GoalVerdict.REJECTED,
            f"strict 策略（{profile.value} 任务）：计划仍有 {pending_steps} 步未完成，"
            "且模型未提供可核验证据",
            independent_evidence=True,
        )

    # ---- advisory：只在拿到反证时驳回 ----
    # 反证 = 既没走完计划、页面一次都没推进过、模型还连理由都懒得说
    if executed_steps > 0 and not page_seen_changed and not action.reason.strip():
        return decided(
            GoalVerdict.REJECTED,
            (
                f"声称完成但缺少任何支撑：计划仍有 {pending_steps} 步未完成，"
                f"已执行的 {executed_steps} 个动作都没有让页面发生任何变化，"
                "且未给出完成理由"
            ),
            independent_evidence=True,
        )

    reasons = [f"计划仍有 {pending_steps} 步未完成"]
    if not page_seen_changed:
        reasons.append("本次任务尚未观察到页面推进")
    if not action.reason.strip():
        reasons.append("模型未说明完成理由")
    return decided(
        GoalVerdict.UNCERTAIN,
        "完成申请缺少独立证据，但不构成反证：" + "；".join(reasons),
    )


def _check_declarations(
    declared: dict[str, str], observation: Observation | None
) -> tuple[dict[str, str], list[str], list[str]]:
    """逐条核验模型的完成声明。

    返回 (明细, 不匹配项, 无法核验项)。三者分开是为了让「矛盾」与「查不了」
    得到不同处置——只有前者才构成反证。
    """
    checks: dict[str, str] = {}
    mismatched: list[str] = []
    unverifiable: list[str] = []

    if observation is None:
        return {key: "无观察" for key in declared}, [], list(declared)

    ui_text = _ui_text_index(observation.ui_tree)

    for key, expected in declared.items():
        expected = str(expected).strip()
        if key in _PAGE_KEYS:
            actual = getattr(observation, key, "") or ""
            checks[key] = f"声明 {expected} / 实际 {actual or '未知'}"
            if not actual:
                unverifiable.append(f"{key}（当前页面信息缺失）")
            elif expected.lower() not in actual.lower():
                mismatched.append(f"{key} 声明为 {expected}，实际是 {actual}")
        elif key in _TEXT_KEYS:
            if ui_text is None:
                checks[key] = "声明 %s / 实际 无 UI 树" % expected
                unverifiable.append(f"text（没有 UI 树可比对）")
            elif expected.lower() in ui_text:
                checks[key] = f"声明 {expected} / 实际 命中"
            else:
                checks[key] = f"声明 {expected} / 实际 未出现"
                mismatched.append(f"页面上找不到声明的文字「{expected}」")
        elif key == "resource_id":
            if ui_text is None:
                checks[key] = "声明 %s / 实际 无 UI 树" % expected
                unverifiable.append("resource_id（没有 UI 树可比对）")
            elif expected.lower() in ui_text:
                checks[key] = f"声明 {expected} / 实际 命中"
            else:
                checks[key] = f"声明 {expected} / 实际 未出现"
                mismatched.append(f"页面上找不到声明的控件 id「{expected}」")
        else:
            checks[key] = f"声明 {expected} / 未知的核验键"
            unverifiable.append(key)

    return checks, mismatched, unverifiable


def _ui_text_index(ui_tree: str | None) -> str | None:
    """把 UI 树里所有可读文本拼成一个可搜索的字符串；树不可解析时返回 None。"""
    if not ui_tree:
        return None
    try:
        root = parser.parse(ui_tree)
    except Exception:  # noqa: BLE001 - 树坏了只是「无法核验」，不是「不匹配」
        return None
    parts: list[str] = []
    for node in parser.iter_nodes(root):
        for candidate in (node.text, node.content_desc, node.resource_id):
            if candidate:
                parts.append(candidate)
    return " ".join(parts).lower()
