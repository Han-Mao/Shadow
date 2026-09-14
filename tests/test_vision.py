"""感知层：UI 树解析、坐标落点、VLM 调用与 prompt 构造。"""
from __future__ import annotations

import httpx
import pytest

from fakes import FakeDevice, FakeResponse
from models.action import Action, ActionType, Point
from vision import grounding, parser, vlm
from vision.grounding import GroundingError
from vision.vlm import VlmError, VlmParseError, _extract_json


# ---------------------------------------------------------------- parser


def test_parse_bounds_accepts_negative_and_survives_bad_value():
    xml = (
        "<hierarchy>"
        '<node bounds="[-1,-1][-1,-1]" clickable="true"/>'
        '<node bounds="[10,20][110,220]" clickable="true" text="OK"/>'
        '<node bounds="oops" clickable="true" text="bad"/>'
        "</hierarchy>"
    )
    nodes = parser.find_clickable(parser.parse(xml))

    assert nodes[0].bounds == (-1, -1, -1, -1)
    assert nodes[1].bounds == (10, 20, 110, 220)
    assert nodes[1].center == (60, 120)
    # 单个坏节点降级为零矩形，而不是让整棵树解析失败
    assert nodes[2].bounds == (0, 0, 0, 0)


def test_match_by_text_prefers_exact_over_contains():
    xml = (
        "<hierarchy>"
        '<node bounds="[0,0][100,100]" clickable="true" text="搜索历史"/>'
        '<node bounds="[0,200][100,300]" clickable="true" text="搜索"/>'
        "</hierarchy>"
    )
    node = parser.match_by_text(parser.find_clickable(parser.parse(xml)), "搜索")
    assert node is not None
    assert node.center == (50, 250)


def test_match_by_text_ignores_empty_description():
    xml = '<hierarchy><node bounds="[0,0][10,10]" clickable="true" text="a"/></hierarchy>'
    assert parser.match_by_text(parser.find_clickable(parser.parse(xml)), "   ") is None


# ---------------------------------------------------------------- grounding


def test_resolve_target_pixel_point_skips_screen_query():
    """像素坐标不该触发 screen_size（每一步 tap 都多一次 dumpsys 是纯浪费）。"""
    fake = FakeDevice()
    assert grounding.resolve_target(fake, Point(x=540, y=1200)) == (540, 1200)
    assert fake.size_calls == 0


def test_resolve_target_normalized_point_uses_screen_size():
    fake = FakeDevice(size=(1000, 2000))
    assert grounding.resolve_target(fake, Point(x=0.5, y=0.25)) == (500, 500)
    assert fake.size_calls == 1


def test_resolve_target_string_forms():
    fake = FakeDevice(size=(1000, 2000))
    assert grounding.resolve_target(fake, "540,1200") == (540, 1200)
    assert grounding.resolve_target(fake, "[100,200,300,400]") == (200, 300)
    assert grounding.resolve_target(fake, "点击(540, 1200)") == (540, 1200)
    assert grounding.resolve_target(fake, "0.5,0.5") == (500, 1000)


def test_resolve_target_never_mistakes_text_for_coordinates():
    """「微信 v8.0.32」里有两个数字，绝不能被当成坐标 (8, 32)。"""
    fake = FakeDevice()
    with pytest.raises(GroundingError):
        grounding.resolve_target(fake, "微信 v8.0.32", ui_tree=None)


def test_resolve_target_matches_ui_tree_then_fails_loudly():
    xml = '<hierarchy><node bounds="[400,1000][600,1200]" clickable="true" text="搜索"/></hierarchy>'
    fake = FakeDevice()
    assert grounding.resolve_target(fake, "搜索", ui_tree=xml) == (500, 1100)
    with pytest.raises(GroundingError):
        grounding.resolve_target(fake, "不存在的元素", ui_tree=xml)


# ---------------------------------------------------------------- vlm 解析与重试


def test_extract_json_handles_fences_and_prose():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('好的，我的判断是：{"result": "ok", "reason": "已进入设置"} 以上。') == {
        "result": "ok",
        "reason": "已进入设置",
    }


def test_extract_json_raises_typed_error_without_json():
    with pytest.raises(VlmParseError):
        _extract_json("这次没有 JSON")
    with pytest.raises(VlmParseError):
        _extract_json("")


def test_image_detail_is_stage_specific(monkeypatch):
    """决策需要 high（坐标精度），计划与验证用 low（省 token 与延迟）。"""
    assert vlm._image_detail("decide") == "high"
    assert vlm._image_detail("plan") == "low"
    assert vlm._image_detail("verify") == "low"

    monkeypatch.setenv("VLM_DETAIL_DECIDE", "low")
    assert vlm._image_detail("decide") == "low"


