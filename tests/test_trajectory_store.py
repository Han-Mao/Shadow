"""TrajectoryStore 落盘（V2.1）。

轨迹落盘要解决的是一个很具体的问题：长跑任务重启后**失忆**——
恢复点还在，但「我前面几步干了什么」全丢了，模型只能盯着当前一屏重新猜，
跟从零开始差不多。

两个取舍需要被钉住，否则很容易被后来者「顺手优化」掉：
1. `ui_tree` 不落盘（最大字段，而决策根本不读它）
2. JSONL 追加 + 定期紧凑化（每步重写整个文件是 O(n²)）
"""
from __future__ import annotations

import json

from models.state import Observation
from storage.trajectory_store import TrajectoryStore


def obs(step: int, *, ui_tree: str | None = None) -> Observation:
    return Observation(
        step=step,
        screenshot_path=f"/shots/{step:03d}.png",
        package="com.demo",
        activity=".Main",
        ui_tree=ui_tree,
    )


def test_trajectory_survives_restart(tmp_path):
    root = tmp_path / "trajectories"
    first = TrajectoryStore(root=root)
    first.append("t1", obs(1))
    first.append("t1", obs(2))

    # 新实例 = 进程重启
    restarted = TrajectoryStore(root=root)

    assert [o.step for o in restarted.history("t1")] == [1, 2]
    assert restarted.last_step("t1") == 2


def test_prompt_context_works_after_restart(tmp_path):
    """恢复上下文才是落盘的主要目的：重启后模型还能看到前面几步。"""
    root = tmp_path / "trajectories"
    first = TrajectoryStore(root=root)
    first.append("t1", obs(1))
    first.append("t1", obs(2))

    context = TrajectoryStore(root=root).prompt_context("t1", last_n=5)

    assert [entry["step"] for entry in context] == [1, 2]
    assert all("screenshot_path" not in entry for entry in context)


def test_ui_tree_is_deliberately_not_persisted(tmp_path):
    """ui_tree 是单条记录里最大的字段，而决策读的是 `to_prompt_dict()`（白名单里没有它）。

    存它只会把磁盘撑爆。要看页面有截图路径，或 Checkpoint 里的 UI 快照。
    """
    root = tmp_path / "trajectories"
    bulky = "<hierarchy>" + "x" * 5000
    TrajectoryStore(root=root).append("t1", obs(1, ui_tree=bulky))

    raw = (root / "t1.jsonl").read_text(encoding="utf-8")
    assert "x" * 200 not in raw, "大块 ui_tree 不该出现在磁盘上"

    restored = TrajectoryStore(root=root).history("t1")[0]
    assert restored.ui_tree is None
    assert restored.screenshot_path == "/shots/001.png", "其余字段要完整保留"
    assert restored.package == "com.demo"


def test_in_memory_window_trims_and_disk_reads_back_the_tail(tmp_path):
    root = tmp_path / "trajectories"
    store = TrajectoryStore(max_entries=3, root=root)
    for step in range(1, 8):
        store.append("t1", obs(step))

    assert [o.step for o in store.history("t1")] == [5, 6, 7], "内存里只留最近 N 条"

    restarted = TrajectoryStore(max_entries=3, root=root)
    assert [o.step for o in restarted.history("t1")] == [5, 6, 7], "重启后读回的也是最近 N 条"


def test_file_is_compacted_so_it_does_not_grow_forever(tmp_path):
    """每步重写整个文件是 O(n²)，所以用追加；追加会无限增长，所以定期紧凑化。"""
    root = tmp_path / "trajectories"
    store = TrajectoryStore(max_entries=4, root=root)
    for step in range(1, 40):
        store.append("t1", obs(step))

    lines = (root / "t1.jsonl").read_text(encoding="utf-8").strip().splitlines()

    assert len(lines) <= 2 * 4, f"文件应被紧凑化，实际 {len(lines)} 行"
    assert [o.step for o in store.history("t1")] == [36, 37, 38, 39]


def test_truncated_line_is_skipped(tmp_path):
    """进程在写一半时被杀，最后一行可能不完整——不能让它污染整段轨迹。"""
    root = tmp_path / "trajectories"
    root.mkdir(parents=True)
    (root / "t1.jsonl").write_text(
        json.dumps({"step": 1, "screenshot_path": "/a.png"}) + "\n"
        + '{"step": 2, "screenshot' + "\n"
        + json.dumps({"step": 3, "screenshot_path": "/c.png"}) + "\n",
        encoding="utf-8",
    )

    assert [o.step for o in TrajectoryStore(root=root).history("t1")] == [1, 3]


def test_drop_clears_memory_and_file(tmp_path):
    root = tmp_path / "trajectories"
    store = TrajectoryStore(root=root)
    store.append("t1", obs(1))
    assert (root / "t1.jsonl").exists()

    store.drop("t1")

    assert not (root / "t1.jsonl").exists()
    assert store.history("t1") == []


def test_tasks_are_isolated(tmp_path):
    root = tmp_path / "trajectories"
    store = TrajectoryStore(root=root)
    store.append("t1", obs(1))
    store.append("t2", obs(9))

    restarted = TrajectoryStore(root=root)
    assert [o.step for o in restarted.history("t1")] == [1]
    assert [o.step for o in restarted.history("t2")] == [9]
    assert restarted.history("t3") == []


def test_memory_only_mode_is_unchanged(tmp_path):
    """不传 root 时行为与落盘前完全一致（纯内存），不影响任何既有用法。"""
    store = TrajectoryStore()
    store.append("t1", obs(1))

    assert store.last_step("t1") == 1
    assert store.prompt_context("t1")[0]["step"] == 1
    assert store.observations_at("t1", 1)[0].package == "com.demo"
    assert store.history("不存在的任务") == []


def test_reload_forces_a_reread_from_disk(tmp_path):
    root = tmp_path / "trajectories"
    writer = TrajectoryStore(root=root)
    writer.append("t1", obs(1))

    reader = TrajectoryStore(root=root)
    assert reader.history("t1")  # 触发一次懒加载

    writer.append("t1", obs(2))
    assert reader.last_step("t1") == 1, "内存里有副本就不会再读盘"
    assert reader.reload("t1") == 2
    assert reader.last_step("t1") == 2
