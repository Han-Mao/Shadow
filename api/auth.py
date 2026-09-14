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

    def may_use_device(self, serial: str | None) -> bool:
        """设备级权限检查。未指定 serial 时不做限制（由调度器去挑设备）。"""
        if not self.devices or not serial:
            return True
        return serial in self.devices

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


def confirmation_secret() -> bytes:
    """签名密钥：优先用配置的令牌派生，否则用进程级随机串。"""
    configured = (os.getenv("SHADOW_CONFIRM_SECRET") or "").strip() or (
        os.getenv("SHADOW_API_TOKEN") or ""
    ).strip()
    material = configured or _PROCESS_SECRET
    return hashlib.sha256(f"shadow-confirm:{material}".encode()).digest()


def confirmation_payload(task_id: str, action_fingerprint: str, task_version: int) -> str:
    """确认令牌绑定的内容。

    绑定 task_id **和那个具体动作**：同一个任务换了另一个危险动作，
    旧令牌就失效——否则「确认过一次」等于把这个任务的所有危险动作都放行了。
    """
    return f"{task_id}|{action_fingerprint}|{task_version}"


def issue_confirmation_token(task_id: str, action_fingerprint: str, task_version: int) -> str:
    """为一次待确认的危险动作签发令牌。"""
    payload = confirmation_payload(task_id, action_fingerprint, task_version)
    return hmac.new(confirmation_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def verify_confirmation_token(
    token: str | None, task_id: str, action_fingerprint: str, task_version: int
) -> bool:
    if not token:
        return False
    expected = issue_confirmation_token(task_id, action_fingerprint, task_version)
    return hmac.compare_digest(expected, str(token).strip())
