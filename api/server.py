"""FastAPI：MVP 任务与单步端点（§7.1 / §7.2）。对外 HTTP 契约。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from agent.loop import AgentLoop
from agent.memory import Memory
from device import screenshot as shots
from device.adb import AdbController, AdbError
from device.emulator import resolve_serial
from models.action import Action, ActionType
from models.state import AgentState, Observation
from models.task import Task
from vision.vlm import VlmError

ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "artifacts/shots"))

app = FastAPI(title="BlueWhale Shadow Phone Agent", version="0.1.0")
adb = AdbController(serial=resolve_serial())
memory = Memory()
loop = AgentLoop(adb, memory)


class TapRequest(BaseModel):
    x: int
    y: int


class TextRequest(BaseModel):
    value: str


class TaskRequest(BaseModel):
    instruction: str
    context: str = ""
    max_steps: int = Field(default=10, ge=1, le=50)


class ActionRequest(BaseModel):
    type: str
    target: Any | None = None
    value: str | None = None


@app.exception_handler(AdbError)
def adb_error_handler(_, exc: AdbError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})


@app.exception_handler(VlmError)
def vlm_error_handler(_, exc: VlmError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"ok": False, "error": str(exc)})


@app.get("/devices")
def devices():
    return {"serial": adb.serial, "state": adb.state()}


@app.post("/tap")
def tap(req: TapRequest):
    adb.tap(req.x, req.y)
    return {"ok": True}


@app.post("/text")
def text(req: TextRequest):
    adb.type_text(req.value)
    return {"ok": True}


@app.post("/back")
def back():
    adb.back()
    return {"ok": True}


@app.post("/screenshot")
def screenshot():
    path = shots.capture(adb, ARTIFACT_DIR, name="latest.png")
    return {"path": str(path)}


def _run_one_off(state: AgentState, fn) -> Observation:
    """在独立 Memory 中注册 state 后执行，使 memory.append 生效且不污染全局 store。"""
    local_memory = Memory()
    local_memory.put(state)
    return fn(AgentLoop(adb, local_memory), state)


@app.post("/observe")
def observe():
    state = AgentState(task=Task(instruction="__observe__"))
    obs = _run_one_off(state, lambda loop, s: loop.observe_once(s))
    return obs.model_dump(exclude={"ui_tree"})


@app.post("/actions")
def execute_action(req: ActionRequest):
    state = AgentState(task=Task(instruction="__action__"))
    try:
        action_type = ActionType(req.type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"未知 action_type: {req.type}")
    obs = _run_one_off(state, lambda loop, s: loop.execute_action(s, action_type.value, req.target, req.value))
    return obs.model_dump(exclude={"ui_tree"})


@app.post("/tasks")
def create_task(req: TaskRequest):
    task = Task(
        instruction=req.instruction,
        context=req.context,
        max_steps=req.max_steps,
    )
    state = AgentState(task=task)
    memory.put(state)
    loop.run(state)
    return state.model_dump()


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    state = memory.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="任务不存在")
    return state.model_dump()


@app.get("/tasks/{task_id}/shots/{n}")
def get_shot(task_id: str, n: int):
    state = memory.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="任务不存在")
    observations = [o for o in state.history if o.step == n]
    if not observations:
        raise HTTPException(status_code=404, detail="该步骤截图不存在")
    path = Path(observations[0].screenshot_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="截图文件已删除")
    return FileResponse(path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8010")))