def test_vlm_retries_retryable_status(monkeypatch):
    calls: list = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        status = 503 if len(calls) < 3 else 200
        return FakeResponse(status, {"choices": [{"message": {"content": '{"ok": 1}'}}]})

    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.setattr(vlm.httpx, "post", fake_post)
    monkeypatch.setattr(vlm.time, "sleep", lambda _s: None)

    message = vlm._call_vlm([{"role": "user", "content": "hi"}])
    assert len(calls) == 3
    assert message["content"] == '{"ok": 1}'


def test_vlm_does_not_retry_client_errors(monkeypatch):
    calls: list = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return FakeResponse(401)

    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.setattr(vlm.httpx, "post", fake_post)
    monkeypatch.setattr(vlm.time, "sleep", lambda _s: None)

    with pytest.raises(VlmError):
        vlm._call_vlm([{"role": "user", "content": "hi"}])
    assert len(calls) == 1


def test_vlm_retries_network_errors(monkeypatch):
    calls: list = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("连接被拒绝")
        return FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.setattr(vlm.httpx, "post", fake_post)
    monkeypatch.setattr(vlm.time, "sleep", lambda _s: None)

    vlm._call_vlm([{"role": "user", "content": "hi"}])
    assert len(calls) == 2


def test_vlm_raises_after_exhausting_retries(monkeypatch):
    calls: list = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return FakeResponse(429)

    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.setattr(vlm.httpx, "post", fake_post)
    monkeypatch.setattr(vlm.time, "sleep", lambda _s: None)

    with pytest.raises(VlmError, match="已重试"):
        vlm._call_vlm([{"role": "user", "content": "hi"}])
    assert len(calls) == vlm.VLM_MAX_ATTEMPTS


