"""FastAPI：任务编排、调度与单步调试端点（V2 §二十）。对外 HTTP 契约。

分层落点：
    API  →  TaskManager（生命周期/注入）  →  TaskScheduler（谁现在执行）
         →  AgentRuntime（怎么完成）      →  Vision / Device
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, ValidationError

from agent import executor, observer, replay as replay_mod, verifier
from agent.classifier import TaskClassifier
from agent.risk_gate import ActionRiskGate, RiskContext
from agent.runtime import AgentRuntime
from agent.scheduler import DeviceNotAllowedError, TaskScheduler
from agent.task_manager import TaskManager
from device import screenshot as shots
from device.adb import AdbController, AdbError
from device.emulator import resolve_serial, resolve_serials
from device.input import build_default_input
from device.pool import DevicePool, UnknownDeviceError, storage_hint
from device.session import DeviceSession
from models.action import Action, ActionRisk, ActionType, Point
from models.budget import TaskBudget
from models.task import RECOVERY_ERROR, TaskPriority, TaskStatus
from storage import CheckpointStore, EventLog, TaskStore, TrajectoryStore
from storage.audit_log import AuditLog
from vision.vlm import VlmError, classify_relation

from . import auth

logger = logging.getLogger(__name__)

ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "artifacts/shots"))
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "artifacts/state"))
AUDIT_DIR = Path(os.getenv("AUDIT_DIR", str(STORAGE_DIR / "audit")))
PORT = int(os.getenv("PORT", "8010"))
HOST = os.getenv("HOST", "127.0.0.1")
# 对外响应默认脱敏（异常文本可能带文件路径等内部细节）；本机调试时置 1 可回传异常摘要
_DEBUG_ERRORS = os.getenv("SHADOW_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
# 请求审计默认开启（写失败永不抛异常）；SHADOW_AUDIT=0 可关掉
_AUDIT = os.getenv("SHADOW_AUDIT", "1").strip().lower() not in {"0", "false", "no", "off"}
# 单步调试端点占用设备时，用它作为 DeviceSession 的 owner 标识
_MANUAL_OWNER = "__manual__"

# ---- 依赖装配 ----

adb = AdbController(serial=resolve_serial())
# 多设备（V2.1 §十三）：ADB_SERIAL 支持逗号分隔，调度器会为每台设备起一个 worker。
# 单设备时 pool 里就一台，行为与老版本完全一致。
device_pool = DevicePool(
    [DeviceSession(AdbController(serial=serial), serial=serial) for serial in resolve_serials()]
)
# 单步调试端点（/tap、/screenshot、/owned）面向「当前主设备」，仍用第一台
session = device_pool.first()
# 审计/重放用的事件日志（V2.1 §二十三）。与轨迹分开：轨迹服务下一步决策（会被裁剪），
# 事件日志服务事后追溯（只追加）。
# V2.4 起它还要接一件更早的事——TaskStore 发现损坏任务时在这里留一条 TASK_CORRUPTED，
# 所以必须先于 task_store 构造。
event_log = EventLog(STORAGE_DIR / "events")
# 损坏任务的隔离与留痕都挂在同一个日志上（V2.4 §十）
task_store = TaskStore(STORAGE_DIR / "tasks", event_log=event_log)
checkpoint_store = CheckpointStore(STORAGE_DIR / "checkpoints")
# 轨迹落盘（V2.1）：长跑任务重启后不能「失忆」——恢复点在，但前面几步干了什么也得在
trajectory = TrajectoryStore(root=STORAGE_DIR / "trajectories")
# 请求审计（V2.2 §九）：回答「谁在什么时候调了什么、被批准还是被拒绝」
audit_log = AuditLog(AUDIT_DIR)

runtime = AgentRuntime(
    device_pool,
    artifact_dir=ARTIFACT_DIR,
    trajectory=trajectory,
    checkpoints=checkpoint_store,
    task_store=task_store,
    event_log=event_log,
)
scheduler = TaskScheduler(
    runtime, device_pool, task_store=task_store, event_log=event_log
)

# 没有 API Key 时不接 LLM 判定，Classifier 自动退化为「规则 + 相似度」两层
classifier = TaskClassifier(
    llm_judge=classify_relation if os.getenv("VLM_API_KEY") else None
)
# runtime 要交给 TaskManager（V2.2 §三/§九）：人工确认这条路径的状态写入
# 收口在 TaskManager 内部，API 不再自己调 runtime.confirm + task.mark(FAILED)。
manager = TaskManager(
    store=task_store, scheduler=scheduler, classifier=classifier, runtime=runtime
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(title="BlueWhale Shadow Phone Agent", version="0.3.0", lifespan=lifespan)


@app.middleware("http")
async def access_control(request: Request, call_next):
    """统一访问控制 + 请求审计（V2.2 §九）。

    顺序刻意如此：**先鉴权，再记账**。被拒绝的请求同样要留痕——
    「谁在反复试 /confirm」本身就是需要被看见的信号。
    """
    path = request.url.path
    principal = auth.authenticate(
        request.headers.get("authorization"), request.headers.get("x-api-token")
    )

    def record(status: int, note: str) -> None:
        if not _AUDIT:
            return
        audit_log.record(
            method=request.method,
            path=path,
            client=request.client.host if request.client else "",
            principal=principal.name if principal else "anonymous",
            status=status,
            note=note,
        )

    if auth.is_public_path(path):
        response = await call_next(request)
        record(response.status_code, "public")
        return response

    if auth.enabled() or auth.require_auth():
        if principal is None:
            record(401, "token missing or invalid")
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "未授权：请携带 Authorization: Bearer <token>"},
            )
        if principal.read_only and request.method not in {"GET", "HEAD", "OPTIONS"}:
            record(403, "read-only token attempted a write")
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": "该令牌是只读的，不能执行会改变设备的操作"},
            )
    else:
        principal = auth.ANONYMOUS

    request.state.principal = principal
    response = await call_next(request)
    record(response.status_code, "ok" if response.status_code < 400 else "error")
    return response


def current_principal(request: Request) -> auth.Principal:
    """取本次请求的身份（鉴权关闭时是匿名身份）。"""
    return getattr(request.state, "principal", auth.ANONYMOUS)


# ---- 对象级授权（V2.2 §一 / §三 / §四）----
#
# 审核的核心判断：现在的授权是「谁能发起操作」，而不是「谁能读取/控制哪个 Task」。
# 三个后果，都真实存在过：
#   1. `/inject` 先调 manager.inject() 改完任务、再检查设备 → Authorization after side effect
#   2. 不指定 device_serial 就能绕过设备限制（调度器自动挑，可能挑到别人的设备）
#   3. GET /tasks/{id} 不查设备 → 受限令牌能读到别人任务的**放行令牌**
#
# 所以这里只有一个入口：先解析对象、再判权限、最后才动它。


def _audit_denied(request: Request, principal: auth.Principal, note: str, detail: str) -> HTTPException:
    if _AUDIT:
        audit_log.record(
            method=request.method,
            path=request.url.path,
            principal=principal.name,
            status=403,
            note=note or detail,
        )
    return HTTPException(status_code=403, detail=detail)


def require_task_access(task_id: str, request: Request):
    """解析任务并做对象级授权；不通过时抛 404/403，通过则返回任务对象。"""
    principal = current_principal(request)
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not principal.may_access_task(task):
        raise _audit_denied(
            request,
            principal,
            "object-level task access denied",
            f"该令牌无权访问任务 {task_id}（任务在设备 {task.device_serial or '未绑定'}；"
            f"允许的设备：{sorted(principal.devices)}）",
        )
    return task


def recovery_error_payload(task_id: str) -> dict:
    """损坏任务的可辨识响应（V2.4 §十 / V2.5 §三、§四）。

    「不存在」与「数据坏了」是两种故障：前者多半是 id 写错，后者需要人工介入。
    但损坏任务没有 device_serial、做不了对象级授权，所以调用方必须先过
    `may_observe_recovery()` —— 无权的调用方统一走 404，避免存在性泄露。
    """
    return {
        "id": task_id,
        "status": RECOVERY_ERROR,
        "detail": "任务数据损坏，已隔离到任务存储的 quarantine/ 目录，不会被调度器恢复",
        "quarantined_as": f"{task_id}.corrupt.json",
        "recoverable": False,
    }


def may_observe_recovery(request: Request) -> bool:
    """当前调用方能不能看到「损坏任务」这种特殊状态（V2.5 §四）。

    只有**不受设备范围限制**的令牌可以。受限令牌看不到任务归属，放行就等于在
    对象级授权上开一个旁路——「知道 id 就能探测该任务是否存在、是否损坏」。
    """
    return current_principal(request).allowed_serials() is None


def allowed_devices_for(request: Request) -> frozenset[str] | None:
    """当前调用方的设备授权范围（None = 不限）。要传给调度器，而不是只用来判断。"""
    return current_principal(request).allowed_serials()


def require_device_access(serial: str | None, request: Request) -> None:
    """设备级授权：指定了设备就必须在允许列表里。"""
    principal = current_principal(request)
    if not principal.may_use_device(serial):
        raise _audit_denied(
            request,
            principal,
            "device access denied",
            f"该令牌无权使用设备 {serial}（允许的设备：{sorted(principal.devices)}）",
        )


def resolve_manual_device(serial: str | None, request: Request):
    """单步调试端点选设备：指定就按指定，不指定就取**被允许的**第一台。

    V2.2 §八：底层早就是多设备（DevicePool / 多车道），但 `/tap` `/observe` 这些
    仍然写死全局 `adb`，形成「Runtime 多设备 ✅ / Manual API 单设备 ❌」的割裂。
    """
    principal = current_principal(request)
    allowed = principal.allowed_serials()
    if serial:
        require_device_access(serial, request)
        try:
            return device_pool.require(serial)
        except UnknownDeviceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    candidates = [s for s in device_pool.serials if allowed is None or s in allowed]
    if not candidates:
        raise _audit_denied(
            request,
            principal,
            "no allowed device",
            f"没有可用的授权设备（池中：{device_pool.serials}；允许：{sorted(principal.devices)}）",
        )
    return device_pool.require(candidates[0])


@contextmanager
def device_access(timeout: float = 0.0):
    """单步调试端点临时占用设备。

    只给会**改变设备状态**的操作加锁：只读端点（/devices、/screenshot、/observe）
    不加锁，否则任务一跑起来连设备信息都查不到。
    """
    if not session.acquire(_MANUAL_OWNER, timeout=timeout):
        raise HTTPException(status_code=409, detail=f"设备忙：{session.owner or '未知任务'} 正在执行")
    try:
        yield adb
    finally:
        session.release(_MANUAL_OWNER)


# ---- 请求模型 ----


class TapRequest(BaseModel):
    x: int
    y: int
    device_serial: str | None = None
    """目标设备（V2.2 §八）。不填则取当前令牌可用设备中的第一台。"""


class TextRequest(BaseModel):
    value: str
    device_serial: str | None = None


class BudgetRequest(BaseModel):
    """三种预算可以分别指定（V2.2 §五）。

    为什么不再只用 `max_steps`：`TaskBudget` 里是三个互相独立的计数
    （动作步数 / 观察次数 / 模型调用），而 `max_steps` 只能映射到第一个。
    调用方写 `max_steps=10` 时，实际拿到的观察与模型预算仍是默认的 60 / 40——
    两边对「10」的理解根本不是一回事。现在可以显式写清楚。
    """

    max_action_steps: int | None = Field(default=None, ge=1, le=200)
    max_observations: int | None = Field(default=None, ge=1, le=500)
    max_model_calls: int | None = Field(default=None, ge=1, le=500)

    def to_budget(self, *, fallback_max_steps: int | None = None) -> TaskBudget:
        base = TaskBudget.from_max_steps(fallback_max_steps) if fallback_max_steps else TaskBudget()
        updates = {
            key: value
            for key, value in self.model_dump().items()
            if value is not None
        }
        return base.model_copy(update=updates) if updates else base


DEFAULT_MAX_STEPS = 10
"""旧的 `max_steps` 缺省值。

