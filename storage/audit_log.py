"""请求审计日志（V2.2 §九）。

审核对 API 层的判断是准确的：

> 你的 API 提供的东西非常敏感：`/actions`、`/tasks`、`confirm`、`inject`
> 都可以直接影响真实手机。现在虽然监听 localhost，但部署方式一变风险就很大。

访问控制（`api/auth.py`）回答「谁可以调」，审计日志回答「谁调过什么」。
两者缺一不可——只做鉴权的话，出了事查不到「是哪一次调用让手机点下了付款」。

与 `EventLog` 的关系：EventLog 是**任务视角**（这个任务经历了什么），
审计日志是**调用方视角**（谁在什么时候发了什么请求、被批准还是被拒绝）。
刻意分开存，因为它们的生命周期与查询方式完全不同。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class AuditLog:
    """按天分文件的追加式请求审计。**写失败永不抛异常**——审计是旁路，不能成为故障源。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    def record(self, **fields) -> None:
        fields.setdefault("at", datetime.now().isoformat(timespec="milliseconds"))
        try:
            path = self._root / f"{datetime.now():%Y-%m-%d}.jsonl"
            line = json.dumps(fields, ensure_ascii=False, default=str)
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("写请求审计失败：%s", exc)

    def read(self, day: str | None = None, limit: int = 200) -> list[dict]:
        """读某一天的审计记录（默认今天），最新的在最后。"""
        stamp = day or f"{datetime.now():%Y-%m-%d}"
        path = self._root / f"{stamp}.jsonl"
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读请求审计失败：%s", exc)
            return []

        records: list[dict] = []
        for line in lines[-limit:]:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 被截断的最后一行，跳过
        return records
