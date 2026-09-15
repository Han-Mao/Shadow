"""JSON 文件存储基类：线程安全 + 原子写。"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

from models.exceptions import CorruptDataError


def _fsync_directory(directory: Path) -> None:
    """把目录项本身刷盘（rename 的持久化落在目录上，V3.2 §三）。

    POSIX 上要单独打开目录 fsync；Windows 不允许打开目录，会抛 `PermissionError`
    ——那边的原子替换语义由 NTFS 日志保证，我们能做到的只有文件内容的 fsync。

    **刻意吞掉异常**：fsync 失败意味着底层不支持或权限不足，此时应当降级为
    「与升级前一致」，而不是让每一次状态写入都失败。持久化的强度可以差一点，
    但「写不进去就整个任务跑不了」是不可接受的。
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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
        with self._lock:
            self._write_unlocked(key, payload)

    def read(self, key: str) -> Any | None:
        with self._lock:
            return self._read_unlocked(key)

    def update_atomic(self, key: str, mutate: Callable[[Any | None], Any]) -> Any | None:
        """在**同一把锁**里完成「读 → 改 → 写」（V2.5 §一）。

        为什么必须合成一个方法：`read()` 与 `write()` 各自加锁，只能保证「单次」
        操作原子；跨两步的 CAS（读 revision → 比较 → 写 revision+1）中间仍有窗口，
        两个写者可以**都通过比较**，然后后写的静默覆盖先写的。只有把三步放进同一个
        临界区，才叫真正的乐观并发控制。

        `mutate` 拿到当前 payload（键不存在时为 None），返回要写入的新 payload；
        返回 None 表示放弃写入。异常原样抛给调用方（锁由 `with` 释放）。
        """
        with self._lock:
            new_payload = mutate(self._read_unlocked(key))
            if new_payload is None:
                return None
            self._write_unlocked(key, new_payload)
            return new_payload

    def _read_unlocked(self, key: str) -> Any | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            # V2.3：损坏条目必须被看见，而不是假装不存在。
            raise CorruptDataError(key, str(path), f"JSON 解析失败：{exc}") from exc
        except OSError as exc:
            raise CorruptDataError(key, str(path), f"读取失败：{exc}") from exc

    def _write_unlocked(self, key: str, payload: Any) -> None:
        path = self._path(key)
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        tmp = path.with_suffix(".json.tmp")
        # V3.2 §三：先 fsync 临时文件、再 rename、再 fsync 目录。
        #
        # 只 rename 不 fsync 的话，「进程被杀」是安全的——页缓存还在，内容一致。
        # 但「机器掉电」不安全：目录项的新指向可能先落盘、文件内容后落盘，
        # 重启后看到的就是一个新名字下面挂着的空文件 / 半截文件。
        #
        # 这一条对 Task ↔ Checkpoint 这对组合尤其要紧（见 runtime._save_checkpoint
        # 的说明）：它的一致性完全建立在「新名字出现时内容一定已经落盘」上。
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(p.stem for p in self.root.glob("*.json"))

    def delete(self, key: str) -> None:
        path = self._path(key)
        with self._lock:
            path.unlink(missing_ok=True)
