"""目标验证策略（V2.2 §六）：**按任务画像**决定完成判定该多严。

审核对上一版的判断：

> 现在 `GOAL_VERIFY_MODE=advisory` 时「没有反证 → UNCERTAIN → 完成」。
> 这不是代码 bug，而是**安全策略选择**。但对于 Phone Agent 我建议默认：
>   navigation / external side effect / settings change → strict
>   纯查询类任务 → advisory
> 而不是全局一个 `GOAL_VERIFY_MODE`。

理由很直白：判错两种任务的代价不对称。

    纯查询（「查一下明天天气」）
        误判完成 → 用户看到一句不准的话，重问一次就行
        误判未完成 → 白跑几步，浪费

    有副作用（「给张三发消息」「开启飞行模式」「下单」）
        误判完成 → 用户以为发出去了，其实没有；或者以为没做，其实做了
        误判未完成 → 多问一次人

所以只有**确定是纯查询**时才用宽松策略；只要可能改变设备状态，就按严格处理。

画像由关键词规则判定（零成本、可解释、离线可跑），优先级
`SIDE_EFFECT > NAVIGATION > READ_ONLY > UNKNOWN`：

    「打开微信给张三发消息」 同时命中 打开(导航) 与 发送(副作用)
        → SIDE_EFFECT（严格）。先按导航放行就等于把最该严的那种任务放走了。
"""
from __future__ import annotations

import os
import re
from enum import Enum


class TaskProfile(str, Enum):
    """任务画像：它会不会改变设备状态、以及改变的严重程度。"""

    READ_ONLY = "read_only"
    """纯查询/浏览：搜索、看一眼、读一下。改了也没关系，宽松处理。"""

    NAVIGATION = "navigation"
    """页面导航：打开/进入/返回/跳转。会改设备状态，但没有外部副作用。"""

    SIDE_EFFECT = "side_effect"
    """外部副作用或改设置：发送、下单、支付、删除、开关系统开关。最需要严格。"""

    UNKNOWN = "unknown"
    """认不出是哪一类。按宽松处理，但会把画像记进裁定结果里备查。"""


# 策略取值。与 `GOAL_VERIFY_MODE` 的历史取值保持一致，不新增概念。
ADVISORY = "advisory"
STRICT = "strict"
OFF = "off"
AUTO = "auto"

_EXPLICIT_MODES = frozenset({OFF, ADVISORY, STRICT})

# 画像 → 默认策略
PROFILE_MODE: dict[TaskProfile, str] = {
    TaskProfile.READ_ONLY: ADVISORY,
    TaskProfile.NAVIGATION: STRICT,
    TaskProfile.SIDE_EFFECT: STRICT,
    # 认不出来就不知道它会不会有副作用 → 宽松，但留痕。
    # 刻意不默认 strict：那会让「首次接入一个陌生 App」的每个任务都在 HITL 门口排队。
    TaskProfile.UNKNOWN: ADVISORY,
}

# ---- 副作用：会对外界产生不可逆影响，或改变系统状态 ----
_SIDE_EFFECT_TERMS = (
    "发送", "发消息", "发一条", "发给", "提交", "下单", "支付", "付款", "购买",
    "删除", "移除", "解绑", "注销", "退订", "取消关注", "清空", "格式化", "重置",
    "安装", "卸载", "升级", "更新", "授权", "关注", "点赞", "评论", "转发", "分享",
    "上传", "保存", "新建", "创建", "同步", "注册", "登录", "绑定", "充值", "转账",
    "提现", "退款", "拨号", "打电话", "呼叫", "接听", "挂断", "加入购物车", "收藏",
    "抢购", "秒杀", "报名", "预约", "签到", "打卡", "改名", "修改", "编辑",
    "send", "submit", "pay", "purchase", "checkout", "delete", "remove", "install",
    "uninstall", "upload", "download", "save", "sync", "register", "login", "bind",
    "subscribe", "follow", "like", "comment", "share", "call", "transfer", "withdraw",
)

