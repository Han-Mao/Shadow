"""历史轨迹与上下文。"""
from __future__ import annotations

import threading

from models.state import AgentState, Observation
from models.task import TaskStatus


class Memory:
    """线程安全的任务状态仓库。

    FastAPI 的同步端点跑在线程池里，后台任务又跑在独立线程中，而两边读写的是
    同一份 AgentState，因此所有访问都在同一把可重入锁内完成。
    """

    def __init__(self) -> None:
        self._states: dict[str, AgentState] = {}
        self._lock = threading.RLock()

    def get(self, task_id: str) -> AgentState | None:
        with self._lock:
            return self._states.get(task_id)

    def put(self, state: AgentState) -> None:
        with self._lock:
            self._states[state.task.id] = state

    def drop(self, task_id: str) -> None:
        with self._lock:
            self._states.pop(task_id, None)

    def append(self, task_id: str, observation: Observation) -> None:
        with self._lock:
            state = self._states.get(task_id)
            if state is None:
                raise KeyError(f"任务 {task_id} 不存在，无法追加观察")
            state.history.append(observation)
            state.current_step = observation.step

    def dump(self, task_id: str) -> dict | None:
        """锁内快照。

        直接 get() 之后再序列化并不安全：后台线程可能在序列化途中改写同一个
        AgentState，产出自相矛盾的响应。
        """
        with self._lock:
            state = self._states.get(task_id)
            return state.model_dump() if state is not None else None

    def observations(self, task_id: str, step: int) -> list[Observation]:
        with self._lock:
            state = self._states.get(task_id)
            if state is None:
                return []
            return [o for o in state.history if o.step == step]

    def is_terminal(self, task_id: str) -> bool:
        """任务是否已进入终态（后台模式轮询用）。"""
        with self._lock:
            state = self._states.get(task_id)
            if state is None:
                return False
            return state.task.status in {TaskStatus.DONE, TaskStatus.FAILED}