保留它作为兼容层的默认，保证「不写 max_steps 也不写 budget」的老请求
行为与升级前完全一致（动作步数上限 10）。
"""


def _resolve_budget(req_budget: BudgetRequest | None, max_steps: int | None) -> TaskBudget:
    """兼容层：优先用显式 `budget`，否则把旧的 `max_steps` 映射成动作步数上限。

    注意映射是**单向**的：`max_steps` 只决定动作步数，观察 / 模型调用取默认值。
    这正是审核指出的坑——调用方以为「10 步 = 整个任务最多循环 10 次」，
    实际拿到的是 10 个动作 + 60 次观察 + 40 次模型调用（V2.2 §五）。
    所以新代码请直接写 `budget`，别再用 `max_steps`。
    """
    effective_steps = max_steps if max_steps is not None else DEFAULT_MAX_STEPS
    if req_budget is not None:
        return req_budget.to_budget(fallback_max_steps=effective_steps)
    return TaskBudget.from_max_steps(effective_steps)


class TaskRequest(BaseModel):
    instruction: str
    context: str = ""
    # 兼容旧调用方。新调用方请用 budget（三个独立上限），语义见 README。
    max_steps: int | None = Field(default=None, ge=1, le=200)
    budget: BudgetRequest | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    # 默认后台执行：同步等一整个 loop 会长期占用 worker 线程
    wait: bool = False
    wait_timeout: float = Field(default=120.0, ge=1, le=600)
    # 指定跑在哪台设备上（V2.1 §十三）。不填就由调度器派给最闲的一台。
    device_serial: str | None = None


class InjectRequest(BaseModel):
    instruction: str
    priority: TaskPriority | None = None
    max_steps: int | None = Field(default=None, ge=1, le=200)
    budget: BudgetRequest | None = None
    # SUPER_TASK 会改写正在执行的任务目标（不可逆）。默认 False：
    # 命中 SUPER_TASK 时先返回 needs_confirmation，由调用方确认后再带 true 重发。
    allow_disruptive: bool = False


class ConfirmRequest(BaseModel):
    approved: bool = True
    token: str | None = None
    """人工确认令牌（V2.2 §九）。

    启用鉴权时必须提供，值从 `GET /tasks/{id}` 的 `pending_confirmation.token` 取。
    这样「知道 task_id」不再等于「有权放行危险动作」——令牌还绑定了具体是哪一个动作。
    """


class ActionRequest(BaseModel):
    type: str
    target: Point | str | None = None
    value: str | None = None
    device_serial: str | None = None


# ---- 异常处理 ----


@app.exception_handler(AdbError)
def adb_error_handler(_, exc: AdbError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})


@app.exception_handler(VlmError)
def vlm_error_handler(_, exc: VlmError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"ok": False, "error": str(exc)})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：任何未预期异常都返回结构化 JSON，而不是裸 500 断连。

    完整堆栈只留在服务端日志，不回传给客户端。
    """
    logger.exception("未处理异常: %s %s", request.method, request.url.path)
    detail = f"{type(exc).__name__}: {exc}" if _DEBUG_ERRORS else "内部错误，详见服务端日志"
    return JSONResponse(status_code=500, content={"ok": False, "error": detail})


