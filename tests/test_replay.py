"""Replay：把事件流按时间轴读回来（V2.1 §二十三）。

回放的价值在于回答「它当时到底干了什么、为什么」，
所以测试重点不是渲染得好不好看，而是：
- 时间轴顺序与偏移正确（顺序错了，回放就没有意义）
- 出问题的那几帧能被挑出来（跑成功的任务没什么好看的）
- 动作重放**默认不执行**、危险动作必须显式放行
"""
from __future__ import annotations

import pytest

from agent.replay import (
    ReplayPhase,
    ReplayRefused,
    build_plan,
    build_timeline,
    load_timeline,
    replay,
)
from storage.event_log import Event, EventLog


def ev(kind: str, at: str, **data) -> Event:
    return Event(task_id="t1", kind=kind, data=data, at=at, id=f"e{kind}")


def log(tmp_path) -> EventLog:
    return EventLog(tmp_path / "events")


# ---------------------------------------------------------------- 时间轴


def test_timeline_keeps_order_and_computes_offsets():
    timeline = build_timeline(
        "t1",
        [
            ev("queued", "2026-09-14T09:00:00.000", priority="high"),
            ev("started", "2026-09-14T09:00:01.500", version=1),
            ev("done", "2026-09-14T09:00:04.000", steps=3),
        ],
    )

    assert [f.kind for f in timeline.frames] == ["queued", "started", "done"]
    assert [round(f.offset_seconds, 3) for f in timeline.frames] == [0.0, 1.5, 4.0]
    assert timeline.duration_seconds == pytest.approx(4.0)
    assert [f.index for f in timeline.frames] == [0, 1, 2]


def test_phases_group_frames_by_what_kind_of_thing_happened():
    timeline = build_timeline(
        "t1",
        [
            ev("started", "2026-09-14T09:00:00.000"),
            ev("action_dispatched", "2026-09-14T09:00:01.000", action="tap"),
            ev("checkpoint_saved", "2026-09-14T09:00:02.000", checkpoint_id="c1"),
            ev("waiting", "2026-09-14T09:00:03.000", action="tap"),
        ],
    )

    phases = [f.phase for f in timeline.frames]
    assert phases == [
        ReplayPhase.LIFECYCLE,
        ReplayPhase.ACTION,
        ReplayPhase.RECOVERY,
        ReplayPhase.CONTROL,
    ]


def test_malformed_timestamp_falls_back_instead_of_breaking_the_timeline():
    """一条坏时间戳不该让整段回放错位——顺序才是回放的价值所在。"""
    timeline = build_timeline(
        "t1",
        [
            ev("queued", "2026-09-14T09:00:00.000"),
            ev("started", ""),  # 缺时间戳
            ev("done", "2026-09-14T09:00:02.000"),
        ],
    )

    offsets = [f.offset_seconds for f in timeline.frames]
    assert offsets == sorted(offsets), "偏移必须单调不减"
    assert timeline.frames[1].offset_seconds == pytest.approx(0.0), "沿用上一个偏移"
    assert timeline.frames[2].offset_seconds == pytest.approx(2.0)


def test_empty_log_yields_empty_timeline():
    timeline = build_timeline("t1", [])
    assert timeline.empty
    assert timeline.duration_seconds == 0.0
    assert "没有事件记录" in timeline.render_markdown()


# ---------------------------------------------------------------- 值得注意的地方


def test_anomalies_surface_preemption_reconciliation_and_failure():
    """一个跑成功的任务没什么好看的，出问题的那几帧才是重点。"""
    timeline = build_timeline(
        "t1",
        [
            ev("queued", "2026-09-14T09:00:00.000"),
            ev("started", "2026-09-14T09:00:00.100"),
            ev("preempt_requested", "2026-09-14T09:00:01.000", by_task_id="t2", by_priority="high", running_priority="normal"),
            ev("suspended", "2026-09-14T09:00:01.200", reason="preemption", step=1),
            ev("recovered", "2026-09-14T09:00:02.000", restored_as="queued(from running)"),
            ev("reconciled", "2026-09-14T09:00:02.500", verdict="retry", reason="页面无变化"),
            ev("failed", "2026-09-14T09:00:03.000", reason="连续失败"),
        ],
    )

    notable = [f.kind for f in timeline.anomalies()]
    assert notable == ["preempt_requested", "suspended", "recovered", "reconciled", "failed"]
    assert timeline.frames[0].notable is False, "正常事件不该被标成异常"


