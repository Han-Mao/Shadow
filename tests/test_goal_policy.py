"""按任务画像决定完成验证策略（V2.2 §六）。

审核原话：

> 现在 `GOAL_VERIFY_MODE=advisory` 时「没有反证 → UNCERTAIN → 完成」。
> 这不是代码 bug，而是安全策略选择。但对于 Phone Agent 我建议默认：
>   navigation / external side effect / settings change → strict
>   纯查询类任务 → advisory
> 而不是全局一个 `GOAL_VERIFY_MODE`。

判错两类任务的代价不对称，所以策略也不该一样：

    纯查询    误判完成 → 用户重问一次
    有副作用  误判完成 → 用户以为发出去了，其实没有；或者以为没做，其实做了

这一组用例分三层：画像识别 → 策略解析 → 端到端裁定确实按画像分流。
"""
from __future__ import annotations

import pytest

from agent import goal_policy
from agent.goal_policy import TaskProfile, classify_profile, resolve_mode
from agent.goal_verifier import GoalVerdict, verify_goal
from agent.runtime import RunOutcome
from models.action import Action, ActionType
from models.budget import TaskBudget
from models.state import Observation
from models.task import Task


@pytest.fixture(autouse=True)
def _auto_mode(monkeypatch):
    """默认走「按画像自动判定」。"""
    monkeypatch.delenv("GOAL_VERIFY_MODE", raising=False)


# ---------------------------------------------------------------- 画像识别


@pytest.mark.parametrize(
    "instruction",
    [
        "打开微信",
        "进入淘宝的商品详情页",
        "返回上一页",
        "跳转到设置",
        "打开设置",          # 「设置」是 App 名，不是改设置 → 导航
        "open the calculator",
    ],
)
def test_navigation_tasks(instruction):
    assert classify_profile(instruction) is TaskProfile.NAVIGATION


@pytest.mark.parametrize(
    "instruction",
    [
        "给张三发一条消息",
        "在淘宝下单买这双鞋",
        "删除这条聊天记录",
        "帮我提交表单",
        "开启飞行模式",       # 改系统设置 → 副作用
        "把 WiFi 关了",
        "开启蓝牙",
        "取消关注这个公众号",
        "转账 100 块",
    ],
)
def test_side_effect_tasks(instruction):
    assert classify_profile(instruction) is TaskProfile.SIDE_EFFECT


@pytest.mark.parametrize(
    "instruction",
    [
        "查一下明天北京的天气",
        "搜索运动鞋",
        "看看这条新闻讲了什么",
        "这个手机多少钱",
        "翻译一下这段话",
    ],
)
def test_read_only_tasks(instruction):
    assert classify_profile(instruction) is TaskProfile.READ_ONLY


@pytest.mark.parametrize("instruction", ["嗯嗯", "", "随便弄一下", "让它更好一点"])
def test_unclassifiable_tasks(instruction):
    assert classify_profile(instruction) is TaskProfile.UNKNOWN


def test_side_effect_outranks_navigation():
    """「打开微信给张三发消息」同时命中导航与副作用 → 必须按副作用（严格）。

    先按导航放行，等于把最该严的那类任务放走了——优先级顺序是这个模块的核心。
    """
    profile = classify_profile("打开微信给张三发消息")

    assert profile is TaskProfile.SIDE_EFFECT


def test_settings_app_is_not_a_settings_change():
    """「打开设置」= 打开设置应用（导航）；「打开 WiFi」= 改系统状态（副作用）。

    只靠「设置」两个字分不出来，所以改设置必须要求一个系统开关作为宾语。
    """
    assert classify_profile("打开设置") is TaskProfile.NAVIGATION
    assert classify_profile("在设置里打开 WiFi") is TaskProfile.SIDE_EFFECT


# ---------------------------------------------------------------- 策略解析


def test_mode_is_resolved_from_the_profile():
    assert resolve_mode("查一下天气")[0] == goal_policy.ADVISORY
    assert resolve_mode("打开微信")[0] == goal_policy.STRICT
    assert resolve_mode("给张三发消息")[0] == goal_policy.STRICT
    assert resolve_mode("嗯嗯")[0] == goal_policy.ADVISORY, "认不出来时别默认最严"


def test_explicit_config_still_overrides(monkeypatch):
    """显式配置是**全局覆盖**，需要单点压过画像时用它（压测、回归）。"""
    monkeypatch.setenv("GOAL_VERIFY_MODE", "strict")
    assert resolve_mode("查一下天气")[0] == goal_policy.STRICT

    monkeypatch.setenv("GOAL_VERIFY_MODE", "advisory")
    assert resolve_mode("给张三发消息")[0] == goal_policy.ADVISORY

    monkeypatch.setenv("GOAL_VERIFY_MODE", "off")
    assert resolve_mode("给张三发消息")[0] == goal_policy.OFF


def test_auto_is_explicitly_supported(monkeypatch):
    monkeypatch.setenv("GOAL_VERIFY_MODE", "auto")
    assert resolve_mode("打开微信") == (goal_policy.STRICT, TaskProfile.NAVIGATION)


