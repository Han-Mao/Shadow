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
