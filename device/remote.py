"""`AndroidBridge` 的**远程传输**实现（V3.3 §1；部署路线 B，见 `android/README.md`）。

为什么需要它：`AndroidBridge` 是一条协议，而协议可以有不止一种承载方式。

    路线 A  同进程（Chaquopy）   Kotlin 对象直接注册 → register_android_bridge(factory)
    路线 B  跨进程/跨机器（本文件）手机上的设备端点 ←HTTP→ Shadow Core

路线 B 存在的理由很具体：**Python 核心原封不动搬进 APK 有一个已知的硬依赖**
（`models/` 全量依赖 pydantic v2，而 pydantic-core 是 Rust 扩展，Chaquopy 官方仓库
目前没有它的 Android 轮子——详见 `android/README.md` 的「路线选择」）。
所以「手机当设备端点、Core 跑在 PC/局域网」不是退而求其次，而是**今天就能跑通的那条路**；
它同时也是方案文档 §10 里「手机 A / B / C → Shadow Cloud」那个形态的第一版。

两条路线共用**同一份 Kotlin 设备层**（`android/`）：因此这份代码不因为路线选择而白写——
路线 A 落地时只需把 Kotlin 对象换成同进程注册，Python 侧一行不用改。

━━━ 协议 ━━━

    POST {base}/bridge/<方法名>
    Header: X-Shadow-Token: <令牌>
    Body:   JSON，键就是桥方法的参数名
    回包:   {"ok": true, "value": <结果>} | {"ok": false, "error": "...", "code": "..."}

`value` 的形状按方法定：`screen_size` → `[w,h]`；`current_focus` → `[package,activity]`；
`dump_ui` / `state` → 字符串；其余 → null。
`code == "service_disabled"` 映射成 `AndroidServiceUnavailable`——这不是「这次没成功」，
而是「用户还没给辅助功能/投屏权限」，上层必须把它当成需要界面上说清楚的事，
而不是当成一次可以重试的设备故障（与 `device/android.py` 的区分保持一致）。

`dump_ui` 的**格式约定不变**：仍然必须是 uiautomator 同构 XML（[92]）。
远程传输只是把同一份字节搬过来，不做任何再加工——所以 `vision/*` 依旧一行不用改。
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any

import httpx

from .android import (
    AndroidBridge,
    AndroidBridgeError,
    AndroidServiceUnavailable,
)

logger = logging.getLogger(__name__)

ENV_BRIDGE_URL = "SHADOW_ANDROID_BRIDGE_URL"
ENV_BRIDGE_TOKEN = "SHADOW_ANDROID_BRIDGE_TOKEN"

# 超时按操作细分（与 [73] 同口径）：读操作 6s、改设备的操作 15s。
# 一次手势/一次启动应用本身就慢，用读超时会把正常操作判成失败。
READ_TIMEOUT_SECONDS = 6.0
WRITE_TIMEOUT_SECONDS = 15.0
CONNECT_TIMEOUT_SECONDS = 3.0

# 服务端明确表示「权限还没给」时的错误码。
CODE_SERVICE_DISABLED = "service_disabled"

# PNG 魔数：用来在没有 content-type 的情况下判断「这一包是图还是 JSON 错误」
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class RemoteBridgeError(AndroidBridgeError):
    """远程设备端点失败（连不上 / 回包不可解析 / 回包说失败）。

    继承 `AndroidBridgeError`（而不是另起一支）：核心只认 `DeviceError` 家族，
    所以「手机本机」和「手机在局域网另一头」在错误处理这条路径上完全同形——
    这正是把 `AndroidBridge` 抽成协议要换来的东西。
    """


class RemoteBridgeUnavailable(RemoteBridgeError, AndroidServiceUnavailable):
    """端点可达但设备侧没就绪（权限没给、服务没起）。

    同时也是 `AndroidServiceUnavailable`：上层按「需要用户在手机上处理」来处置，
    而不是按「网络抖了一下、重试就好」。
    """


def bridge_url_from_env() -> str | None:
    """环境里配了端点地址就返回它，否则 None。"""
    raw = (os.getenv(ENV_BRIDGE_URL) or "").strip()
    return raw.rstrip("/") or None


class RemoteAndroidBridge:
    """把手机上的设备端点适配成 `AndroidBridge`（12 个方法一对一）。

    这一层刻意**不做任何语义加工**：不缓存、不重试、不改写 UI 树。
    加工都在 `AndroidDeviceController` 里做（那是统一的地方，见 `device/android.py`）——
    两个传输实现如果各自加工一遍，就会长出两套语义，然后有一处忘了改。
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url or not base_url.strip():
            raise RemoteBridgeError("设备端点地址为空")
        self._base = base_url.strip().rstrip("/")
        self._token = (token if token is not None else os.getenv(ENV_BRIDGE_TOKEN) or "").strip()
        # trust_env=False：手机上不该被系统/http_proxy 之类环境变量改写目标地址——
        # 一旦被代理接管，症状是「莫名其妙连不上」，而且日志里看不出来。
        self._client = httpx.Client(
            base_url=self._base,
            timeout=httpx.Timeout(
                READ_TIMEOUT_SECONDS,
                connect=CONNECT_TIMEOUT_SECONDS,
                write=WRITE_TIMEOUT_SECONDS,
                pool=CONNECT_TIMEOUT_SECONDS,
            ),
            trust_env=False,
            transport=transport,
        )

    # ---- 基础设施 ----

    @property
    def base_url(self) -> str:
        return self._base

    def close(self) -> None:
        self._client.close()

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"RemoteAndroidBridge({self._base})"

    def _call(self, method: str, payload: dict[str, Any] | None = None, *, timeout: float = READ_TIMEOUT_SECONDS) -> Any:
        """发一次请求并解包。

        失败一律转成 `AndroidBridgeError` 家族——**不把 httpx 的异常泄漏出去**：
        核心侧只写一条 `except DeviceError`，`httpx.ConnectError` 漏上去就会变成
        一段没人认识的堆栈，而不是「设备不可用」这条可判定的事实。
        """
        headers = {"X-Shadow-Token": self._token} if self._token else {}
        try:
            response = self._client.post(
                f"/bridge/{method}", json=payload or {}, headers=headers, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise RemoteBridgeUnavailable(
                f"连不上手机设备端点 {self._base}/bridge/{method}：{exc}。"
                "请确认：① Shadow 应用在手机上已启动并处于「设备端点」运行状态；"
                f"② 地址可达（当前来自 {ENV_BRIDGE_URL}）；③ 手机与 Core 在同一网络。"
            ) from exc

        if response.status_code in (401, 403):
            raise RemoteBridgeError(
                f"设备端点拒绝请求（HTTP {response.status_code}）：令牌不对。"
                f"Core 与手机上的 {ENV_BRIDGE_TOKEN} 必须一致。"
            )
        if response.status_code >= 500:
            raise RemoteBridgeError(f"设备端点内部错误（HTTP {response.status_code}）")

        if method == "screenshot_bytes":
            # 截图走裸字节：base64 会让每步观察多出 33% 的传输量，
            # 而这是整条链路里唯一的高频大包。
            #
            # 但**失败时**服务端回的是 JSON（HTTP 仍是 200，与其它方法一致）——
            # 那种情况下必须走统一的解包，否则「投屏权限没给」会被降级成一句
            # 「截图失败」，用户就不知道要去开权限了（`service_disabled` 的映射会丢掉）。
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("image/") or response.content[:8] == _PNG_MAGIC:
                if not response.content:
                    raise RemoteBridgeError("设备端点返回了空截图（MediaProjection 可能还没就绪）")
                return response.content
            return self._unwrap(method, response)

        return self._unwrap(method, response)

    @staticmethod
    def _unwrap(method: str, response: httpx.Response) -> Any:
        try:
            body = response.json()
        except ValueError as exc:
            # 回包不是 JSON 通常意味着打到了别的东西（登录页、反向代理错误页）。
            # 说清楚这件事，比抛一个 JSONDecodeError 有用得多。
            snippet = response.text[:120].replace("\n", " ")
            raise RemoteBridgeError(
                f"{method} 的回包不是 JSON（HTTP {response.status_code}），"
                f"很可能打到了代理或其它服务：{snippet!r}"
            ) from exc

        if not isinstance(body, dict):
            raise RemoteBridgeError(f"{method} 的回包结构不对：{body!r}")

        if body.get("ok"):
            return body.get("value")

        error = str(body.get("error") or "设备端点报告失败")
        if body.get("code") == CODE_SERVICE_DISABLED:
            raise RemoteBridgeUnavailable(error)
        raise RemoteBridgeError(error)

    # ---- Observe ----

    def screen_size(self) -> tuple[int, int]:
        value = self._call("screen_size")
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise RemoteBridgeError(f"screen_size 回包形状不对：{value!r}（应为 [width, height]）")
        return int(value[0]), int(value[1])

    def current_focus(self) -> tuple[str, str]:
        value = self._call("current_focus")
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise RemoteBridgeError(f"current_focus 回包形状不对：{value!r}（应为 [package, activity]）")
        return str(value[0] or ""), str(value[1] or "")

    def dump_ui(self) -> str:
        value = self._call("dump_ui")
        if not isinstance(value, str) or not value:
            raise RemoteBridgeError("设备端点没有返回 UI 树（dump_ui 回包为空）")
        return value

    def screenshot_bytes(self) -> bytes:
        data = self._call("screenshot_bytes")
        if isinstance(data, str):  # 允许服务端用 base64 回（例如经过文本代理时）
            return base64.b64decode(data)
        return bytes(data)

    def state(self) -> str:
        value = self._call("state")
        return str(value or "unknown")

    def user_activity(self) -> dict | None:
        """用户活动快照（V5 §十）。

        `None` 有三种来源，Python 侧**不需要区分**——它们都表示「本能力不可用」，
        消费方（`AndroidDeviceController.supports_user_activity`）会据此如实返回 False：
        JSON 的 `null`（老 APK 端点没有这个方法时的 404 也会落到这里）、
        端点如实回的 null（辅助功能没连上）。

        回包不是 dict 时原样返回，由 `_user_context_from_bridge` 做形状校验并降级——
        跨语言边界的形状错误要在**一个地方**统一处理，而不是在这里再写一遍。
        """
        value = self._call("user_activity")
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        return {"confirmed": False, "reason": f"端点返回了意外的形状：{type(value).__name__}"}

    # ---- 影子执行平面（V5 §五 / §六）----

    def shadow_session(self, session_id: str) -> dict:
        """探测影子平面（V5 §五）。

        与 `user_activity` 的关键区别：这里**不返回 None**。老 APK 端点没有这个路由时
        `_call` 会抛出来（404），由 `_shadow_session_from_bridge` 接成
        `available=False`——因为「影子平面不可用」本来就是这个调用的**正常答案**，
        而 `user_activity` 的 None 是「这个能力不存在」。两件事的处置不同，
        所以在传输层就不要把它们混成一个形状。
        """
        value = self._call("shadow_session", {"session_id": str(session_id)})
        if isinstance(value, dict):
            return value
        return {
            "available": False,
            "reason": f"端点返回了意外的形状：{type(value).__name__}",
            "display_id": None,
        }

    def shadow_release(self, session_id: str) -> None:
        """释放影子会话。走写超时（它是个动作，不是读数）。"""
        self._call(
            "shadow_release", {"session_id": str(session_id)}, timeout=WRITE_TIMEOUT_SECONDS
        )

    # ---- Act ----

    def tap(self, x: int, y: int) -> None:
        self._call("tap", {"x": int(x), "y": int(y)}, timeout=WRITE_TIMEOUT_SECONDS)

    def long_press(self, x: int, y: int, duration_ms: int) -> None:
        self._call(
            "long_press",
            {"x": int(x), "y": int(y), "duration_ms": int(duration_ms)},
            timeout=WRITE_TIMEOUT_SECONDS,
        )

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self._call(
            "swipe",
            {"x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2), "duration_ms": int(duration_ms)},
            timeout=WRITE_TIMEOUT_SECONDS,
        )

    def set_text(self, value: str) -> None:
        self._call("set_text", {"value": str(value)}, timeout=WRITE_TIMEOUT_SECONDS)

    def press_back(self) -> None:
        self._call("press_back", timeout=WRITE_TIMEOUT_SECONDS)

    def press_home(self) -> None:
        self._call("press_home", timeout=WRITE_TIMEOUT_SECONDS)

    def launch(self, package: str, activity: str | None) -> None:
        self._call(
            "launch",
            {"package": str(package), "activity": activity or None},
            timeout=WRITE_TIMEOUT_SECONDS,
        )


def build_remote_bridge(url: str | None = None, *, token: str | None = None) -> RemoteAndroidBridge:
    """按环境变量（或显式入参）构造远程桥，并**在装配期校验端口完整性**。

    校验放在这里而不是只放在 `factory`：任何直接 new 这个类的调用方都拿到同一条保证，
    不会出现「某条路径漏检、任务跑到中途才 AttributeError」——那是最难定位的一类失败。
    """
    resolved = (url or bridge_url_from_env() or "").strip()
    if not resolved:
        raise RemoteBridgeError(
            f"没有配置手机设备端点地址。请设置 {ENV_BRIDGE_URL}=http://<手机IP>:8765"
            "（地址在手机 Shadow 应用的「设备端点」页上显示）。"
        )
    bridge = RemoteAndroidBridge(resolved, token=token)
    # 复用 android.py 的同一份检查：远程桥也必须实现完整协议。
    from .android import assert_bridge_complete

    assert_bridge_complete(bridge)
    return bridge


__all__ = [
    "CODE_SERVICE_DISABLED",
    "ENV_BRIDGE_TOKEN",
    "ENV_BRIDGE_URL",
    "RemoteAndroidBridge",
    "RemoteBridgeError",
    "RemoteBridgeUnavailable",
    "bridge_url_from_env",
    "build_remote_bridge",
]
