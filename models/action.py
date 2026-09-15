"""Action Schema（§3.2）+ 动作风险等级与指纹（V2 §十四 / §十七 / V2.2 §一）。

风险这件事有**两个来源**，本模块只负责其中「动作自身能算出来的那一半」：

    policy_risk()  ——  服务端策略（动作类型 + 文本关键词）
    model_risk()   ——  模型声明（`risk` 权威标注 / `risk_hint` 仅建议）

真正的合议在 `agent.risk_gate.ActionRiskGate`：它还能拿到 UI 树、当前页面等
上下文，因此能判出「点击红色按钮」这种本模块看不见的风险。**任何会改设备的动作
都必须过那道门禁**，本模块的 `resolved_risk()` 只是没有上下文时的退化版本。
"""
from __future__ import annotations

import hashlib
from enum import Enum

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    TAP = "tap"
    LONG_PRESS = "long_press"
    TYPE = "type"
    SWIPE = "swipe"
    BACK = "back"
    HOME = "home"
    LAUNCH = "launch"
    WAIT = "wait"
    DONE = "done"
    DONE_REQUEST = "done_request"
    """模型**申请**完成（V2.2 §四）。

    语义上与 DONE 等价，改这个名字是为了让「申请」这个性质在数据里显式可见：
    模型说 done 不等于任务 done，最终由 `agent.goal_verifier` 依据独立证据裁定。
    """


# 所有「宣告完成」的动作类型。判断完成申请一律用 `is_completion_request`，
# 不要到处写 `type is ActionType.DONE`——那样新增别名时必漏。
COMPLETION_ACTION_TYPES = frozenset({ActionType.DONE, ActionType.DONE_REQUEST})


class ActionRisk(str, Enum):
    """动作风险等级。用于在 Executor 之前插入确认门禁（HITL），而不是事后补救。"""

    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"


# effective_risk 取「服务端策略」与「模型建议」中更严格的一个：DANGEROUS > CAUTION > SAFE
RISK_ORDER = {ActionRisk.SAFE: 0, ActionRisk.CAUTION: 1, ActionRisk.DANGEROUS: 2}


def risk_rank(risk: "ActionRisk") -> int:
    """风险的严重度数值（越大越严格）。"""
    return RISK_ORDER[risk]


def strictest(*risks: "ActionRisk") -> "ActionRisk":
    """取最严格的一个。空的输入按 SAFE 处理（没有风险信息 ≠ 有风险）。"""
    return max(risks or (ActionRisk.SAFE,), key=risk_rank)


class ActionEffectStatus(str, Enum):
    """一次动作「到底有没有在设备上生效」的判定（V2.1 §五 · V2.3）。

    手机 Agent 最大的恢复难题不是「当前页面是什么」，而是「上一次动作到底执行没执行」。
    例如进程在「点击提交订单」之后、拿到验证截图之前崩溃：重启时只知道 Step = 提交订单，
    却不知道订单到底提交没提交——若直接重试就会重复下单。

    - NOT_STARTED      ：还没发出去
    - DISPATCHED       ：ADB 命令已发出（executor 返回 ok），但还没验证页面变化
    - EFFECT_UNKNOWN   ：dispatch 后拿不到验证观察 → 效果未知，绝不能默认 retry
    - NAVIGATED        ：页面确实发生了导航/activity 变化，但不等于原始动作的最终 side effect 已确认
    - UI_CHANGED       ：页面结构或目标元素发生变化，但未确认是不可逆 side effect
    - VERIFIED_SUCCESS ：验证通过（页面确实按预期变化）
    - VERIFIED_FAILED  ：验证失败
    """

    NOT_STARTED = "not_started"
    DISPATCHED = "dispatched"
    EFFECT_UNKNOWN = "effect_unknown"
    NAVIGATED = "navigated"
    UI_CHANGED = "ui_changed"
    VERIFIED_SUCCESS = "verified_success"
    VERIFIED_FAILED = "verified_failed"


# 不改设备状态、或可轻易撤销的动作
SAFE_ACTION_TYPES = frozenset(
    {ActionType.BACK, ActionType.HOME, ActionType.WAIT, ActionType.DONE, ActionType.DONE_REQUEST}
)
# 会改变设备状态，但通常可撤销
CAUTION_ACTION_TYPES = frozenset(
    {ActionType.TAP, ActionType.LONG_PRESS, ActionType.TYPE, ActionType.SWIPE, ActionType.LAUNCH}
)

