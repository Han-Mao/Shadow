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
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field

# 预占 TTL 的**唯一来源**是生产实现（SQLite 那个），这里 import 的是同一个常量：
# 内存守卫只在没有存储层的场景（纯函数单测、嵌入式）用，但它的过期策略必须与生产
# 一致——否则「测试通过、生产却是另一套语义」是最难发现的那类偏差。
# （方向是 api → storage 的常量导入，不是反向：storage 不该知道 auth 的存在。）
from storage.confirmation_store import RESERVE_TTL_SECONDS

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


# 配置错误（V3.2 §六）。解析不出 principals 时**绝不能**退回「没配鉴权」——
# 那会把「配错了」变成「谁都能进」，是最坏的方向。所以记下原因、让鉴权处于
# 「开着但一个人都认不出」的状态（所有请求 401），并把原因暴露到 /health。
_config_error: str | None = None


def _parse_principals(raw: str) -> tuple[dict[str, Principal], str | None]:
    """解析 `SHADOW_API_PRINCIPALS`（V3.2 §六）。返回 (表, 问题描述)。

    格式（每个 principal 有**自己的设备范围**，这正是审核要的 Principal → Device ACL）：

        {"alice":   {"token": "t-alice", "devices": ["phone-001"]},
         "bob":     {"token": "t-bob",   "devices": ["phone-002"]},
         "auditor": {"token": "t-aud",   "read_only": true, "devices": ["*"]}}

    `devices` 省略 / 空数组 / `["*"]` 都表示不限。
    """
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"SHADOW_API_PRINCIPALS 不是合法 JSON：{exc}"
    if not isinstance(spec, dict):
        return {}, "SHADOW_API_PRINCIPALS 必须是一个对象（principal 名 → 配置）"

    table: dict[str, Principal] = {}
    problems: list[str] = []
    for name, item in spec.items():
        if not isinstance(item, dict):
            problems.append(f"{name}: 配置必须是对象")
            continue
        token = str(item.get("token") or "").strip()
        if not token:
            problems.append(f"{name}: 缺少 token")
            continue
        devices = item.get("devices")
        if devices is None:
            device_set = frozenset()
        elif isinstance(devices, (list, tuple)):
            device_set = frozenset(
                text for text in (str(d).strip() for d in devices) if text and text != "*"
            )
        elif isinstance(devices, str):
            device_set = _split_devices(devices.replace("*", ""))
        else:
            problems.append(f"{name}: devices 必须是数组或逗号分隔字符串")
            continue
        if token in table:
            problems.append(f"{name}: 令牌与 {table[token].name} 重复")
            continue
        table[token] = Principal(
            name=str(name),
            read_only=bool(item.get("read_only", False)),
            devices=device_set,
        )
    return table, ("；".join(problems) if problems else None)


def configured_tokens() -> dict[str, Principal]:
    """从环境变量读出「令牌 → 身份」表。每次调用都重读，方便测试与热改配置。

    两种方式（同时配置时以 principals 为准，legacy 被忽略并告警）：

    - **`SHADOW_API_PRINCIPALS`（V3.2 §六，推荐）**：每个 principal 有**自己的设备范围**。
    - **legacy**：`SHADOW_API_TOKEN` / `SHADOW_API_READONLY_TOKEN` +
      `SHADOW_API_DEVICE_ALLOW`。这一对只能表达「所有令牌共用一份设备白名单」——
      也就是审核说的「Token + Global Device ACL」。保底兼容，新部署请用上面那种。
    """
    global _config_error

    raw = (os.getenv("SHADOW_API_PRINCIPALS") or "").strip()
    if raw:
        table, problem = _parse_principals(raw)
        _config_error = problem
        if problem:
            # 严格失败：宁可整表作废（全部 401），也不要「少配了一个 principal，
            # 于是它变成未受限的匿名身份」。原因会出现在 /health 与服务日志里。
            logger.error("principals 配置有误，鉴权将拒绝所有请求：%s", problem)
            return {}
        if os.getenv("SHADOW_API_TOKEN") or os.getenv("SHADOW_API_READONLY_TOKEN"):
            logger.warning(
                "同时配置了 SHADOW_API_PRINCIPALS 与 legacy 令牌变量，"
                "以 principals 为准，legacy 已被忽略"
            )
        return table

    _config_error = None
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


