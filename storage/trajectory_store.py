"""TrajectoryStore：任务轨迹（观察序列）。

只保留内存态 + 可选落盘：轨迹主要服务于「下一步决策要带多少上下文」，
不是审计日志，没必要每步都刷磁盘。
"""
from __future__ import annotations

import threading

from models.state import Observation, compact_observations

# 每个任务最多保留的轨迹条数，防止长跑任务无限累积
MAX_TRAJECTORY = 200


class TrajectoryStore:
    def __init__(self, max_entries: int = MAX_TRAJECTORY) -> None:
        self._entries: dict[str, list[Observation]] = {}
        self._lock = threading.RLock()
        self._max_entries = max_entries

    def append(self, task_id: str, observation: Observation) -> None:
        with self._lock:
            bucket = self._entries.setdefault(task_id, [])
            bucket.append(observation)
            if len(bucket) > self._max_entries:
                del bucket[: len(bucket) - self._max_entries]

    def extend(self, task_id: str, observations: list[Observation]) -> None:
        for observation in observations:
            self.append(task_id, observation)

    def history(self, task_id: str) -> list[Observation]:
        with self._lock:
            return list(self._entries.get(task_id, []))

    def tail(self, task_id: str, count: int = 5) -> list[Observation]:
        with self._lock:
            return list(self._entries.get(task_id, [])[-count:])

    def prompt_context(self, task_id: str, last_n: int = 5) -> list[dict]:
        return compact_observations(self.tail(task_id, last_n), last_n)

    def last_step(self, task_id: str) -> int:
        with self._lock:
            bucket = self._entries.get(task_id, [])
            return bucket[-1].step if bucket else 0

    def observations_at(self, task_id: str, step: int) -> list[Observation]:
        with self._lock:
            return [o for o in self._entries.get(task_id, []) if o.step == step]

    def drop(self, task_id: str) -> None:
        with self._lock:
            self._entries.pop(task_id, None)
