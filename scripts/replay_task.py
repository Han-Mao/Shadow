"""命令行回放一个任务的事件流（V2.1 §二十三）。

用法::

    python scripts/replay_task.py --list              # 列出有事件记录的任务
    python scripts/replay_task.py <task_id>           # 打印人读报告（Markdown）
    python scripts/replay_task.py <task_id> --json    # 输出 JSON（含动作计划）

数据源是落盘的事件日志（`$STORAGE_DIR/events/`，默认 `artifacts/state/events`），
不是内存态的轨迹——因为最需要回放的时刻，恰恰是任务失败或进程崩溃之后。

只读，不会重放任何动作。要真重放请用 `agent.replay.replay()`：
它默认 dry-run，且含危险动作时必须显式放行。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.replay import build_plan, load_timeline  # noqa: E402
from storage.event_log import EventLog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认不是 UTF-8，中文报告会乱码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="回放一个 Shadow 任务的事件流")
    parser.add_argument("task_id", nargs="?", help="任务 id")
    parser.add_argument(
        "--storage-dir",
        default=os.getenv("STORAGE_DIR", "artifacts/state"),
        help="存储目录（默认 artifacts/state）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是 Markdown")
    parser.add_argument("--list", action="store_true", help="列出有事件记录的任务")
    parser.add_argument("--limit", type=int, default=1000, help="最多读取多少条事件")
    args = parser.parse_args(argv)

    # V4 §四：事件已进 SQLite（`<storage>/shadow.db`），直接传存储目录——
    # `EventLog` 会把目录解析成 `<目录>/shadow.db`。旧写法传 `events` 子目录，
    # 会去找 `events/shadow.db` 这个不存在的库，报「还没有事件记录」却指不到原因。
    event_log = EventLog(args.storage_dir)

    if args.list:
        # V4 §四：事件已进 SQLite，"有哪些任务" 直接问存储，不再靠 glob 文件。
        # （旧写法 `event_dir.glob("*.jsonl")` 在迁移之后会列出空集，
        #   而报错信息是「事件目录是空的」——很难联想到「存储换了一代」。）
        task_ids = event_log.task_ids()
        if not task_ids:
            print(f"还没有任何任务的事件记录：{event_log.path}")
            return 1
        for task_id in task_ids:
            print(f"{task_id}  {len(event_log.read(task_id, limit=100000))} 条事件")
        return 0

    if not args.task_id:
        parser.error("需要提供 task_id，或用 --list 先看有哪些任务")

    timeline = load_timeline(event_log, args.task_id, limit=args.limit)
    if timeline.empty:
        print(f"任务 {args.task_id} 没有事件记录（存储：{event_log.path}）")
        return 1

    if args.json:
        payload = {**timeline.to_dict(), "plan": build_plan(timeline).to_dict()}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(timeline.render_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
