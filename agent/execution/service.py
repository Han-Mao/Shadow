"""ExecutionService：动作执行的**唯一写入口**（v4.1 §九）。

V4 §一 之后，「谁在什么时候对哪台手机做了什么」分散在三处：手工端点在
`api/server.py` 里建记录、Agent 在 `agent/_execution.py` 里发事件、验证层在别处写结果。
§九 要的是把它们收成一个入口，理由不是「少写几行」，而是下面这三条**只有在一个入口里
才做得到**的事：

    状态迁移 + 它的事件，在**同一个事务**里
    外部副作用（设备调用）**不在**事务里
    失败的那一半不留痕、成功的那一半不缺失

## 事务边界（v4.1 §四）

审核给的那条流程被照搬下来，只改了一处**顺序**：

    BEGIN  → 建执行记录(CREATED)          → COMMIT      受理
    BEGIN  → 状态 RISK_CHECKED + RISK_ASSESSED 事件 → COMMIT   判风险
    BEGIN  → 状态 DISPATCHED  + ACTION_DISPATCHED 事件 → COMMIT   记录意图
    ──────── 这里才调用设备（executor.execute）────────
    BEGIN  → 状态 RUNNING                  → COMMIT      交给设备
    BEGIN  → 状态终态 + ACTION_VERIFIED 事件 → COMMIT      记录结果

审核的原图把「执行设备」画在 `ACTION_DISPATCHED` **之前**。那样写的话，
「先记录意图、再产生副作用」这条写前日志的纪律就没了——设备调用成功而
`DISPATCHED` 写失败时，没有任何东西能证明「那次点击是被本系统发出的」。
所以顺序改成上面这样。

## 设备调用为什么必须在事务外

    BEGIN → 写记录 → device.tap() → 写事件 → COMMIT
                                             ↑ 这里失败就 ROLLBACK

ROLLBACK 抹掉的是**数据库**，抹不掉手机上已经发生的那一次点击。结果是
「手机支付成功了，而系统里查不到是谁点的」——这是最危险的一种状态。
正确形状是审核 §四 的 **Prepare → Effect → Commit**：事前的意图先落盘（它是可回滚的），
副作用发生在事务之外（它不可回滚，所以必须自己承担后果），事后再把结果落盘。

## 「合法」与「竞争」分开表达

- 迁移**不合法**（想从终态复活）→ 抛 `InvalidExecutionTransition`（调用方写错了）。
- 迁移**合法但守卫落空**（并发派发同一条执行）→ 返回 `False`，不抛异常。

两者的处置完全不同，混成一个结果会让「并发」看起来像「bug」，或者反过来。
"""
from __future__ import annotations

import logging

from models.exceptions import PersistenceError
from models.execution import (
    NON_TERMINAL_EXECUTION_STATUSES,
    ActionExecution,
    ExecutionStatus,
)
from storage.event_log import (
    ACTION_DISPATCHED,
    ACTION_VERIFIED,
    EXECUTION_RECOVERED,
    RISK_ASSESSED,
)

from . import state

logger = logging.getLogger(__name__)


def summarize(action) -> dict:
    """把 `Action`（或已经摊平的 dict）收成记录里那份**动作摘要**。

    只留 `type / target / value`：设备返回的细节属于 `result`，两样混在一起之后
    「这次想做什么」和「实际发生了什么」就分不开了。
    """
    if isinstance(action, dict):
        return dict(action)
    return {
        "type": getattr(getattr(action, "type", ""), "value", None) or str(getattr(action, "type", "")),
        "target": str(getattr(action, "target", "") or ""),
        "value": getattr(action, "value", None),
    }


def _apply(record: ActionExecution, stored: ActionExecution) -> None:
    """把库里那份回灌到调用方手里的**同一个对象**上。

    为什么不做成「返回新对象、调用方自己换」：手工路径在 `run_manual_action` 里
    拿着 `execution` 一路往下走（`_execution_guard` 判断 `is_finished`、
    响应体读 `execution.status`），留两个版本必然漂移。回灌之后
    「内存对象 == 库里的行」在任何一次 service 调用之后都成立。
    """
    record.status = stored.status
    record.risk = stored.risk
    record.started_at = stored.started_at
    record.finished_at = stored.finished_at
    record.result = stored.result
    record.note = stored.note
    record.revision = stored.revision


