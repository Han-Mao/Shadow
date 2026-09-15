"""事件日志（V2.1 §二十三）。

轨迹（`TrajectoryStore`）记的是「每一步看到了什么」——为**下一步决策**服务，
所以只保留最近几条，而且会被裁剪。

事件日志记录的是「这个任务发生过什么」——为**审计与重放**服务，
所以只追加、不裁剪，并且要能事后回答「它为什么会被抢占」「为什么会失败」。
两者用途不同，不要合并成一个。

格式选 JSONL 而不是单个 JSON 数组：追加不需要「读出来改完再整个写回」，
进程在写一半时被杀也只丢最后一行，不会损坏已有记录。

## 一致性模型（V3.3 修复轮 §一/§二：写清楚它到底保证什么）

这份日志**不是**一个数据库，能力边界必须写明白，否则「durable」会被理解错：

| 保证 | 成立吗 | 靠什么 |
|---|---|---|
| 单条事件不撕裂（不会读到半行） | ✅ | `O_APPEND` + **单次 `os.write`** |
| 安全关键事件落盘（掉电后仍在） | ✅ | `emit_critical` 里 `fsync(文件)` |
| 同进程内多条事件顺序 = 调用顺序 | ✅ | 进程内 `threading.Lock` + 单次 write |
| **跨进程**事件顺序（全局单调 id） | ❌ | 没有跨进程锁；多进程各写各的行 |
| 跨进程互斥（同一 jti 只用一次那种语义） | ❌ | 那类语义由 SQLite 承担（confirmation / lease） |

也就是说：**多进程部署下这里保证的是「不撕裂」，不是「串行化」**。
事件流里不要假设「先写的一定排在前面」——需要跨进程全序时，正解是换
SQLite 的 `events` 表（见 README 的 V4 Storage Refactor），
而不是继续在这个文件格式上加锁。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from models.exceptions import PersistenceError

logger = logging.getLogger(__name__)

# 事件类型集中定义，避免字符串散落各处后拼写漂移、事后查不到
CREATED = "created"
QUEUED = "queued"
STARTED = "started"
ACTION_DISPATCHED = "action_dispatched"
ACTION_VERIFIED = "action_verified"
CHECKPOINT_SAVED = "checkpoint_saved"
RECONCILED = "reconciled"
WAITING = "waiting"
CONFIRMED = "confirmed"
PREEMPT_REQUESTED = "preempt_requested"
SUSPENDED = "suspended"
RESUMED = "resumed"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
RECOVERED = "recovered"

# V2.3：数据损坏必须被看见，而不是 404。
TASK_CORRUPTED = "task_corrupted"

# ---- V2.2 新增：把「谁做的决定」也变成可追溯事实 ----

GOAL_REQUESTED = "goal_requested"
"""模型申请完成（DONE_REQUEST）。申请 ≠ 完成。"""

GOAL_CONFIRMED = "goal_confirmed"
"""目标验证器确认完成（附独立证据）。"""

GOAL_REJECTED = "goal_rejected"
"""目标验证器**驳回**完成申请——声称完成与可核验事实矛盾。"""

EFFECT_UNKNOWN = "effect_unknown"
"""动作已发出但效果无法确认（拿不到验证观察）。下一个安全点要对账。"""

RISK_ASSESSED = "risk_assessed"
"""风险门禁的判定结论，含「模型试图降级被拒」这种要留痕的情况。"""

OBSERVATION_STALE = "observation_stale"
"""V3.1 P1-6：决策所依据的那一屏在执行前已经变了，本次动作被放弃、改为重新观察。

