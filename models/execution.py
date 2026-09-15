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

class ExecutionStatus:
    """一条执行**可能处于**的状态（v4.1 §五）。

    为什么要从「RUNNING / SUCCEEDED / FAILED / REFUSED / UNVERIFIED」补成一套状态机：
    V4 §一 那时只有终态，而**中间态**恰恰是崩溃恢复唯一能用的信息。

    一条执行从受理到落定的真实过程是：

        CREATED ──► RISK_CHECKED ──► DISPATCHED ──► RUNNING ──► SUCCEEDED
            │            │               │            │
            │            │               │            └──► FAILED / UNVERIFIED
            │            │               └──► （进程死在派发与设备调用之间：动作**没发生**）
            │            └──► REFUSED（门禁拦下，没碰设备）
            └──► （进程死在受理与判风险之间：动作**没发生**）

    关键分界在 `DISPATCHED` 与 `RUNNING` 之间：**前者只代表「意图已落盘」，后者才是
    「动作已经交给设备」**。恢复时这条分界决定两条完全不同的出路——

        CREATED / RISK_CHECKED / DISPATCHED  → 设备从未被调用 → FAILED（可安全重做）
        RUNNING                              → 可能已经生效   → UNKNOWN（禁止自动重试）

    少了这条分界，「进程死在点击之前」与「进程死在点击之后」长得一模一样，
    恢复就只能一律保守（对没发生的动作也拒绝重试），或者一律激进（重复点一次付款）。
    """

    CREATED = "CREATED"
    """已受理、已落盘，**还没过风险门禁，也没碰设备**。"""

    RISK_CHECKED = "RISK_CHECKED"
    """风险判定已完成，动作尚未发出。"""

    DISPATCHED = "DISPATCHED"
    """派发意图已落盘（`ACTION_DISPATCHED` 事件也是在这个事务里写的），
    **但设备调用还没开始**——这一瞬间崩溃，动作确定没发生。"""

    RUNNING = "RUNNING"
    """动作已交给设备层（`executor.execute` 即将/正在被调用）。

    从这一刻起「手机有没有被操作过」不再由我们决定，所以崩溃后只能记 `UNKNOWN`。
    """

    SUCCEEDED = "SUCCEEDED"
    """动作发出去了，且验证层认为它生效了。"""

    FAILED = "FAILED"
    """执行内核返回失败（设备拒绝、参数非法……），或进程在**碰设备之前**终止。"""

    REFUSED = "REFUSED"
    """被风险门禁拦下，**根本没有碰设备**。

    与 FAILED 必须分开：事后追责里「被拒绝」和「执行失败」的含义完全不同，
    而且只有分开才答得上「有没有发生过一次没被记录的点击」。
    """

    UNVERIFIED = "UNVERIFIED"
    """动作发出去了，但效果无法确认（拿不到独立证据）。

    **这一条最要紧**：它对应的正是「已经点了，但不知道成没成」——
    不允许自动重做，只能对账或转人工（见 `agent/reconciliation`）。
    """

    UNKNOWN = "UNKNOWN"
    """**进程死在动作交给设备之后**，且没有走到落定（v4.1 §六/§七）。

    它不等于失败，也不等于成功：手机那边可能已经发生了副作用（付款、发送、删除）。
    因此它是**终态**但**不可自动重试**——唯一正确的下一步是重新观察手机对账
    （`agent/reconciliation`），或转人工。
    """


# 非终态：进程若在此状态下消失，恢复逻辑必须接管（§六 的扫描条件就是它）
NON_TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.CREATED,
        ExecutionStatus.RISK_CHECKED,
        ExecutionStatus.DISPATCHED,
        ExecutionStatus.RUNNING,
    }
)

# 终态。`UNKNOWN` 在这里面：它虽然「不知道结果」，但它是**已定论**的——
# 不再有进程会去推进它，恢复逻辑不该反复处理同一条。
TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.REFUSED,
        ExecutionStatus.UNVERIFIED,
        ExecutionStatus.UNKNOWN,
    }
)

EXECUTION_STATUSES = NON_TERMINAL_EXECUTION_STATUSES | TERMINAL_EXECUTION_STATUSES

# 「动作**已经交到设备层**」的状态集合（v4.1 §七 的判据）。
#
# 今天它只有一个成员，这是刻意的：`DISPATCHED` 不在里面。那条分界不在「记录写了没」，
# 而在 `executor.execute` 被调用之前还是之后——`DISPATCHED` 之后崩溃，动作确定没发生；
# 一旦进了 `RUNNING`，手机那边可能已经发生了副作用。把 `DISPATCHED` 也算进来会让恢复
# 一律落 UNKNOWN，代价是「明明没点过的动作也永远不许重做」。
#
# 将来若在 `RUNNING` 与终态之间再加中间态（例如「已发出、等验证」），它也属于这一组。
DEVICE_REACHED_STATUSES = frozenset({ExecutionStatus.RUNNING})

