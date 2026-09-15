"""动作语义层（V3 M2）：把「风险」与「副作用幂等」统一到同一个 `SemanticRole` 上。

v2.9 指出的核心问题：风险（`DANGEROUS_KEYWORDS`）与幂等
（`IRREVERSIBLE_KEYWORDS` / `NON_IDEMPOTENT_KEYWORDS`）是**两套独立的关键词表**，
各自 grep 各自，于是出现自相矛盾：

    「点赞」→ side_effect 判 NON_IDEMPOTENT_WRITE（不能重做），
              但 risk 判 SAFE（不在危险词表里）
    「继续」→ 语义上可能是「继续支付」，但一个危险词都没有 → SAFE

本层把「语义角色」抽成第一等概念：先给动作**定性**（它到底是什么动作），
再从同一个角色**查表**派生 risk 与 side_effect——不再让两个维度各维护一份词表。

    SemanticRole（语义角色）
        ↓ 查 ROLE_SEMANTICS 表
    (risk, side_effect, idempotent)

关键词表**保留**，但降级为「role 推断的原始材料」，不再是判定结果本身。
这样新增一个词 / 换一种说法，只需改 role 推断，risk 与幂等自动跟着一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .action import ActionRisk, SideEffectClass


class SemanticRole(str, Enum):
    """动作的语义角色：这个动作**到底在做什么**。"""

    READ = "read"
    """只读/浏览：搜索、查看、读取。无副作用。"""

    NAVIGATE = "navigate"
    """页面导航：打开、进入、跳转、返回、切 Tab、划动。会改页面，可重做等价。"""

    LIKE = "like"
    """轻量非幂等：点赞、关注、收藏、分享、评论、转发、投币。重做产生第二条。"""

    SUBMIT = "submit"
    """提交/发送：发消息、提交表单、报名、预约、发布、签到。重做产生第二条。"""

    PURCHASE = "purchase"
    """交易：支付、付款、下单、购买、结算、转账、汇款、充值、退款、提现。不可逆。"""

    DELETE = "delete"
    """删除/解绑：删除、移除、解绑、注销、退订、清空、格式化。不可逆。"""

    AUTHORIZE = "authorize"
    """授权/开通：同意、授权、开通、订购、订阅、免密、获取验证码。可能不可逆。"""

    SETTINGS = "settings"
    """改系统设置：开关 Wi-Fi、蓝牙、飞行模式、亮度、音量等。可逆但改状态。"""

    UNKNOWN = "unknown"
    """认不出是什么动作。按动作类型兜底。"""


@dataclass(frozen=True)
class ActionSemantic:
    """一个动作的语义结论：风险与幂等**都从这里派生**，保证两个维度一致。"""

    role: SemanticRole
    risk: ActionRisk
    side_effect: SideEffectClass

    @property
    def idempotent(self) -> bool:
        """是否可安全重做（EFFECT_UNKNOWN 之后能不能自动再来一次）。"""
        return self.side_effect in (
            SideEffectClass.READ_ONLY,
            SideEffectClass.IDEMPOTENT_WRITE,
        )


# ---- 角色 → 语义 映射表（M2 的灵魂：一处钉死，两处消费）----
#
# 这是唯一决定「某类动作是什么风险、什么幂等」的地方。risk 与 side_effect
# 不再各自维护词表，都从这里查。加一类动作 = 加一行，而不是在两个文件里各加一个词。

ROLE_SEMANTICS: dict[SemanticRole, ActionSemantic] = {
    SemanticRole.READ: ActionSemantic(
        SemanticRole.READ, ActionRisk.SAFE, SideEffectClass.READ_ONLY
    ),
    SemanticRole.NAVIGATE: ActionSemantic(
        SemanticRole.NAVIGATE, ActionRisk.SAFE, SideEffectClass.IDEMPOTENT_WRITE
    ),
    SemanticRole.LIKE: ActionSemantic(
        SemanticRole.LIKE, ActionRisk.CAUTION, SideEffectClass.NON_IDEMPOTENT_WRITE
    ),
    SemanticRole.SUBMIT: ActionSemantic(
        SemanticRole.SUBMIT, ActionRisk.DANGEROUS, SideEffectClass.NON_IDEMPOTENT_WRITE
    ),
    SemanticRole.PURCHASE: ActionSemantic(
        SemanticRole.PURCHASE, ActionRisk.DANGEROUS, SideEffectClass.IRREVERSIBLE
    ),
    SemanticRole.DELETE: ActionSemantic(
        SemanticRole.DELETE, ActionRisk.DANGEROUS, SideEffectClass.IRREVERSIBLE
    ),
    SemanticRole.AUTHORIZE: ActionSemantic(
        SemanticRole.AUTHORIZE, ActionRisk.DANGEROUS, SideEffectClass.NON_IDEMPOTENT_WRITE
    ),
    SemanticRole.SETTINGS: ActionSemantic(
        SemanticRole.SETTINGS, ActionRisk.CAUTION, SideEffectClass.IDEMPOTENT_WRITE
    ),
    SemanticRole.UNKNOWN: ActionSemantic(
        SemanticRole.UNKNOWN, ActionRisk.SAFE, SideEffectClass.IDEMPOTENT_WRITE
    ),
}


def semantic_for(role: SemanticRole) -> ActionSemantic:
    """按角色查语义结论。未知角色退化为 UNKNOWN（按动作类型再兜底）。"""
    return ROLE_SEMANTICS.get(role, ROLE_SEMANTICS[SemanticRole.UNKNOWN])


# ---- 角色 → 关键词（role 推断的原始材料）----
#
# 这些词表**只用于推断 role**，不再直接决定 risk / side_effect。
# 与旧 `DANGEROUS_KEYWORDS` / `IRREVERSIBLE_KEYWORDS` / `NON_IDEMPOTENT_KEYWORDS`
# 的区别：旧表是「三套各管一个维度」，这里是一套「按角色归类」。
# 判定优先级从最危险到最不危险（拿不准往重里判），命中即停。

ROLE_KEYWORDS: dict[SemanticRole, tuple[str, ...]] = {
    SemanticRole.PURCHASE: (
        "支付", "付款", "下单", "购买", "结算", "转账", "汇款", "提现", "充值", "退款",
        "扣款", "免密",
        "pay", "purchase", "checkout", "check out", "transfer", "withdraw", "top up",
        "recharge",
    ),
    SemanticRole.DELETE: (
        "删除", "移除", "解绑", "注销", "解约", "退订", "清空", "格式化",
        "delete", "remove", "unbind", "reset",
    ),
    SemanticRole.AUTHORIZE: (
        "同意", "授权", "允许访问", "获取验证码", "开通", "订购", "续费", "订阅",
        "authorize", "agree", "accept", "subscribe",
    ),
    SemanticRole.SUBMIT: (
        "发送", "提交", "确认", "报名", "预约", "发布", "签到", "打卡", "邀请", "添加好友",
        "send", "submit", "confirm",
    ),
    SemanticRole.LIKE: (
        "点赞", "关注", "收藏", "分享", "转发", "评论", "投币",
        "like", "follow", "share", "comment",
    ),
    SemanticRole.SETTINGS: (
        "wifi", "wi-fi", "蓝牙", "bluetooth", "飞行模式", "airplane", "定位", "gps",
        "亮度", "brightness", "音量", "volume", "热点", "hotspot", "nfc", "移动数据",
        "蜂窝数据", "深色模式", "夜间模式", "省电", "自动旋转", "旋转锁定",
    ),
    SemanticRole.NAVIGATE: (
        "打开", "进入", "跳转", "前往", "返回", "回到", "去", "切换到", "切到", "翻到",
        "滑到", "滚动到", "启动", "退出", "上一页", "下一页", "下一步", "继续",
        "open", "enter", "navigate", "go to", "goto", "launch", "back", "next", "continue",
    ),
    SemanticRole.READ: (
        "搜索", "查询", "查一下", "看一下", "看看", "浏览", "读取", "查看", "对比",
        "比较", "显示", "列出",
        "search", "query", "look up", "lookup", "find", "show", "read", "check",
        "compare", "list",
    ),
}

# 判定顺序：从最危险到最不危险。命中即返回，保证「确认付款」先命中 PURCHASE
# 而不是被「确认」→ SUBMIT 抢先。
_ROLE_ORDER: tuple[SemanticRole, ...] = (
    SemanticRole.PURCHASE,
    SemanticRole.DELETE,
    SemanticRole.AUTHORIZE,
    SemanticRole.SUBMIT,
    SemanticRole.LIKE,
    SemanticRole.SETTINGS,
    SemanticRole.NAVIGATE,
    SemanticRole.READ,
)


def infer_role(text: str) -> SemanticRole:
    """从文本推断动作的语义角色。纯本地、零成本、可解释。

    文本应是小写（调用方负责 lowercase），这里是「角色 → 关键词」的命中判定。
    认不出返回 UNKNOWN，由调用方按动作类型兜底。
    """
    if not text:
        return SemanticRole.UNKNOWN
    for role in _ROLE_ORDER:
        if any(keyword in text for keyword in ROLE_KEYWORDS[role]):
            return role
    return SemanticRole.UNKNOWN


def infer_semantic(text: str) -> ActionSemantic:
    """一步到位：文本 → 语义结论（role + risk + side_effect）。"""
    return semantic_for(infer_role(text))