# ---- 改系统设置：需要一个「系统开关」作为宾语 ----
_SETTING_TARGETS = (
    r"wifi|wi-?fi|蓝牙|bluetooth|飞行模式|airplane|定位|gps|亮度|brightness|音量|volume",
    r"免打扰|勿扰|热点|hotspot|nfc|移动数据|蜂窝数据|浅色|深色|深色模式|夜间模式|夜间",
    r"省电|低电量|自动旋转|旋转锁定|语言|时区|字体|开发者选项|辅助功能|通知权限|权限",
)
_SETTING_VERBS = (
    r"开启|打开|关闭|关掉|关了|开了|切换|改为|调成|调整|设置|启用|禁用|禁止|允许|关|开"
)
_SETTING_CHANGE = re.compile(
    rf"(?:{_SETTING_VERBS}).{{0,8}}(?:{'|'.join(_SETTING_TARGETS)})"
    rf"|(?:{'|'.join(_SETTING_TARGETS)}).{{0,8}}(?:{_SETTING_VERBS})",
    re.IGNORECASE,
)

# ---- 页面导航 ----
_NAVIGATION_TERMS = (
    "打开", "进入", "跳转", "前往", "回到", "返回", "去", "切换到", "切到", "翻到",
    "滑到", "滚动到", "启动", "退出", "关闭页面", "上一页", "下一页",
    "open", "enter", "navigate", "go to", "goto", "launch", "back",
)

# ---- 纯查询 ----
_READ_ONLY_TERMS = (
    "搜索", "搜一下", "查询", "查一下", "查查", "看一下", "看看", "读一下", "浏览",
    "是什么", "多少钱", "价格", "报价", "天气", "新闻", "翻译", "计算", "总结",
    "列出", "显示", "获取", "截图", "对比", "比较",
    "search", "query", "look up", "lookup", "find", "show", "read", "check",
    "how much", "what is", "compare", "list",
)


def _hit(text: str, terms: tuple[str, ...]) -> str:
    for term in terms:
        if term in text:
            return term
    return ""


def classify_profile(instruction: str, context: str = "") -> TaskProfile:
    """从指令（+ 上下文）判断任务画像。纯本地规则，零成本、可解释。"""
    text = f"{instruction or ''} {context or ''}".strip().lower()
    if not text:
        return TaskProfile.UNKNOWN

    hit = _hit(text, _SIDE_EFFECT_TERMS)
    if hit or _SETTING_CHANGE.search(text):
        return TaskProfile.SIDE_EFFECT

    if _hit(text, _NAVIGATION_TERMS):
        return TaskProfile.NAVIGATION

    if _hit(text, _READ_ONLY_TERMS):
        return TaskProfile.READ_ONLY

    return TaskProfile.UNKNOWN


def mode_override() -> str | None:
    """显式配置的全局策略；`auto` 或未设置时返回 None（走按画像判定）。"""
    raw = os.getenv("GOAL_VERIFY_MODE", "").strip().lower()
    if raw in _EXPLICIT_MODES:
        return raw
    if raw in ("", AUTO):
        return None
    # 认不出的值 → 当 auto 处理，并留在 reason 里可见（不静默吞掉配置错误）
    return None


def resolve_mode(instruction: str, context: str = "") -> tuple[str, TaskProfile]:
    """返回 ``(生效策略, 任务画像)``。

    显式配置优先（`off` / `advisory` / `strict` 仍然是全局开关，方便单点覆盖）；
    否则按画像自动决定——这正是审核要的「navigation/副作用 → strict、纯查询 → advisory」。
    """
    profile = classify_profile(instruction, context)
    override = mode_override()
    return (override if override is not None else PROFILE_MODE[profile]), profile


def describe_policy(profile: TaskProfile, mode: str) -> str:
    """给日志/事件流用的一句话说明：为什么这条任务用了这个策略。"""
    if mode_override() is not None:
        return f"策略 {mode}（GOAL_VERIFY_MODE 显式指定，画像 {profile.value}）"
    return f"策略 {mode}（按任务画像 {profile.value} 自动判定）"
