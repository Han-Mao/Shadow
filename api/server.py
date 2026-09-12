"""FastAPI：MVP 任务与单步端点（§7.1 / §7.2）。对外 HTTP 契约。"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from agent.loop import AgentLoop
from agent.memory import Memory
from device import screenshot as shots
from device.adb import AdbController, AdbError
from device.emulator import resolve_serial
from models.action import ActionType, Point
from models.state import AgentState, Observation
from models.task import Task
from vision.vlm import VlmError

logger = logging.getLogger(__name__)

ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "artifacts/shots"))
# 对外响应默认脱敏（异常文本可能带文件路径等内部细节）；本机调试时置 1 可回传异常摘要
_DEBUG_ERRORS = os.getenv("SHADOW_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
# 全局只有一台目标设备：并发驱动会让点击/输入互相交错，必须串行化
_DEVICE_LOCK = threading.Lock()

app = FastAPI(title="BlueWhale Shadow Phone Agent", version="0.1.0")
adb = AdbController(serial=resolve_serial())
memory = Memory()
loop = AgentLoop(adb, memory)


@contextmanager
def device_access(wait_seconds: float = 0.0):
    """取得设备独占权。只给会**改变设备状态**的操作加锁。

    只读端点（/devices、/screenshot、/observe）不加锁：它们不改变设备状态，
    锁住反而会让任务运行时连设备信息都查不到。
    """
    acquired = (
        _DEVICE_LOCK.acquire(timeout=wait_seconds)
        if wait_seconds > 0
        else _DEVICE_LOCK.acquire(blocking=False)
    )
    if not acquired:
        raise HTTPException(status_code=409, detail="设备忙：已有任务或操作正在执行")
    try:
        yield
    finally:
        _DEVICE_LOCK.release()


class TapRequest(BaseModel):
    x: int
    y: int


class TextRequest(BaseModel):
    value: str


class TaskRequest(BaseModel):
    instruction: str
    context: str = ""
    max_steps: int = Field(default=10, ge=1, le=50)
    # 默认后台执行：同步跑完一个 loop 会长期占用 worker 线程（最长 max_steps × VLM 超时），
    # 并发时一个任务就能拖垮整个服务。演示时想一条命令看结果就传 wait=true。
    wait: bool = False
    wait_timeout: float = Field(default=120.0, ge=1, le=600)


class ActionRequest(BaseModel):
    type: str
    # 在契约层就约束 target，非法坐标会在进入 handler 前被拒（422），
    # 不会先拍一张截图、碰一次真机、再回头报参数错
    target: Point | str | None = None
    value: str | None = None


@app.exception_handler(AdbError)
def adb_error_handler(_, exc: AdbError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})


@app.exception_handler(VlmError)
def vlm_error_handler(_, exc: VlmError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"ok": False, "error": str(exc)})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：任何未预期异常都返回结构化 JSON，而不是裸 500 断连。

    客户端（尤其 PowerShell 的 ConvertFrom-Json）拿到非 JSON 响应时无法区分失败原因，
    这里统一成契约内的格式。完整堆栈只留在服务端日志，不回传给客户端。
    """
    logger.exception("未处理异常: %s %s", request.method, request.url.path)
    detail = f"{type(exc).__name__}: {exc}" if _DEBUG_ERRORS else "内部错误，详见服务端日志"
    return JSONResponse(status_code=500, content={"ok": False, "error": detail})


@app.get("/devices")
def devices():
    return {"serial": adb.serial, "state": adb.state()}


@app.post("/tap")
def tap(req: TapRequest):
    with device_access():
        adb.tap(req.x, req.y)
    return {"ok": True}


@app.post("/text")
def text(req: TextRequest):
    with device_access():
        adb.type_text(req.value)
    return {"ok": True}


@app.post("/back")
def back():
    with device_access():
        adb.back()
    return {"ok": True}