# ---- 设备直连端点 ----


@app.get("/devices")
def devices(request: Request):
    """列出**当前令牌可用**的设备（V2.2 §八）。

    以前只回一台全局 `adb`，多设备下等于看不见另一半设备；
    而且没有任何授权过滤，受限令牌也能看到全部。
    """
    principal = current_principal(request)
    allowed = principal.allowed_serials()
    items = []
    for serial in device_pool.serials:
        if allowed is not None and serial not in allowed:
            continue
        session_item = device_pool.require(serial)
        items.append({"serial": serial, **session_item.snapshot(), "state": session_item.controller.state()})
    return {"count": len(items), "devices": items, "primary": adb.serial}


def _artifact_dir_for(session_item) -> Path:
    """产物按设备分目录（V2.2 §七）：多设备共用一个目录会互相覆盖、串证据。"""
    return storage_hint(ARTIFACT_DIR, session_item.serial)


@contextmanager
def device_access(session_item, *, timeout: float = 0.0):
    """单步调试端点临时占用设备。

    只给会**改变设备状态**的操作加锁：只读端点（/devices、/screenshot、/observe）
    不加锁，否则任务一跑起来连设备信息都查不到。
    """
    if not session_item.acquire(_MANUAL_OWNER, timeout=timeout):
        raise HTTPException(
            status_code=409, detail=f"设备忙：{session_item.owner or '未知任务'} 正在执行"
        )
    try:
        yield session_item.controller
    finally:
        session_item.release(_MANUAL_OWNER)


