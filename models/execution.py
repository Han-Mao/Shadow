"""ActionExecution：**动作级执行**的身份与结果（V4 §一/§五）。

手工端点（`/tap` / `/text` / `/back` / `/actions`）以前把所有操作都记在一个固定的
假任务 id（`MANUAL_ACTION_TASK_ID = "__manual__"`）下，于是：

- 不同请求的记录**混在同一条流**里，事后只能靠逐条字段去拼「这次是哪个请求」；
- 根本没有「执行身份」，也就回答不了审核 §五 要的那条链——
  谁 → 什么时候 → 哪台手机 → 做了什么 → 什么风险 → 为什么允许 → 结果如何。

现在每次调用都有一个 `execution_id`：

    POST /tap  →  execution_id = exe_xxx
        ├── principal / device_id / action / risk / request_id / created_at / finished_at / result

`Task` 是「任务级执行」，`ActionExecution` 是「动作级执行」；两者都属于更高一级的
`Execution`（V4 §二）——将来手工点击、Agent 自动执行、Android 真机操作可以共用同一张表。

刻意用 dataclass 而不是 pydantic `BaseModel`：这是**事实记录**，不需要校验魔法，
落盘复用 `JsonStore`（已经有 tmp + fsync + os.replace 的原子写），改动面最小。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

# 执行状态。与 `TaskStatus` 刻意分开：执行只有下面这几种结果，
# 它不需要 CREATED / QUEUED / PAUSED 那些**编排**状态——手工动作不进调度器。
EXECUTION_RUNNING = "RUNNING"
"""已受理、正在执行。记录在动作真正发出**之前**就落盘。"""

EXECUTION_SUCCEEDED = "SUCCEEDED"
"""动作发出去了，且验证层认为它生效了。"""

EXECUTION_FAILED = "FAILED"
"""执行内核返回失败（设备拒绝、参数非法……）。"""

EXECUTION_REFUSED = "REFUSED"
"""被风险门禁拦下，**根本没有碰设备**。

与 FAILED 必须分开：事后追责里「被拒绝」和「执行失败」的含义完全不同，
而且只有分开才答得上「有没有发生过一次没被记录的点击」。
"""

EXECUTION_UNVERIFIED = "UNVERIFIED"
"""动作发出去了，但效果无法确认（拿不到独立证据）。

**这一条最要紧**：它对应的正是「已经点了，但不知道成没成」——
不允许自动重做，只能对账或转人工（见 `agent/reconciliation`）。
"""

EXECUTION_STATUSES = frozenset(
    {
        EXECUTION_RUNNING,
        EXECUTION_SUCCEEDED,
        EXECUTION_FAILED,
        EXECUTION_REFUSED,
        EXECUTION_UNVERIFIED,
    }
)


def new_execution_id() -> str:
    """执行 id：`exe_` + 16 位十六进制。

    不用自增整数：执行记录要能**跨重启、跨进程**独立生成（多 worker 下自增需要一个
    中心计数器，那又变成单点）。前缀让人一眼看出这是执行 id 而不是任务 id。
    """
    return f"exe_{uuid.uuid4().hex[:16]}"


def _now() -> str:
    # 毫秒精度：要能看出「受理 → 发出 → 完成」之间的间隔
    return datetime.now().isoformat(timespec="milliseconds")


@dataclass
class ActionExecution:
    """一次动作执行的完整事实。"""

    execution_id: str = field(default_factory=new_execution_id)
    principal: str = ""
    device_id: str = ""
    action: dict = field(default_factory=dict)
    """动作摘要（type / target / value），**不含**设备返回的细节。"""
    channel: str = "manual"
    """谁发起的：`manual`（人点）/ `agent`（任务里的 runtime）。"""
    status: str = EXECUTION_RUNNING
    risk: str = ""
    request_id: str = ""
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None
    result: dict = field(default_factory=dict)
    note: str = ""

    @property
    def is_finished(self) -> bool:
        return self.status != EXECUTION_RUNNING

    def finish(
        self,
        status: str,
        *,
        result: dict | None = None,
        note: str = "",
        risk: str = "",
    ) -> None:
        """落定为某个终态。`risk` 只在需要补记时传（例如被拒时它是拒绝依据）。"""
        self.status = status
        self.finished_at = _now()
        if result is not None:
            self.result = result
        if note:
            self.note = note
        if risk:
            self.risk = risk

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "principal": self.principal,
            "device_id": self.device_id,
            "action": self.action,
            "channel": self.channel,
            "status": self.status,
            "risk": self.risk,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ActionExecution":
        """从落盘的字典还原。

        未知字段直接丢掉而不是报错：执行记录是**只增不改的事实**，
        老版本写下的记录在新版本里必须仍能读出来（多出来的字段只是还没用上）。
        """
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items() if key in known})
