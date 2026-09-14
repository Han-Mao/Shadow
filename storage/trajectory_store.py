"""TrajectoryStore：任务轨迹（观察序列）。

轨迹服务的是「下一步决策要带多少上下文」，不是审计日志——
所以内存里只留最近 `MAX_TRAJECTORY` 条，超了就从前面裁掉。

V2.1：补上**落盘**。之前它纯内存，进程一重启轨迹就没了，
长跑任务恢复后等于「失忆」——恢复点还在，但「我前面几步干了什么」全丢了，
模型只能看当前一屏重新猜，这跟从零开始没差多少。

落盘的两个取舍：

1. **不存 `ui_tree`**。它是单条观察里最大的字段（几十 KB），
   而决策**根本不读它**（进 prompt 的是 `to_prompt_dict()`，字段白名单见
   `models.state.PROMPT_FIELDS`）。存下来只是把磁盘撑爆。
   要看页面内容有两条路：截图（`screenshot_path` 已存）或 Checkpoint 里的 UI 快照。
   实测存一条记录从几十 KB 降到几百字节。

2. **用 JSONL 追加，而不是整文件重写**。每步都重写整个文件是 O(n²)，
   长跑任务越跑越慢。代价是文件会无限增长，所以定期「紧凑化」：
   每追加 `max_entries` 条就把文件重写成最后 `max_entries` 条（摊销成本极低）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from pydantic import ValidationError

from models.state import Observation, compact_observations

logger = logging.getLogger(__name__)

# 每个任务最多保留的轨迹条数，防止长跑任务无限累积
MAX_TRAJECTORY = 200


class TrajectoryStore:
    def __init__(
        self,
        max_entries: int = MAX_TRAJECTORY,
        root: str | Path | None = None,
    ) -> None:
        self._entries: dict[str, list[Observation]] = {}
        self._lock = threading.RLock()
        self._max_entries = max_entries
        self._root = Path(root) if root is not None else None
        # 每个任务自上次紧凑化以来追加了多少条
        self._appends_since_compact: dict[str, int] = {}

    # ---- 写入 ----

    def append(self, task_id: str, observation: Observation) -> None:
        with self._lock:
            bucket = self._entries.setdefault(task_id, [])
            bucket.append(observation)
            if len(bucket) > self._max_entries:
                del bucket[: len(bucket) - self._max_entries]
            self._append_to_disk(task_id, observation)

    def extend(self, task_id: str, observations: list[Observation]) -> None:
        for observation in observations:
            self.append(task_id, observation)

    # ---- 读取 ----

    def history(self, task_id: str) -> list[Observation]:
        with self._lock:
            return list(self._ensure_loaded(task_id))

    def tail(self, task_id: str, count: int = 5) -> list[Observation]:
        with self._lock:
            return list(self._ensure_loaded(task_id)[-count:])

    def prompt_context(self, task_id: str, last_n: int = 5) -> list[dict]:
        return compact_observations(self.tail(task_id, last_n), last_n)

    def last_step(self, task_id: str) -> int:
        with self._lock:
            bucket = self._ensure_loaded(task_id)
            return bucket[-1].step if bucket else 0

    def observations_at(self, task_id: str, step: int) -> list[Observation]:
        with self._lock:
            return [o for o in self._ensure_loaded(task_id) if o.step == step]

    def drop(self, task_id: str) -> None:
        with self._lock:
            self._entries.pop(task_id, None)
            self._appends_since_compact.pop(task_id, None)
            path = self._path(task_id)
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:  # noqa: BLE001 - 删不掉不该影响业务流程
                    logger.warning("删除轨迹文件失败（任务 %s）：%s", task_id, exc)

    def reload(self, task_id: str) -> int:
        """丢弃内存副本、强制从磁盘重读。返回读到的条数。"""
        with self._lock:
            self._entries.pop(task_id, None)
            return len(self._ensure_loaded(task_id))

    # ---- 落盘 ----

    def _path(self, task_id: str) -> Path | None:
        if self._root is None:
            return None
        # 防目录穿越：task_id 只允许当文件名用
        safe = os.path.basename(str(task_id)).strip() or "unnamed"
        return self._root / f"{safe}.jsonl"

    @staticmethod
    def _to_record(observation: Observation) -> dict:
        record = observation.model_dump(mode="json")
        # 见模块 docstring：ui_tree 最大且决策不读，落盘时丢掉
        record["ui_tree"] = None
        return record

    def _append_to_disk(self, task_id: str, observation: Observation) -> None:
        path = self._path(task_id)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(self._to_record(observation), ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - 轨迹是旁路，写失败绝不能影响执行
            logger.warning("写轨迹失败（任务 %s）：%s", task_id, exc)
            return

        pending = self._appends_since_compact.get(task_id, 0) + 1
        if pending >= self._max_entries:
            self._appends_since_compact[task_id] = 0
            self._compact(task_id, path)
        else:
            self._appends_since_compact[task_id] = pending

    def _compact(self, task_id: str, path: Path) -> None:
        """把文件重写成最后 `max_entries` 条，避免无限增长。

        摊销成本很低：每追加 `max_entries` 条才重写一次，
        而重写的内容本身也只有 `max_entries` 条。
        """
        try:
            recent = self._read_file(task_id, self._max_entries)
            tmp = path.with_suffix(".jsonl.tmp")
            body = "".join(
                json.dumps(self._to_record(item), ensure_ascii=False) + "\n" for item in recent
            )
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001 - 紧凑化失败不影响正确性，只是文件大一点
            logger.warning("轨迹紧凑化失败（任务 %s）：%s", task_id, exc)

    def _read_file(self, task_id: str, limit: int) -> list[Observation]:
        path = self._path(task_id)
        if path is None or not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:  # noqa: BLE001
            logger.warning("读轨迹失败（任务 %s）：%s", task_id, exc)
            return []

        records: list[Observation] = []
        for line in lines[-limit:]:
            if not line.strip():
                continue
            try:
                records.append(Observation.model_validate(json.loads(line)))
            except (json.JSONDecodeError, ValidationError):
                # 被截断的最后一行、或旧版本留下的坏记录：跳过，不能让整段轨迹作废
                continue
        return records

    def _ensure_loaded(self, task_id: str) -> list[Observation]:
        """内存里没有就从磁盘恢复——进程重启后的第一次访问走的就是这里。

        结果会被缓存（包括「确实没有轨迹」这种情况），避免每次读盘。
        """
        bucket = self._entries.get(task_id)
        if bucket is not None:
            return bucket
        bucket = self._read_file(task_id, self._max_entries) if self._root else []
        self._entries[task_id] = bucket
        return bucket