@app.post("/tap")
def tap(req: TapRequest, request: Request, device_serial: str | None = None):
    session_item = resolve_manual_device(req.device_serial or device_serial, request)
    with device_access(session_item) as device:
        device.tap(req.x, req.y)
    return {"ok": True, "device": session_item.serial, "generation": session_item.generation}


@app.post("/text")
def text(req: TextRequest, request: Request, device_serial: str | None = None):
    session_item = resolve_manual_device(req.device_serial or device_serial, request)
    # 中文走 ADB Keyboard 广播，ASCII 走 input text —— 调用方不需要知道区别
    provider = build_default_input(session_item.controller)
    with device_access(session_item):
        provider.input(req.value)
    return {
        "ok": True,
        "provider": provider.name,
        "device": session_item.serial,
        "generation": session_item.generation,
    }


@app.post("/back")
def back(request: Request, device_serial: str | None = None):
    session_item = resolve_manual_device(device_serial, request)
    with device_access(session_item) as device:
        device.back()
    return {"ok": True, "device": session_item.serial, "generation": session_item.generation}


# `stable` 的语义常量（V2.4 §八）。审核指出这个名字容易被读成「UI 已经稳定」，
# 但它实际只表示「这次采集没有跨越 Shadow 自己发起的写入」——页面动画自己在播、
# 别的客户端在改，generation 都不会动。把这个含义写进响应，免得调用方自己猜。
STABLE_MEANING = "no_known_shadow_write_during_observation"


