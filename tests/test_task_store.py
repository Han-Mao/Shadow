"""TaskStore：损坏任务的隔离、记账与留痕（V2.4 §十）。

审核的原话是「不能让『数据损坏』表现成『不存在』」——这两种故障的处置完全不同，
所以在存储层就把它们分开：文件移进 `quarantine/`、id 记进 `corrupt_ids`、
事件流里留一条 `TASK_CORRUPTED`。
"""
from __future__ import annotations

from models.task import Task
from storage import EventLog, TaskStore
from storage.event_log import TASK_CORRUPTED


def _write_broken(root, task_id: str, text: str = '{ "instruction": 这不是合法 json') -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{task_id}.json").write_text(text, encoding="utf-8")


def test_corrupt_task_is_quarantined_and_recorded(tmp_path):
    """坏文件要被移进 quarantine/，而且这条 id 必须仍然「看得见」。"""
    root = tmp_path / "tasks"
    store = TaskStore(root)
    _write_broken(root, "t-corrupt")

    assert store.load("t-corrupt") is None
    assert store.is_corrupt("t-corrupt")
    assert "t-corrupt" in store.corrupt_ids()
    assert (root / "quarantine" / "t-corrupt.corrupt.json").exists(), "损坏文件要留在隔离区"
    assert not (root / "t-corrupt.json").exists(), (
        "原文件必须被移走，否则下次 list_all 还会把它当正常条目读一遍"
    )


def test_corrupt_task_emits_event(tmp_path):
    """隔离的同时要在事件流留一条可追溯的事实。

    否则「这条任务去哪了」没人答得上——隔离本身只是把文件挪走，不是解释。
    """
    root = tmp_path / "tasks"
    events = EventLog(tmp_path / "events")
    store = TaskStore(root, event_log=events)
    _write_broken(root, "t-corrupt")

    store.load("t-corrupt")

    assert TASK_CORRUPTED in events.kinds("t-corrupt")
    record = next(e for e in events.read("t-corrupt") if e.kind == TASK_CORRUPTED)
    assert record.data["moved"] is True
    assert "quarantine" in record.data["quarantined_to"]
    assert record.data["reason"], "原因必须落进事件里，否则事后只能重新猜"


def test_corrupt_task_disappears_from_list_all_but_stays_traceable(tmp_path):
    """list_all 里看不到它（反序列化不出来），但 corrupt_ids 必须留着它。"""
    root = tmp_path / "tasks"
    store = TaskStore(root)
    good = Task(instruction="正常的任务")
    store.save(good)
    _write_broken(root, "t-corrupt")

    listed = store.list_all()

    assert [t.id for t in listed] == [good.id]
    assert store.corrupt_ids() == {"t-corrupt"}


def test_quarantine_works_without_event_log(tmp_path):
    """没接事件日志时也要照常隔离——留痕是增益，不是隔离的前提。"""
    root = tmp_path / "tasks"
    store = TaskStore(root)
    _write_broken(root, "t-corrupt")

    assert store.load("t-corrupt") is None
    assert store.is_corrupt("t-corrupt")
    assert (root / "quarantine" / "t-corrupt.corrupt.json").exists()


def test_schema_drift_is_treated_as_corruption(tmp_path):
    """JSON 合法但结构对不上（老版本写下的字段）同样算损坏，不能悄悄消失。"""
    root = tmp_path / "tasks"
    store = TaskStore(root)
    # 合法 JSON，但缺 instruction 等必填字段 → pydantic 校验失败
    _write_broken(root, "t-drift", text='{"id": "t-drift", "unknown_field": 1}')

    assert store.load("t-drift") is None
    assert store.is_corrupt("t-drift")
