"""Shadow 核心异常（V2.3 Reliability）。

把「状态机拒绝」「关键持久化失败」「绑定设备不可用」这类会改变任务命运的异常
集中定义，避免各个模块各自抛裸 RuntimeError。

V2.7 P2-2 起每个异常还自带 `error_class`：错误分类**优先看它**，文本匹配只作兜底。
同一个错误在不同层可能有完全不同的措辞（`device offline` / `adb: device offline` /
「设备已离线」），但对异常类型来说它始终是同一个类——按类型分比按文本分可靠得多。
这里写字符串而不是直接引用 `models.retry.ErrorClass`，是为了避免「底层设施依赖上层
策略」的反向依赖。
"""
from __future__ import annotations


class ShadowError(RuntimeError):
    """Shadow 内部错误基类。"""

    #: 取值同 `models.retry.ErrorClass`；子类按语义覆盖
    error_class = "unknown"


class InvalidTransitionError(ShadowError):
    """状态机拒绝的迁移，尤其是终态试图复活。"""

    # 状态机都不让迁了，说明调用方写错了——重试没有意义
    error_class = "fatal"

    def __init__(self, task_id: str, from_status: str, to_status: str, source: str = "") -> None:
        self.task_id = task_id
        self.from_status = from_status
        self.to_status = to_status
        self.source = source or "未标注"
        super().__init__(
            f"任务 {task_id} 不允许从 {from_status} 迁移到 {to_status}（来源 {self.source}）"
        )


class PersistenceError(ShadowError):
    """关键持久化失败：内存状态不能再继续领先于 durable state。"""

    # 状态落不了盘：继续跑会在崩溃恢复后重复副作用
    error_class = "fatal"

    def __init__(self, task_id: str, reason: str) -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"任务 {task_id} 持久化失败：{reason}")


class InvalidExecutionTransition(ShadowError):
    """一条**执行**（`ActionExecution`）的状态迁移被状态机拒绝（v4.1 §五）。

    与 `InvalidTransitionError`（任务状态机）刻意分开，理由不是洁癖：
    两者的状态集、守卫与「谁有资格迁移」完全不同，混成一个类型之后，
    「日志里看到 InvalidTransitionError」不再能说明出问题的是一条任务还是一次动作。

    它表示的是**调用方写错了**（例如想从 `SUCCEEDED` 回到 `RUNNING`），
    而不是并发竞争——并发导致的守卫落空由 `ExecutionStore.transition()` 返回 `None`
    表达（那是预期内的、可以重读一次再决定的情形）。所以 `error_class = fatal`。
    """

    error_class = "fatal"

    def __init__(self, execution_id: str, from_status: str, to_status: str) -> None:
        self.execution_id = execution_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"执行 {execution_id} 不允许从 {from_status} 迁移到 {to_status}"
        )


class DeviceUnavailableError(ShadowError):
    """任务绑定的设备当前不在池中。"""

    # 设备只是暂时不在（拔线、模拟器重启），等它回来还能接着做
    error_class = "transient"

    def __init__(self, task_id: str, serial: str) -> None:
        self.task_id = task_id
        self.serial = serial
        super().__init__(f"任务 {task_id} 绑定的设备 {serial} 不可用")


class CorruptDataError(PersistenceError):
    """从持久化层读出的数据损坏或无法解析。

    继承 `PersistenceError.error_class = "fatal"`：坏数据要人来处理，重试不会变好。
    """

    def __init__(self, key: str, path: str, reason: str) -> None:
        self.key = key
        self.path = path
        self.reason = reason
        super().__init__(key, f"数据损坏 {path}：{reason}")


class ActionArgumentError(ShadowError):
    """Action 参数非法——executor 在下发到设备**之前**就判定这个动作没法执行。

    例如 `SWIPE` 没给 4 个坐标、`TYPE` 没给 value、时长超出合法区间。
    它与设备后端无关（ADB 和 Android 都会遇到），所以放在核心异常里，
    而不是让 `agent/executor.py` 去 import 某个后端模块的异常类型
    ——那正是 V3.3 §1 要拆掉的「ADB 泄漏进核心」的一种。

    `error_class` 保持 `unknown`（与升级前 `AdbError` 的分类结果一致）。
    把它改成 `parse_error` 会让重试策略从「重试」变成「换策略」，
    那是一次独立的语义改动，不在本轮范围内（见 README 的延期说明）。
    """

    error_class = "unknown"


class ConcurrentModificationError(ShadowError):
    """乐观并发校验失败：手里这份 Task 快照已经不是最新的一份（V2.4 §二）。

    典型场景：Runtime 刚把任务判成 DONE 并落盘，TaskManager 还拿着「之前读到的
    RUNNING」准备改写 instruction。放任写入的话，磁盘上会出现

        status = done  +  instruction = 新目标

    这种「语义损坏」——状态机看不出问题，因为 status 根本没变，但任务的核心
    语义（它到底在做什么）已经被改掉了。CAS 让后到的写入者**失败**，
    而不是静默覆盖。
    """

    # 并发冲突：重新读一次、换个时机再试通常就好了，不是致命错误
    error_class = "transient"

    def __init__(
        self,
        task_id: str,
        expected_revision: int,
        actual_revision: int,
        source: str = "",
    ) -> None:
        self.task_id = task_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        self.source = source or "未标注"
        super().__init__(
            f"任务 {task_id} 已被并发修改（期望 revision={expected_revision}，"
            f"实际 {actual_revision}，来源 {self.source}）"
        )
