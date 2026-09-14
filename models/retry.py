"""错误分类与重试策略（V2.1 §十二 / §十三）。

背景（审核指出的问题）：
    RuntimeState.retry_count 以 MAX_RETRY_COUNT = 3 为上限，
    TaskStep.max_retries 默认却是 2 —— 两套重试状态并存，
    「一个步骤到底能重试 2 次还是 3 次」没有答案。

    更糟的是错误不分类：ADB 超时（重试有意义）、付款被拒（重试无意义，
    只会重复扣款）、JSON 解析失败（该重规划而不是重试）、用户拒绝
    （绝不能再问一次）—— 全都只是 `retry_count += 1`。

修法：
    1. 错误先分类（`ErrorClass`），分类依据是错误文本，不需要调用方改造。
    2. 由**唯一一份** `RetryPolicy` 决定下一步干什么（`RetryAction`）。
    3. 步骤级重试上限 `step_max_retries` 从同一份策略派生，
       `build_steps` 不再硬编码 2，Runtime 不再硬编码 3。

这样三种预算有了明确语义分工：
    - TRANSIENT  设备/网络抖动 → 同一动作再试（max_transient 次）
    - 其余        换策略 / 重规划（max_replan 次，也是步骤级上限）
    - 无法判断    试到上限后交给人（ASK_HUMAN），不再无限重试
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ErrorClass(str, Enum):
    """错误的性质，决定「值不值得重试」。"""

    TRANSIENT = "transient"
    """设备/网络抖动（ADB 超时、设备离线、429/5xx）。同一动作再试通常能过。"""

    ACTION_REJECTED = "action_rejected"
    """App 明确拒绝了这次操作（付款被拒、密码错误、按钮置灰）。
    重试同一个动作没有意义，只会重复触发副作用。"""

    PARSE_ERROR = "parse_error"
    """模型输出/响应解析失败。要的是重新规划，不是把同一个动作再发一遍。"""

    USER_DENIED = "user_denied"
    """人被问过后否决了。绝不能换个说法再问一次，必须换做法。"""

    UNKNOWN = "unknown"
    """判断不了属于哪一类。给有限的试探机会，用尽后交给人。"""

    FATAL = "fatal"
    """不可逆或无法恢复（预算耗尽、设备彻底不可用）。立即放弃。"""


class RetryAction(str, Enum):
    """错误发生后的应对动作。"""

    RETRY = "retry"
    """同一动作再执行一次（限于 TRANSIENT）。"""

    REPLAN = "replan"
    """换一种做法（重规划 / 走 Re-plan 通道）。"""

    ASK_HUMAN = "ask_human"
    """自己判断不了，转人工确认（HITL）。"""

    ABORT = "abort"
    """放弃，任务判失败。"""


@dataclass(frozen=True)
class RetryDecision:
    action: RetryAction
    reason: str


# ---- 分类规则（按文本匹配，调用方无需改造）----

_TRANSIENT = re.compile(
    r"timeout|timed out|超时|离线|offline|unreachable|连接失败|connection"
    r"|\badb\b|设备|device|网络|network|429|503|502|504|5\d\d|暂时|临时",
    re.IGNORECASE,
)
_ACTION_REJECTED = re.compile(
    r"拒绝|被拒|拒付|rejected|denied by app|密码错误|验证码|余额不足|不可用|置灰"
    r"|disabled|not clickable|无法点击|已下架|库存不足",
    re.IGNORECASE,
)
_PARSE_ERROR = re.compile(
    r"json|解析|parse|格式|format|decode|反序列化|非法|invalid|schema|expect",
    re.IGNORECASE,
)
_USER_DENIED = re.compile(r"人工否决|用户否决|用户拒绝|被人工|user denied", re.IGNORECASE)
_FATAL = re.compile(r"预算|budget|上限|exhausted|设备丢失|no device|未授权|unauthorized|401|403", re.IGNORECASE)


def classify_error(message: str | None) -> ErrorClass:
    """按错误文本判定错误类别（**兜底路径**，V2.7 P2-2）。

    判定顺序有意如此：先认「人否决过」和「不可逆」，这两类优先级最高，
    绝不能被后面的宽泛关键词（比如「设备」）抢走。

    注意这是**最后一道兜底**：能拿到结构化信息时（异常对象、执行结果里的
    `error_class`）应走 `classify_exception` / `classify_result` 的结构化分支。
    同一个错误在不同层措辞不同（`device offline` / `adb: device offline` /「设备已离线」），
    按文本匹配迟早会漏。
    """
    text = (message or "").strip()
    if not text:
        return ErrorClass.UNKNOWN
    if _USER_DENIED.search(text):
        return ErrorClass.USER_DENIED
    if _FATAL.search(text):
        return ErrorClass.FATAL
    if _ACTION_REJECTED.search(text):
        return ErrorClass.ACTION_REJECTED
    if _PARSE_ERROR.search(text):
        return ErrorClass.PARSE_ERROR
    if _TRANSIENT.search(text):
        return ErrorClass.TRANSIENT
    return ErrorClass.UNKNOWN


_ERROR_CLASS_BY_VALUE: dict[str, ErrorClass] = {member.value: member for member in ErrorClass}


def classify_exception(exc: BaseException) -> ErrorClass:
    """按**异常类型**分类（V2.7 P2-2）：结构化优先，比文本匹配可靠得多。

    我们自己的异常体系自带 `error_class`（见 `models.exceptions`），标准库异常按类型判断，
    两者都没有才落到 UNKNOWN，由调用方决定怎么处置。
    """
    declared = getattr(exc, "error_class", None)
    if declared is not None:
        found = _ERROR_CLASS_BY_VALUE.get(str(declared))
        if found is not None:
            return found
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return ErrorClass.TRANSIENT
    if isinstance(exc, (ValueError, UnicodeDecodeError)):  # JSONDecodeError 也在这条线上
        return ErrorClass.PARSE_ERROR
    return ErrorClass.UNKNOWN


def classify_result(result: dict | None) -> ErrorClass:
    """从执行器返回值分类（executor 永不抛异常，失败是 ``{"ok": False, ...}``）。

    V2.7 P2-2：**优先读结构化字段 `error_class`**，没有才回退到文本匹配。
    executor / runtime 收敛异常时会把 `classify_exception` 的结果一起带上，
    于是下游不必再从一句中文错误里猜它属于哪一类。
    """
    if result is None:
        return ErrorClass.UNKNOWN
    if result.get("ok"):
        return ErrorClass.TRANSIENT  # 没出错，分类无意义
    declared = result.get("error_class")
    if declared:
        found = _ERROR_CLASS_BY_VALUE.get(str(declared))
        if found is not None:
            return found
    return classify_error(str(result.get("error") or ""))


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略：全系统只有这一处定义「能重试几次、失败后干什么」。

    两个上限语义不同，不要合并：
    - ``max_transient`` 设备抖动的重试次数（同一动作，便宜且安全）
    - ``max_replan``    换策略的次数（每次都要调模型，贵）
      它同时是步骤级 ``TaskStep.max_retries`` 的来源。
    """

    max_transient: int = 3
    max_replan: int = 2
    max_unknown: int = 1

    def limit_for(self, error_class: ErrorClass) -> int:
        if error_class is ErrorClass.TRANSIENT:
            return self.max_transient
        if error_class is ErrorClass.UNKNOWN:
            return self.max_unknown
        return self.max_replan

    def decide(self, error_class: ErrorClass, attempt: int) -> RetryDecision:
        """``attempt`` 是**已发生**的失败次数（0 表示第一次失败）。"""
        if error_class is ErrorClass.FATAL:
            return RetryDecision(RetryAction.ABORT, "错误不可逆，直接终止")

        if error_class is ErrorClass.USER_DENIED:
            return RetryDecision(
                RetryAction.REPLAN, "该做法已被人工否决，必须换一种做法（不重试同一动作）"
            )

        limit = self.limit_for(error_class)

        if error_class is ErrorClass.TRANSIENT:
            if attempt < limit:
                return RetryDecision(
                    RetryAction.RETRY, f"瞬时错误，重试第 {attempt + 1}/{limit} 次"
                )
            return RetryDecision(RetryAction.ABORT, f"瞬时错误已重试 {limit} 次仍失败")

        if error_class is ErrorClass.UNKNOWN:
            if attempt < limit:
                return RetryDecision(
                    RetryAction.RETRY, f"错误类别不明，试探性重试第 {attempt + 1}/{limit} 次"
                )
            return RetryDecision(
                RetryAction.ASK_HUMAN, f"连续 {attempt + 1} 次失败且无法判断原因，转人工确认"
            )

        # ACTION_REJECTED / PARSE_ERROR：重试同一动作没有意义，只能换策略
        if attempt < limit:
            return RetryDecision(
                RetryAction.REPLAN,
                f"{error_class.value} 重试无意义，换策略（第 {attempt + 1}/{limit} 次）",
            )
        return RetryDecision(RetryAction.ABORT, f"{error_class.value} 已换策略 {limit} 次仍失败")

    @property
    def step_max_retries(self) -> int:
        """步骤级重试上限——与 Runtime 共用同一份策略，消除「2 次还是 3 次」的歧义。"""
        return self.max_replan


DEFAULT_POLICY = RetryPolicy()
"""全系统默认策略。想调重试行为只改这一处。"""
