"""TaskStore：任务的生命周期持久化。"""
from __future__ import annotations

from pathlib import Path

from models.task import Task, TaskStatus

from .json_store import JsonStore


class TaskStore:
    def __init__(self, root: str | Path) -> None:
        self._store = JsonStore(root)

    def save(self, task: Task) -> None:
        self._store.write(task.id, task.model_dump(mode="json"))

    def load(self, task_id: str) -> Task | None:
        payload = self._store.read(task_id)
        if payload is None:
            return None
        try:
            return Task.model_validate(payload)
        except Exception:
            # 老版本写下的结构可能在升级后校验失败，当作不存在处理而不是崩掉服务
            return None

    def list_all(self) -> list[Task]:
        tasks = [t for t in (self.load(key) for key in self._store.keys()) if t is not None]
        return sorted(tasks, key=lambda t: t.created_at)

    def list_active(self) -> list[Task]:
        return [t for t in self.list_all() if not t.is_terminal]

    def delete(self, task_id: str) -> None:
        self._store.delete(task_id)

    def mark_cancelled(self, task_id: str) -> Task | None:
        task = self.load(task_id)
        if task is None:
            return None
        task.mark(TaskStatus.CANCELLED)
        self.save(task)
        return task