def test_vlm_requires_api_key(monkeypatch):
    monkeypatch.delenv("VLM_API_KEY", raising=False)
    with pytest.raises(VlmError, match="VLM_API_KEY"):
        vlm._call_vlm([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------- prompt 构造


def test_decision_prompt_renders_observation_only_step():
    """action 为 None 的观察步应显示占位说明，而不是 "None"。"""
    history = [
        {
            "step": 0,
            "action": None,
            "status": "ok",
            "message": "",
            "result": {},
            "package": "com.android.settings",
            "activity": ".Settings",
        },
        {
            "step": 1,
            "action": {"type": "tap", "target": {"x": 10.0, "y": 20.0}, "value": None, "reason": "点搜索"},
            "status": "ok",
            "message": "已点击",
            "result": {"ok": True, "x": 10, "y": 20},
            "package": "com.android.settings",
            "activity": ".Settings",
        },
    ]
    text = vlm.build_decision_prompt("打开设置", "", None, history, [], "找到搜索入口")

    assert "（无动作，仅观察）" in text
    history_section = text.split("最近执行记录：")[1].split("当前页面")[0]
    assert "None" not in history_section
    assert "tap" in history_section and "x=10.0" in history_section
    # 当前聚焦步骤要出现在 prompt 里，模型才知道这一步该干什么
    assert "找到搜索入口" in text


def test_decision_prompt_shows_plan_with_statuses():
    text = vlm.build_decision_prompt("x", "", None, [], ["[done] 打开设置", "[pending] 开启飞行模式"])
    assert "[done] 打开设置" in text
    assert "[pending] 开启飞行模式" in text


def test_replan_prompt_frames_failure_as_strategy_failure():
    """Re-plan 必须让模型明白「是这种执行方式失败，不是任务失败」，否则它会原样重发。"""
    context = {
        "task": "打开设置并开启飞行模式",
        "current_step": "找到飞行模式开关",
        "previous_action": {"type": "tap", "target": {"x": 100.0, "y": 200.0}, "value": None},
        "failure_reason": "UI 树与执行前完全一致，疑似无效操作",
        "failed_strategies": ["tap target={'x': 100.0, 'y': 200.0} value=None"],
        "available_alternatives": ["改用元素文本定位"],
    }
    text = vlm.build_replan_prompt(context)

    assert "任务本身没有失败" in text
    assert "找到飞行模式开关" in text
    assert "UI 树与执行前完全一致" in text
    assert "tap" in text
    assert "改用元素文本定位" in text
    assert "不要重复已经失败过的动作" in text


def test_replan_prompt_handles_empty_context():
    text = vlm.build_replan_prompt({})
    assert "（暂无）" in text
    assert "未知" in text


def test_parse_decision_reads_step_done():
    decision = vlm._parse_decision(
        {"action_type": "tap", "target": {"x": 1, "y": 2}, "thought": "点搜索", "step_done": True}
    )
    assert decision.step_done is True
    assert decision.thought == "点搜索"
    assert decision.action.type is ActionType.TAP


def test_parse_decision_treats_done_flag_as_completion():
    decision = vlm._parse_decision({"action_type": "tap", "done": True})
    assert decision.action.type is ActionType.DONE


def test_parse_action_ignores_invalid_risk():
    decision = vlm._parse_decision({"action_type": "tap", "risk": "apocalyptic"})
    assert decision.action.risk is None


def test_parse_action_accepts_valid_risk():
    decision = vlm._parse_decision({"action_type": "tap", "risk": "dangerous"})
    assert decision.action.resolved_risk().value == "dangerous"


def test_classify_relation_parses_llm_verdict(monkeypatch):
    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.setattr(
        vlm.httpx,
        "post",
        lambda *a, **k: FakeResponse(
            200,
            {"choices": [{"message": {"content": '{"relation": "subtask", "confidence": 0.94, "reason": "前置步骤"}'}}]},
        ),
    )
    result = vlm.classify_relation("先帮我查东京酒店", "帮我规划东京三日游")
    assert result.relation.value == "subtask"
    assert result.confidence == pytest.approx(0.94)


def test_classify_relation_rejects_unknown_relation(monkeypatch):
    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.setattr(
        vlm.httpx,
        "post",
        lambda *a, **k: FakeResponse(
            200, {"choices": [{"message": {"content": '{"relation": "befriends"}'}}]}
        ),
    )
    result = vlm.classify_relation("x", "y")
    assert result.relation.value == "unrelated"
    assert "未知关系" in result.reason


# ---------------------------------------------------------------- V2.2 §七：严格验证枚举


def patch_chat(monkeypatch, content: str) -> None:
    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.setattr(
        vlm.httpx,
        "post",
        lambda *a, **k: FakeResponse(200, {"choices": [{"message": {"content": content}}]}),
    )


def two_shots(tmp_path) -> tuple[str, str]:
    """验证要读两张真实截图文件（`_encode_image` 会 open），所以先落两个占位图。"""
    pre = tmp_path / "pre.png"
    post = tmp_path / "post.png"
    pre.write_bytes(b"\x89PNG\r\n\x1a\n")
    post.write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(pre), str(post)


def test_verify_transition_returns_strict_enum(monkeypatch, tmp_path):
    patch_chat(monkeypatch, '{"result": "DONE", "reason": "页面已到详情页"}')

    verdict = vlm.verify_transition(*two_shots(tmp_path), "任务", {"type": "tap"})

    assert verdict is vlm.VerifyResult.DONE


def test_verify_transition_rejects_unknown_value(monkeypatch, tmp_path):
    """`{"result": "banana"}` 必须报错，不能原样传下去。

    旧实现是 `str(data.get("result", "ok")).lower()`：认不出的值会一路传到 verifier，
    匹配不上 done/error 之后落到默认分支 `outcome=OK` —— 相当于「模型胡说 = 这步过了」。
    """
    patch_chat(monkeypatch, '{"result": "banana"}')

    with pytest.raises(vlm.VlmVerifyError, match="未知验证结论"):
        vlm.verify_transition(*two_shots(tmp_path), "任务", {"type": "tap"})


def test_verify_transition_rejects_missing_result(monkeypatch, tmp_path):
    """空对象以前会被解析成 "ok"（默认值），现在必须报错。"""
    patch_chat(monkeypatch, "{}")

    with pytest.raises(vlm.VlmVerifyError, match="未返回 result"):
        vlm.verify_transition(*two_shots(tmp_path), "任务", {"type": "tap"})


# ---------------------------------------------------------------- V2.2 §一/§四：解析模型表态


def test_parse_action_writes_model_risk_as_hint():
    """模型的风险表态进 risk_hint（建议），不是 risk（权威标注）。"""
    action = vlm._parse_action({"action_type": "tap", "risk": "dangerous"})

    assert action.risk is None, "模型不该直接写权威字段"
    assert action.risk_hint is vlm.ActionRisk.DANGEROUS
    assert action.resolved_risk() is vlm.ActionRisk.DANGEROUS


def test_parse_action_reads_goal_evidence_whitelist():
    """完成声明只认白名单键——多写的字段不参与核验，避免用没人看的键伪装「有证据」。"""
    action = vlm._parse_action(
        {
            "action_type": "done",
            "goal_evidence": {
                "package": "com.taobao.taobao",
                "text": "立即购买",
                "whatever": "noise",
                "activity": "",
            },
        }
    )

    assert action.goal_evidence == {"package": "com.taobao.taobao", "text": "立即购买"}


def test_parse_decision_accepts_done_request_alias():
    """模型可以直接申请完成（done_request），语义与 done 一致。"""
    decision = vlm._parse_decision({"action_type": "done_request", "thought": "做完了"})

    assert decision.action.type is ActionType.DONE_REQUEST
    assert decision.action.is_completion_request


def test_done_evidence_is_rendered_in_prompt():
    text = vlm.build_decision_prompt("x", "", None, [], [], "第一步")

    assert "goal_evidence" in text
    assert "risk_hint" in text