def test_failed_verification_counts_as_notable_even_though_the_kind_is_routine():
    timeline = build_timeline(
        "t1",
        [
            ev("action_verified", "2026-09-14T09:00:01.000", outcome="error", effect="verified_failed"),
            ev("action_verified", "2026-09-14T09:00:02.000", outcome="ok", effect="verified_success"),
        ],
    )

    assert timeline.frames[0].notable is True
    assert timeline.frames[1].notable is False


# ---------------------------------------------------------------- 报告


def test_markdown_report_covers_summary_anomalies_and_timeline():
    timeline = build_timeline(
        "t1",
        [
            ev("started", "2026-09-14T09:00:00.000", version=1, instruction="打开设置"),
            ev(
                "action_dispatched",
                "2026-09-14T09:00:01.000",
                action="tap",
                target={"x": 100, "y": 200},
                risk="dangerous",
                attempt_id="a1",
                step=1,
            ),
            ev("reconciled", "2026-09-14T09:00:02.000", verdict="retry", reason="页面无变化"),
        ],
    )

    report = timeline.render_markdown()

    assert "## 值得注意的地方" in report
    assert "## 时间轴" in report
    assert "动作：1（其中危险动作 1）" in report
    assert "执行 tap → (100,200)［dangerous］" in report
    assert "t+2.000s" in report


def test_summaries_are_human_readable_for_key_events():
    timeline = build_timeline(
        "t1",
        [
            ev("preempt_requested", "2026-09-14T09:00:00.000", by_task_id="t2", by_priority="high", running_priority="normal"),
            ev("waiting", "2026-09-14T09:00:01.000", action="tap", risk="dangerous"),
            ev("reconciled", "2026-09-14T09:00:02.000", verdict="ask_human", reason="无法判断"),
        ],
    )

    summaries = [f.summary for f in timeline.frames]
    assert "t2" in summaries[0] and "抢占" in summaries[0]
    assert "等待人工确认" in summaries[1]
    assert "ask_human" in summaries[2]


# ---------------------------------------------------------------- 动作计划


def test_plan_extracts_actions_and_flags_dangerous_ones():
    timeline = build_timeline(
        "t1",
        [
            ev("action_dispatched", "2026-09-14T09:00:00.000", action="tap", target={"x": 1, "y": 2}, risk="safe", attempt_id="a1", step=1),
            ev("action_verified", "2026-09-14T09:00:00.500", outcome="ok"),
            ev("action_dispatched", "2026-09-14T09:00:01.000", action="tap", target={"x": 3, "y": 4}, risk="dangerous", attempt_id="a2", step=2),
        ],
    )

    plan = build_plan(timeline)

    assert len(plan.steps) == 2, "只统计「发出」的事件，验证不产生新动作"
    assert plan.steps[0].action == "tap" and plan.steps[0].attempt_id == "a1"
    assert not plan.steps[0].dangerous
    assert plan.steps[1].dangerous
    assert len(plan.dangerous_steps()) == 1
    assert "1 个危险动作" in plan.summary()
    assert "→ (3,4)" in plan.steps[1].describe()


# ---------------------------------------------------------------- 重放的安全默认


def test_replay_defaults_to_dry_run_and_executes_nothing(tmp_path):
    store = log(tmp_path)
    store.emit("t1", "action_dispatched", action="tap", target={"x": 1, "y": 2}, risk="safe")

    executed: list = []
    result = replay(store, "t1", execute=executed.append)

    assert result.dry_run is True
    assert result.executed == 0
    assert executed == [], "默认必须一个动作都不执行"
    assert result.plan is not None and len(result.plan.steps) == 1
    assert "未执行任何动作" in result.note


def test_replay_refuses_dangerous_actions_without_explicit_allowance(tmp_path):
    """手机上的动作有真实副作用，重放「提交订单」比不重放危险得多。"""
    store = log(tmp_path)
    store.emit("t1", "action_dispatched", action="tap", target={"x": 9, "y": 9}, risk="dangerous")

    with pytest.raises(ReplayRefused) as excinfo:
        replay(store, "t1", dry_run=False, execute=lambda step: None)

    assert "危险动作" in str(excinfo.value)
    assert "allow_dangerous" in str(excinfo.value)


