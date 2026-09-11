"""历史轨迹与上下文。"""
from __future__ import annotations

from models.state import AgentState, Observation


class Memory:
    """内存中的任务状态仓库。"""

    def __init__(self) -> None:
        self._states: dict[str, AgentState] = {}

    def get(self, task_id: str) -> AgentState | None:
        return self._states.get(task_id)

    def put(self, state: AgentState) -> None:
        self._states[state.task.id] = state

    def append(self, task_id: str, observation: Observation) -> None:
        state = self._states.get(task_id)
        if state is None:
            raise KeyError(f"任务 {task_id} 不存在，无法追加观察")
        state.history.append(observation)
        state.current_step = observation.step
