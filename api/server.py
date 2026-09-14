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
from agent.scheduler import TaskScheduler
from agent.task_manager import TaskManager
from device import screenshot as shots
from device.adb import AdbController, AdbError
from device.emulator import resolve_serial, resolve_serials
from device.input import build_default_input
from device.pool import DevicePool
from device.session import DeviceSession
from models.action import Action, ActionRisk, ActionType, Point
from models.budget import TaskBudget
from models.task import TaskPriority, TaskStatus
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
task_store = TaskStore(STORAGE_DIR / "tasks")
checkpoint_store = CheckpointStore(STORAGE_DIR / "checkpoints")
# 轨迹落盘（V2.1）：长跑任务重启后不能「失忆」——恢复点在，但前面几步干了什么也得在
trajectory = TrajectoryStore(root=STORAGE_DIR / "trajectories")
# 审计/重放用的事件日志（V2.1 §二十三）。与轨迹分开：轨迹服务下一步决策（会被裁剪），
# 事件日志服务事后追溯（只追加）
event_log = EventLog(STORAGE_DIR / "events")
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
input_provider = build_default_input(adb)

# 没有 API Key 时不接 LLM 判定，Classifier 自动退化为「规则 + 相似度」两层
classifier = TaskClassifier(
    llm_judge=classify_relation if os.getenv("VLM_API_KEY") else None
)
manager = TaskManager(store=task_store, scheduler=scheduler, classifier=classifier)


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


class TextRequest(BaseModel):
    value: str


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
def devices():
    return {"serial": adb.serial, "state": adb.state()}


@app.post("/tap")
def tap(req: TapRequest):
    with device_access() as device:
        device.tap(req.x, req.y)
    return {"ok": True}


@app.post("/text")
def text(req: TextRequest):
    # 中文走 ADB Keyboard 广播，ASCII 走 input text —— 调用方不需要知道区别
    with device_access():
        input_provider.input(req.value)
    return {"ok": True, "provider": input_provider.name}


@app.post("/back")
def back():
    with device_access() as device:
        device.back()
    return {"ok": True}


@app.post("/screenshot")
def screenshot():
    path = shots.capture(adb, ARTIFACT_DIR, name="latest.png")
    return {"path": str(path)}


@app.post("/observe")
def observe():
    obs = observer.observe(adb, ARTIFACT_DIR, step=0)
    return obs.model_dump(exclude={"ui_tree"})


@app.post("/actions")
def execute_action(req: ActionRequest, request: Request):
    try:
        action_type = ActionType(req.type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"未知 action_type: {req.type}")
    try:
        action = Action(type=action_type, target=req.target, value=req.value)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Action 参数非法: {exc}") from exc

    with device_access() as device:
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

        pre = observer.observe(adb, ARTIFACT_DIR, step=0)
        assessment = ActionRiskGate.assess(
            action, context=RiskContext.from_observation(pre, instruction="__action__")
        )
        if assessment.requires_confirmation:
            raise HTTPException(
                status_code=403,
                detail=f"危险动作需经人工确认（目标元素风险复核）：{assessment.describe()}",
            )
        result = executor.execute(device, action, pre.ui_tree)
        post = observer.observe(adb, ARTIFACT_DIR, step=0, suffix="post") if result.get("ok") else pre
        post.action = action
        post.result = result
        verdict = verifier.verify_action("__action__", pre, action, post, result)
        post.status = verdict.outcome
        post.message = verdict.message

    payload = post.model_dump(exclude={"ui_tree"})
    payload["verification"] = verdict.model_dump()
    payload["risk"] = assessment.effective.value
    payload["risk_detail"] = assessment.describe()
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
    if not principal.may_use_device(req.device_serial):
        raise HTTPException(
            status_code=403,
            detail=f"该令牌无权使用设备 {req.device_serial}（允许：{sorted(principal.devices)}）",
        )
    task = manager.create(
        req.instruction,
        context=req.context,
        budget=_resolve_budget(req.budget, req.max_steps),
        priority=req.priority,
        device_serial=req.device_serial,
    )
    if req.wait:
        payload = _wait_for(task.id, req.wait_timeout)
        payload["mode"] = "sync"
        return payload

    payload = task.model_dump(mode="json")
    payload["mode"] = "background"
    return payload


@app.get("/tasks")
def list_tasks():
    return {"tasks": [t.model_dump(mode="json") for t in manager.list_all()]}


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    payload = task.model_dump(mode="json")
    payload["plan_progress"] = task.plan_progress()
    pending = runtime.pending_confirmation(task_id)
    if pending is not None:
        item = pending.model_dump(mode="json")
        # 确认令牌绑定「任务 + 哪个动作 + 任务版本」（V2.2 §九）：
        # 换个危险动作或任务被改写后，旧令牌立刻失效
        item["token"] = auth.issue_confirmation_token(
            task_id, pending.fingerprint, task.version
        )
        payload["pending_confirmation"] = item
    check = runtime.last_goal_check(task_id)
    if check is not None and hasattr(check, "to_dict"):
        payload["goal_verification"] = check.to_dict()
    return payload


@app.post("/tasks/{task_id}/pause")
def pause_task(task_id: str):
    if manager.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not manager.pause(task_id):
        raise HTTPException(status_code=409, detail="任务当前无法暂停（可能已结束）")
    return {"ok": True, "task": manager.get(task_id).model_dump(mode="json")}