class ExecutionService:
    """动作执行的写入口。`event_log` 可以不传（只记执行记录、不发事件）。"""

    def __init__(self, store, *, event_log=None) -> None:
        self._store = store
        self._events = event_log

    @property
    def store(self):
        return self._store

    # ---- 准备阶段（事务内，可回滚） ----

    def start(
        self,
        *,
        action,
        device_id: str,
        principal: str = "",
        task_id: str = "",
        channel: str = "manual",
        request_id: str = "",
        risk: str = "",
    ) -> ActionExecution:
        """受理一条执行：建记录并**落盘**（状态 `CREATED`）。

        写不进去抛 `PersistenceError`，调用方必须据此**拒绝执行**——
        这条记录是「手机被操作过」的唯一凭据。反过来（先做后记）就是
        「真的点了付款，但查不到是谁点的」。
        """
        record = ActionExecution(
            task_id=task_id,
            principal=principal,
            device_id=device_id,
            action=summarize(action),
            channel=channel,
            request_id=request_id,
            risk=risk,
            status=ExecutionStatus.CREATED,
        )
        return self._store.create(record)

    def assessed(self, execution: ActionExecution, *, risk: str = "", **detail) -> bool:
        """记风险判定结论（`RISK_ASSESSED` 是安全关键事件，写不进就不继续）。"""
        return self._advance(
            execution,
            expect={ExecutionStatus.CREATED, ExecutionStatus.RISK_CHECKED},
            to=ExecutionStatus.RISK_CHECKED,
            event=RISK_ASSESSED,
            detail=detail,
            risk=risk,
        )

    def refuse(self, execution: ActionExecution, *, risk: str = "", note: str = "", **detail) -> bool:
        """被风险门禁拦下：**没碰设备**（v4.1 §五 的 `REFUSED`）。

        与「执行失败」分开是一种事实区分：它回答的是「有没有过一次没被记录的点击尝试」。
        """
        return self._advance(
            execution,
            expect=set(NON_TERMINAL_EXECUTION_STATUSES),
            to=ExecutionStatus.REFUSED,
            event=RISK_ASSESSED,
            detail=detail,
            note=note,
            risk=risk,
        )

    def dispatched(self, execution: ActionExecution, *, risk: str = "", **detail) -> bool:
        """记录派发意图（`ACTION_DISPATCHED`，fail-closed）——**必须在设备调用之前**。

        返回 `False` 表示这次派发被守卫拒了（同一条执行已经派发过）。
        调用方必须**不要**再去调设备：那正是「同一个 execution_id 不能 tap 两次」。
        """
        return self._advance(
            execution,
            expect={ExecutionStatus.RISK_CHECKED, ExecutionStatus.CREATED},
            to=ExecutionStatus.DISPATCHED,
            event=ACTION_DISPATCHED,
            detail=detail,
            risk=risk,
        )

    def running(self, execution: ActionExecution) -> bool:
        """动作**即将交给设备**（`RUNNING`）。

        这可能是整个流程里最要紧的一次写入：从它之后，「手机有没有被操作过」
        就不再由我们决定，崩溃恢复也只能记 `UNKNOWN`（禁止自动重试）。
        所以它在 `executor.execute` 之前单独提交一次，而不是和 `DISPATCHED` 合并。
        """
        return self._advance(
            execution,
            expect={ExecutionStatus.DISPATCHED},
            to=ExecutionStatus.RUNNING,
        )

    # ---- 收尾（事务内，可回滚） ----

    def verified(self, execution: ActionExecution, **detail) -> None:
        """记验证结论（`ACTION_VERIFIED`）。

        普通事件（fail-open）：验证结论丢了只会少一条解释，不会改变已经发生的事。
        所以这里**不**因为写失败而抛——它跟在副作用之后，没有任何东西可以撤销了。
        """
        if self._events is None:
            return
        self._events.emit(self._owner(execution), ACTION_VERIFIED, **self._enrich(execution, detail))

    def settle(
        self,
        execution: ActionExecution,
        status: str,
        *,
        result: dict | None = None,
        note: str = "",
        risk: str = "",
    ) -> bool:
        """落定为终态。返回 `False` 表示**守卫落空**（记录不存在 / 已被并发落定）。

        两种「没落成」的处理刻意不同，别把它们混成一种：

        - **守卫落空**（合法但输了竞争）→ 返回 `False`。这是预期内的，
          调用方重读一次再决定即可。
        - **已经是终态再落定** → 抛 `InvalidExecutionTransition`（调用方写错了）。
          `settle(UNKNOWN → FAILED)` 正是 §七 要防的那种「顺手补记」，
          静默返回 `False` 会让它看起来像一次无害的失败调用。终态是事实，事实不改写。

        注意与 `ExecutionStore.finish()` 的分工：那一层是**宽松**的（告警 + 返回现状），
        它保护的是「事实不被覆盖」；这一层是**严格**的，它执行的是状态机。
        宽松的那层留给旧调用点，新代码走这里。

        **没落成不代表动作没发生**——调用方要照旧把这次调用的真实结果返回给使用者。
        `PersistenceError` 仍然抛出去，让上层能区分「动作失败」与「记录失败」。
        """
        return self._advance(
            execution,
            expect=set(NON_TERMINAL_EXECUTION_STATUSES),
            to=status,
            result=result,
            note=note,
            risk=risk,
        )

    # ---- 启动恢复（v4.1 §六） ----

    def recover(self, execution: ActionExecution, *, target: str, note: str = "") -> bool:
        """把一条**进程死后留下来的**执行落定为 `target`（启动恢复专用）。

        与 `settle()` 的差别只有 `expect`：这里用**启动时观察到的那个状态**，
        而不是「任意非终态」。理由是恢复可能在和一个还活着的进程抢同一条记录
        （多进程部署，见 `SHADOW_ALLOW_MULTI_PROCESS`）——
        「我看到它是什么，就从那个状态迁走」比「不管它现在是什么都能改成 UNKNOWN」
        精确得多：后者会把别的进程正在跑的执行也一起接管。

        恢复事件（`EXECUTION_RECOVERED`）**不是安全关键事件**：它丢了审计链也断不了，
        因为「为什么被改成这个终态」同时写在记录的 `note` 里，而记录才是权威事实。
        """
        return self._advance(
            execution,
            expect={execution.status},
            to=target,
            note=note,
            event=EXECUTION_RECOVERED,
        )

    # ---- 内部 ----

    def _owner(self, execution: ActionExecution) -> str:
        """这条执行的事件挂在谁名下。

        有任务就挂任务（于是 `GET /tasks/{id}/events` 能看到这一步），
        手工动作不属于任何任务，就挂**执行自己**——这是 V4 §一 起的既有约定
        （`GET /executions/{id}` 靠 `read(execution_id)` 取回它的过程）。
        """
        return execution.task_id or execution.execution_id

    def _enrich(self, execution: ActionExecution, detail: dict) -> dict:
        """给事件补上身份字段——省得每个调用点都要记得传一遍。

        §八 要的那条链是「`execution_id` → 事件 → 完整轨迹」，所以
        `execution_id` 一定要出现在**每一条**属于这次执行的事件里。
        """
        payload = dict(detail)
        payload.setdefault("execution_id", execution.execution_id)
        payload.setdefault("principal", execution.principal)
        payload.setdefault("request_id", execution.request_id)
        payload.setdefault("channel", execution.channel)
        payload.setdefault("device", execution.device_id)
        return payload

    def _advance(
        self,
        execution: ActionExecution,
        *,
        expect: set[str],
        to: str,
        event: str | None = None,
        detail: dict | None = None,
        result: dict | None = None,
        note: str = "",
        risk: str = "",
    ) -> bool:
        """一次「状态迁移 + 它的事件」，同一个事务。

        顺序刻意是「先迁移、后写事件」：事件写失败时事务整体回滚，
        状态**仍然停在迁移前那一格**，于是「读状态」就足以判断「事件到底有没有留下」。
        反过来（先写事件再迁移）会造出「事件说派发了，状态说还没派发」的分叉。
        """
        state.assert_transition(execution.status, to, execution.execution_id)

        with self._store.transaction():
            stored = self._store.transition(
                execution.execution_id,
                expect=expect,
                to=to,
                result=result,
                note=note,
                risk=risk,
            )
            if stored is None:
                return False
            if event is not None and self._events is not None:
                # 安全关键事件（risk_assessed / action_dispatched）在这里是 fail-closed：
                # `EventLog.emit` 自己按 kind 分派，写失败抛 PersistenceError → 事务回滚。
                self._events.emit(self._owner(execution), event, **self._enrich(execution, detail or {}))

        _apply(execution, stored)
        return True
