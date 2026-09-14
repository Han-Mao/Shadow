"""JSON 文件存储基类：线程安全 + 原子写。"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from models.exceptions import CorruptDataError


class JsonStore:
    """把每个实体存成 `<root>/<key>.json`。

    原子写（临时文件 + os.replace）：服务在写一半时被杀掉，也不会留下半截 JSON
    把整个任务恢复链带崩。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # 后台 worker 与 API 线程都会读写同一份状态
        self._lock = threading.RLock()

    def _path(self, key: str) -> Path:
        # 防目录穿越：key 只允许是文件名
        safe = os.path.basename(str(key)).strip() or "unnamed"
        return self.root / f"{safe}.json"

    def write(self, key: str, payload: Any) -> None:
        path = self._path(key)
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        tmp = path.with_suffix(".json.tmp")
        with self._lock:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)

    def read(self, key: str) -> Any | None:
        path = self._path(key)
        with self._lock:
            if not path.exists():
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                # V2.3：损坏条目必须被看见，而不是假装不存在。
                raise CorruptDataError(key, str(path), f"JSON 解析失败：{exc}") from exc
            except OSError as exc:
                raise CorruptDataError(key, str(path), f"读取失败：{exc}") from exc

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(p.stem for p in self.root.glob("*.json"))

    def delete(self, key: str) -> None:
        path = self._path(key)
        with self._lock:
            path.unlink(missing_ok=True)