# ---- 兼容别名 ----
#
# V4 §一 起这些名字有几十个调用点（`api/server.py`、测试）。状态机引入后**取值不变**，
# 所以保留别名而不是去改所有调用点——那是另一件事，混在这里做只会扩大改动面。
EXECUTION_CREATED = ExecutionStatus.CREATED
EXECUTION_RISK_CHECKED = ExecutionStatus.RISK_CHECKED
EXECUTION_DISPATCHED = ExecutionStatus.DISPATCHED
EXECUTION_RUNNING = ExecutionStatus.RUNNING
EXECUTION_SUCCEEDED = ExecutionStatus.SUCCEEDED
EXECUTION_FAILED = ExecutionStatus.FAILED
EXECUTION_REFUSED = ExecutionStatus.REFUSED
EXECUTION_UNVERIFIED = ExecutionStatus.UNVERIFIED
EXECUTION_UNKNOWN = ExecutionStatus.UNKNOWN


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
    task_id: str = ""
    """这条执行属于哪个**任务**（v4.1 §一 的那张 `task_id` 列）。

    手工动作**刻意为空**：`/tap` 这类调用不属于任何任务，硬塞一个假 id
    （`__manual__`）正是 V3.2 §一 否掉的做法——它把「属于某个任务」这件事说成了真的。
    Agent 执行时这里才是真实任务 id，于是 §八 那条
    「Task → Execution → Events」的链在表上就成立。
    """
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
    started_at: str | None = None
    """动作**真正交给设备**的那一刻（进入 `RUNNING`）。`None` = 还没交出去过。

    与 `created_at` 分开是 v4.1 §一 的字段要求，也真的有用：两者相差多少，
    就是「受理到发出」的排队/判定开销（含风险门禁那两轮判定与一次观察）。
    """
    finished_at: str | None = None
    result: dict = field(default_factory=dict)
    note: str = ""
    revision: int = 0
    """这条记录被改过几次（v4.1 §一 的 `revision` 列）。

    **它只是自省信息，不是并发控制手段**：判断「这条还能不能派发」靠的是
    `ExecutionStore.transition()` 里那句带 `WHERE status=?` 的受保护 UPDATE
    ——状态本身就是那个守卫，比版本号更贴事实。别把它读成 CAS。
    """

    @property
    def is_finished(self) -> bool:
        return self.status in TERMINAL_EXECUTION_STATUSES

    @property
    def may_have_touched_device(self) -> bool:
        """这条执行是否**可能**已经把动作作用到手机上（v4.1 §七）。

        `DISPATCHED` 不含在内：那个状态写入之后、`executor.execute` 调用之前崩掉，
        设备确定没被碰过——把「意图已记录」当成「动作已发生」会让恢复过度保守
        （没发生的动作也永远不许重做）。只有进了 `RUNNING`，副作用才真的可能已经发生。
        """
        return self.status in DEVICE_REACHED_STATUSES

    def mark(self, status: str, *, at: str | None = None) -> None:
        """在**本地**推进状态（不动磁盘，也不校验合法性）。

        合法性与写入顺序由 `agent.execution.state` + `ExecutionStore.transition()`
        负责——这个方法是纯数据操作，`to_dict()` 要能被任何测试直接构造出来。
        `started_at` 只在第一次真正交给设备时填。
        """
        stamp = at or _now()
        self.status = status
        if status == ExecutionStatus.RUNNING and not self.started_at:
            self.started_at = stamp
        if status in TERMINAL_EXECUTION_STATUSES:
            self.finished_at = stamp

    def finish(
        self,
        status: str,
        *,
        result: dict | None = None,
        note: str = "",
        risk: str = "",
    ) -> None:
        """落定为某个终态。`risk` 只在需要补记时传（例如被拒时它是拒绝依据）。"""
        self.mark(status, at=_now())
        if result is not None:
            self.result = result
        if note:
            self.note = note
        if risk:
            self.risk = risk

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "principal": self.principal,
            "device_id": self.device_id,
            "action": self.action,
            "channel": self.channel,
            "status": self.status,
            "risk": self.risk,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "note": self.note,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ActionExecution":
        """从落盘的字典还原。

        未知字段直接丢掉而不是报错：执行记录是**只增不改的事实**，
        老版本写下的记录在新版本里必须仍能读出来（多出来的字段只是还没用上）。
        """
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items() if key in known})
