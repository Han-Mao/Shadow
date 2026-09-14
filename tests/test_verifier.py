"""verifier：验证结论的「发出 / 效果 / 目标」三层（V2.1 §十九）。

这三件事原来挤在一个 `Verification` 里，日志只看到一句「失败」，分不清
「命令没发出去」「发出去了但页面没反应」「做完了但任务还没完」——
而这三者的处置完全不同（重试 / 换策略 / 继续）。
"""
from __future__ import annotations

import agent.verifier as verifier_mod
from agent.verifier import verify_action
from models.action import Action, ActionEffectStatus, ActionType, Point
from models.state import Observation, StepOutcome
from models.verification import DispatchStatus

UI_A = '<hierarchy><node class="android.widget.Button" text="确定" clickable="true"/></hierarchy>'
UI_B = '<hierarchy><node class="android.widget.Button" text="已提交" clickable="true"/></hierarchy>'


def obs(step: int, ui_tree: str) -> Observation:
    return Observation(
        step=step,
        screenshot_path=f"/tmp/s{step}.png",
        package="com.demo",
        activity=".Main",
        ui_tree=ui_tree,
    )


def patch_vlm(monkeypatch, verdict: str) -> None:
    monkeypatch.setattr(verifier_mod.vlm, "verify_transition", lambda *a, **k: verdict)


def tap() -> Action:
    return Action(type=ActionType.TAP, target=Point(x=10, y=20))


def test_device_failure_marks_dispatch_failed(monkeypatch):
    """命令没送达：dispatch 失败、效果无从谈起，与「发出去了没效果」明确区分。"""
    patch_vlm(monkeypatch, "ok")
    result = verify_action(
        "做事", obs(1, UI_A), tap(), obs(2, UI_B), {"ok": False, "error": "adb 超时"}
    )

    assert result.dispatch.status is DispatchStatus.FAILED
    assert result.dispatch.transport_error == "adb 超时"
    assert result.effect.status is ActionEffectStatus.VERIFIED_FAILED
    assert not result.goal.achieved
    assert result.outcome is StepOutcome.ERROR


def test_success_marks_dispatch_sent_and_effect_verified(monkeypatch):
    patch_vlm(monkeypatch, "ok")
    result = verify_action("做事", obs(1, UI_A), tap(), obs(2, UI_B), {"ok": True})

    assert result.dispatch.status is DispatchStatus.SENT
    assert result.effect.status is ActionEffectStatus.VERIFIED_SUCCESS
    assert result.effect.changed
    assert result.goal.achieved


def test_ui_unchanged_on_mutating_action_is_effect_unknown(monkeypatch):
    """VLM 说成功但 UI 树没变 → 效果**存疑**，绝不能记成「已验证成功」。

    这是本组用例里最关键的一条：如果记成 VERIFIED_SUCCESS，
    之后进程崩溃、从这个恢复点续跑时就会跳过对账，把一次可能根本没生效的点击当成已完成。
    """
    patch_vlm(monkeypatch, "ok")
    result = verify_action("做事", obs(1, UI_A), tap(), obs(2, UI_A), {"ok": True})

    assert result.dispatch.status is DispatchStatus.SENT, "命令确实发出去了"
    assert result.effect.status is ActionEffectStatus.EFFECT_UNKNOWN
    assert not result.effect.changed
    assert result.outcome is StepOutcome.OK


def test_vlm_done_marks_goal_achieved(monkeypatch):
    patch_vlm(monkeypatch, "done")
    result = verify_action("做事", obs(1, UI_A), tap(), obs(2, UI_B), {"ok": True})

    assert result.goal.achieved and result.goal.done
    assert result.outcome is StepOutcome.DONE


def test_vlm_error_marks_effect_failed(monkeypatch):
    patch_vlm(monkeypatch, "error")
    result = verify_action("做事", obs(1, UI_A), tap(), obs(2, UI_B), {"ok": True})

    assert result.effect.status is ActionEffectStatus.VERIFIED_FAILED
    assert not result.goal.achieved
    assert result.should_replan


def test_done_action_skips_dispatch(monkeypatch):
    """规划器直接宣告完成：没有动作可发，dispatch 应当记为 SKIPPED 而不是 SENT。"""
    patch_vlm(monkeypatch, "ok")
    result = verify_action(
        "做事", obs(1, UI_A), Action(type=ActionType.DONE, reason="完成"), obs(2, UI_B)
    )

    assert result.dispatch.status is DispatchStatus.SKIPPED
    assert result.goal.done


# ---------------------------------------------------------------- V2.2 §七：认不出的结论不当成功


def patch_vlm_unusable(monkeypatch):
    """模拟 VLM 结论不可解读（`VlmVerifyError`）或整个不可用。"""
    def boom(*args, **kwargs):
        raise verifier_mod.vlm.VlmVerifyError("VLM 返回未知验证结论：'banana'")

    monkeypatch.setattr(verifier_mod.vlm, "verify_transition", boom)


def test_unusable_vlm_verdict_does_not_default_to_success(monkeypatch):
    """没有语义证据 + 页面无变化 → 效果未知，**不是** OK。

    旧实现里 `{"result": "banana"}` 匹配不上 done/error，直接落到默认分支
    `outcome=OK`，等于「模型胡说 = 这步过了」。
    """
    patch_vlm_unusable(monkeypatch)

    result = verify_action("做事", obs(1, UI_A), tap(), obs(2, UI_A), {"ok": True})

    assert result.effect.status is ActionEffectStatus.EFFECT_UNKNOWN
    assert not result.goal.achieved
    assert result.effect.ambiguous


def test_dangerous_action_without_evidence_is_never_treated_as_success(monkeypatch):
    """不可撤销动作 + 拿不到独立证据 → 效果存疑，绝不能静默放行。"""
    patch_vlm_unusable(monkeypatch)
    danger = Action(type=ActionType.TAP, value="确认付款", target=Point(x=10, y=20))

    result = verify_action("付款", obs(1, UI_A), danger, obs(2, UI_A), {"ok": True})

    assert result.effect.status is ActionEffectStatus.EFFECT_UNKNOWN
    assert result.effect.ambiguous
    assert not result.goal.achieved
    assert not result.should_retry, "危险动作绝不自动重试"


def test_local_evidence_rescues_an_unusable_vlm(monkeypatch):
    """VLM 不可用，但页面结构确实变了 → 有据可依，成功。

    注意这不是「默认成功」：依据（l3_structure）会被明确记录。
    """
    patch_vlm_unusable(monkeypatch)

    result = verify_action("做事", obs(1, UI_A), tap(), obs(2, UI_B), {"ok": True})

    assert result.outcome is StepOutcome.OK
    assert result.effect.status is ActionEffectStatus.VERIFIED_SUCCESS
    assert result.effect.evidence == "l3_structure"
    assert result.goal.independent_evidence


def test_navigation_change_outranks_structure_evidence(monkeypatch):
    """同一次验证里，导航变化（L2）比结构变化（L3）更硬，证据层要如实反映。"""
    patch_vlm(monkeypatch, "ok")
    after = Observation(
        step=2,
        screenshot_path="/tmp/s2.png",
        package="com.demo",
        activity=".Detail",
        ui_tree=UI_B,
    )

    result = verify_action("做事", obs(1, UI_A), tap(), after, {"ok": True})

    assert result.layer == "vlm+l2_navigation"
    assert result.effect.evidence == "l2_navigation"
    assert result.effect.changed