它记录的是**一次被避免的 TOCTOU**：拿 A 屏算出来的坐标去点 B 屏。
刻意**不**放进 `SAFETY_CRITICAL_KINDS`——这条事件丢失只会少一条解释，
而它对应的行为（拒绝执行）本身仍然是最安全的那一侧。
"""


# ---- V3 M4：安全关键事件 fail-safe ----
#
# v2.9 P1 §八：EventLog 是 fail-open——写失败只 warning 后继续执行。
# 对普通业务日志没问题，但这个日志同时承担「审计 + 回放 + 安全决策追踪」。
# 最坏情形是：手机实际发生了转账，但 EventLog 没记录，事后「发生了什么 / 谁批准」无法追溯。
#
# 所以安全关键事件要 fail-safe：写不进 durable store，副作用就不该继续。
# 这些事件一旦丢失，审计链就断了：
SAFETY_CRITICAL_KINDS = frozenset(
    {
        ACTION_DISPATCHED,  # 动作已经要发出去了，这是「发生了什么」的最后一处记录点
        RISK_ASSESSED,      # 风险怎么判的、模型有没有试图降级
        CONFIRMED,          # 谁批准了危险动作 / 恢复 / 完成
        GOAL_CONFIRMED,     # 谁认定任务完成
    }
)


def is_safety_critical(kind: str) -> bool:
    """这条事件是否属于「丢失即审计链断裂」的安全关键事件。"""
    return kind in SAFETY_CRITICAL_KINDS


@dataclass
class Event:
    task_id: str
    kind: str
    data: dict = field(default_factory=dict)
    # 毫秒精度：回放要看「两个事件之间隔了多久」（比如抢占请求到真正让出），
    # 秒级粒度会把这类分析糊掉
    at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="milliseconds"))
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "kind": self.kind,
            "at": self.at,
            "data": self.data,
        }


def _encode(event: Event) -> bytes:
    """一行 JSONL（含换行），**一次编码完**。

    先编码再写，是为了让「写」这一步只有一次 `os.write`——见 `_append_line`。
    """
    return (json.dumps(event.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")


def _fsync_dir_best_effort(directory: Path) -> None:
    """把目录项本身刷盘，失败就算了。

    与 `JsonStore._fsync_directory` 同义，但**刻意不复用**：两者的失败策略不同。
    那边任何时候都吞异常（降级是可接受的），这里只在「新建了日志文件」这一种情形
    下调用——文件内容已经由 `os.fsync(fd)` 兜住，目录项丢失只影响「名字可见性」。
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