# 时长类参数的合理区间（毫秒）：`wait` / `long_press` / `swipe` 共用。
#
# 下限取 1 而非 0——`wait(0)` 是无意义空转，未来若出现轮询容易演变成忙循环；
# 上限防 VLM 返回天文数字把任务卡死。
#
# V3.3 从 `device/adb.py` 挪到这里：它是**动作参数的合法区间**，不是 ADB 的实现细节。
# 放这儿之后 `agent/executor.py` 不必为了校验一个参数去 import 设备后端
# （`device.adb` 仍 re-export，老导入路径继续有效）。
DURATION_RANGE_MS = (1, 60_000)
# 理应让页面或控件状态发生变化的动作（UIA 层验证只对这类动作有意义）
MUTATING_ACTION_TYPES = frozenset(
    {ActionType.TAP, ActionType.LONG_PRESS, ActionType.TYPE, ActionType.SWIPE, ActionType.LAUNCH}
)

# 命中即升级为 DANGEROUS：这类动作在真实 App 里往往不可撤销（下单 / 转账 / 删除）
#
# V3 M2 起**降级为遗留材料**：风险/幂等的判定已统一走 `models.semantic` 的语义角色
# （`infer_role` → `ROLE_SEMANTICS`），不再直接 grep 本表。保留它只为 `policy_risk_hits()`
# 的日志/审计用途与历史兼容；新增语义动作请在 `models.semantic.ROLE_KEYWORDS` 里归类。
DANGEROUS_KEYWORDS = (
    # 交易与资金
    "支付", "付款", "下单", "购买", "结算", "转账", "汇款", "提现", "充值", "退款", "扣款",
    "开通", "订购", "续费", "订阅", "免密",
    # 内容与关系不可逆
    "发送", "提交", "确认", "删除", "移除", "解绑", "注销", "解约", "退订", "清空", "格式化",
    # 授权与协议
    "同意", "授权", "允许访问", "获取验证码",
    # English
    "send", "pay", "purchase", "checkout", "check out", "delete", "remove", "confirm",
    "submit", "transfer", "unbind", "withdraw", "top up", "recharge", "subscribe",
    "authorize", "agree", "accept", "reset",
)

# 敏感 App 的 package 特征：在这些应用里，任何会改页面的动作至少按 CAUTION 对待。
# 只抬高到 CAUTION 而不是 DANGEROUS——否则一个「返回上一页」都会把任务卡进 HITL。
SENSITIVE_PACKAGE_MARKERS = (
    "pay", "bank", "wallet", "alipay", "tenpay", "unionpay", "credit", "money",
    "securities", "stock", "insurance", "billing",
)

# ---- 敏感**屏幕**特征文案（V4 · Policy Engine 第一步）----
#
# 包名只回答「这是不是支付/银行 App」，回答不了「**当前这一屏**是不是支付确认页」——
# 一个普通 App 里也能弹起收银台 / 转账 / 绑卡 / 验证码页。反过来，进了支付 App 的
# 首页（查余额、看账单）也未必在敏感操作上。真正该抬级的是「这一屏正在做敏感事」。
#
# 所以这里列「**敏感屏特征文案**」：UI 树里出现这些字样，说明当前屏属于
# 「确认支付 / 转账 / 充值 / 绑卡 / 验证码」这一类。它们与 `SENSITIVE_PACKAGE_MARKERS`
# 互补——包名是「静态的 App 身份」，这些是「动态的屏幕内容」。
#
# 判定口径与 `SENSITIVE_PACKAGE_MARKERS` 一样是 `marker in text`（小写后），
# 刻意**不含**「支付」「付款」这种会误伤的宽泛词（一个「支付方式」列表页不该抬级），
# 只收「金额 + 动作」这类强信号：出现在屏幕上几乎一定意味着不可逆操作。
SENSITIVE_SCREEN_MARKERS = (
    "确认支付", "立即支付", "确认付款", "确认转账", "转账金额", "付款金额",
    "支付金额", "实付金额", "确认下单", "提交订单", "确认充值", "充值金额",
    "绑定银行卡", "添加银行卡", "验证码", "短信验证码", "输入支付密码",
    "确认购买", "确认购买并支付", "余额支付", "指纹支付", "人脸支付",
)

# ---- 副作用幂等性（V2.7 P1-2）----
#
# 审核指出：**「风险等级」解决的是「能不能做」，「幂等性」解决的是「做过但不知道结果时
# 能不能再做一次」。** 两者不能混为一谈——同属 DANGEROUS 的动作里，「查余额」和「转账」
# 在 EFFECT_UNKNOWN 之后的正确处置完全不同。