def test_replay_requires_an_executor_when_not_dry_run(tmp_path):
    store = log(tmp_path)
    store.emit("t1", "action_dispatched", action="tap", risk="safe")

    with pytest.raises(ReplayRefused, match="execute"):
        replay(store, "t1", dry_run=False)


def test_replay_executes_when_fully_authorised(tmp_path):
    store = log(tmp_path)
    store.emit("t1", "action_dispatched", action="tap", target={"x": 1, "y": 1}, risk="safe")
    store.emit("t1", "action_dispatched", action="back", risk="caution")

    seen: list = []
    result = replay(store, "t1", dry_run=False, execute=seen.append)

    assert result.executed == 2
    assert [step.action for step in seen] == ["tap", "back"]


def test_load_timeline_reads_from_the_persisted_log(tmp_path):
    """数据源是落盘的事件日志，不是内存态的轨迹——任务崩溃后才最需要回放。"""
    store = log(tmp_path)
    store.emit("t1", "started", version=1, instruction="打开设置")
    store.emit("t1", "done", steps=1)

    timeline = load_timeline(store, "t1")

    assert [f.kind for f in timeline.frames] == ["started", "done"]
    assert load_timeline(store, "不存在的任务").empty


# ---------------------------------------------------------------- V2.2：新事件类型


def test_new_v22_events_are_rendered_and_flagged():
    """V2.2 新增的三类事件要能在回放里读成人话，并且被标成「值得注意」。

    `goal_rejected` 与 `effect_unknown` 都是「系统差点做错但被拦下」的证据，
    排查时最该先看到它们。
    """
    timeline = build_timeline(
        "t1",
        [
            ev("effect_unknown", "2026-09-14T09:00:00.000", action="tap", reason="拿不到截图"),
            ev("reconciled", "2026-09-14T09:00:01.000", verdict="retry", reason="页面无变化"),
            ev("goal_requested", "2026-09-14T09:00:02.000", reason="我觉得做完了", rejections=0),
            ev("goal_rejected", "2026-09-14T09:00:03.000", reason="计划仍有 2 步未完成"),
            ev("risk_assessed", "2026-09-14T09:00:04.000", effective="dangerous", downgrade_blocked=True),
        ],
    )

    summaries = {frame.kind: frame.summary for frame in timeline.frames}
    assert "效果未知" in summaries["effect_unknown"]
    assert "对账" in summaries["reconciled"]
    assert "申请完成" in summaries["goal_requested"]
    assert "驳回" in summaries["goal_rejected"]
    assert "降级被拒" in summaries["risk_assessed"]

    notable = {frame.kind for frame in timeline.anomalies()}
    assert {"effect_unknown", "goal_rejected"} <= notable

    markdown = timeline.render_markdown()
    assert "effect_unknown" in markdown and "goal_rejected" in markdown


# ---------------------------------------------------------------- V2.2：新事件类型


def test_new_v22_events_are_rendered_and_flagged():
    """V2.2 新增的三类事件要能在回放里读成人话，并且被标成「值得注意」。

    `goal_rejected` 与 `effect_unknown` 都是「系统差点做错但被拦下」的证据，
    排查时最该先看到它们。
    """
    timeline = build_timeline(
        "t1",
        [
            ev("effect_unknown", "2026-09-14T09:00:00.000", action="tap", reason="拿不到截图"),
            ev("reconciled", "2026-09-14T09:00:01.000", verdict="retry", reason="页面无变化"),
            ev("goal_requested", "2026-09-14T09:00:02.000", reason="我觉得做完了", rejections=0),
            ev("goal_rejected", "2026-09-14T09:00:03.000", reason="计划仍有 2 步未完成"),
            ev("risk_assessed", "2026-09-14T09:00:04.000", effective="dangerous", downgrade_blocked=True),
        ],
    )

    summaries = {frame.kind: frame.summary for frame in timeline.frames}
    assert "效果未知" in summaries["effect_unknown"]
    assert "对账" in summaries["reconciled"]
    assert "申请完成" in summaries["goal_requested"]
    assert "驳回" in summaries["goal_rejected"]
    assert "降级被拒" in summaries["risk_assessed"]

    notable = {frame.kind for frame in timeline.anomalies()}
    assert {"effect_unknown", "goal_rejected"} <= notable

    markdown = timeline.render_markdown()
    assert "effect_unknown" in markdown and "goal_rejected" in markdown
