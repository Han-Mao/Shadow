"""GoalVerifier（V2.2 §四）：把「模型说完成」降级为「模型申请完成」。

审核的原话：

> `DONE` 是一个过于强的模型权限。模型一旦返回 `{"action_type": "done"}`，
> 整个任务直接完成，没有真正独立验证目标。

这里锁定新语义：**有反证才驳回，证据不足则如实记为 uncertain 并放行**。
两种错误不对等——「没有证据就拦」会让 Agent 不可用，
「有反证还放行」才是不可接受的。
"""
from __future__ import annotations

import pytest

from agent import goal_verifier
from agent.goal_verifier import GoalVerdict, verify_goal
from models.action import Action, ActionType
from models.state import Observation

PAGE = (
    '<hierarchy>'
    '<node class="android.widget.TextView" text="商品详情" bounds="[0,0][100,50]"/>'
    "</hierarchy>"
)


def observation(package: str = "com.taobao.taobao", activity: str = ".DetailActivity") -> Observation:
    return Observation(
        step=1,
        screenshot_path="/tmp/a.png",
        package=package,
        activity=activity,
        ui_tree=PAGE,
    )


def done(reason: str = "已经进入商品页面", **evidence) -> Action:
    return Action(type=ActionType.DONE_REQUEST, reason=reason, goal_evidence=evidence)


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch):
    monkeypatch.delenv("GOAL_VERIFY_MODE", raising=False)


# ---------------------------------------------------------------- 可核验声明


def test_matching_declaration_confirms_completion():
    check = verify_goal(
        action=done(package="com.taobao.taobao", text="商品详情"),
        observation=observation(),
        pending_steps=3,
        executed_steps=1,
        page_seen_changed=False,
    )

    assert check.verdict is GoalVerdict.CONFIRMED
    assert check.independent_evidence
    assert len(check.checks) == 2


def test_contradicted_declaration_is_rejected():
    """模型说自己在商品详情页，实际还在搜索结果页——这是**正面反证**。"""
    check = verify_goal(
        action=done(package="com.taobao.taobao", text="立即购买"),
        observation=observation(),
        pending_steps=3,
        executed_steps=1,
        page_seen_changed=True,
    )

    assert check.verdict is GoalVerdict.REJECTED
    assert check.blocks_completion
    assert any("立即购买" in reason for reason in check.reason.split("；"))


def test_undeclarable_evidence_is_uncertain_not_rejected():
    """声明了但当前拿不到页面信息 → 既不能确认也不能否证。"""
    check = verify_goal(
        action=done(package="com.taobao.taobao"),
        observation=None,
        pending_steps=1,
        executed_steps=1,
        page_seen_changed=True,
    )

    assert check.verdict is GoalVerdict.UNCERTAIN


# ---------------------------------------------------------------- 计划与页面


def test_finished_plan_is_independent_evidence():
    """计划状态是本地事实，不是模型自述——它跑完了就构成独立证据。"""
    check = verify_goal(
        action=done(reason="做完了"),
        observation=observation(),
        pending_steps=0,
        executed_steps=4,
        page_seen_changed=True,
    )

    assert check.verdict is GoalVerdict.CONFIRMED
    assert check.independent_evidence


def test_bare_completion_claim_is_rejected():
    """既没走完计划、页面一次都没推进过、连理由都没给 → 驳回。"""
    check = verify_goal(
        action=done(reason=""),
        observation=observation(),
        pending_steps=2,
        executed_steps=3,
        page_seen_changed=False,
    )

    assert check.verdict is GoalVerdict.REJECTED


def test_completion_with_reason_but_no_evidence_is_uncertain():
    """证据不足 ≠ 有反证：有理由就放行，但如实记为「未获独立证据」。"""
    check = verify_goal(
        action=done(reason="已经进入商品页面"),
        observation=observation(),
        pending_steps=2,
        executed_steps=3,
        page_seen_changed=False,
    )

    assert check.verdict is GoalVerdict.UNCERTAIN
    assert not check.independent_evidence
    assert "缺少独立证据" in check.reason


def test_page_progress_counts_as_support():
    check = verify_goal(
        action=done(reason=""),
        observation=observation(),
        pending_steps=2,
        executed_steps=3,
        page_seen_changed=True,
    )

    assert check.verdict is GoalVerdict.UNCERTAIN


# ---------------------------------------------------------------- 模式


def test_strict_mode_rejects_unfinished_plan(monkeypatch):
    monkeypatch.setenv("GOAL_VERIFY_MODE", "strict")

    check = verify_goal(
        action=done(reason="我觉得可以了"),
        observation=observation(),
        pending_steps=2,
        executed_steps=1,
        page_seen_changed=True,
    )

    assert check.verdict is GoalVerdict.REJECTED


def test_strict_mode_still_accepts_verifiable_evidence(monkeypatch):
    """严格模式下，模型给出可核验且命中的声明依然能通过——证据说话。"""
    monkeypatch.setenv("GOAL_VERIFY_MODE", "strict")

    check = verify_goal(
        action=done(activity="DetailActivity"),
        observation=observation(),
        pending_steps=2,
        executed_steps=1,
        page_seen_changed=False,
    )

    assert check.verdict is GoalVerdict.CONFIRMED


def test_off_mode_skips_verification(monkeypatch):
    monkeypatch.setenv("GOAL_VERIFY_MODE", "off")

    check = verify_goal(
        action=done(reason=""),
        observation=observation(),
        pending_steps=9,
        executed_steps=0,
        page_seen_changed=False,
    )

    assert check.verdict is GoalVerdict.UNCERTAIN
    assert "关闭" in check.reason


def test_rejection_budget_is_exposed():
    """连续驳回的上界必须存在，否则「模型坚持 + 验证器坚持」会变成昂贵空转。"""
    assert goal_verifier.MAX_GOAL_REJECTIONS >= 1