def config_error() -> str | None:
    """principals 配置的问题描述（没有问题时为 None）。

    有值 = 配置写错了、鉴权正在拒绝所有请求。它必须**可查**（`/health` 与启动检查），
    否则运维看到的是「所有请求 401」，而原因只在一行日志里。
    """
    return _config_error


def enabled() -> bool:
    """是否启用了鉴权。未配置任何令牌 = 关闭（本地开发零配置）。"""
    return bool(configured_tokens()) or _config_error is not None


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

# ---- 已消费的确认令牌（V2.4 §九 · V3.2 §二）----
#
# V2.4 加的「消费后记名」解决了「连续 GET 拿到多张票据、任意一张都能用」的问题，
# 但记录本身是**进程内存里的 dict**。而确认令牌的签名密钥在配置了
# `SHADOW_API_TOKEN` / `SHADOW_CONFIRM_SECRET` 时是**稳定**的，两者相加：
#
#     09:00 签发令牌 A → 09:01 用它确认 → 09:02 服务重启 → 09:03 令牌 A
#     仍在 TTL 内、签名依然有效 → 它又「可用」了
#
# 所以 V2.4 的实现是「**进程生命周期内**一次性」，不是「一次性」（V3.2 §二）。
# 现在消费记录走可插拔的守卫：生产用 `storage.confirmation_store` 的 SQLite 实现
# （jti 主键 + 原子 INSERT），默认的内存实现只留给单测与「没配存储」的场景。


class InMemoryConsumption:
    """消费记录默认实现：进程内存。

    **它不满足跨重启语义**。保留它是因为 `api.auth` 需要在没有存储层的场景下
    也能工作（纯函数单测、嵌入式使用），而且它把「一次性」这条语义在
    进程内表达完整了。生产部署必须在启动时 `configure_consumption()` 换成
    SQLite 实现——`api/server.py` 已经这么做了。

    V3.3 §六 起它也要实现两阶段（`reserve` / `commit` / `release`），
    与 `storage.confirmation_store.ConfirmationConsumptionStore` 保持同一套接口——
    否则「生产用 SQLite、单测用内存」时，被测试的根本不是生产的那条路径。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._used: dict[str, float] = {}
        # 预占中的票据：jti → 预占时刻（超过 RESERVE_TTL_SECONDS 视为「崩溃残留」）
        self._reserved: dict[str, float] = {}

    def consume(self, jti: str, *, expires_at: float = 0.0, **_evidence) -> bool:
        current = time.time()
        with self._lock:
            self._drop_expired(current)
            if jti in self._used or jti in self._reserved:
                return False
            self._used[jti] = expires_at
            return True

    def reserve(self, jti: str, *, expires_at: float = 0.0, now: float | None = None, **_evidence) -> bool:
        """预占（还没消费）。已被消费 / 已被别人预占 → False。"""
        current = time.time() if now is None else now
        with self._lock:
            self._drop_expired(current)
            if jti in self._used or jti in self._reserved:
                return False
            self._reserved[jti] = current
            return True

    def commit(
        self, jti: str, *, expires_at: float | None = None, now: float | None = None, **_evidence
    ) -> bool:
        """把预占转成已消费。没有预占 → False。"""
        current = time.time() if now is None else now
        with self._lock:
            if jti not in self._reserved:
                return False
            self._reserved.pop(jti, None)
            self._used[jti] = expires_at if expires_at else current + CONFIRM_TTL_SECONDS
            return True

    def release(self, jti: str) -> bool:
        """退回预占（已消费的退不掉）。"""
        with self._lock:
            return self._reserved.pop(jti, None) is not None

    def is_consumed(self, jti: str) -> bool:
        with self._lock:
            return jti in self._used

    def is_reserved(self, jti: str) -> bool:
        with self._lock:
            return jti in self._reserved

    def _drop_expired(self, current: float) -> None:
        """清掉过期的已消费记录与**过期的预占**（后者是崩溃残留）。"""
        for used_jti in [k for k, exp in self._used.items() if exp < current]:
            self._used.pop(used_jti, None)
        for jti in [k for k, at in self._reserved.items() if at + RESERVE_TTL_SECONDS < current]:
            self._reserved.pop(jti, None)

    def clear(self) -> None:
        with self._lock:
            self._used.clear()
            self._reserved.clear()


_consumption: object = InMemoryConsumption()


def configure_consumption(guard) -> None:
    """换掉消费记录的后端（V3.2 §二）。

    接受任何实现下面这套接口的对象——`storage.confirmation_store.
    ConfirmationConsumptionStore` 就是生产用的那个：

        consume(jti, *, expires_at, **evidence) -> bool     # 一步式：校验即作废
        reserve(jti, *, expires_at, now=None, **evidence) -> bool   # 预占（V3.3 §六）
        commit(jti, *, expires_at=None) -> bool             # 预占 → 已消费
        release(jti) -> bool                                # 退回预占
        is_consumed(jti) / clear()

    `reserve` / `commit` / `release` 三个是 V3.3 §六 加的：让「改业务状态」失败时
    票据能被退回，而不是烧掉。实现方必须保证**预占也是原子的**（唯一约束），
    否则两个并发请求会同时占上同一张票据。
    """
    global _consumption
    _consumption = guard


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
    """校验并**消费**令牌：同一个令牌只能用一次（V2.4 §九 · V3.2 §二）。

    与 `verify_confirmation_token` 的差别只在「记名」这一步——校验通过后把 jti
    交给消费守卫，重复提交被明确拒绝，而不是当成一次全新的审批。

    记名的**持久性由守卫决定**（`configure_consumption`）：生产用的是 SQLite，
    所以「同一张票据不许用第二次」跨进程、跨重启都成立。默认的内存守卫只保证
    进程内——那不够，但它是显式可替换的，而不是藏在实现里。
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
    consumed = _consumption.consume(
        jti,
        task_id=task_id,
        principal=principal,
        fingerprint=action_fingerprint,
        expires_at=expires_at,
    )
    if not consumed:
        return False, "确认令牌已被使用过（确认是一次性审批，请重新读取待确认事项）"
    return True, ""