def test_policy_description_explains_itself():
    """「为什么这条任务判得比别人严」要能从裁定结果里读出来。"""
    override = goal_policy.describe_policy(TaskProfile.NAVIGATION, goal_policy.STRICT)
    assert "navigation" in override and "strict" in override

    assert goal_policy.mode_override() is None, "未配置时没有全局覆盖"


# ---------------------------------------------------------------- 裁定分流


def _done(reason: str = "已经完成了") -> Action:
    return Action(type=ActionType.DONE_REQUEST, reason=reason)


def _observation() -> Observation:
    return Observation(
        step=1,
        screenshot_path="/tmp/a.png",
        package="com.android.settings",
        activity=".Main",
        ui_tree='<hierarchy><node class="android.widget.TextView" text="设置"/></hierarchy>',
    )


def _same_shape_check(instruction: str):
    """同样的输入（计划剩 1 步、执行过 2 步、页面没推进、有理由），只换任务类型。"""
    return verify_goal(
        action=_done(),
        observation=_observation(),
        pending_steps=1,
        executed_steps=2,
        page_seen_changed=False,
        instruction=instruction,
    )


def test_same_situation_different_verdict_by_task_type():
    """同一个「计划没走完就声称完成」，查询类放行、副作用类驳回。"""
    query = _same_shape_check("查一下明天的天气")
    side_effect = _same_shape_check("给张三发消息")

    assert query.verdict is GoalVerdict.UNCERTAIN, "纯查询：证据不足 ≠ 反证，放行但留痕"
    assert query.mode == goal_policy.ADVISORY
    assert query.profile == TaskProfile.READ_ONLY.value

    assert side_effect.verdict is GoalVerdict.REJECTED, "有副作用：计划没走完就不认"
    assert side_effect.mode == goal_policy.STRICT
    assert side_effect.profile == TaskProfile.SIDE_EFFECT.value


def test_verifiable_evidence_still_wins_over_strict():
    """严格不等于不讲理：模型给出可核验且命中的声明，照样通过。"""
    check = verify_goal(
        action=Action(
            type=ActionType.DONE_REQUEST,
            reason="已发出",
            goal_evidence={"package": "com.android.settings"},
        ),
        observation=_observation(),
        pending_steps=3,
        executed_steps=1,
        page_seen_changed=False,
        instruction="给张三发消息",
    )

    assert check.verdict is GoalVerdict.CONFIRMED
    assert check.independent_evidence


def test_reason_string_records_which_policy_applied():
    check = _same_shape_check("给张三发消息")

    assert "strict" in check.reason
    assert "side_effect" in check.reason
    assert check.to_dict()["profile"] == "side_effect"
    assert check.to_dict()["mode"] == "strict"


def test_off_mode_still_short_circuits(monkeypatch):
    monkeypatch.setenv("GOAL_VERIFY_MODE", "off")

    check = _same_shape_check("给张三发消息")

    assert check.verdict is GoalVerdict.UNCERTAIN
    assert "关闭" in check.reason


# ---------------------------------------------------------------- 端到端：Runtime 里真的分流


def test_runtime_rejects_side_effect_completion(monkeypatch, tmp_path):
    """副作用任务：计划没走完就声称完成 → 事件流里能看到驳回与画像。"""
    from models.action import Decision
    from storage import EventLog

    from test_runtime import build, patch_observe, patch_planner, patch_verifier

    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["打开微信", "找到张三", "发送消息"],
        decisions=[
            Decision(action=Action(type=ActionType.DONE_REQUEST, reason="已经发出去了"))
        ],
    )
    patch_verifier(monkeypatch)

    events = EventLog(tmp_path / "events")
    session, runtime = build(tmp_path, event_log=events)
    task = _task("给张三发消息")
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    kinds = events.kinds(task.id)
    assert "goal_rejected" in kinds, "副作用任务不能凭一句「发出去了」就结束"
    rejected = [e for e in events.read(task.id) if e.kind == "goal_rejected"][-1]
    assert rejected.data["mode"] == "strict"
    assert rejected.data["profile"] == "side_effect"
    assert outcome is not RunOutcome.DONE


def test_runtime_lets_read_only_completion_through(monkeypatch, tmp_path):
    """纯查询任务：同样的形状，宽松策略下放行——否则 Agent 会变得不能用。"""
    from models.action import Decision
    from storage import EventLog

    from test_runtime import build, patch_observe, patch_planner, patch_verifier

    patch_observe(monkeypatch, tmp_path)
    patch_planner(
        monkeypatch,
        goals=["打开天气", "读取温度"],
        decisions=[Decision(action=Action(type=ActionType.DONE_REQUEST, reason="已看到温度"))],
    )
    patch_verifier(monkeypatch)

    events = EventLog(tmp_path / "events")
    session, runtime = build(tmp_path, event_log=events)
    task = _task("查一下明天天气")
    session.acquire(task.id)
    try:
        outcome = runtime.run(task)
    finally:
        session.release(task.id)

    assert outcome is RunOutcome.DONE
    confirmed = [e for e in events.read(task.id) if e.kind == "goal_confirmed"][-1]
    assert confirmed.data["mode"] == "advisory"
    assert confirmed.data["profile"] == "read_only"


def _task(instruction: str) -> Task:
    return Task(instruction=instruction, budget=TaskBudget(max_action_steps=5))
