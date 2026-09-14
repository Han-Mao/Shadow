"""API 访问控制与人工确认令牌（V2.2 §九）。

审核的结论：

> API 提供的东西非常敏感（`/actions`、`/tasks`、`confirm`、`inject` 都能直接
> 影响真实手机）。现在虽然监听 localhost，但只要改成 0.0.0.0 / Docker /
> 反向代理 / 云端控制端，这个 API 就会立即变成高风险入口。
> 尤其是 `/confirm`：不能仅靠知道 task_id 就确认危险动作。

四层防护，按「默认不破坏本地开发」的原则实现：

1. **令牌鉴权**（`SHADOW_API_TOKEN`）。未配置 = 关闭（本地开发零配置），
   配置后所有端点都要带 `Authorization: Bearer <token>` 或 `X-API-Token`。
2. **只读令牌**（`SHADOW_API_READONLY_TOKEN`）：只允许 GET，拿它去点手机一律 403。
3. **设备级权限**（`SHADOW_API_DEVICE_ALLOW`）：限制这台令牌只能操作哪些 serial。
4. **人工确认令牌**：`/confirm` 除了要登录，还要带上本次待确认动作的签名令牌——
   知道 task_id 不等于有权放行危险动作。

另有一条**硬约束**：绑定到非回环地址却没有任何令牌时，服务直接拒绝启动
（见 `api/server.py` 的 `__main__`）。这是防止「图省事改个 HOST 就裸奔上线」。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 进程级随机密钥：没配 API Token 时用它签名确认令牌，
# 重启即失效——本地场景这比「用固定弱密钥」安全得多
_PROCESS_SECRET = secrets.token_hex(32)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class Principal:
    """一个通过鉴权的调用方。"""

    name: str
    read_only: bool = False
    devices: frozenset[str] = field(default_factory=frozenset)
    """允许操作的设备 serial；空集合表示不限。"""

    def allowed_serials(self) -> frozenset[str] | None:
        """该令牌可用的设备集合；None 表示不限。

        传给调度器用（V2.2 §二）：**未指定设备时只能从这里挑**。
        以前 API 只检查「指定的 serial 在不在列表里」，于是不指定就等于绕过——
        调度器会自动挑一台最闲的，很可能就是别人的设备。
        """
        return None if not self.devices else frozenset(self.devices)

    def may_use_device(self, serial: str | None) -> bool:
        """能不能操作这台设备。

        注意 `serial=None` 在**受限令牌**下返回 False：`None` 不等于「哪台都行」，
        而是「还不知道会是哪台」。真正的「哪台都行」判断要交给调度器在
        允许列表内挑（见 `allowed_serials`）。
        """
        if not self.devices:
            return True
        return bool(serial) and serial in self.devices

    def may_access_task(self, task) -> bool:
        """能不能读/控制这条任务的**具体对象**（V2.2 §四）。

        审核原话：现在的授权是「谁能发起操作」，而不是「谁能读取/控制哪个 Task」。
        读取侧同样要过对象级检查——`GET /tasks/{id}` 还会带出待确认动作的令牌，
        读到了就等于拿到了放行危险动作的凭据。

        未绑定设备的任务在受限令牌下一律拒绝：它迟早会被派到某台设备上，
        而「派到哪台」此刻并不确定，不能默认它落在自己名下。
        """
        if not self.devices:
            return True
        return self.may_use_device(getattr(task, "device_serial", None))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "read_only": self.read_only,
            "devices": sorted(self.devices),
        }


ANONYMOUS = Principal(name="anonymous")


def _split_devices(raw: str) -> frozenset[str]:
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def configured_tokens() -> dict[str, Principal]:
    """从环境变量读出「令牌 → 身份」表。每次调用都重读，方便测试与热改配置。"""
    table: dict[str, Principal] = {}

    main = (os.getenv("SHADOW_API_TOKEN") or "").strip()
    if main:
        table[main] = Principal(
            name=os.getenv("SHADOW_API_TOKEN_NAME", "operator"),
            devices=_split_devices(os.getenv("SHADOW_API_DEVICE_ALLOW", "")),
        )

    readonly = (os.getenv("SHADOW_API_READONLY_TOKEN") or "").strip()
    if readonly:
        table[readonly] = Principal(
            name="readonly",
            read_only=True,
            devices=_split_devices(os.getenv("SHADOW_API_DEVICE_ALLOW", "")),
        )

    return table


def enabled() -> bool:
    """是否启用了鉴权。未配置任何令牌 = 关闭（本地开发零配置）。"""
    return bool(configured_tokens())


def require_auth() -> bool:
    """显式要求鉴权（即使没配令牌也拒绝一切请求）。用于「我知道我要上公网」。"""
    return os.getenv("SHADOW_REQUIRE_AUTH", "").strip().lower() in {"1", "true", "yes", "on"}


def extract_token(authorization: str | None, api_token: str | None) -> str:
    """从 `Authorization: Bearer xxx` 或 `X-API-Token` 里取出令牌。"""
    if api_token and api_token.strip():
        return api_token.strip()
    header = (authorization or "").strip()
    if not header:
        return ""
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    # 容错：直接塞裸令牌也接受（有些客户端不方便加 scheme）
    return header


def authenticate(authorization: str | None, api_token: str | None) -> Principal | None:
    """校验令牌并返回身份；不通过返回 None。"""
    provided = extract_token(authorization, api_token)
    if not provided:
        return None
    for token, principal in configured_tokens().items():
        # 定长比较，避免用长度/前缀做时序侧信道（这里威胁不大，但没成本）
        if hmac.compare_digest(token, provided):
            return principal
    return None


def is_public_path(path: str) -> bool:
    """不需要鉴权的端点。只有健康检查——运维探活不该还要带密钥。"""
    return path in {"/health", "/healthz"}


def loopback_only(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


def bare_bind_refused(host: str) -> str | None:
    """非回环绑定且无令牌时返回拒绝理由，否则 None。

    这是最后一道兜底：与其让一个能操作真实手机的 API 裸奔在局域网里，
    不如直接不起来，并告诉使用者怎么配。
    """
    if loopback_only(host) or enabled():
        return None
    return (
        f"拒绝启动：HOST={host} 不是回环地址，但未配置 SHADOW_API_TOKEN。\n"
        "这个 API 能直接操作真实手机（点付款、发消息、装应用），"
        "暴露到局域网等于把这些能力交给所有人。\n"
        "请二选一：\n"
        "  1) 设置 SHADOW_API_TOKEN=<一个足够长的随机串>（可选 "
        "SHADOW_API_READONLY_TOKEN / SHADOW_API_DEVICE_ALLOW 做细粒度控制）\n"
        "  2) 保持 HOST=127.0.0.1，只在本机使用"
    )


# ---------------------------------------------------------------- 人工确认令牌

# 确认令牌的有效期（秒）。审核指出：没有过期时间的话它不是「一次性审批票据」，
# 而是「能力票据」——拿到就一直有效。给一个短时限，够人看清内容再点就行。
CONFIRM_TTL_SECONDS = float(os.getenv("SHADOW_CONFIRM_TTL_SECONDS", "300"))

# 已消费的确认令牌（V2.4 §九）：jti -> 过期时间戳。
# 审核指出光有 TTL + 签名仍然只是「signed capability」：连续 GET 三次会拿到三个
# 有效令牌，任意一个在 TTL 内都能确认。加上 jti 并在消费后记名，同一个令牌第二次
# 提交直接被拒——确认是**一次性审批**，不是可以反复使用的凭据。
_consumed_confirmations: dict[str, int] = {}
_consumption_lock = threading.Lock()


def confirmation_secret() -> bytes:
    """签名密钥：优先用配置的令牌派生，否则用进程级随机串。"""
    configured = (os.getenv("SHADOW_CONFIRM_SECRET") or "").strip() or (
        os.getenv("SHADOW_API_TOKEN") or ""
    ).strip()
    material = configured or _PROCESS_SECRET
    return hashlib.sha256(f"shadow-confirm:{material}".encode()).digest()


def confirmation_payload(
    principal: str,
    task_id: str,
    action_fingerprint: str,
    task_version: int,
    expires_at: int,
    jti: str,
) -> str:
    """确认令牌绑定的内容。

    绑的五样东西各有理由（V2.2 §五 / V2.4 §九）：
    - `task_id` + 动作指纹：同一个任务换了另一个危险动作，旧令牌就失效——
      否则「确认过一次」等于把这个任务的所有危险动作都放行了
    - `task_version`：任务目标被改写（SUPER_TASK）后旧令牌即失效
    - `principal`：**审核新指出的缺口**。不绑人的话，令牌是「谁拿到谁能用」——
      A 读一次任务拿到令牌，B 只要有普通写权限就能放行危险动作
    - `expires_at`：短时效，避免变成长期有效的能力票据
    - `jti`：令牌身份，用于一次性消费（见 `consume_confirmation_token`）
    """
    return f"{principal}|{task_id}|{action_fingerprint}|{task_version}|{expires_at}|{jti}"


def issue_confirmation_token(
    principal: str,
    task_id: str,
    action_fingerprint: str,
    task_version: int,
    *,
    ttl: float | None = None,
    now: float | None = None,
) -> str:
    """为一次待确认的危险动作签发令牌，格式 `<过期时间戳>.<jti>.<签名>`。"""
    issued = time.time() if now is None else now
    expires_at = int(issued + (CONFIRM_TTL_SECONDS if ttl is None else ttl))
    jti = secrets.token_hex(8)
    payload = confirmation_payload(
        principal, task_id, action_fingerprint, task_version, expires_at, jti
    )
    signature = hmac.new(confirmation_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{expires_at}.{jti}.{signature}"


def _verify_and_parse(
    token: str | None,
    principal: str,
    task_id: str,
    action_fingerprint: str,
    task_version: int,
    *,
    now: float | None = None,
) -> tuple[bool, str, str]:
    """校验令牌，返回 (是否通过, 不通过的原因, jti)。

    返回原因是为了进审计日志——「令牌为什么被拒」是排查权限问题的第一手信息。
    """
    if not token:
        return False, "缺少确认令牌", ""

    raw = str(token).strip()
    expires_text, _, rest = raw.partition(".")
    jti, _, signature = rest.partition(".")
    if not expires_text or not jti or not signature:
        return False, "确认令牌格式非法", ""
    try:
        expires_at = int(expires_text)
    except ValueError:
        return False, "确认令牌格式非法", ""

    current = time.time() if now is None else now
    if current > expires_at:
        return False, f"确认令牌已过期（{expires_at}）", ""

    payload = confirmation_payload(
        principal, task_id, action_fingerprint, task_version, expires_at, jti
    )
    expected = hmac.new(confirmation_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, signature):
        # 指纹/版本/身份对不上都会走到这里，只回一句笼统原因，不泄露是哪一项不匹配
        return False, "确认令牌与当前待确认事项或调用方身份不匹配", ""
    return True, "", jti


def verify_confirmation_token(
    token: str | None,
    principal: str,
    task_id: str,
    action_fingerprint: str,
    task_version: int,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """只校验、不消费（幂等，适合做预检）。

    真正放行动作请用 `consume_confirmation_token`。
    """
    ok, reason, _ = _verify_and_parse(
        token, principal, task_id, action_fingerprint, task_version, now=now
    )
    return ok, reason


def consume_confirmation_token(
    token: str | None,
    principal: str,
    task_id: str,
    action_fingerprint: str,
    task_version: int,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """校验并**消费**令牌：同一个令牌只能用一次（V2.4 §九）。

    与 `verify_confirmation_token` 的差别只在「记名」这一步——校验通过后把 jti
    记进已消费集合，重复提交会被明确拒绝，而不是当成一次全新的审批。

    进程重启会丢掉这份记录，所以它只是 TTL 之外的第二道闸；真正的语义保障仍然
    来自「待确认事项被消费后 `/confirm` 直接 409」，这里补的是「同一张票据不许
    用第二次」这条显式语义。
    """
    ok, reason, jti = _verify_and_parse(
        token, principal, task_id, action_fingerprint, task_version, now=now
    )
    if not ok:
        return False, reason

    current = time.time() if now is None else now
    try:
        expires_at = int(str(token).strip().partition(".")[0])
    except ValueError:  # pragma: no cover - _verify_and_parse 已挡住
        expires_at = int(current)
    with _consumption_lock:
        # 顺手清掉过期条目，避免这个集合无限增长
        for used_jti in [k for k, exp in _consumed_confirmations.items() if exp < current]:
            _consumed_confirmations.pop(used_jti, None)
        if jti in _consumed_confirmations:
            return False, "确认令牌已被使用过（确认是一次性审批，请重新读取待确认事项）"
        _consumed_confirmations[jti] = expires_at
    return True, ""


def clear_consumed_confirmations() -> None:
    """清空已消费记录（测试用）。"""
    with _consumption_lock:
        _consumed_confirmations.clear()
