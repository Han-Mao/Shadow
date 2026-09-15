"""执行状态机（v4.1 §五）。

`models/execution.py` 定义的是**词汇**（有哪些状态、哪些是终态），这里定义的是**规则**：
哪些迁移合法、进程死在某个状态上该怎么收。

## 为什么「少几个状态」不是小事

V4 §一 那版只有 `RUNNING → SUCCEEDED / FAILED / REFUSED / UNVERIFIED`，一条执行从落盘
那一刻就是 `RUNNING`。于是恢复时**分不出**下面这两种崩溃：

    在点付款按钮**之前**崩溃   → 手机没被碰过，重做是安全的
    在点付款按钮**之后**崩溃   → 可能已经付款了，重做 = 第二次扣款

两者在盘上长得一模一样（都是 `RUNNING`），所以恢复只能二选一：要么一律保守
（没发生的动作也永远不许重做），要么一律激进（重复付款）。补上 `CREATED` /
`RISK_CHECKED` / `DISPATCHED` 之后，这条分界变成了一个**可以查的事实**：

    CREATED / RISK_CHECKED / DISPATCHED  → 设备从未被调用 → FAILED（可安全重做）
    RUNNING                              → 副作用可能已发生 → UNKNOWN（禁止自动重试）

`DISPATCHED` 与 `RUNNING` 之间只隔着一次 `executor.execute` 调用，而这个区分是**有代价的**
——每条动作多一次 SQLite 提交（`started_at` 与 `RUNNING` 必须在设备调用之前落盘）。
这个代价换的是「敢不敢重做」这个判断，值。

## 非法 ≠ 竞争失败

`assert_transition()` 管的是「调用方写错了」（想从终态复活），抛
`InvalidExecutionTransition`。而「合法但守卫落空」（两个请求同时派发同一条执行）
由 `ExecutionStore.transition()` 返回 `None` 表达——那是预期内的，重读一次再决定即可。
两种情形混成一个结果，会让「并发」看起来像「bug」，或者反过来。
"""
from __future__ import annotations

from models.exceptions import InvalidExecutionTransition
from models.execution import (
    ExecutionStatus,
    TERMINAL_EXECUTION_STATUSES,
)

# 合法迁移表。**只增不改地维护它**：这张表既是守卫，也是「这套状态机到底怎么走」的
# 唯一说明——散在调用点的 if 判断查不出漏了哪条边。
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    # 受理完、还没判风险。门禁可以在这里就拒（第一轮无上下文判定），
    # 也可以判完风险再拒；进程死在这里 → 设备没被碰过 → FAILED。
    ExecutionStatus.CREATED: frozenset(
        {
            ExecutionStatus.RISK_CHECKED,
            ExecutionStatus.REFUSED,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.RISK_CHECKED: frozenset(
        {
            ExecutionStatus.DISPATCHED,
            ExecutionStatus.REFUSED,
            ExecutionStatus.FAILED,
            # 自环：**再判一次风险**是合法事实而不是错误（判定依据可能变了，
            # 例如第一轮无上下文、第二轮带 UI 树）。状态不变，但会多留一条痕。
            # 有它就不必在 service 里为一个幂等分支开一条特殊路径——状态机里
            # 有一条自环，比代码里有一个「这种情况不算迁移」的例外干净。
            ExecutionStatus.RISK_CHECKED,
        }
    ),
    # 派发意图已落盘、设备还没被调用。
    ExecutionStatus.DISPATCHED: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.FAILED,
        }
    ),
    # **唯一**可能已经产生副作用的非终态。`UNKNOWN` 只允许从这里进来——
    # 这就是「只有真的把动作交出去过，才存在『不知道成没成』」这条规则。
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.UNVERIFIED,
            ExecutionStatus.UNKNOWN,
        }
    ),
    # 终态没有出边：事实一旦落定就不再被改写（尤其是 UNKNOWN，
    # 被改成 FAILED 就等于把「可能已付款」抹成「失败了，重试吧」）。
    ExecutionStatus.SUCCEEDED: frozenset(),
    ExecutionStatus.FAILED: frozenset(),
    ExecutionStatus.REFUSED: frozenset(),
    ExecutionStatus.UNVERIFIED: frozenset(),
    ExecutionStatus.UNKNOWN: frozenset(),
}

# 「设备可能已经被操作过」的终态集合——恢复与对账要按它分流（v4.1 §七）。
# `UNVERIFIED` 在内：它同样是「发出去了但不知道成没成」，同样不许自动重试。
NO_AUTO_RETRY_STATUSES = frozenset(
    {ExecutionStatus.UNVERIFIED, ExecutionStatus.UNKNOWN}
)


def is_terminal(status: str) -> bool:
    return status in TERMINAL_EXECUTION_STATUSES


def can_transition(current: str, target: str) -> bool:
    """`current → target` 是不是这条状态机允许的边。未知状态一律 False。"""
    return target in LEGAL_TRANSITIONS.get(current, frozenset())


def assert_transition(current: str, target: str, execution_id: str = "") -> None:
    """不合法就抛 `InvalidExecutionTransition`（调用方写错了，不是竞争）。"""
    if not can_transition(current, target):
        raise InvalidExecutionTransition(execution_id, current, target)


def recovery_target(status: str) -> str | None:
    """进程消失时某条非终态执行该落到哪个终态（v4.1 §六）。

    返回 `None` 表示这条执行**已经是终态**，恢复不该碰它。

    两个分支的判据只有一条——**设备有没有被调用过**：

    - 没被调用过（`CREATED` / `RISK_CHECKED` / `DISPATCHED`）→ `FAILED`。
      这**不是**保守化，是事实：`DISPATCHED` 的写入与 `executor.execute` 的调用之间
      没有任何别的可能，进程死在那里就等于动作没发生。记 `FAILED` 之后它可以被重做。
    - 已经交出去了（`RUNNING`）→ `UNKNOWN`。手机那边可能已经发生了副作用，
      所以它既不能记成成功，也**不许自动重试**（§七），只能靠重新观察对账或转人工。
    """
    if is_terminal(status):
        return None
    if status == ExecutionStatus.RUNNING:
        return ExecutionStatus.UNKNOWN
    return ExecutionStatus.FAILED
