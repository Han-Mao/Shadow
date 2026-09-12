"""CheckpointStore：保存与校验任务恢复点（V2 §八）。

关键约定：**不允许盲目恢复**。恢复前必须重新观察、对照恢复点，判不出来就 Re-plan。
"""
from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path

from models.checkpoint import Checkpoint
from models.state import Observation

from .json_store import JsonStore

logger = logging.getLogger(__name__)


class RestoreVerdict(str, Enum):
    RESUME = "resume"
    """页面仍在恢复点那一屏，可以接着往下跑。"""

    REPLAN = "replan"
    """页面已经变了（或丢失恢复点），必须重新规划。"""

    STALE = "stale"
    """恢复点版本与当前任务版本不一致（例如刚发生过 SUPER_TASK / re-plan），
    旧计划对应的旧恢复点绝对不能续用，必须重新规划。"""


class CheckpointStore:
    def __init__(self, root: str | Path) -> None:
        self._store = JsonStore(root)

    @staticmethod
    def _key(task_id: str, checkpoint_id: str) -> str:
        # 文件名带 task_id 前缀，latest_for_task 才能只按前缀扫描，而不用读全部文件
        return f"{task_id}__{checkpoint_id}"

    def save(self, checkpoint: Checkpoint) -> None:
        self._store.write(
            self._key(checkpoint.task_id, checkpoint.id),
            checkpoint.model_dump(mode="json"),
        )

    def load(self, task_id: str, checkpoint_id: str) -> Checkpoint | None:
        payload = self._store.read(self._key(task_id, checkpoint_id))
        if payload is None:
            return None
        try:
            return Checkpoint.model_validate(payload)
        except Exception:
            return None

    def latest_for_task(self, task_id: str) -> Checkpoint | None:
        prefix = f"{task_id}__"
        keys = [k for k in self._store.keys() if k.startswith(prefix)]
        if not keys:
            return None
        # checkpoint id 里带时间戳，字典序即时间序
        for key in sorted(keys, reverse=True):
            payload = self._store.read(key)
            if payload is None:
                continue
            try:
                return Checkpoint.model_validate(payload)
            except Exception:
                continue
        return None

    def validate(
        self,
        checkpoint: Checkpoint | None,
        observation: Observation,
        task_version: int | None = None,
    ) -> RestoreVerdict:
        """判断能否从恢复点继续（V2.1 §十八：版本是第一道闸门）。

        顺序：版本 → 页面（package/activity）。版本不匹配直接 STALE，不比较页面——
        旧计划对应的恢复点即使页面恰好没变，续跑也会用错的目标。
        """
        if checkpoint is None:
            return RestoreVerdict.REPLAN

        if task_version is not None and checkpoint.task_version != task_version:
            logger.info(
                "恢复点 %s 已失效（checkpoint 版本 %s != 任务版本 %s），转入 Re-plan",
                checkpoint.id,
                checkpoint.task_version,
                task_version,
            )
            return RestoreVerdict.STALE

        if checkpoint.same_screen_as(observation):
            logger.info("恢复点 %s 校验通过，继续执行", checkpoint.id)
            return RestoreVerdict.RESUME

        logger.info(
            "恢复点 %s 已失效（当时 %s/%s，现在 %s/%s），转入 Re-plan",
            checkpoint.id,
            checkpoint.package,
            checkpoint.activity,
            observation.package,
            observation.activity,
        )
        return RestoreVerdict.REPLAN

    def delete_for_task(self, task_id: str) -> None:
        prefix = f"{task_id}__"
        for key in [k for k in self._store.keys() if k.startswith(prefix)]:
            self._store.delete(key)