@app.post("/screenshot")
def screenshot():
    path = shots.capture(adb, ARTIFACT_DIR, name="latest.png")
    return {"path": str(path)}


def _run_one_off(state: AgentState, fn) -> Observation:
    """在独立 Memory 中注册 state 后执行。

    单步端点是一次性调试调用，刻意不写全局 store：否则每次试探性点击都会在
    任务列表里留下一批 __action__ 假任务。
    """
    local_memory = Memory()
    local_memory.put(state)
    return fn(AgentLoop(adb, local_memory), state)


@app.post("/observe")
def observe():
    state = AgentState(task=Task(instruction="__observe__"))
    obs = _run_one_off(state, lambda one_off, s: one_off.observe_once(s))
    return obs.model_dump(exclude={"ui_tree"})


@app.post("/actions")
def execute_action(req: ActionRequest):
    state = AgentState(task=Task(instruction="__action__"))
    try:
        action_type = ActionType(req.type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"未知 action_type: {req.type}")
    try:
        with device_access():
            obs = _run_one_off(
                state,
                lambda one_off, s: one_off.execute_action(s, action_type.value, req.target, req.value),
            )
    except ValidationError as exc:
        # target 由 pydantic 校验（Point | str | None），非法入参属于客户端错误，应回 400
        raise HTTPException(status_code=400, detail=f"Action 参数非法: {exc}") from exc
    return obs.model_dump(exclude={"ui_tree"})


def _run_background_task(state: AgentState) -> None:
    """后台线程入口。异常必须就地消化，线程里抛出的异常只会静默丢失。"""
    try:
        loop.run(state)
    except Exception:
        logger.exception("后台任务 %s 执行失败", state.task.id)
    finally:
        _DEVICE_LOCK.release()


def _run_task_async(state: AgentState) -> dict:
    if not _DEVICE_LOCK.acquire(blocking=False):
        memory.drop(state.task.id)
        raise HTTPException(status_code=409, detail="设备忙：已有任务在执行，请稍后重试")
    threading.Thread(
        target=_run_background_task,
        args=(state,),
        name=f"shadow-task-{state.task.id}",
        daemon=True,
    ).start()
    return memory.dump(state.task.id) or state.model_dump()


def _run_task_sync(state: AgentState, timeout: float) -> dict:
    """同步等任务结束。

    仍放到独立线程里跑：loop.run 是阻塞的，直接在本线程执行就没法在超时后返回。
    """
    if not _DEVICE_LOCK.acquire(timeout=timeout):
        memory.drop(state.task.id)
        raise HTTPException(status_code=409, detail=f"设备忙：{timeout}s 内未取得执行权")

    worker = threading.Thread(target=_run_background_task, args=(state,), daemon=True)
    worker.start()
    worker.join(timeout=timeout)

    if worker.is_alive():
        # 任务仍在跑，执行权由后台线程结束时释放——此处绝不能自己释放锁
        raise HTTPException(
            status_code=504,
            detail=f"任务未在 {timeout}s 内完成，仍在后台执行，请轮询 GET /tasks/{state.task.id}",
        )
    return memory.dump(state.task.id) or state.model_dump()


@app.post("/tasks")
def create_task(req: TaskRequest):
    state = AgentState(
        task=Task(
            instruction=req.instruction,
            context=req.context,
            max_steps=req.max_steps,
        )
    )
    memory.put(state)

    payload = _run_task_sync(state, req.wait_timeout) if req.wait else _run_task_async(state)
    payload["mode"] = "sync" if req.wait else "background"
    return payload


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    payload = memory.dump(task_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return payload


@app.get("/tasks/{task_id}/shots/{n}")
def get_shot(task_id: str, n: int):
    if memory.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    observations = memory.observations(task_id, n)
    if not observations:
        raise HTTPException(status_code=404, detail="该步骤截图不存在")
    path = Path(observations[0].screenshot_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="截图文件已删除")
    return FileResponse(path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8010")))