@app.post("/tasks/{task_id}/resume")
def resume_task(task_id: str):
    if manager.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not manager.resume(task_id):
        raise HTTPException(status_code=409, detail="任务当前无法恢复（未处于暂停状态）")
    return {"ok": True, "task": manager.get(task_id).model_dump(mode="json")}


@app.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    if manager.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not manager.cancel(task_id):
        raise HTTPException(status_code=409, detail="任务当前无法取消（可能已结束）")
    return {"ok": True, "task": manager.get(task_id).model_dump(mode="json")}


@app.post("/tasks/{task_id}/confirm")
def confirm_task(task_id: str, req: ConfirmRequest):
    """危险动作的人工确认（HITL）。批准后该动作放行一次。

    两种语义要分开（V2.2 §四）：
    - **危险动作**：批准 = 放行该动作；否决 = 放弃该动作（进黑名单，换策略）
    - **完成裁定**：批准 = 认定任务完成；否决 = 任务还没完，继续做

    启用鉴权时还必须带上 `token`（见 `GET /tasks/{id}`）。
    """
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    goal_decision = runtime.is_goal_decision(task_id)
    pending = runtime.pending_confirmation(task_id)
    if pending is None and not goal_decision:
        raise HTTPException(status_code=409, detail="该任务当前没有待确认的动作或完成裁定")

    if auth.enabled():
        fingerprint = pending.fingerprint if pending is not None else "goal"
        if not auth.verify_confirmation_token(req.token, task_id, fingerprint, task.version):
            audit_log.record(
                method="POST",
                path=f"/tasks/{task_id}/confirm",
                principal="anonymous",
                status=403,
                note="confirmation token missing or invalid",
            )
            raise HTTPException(
                status_code=403,
                detail="缺少或错误的确认令牌：请从 GET /tasks/{id} 的 "
                "pending_confirmation.token 取值后再提交",
            )

    runtime.confirm(task_id, req.approved)
    task = manager.get(task_id)
    if req.approved or goal_decision:
        # 批准：重新入队（危险动作放行一次 / 完成认定生效）
        # 否决「完成裁定」：`runtime.confirm` 已经塞好 Re-plan 理由，重新入队继续做——
        # 把任务判死是错的，人说的是「还没做完」，不是「别做了」
        scheduler.submit(task, allow_preempt=False)
    else:
        task.mark(TaskStatus.FAILED)
        task_store.save(task)
    return {"ok": True, "approved": req.approved, "task": manager.get(task_id).model_dump(mode="json")}


@app.post("/tasks/{task_id}/inject")
def inject_task(task_id: str, req: InjectRequest, request: Request):
    """执行过程中注入新指令：由任务关系决定并入、排队还是抢占（V2 §二十一）。"""
    if manager.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    principal = current_principal(request)
    result = manager.inject(
        req.instruction,
        current_task_id=task_id,
        priority=req.priority,
        budget=_resolve_budget(req.budget, req.max_steps),
        allow_disruptive=req.allow_disruptive,
    )
    payload = result.model_dump()
    # 设备级权限：新任务会落到某台设备上，令牌没权限的设备不该被派到
    task = result.task
    if task is not None and not principal.may_use_device(task.device_serial):
        raise HTTPException(
            status_code=403,
            detail=f"该令牌无权使用设备 {task.device_serial}（允许：{sorted(principal.devices)}）",
        )
    return payload


@app.get("/tasks/{task_id}/history")
def task_history(task_id: str, limit: int = 50):
    if manager.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    entries = trajectory.history(task_id)
    return {"task_id": task_id, "count": len(entries), "history": [o.model_dump() for o in entries[-limit:]]}


@app.get("/tasks/{task_id}/events")
def task_events(task_id: str, limit: int = 200):
    """审计事件流（V2.1 §二十三）。

    与 `/history` 的区别：history 是给下一步决策看的观察轨迹（会被裁剪），
    events 是给事后追溯看的只追加事件流（抢占、对账、失败原因都在里面），
    也是未来做 Replay 的数据源。
    """
    if manager.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    events = event_log.read(task_id, limit=limit)
    return {
        "task_id": task_id,
        "count": len(events),
        "events": [event.to_dict() for event in events],
    }


@app.get("/tasks/{task_id}/replay")
def task_replay(task_id: str, format: str = "json", limit: int = 1000):
    """任务回放（V2.1 §二十三）。

    `format=json` 返回时间轴 + 动作计划（给程序用）；
    `format=markdown` 返回人读报告，开头就是「值得注意的地方」——
    排查时最先要看的是出问题那几帧，不是完整流水。

    只读，不重放动作。要真重放请用 `agent.replay.replay()`，
    它默认 dry-run，且危险动作必须显式放行。
    """
    if manager.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    timeline = replay_mod.load_timeline(event_log, task_id, limit=limit)
    if format == "markdown":
        return PlainTextResponse(timeline.render_markdown())
    return {**timeline.to_dict(), "plan": replay_mod.build_plan(timeline).to_dict()}


@app.get("/tasks/{task_id}/checkpoint")
def task_checkpoint(task_id: str):
    if manager.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    checkpoint = checkpoint_store.latest_for_task(task_id)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="该任务还没有 Checkpoint")
    return checkpoint.summary()


@app.get("/tasks/{task_id}/shots/{n}")
def get_shot(task_id: str, n: int):
    if manager.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    observations = trajectory.observations_at(task_id, n)
    if not observations:
        raise HTTPException(status_code=404, detail="该步骤截图不存在")
    path = Path(observations[0].screenshot_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="截图文件已删除")
    return FileResponse(path)


@app.get("/scheduler")
def scheduler_state():
    return scheduler.snapshot()


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