@app.post("/screenshot")
def screenshot(request: Request, device_serial: str | None = None):
    """只读端点：不加设备锁，但会明确告诉你这次截图是不是「无已知写入」（V2.2 §九）。

    `stable` 的准确含义是 **no known Shadow write during observation**：
    只说明这段时间里没有 Shadow 自己的写操作越过去，**不等于**「UI 已经完全静止」
    （V2.4 §八）。所以它适合回答「这张图能不能当权威状态用」，
    不适合回答「这一屏是不是已经不抖了」。
    """
    session_item = resolve_manual_device(device_serial, request)
    before = session_item.generation
    path = shots.capture(
        session_item.controller,
        _artifact_dir_for(session_item),
        name=f"latest_{session_item.serial}.png",
    )
    after = session_item.generation
    return {
        "path": str(path),
        "device": session_item.serial,
        "generation": after,
        # 前后代次不同 = 中途有 Shadow 的写操作发生，这一张可能落在动画/过渡帧上
        "stable": before == after,
        "stable_meaning": STABLE_MEANING,
    }


@app.post("/observe")
def observe(request: Request, device_serial: str | None = None):
    """只读端点，返回带代次的整屏观察。

    `stable=false` 表示这次观察跨越了一次 Shadow 自己的设备写入——消费者**不能**
    把它当成「当前稳定页面」，否则很容易把过渡动画页当成真实状态（V2.2 §九）。
    `stable=true` 也只是 **no known Shadow write during observation**，
    不代表 UI 已经静止（V2.4 §八）。
    """
    session_item = resolve_manual_device(device_serial, request)
    before = session_item.generation
    obs = observer.observe(session_item.controller, _artifact_dir_for(session_item), step=0)
    after = session_item.generation
    payload = obs.model_dump(exclude={"ui_tree"})
    payload.update(
        {
            "device": session_item.serial,
            "generation": after,
            "stable": before == after,
            "stable_meaning": STABLE_MEANING,
        }
    )
    return payload


@app.post("/actions")
def execute_action(req: ActionRequest, request: Request, device_serial: str | None = None):
    try:
        action_type = ActionType(req.type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"未知 action_type: {req.type}")
    try:
        action = Action(type=action_type, target=req.target, value=req.value)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Action 参数非法: {exc}") from exc

    session_item = resolve_manual_device(req.device_serial or device_serial, request)
    controller = session_item.controller
    artifact_dir = _artifact_dir_for(session_item)

    with device_access(session_item) as device:
        # 统一风险门禁：危险动作不能绕过 HITL 直接执行（V2.1 §十）。
        # 之前这里在 execute 之后才回传 risk，等于外部调用能静默执行危险动作。
        #
        # 分两轮判定（V2.2 §一）：
        #   第一轮不带上下文——零成本，先把「确认付款」这类明显的拦下来，
        #     而且不用碰设备（否则设备不可用时给的是 502 而不是 403，很误导）
        #   第二轮带 UI 树上下文复核——抓「点击红色按钮」其实是「立即购买」这种情况
        assessment = ActionRiskGate.assess(action)
        if assessment.requires_confirmation:
            raise HTTPException(
                status_code=403,
                detail=(
                    "危险动作需经人工确认：请通过 POST /tasks 触发任务流程，"
                    f"再由 /tasks/{{id}}/confirm 放行（判定依据：{assessment.describe()}）"
                ),
            )

        pre = observer.observe(controller, artifact_dir, step=0)
        assessment = ActionRiskGate.assess(
            action, context=RiskContext.from_observation(pre, instruction="__action__")
        )
        if assessment.requires_confirmation:
            raise HTTPException(
                status_code=403,
                detail=f"危险动作需经人工确认（目标元素风险复核）：{assessment.describe()}",
            )
        result = executor.execute(device, action, pre.ui_tree)
        post = (
            observer.observe(controller, artifact_dir, step=0, suffix="post")
            if result.get("ok")
            else pre
        )
        post.action = action
        post.result = result
        verdict = verifier.verify_action("__action__", pre, action, post, result)
        post.status = verdict.outcome
        post.message = verdict.message

    payload = post.model_dump(exclude={"ui_tree"})
    payload["verification"] = verdict.model_dump()
    payload["risk"] = assessment.effective.value
    payload["risk_detail"] = assessment.describe()
    payload["device"] = session_item.serial
    payload["generation"] = session_item.generation
    return payload


# ---- 任务端点 ----