class SideEffectClass(str, Enum):
    READ_ONLY = "read_only"
    """不改变外部世界：观察、返回、回桌面。重做没有任何副作用。"""

    IDEMPOTENT_WRITE = "idempotent_write"
    """会改页面，但重做等价：打开某页面、切 Tab、划动、点击进入下一步。"""

    NON_IDEMPOTENT_WRITE = "non_idempotent_write"
    """重做会产生「第二条」：发消息、点赞、关注、收藏、分享、提交表单。"""

    IRREVERSIBLE = "irreversible"
    """不可撤销：支付、转账、下单、删除、注销、解绑。"""


# V3 M2 起这两个词表已**零引用**：副作用幂等判定改走 `models.semantic.infer_role`，
# 语义角色（purchase/delete/submit/like…）→ `ROLE_SEMANTICS` 查表。保留定义仅为
# 历史兼容与可读性（它们是 `ROLE_KEYWORDS` 的原始材料），新增词请改 `models/semantic`。
IRREVERSIBLE_KEYWORDS = (
    "支付", "付款", "下单", "购买", "结算", "转账", "汇款", "提现", "扣款", "退款",
    "删除", "移除", "注销", "解绑", "解约", "退订", "清空", "格式化", "免密",
    "pay", "purchase", "checkout", "check out", "transfer", "withdraw", "delete",
    "remove", "unbind", "reset",
)

NON_IDEMPOTENT_KEYWORDS = (
    "发送", "提交", "点赞", "关注", "收藏", "分享", "转发", "评论", "报名", "预约",
    "确认", "同意", "授权", "订购", "订阅", "邀请", "添加好友", "发布", "投币",
    "send", "submit", "like", "follow", "share", "comment", "confirm", "agree",
    "accept", "subscribe", "invite", "post",
)

# 只读 / 导航类动作类型：重做它们不会改变外部世界
READ_ONLY_ACTION_TYPES = frozenset(
    {ActionType.WAIT, ActionType.BACK, ActionType.HOME, ActionType.DONE, ActionType.DONE_REQUEST}
)

# 会改变页面的基础动作类型（点击 / 输入 / 划动）
MUTATING_ACTION_TYPES = frozenset(
    {ActionType.TAP, ActionType.LONG_PRESS, ActionType.TYPE, ActionType.SWIPE, ActionType.LAUNCH}
)

# 坐标量化粒度：落在同一个 16px 网格里的点击视为同一个动作。
# 用向下取整而不是 round——round 在桶边界上会因 1px 抖动跳到相邻桶，让循环检测漏判。
_FINGERPRINT_QUANTUM = 16


class Point(BaseModel):
    x: float
    y: float


