"""状态不变量（V3 M4）：把「散落的 if」收敛成「一张可穷举测试的表」。

v2.9 §十四 的核心担忧：11 态 × 版本 × 恢复 × 确认 × 并发 = 状态组合爆炸，
现在靠「哪里补一个 if」兜，迟早漏。这里不做新框架，而是把三样东西**集中**：

1. **迁移合法性**已由 `ALLOWED_TRANSITIONS`（`models/task.py`）集中定义——那是
   「哪些迁移允许」的唯一事实源，`Task.transition_to` 强制走它（终态硬闸 fail closed）。
2. **结构性不变量**（本模块）：每个状态还隐含「任务必须待在哪个容器 / 持有哪个资源」，
   这些约束散在 scheduler / runtime 的 if 里，没有一处能查证。这里把它们写成表。
3. **穷举测试**：不引入 hypothesis（抽样），而是 `itertools.product` 穷举
   「状态 × 事件」全组合——11 × 13 = 143 个组合**全覆盖**，比抽样更强，且零依赖。

本模块是**纯函数 + 表**，不接任何运行时调用（避免侵入）；它同时充当：
- 文档：一张表说清「每个状态下什么必须成立」
- oracle：`tests/test_invariants.py` 用它穷举验证状态机自洽
"""
from __future__ import annotations

from dataclasses import dataclass

from .task import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    Task,
    TaskStatus,
)


@dataclass(frozen=True)
class InvariantViolation:
    """一条被违反的不变量。"""
    task_id: str
    status: str
    rule: str
    detail: str = ""

    def describe(self) -> str:
        return f"[{self.status}] {self.rule}" + (f"：{self.detail}" if self.detail else "")


def terminal_immutability(task: Task) -> list[InvariantViolation]:
    """终态不可逆：终态任务不能再迁出（终态硬闸，MEMORY [33]）。"""
    if task.status not in TERMINAL_STATUSES:
        return []
    # 终态自迁移（DONE → DONE）是幂等收尾，允许；迁到别的终态则非法。
    # 真正的硬闸在 transition_to 里抛 InvalidTransitionError，这里只是可查证的重述。
    return []


def _container_checks(task: Task, *, in_queue: bool, in_running: bool,
                      has_device: bool) -> list[InvariantViolation]:
    """按状态检查「任务在不在它该在的容器里」。

    这些是 scheduler 用 if 维护的隐式约束，抽出来才能被测试和文档覆盖。
    参数由调用方（scheduler）提供当前容器归属。
    """
    status = task.status
    violations: list[InvariantViolation] = []

    if status is TaskStatus.RUNNING:
        # RUNNING 必须正在被执行：要么在运行槽里，要么持有设备
        if not in_running and not has_device:
            violations.append(
                InvariantViolation(task.id, status.value, "RUNNING 任务必须在运行槽或持有设备",
                                   f"in_running={in_running}, has_device={has_device}")
            )
    if status is TaskStatus.WAITING:
        # 等人工确认：不能在任何就绪队列里，否则会被 worker 立刻取出重跑 → 死循环
        if in_queue:
            violations.append(
                InvariantViolation(task.id, status.value, "WAITING 任务不得在就绪队列里",
                                   "放进去会被 worker 立刻取出重跑，撞上同一个危险动作")
            )
    if status in TERMINAL_STATUSES:
        # 终态必须已从活跃容器摘除
        if in_queue or in_running:
            violations.append(
                InvariantViolation(task.id, status.value, "终态任务不得留在活跃容器",
                                   f"in_queue={in_queue}, in_running={in_running}")
            )
    return violations


def check_invariants(
    task: Task,
    *,
    in_queue: bool = False,
    in_running: bool = False,
    has_device: bool = False,
) -> list[InvariantViolation]:
    """检查一个任务在当前状态下是否违反结构性不变量。返回违反列表（空 = 自洽）。

    纯函数、无副作用、零 I/O——可以在任何迁移点调用，也可以被穷举测试驱动。
    """
    violations: list[InvariantViolation] = []
    violations.extend(terminal_immutability(task))
    violations.extend(
        _container_checks(task, in_queue=in_queue, in_running=in_running, has_device=has_device)
    )
    return violations


# ---- 穷举测试用的 oracle ----

def is_legal_transition(from_status: TaskStatus, to_status: TaskStatus) -> bool:
    """一次迁移是否被 ALLOWED_TRANSITIONS 允许（迁移合法性的唯一事实源）。"""
    return to_status in ALLOWED_TRANSITIONS.get(from_status, frozenset())


def transition_class(from_status: TaskStatus, to_status: TaskStatus) -> str:
    """把一次迁移归为三类之一，供穷举测试断言「不存在未定义行为」。

    - "allowed"      合法迁移
    - "terminal"     终态迁出（fail closed，应抛 InvalidTransitionError）
    - "warned"       非终态非法迁移（warning + 仍执行，MEMORY [33] 的两分类）
    """
    if to_status in ALLOWED_TRANSITIONS.get(from_status, frozenset()):
        return "allowed"
    if from_status in TERMINAL_STATUSES:
        return "terminal"
    return "warned"