def _wait_for(task_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = manager.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.is_terminal:
            return task.model_dump(mode="json")
        time.sleep(0.1)

    raise HTTPException(
        status_code=504,
        detail=f"任务未在 {timeout}s 内完成，仍在后台执行，请轮询 GET /tasks/{task_id}",
    )


@app.post("/tasks")
def create_task(req: TaskRequest, request: Request):
    principal = current_principal(request)
    allowed = allowed_devices_for(request)
    if req.device_serial is not None:
        require_device_access(req.device_serial, request)

    try:
        task = manager.create(
            req.instruction,
            context=req.context,
            budget=_resolve_budget(req.budget, req.max_steps),
            priority=req.priority,
            device_serial=req.device_serial,
            # 未指定设备时，调度器只能从被允许的设备里挑（V2.2 §二）。
            # 以前只检查 req.device_serial，不填就等于绕过限制。
            allowed_devices=allowed,
        )
    except DeviceNotAllowedError as exc:
        raise _audit_denied(request, principal, "no allowed device", str(exc)) from exc
    if req.wait:
        payload = _wait_for(task.id, req.wait_timeout)
        payload["mode"] = "sync"
        return payload

    payload = task.model_dump(mode="json")
    payload["mode"] = "background"
    return payload


@app.get("/tasks")
def list_tasks(request: Request):
    """列出**当前令牌有权访问**的任务（V2.2 §四）。

    受限令牌不该看到别人设备上的任务——列表本身就是一种信息泄露
    （任务指令里有用户要干什么）。
    """
    principal = current_principal(request)
    visible = [t for t in manager.list_all() if principal.may_access_task(t)]
    return {
        "tasks": [t.model_dump(mode="json") for t in visible],
        "count": len(visible),
        # V2.4 §十：损坏任务不会出现在 tasks 里（它根本反序列化不出来），但必须让调用方
        # 知道「有这么一条」，否则它就从系统里静默消失了——只看 count 是看不出来的。
        # V2.5 §四：受限令牌看不到这份清单（无法判断归属，宁可少给）。
        "corrupt": sorted(task_store.corrupt_ids()) if may_observe_recovery(request) else [],
    }


@app.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request):
    """任务详情。

    V2.5 §三 / §四：损坏任务要能被识别，但**只能被有权的人识别**。

    - 先探一次 `manager.get()`：隔离是惰性的，可能就发生在这次读取里；不复查的话
      第一次请求会 404、第二次才 recovery_error，前后不一致。
    - `recovery_error` 只对不受设备范围限制的令牌返回，受限令牌一律 404 ——
      损坏任务没有 device_serial，做不了对象级授权，放行就等于存在性泄露。
    """
    task = manager.get(task_id)
    if task is None and task_store.is_corrupt(task_id) and may_observe_recovery(request):
        return recovery_error_payload(task_id)
    task = require_task_access(task_id, request)
    principal = current_principal(request)
    payload = task.model_dump(mode="json")
    payload["plan_progress"] = task.plan_progress()
    pending = runtime.pending_confirmation(task_id)
    if pending is not None:
        item = pending.model_dump(mode="json")
        # V2.7 P1-8：**GET 不再下发可直接使用的确认令牌**。响应会进日志、前端状态、
        # 代理缓存和浏览器调试工具，而这是一张「能放行真实危险动作」的凭据。
        # 这里只给元数据，令牌改为 POST 显式申请。
        item.pop("token", None)
        item["token_endpoint"] = f"/tasks/{task_id}/confirmation-token"
        item["token_expires_in_seconds"] = auth.CONFIRM_TTL_SECONDS
        payload["pending_confirmation"] = item
    payload["confirmation_kind"] = manager.confirmation_kind(task_id)
    check = runtime.last_goal_check(task_id)
    if check is not None and hasattr(check, "to_dict"):
        payload["goal_verification"] = check.to_dict()
    return payload


@app.post("/tasks/{task_id}/pause")
def pause_task(task_id: str, request: Request):
    require_task_access(task_id, request)
    if not manager.pause(task_id):
        raise HTTPException(status_code=409, detail="任务当前无法暂停（可能已结束）")
    return {"ok": True, "task": manager.get(task_id).model_dump(mode="json")}


@app.post("/tasks/{task_id}/resume")
def resume_task(task_id: str, request: Request):
    require_task_access(task_id, request)
    if not manager.resume(task_id, allowed_devices=allowed_devices_for(request)):
        raise HTTPException(status_code=409, detail="任务当前无法恢复（未处于暂停状态）")
    return {"ok": True, "task": manager.get(task_id).model_dump(mode="json")}


@app.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, request: Request):
    """取消任务。**这才是「结束这条任务」的唯一入口**（V2.2 §三）。

    否决一次危险动作、或者否决一次完成申请，都不该让任务结束——
    那只是「这个做法不行」或「还没做完」。
    """
    require_task_access(task_id, request)
    if not manager.cancel(task_id):
        raise HTTPException(status_code=409, detail="任务当前无法取消（可能已结束）")
    return {"ok": True, "task": manager.get(task_id).model_dump(mode="json")}


