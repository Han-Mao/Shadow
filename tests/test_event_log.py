"""EventLog：只追加的审计事件流（V2.1 §二十三）。

与 TrajectoryStore 的分工：轨迹服务「下一步决策」（会被裁剪），
事件日志服务「事后追溯」（只追加）。合并会两头不讨好。
"""
from __future__ import annotations

import json

from storage.event_log import EventLog


def test_events_are_appended_in_order(tmp_path):
    log = EventLog(tmp_path / "events")
    log.emit("t1", "queued", priority="high")
    log.emit("t1", "started")
    log.emit("t1", "done", steps=3)

    kinds = log.kinds("t1")
    assert kinds == ["queued", "started", "done"]


def test_events_are_isolated_per_task(tmp_path):
    log = EventLog(tmp_path / "events")
    log.emit("t1", "queued")
    log.emit("t2", "queued")

    assert len(log.read("t1")) == 1
    assert len(log.read("t2")) == 1
    assert log.read("t3") == []


def test_read_honours_limit_and_keeps_the_latest(tmp_path):
    log = EventLog(tmp_path / "events")
    for i in range(10):
        log.emit("t1", f"step_{i}")

    recent = log.read("t1", limit=3)
    assert [e.kind for e in recent] == ["step_7", "step_8", "step_9"]


def test_payload_is_preserved(tmp_path):
    log = EventLog(tmp_path / "events")
    log.emit("t1", "preempt_requested", by_task_id="t2", by_priority="high")

    event = log.read("t1")[0]
    assert event.data["by_task_id"] == "t2"
    assert event.data["by_priority"] == "high"
    assert event.at and event.id


def test_truncated_last_line_is_skipped(tmp_path):
    """进程在写一半时被杀，最后一行可能不完整——不能让它污染整个读取。"""
    root = tmp_path / "events"
    root.mkdir(parents=True)
    (root / "t1.jsonl").write_text(
        json.dumps({"id": "a", "task_id": "t1", "kind": "queued", "at": "", "data": {}}) + "\n"
        + '{"id": "b", "task_id": "t1", "kind": "star',  # 被截断
        encoding="utf-8",
    )

    log = EventLog(root)
    assert log.kinds("t1") == ["queued"]


def test_emit_never_raises_on_unwritable_path(tmp_path):
    """审计日志是旁路，写失败绝不能把任务执行带崩。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("占位：让它当目录用会失败", encoding="utf-8")

    log = EventLog(blocker / "events")
    event = log.emit("t1", "queued")  # 不该抛异常
    assert event.kind == "queued"
    assert log.read("t1") == []
