"""CheckpointStore：保存与校验任务恢复点（V2 §八 · V4 §二）。

关键约定：**不允许盲目恢复**。恢复前必须重新观察、对照恢复点，判不出来就 Re-plan。

V4 §二 起恢复点存进 SQLite（与任务、事件同一个库）：`save()` 能在**与任务指针同一个
事务**里提交（V4 §三），于是「恢复点在盘上、任务指针没提交」这个崩溃窗口从根上消失；
升级前的处置是启动时扫孤儿（`prune_orphans`，仍然保留——历史上留下的孤儿还要清）。

表：
    checkpoints(task_id, checkpoint_id, created_at, payload, PRIMARY KEY(task_id, checkpoint_id))

`checkpoint_id` 里带时间戳，所以按它**倒序**取第一条就是最新的一份——与升级前
「按 key 字典序倒排」等价，但由索引提供而不是自己读遍目录。
"""
from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path

from models.checkpoint import Checkpoint
from models.state import Observation

from .database import Database

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
    def __init__(self, target) -> None:
        """`target` 可以是 `Database`（推荐：与 TaskStore / EventLog 共享一个库与事务）
        或一个路径（目录 → `<目录>/shadow.db`，`*.db` → 用它本身）。"""
        self._db = target if isinstance(target, Database) else Database(target)
        # V4 之前恢复点写在 `<存储目录>/checkpoints/*.json`，key 是 `{task_id}__{checkpoint_id}`
        self._legacy_root = self._db.legacy_dir("checkpoints")
        imported = self._import_legacy_json()
        if imported:
            logger.info("已把 %d 个历史恢复点 JSON 导入 SQLite（%s）", imported, self._db.path)

    # ---- 迁移 ----

    def _import_legacy_json(self) -> int:
        """把旧版 `<存储目录>/checkpoints/*.json` 一次性搬进表（V4 §二）。

        key 的约定是 `{task_id}__{checkpoint_id}`（见升级前的 `_key`），据此反推两列。
        坏 JSON 也照原样搬（payload 存原文），它会在第一次读到时被判为不可用——
        与「数据损坏 ≠ 不存在」同一个口径。
        """
        root = self._legacy_root
        if not root.exists() or self._db.query_one("SELECT 1 FROM checkpoints LIMIT 1") is not None:
            return 0

        imported = 0
        for path in sorted(root.glob("*.json")):
            stem = path.stem
            if "__" not in stem:
                continue
            task_id, _, checkpoint_id = stem.partition("__")
            if not task_id or not checkpoint_id:
                continue
            text = path.read_text(encoding="utf-8")
            created_at = ""
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                raw = None
            if isinstance(raw, dict):
                created_at = str(raw.get("created_at") or "")
            self._db.execute(
                "INSERT OR IGNORE INTO checkpoints (task_id, checkpoint_id, created_at, payload) "
                "VALUES (?, ?, ?, ?)",
                (task_id, checkpoint_id, created_at, text),
            )
            imported += 1
        return imported

    # ---- 读写 ----

    def save(self, checkpoint: Checkpoint) -> None:
        self._db.execute(
            "INSERT INTO checkpoints (task_id, checkpoint_id, created_at, payload) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(task_id, checkpoint_id) DO UPDATE SET "
            "created_at = excluded.created_at, payload = excluded.payload",
            (
                checkpoint.task_id,
                checkpoint.id,
                str(checkpoint.created_at),
                json.dumps(checkpoint.model_dump(mode="json"), ensure_ascii=False),
            ),
        )

    def load(self, task_id: str, checkpoint_id: str) -> Checkpoint | None:
        row = self._db.query_one(
            "SELECT payload FROM checkpoints WHERE task_id = ? AND checkpoint_id = ?",
            (task_id, checkpoint_id),
        )
        if row is None:
            return None
        return self._parse(row["payload"])

    def latest_for_task(self, task_id: str) -> Checkpoint | None:
        """最新的恢复点。`checkpoint_id` 里带时间戳 → 倒序取第一条即最新。

        解析不出来就跳过（继续找更早的那条），与升级前「挨个读、坏的就跳过」一致。
        """
        rows = self._db.query(
            "SELECT payload FROM checkpoints WHERE task_id = ? ORDER BY checkpoint_id DESC",
            (task_id,),
        )
        for row in rows:
            checkpoint = self._parse(row["payload"])
            if checkpoint is not None:
                return checkpoint
        return None

    @staticmethod
    def _parse(payload: str) -> Checkpoint | None:
        try:
            return Checkpoint.model_validate(json.loads(payload))
        except Exception:  # noqa: BLE001 - 坏恢复点当作不可用，不当成故障
            return None

    def validate(
        self,
        checkpoint: Checkpoint | None,
        observation: Observation,
        task_version: int | None = None,
        plan_version: int | None = None,
    ) -> RestoreVerdict:
        """判断能否从恢复点继续（V2.1 §十八：版本是第一道闸门）。

        顺序：task 版本 → plan 版本 → 页面（package/activity）。任一版本不匹配直接
        STALE / REPLAN，不比较页面——旧计划对应的恢复点即使页面恰好没变，
        续跑也会用错的目标或步骤。

        `plan_version` 门控（V2.7 P1-9）：计划被改（插入子任务 / Re-plan）之后，
        恢复点里 `action_attempt_id` 对应的是旧计划的步骤编排；即使目标没变、页面没变，
        沿用旧恢复点也会把「旧计划的第 N 步」接到「新计划的第 N 步」上。所以计划版本
        不一致一律判 STALE，交给 `_prepare` 重新规划。之前是「只记录不门控」的取舍，
        审查指出这会埋下「旧动作 + 新计划」的错配，这里改回门控。
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

        if plan_version is not None and checkpoint.plan_version != plan_version:
            logger.info(
                "恢复点 %s 已失效（checkpoint 计划版本 %s != 任务计划版本 %s），转入 Re-plan",
                checkpoint.id,
                checkpoint.plan_version,
                plan_version,
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

    # ---- 维护 ----

    def delete_for_task(self, task_id: str) -> None:
        self._db.execute("DELETE FROM checkpoints WHERE task_id = ?", (task_id,))

    def keys(self) -> list[str]:
        """全部恢复点的 `{task_id}__{checkpoint_id}` 键（诊断与测试用）。"""
        return [
            f"{row['task_id']}__{row['checkpoint_id']}"
            for row in self._db.query("SELECT task_id, checkpoint_id FROM checkpoints")
        ]

    def prune_orphans(self, committed: dict[str, str]) -> int:
        """删掉没被任何任务**提交过**的恢复点，返回删除条数（V3.2 §三）。

        `committed` 是 `{task_id: checkpoint_id}`，即每个任务指针里真正记着的那个。

        为什么会有孤儿：`runtime._save_checkpoint` 是三步——① 写 checkpoint、
        ② 改内存里的指针、③ 落盘任务。进程在 ③ 之前崩溃，恢复点已经在库里、指针却
        没被提交。它不是「数据不一致」（任务不会指向一个不存在/不完整的恢复点，
        原因见那里的说明），而是**没人认领的记录**：恢复流程不会用它，却会让
        `GET /tasks/{id}/checkpoint` 显示一个任务从未提交过的恢复点。

        V4 §三 之后这件事有了更彻底的解法：`save()` 与任务指针写在**同一个事务**里，
        新产生的孤儿窗口从根上消失。这个扫描保留下来清理**历史遗留**的孤儿。

        哪些情况会被删：孤儿的崩溃残留，以及**被新恢复点取代的旧恢复点**。
        为什么旧恢复点也可以删——今天没有任何代码路径会读「非指针指向的恢复点」：
        `load()` 只用指针里的 id，`latest_for_task()` 只被 `GET /tasks/{id}/checkpoint`
        与演示脚本用到，而它们要的本来就是「任务提交过的那个」。

        **如果将来实现「回滚到更早的恢复点」，这个扫描必须改成按引用计数**，
        否则会把回滚目标删掉。这句话就是那条改动的触发条件。

        调用时机：启动恢复时、worker 起来之前扫一次即可（`Scheduler.recover()`）。
        运行期不需要——运行期产生的孤儿只有崩溃才会留下。
        """
        keep = {
            f"{task_id}__{checkpoint_id}"
            for task_id, checkpoint_id in committed.items()
            if checkpoint_id
        }
        orphans = [key for key in self.keys() if key not in keep]
        for key in orphans:
            task_id, _, checkpoint_id = key.partition("__")
            self._db.execute(
                "DELETE FROM checkpoints WHERE task_id = ? AND checkpoint_id = ?",
                (task_id, checkpoint_id),
            )
        if orphans:
            logger.info("清理 %d 个未被任何任务提交的恢复点", len(orphans))
        return len(orphans)