@app.post("/tasks/{task_id}/confirmation-token")
def issue_task_confirmation_token(task_id: str, request: Request):
    """为当前待确认事项**显式申请**一张一次性令牌（V2.7 P1-8）。

    为什么不跟 `GET /tasks/{id}` 一起返回：拿到令牌就能放行真实危险动作，而 GET 响应会
    被写进日志、前端状态、代理缓存和调试工具。显式 POST 让「要动手机」这件事在审计里
    看得见，获取时机也可控。

    令牌绑定「调用方身份 + task_id + 动作指纹（或确认类型）+ 任务版本 + 有效期」，
    并且是一次性的——消费后再用同一张票会被拒（V2.4 §九）。
    """
    task = require_task_access(task_id, request)
    principal = current_principal(request)
    kind = manager.confirmation_kind(task_id)
    if kind == "none":
        raise HTTPException(status_code=409, detail="该任务当前没有待确认的动作或裁定")

    pending = runtime.pending_confirmation(task_id)
    fingerprint = pending.fingerprint if pending is not None else kind
    token = auth.issue_confirmation_token(principal.name, task_id, fingerprint, task.version)
    if _AUDIT:
        audit_log.record(
            method="POST",
            path=request.url.path,
            principal=principal.name,
            status=200,
            note=f"confirmation token issued (kind={kind})",
        )
    return {
        "token": token,
        "expires_in_seconds": auth.CONFIRM_TTL_SECONDS,
        "kind": kind,
        "task_id": task_id,
    }


@app.post("/tasks/{task_id}/confirm")
def confirm_task(task_id: str, req: ConfirmRequest, request: Request):
    """人工处理一次待确认事项（危险动作 / 完成裁定）。

    两种语义不同，但**落点一致：都重新入队继续做**（V2.2 §三）
    - **危险动作**：批准 = 放行该动作；否决 = 这个动作不要了，换策略继续
    - **完成裁定**：批准 = 认定任务完成；否决 = 还没做完，继续做

    只有 `POST /tasks/{id}/cancel` 才会结束任务。以前这里否决就直接
    `task.mark(FAILED)`，与 Runtime「拒绝后换策略」的设计相反——
    那不是权限问题，是两个模块的状态机在打架。

    启用鉴权时还必须带上 `token`：它绑定调用方身份、动作指纹、任务版本与有效期，
    而且是**一次性**的——同一张票据第二次提交会被明确拒绝（V2.4 §九）。
    """
    task = require_task_access(task_id, request)
    principal = current_principal(request)
    kind = manager.confirmation_kind(task_id)

    if kind == "none":
        raise HTTPException(status_code=409, detail="该任务当前没有待确认的动作或完成裁定")

    if auth.enabled():
        pending = runtime.pending_confirmation(task_id)
        # 没有 pending 动作时，指纹取确认类型（goal / recovery）——它们同样要绑进令牌，
        # 否则「完成裁定令牌」和「崩溃恢复放行令牌」可以互换（V2.6 §七）
        fingerprint = (
            pending.fingerprint if pending is not None else manager.confirmation_kind(task_id)
        )
        ok, reason = auth.consume_confirmation_token(
            req.token, principal.name, task_id, fingerprint, task.version
        )
        if not ok:
            if _AUDIT:
                audit_log.record(
                    method="POST",
                    path=request.url.path,
                    principal=principal.name,
                    status=403,
                    note=f"confirmation denied: {reason}",
                )
            raise HTTPException(
                status_code=403,
                detail=(
                    f"确认未通过（{reason}）：请从 GET /tasks/{{id}} 的 "
                    "pending_confirmation.token 取值后再提交"
                ),
            )

    # 状态写入全部收口到 TaskManager（V2.2 §九 / §十）：API 不再自己 task.mark(...)
    resolved = manager.resolve_confirmation(task_id, approved=req.approved)
    if resolved is None:
        raise HTTPException(status_code=409, detail="该任务当前没有可处理的待确认事项")
    return {
        "ok": True,
        "approved": req.approved,
        "kind": kind,
        "task": manager.get(task_id).model_dump(mode="json"),
    }


@app.post("/tasks/{task_id}/inject")
def inject_task(task_id: str, req: InjectRequest, request: Request):
    """执行过程中注入新指令：由任务关系决定并入、排队还是抢占（V2 §二十一）。

    **授权必须在 `manager.inject()` 之前完成**（V2.2 §一）。

    这不是「位置不优雅」，而是一个真实的授权后置漏洞：
    `manager.inject()` 会在一次调用里改 instruction / version / plan /
    checkpoint_id / priority，SUPER_TASK 还会请求抢占。如果先执行它、
    再检查设备权限并返回 403，**任务已经被改掉了**——
    Authorization after side effect：调用方拿到了失败响应，副作用却已经发生。
    """
    task = require_task_access(task_id, request)
    principal = current_principal(request)
    allowed = allowed_devices_for(request)

    try:
        result = manager.inject(
            req.instruction,
            current_task_id=task.id,
            priority=req.priority,
            budget=_resolve_budget(req.budget, req.max_steps),
            allow_disruptive=req.allow_disruptive,
            allowed_devices=allowed,
        )
    except DeviceNotAllowedError as exc:
        raise _audit_denied(request, principal, "no allowed device", str(exc)) from exc
    payload = result.model_dump()

    # 防御性复核：新任务也必须在授权范围内。
    # 正常情况下提交时就被调度器挡下了（`allowed_devices` 已经生效），
    # 这里是第二道闸——**但它不是唯一的一道**，判权限的主战场在前面。
    launched = result.task
    if launched is not None and not principal.may_access_task(
        manager.get(launched.id) or launched
    ):
        logger.error(
            "已授权调用 %s 产生了一条越权任务 %s（设备 %s）——调度器的设备限制未生效",
            principal.name,
            launched.id,
            launched.device_serial,
        )
        raise _audit_denied(
            request,
            principal,
            "post-inject cross-device escape",
            "注入结果落在了未授权的设备上，已记录（请检查 allowed_devices 传递链路）",
        )
    return payload