class Action(BaseModel):
    type: ActionType
    target: Point | str | None = None
    value: str | None = None
    reason: str = ""

    # 权威风险标注：由人 / 服务端 / 已确认的策略写入，参与 effective_risk 计算。
    risk: ActionRisk | None = None

    # 模型建议（V2.2 §一）：模型**没有**决定风险的权力，最多给个提示。
    # 单独一个字段的好处是「模型说的」与「人定的」在数据里分得开：
    # 审计时能一眼看出这级风险是抬上去的还是策略判出来的。
    risk_hint: ActionRisk | None = None

    # 模型对「目标已达成」的可核验声明（V2.2 §四）：形如
    #   {"package": "com.taobao.taobao", "activity": "DetailActivity", "text": "立即购买"}
    # 有它就由 goal_verifier 逐条与真实 Observation 比对，比对不过直接驳回完成申请。
    goal_evidence: dict[str, str] = Field(default_factory=dict)

    # ---- 风险 ----

    def _policy_haystack(self) -> str:
        return " ".join(
            str(part).lower() for part in (self.value, self.target, self.reason) if part is not None
        )

    def policy_risk_hits(self) -> list[str]:
        """命中的危险关键词。日志与审计要能回答「为什么这步被判成危险」。"""
        haystack = self._policy_haystack()
        return [keyword for keyword in DANGEROUS_KEYWORDS if keyword in haystack]

    def policy_risk(self) -> ActionRisk:
        """服务端规则推断的风险等级（类型 + 文本语义），**不采纳**模型声明。

        这是「没有上下文时」的版本：只看动作自身。完整策略风险（含 UI 节点文本、
        当前页面）在 `ActionRiskGate.policy_risk`——它会把这些信息一起算进来。

        V3 M2 起文本风险改走 `models.semantic.infer_role`：从「语义角色」查表派生，
        不再只认 `DANGEROUS_KEYWORDS`。这补上了一个真实漏洞——「点赞 / 关注 / 分享」
        这些动作副作用非幂等（不能重做），但旧逻辑判 SAFE（不在危险词表里），
        导致「不能重做」却「不危险」的矛盾。现在它们判 CAUTION。
        """
        from .semantic import SemanticRole, infer_role, semantic_for

        role = infer_role(self._policy_haystack())
        if role is not SemanticRole.UNKNOWN:
            return semantic_for(role).risk
        # 认不出语义 → 按动作类型兜底
        if self.type in SAFE_ACTION_TYPES:
            return ActionRisk.SAFE
        return ActionRisk.CAUTION

    def model_risk(self) -> ActionRisk:
        """模型声明的风险（用于参与 effective_risk 计算）。

        取 `risk` 与 `risk_hint` 中更严格的一个：历史调用方与人工标注写 `risk`，
        VLM 走 `risk_hint`。两者都为空时按 SAFE 处理——**这不等于降级**，
        因为 `strictest(policy, SAFE)` 恒等于 policy。要判断「模型有没有试图降级」，
        必须看 `declared_risk()`，而不是这个方法的结果。
        """
        return strictest(self.risk or ActionRisk.SAFE, self.risk_hint or ActionRisk.SAFE)

    def declared_risk(self) -> ActionRisk | None:
        """模型/人工**明确**声明的风险；没表态时返回 None。

        与 `model_risk()` 的区别很关键：没表态 ≠ 说了 safe。
        分不清这两者的话，每一个动作都会被记成「模型试图把风险降级成 safe」，
        告警立刻变成噪声，真正的降级尝试反而看不见了。
        """
        return strictest(self.risk or ActionRisk.SAFE, self.risk_hint or ActionRisk.SAFE) if (
            self.risk is not None or self.risk_hint is not None
        ) else None

    def resolved_risk(self) -> ActionRisk:
        """effective_risk = max(policy_risk, model_risk)。

        模型声明的风险只是「建议」：它可以把风险**说高**（要求更严格确认），
        但**不能把危险动作说低**。例如 `{"type":"tap","target":"确认付款","risk_hint":"safe"}`
        仍会被判定为 DANGEROUS——这是 HITL 门禁不被绕过的底线。
        """
        return strictest(self.policy_risk(), self.model_risk())

    # ---- 副作用幂等性（V2.7 P1-2）----

    def side_effect(self) -> SideEffectClass:
        """这次动作的副作用类型：决定「EFFECT_UNKNOWN 之后能不能再做一次」。

        与 `risk` 分工不同——风险管「能不能做」，幂等性管「做过但不知道结果时能不能再
        做一次」。派生规则刻意保守（拿不准就往重里判）：不可逆关键词 → IRREVERSIBLE；
        非幂等关键词 → NON_IDEMPOTENT_WRITE；只读 / 导航动作类型 → READ_ONLY；
        其余会改页面的动作 → IDEMPOTENT_WRITE。

        V3 M2 起改走 `models.semantic`：副作用与风险都从同一个「语义角色」查表派生，
        不再各自 grep 一套关键词（消灭旧 `IRREVERSIBLE_KEYWORDS` /
        `NON_IDEMPOTENT_KEYWORDS` 与 `DANGEROUS_KEYWORDS` 三表并存的矛盾）。
        函数内延迟 import 以避免 `semantic` ↔ `action` 的循环依赖。

        V3.1 P0-3：**角色未知时的类型兜底也必须是保守的**。原来「认不出 → 会改页面的
        动作一律 IDEMPOTENT_WRITE」意味着 `tap((540,1600))` 这种「按钮无文字、UI 解析
        也找不到节点」的动作被允许自动重试——而那个坐标可能是「确认支付」。现在：
        TAP / LONG_PRESS / TYPE 判非幂等（落点未知就不许自动重做）；SWIPE 例外，
        它只是改变页面位置，没有「点中某个按钮」这回事，重做等价。
        """
        from .semantic import SemanticRole, infer_role, semantic_for

        role = infer_role(self._policy_haystack())
        if role is not SemanticRole.UNKNOWN:
            return semantic_for(role).side_effect
        # 认不出语义 → 按动作类型分档兜底
        if self.type in READ_ONLY_ACTION_TYPES:
            return SideEffectClass.READ_ONLY
        if self.type is ActionType.SWIPE:
            return SideEffectClass.IDEMPOTENT_WRITE
        return SideEffectClass.NON_IDEMPOTENT_WRITE

    def is_safe_to_retry(self) -> bool:
        """效果未知时，能不能自动再执行一次（V2.7 P1-2）。

        只有**重做等价**的动作可以：只读动作重做没有副作用；打开页面 / 切 Tab / 划动
        这类重做也等价。而「发消息」「点关注」「提交表单」重做会产生第二条，
        「支付」「删除」重做更是不可逆——这两类一律交给人，不自动重试。
        """
        return self.side_effect() in (
            SideEffectClass.READ_ONLY,
            SideEffectClass.IDEMPOTENT_WRITE,
        )

    @property
    def is_completion_request(self) -> bool:
        """这是不是一个「申请完成」的动作（DONE / DONE_REQUEST）。"""
        return self.type in COMPLETION_ACTION_TYPES

    @property
    def is_mutating(self) -> bool:
        """这个动作**理应**改变页面/控件状态吗。"""
        return self.type in MUTATING_ACTION_TYPES

    # ---- 指纹 ----

    @property
    def fingerprint(self) -> str:
        """**动作身份**指纹（V2 §十七；V2.7 P2-3 明确语义）。

        它回答「**做的是不是同一个动作**」——由「动作类型 + 目标（坐标量化）+ 取值」共同
        决定，**不包含**「在哪一屏、哪一次尝试」。因此：

        - 同一个 `fingerprint` 在**不同页面**上可能是完全不同的副作用（坐标 (500,800)
          在微信是「发送」、在设置是「清除数据」）。所以它**只在本任务的运行态里比较**
          （`denied_fingerprints`、`recent_actions` 都挂在按 task_id 隔离的 `RuntimeState`），
          绝不跨任务、跨页面当作全局身份。
        - 「哪一次尝试」是另一个维度，由 `current_attempt_id`（`{task_id}:a{n}`）承担；
          同一步骤每次重试都不同。需要「动作 + 尝试」双重绑定（如人工放行凭据）时，
          两者要**分开存**，不能拿 `fingerprint` 一个字段冒充两种语义。

        坐标按 16px 网格向下取整：模型每步给出 (500,1200) 与 (503,1198) 这种抖动时，
        指纹保持一致，不会让死循环伪装成「每步都在做新动作」。
        """
        target = self.target
        if isinstance(target, Point):
            bucket_x = int(target.x) // _FINGERPRINT_QUANTUM
            bucket_y = int(target.y) // _FINGERPRINT_QUANTUM
            target_key = f"{bucket_x},{bucket_y}"
        else:
            target_key = str(target or "")

        raw = f"{self.type.value}|{target_key}|{self.value or ''}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def page_bound_fingerprint(self, package: str = "", activity: str = "") -> str:
        """**页面绑定**指纹：动作身份 + 它被执行时所在的那一屏（V3.1 P2-9）。

        为什么安全授权不能用裸 `fingerprint`：`tap((500, 800))` 在微信、淘宝、
        设置里是三个完全不同的动作，裸指纹把它们算成同一个。用它做放行凭据，
        「批准了微信里的发送」在页面上恰好有同坐标按钮时就可能放行别的东西。

        为什么是**独立方法**而不是把页面塞进 `fingerprint`：`fingerprint` 的语义是
        「做的是不是同一个动作」，页面维度会让 `denied_fingerprints`（用户否决过的
        动作黑名单）跨页失效——同一屏上的同一个按钮被否决后，换个页面再出现就被
        当成新动作重问一遍，那正是 [65] 要避免的骚扰。两个用途需要两种粒度，
        所以分成两个函数，而不是拿一个字段冒充两种语义。

        `package` / `activity` 都为空时退化为裸指纹（拿不到页面信息时不能凭空造证据）。
        """
        if not package and not activity:
            return self.fingerprint
        basis = f"{self.fingerprint}|{package.lower()}|{activity.lower()}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]

    def is_same_as(self, other: "Action", *, tolerance: float = 24.0) -> bool:
        """语义上是不是同一个动作：类型与取值一致，且落点足够接近。

        死循环检测用这个而不是比指纹字符串——指纹把坐标量化到固定网格，
        坐标恰好跨网格边界时（例如 1198 与 1200）会被判成两个动作，漏掉循环。
        这里直接比距离，没有边界问题。
        """
        if self.type is not other.type:
            return False
        if (self.value or "") != (other.value or ""):
            return False

        this_target, other_target = self.target, other.target
        if isinstance(this_target, Point) and isinstance(other_target, Point):
            return (
                abs(this_target.x - other_target.x) <= tolerance
                and abs(this_target.y - other_target.y) <= tolerance
            )
        return str(this_target or "") == str(other_target or "")


class Decision(BaseModel):
    """规划器的一次决策。

    除了动作本身，还带上「做完这个动作后，当前聚焦的步骤是否算完成」——
    这是步骤状态得以逐级推进、暂停后能从正确位置恢复的依据。
    """

    action: Action
    step_done: bool = False
    thought: str = ""

    def describe(self) -> str:
        return f"{self.action.type.value}({self.action.target}) step_done={self.step_done}"
