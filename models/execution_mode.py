"""执行模式与执行目标（V5 §三 / §十一 / §十六，依据 `问题修复.md`）。

**这一层解决的问题**：Shadow 以前只有一台设备、一个屏幕、一个 UI 状态空间。
`DeviceSession` 的所有权（`_owner = task_id`）回答的是「哪个 **Agent** 拿设备控制权」，
却回答不了「**用户**正在用这台设备时，Agent 到哪里去执行」。

于是出现一个物理冲突：

    用户操作手机 ──┐
                   ├──→ 真实 Display 0（同一个 UI 状态空间）
    Agent 操作手机 ─┘

Agent 的下一次 `tap()` 会打到用户当前正看着的那一屏上。这不是调度问题——
抢占/恢复再完美，也只是「轮流用同一个屏幕」，不是「同时用两个屏幕」。

所以把「在哪里执行」从调度器里拆出来，变成**任务自带的属性**：

    ExecutionMode.FOREGROUND  →  当前屏幕直接执行（旧行为；`DeviceSession` 的语义）
    ExecutionMode.SHADOW      →  影子环境执行，不碰用户的屏幕
    ExecutionMode.HYBRID      →  能后台的后台，不能后台的才申请前台

`FOREGROUND` 是默认值，**因此这次改动不改变任何既有任务的语义**——
只是在模型层把「另一个平面」表达出来，好让调度器与运行时能区分对待
（见 `agent/scheduler.py` 的 `_maybe_preempt` 与 `agent/runtime.py` 的 `safe_point`）。

------
本模块的边界：**只有数据，没有行为**。

放到 `device/` 下是不合适的——`ExecutionTarget` 要进 `Task`、`Checkpoint`、
`/tasks` 响应，而 `device/` 是「怎么操作一台设备」的实现层。放进 `models/`
才能让 `models/task.py` 依赖它而不产生 `models → device` 的反向依赖
（`tests/test_device_port.py` 钉住了「核心只认设备端口」这条边界，反向依赖会破坏它）。

`ExecutionTarget` 刻意用 `dataclass(frozen=True)` 而不是 pydantic：它是**运行期
路标**（「这次执行往哪走」），不落盘、不进 API 响应。真正需要跨重启存活的是
`Task.execution_mode` / `Task.session_id` 与 `Checkpoint` 上的那几个字段，
让它们分别落在各自的模型里，比让一个「什么场合都带上」的万能对象到处漂要清楚。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any

from pydantic import BeforeValidator


def _parse_enum(value: object, enum_cls: type[Enum], default: Enum) -> Enum:
    """把外部输入（API 字符串、旧记录里的值）解析成枚举成员。

    认不出来时返回 `default` 而不是抛异常。调用点之一是**反序列化旧任务记录**，
    那里抛异常会让一条本来能跑的历史任务变成「数据损坏」（`TaskStore` 会把它
    隔离进 `corrupt_ids`，用户看到的是任务凭空消失）。

    **但严格性不能一概而论**，两类入口的期望相反：

    - `POST /tasks` 这类**用户提交**的入口要严格：写了 `"shodow"` 这种拼错的值，
      静默按 `FOREGROUND` 执行 = 用户以为在后台安全跑、实际占着他的屏幕。
      所以 pydantic 的 `TaskRequest.execution_mode: ExecutionMode` 保持**严格**
      （非法值 → 422），不走这里。
    - 读**既有记录**的入口要宽容：未知值只可能来自「记录是更新版本写的」
      或「记录损坏」，两种情况下都不该让整个任务 load 不出来。

    所以宽容逻辑只挂在 `Task` 的字段上（见 `_loose_mode`），不挂在 API 请求模型上。
    """
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            return default
    return default


def _loose_mode(value: object) -> ExecutionMode:
    """pydantic `BeforeValidator` 钩子：把未知的执行模式**降级成 FOREGROUND**。

    为什么降级到 `FOREGROUND` 而不是 `SHADOW`：`FOREGROUND` 是「按老规矩、
    老老实实占着屏幕干」，最坏结果是**打扰用户**；降级到 `SHADOW` 意味着
    「去一个可能并不存在的执行平面干活」，最坏结果是**动作不知道打到哪去了**。
    两种失败模式的严重性差着量级。

    **注意这不削弱 `POST /tasks` 的严格性**：那条路走 `TaskRequest` 的字段类型注解
    （`ExecutionMode`，严格枚举），非法值在进 `TaskManager.create` 之前就 422 了。
    """
    return _parse_enum(value, ExecutionMode, ExecutionMode.FOREGROUND)  # type: ignore[return-value]


class ExecutionMode(str, Enum):
    """任务在哪里执行（V5 §三）。

    三个值的分界**不是**「谁更优先」，而是「会不会占用用户正在看的那个屏幕」：

    - `FOREGROUND`：会。执行期间用户不能同时用手机（否则两边互相打断）。
    - `SHADOW`：不会。Agent 在自己的执行平面里跑，用户的 Display 0 完全不受影响。
    - `HYBRID`：看动作。大部分动作在影子平面，只有影子做不到的才回落到前台。
    """

    FOREGROUND = "foreground"
    """当前屏幕直接执行。**默认值**，即 V4.5 之前的所有行为。"""

    SHADOW = "shadow"
    """影子环境执行（V5 §二／§十五）。要求后端真的具备独立执行平面。"""

    HYBRID = "hybrid"
    """按动作区分：能后台的后台，不能后台的才申请前台（V5 §三）。"""


# 执行模式的**宽松**字段类型：读既有记录时用。
#
# 与裸 `ExecutionMode` 的区别：这个类型注解在解析失败时降级成 `FOREGROUND`
# 而不是抛 `ValidationError`。为什么需要它——`TaskStore` 对「反序列化不出来的记录」
# 的处理是**隔离**（进 `corrupt_ids`，`GET /tasks/{id}` 返回 `recovery_error`），
# 所以一个未知的 mode 值会让整条任务从列表里消失。可这个值在现实中只可能来自
# 「记录是更新版本写的」（例如以后真做出 v5 加了新平面）——那时把老版本的进程
# 变成「看到新记录就当它坏了」显然不对。
#
# 写入口（API）仍然用严格的 `ExecutionMode`：这是「读宽写严」，
# 两条路各取所需，不是双重标准。
LooseExecutionMode = Annotated[ExecutionMode, BeforeValidator(_loose_mode)]


class ExecutionRequirement(str, Enum):
    """一个动作对「用户是否需要在场」的要求（V5 §十一）。

    与 `ExecutionMode` 的区别：`ExecutionMode` 是**任务**的属性（用户提出的期望），
    `ExecutionRequirement` 是**动作**的属性（这件事物理上能不能背着人干）。
    两者相乘才得出「这个动作能不能在影子平面做」。

    典型映射（文档 §十一 的例子）：

        打开淘宝        → SHADOW_ONLY        影子平面完全够用
        搜索商品        → SHADOW_ONLY
        加入购物车      → SHADOW_PREFERRED   优先影子；影子不可用时可回落到前台
        支付            → USER_REQUIRED      必须真实用户参与，不能偷偷做

    **注意 `USER_REQUIRED` ≠ `ActionRisk.DANGEROUS`**：两者正交。
    付款既要转人工（风险门禁的结论）又必须用户在场（执行要求的结论），
    但「生物识别」是 USER_REQUIRED 而非危险动作，「删除本地草稿」是 DANGEROUS
    却不需要用户在场。混在一起会让「影子平面能不能做」这个问题失去答案。
    """

    SHADOW_ONLY = "shadow_only"
    """只能在影子平面做。当前端做不到时，**宁可不做**也不许落到用户屏幕上。"""

    SHADOW_PREFERRED = "shadow_preferred"
    """优先影子平面；影子不可用且动作本身安全时，允许回落到前台。"""

    USER_REQUIRED = "user_required"
    """必须真实用户参与（生物识别 / OTP / 系统授权 / 相机 / NFC / 支付确认）。
    影子平面**和**前台自动执行都不行，只能转人工。"""


# 默认执行要求：按 `ActionType` 判定，缺省值就是「影子优先」。
#
# 为什么缺省不是 `SHADOW_ONLY` 也不是 `USER_REQUIRED`：
#   - `SHADOW_ONLY` 做缺省，等于宣称「任何没被我分类的动作都能安全地背着用户做」——
#     这是一个**没有证据的强断言**，而它的失败模式是「用户没看见的动作已经发生了」。
#   - `USER_REQUIRED` 做缺省，等于宣称「任何动作都要用户在场」——门禁变成噪声，
#     然后被绕过（V3.1 保守化时踩过的同一个坑：一刀切的保守 = 什么都没拦）。
#   - `SHADOW_PREFERRED` 居中：优先不打扰用户，但**允许**在影子不可用时回落，
#     而回落这一跳会经过风险门禁（见 `agent/risk_gate.py`）。
DEFAULT_REQUIREMENT = ExecutionRequirement.SHADOW_PREFERRED

# 需要用户本人在场才能完成的动作（V5 §十一）。这是**物理约束**，不是安全策略：
# 这些动作要求真人（指纹/人脸/短信/硬件），程序没法替。
#
# 判定按「动作类型 + 语义角色」而不是文本关键词——理由与 `ActionRiskGate` 相同：
# 文本匹配能被改一个字的按钮标题绕过。
USER_REQUIRED_SEMANTIC_ROLES: frozenset[str] = frozenset(
    {
        # 支付 / 转账类：几乎必然触发生物识别或密码
        "payment",
        "transfer",
        "checkout",
        # 身份验证类：OTP、实名、绑卡
        "auth",
        "identity",
        "otp",
        "credential",
    }
)


def normalize_mode(value: object) -> ExecutionMode:
    """把任意外部输入规范成 `ExecutionMode`，认不出来时**保守回退 FOREGROUND**。

    供**非 pydantic** 的路径使用（`ExecutionTarget` 的构造、日志、调度器读
    `Task.execution_mode` 时的防御）。pydantic 路径请用 `LooseExecutionMode`
    （读）或裸 `ExecutionMode`（写）。
    """
    return _parse_enum(value, ExecutionMode, ExecutionMode.FOREGROUND)  # type: ignore[return-value]


def normalize_requirement(value: object) -> ExecutionRequirement:
    """把任意外部输入规范成 `ExecutionRequirement`，认不出来时回退到默认要求。"""
    return _parse_enum(value, ExecutionRequirement, DEFAULT_REQUIREMENT)  # type: ignore[return-value]


@dataclass(frozen=True)
class ExecutionTarget:
    """这一次执行「往哪走」的路标（V5 §十六）。

    存在的理由是**给执行器一个统一的问题答案**：`AgentRuntime` 的 planner 只回答
    「下一步做什么」，`ExecutionTarget` 回答「在哪里做」。把后者做成一个显式对象
    而不是在执行器里散落 if-else，才能满足文档 §十二 的职责拆分要求：

        planner      → 下一步做什么（不需要知道手机有没有用户）
        executor     → 在哪里做（`ExecutionTarget` 就是它的输入）

    字段说明：

    - `mode`：任务声明的执行模式。注意它可能是 `HYBRID`——**任务级**声明，
      单个动作最终落在哪个平面还要看 `requirement` 与本端实际能力。
    - `session_id`：执行会话 id。影子平面里不同任务的执行环境必须彼此隔离，
      这个 id 就是隔离的句柄（`device/session.py` 的 `ShadowSession`）。
      `FOREGROUND` 模式下为空串——前台平面只有一台设备、一个会话，不需要句柄。
    - `device_serial`：落在哪台设备上。影子平面里它可能与前台的设备相同
      （同一台手机上的两个执行平面），所以**不能**用它来判断平面。
    - `display_id`：影子显示 id。`FOREGROUND` 恒为 0（真实 Display 0）；
      `None` 表示「本端拿不到显示 id」——**不是**「用默认显示」。
      消费方必须区分这两个含义：拿不到 id 时不能假装自己知道往哪画。
    - `requirement`：这个动作对用户在场的要求（见 `ExecutionRequirement`）。
    """

    mode: ExecutionMode = ExecutionMode.FOREGROUND
    session_id: str = ""
    device_serial: str = ""
    display_id: int | None = None
    requirement: ExecutionRequirement = DEFAULT_REQUIREMENT

    @property
    def is_shadow(self) -> bool:
        """这次执行是否落在影子平面。

        `HYBRID` 返回 True：它的**意**图是「能不打扰用户就不打扰」，
        具体到某个动作能不能真做到由 `requirement` 说话。
        判定「是否一定会碰用户屏幕」请用 `requires_foreground`。
        """
        return self.mode in (ExecutionMode.SHADOW, ExecutionMode.HYBRID)

    @property
    def requires_foreground(self) -> bool:
        """这次执行是否**必定**占用用户当前屏幕。

        只有两种情况必定占用：任务声明了 `FOREGROUND`，或这个动作物理上
        需要用户在场（`USER_REQUIRED`）。后者即使在影子模式里也必须转人工——
        §十一 的支付宝例子：影子环境也绕不过生物识别。
        """
        return self.mode is ExecutionMode.FOREGROUND or (
            self.requirement is ExecutionRequirement.USER_REQUIRED
        )

    def describe(self) -> str:
        """给日志/事件用的一行摘要。不含密钥类信息，可以进审计。"""
        where = f"display={self.display_id}" if self.display_id is not None else "display=?"
        session = self.session_id or "-"
        return (
            f"mode={self.mode.value} requirement={self.requirement.value} "
            f"session={session} {where} device={self.device_serial or '-'}"
        )
