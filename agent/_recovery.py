"""崩溃恢复门禁与「跨重启必须记住什么」（V2.7 P0-1 / V2.8 §八 · v4.2 §三 P0 拆出的职责）。

为什么要单独一个模块：v4.2 指出 Runtime 有长成
「Agent + Scheduler + Recovery + Persistence + Safety」的趋势，并建议继续拆。
判断成立，但**不是所有四块都值得拆**——执行闭环（observe / think / act / verify）
本来就在 `_execution.py` 里，装配与事件原语（`_emit` / `_persist`）是所有 mixin
共用的动词。真正能划出一条干净界线的只有**恢复**这一块：

    执行闭环问的是「这一屏现在该做什么」
    恢复模块问的是「盘上留下了什么，上一个动作到底发出去没有」

后者的输入只有两样：`Task`（`recovery_required` / `recovery_note`）与恢复点，
输出只有一样：要么放行，要么转人工。把它独立出来之后，Runtime 主类只剩
「取会话 + 装配依赖 + 事件/持久化原语」，而这条路是**唯一一条会把任务交给人的路**
（半途消失的副作用不能靠重跑猜），值得单独一个文件讲清楚。

**刻意没做**：把 Runtime 改成 `runtime/` 包（loop / recovery / checkpoint / safety /
context 五个模块）。那是一次大搬迁，收益是目录好看，代价是全仓库的 import 与
`git blame` 断裂；而现状（runtime.py 354 行 + 4 个 mixin）离「God object」还很远。
触发条件：`runtime.py` 再涨到 600 行以上，或 `_emit`/`_persist` 之外出现第三类
需要独立测试的横切关注点。
"""
from __future__ import annotations

import logging

from models.task import Task, TaskEvent
from storage.event_log import WAITING

from ._runtime_types import RunOutcome, RuntimeState

logger = logging.getLogger(__name__)


class RecoveryMixin:
    """崩溃恢复门禁 + 恢复备注的容器。"""

    def _init_recovery(self) -> None:
        """在 `__init__` 里调一次。恢复备注单独一个容器，因为它既不是「某个危险动作」
        也不是「完成裁定」——它是「上次那个动作到底生效没有，我们判断不了」。"""
        self._recovery_notes: dict[str, str] = {}

    # ---- 备注容器（对外只暴露这三个动作，`_confirm.py` 不再直接摸 dict）----

    def _note_recovery(self, task_id: str, reason: str) -> None:
        with self._states_lock:
            self._recovery_notes[task_id] = reason

    def _recovery_reason(self, task_id: str) -> str | None:
        with self._states_lock:
            return self._recovery_notes.get(task_id)

    def _has_recovery_note(self, task_id: str) -> bool:
        with self._states_lock:
            return task_id in self._recovery_notes

    def _clear_recovery_note(self, task_id: str) -> None:
        with self._states_lock:
            self._recovery_notes.pop(task_id, None)

    # ---- 跨重启必须记住什么 ----

    @staticmethod
    def _sync_durable_state(task: Task, state: RuntimeState) -> None:
        """把该跨重启存活的运行时状态同步到 Task 上（V2.7 P0-1）。

        「哪些运行时状态必须落盘」是有判断的，不是越多越好：

        - **`denied_fingerprints` 必须持久化**：它记的是「用户明确拒绝过这个动作」。
          只放内存的话，重启后系统会重新请求同一个动作——骚扰，而且让人以为系统没记住。
        - **`approval`（放行凭据）刻意不落盘**：批准是针对**当时那一屏**给的，重启后
          页面可能已经变了；把批准带过重启，等于执行一个用户从没真正看过的东西。
          恢复后重新请人确认是**正确行为**，不是缺陷。
        - `pending_confirmation` / `goal_approved_by_human` 同理：都是「此刻这一屏」的
          上下文，重启即失效（`recover()` 会留下 `confirmation_reset` 的记录）。
        """
        for fingerprint in state.denied_fingerprints:
            if fingerprint not in task.denied_fingerprints:
                task.denied_fingerprints.append(fingerprint)

    # ---- 门禁 ----

    def _gate_crash_recovery(self, task: Task) -> RunOutcome | None:
        """崩溃恢复门禁（V2.6 §七）：先回答「上次那个动作到底发出去没有」。

        任务停在 RUNNING 就被掐断，说明上一次执行是半途消失的：

        - **有恢复点** → 交给既有的恢复路径（它用 `validate` + `needs_reconciliation`
          对账 checkpoint 里的 `action_effect` / `attempt_id`）。这是已经能工作的部分，
          返回 None 让它照常继续。
        - **没有恢复点** → 既不知道动作发没发出，也没有 attempt 记录可比对。这时候
          重新规划再点一次手机，可能就是把同一条消息发第二遍、同一个订单下第二次。
          唯一诚实的做法是停下来交给人，而不是替用户赌一把。

        返回 None 表示「门禁放行，可以正常执行」。
        """
        checkpoint = self._load_checkpoint(task)
        if checkpoint is not None:
            return None

        reason = (
            "重启前任务停在执行中，且没有可用恢复点：无法判断上一个动作是否已经生效，"
            "已暂停等待人工确认（不要盲目重跑）"
        )
        logger.warning("任务 %s 崩溃重启且无可信恢复点，转人工确认", task.id)
        self._note_recovery(task.id, reason)
        # 把「上次动作效果未知」这件事落到 Task 上（V2.8 §八）：人工批准继续后它不能丢，
        # 否则重新规划时模型不知道崩溃前可能有未决副作用，可能把同一条消息发第二遍。
        task.recovery_note = reason
        # 这次标记已经处理过了，人工放行后不该被同一个门禁拦第二次
        task.recovery_required = False
        task.apply_event(TaskEvent.AWAITING_CONFIRMATION, source="runtime")
        self._emit(task.id, WAITING, reason="recovery_requires_human", detail=reason)
        return RunOutcome.AWAITING_CONFIRMATION
