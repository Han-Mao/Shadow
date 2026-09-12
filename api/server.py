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
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from agent import executor, observer, verifier
from agent.classifier import TaskClassifier
from agent.runtime import AgentRuntime
from agent.scheduler import TaskScheduler
from agent.task_manager import TaskManager
from device import screenshot as shots
from device.adb import AdbController, AdbError
from device.emulator import resolve_serial
from device.input import build_default_input
from device.session import DeviceSession
from models.action import Action, ActionRisk, ActionType, Point
from models.budget import TaskBudget
from models.task import TaskPriority, TaskStatus
from storage import CheckpointStore, TaskStore, TrajectoryStore
from vision.vlm import VlmError, classify_relation

from agent.risk_gate import ActionRiskGate

logger = logging.getLogger(__name__)

ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "artifacts/shots"))
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "artifacts/state"))
PORT = int(os.getenv("PORT", "8010"))
# 对外响应默认脱敏（异常文本可能带文件路径等内部细节）；本机调试时置 1 可回传异常摘要
_DEBUG_ERRORS = os.getenv("SHADOW_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
# 单步调试端点占用设备时，用它作为 DeviceSession 的 owner 标识
_MANUAL_OWNER = "__manual__"

# ---- 依赖装配 ----

adb = AdbController(serial=resolve_serial())
session = DeviceSession(adb, serial=adb.serial)
task_store = TaskStore(STORAGE_DIR / "tasks")
checkpoint_store = CheckpointStore(STORAGE_DIR / "checkpoints")
trajectory = TrajectoryStore()

runtime = AgentRuntime(
    session,
    artifact_dir=ARTIFACT_DIR,
    trajectory=trajectory,
    checkpoints=checkpoint_store,
    task_store=task_store,
)
scheduler = TaskScheduler(runtime, session, task_store=task_store)
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


app = FastAPI(title="BlueWhale Shadow Phone Agent", version="0.2.0", lifespan=lifespan)


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


class TaskRequest(BaseModel):
    instruction: str
    context: str = ""
    max_steps: int = Field(default=10, ge=1, le=50)
    priority: TaskPriority = TaskPriority.NORMAL
    # 默认后台执行：同步等一整个 loop 会长期占用 worker 线程
    wait: bool = False
    wait_timeout: float = Field(default=120.0, ge=1, le=600)


class InjectRequest(BaseModel):
    instruction: str
    priority: TaskPriority | None = None
    max_steps: int = Field(default=10, ge=1, le=50)


class ConfirmRequest(BaseModel):
    approved: bool = True


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
def execute_action(req: ActionRequest):
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
        risk = ActionRiskGate.assess(action)
        if risk is ActionRisk.DANGEROUS:
            raise HTTPException(
                status_code=403,
                detail="危险动作需经人工确认：请通过 POST /tasks 触发任务流程，"
                "再由 /tasks/{id}/confirm 放行",
            )
        pre = observer.observe(adb, ARTIFACT_DIR, step=0)
        result = executor.execute(device, action, pre.ui_tree)
        post = observer.observe(adb, ARTIFACT_DIR, step=0, suffix="post") if result.get("ok") else pre
        post.action = action
        post.result = result
        verdict = verifier.verify_action("__action__", pre, action, post, result)
        post.status = verdict.outcome
        post.message = verdict.message

    payload = post.model_dump(exclude={"ui_tree"})
    payload["verification"] = verdict.model_dump()
    payload["risk"] = risk.value
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
def create_task(req: TaskRequest):
    task = manager.create(
        req.instruction,
        context=req.context,
        budget=TaskBudget(max_action_steps=req.max_steps),
        priority=req.priority,
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
        payload["pending_confirmation"] = pending.model_dump(mode="json")
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
    """危险动作的人工确认（HITL）。批准后该动作放行一次。"""
    if manager.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if runtime.pending_confirmation(task_id) is None:
        raise HTTPException(status_code=409, detail="该任务当前没有待确认的危险动作")

    runtime.confirm(task_id, req.approved)
    task = manager.get(task_id)
    if req.approved:
        scheduler.submit(task, allow_preempt=False)
    else:
        task.mark(TaskStatus.FAILED)
        task_store.save(task)
    return {"ok": True, "approved": req.approved, "task": manager.get(task_id).model_dump(mode="json")}


@app.post("/tasks/{task_id}/inject")
def inject_task(task_id: str, req: InjectRequest):
    """执行过程中注入新指令：由任务关系决定并入、排队还是抢占（V2 §二十一）。"""
    if manager.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    result = manager.inject(
        req.instruction,
        current_task_id=task_id,
        priority=req.priority,
        max_steps=req.max_steps,
    )
    return result.model_dump()


@app.get("/tasks/{task_id}/history")
def task_history(task_id: str, limit: int = 50):
    if manager.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    entries = trajectory.history(task_id)
    return {"task_id": task_id, "count": len(entries), "history": [o.model_dump() for o in entries[-limit:]]}


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)