@app.get("/tasks/{task_id}/history")
def task_history(task_id: str, request: Request, limit: int = 50):
    require_task_access(task_id, request)
    entries = trajectory.history(task_id)
    return {"task_id": task_id, "count": len(entries), "history": [o.model_dump() for o in entries[-limit:]]}


@app.get("/tasks/{task_id}/events")
def task_events(task_id: str, request: Request, limit: int = 200):
    """审计事件流（V2.1 §二十三）。

    与 `/history` 的区别：history 是给下一步决策看的观察轨迹（会被裁剪），
    events 是给事后追溯看的只追加事件流（抢占、对账、失败原因都在里面），
    也是未来做 Replay 的数据源。
    """
    require_task_access(task_id, request)
    events = event_log.read(task_id, limit=limit)
    return {
        "task_id": task_id,
        "count": len(events),
        "events": [event.to_dict() for event in events],
    }


@app.get("/tasks/{task_id}/replay")
def task_replay(task_id: str, request: Request, format: str = "json", limit: int = 1000):
    """任务回放（V2.1 §二十三）。

    `format=json` 返回时间轴 + 动作计划（给程序用）；
    `format=markdown` 返回人读报告，开头就是「值得注意的地方」——
    排查时最先要看的是出问题那几帧，不是完整流水。

    只读，不重放动作。要真重放请用 `agent.replay.replay()`，
    它默认 dry-run，且危险动作必须显式放行。
    """
    require_task_access(task_id, request)

    timeline = replay_mod.load_timeline(event_log, task_id, limit=limit)
    if format == "markdown":
        return PlainTextResponse(timeline.render_markdown())
    return {**timeline.to_dict(), "plan": replay_mod.build_plan(timeline).to_dict()}


@app.get("/tasks/{task_id}/checkpoint")
def task_checkpoint(task_id: str, request: Request):
    require_task_access(task_id, request)
    checkpoint = checkpoint_store.latest_for_task(task_id)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="该任务还没有 Checkpoint")
    return checkpoint.summary()


@app.get("/tasks/{task_id}/shots/{n}")
def get_shot(task_id: str, request: Request, n: int):
    require_task_access(task_id, request)
    observations = trajectory.observations_at(task_id, n)
    if not observations:
        raise HTTPException(status_code=404, detail="该步骤截图不存在")
    path = Path(observations[0].screenshot_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="截图文件已删除")
    return FileResponse(path)


@app.get("/scheduler")
def scheduler_state(request: Request):
    """调度器状态。受限令牌只看得到自己那几台设备（V2.2 §四）。

    这条接口会把每台设备的 owner、队列、可抢占目标都列出来——
    对受限令牌来说，别人的任务 id 与设备占用都是不该看到的信息。
    """
    principal = current_principal(request)
    allowed = principal.allowed_serials()
    snapshot = scheduler.snapshot()
    if allowed is None:
        return snapshot

    devices = {s: v for s, v in snapshot.get("devices", {}).items() if s in allowed}
    visible_running = {s: v for s, v in snapshot.get("running_tasks", {}).items() if s in allowed}
    snapshot["devices"] = devices
    snapshot["running_tasks"] = visible_running
    snapshot["scoped_to"] = sorted(allowed)
    return snapshot


@app.get("/health")
def health():
    """探活端点。唯一不需要鉴权的入口——运维探活不该还要带密钥。"""
    return {"ok": True, "auth": "token" if auth.enabled() else "disabled", "host": HOST}


if __name__ == "__main__":
    import uvicorn

    refusal = auth.bare_bind_refused(HOST)
    if refusal:
        raise SystemExit(refusal)
    if not auth.enabled():
        logger.warning(
            "未配置 SHADOW_API_TOKEN，API 无鉴权（仅建议在 127.0.0.1 本机使用）"
        )
    uvicorn.run(app, host=HOST, port=PORT)