class EventLog:
    """追加式事件日志，每个任务一个 `.jsonl` 文件。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    def emit(self, task_id: str, kind: str, **data) -> Event:
        """写一条事件。**按 kind 自动分级**（V3.1 P0）。

        - 普通事件 → fail-open：审计是旁路，写失败只 warning，不挡执行。
        - 安全关键事件（`is_safety_critical(kind)` 为真）→ fail-closed：
          写失败抛 `PersistenceError`，调用方必须中止后续副作用。

        分派刻意放在 `emit` **内部**，而不是靠调用方自觉去选 `emit_critical`。
        V3 M4 定义了 `SAFETY_CRITICAL_KINDS` 却只让 `ACTION_DISPATCHED` 一处走
        `emit_critical`，`RISK_ASSESSED` / `CONFIRMED` / `GOAL_CONFIRMED` 仍走这里
        的 fail-open 分支——常量表说它们是「丢失即审计链断裂」，真实行为却是
        「写不进去也照跑」。「约定调用方记得选对方法」这种事早晚会漏，
        所以让唯一的写入口自己按 kind 决定。

        报错方式（`PersistenceError`）与 `Runtime._degrade` / TaskManager 的失败分流
        完全一致：安全事件写不进 = durable state 落后于现实，任务停在 DEGRADED，
        而不是带着「转账了但没有授权记录」继续跑。
        """
        if is_safety_critical(kind):
            return self.emit_critical(task_id, kind, **data)

        event = Event(task_id=task_id, kind=kind, data=data)
        try:
            with self._lock:
                # durable=False：普通事件是**旁路**。它是审计的补充，不是副作用的
                # 前置条件，所以不为它付 fsync 的代价（那会把热路径拖慢，
                # 而且没有任何东西会因为这条事件没落盘而变危险）。
                self._append_line(task_id, _encode(event), durable=False)
        except Exception as exc:  # noqa: BLE001 - 普通事件是旁路，不能成为故障源
            logger.warning("写事件日志失败（任务 %s / %s）：%s", task_id, kind, exc)
        return event

    def emit_critical(self, task_id: str, kind: str, **data) -> Event:
        """写一条**安全关键**事件。写失败抛 `PersistenceError`（V3 M4）。

        这是 `emit` 在 `is_safety_critical(kind)` 为真时的实际执行体；
        也可以被显式调用，用来强制让一个**不在** `SAFETY_CRITICAL_KINDS` 里的事件
        走 fail-closed（例如未来新增的审计关键事件先上线、再补进常量表）。

        与 `emit` 的区别：`emit` 是旁路（fail-open，写不进不挡执行）；
        安全关键事件（危险动作已 dispatch、风险判定、人工批准、完成认定）一旦
        丢失，审计链就断了——「手机转账了但没记录」不可接受。所以这里写失败要
        **抛出去**，由调用方（runtime）把任务降级、副作用不继续。

        V3.3 §一：`durable` 的含义也是在这里被写实的。以前这里只做到「`write` 返回」，
        而 `write` 返回只代表「进了内核 page cache」——「手机已经点了付款 + 进程认为
        事件已记录 + 掉电」这三件事叠起来，日志可能根本不在盘上。现在这条路是
        `单次 write → fsync → 返回`，`fsync` 失败即抛（不 durable 就不放行）。

        注意：普通事件仍走 `emit`（fail-open），只有明确的安全关键事件才走这里。
        分级而不是一刀切，避免「审计日志抖动就把所有任务都降级」。
        """
        event = Event(task_id=task_id, kind=kind, data=data)
        try:
            with self._lock:
                self._append_line(task_id, _encode(event), durable=True)
        except Exception as exc:  # noqa: BLE001 - 转换后重新抛出
            raise PersistenceError(task_id, f"安全关键事件 {kind} 写盘失败：{exc}") from exc
        return event

    # ---- 写入原语 ----

    def _append_line(self, task_id: str, payload: bytes, *, durable: bool) -> None:
        """把一行追加进 `<task_id>.jsonl`。

        为什么不用 `path.open("a")` 那套文本层写法：`write` 返回只说明「进了用户态
        缓冲」，离「持久」还差两层（Python 缓冲 + 内核 page cache）；而且文本层无法
        保证跨进程时一次 append 不被切开。这里改成：

            os.open(O_APPEND | O_CREAT | O_WRONLY) → 单次 os.write → （可选）os.fsync

        - `O_APPEND` + **单次** `write`：内核把「定位到文件尾」与「写入」做成一次原子
          操作，所以多进程同时追加时行与行不会交错（一行远小于一次 write 的原子粒度）。
          它给的是**不撕裂**，不是**串行化**——跨进程的先后顺序仍不做承诺，
          详见模块 docstring 的一致性模型表。
        - `durable=True`：再 `fsync`，把内核页缓存压到盘上。
        - `fsync` 失败**必须抛**：它意味着这次写入不 durable，而安全关键事件的契约
          就是「不 durable 就不放行」。这与 `JsonStore` 那边「fsync 失败就降级为
          升级前行为」是**相反**的取舍，因为状态写入迟早会再发生一次，
          而「已发生的副作用」没有第二次机会。
        """
        path = self._root / f"{task_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, payload)
            if durable:
                os.fsync(fd)
        finally:
            os.close(fd)
        if durable and not existed:
            # 新建文件时目录项也值得刷一次（掉电可能「内容在、名字没了」）。
            # 文件本来就存在时目录项早已稳定，跳过。
            _fsync_dir_best_effort(path.parent)

    def read(self, task_id: str, limit: int = 200) -> list[Event]:
        """按时间顺序读取最近的事件。"""
        path = self._root / f"{task_id}.jsonl"
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读事件日志失败（任务 %s）：%s", task_id, exc)
            return []

        events: list[Event] = []
        for line in lines[-limit:]:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue  # 被截断的最后一行，跳过即可
            events.append(
                Event(
                    task_id=raw.get("task_id", task_id),
                    kind=raw.get("kind", ""),
                    data=raw.get("data", {}),
                    at=raw.get("at", ""),
                    id=raw.get("id", ""),
                )
            )
        return events

    def kinds(self, task_id: str) -> list[str]:
        return [event.kind for event in self.read(task_id)]
