"""AgentRuntime 共享类型（V2.7 P2-1 拆出）。

`RunOutcome` / `RuntimeState` / `ApprovalGrant` 被拆解后的多个 mixin 共用，
抽到独立模块避免循环导入——mixin 与主类都能干净地 `from ._runtime_types import ...`。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from models.action import Action, ActionEffectStatus
from models.state import ObservationEpoch
from models.task import Task

# 最近 LOOP_WINDOW 个动作里，同一个指纹出现 LOOP_REPEAT_THRESHOLD 次即判定死循环
LOOP_WINDOW = 4
LOOP_REPEAT_THRESHOLD = 3


class RunOutcome(str, Enum):
    DONE = "done"
    FAILED = "failed"
    SUSPENDED = "suspended"
    """被抢占或用户暂停，任务保留状态等待恢复。"""

    CANCELLED = "cancelled"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    """撞上危险动作，等人确认（HITL）。"""


@dataclass
class RuntimeState:
    """单个任务的运行时状态。不落盘——恢复靠 Checkpoint，不靠这份内存。

    V2.1 §二：预算拆成三个互相独立的计数，不再用一个 step_index 包打天下。
    - execution_step   ：真正执行了多少个 Agent Action（Observe / DONE 都不算）
    - observation_count：观察了多少次（观察失败也消耗）
    - model_call_count ：调用了多少次 VLM
    """

    execution_step: int = 0
    observation_count: int = 0
    model_call_count: int = 0
    last_action_effect: ActionEffectStatus = ActionEffectStatus.NOT_STARTED

    page_seen_changed: bool = False
    """本次执行期间页面是否**真的**推进过（导航或结构变化）。

    V2.2 §四：GoalVerifier 判「任务是否真的完成」时，这是最要紧的一条独立证据——
    如果整个任务跑下来页面一次都没变，却声称完成，就该被质疑。
    """

    pending_effect_reconcile: bool = False
    """上一个动作发出了但效果未知，下一个安全点要拿新观察去对账（V2.2 §三）。"""

    effect_reconciliations: int = 0
    """对账次数。对账可能重做动作，所以必须有上界。"""

    goal_rejections: int = 0
    """完成申请被独立验证驳回了几次。连续驳回会转人工，避免昂贵空转。"""

    awaiting_goal_decision: bool = False
    """正在等人裁定「任务是否算完成」（完成申请被驳回太多次之后的兜底）。"""

    goal_approved_by_human: bool = False
    """人工已经认定任务完成：下一次完成申请直接放行，不再走自动验证。"""

    pending_replan_reason: str = ""
    """下一轮 Think 必须走 Re-plan 而不是常规决策的原因（如完成申请被驳回）。"""

    last_goal_check: object | None = None
    """最近一次完成裁定的结论（`goal_verifier.GoalCheck`），供 API / 审计查询。"""

    retry_count: int = 0
    prepared: bool = False
    # V2.3：SUPER_TASK / re-plan / 人工干预时 task.version 会变化。
    # 每一轮决策前检查 task.version == run_version，否则旧 decision 必须作废，
    # 防止「新目标 + 旧 observation + 旧 trajectory」混合规划。
    run_version: int = 0
    forced_action: Action | None = None
    """对账判定「上次动作未生效」后要强制重做的动作（跳过一次模型决策）。"""

    attempt_seq: int = 0
    current_attempt_id: str | None = None
    """当前动作尝试的唯一 id（V2.1 §二十一）：同一步骤的每次重试都不同。"""

    recent_actions: deque[Action] = field(default_factory=lambda: deque(maxlen=LOOP_WINDOW))
    failed_strategies: list[str] = field(default_factory=list)
    denied_fingerprints: set[str] = field(default_factory=set)
    pending_confirmation: Action | None = None
    # 人工放行凭据（V2.7 P0-2）：绑定到**具体那一个动作**，不是「下一个危险动作」。
    # 原来的 `approved_dangerous: bool` 只要为真就放行随后任何危险动作——批准「付款」
    # 可能被用来放行紧接着出现的「删除」。改成凭证 + 逐项匹配 + 放行即消费。
    approval: "ApprovalGrant | None" = None
    # 请求确认那一刻的目标 / 计划版本，用来构造上面的放行凭据
    pending_task_version: int = 0
    pending_plan_version: int = 0
    # 请求确认那一刻所在的那一屏（package, activity）（V3.1 P2-9）。
    # 放行凭据要绑「动作 + 页面」，所以得把页面记下来——`_confirm_locked` 里没有观察。
    pending_page: tuple[str, str] = ("", "")

    decision_epoch: "ObservationEpoch | None" = None
    """做出当前决策所依据的那一屏（V3.1 P1-6）。

    `DeviceSession.generation` 只记 Shadow 自己的写入，看不到用户手动点击、通知栏、
    App 异步刷新。决策（可能包含一次几秒的模型调用）与执行之间必须能比对「页面还是
    不是那一屏」，否则就是典型 TOCTOU：拿 A 屏的坐标去点 B 屏。
    """

    stale_observations: int = 0
    """连续几次在「决策 → 执行」之间发现页面已变。

    必须计数：判 stale 就 `continue` 重新观察，如果 stale 判定本身有抖动，
    不加计数就会变成死循环。连续多次都稳不下来 → 按瞬时故障处理。"""


@dataclass
class ApprovalGrant:
    """一次人工放行：**只对这一个任务、这一个动作、这一版目标与这一版计划、
    且批准后未执行过任何其它动作时有效**（V2.7 P0-2）。

    布尔开关表达不了「批准的是哪一个动作」，于是批准完 A 之后紧接着出现的 B 也会被
    静默放行。凭证化之后放行前必须逐项匹配，而且**匹配成功即消费**——人工批准是
    「这一次可以」，不是「这个任务以后都可以」。

    绑定维度（V2.7 P0-2 补强，审查指出原实现漏了 task_id 与尝试身份）：
    - `task_id`：防止 A 任务的放行凭证拿去放行 B 任务的同名动作（跨任务串号）；
    - `attempt_seq`：批准那一刻的执行序号。放行时校验它没变——若批准后模型已经
      执行过别的动作（attempt_seq 前进过），这张凭证就作废。这是「绑定具体动作尝试」
      的等价物：真正的 attempt_id 要到 Act 阶段才分配（批准时还不存在），
      用 attempt_seq 快照同样能锁住「批准的到底是哪一次」；
    - `page_bound_fingerprint`（V3.1 P2-9）：动作 + 批准时所在的那一屏。
      裸动作指纹在跨页面时不可比，授权必须绑页面。
    """

    task_id: str
    action_fingerprint: str
    """**动作身份**（类型 + 目标量化 + 取值）。用于审计「批的是哪一个动作」。"""

    page_bound_fingerprint: str
    """**动作 + 当时那一屏**（V3.1 P2-9）。这是放行时真正比对的字段。

    裸 `action_fingerprint` 表达不了「在哪一屏」：`tap((500, 800))` 在微信、淘宝、
    设置里是三个完全不同的动作。授权凭据必须绑页面，否则「批准微信里的发送」
    在别处恰好有同坐标按钮时会被误用。
    """

    task_version: int
    plan_version: int
    attempt_seq: int

    def matches(self, action: Action, task: Task, state, *, package: str = "", activity: str = "") -> bool:
        """放行前逐项比对。任何一项对不上 → 凭证作废，重新请求确认。

        `package` / `activity` 是**放行那一刻**的当前页面（由调用方从最新观察里传进来）。
        与凭据里记录的页面不一致时作废——这正好覆盖「批准之后页面被换掉了」这种
        TOCTOU 场景（V3.1 P1-6 / P2-9）。
        """
        if (
            self.task_id != task.id
            or self.action_fingerprint != action.fingerprint
            or self.task_version != task.version
            or self.plan_version != task.plan_version
            or self.attempt_seq != state.attempt_seq
        ):
            return False
        # 拿不到页面信息时（凭据或当前观察任一为空）不做页面比对：不能凭空造证据，
        # 也不能因为「读不到页面」就把一张本来正确的凭据永久作废。
        if self.page_bound_fingerprint and (package or activity):
            if self.page_bound_fingerprint != action.page_bound_fingerprint(package, activity):
                return False
        return True

    def describe(self) -> str:
        return (
            f"task={self.task_id} fingerprint={self.action_fingerprint} "
            f"page_bound={self.page_bound_fingerprint} "
            f"task_version={self.task_version} plan_version={self.plan_version} "
            f"attempt_seq={self.attempt_seq}"
        )