def reserve_confirmation_token(
    token: str | None,
    principal: str,
    task_id: str,
    action_fingerprint: str,
    task_version: int,
    *,
    now: float | None = None,
) -> tuple[bool, str, str]:
    """校验并**预占**令牌（V3.3 §六）。返回 `(是否占上, 原因, jti)`。

    与 `consume_confirmation_token` 的差别在于**作废的时机**：这里只是先把这个
    `jti` 占住，还没有烧掉它；等业务状态真的改成功之后再
    `commit_confirmation_token(jti)`。于是「票据已作废、业务却没做成」那个窗口
    消失了（审核 §六 原话：那虽然不是安全漏洞、是 fail-safe，但用户得重新申请票据）。

    预占与消费一样是**原子**的（`jti` 唯一约束），所以两个并发请求不可能同时占上
    同一张票据；`commit` 之后任何预占都会失败。
    """
    ok, reason, jti = _verify_and_parse(
        token, principal, task_id, action_fingerprint, task_version, now=now
    )
    if not ok:
        return False, reason, ""

    current = time.time() if now is None else now
    try:
        expires_at = int(str(token).strip().partition(".")[0])
    except ValueError:  # pragma: no cover - _verify_and_parse 已挡住
        expires_at = int(current)
    reserved = _consumption.reserve(
        jti,
        task_id=task_id,
        principal=principal,
        fingerprint=action_fingerprint,
        expires_at=expires_at,
        now=current,
    )
    if not reserved:
        return False, "确认令牌已被使用过（确认是一次性审批，请重新读取待确认事项）", ""
    return True, "", jti


def commit_confirmation_token(jti: str, *, expires_at: float | None = None) -> bool:
    """把预占转成**已消费**——`POST /confirm` 在业务状态改成功之后必须调它。

    顺序很要紧：**先改状态、后提交票据**。反过来就是 V3.2 的老行为（票据先作废），
    遇到并发冲突时用户会看到 409 而且票据已经没了。
    """
    if not jti:
        return False
    return bool(_consumption.commit(jti, expires_at=expires_at))


def release_confirmation_token(jti: str) -> bool:
    """退回预占（业务没做成时调用）。**已消费的票据退不掉**——过滤在守卫那一层。"""
    if not jti:
        return False
    return bool(_consumption.release(jti))


def clear_consumed_confirmations() -> None:
    """清空已消费记录（**测试用**；生产上清空等于让用过的票据复活）。"""
    _consumption.clear()


def consumption_backend() -> str:
    """当前消费记录后端的名字，供 /health 与启动日志核对。

    值不是内存实现时就说明「一次性」是跨重启成立的——这个信息值得能被查到，
    而不是靠人记住启动时配了什么。
    """
    return type(_consumption).__name__
