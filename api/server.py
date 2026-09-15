"""FastAPI：任务编排、调度与单步调试端点（V2 §二十）。对外 HTTP 契约。

分层落点：
    API  →  TaskManager（生命周期/注入）  →  TaskScheduler（谁现在执行）
         →  AgentRuntime（怎么完成）      →  Vision / Device
"""
from __future__ import annotations

import contextlib
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, ValidationError

from dataclasses import dataclass

from agent import executor, observer, replay as replay_mod, verifier
from agent.classifier import TaskClassifier
from agent.execution import ExecutionService
from agent.risk_gate import ActionRiskGate, RiskAssessment, RiskContext
from agent.runtime import AgentRuntime
from agent.scheduler import DeviceNotAllowedError, TaskScheduler
from agent.task_manager import TaskManager
from agent.verifier import Verification
from device import screenshot as shots
# V3.3 §1：API 只认设备**端口**与**装配函数**，不再直接 new 具体后端。
# 于是同一份 `api/server.py` 既能在 PC 上用 ADB 跑，也能在手机本机上跑
# （`SHADOW_DEVICE_BACKEND=android`）——方案文档 §10 的两个产品形态共用这一个入口。
from device.controller import DeviceController, DeviceError, is_read_only
from device.factory import (
    build_controller,
    describe_backend,
    resolve_device_serial,
    resolve_device_serials,
    selected_backend,
)
from device.pool import DevicePool, UnknownDeviceError, storage_hint
from device.session import DeviceSession
from models.action import Action, ActionEffectStatus, ActionRisk, ActionType, Point
from models.budget import TaskBudget
from models.exceptions import PersistenceError
from models.execution import (
    ActionExecution,
    EXECUTION_FAILED,
    EXECUTION_REFUSED,
    EXECUTION_SUCCEEDED,
    EXECUTION_UNVERIFIED,
)
from models.state import Observation
from models.task import RECOVERY_ERROR, TaskPriority, TaskStatus
from storage import (
    CheckpointStore,
    ConfirmationConsumptionStore,
    Database,
    EventLog,
    ExecutionStore,
    TaskStore,
    TrajectoryStore,
)
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

# ---- 单进程前提的硬闸（V3.1 §七 / §八）----
#
# `TaskStore` 的 revision CAS（`JsonStore.update_atomic`）与 `TaskManager._mutation_lock`
# 都只在**同一个 Python 进程内**成立：`threading.RLock` 锁不住跨进程，CAS 比较的又是
# 同一份磁盘 revision——两个 worker 各自持各自进程的锁、都通过比较、后写覆盖先写，
# 中间那次改写**静默丢失**。
#
# 跨进程的「一个任务同一时刻最多一个执行者」已经由 TaskLease 兜住（V3 M3，SQLite
# 原子 claim），但那只保证**不双执行**，保证不了 Task 文档不被互相覆盖。所以多 worker
# 不是「有风险但能用」，而是明确不支持。
#
# 审核意见这一点说得很对：代码里到处是 thread-safe / atomic / CAS 这些词，很容易被
# 读成「支持多 worker」。这种前提写在 README 里没人看，所以做成**拒绝启动**。
_MULTI_PROCESS_OPT_IN_VALUES = {"1", "true", "yes", "on"}


def _multi_process_opted_in() -> bool:
    """是否已显式声明「自行承担多进程的状态一致性风险」。

    刻意在**调用时**读环境变量而不是在导入时固化成常量——这样运维改完环境重启即可，
    也让这条硬闸可被单测直接驱动。
    """
    return (
        os.getenv("SHADOW_ALLOW_MULTI_PROCESS", "").strip().lower()
        in _MULTI_PROCESS_OPT_IN_VALUES
    )


def _guard_single_process() -> None:
    """检测到多 worker 时拒绝启动，而不是静默地丢状态（V3.1 §七/§八）。"""
    raw = (
        os.getenv("WEB_CONCURRENCY", "")
        or os.getenv("UVICORN_WORKERS", "")
        or os.getenv("GUNICORN_WORKERS", "")
    ).strip()
    if not raw:
        return
    try:
        workers = int(raw)
    except ValueError:
        # 配置值不是数字不在这里拦——那属于部署配置错误，交给启动脚本自己报
        logger.warning("无法解析的 worker 数量配置 %r，跳过单进程检查", raw)
        return
    if workers <= 1 or _multi_process_opted_in():
        return
    raise RuntimeError(
        f"检测到 {workers} 个 worker（WEB_CONCURRENCY/UVICORN_WORKERS={raw}），"
        "但 Shadow 目前只支持单进程：TaskStore 的 revision CAS 与写锁都只在进程内有效，"
        "多 worker 下任务文档会互相覆盖（跨进程 TaskLease 只保证不双执行，不保证不丢写）。"
        "请用 --workers 1 启动；若确有需要并已自行承担状态一致性风险，"
        "可显式设置 SHADOW_ALLOW_MULTI_PROCESS=1 跳过本检查。"
    )


_guard_single_process()

# ---- 依赖装配 ----

# 设备后端由 `SHADOW_DEVICE_BACKEND` 决定（V3.3 §1）：
#   adb（默认）  PC 通过 ADB 控制手机——开发模式
#   android      手机本机跑 Shadow 自己——产品模式（Accessibility + MediaProjection）
# 这里取到的是同一个 `DeviceController` 协议实例，所以下面所有的
# TaskManager / Scheduler / Runtime / RiskGate / Checkpoint 都**不用改一行**。
adb = build_controller(resolve_device_serial())
# 多设备（V2.1 §十三）：ADB_SERIAL 支持逗号分隔，调度器会为每台设备起一个 worker。
# 单设备时 pool 里就一台，行为与老版本完全一致。
device_pool = DevicePool(
    [DeviceSession(build_controller(serial), serial=serial) for serial in resolve_device_serials()]
)
# 单步调试端点（/tap、/screenshot、/owned）面向「当前主设备」，仍用第一台
session = device_pool.first()
logger.info("设备后端：%s", describe_backend(resolve_device_serial()))
# 审计/重放用的事件日志（V2.1 §二十三）。与轨迹分开：轨迹服务下一步决策（会被裁剪），
# 事件日志服务事后追溯（只追加）。
# V2.4 起它还要接一件更早的事——TaskStore 发现损坏任务时在这里留一条 TASK_CORRUPTED，
# 所以必须先于 task_store 构造。
#
# V4 §二/§三：任务、恢复点、事件现在在**同一个数据库**（`<STORAGE_DIR>/shadow.db`）。
# 一个 `Database` 对象 = 一个连接 + 一套迁移 + **可重入事务**，于是 §三 要的
# 「确认票据 + Task 状态 + 事件同一个事务」才有可能。旧的 `tasks/*.json` /
# `checkpoints/*.json` / `events/*.jsonl` 会在各 store 构造时一次性导入，升级不丢数据。
db = Database(STORAGE_DIR)
event_log = EventLog(db)
task_store = TaskStore(db, event_log=event_log)
checkpoint_store = CheckpointStore(db)
# 执行记录（V4 §一 · v4.1 §二）：手工端点每次调用一条，`execution_id` 同时是它的事件流所有者。
#
# v4.1 §二 起它也在**同一个库**里（此前是一执行一个 JSON 文件，是最后一个还没收敛的
# 存储）。搬进来的实际收益有三条：状态成为可查询的列（§六 的恢复扫描靠它）、
# 状态迁移可以用一条带 `WHERE status=?` 的语句做守卫（§七 的「同一条执行不能派发两次」）、
# 与它的事件流在同一个库里（§八 那条「Task → Execution → Events」的链）。
execution_store = ExecutionStore(db)
# v4.1 §九：状态迁移与它的事件收成一个入口。以前「建记录」在 API 层、「发事件」散在
# 各处，两者谁先谁后、失败一半怎么办全靠每个调用点自己记得——现在由它一处保证
# 「同一个事务、副作用在事务外」。
execution_service = ExecutionService(execution_store, event_log=event_log)

# ---- 确认令牌的「已消费」记录（V3.2 §二）----
#
# 默认实现是**进程内存**，而确认令牌的签名密钥在配置了 `SHADOW_API_TOKEN` /
# `SHADOW_CONFIRM_SECRET` 时是稳定的。两者相加的后果：服务重启后，一张在 TTL 内、
# 签名依然有效的旧票据会**重新变得可用**——「一次性」退化成了「进程生命周期内一次性」。
#
# 生产路径换成 SQLite：jti 是主键，消费是一条 INSERT，唯一约束由数据库保证，
# 天然是跨进程 + 跨重启的原子 consume。`SHADOW_CONFIRM_DB` 可显式指定位置
# （多个实例要共享「谁用过这张票据」时必须指向同一个文件）。
# V4 §三：票据表与 tasks / events 现在在**同一个库**里（`shadow.db`），
# 所以 `/confirm` 能把「校验并预占票据 → 改 Task 状态 → 写 CONFIRMED 事件 → 作废票据」
# 放进**一个事务**——跨文件是没有事务的，这是它从独立 `confirmations.db` 搬进来的原因。
#
# `SHADOW_CONFIRM_DB` 仍然可以覆盖（多实例必须共享「谁用过这张票据」时用），
# 但要清楚它的代价：指向**另一个文件**时跨库没有事务，§三 的原子性只在默认布局下成立。
_confirm_db = os.getenv("SHADOW_CONFIRM_DB", "").strip()
auth.configure_consumption(ConfirmationConsumptionStore(_confirm_db or db))
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


def _prune_orphan_checkpoints() -> int:
    """启动时清掉没人认领的恢复点（V3.2 §三）。

    `runtime._save_checkpoint` 是「写恢复点 → 改指针 → 落盘任务」三步。进程在最后
    一步之前崩溃，就会留下一个**孤儿恢复点**：文件在盘上，任务指针没提交。
    它不是数据不一致（任务永远不会指向不存在/没写完的恢复点），但会一直占着磁盘、
    还会让 `GET /tasks/{id}/checkpoint` 显示一个任务从未提交过的恢复点。

    两个刻意的选择：

    - 用 `list_all()` 而**不是** `list_active()`。终态任务同样可能有被提交过的恢复点
      （那个 GET 端点要读它）；只按活跃任务算会把它们当孤儿**误删**。
    - 只在启动时扫，不在运行期扫。孤儿只有「崩溃在恢复点与任务落盘之间」才会产生，
      启动扫一次就够；运行期扫反而会与正在写恢复点的 worker 抢。

    清理失败绝不能挡住启动——它是维护动作，不是启动前提。
    """
    try:
        committed = {
            task.id: task.checkpoint_id
            for task in task_store.list_all()
            if getattr(task, "checkpoint_id", "")
        }
        removed = checkpoint_store.prune_orphans(committed)
    except Exception as exc:  # noqa: BLE001 - 维护动作失败不影响服务启动
        logger.warning("启动清理孤儿恢复点失败（已跳过）：%s", exc)
        return 0
    if removed:
        logger.info("启动清理：移除 %d 个未被任务提交的恢复点", removed)
    return removed


@asynccontextmanager
async def lifespan(_: FastAPI):
    _prune_orphan_checkpoints()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(title="BlueWhale Shadow Phone Agent", version="0.3.2", lifespan=lifespan)


# 只读令牌允许的方法（V3.2 §七）。
#
# 此前判定是「readonly ⇒ 只能 GET」，而 `/screenshot` 与 `/observe` 是**纯读取**
# 却注册成了 POST —— 于是「只读令牌访问只读接口」拿到 403，一个纯粹的契约不一致。
# 两件事一起做：
#   1. 这两个端点同时注册 GET（契约上正确的读方法，见它们的 docstring）；
#   2. 判定从「HTTP 方法」升级成「**操作能力**」——方法只是代理指标，
#      真正的判据是「这次请求会不会改变设备状态」。
# 白名单刻意写死，不靠路径前缀猜：将来新增端点不会因为「恰好是 POST」被自动放行。
_READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_READ_ONLY_ALLOWED_POST_PATHS = frozenset({"/screenshot", "/observe"})


def _is_read_only_request(request: Request) -> bool:
    """这次请求是不是**真的**只读（V3.2 §七）。

    取「操作能力」而非「HTTP 方法」作判据：方法只是代理指标，
    拿它当唯一判据会把只读令牌挡在只读接口外面。
    """
    if request.method in _READ_ONLY_METHODS:
        return True
    return request.method == "POST" and request.url.path in _READ_ONLY_ALLOWED_POST_PATHS


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
        if principal.read_only and not _is_read_only_request(request):
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
#   3. GET /tasks/{id} 曾随响应下发放行令牌 → 受限令牌能读到别人任务的**放行令牌**
#      （V2.7 P1-8 已改为不随 GET 下发，需 POST /confirmation-token 显式申请）
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

    启用鉴权时必须提供。令牌**不随 GET 下发**（V2.7 P1-8），要先
    `POST /tasks/{id}/confirmation-token` 显式申请，再把拿到的值传到这里。
    这样「知道 task_id」不再等于「有权放行危险动作」——令牌还绑定了具体是哪一个动作，
    且申请动作本身会进审计。
    """


class ActionRequest(BaseModel):
    type: str
    target: Point | str | None = None
    value: str | None = None
    device_serial: str | None = None


# ---- 异常处理 ----


@app.exception_handler(DeviceError)
def device_error_handler(_, exc: DeviceError) -> JSONResponse:
    """设备后端失败 → 502。

    注册在**端口基类**上（V3.3 §1）：`AdbError` 与 `AndroidBridgeError` 都是它的子类，
    所以换后端不必动这里。以前只注册 `AdbError`，Android 后端一出错就会掉进 500 兜底，
    返回「内部错误」而不是「设备侧失败」——排查时会往完全错的方向找。
    """
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
    # V3.3 §1：以前这里写死 `adb.serial`（模块级的 ADB 控制器），是「ADB 泄漏进核心」
    # 的典型一处——换成 Android 后端后那个全局对象语义就变了。主设备应当是池里的第一台。
    return {"count": len(items), "devices": items, "primary": device_pool.first().serial}


def _artifact_dir_for(session_item) -> Path:
    """产物按设备分目录（V2.2 §七）：多设备共用一个目录会互相覆盖、串证据。"""
    return storage_hint(ARTIFACT_DIR, session_item.serial)


@contextmanager
def device_access(session_item, *, timeout: float = 0.0, operation: str | None = None):
    """单步调试端点临时占用设备。

    只给会**改变设备状态**的操作加锁：只读端点（/devices、/screenshot、/observe）
    不加锁，否则任务一跑起来连设备信息都查不到。

    `operation` 是被保护的设备操作名（V2.7 P1-7）：加锁前用 `device.adb.is_read_only`
    **真正校验**这次操作是写操作。若传入的是只读操作（screenshot / dump_ui 等），
    说明调用方把只读端点误包进了加锁路径，直接抛错暴露这个 bug——而不是靠 docstring
    里的一句「只读端点不加锁」约定。省略 `operation` 时保守按写处理（宁可多锁，不漏锁）。
    """
    if operation is not None and is_read_only(operation):
        raise HTTPException(
            status_code=500,
            detail=f"只读操作 {operation!r} 不该占用设备锁——这是调用方的接线错误",
        )
    if not session_item.acquire(_MANUAL_OWNER, timeout=timeout):
        raise HTTPException(
            status_code=409, detail=f"设备忙：{session_item.owner or '未知任务'} 正在执行"
        )
    try:
        yield session_item.controller
    finally:
        session_item.release(_MANUAL_OWNER)


# ---- 手工端点的统一执行内核（V3.2 §一 / §五）----
#
# 在此之前 `/tap`、`/text`、`/back` 直接调 `device.tap()` —— **完全绕过**
# `ActionRiskGate`。而 README 声称「所有改变设备的 Action 都先经过统一风险门禁」，
# 于是实际存在两条控制链：
#
#     Agent 路径： Action → RiskGate → HITL → Executor → Verifier
#     手工路径：   /tap   → DeviceSession → device.tap()
#
# 后果不是理论上的：在「订单确认页」上 `POST /tap {"x":680,"y":1200}` 能点掉
# 「立即付款」，既不判风险、也不进 HITL、设备侧还留不下审计记录。
#
# 现在四个手工端点共用下面这一个内核。要说清它**统一了什么、没统一什么**：
#
#   统一了：风险门禁（两轮，第二轮带 UI 上下文）、危险动作拒绝、执行、验证、
#           设备侧事件留痕（RISK_ASSESSED / ACTION_DISPATCHED / ACTION_VERIFIED）；
#   没统一：Runtime 那套 Task / TaskStep / StepAttempt / Checkpoint /
#           GoalVerification 闭环。手工动作不属于任何任务，硬塞进 Task 只会把
#           「任务事实」和「手工操作」混成一锅——共用的是**执行内核**，不是任务编排。
#           v4.1 §九 把这个执行内核抽成了 `agent/execution`（`ExecutionService`），
#           Runtime 与 API 都指向它；**仍然没统一**的是上面那句说的任务编排。

MANUAL_ACTION_TASK_ID = _MANUAL_OWNER
"""**已废弃：不要再用它当执行归属**（V4 §一）。

V3.2 起手工动作的事件流记在这个固定 id 下。好处是「有记录」，代价是所有请求的记录
混在同一条流里：两个同时进来的 `/tap` 与 `/text` 事后只能靠逐条字段去拼。
现在改用每次调用独立的 `ActionExecution.execution_id`，这个常量只为兼容旧引用而保留。

（注意 `_MANUAL_OWNER` 本身仍是**设备锁**的 owner——那是「手工路径整体占用设备」的
身份，不随请求变化，两者是不同的东西，别一起改掉。）

原说明：手工动作的事件流记在这个 id 下（V3.2 §五）。

它们不属于任何 Task，但「谁在什么时候点了哪、判成什么风险、验证结果如何」同样是
必须可追溯的事实——否则 `/history` 与 `/replay` 里的设备操作历史是残缺的。
"""


@dataclass(frozen=True)
class ManualActionOutcome:
    """一次手工动作的完整结果。内核只产出事实，端点负责翻译成 HTTP。"""

    action: Action
    assessment: RiskAssessment
    result: dict
    pre: Observation
    post: Observation
    verdict: Verification
    execution: ActionExecution
    """这次执行的身份与落定状态（V4 §一）：`execution_id` 在动作发出前就已落盘。"""


def _dangerous_refusal(assessment: RiskAssessment) -> str:
    """危险动作的统一拒绝文案。指路而不是含糊地 403，否则调用方只会反复重试。"""
    return (
        "危险动作需经人工确认：请通过 POST /tasks 触发任务流程，"
        f"再由 /tasks/{{id}}/confirm 放行（判定依据：{assessment.describe()}）"
    )


@contextlib.contextmanager
def _execution_guard(execution: ActionExecution, settle):
    """异常路径统一补记执行记录（V4 §一）。

    为什么需要它：`device_access` 在设备被占用时直接 409、`observer.observe` 也可能抛
    `DeviceUnavailableError`——如果没有这层，那条执行记录会**永远停在 RUNNING**，
    于是 `/executions` 里出现「看起来还在执行、其实早就失败了」的幽灵记录。

    门禁拒绝不走这里：它自己先 `settle(REFUSED, ...)`，被 `is_finished` 挡掉，
    不会被这层覆盖成 FAILED（「被拒绝」和「执行失败」是两种事实，不能混）。
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - 一律补记后原样抛出
        if not execution.is_finished:
            settle(EXECUTION_FAILED, note=f"未完成：{type(exc).__name__}")
        raise


def run_manual_action(
    action: Action,
    session_item,
    *,
    operation: str | None = None,
    request: Request | None = None,
) -> ManualActionOutcome:
    """手工端点**唯一**的执行内核（V3.2 §一）。

    `operation` 用于 `device_access` 的只读性结构化校验（`is_read_only`）。
    `/actions` 是动态分发（wait / done 这类无设备副作用的动作也会进来），
    传 None 走保守加锁——与它原来的行为一致。

    v4.1 §三/§四/§九：状态推进与事件现在都经 `execution_service`，顺序是
    **受理 → 判风险 → 记录意图 → 交给设备 → 记录结果**，每一段各自一个事务，
    而设备调用（`executor.execute`）在所有这些事务**之外**。
    """
    controller = session_item.controller
    artifact_dir = _artifact_dir_for(session_item)

    # ---- V4 §一 / v4.1 §三：先落一条执行记录，再做任何事 ----
    #
    # 顺序刻意的：记录先落盘，动作才有可能发出。反过来（先做后记）就是那种
    # 「手机真的点了付款、但查不到是谁点的」的状态。
    execution = _start_execution_or_refuse(
        action,
        session_item,
        principal=current_principal(request).name if request is not None else "",
        request_id=_request_id(request),
    )

    def settle(status: str, *, result: dict | None = None, note: str = "", risk: str = "") -> None:
        """落定这次执行（内存对象与磁盘一次改完）。

        落定失败**不改这次调用的结果**：动作已经做完（或没做），把一次成功的点击
        报成 503 是更严重的失真。记录会停在非终态——`GET /executions` 里看得见，
        启动恢复（v4.1 §六）还会把它收成 `UNKNOWN`，这比悄悄丢掉它好。
        """
        try:
            execution_service.settle(
                execution, status, result=result, note=note, risk=risk
            )
        except PersistenceError as exc:
            logger.warning(
                "执行记录落定失败（%s → %s）：%s", execution.execution_id, status, exc
            )

    with _execution_guard(execution, settle), device_access(
        session_item, operation=operation
    ) as device:
        # 第一轮：不带上下文。零成本，而且**不碰设备**——设备不可用时该给 403，
        # 而不是先观察失败、再给一个把责任推给设备的误导性 502。
        assessment = ActionRiskGate.assess(action)
        if assessment.requires_confirmation:
            _refuse_execution(execution, assessment, device=session_item.serial)
            raise HTTPException(status_code=403, detail=_dangerous_refusal(assessment))

        pre = observer.observe(controller, artifact_dir, step=0)

        # 第二轮：带 UI 树复核——抓「点击红色按钮」其实是「立即购买」这种情况。
        # 这是 V2.2 §一 的核心能力，而手工路径以前**完全没有**这一轮。
        assessment = ActionRiskGate.assess(
            action,
            context=RiskContext.from_observation(pre, instruction=MANUAL_ACTION_TASK_ID),
        )
        if assessment.requires_confirmation:
            _refuse_execution(execution, assessment, device=session_item.serial)
            raise HTTPException(status_code=403, detail=_dangerous_refusal(assessment))

        # 设备侧事实必须落盘（V3.2 §五）。这三步都是安全关键（会 fail-closed），
        # 写不进 durable store 就**不执行**——否则会出现「手机真的点了付款，
        # 但审计里查不到是谁点的」。手工路径以前一条事件都不留。
        #
        # v4.1 §四/§五：三步各自一个事务（状态迁移 + 它的事件同生共死），
        # 而设备调用在**所有这些事务之外**：
        #   RISK_CHECKED（判风险）→ DISPATCHED（记意图）→ RUNNING（交给设备）
        _dispatch_or_refuse(execution, action, assessment, session_item)

        result = executor.execute(device, action, pre.ui_tree)
        post = (
            observer.observe(controller, artifact_dir, step=0, suffix="post")
            if result.get("ok")
            else pre
        )
        post.action = action
        post.result = result
        verdict = verifier.verify_action(execution.execution_id, pre, action, post, result)
        post.status = verdict.outcome
        post.message = verdict.message

        execution_service.verified(
            execution,
            outcome=verdict.outcome.value,
            dispatch=verdict.dispatch.status.value,
            effect=verdict.effect.status.value,
            target=verdict.effect.target.value,
            message=verdict.message,
            device=session_item.serial,
        )

        # 落定状态（V4 §一）。三档而不是两档：
        #   FAILED     —— 设备层就没成（参数错、被设备拒绝……）
        #   SUCCEEDED  —— 验证层确认生效
        #   UNVERIFIED —— 发出去了但效果没被确认（含 dispatched / navigated / ui_changed）
        # 中间那一档是审核最关心的「已经点了但不知道成没成」——它绝不能被记成成功。
        if not result.get("ok"):
            settle(EXECUTION_FAILED, result=result, note=verdict.message)
        elif verdict.effect.status is ActionEffectStatus.VERIFIED_SUCCESS:
            settle(EXECUTION_SUCCEEDED, result=result, note=verdict.message)
        else:
            settle(EXECUTION_UNVERIFIED, result=result, note=verdict.message)

    return ManualActionOutcome(
        action=action,
        assessment=assessment,
        result=result,
        pre=pre,
        post=post,
        verdict=verdict,
        execution=execution,
    )


def _request_id(request: Request | None) -> str:
    """这次 HTTP 请求的 id：优先用调用方给的 `X-Request-Id`，没有就生成一个。

    为什么值得带上它：一次操作要能被**串起来**——审计日志（HTTP 层）、执行记录（动作层）、
    事件流（过程）三者用同一个 request_id / execution_id 就能对上。调用方自带
    `X-Request-Id` 时，还能把「上游一次操作触发的多个动作」串进同一条链。
    """
    if request is None:
        return ""
    header = (request.headers.get("x-request-id") or "").strip()
    return header or f"req_{uuid.uuid4().hex[:12]}"


def _start_execution_or_refuse(
    action: Action, session_item, *, principal: str = "", request_id: str = ""
) -> ActionExecution:
    """受理一条执行；写不进去就**拒绝执行**（V4 §一 / v4.1 §三）。

    与安全关键事件同一个口径：这条记录是「手机被操作过」的唯一凭据。
    写不进去还继续执行，就会造出「真的点了、但查不到是谁点的」。

    新记录的状态是 `CREATED`（v4.1 §五）——**还没判风险、更没碰设备**。
    V4 §一 那版一落盘就是 `RUNNING`，于是进程若死在这一刻，恢复时分不出
    「只是刚受理」和「动作已经交出去了」。
    """
    try:
        return execution_service.start(
            action=action,
            device_id=session_item.serial,
            principal=principal,
            request_id=request_id,
        )
    except PersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"执行记录无法落盘，已拒绝执行：{exc.reason}",
        ) from exc


def _refuse_execution(execution: ActionExecution, assessment, *, device: str = "") -> None:
    """门禁拒绝的留痕（`REFUSED`，v4.1 §五）。

    "被拒绝"与"执行失败"是两种事实：它回答的是「有没有过一次**没被记录的**点击尝试」。
    所以状态与依据都要落盘——而且这里**一次设备都没碰**。
    """
    try:
        execution_service.refuse(
            execution,
            risk=assessment.effective.value,
            note=assessment.describe(),
            action=execution.action.get("type", ""),
            effective=assessment.effective.value,
            policy=assessment.policy.value,
            model=assessment.model.value,
            downgrade_blocked=assessment.downgrade_blocked,
            reason=assessment.describe(),
            target_resolution=assessment.target_resolution,
            unresolved_target=assessment.unresolved_target,
            device=device,
        )
    except PersistenceError as exc:
        # 拒绝本身已经成立（调用方拿到的就是 403），留痕失败只降级为告警：
        # 为了「记不下来」而把 403 变成 503，是把安全结论换成了可用性问题。
        logger.warning("拒绝记录落盘失败（%s）：%s", execution.execution_id, exc)


def _dispatch_or_refuse(execution: ActionExecution, action: Action, assessment, session_item) -> None:
    """判风险 → 记意图 → 交给设备这三步（v4.1 §四），失败一律**不执行**。

    两条失败支路的区别要说清：

    - `PersistenceError`（写不进去）→ **503**：设备操作记录是「手机被操作过」的唯一
      凭据，写不进去就不该操作。而且事务已回滚，状态停在上一格——「读状态」就能看出
      这一步没留下。
    - 守卫落空（`dispatched` 返回 False）→ **409**：同一条执行已经派发过了。
      这时**绝不能**再去调设备——那正是「同一个 execution_id 点了两次」。
    """
    try:
        execution_service.assessed(
            execution,
            risk=assessment.effective.value,
            action=action.type.value,
            effective=assessment.effective.value,
            policy=assessment.policy.value,
            model=assessment.model.value,
            downgrade_blocked=assessment.downgrade_blocked,
            reason=assessment.describe(),
            target_resolution=assessment.target_resolution,
            unresolved_target=assessment.unresolved_target,
            device=session_item.serial,
        )
        dispatched = execution_service.dispatched(
            execution,
            risk=assessment.effective.value,
            action=action.type.value,
            fingerprint=action.fingerprint,
            target=str(action.target or ""),
            value=action.value,
            device=session_item.serial,
        )
        if not dispatched:
            raise HTTPException(status_code=409, detail="该执行已经派发过，拒绝重复执行")
        # `RUNNING` 在设备调用**之前**单独提交一次（v4.1 §五）：从这里往后，
        # 「手机有没有被操作过」不再由我们决定，崩溃恢复只能把它记成 UNKNOWN。
        execution_service.running(execution)
    except HTTPException:
        raise
    except PersistenceError as exc:
        try:
            execution_service.settle(
                execution, EXECUTION_FAILED, note=f"安全记录落盘失败，未执行：{exc.reason}"
            )
        except PersistenceError:
            pass  # 连落定都写不进去：记录会停在上一格，启动恢复会接管（v4.1 §六）
        raise HTTPException(
            status_code=503,
            detail=f"设备操作记录无法落盘，已拒绝执行：{exc.reason}",
        ) from exc


def _manual_ok_payload(outcome: ManualActionOutcome, session_item, extra: dict | None = None) -> dict:
    """成功响应。形状与升级前保持一致，只**追加**字段，不删也不改名。"""
    payload = {
        "ok": True,
        "device": session_item.serial,
        # V3.2 §八：generation 是「设备操作代次」，只记 Shadow 自己知道的写入，
        # **不是** Android 全局 UI 版本。别把它读成「页面没变过」。
        "generation": session_item.generation,
        "generation_meaning": GENERATION_MEANING,
        "risk": outcome.assessment.effective.value,
        "risk_detail": outcome.assessment.describe(),
        "verification": outcome.verdict.model_dump(),
        # V4 §一/§五：这次执行的身份。拿它去 `GET /executions/{id}` 能取回
        # 「谁 → 什么时候 → 哪台手机 → 什么动作 → 什么风险 → 结果如何」整条链。
        "execution_id": outcome.execution.execution_id,
        "execution_status": outcome.execution.status,
    }
    payload.update(extra or {})
    return payload


def _manual_failure_response(outcome: ManualActionOutcome) -> JSONResponse:
    """执行失败 → 502，形状与升级前 `AdbError` 的处理器一致。"""
    return JSONResponse(
        status_code=502,
        content={
            "ok": False,
            "error": outcome.result.get("error", "设备操作失败"),
            "execution_id": outcome.execution.execution_id,
            "execution_status": outcome.execution.status,
        },
    )


@app.post("/tap")
def tap(req: TapRequest, request: Request, device_serial: str | None = None):
    """单步点击。**与 Agent 走同一条风险门禁**（V3.2 §一）。

    升级前这里直接 `device.tap()`：不判风险、不进 HITL、不留设备侧审计。
    现在会先观察一次并带上下文复核风险——「点掉立即付款」会被 403 拦下，
    而不是静默执行。
    """
    session_item = resolve_manual_device(req.device_serial or device_serial, request)
    outcome = run_manual_action(
        Action(type=ActionType.TAP, target=Point(x=req.x, y=req.y)),
        session_item,
        operation="tap",
        request=request,
    )
    if not outcome.result.get("ok"):
        return _manual_failure_response(outcome)
    return _manual_ok_payload(outcome, session_item, {"x": req.x, "y": req.y})


@app.post("/text")
def text(req: TextRequest, request: Request, device_serial: str | None = None):
    """单步输入。走统一内核（V3.2 §一）。

    中文走 ADB Keyboard 广播、ASCII 走 `input text` —— 调用方不需要知道区别，
    这一步由 `executor` 的 TYPE 分支统一处理（与 Agent 的输入链路是同一条）。
    """
    session_item = resolve_manual_device(req.device_serial or device_serial, request)
    outcome = run_manual_action(
        Action(type=ActionType.TYPE, value=req.value),
        session_item,
        operation="type_text",
        request=request,
    )
    if not outcome.result.get("ok"):
        return _manual_failure_response(outcome)
    return _manual_ok_payload(
        outcome,
        session_item,
        {
            "text": req.value,
            "provider": outcome.result.get("provider"),
        },
    )


@app.post("/back")
def back(request: Request, device_serial: str | None = None):
    """单步返回。走统一内核（V3.2 §一）。"""
    session_item = resolve_manual_device(device_serial, request)
    outcome = run_manual_action(
        Action(type=ActionType.BACK),
        session_item,
        operation="back",
        request=request,
    )
    if not outcome.result.get("ok"):
        return _manual_failure_response(outcome)
    return _manual_ok_payload(outcome, session_item)


# `stable` 的语义常量（V2.4 §八）。审核指出这个名字容易被读成「UI 已经稳定」，
# 但它实际只表示「这次采集没有跨越 Shadow 自己发起的写入」——页面动画自己在播、
# 别的客户端在改，generation 都不会动。把这个含义写进响应，免得调用方自己猜。
STABLE_MEANING = "no_known_shadow_write_during_observation"

# V3.2 §八：同一个坑的第二次记录。`generation` 既不是 UI 版本、也不是设备版本，
# 它只回答「Shadow 自己在这段时间里有没有动过设备」。外部操作（用户手点、通知栏、
# App 异步刷新、另一个 adb client）**不会**推进它。
# 需要「决策依据的那一屏还是不是现在这一屏」时，用 runtime 的 `ObservationEpoch`
# （V3.1 P1-6），不要拿 generation 当替代品。
GENERATION_MEANING = "shadow_known_device_operations_only"


@app.post("/screenshot")
@app.get("/screenshot")
def screenshot(request: Request, device_serial: str | None = None):
    """只读端点：不加设备锁，但会明确告诉你这次截图是不是「无已知写入」（V2.2 §九）。

    `stable` 的准确含义是 **no known Shadow write during observation**：
    只说明这段时间里没有 Shadow 自己的写操作越过去，**不等于**「UI 已经完全静止」
    （V2.4 §八）。所以它适合回答「这张图能不能当权威状态用」，
    不适合回答「这一屏是不是已经不抖了」。

    只读性由 `device.adb.is_read_only` 结构化声明（V2.7 P1-7）：
    这里只调 `screenshot.capture` → `adb.screenshot`，属于只读封装，不改变设备状态。

    V3.2 §七：同时注册 **GET**。此前只有 POST，而只读令牌被限定为只能 GET，
    于是「只读令牌访问只读接口」反而拿到 403——一个纯粹的 API 契约不一致。
    两种方法都保留，POST 不动（老调用方不受影响），新调用方请用 GET。
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
        "generation_meaning": GENERATION_MEANING,
        # 前后代次不同 = 中途有 Shadow 的写操作发生，这一张可能落在动画/过渡帧上
        "stable": before == after,
        "stable_meaning": STABLE_MEANING,
    }


@app.post("/observe")
@app.get("/observe")
def observe(request: Request, device_serial: str | None = None):
    """只读端点，返回带代次的整屏观察。

    `stable=false` 表示这次观察跨越了一次 Shadow 自己的设备写入——消费者**不能**
    把它当成「当前稳定页面」，否则很容易把过渡动画页当成真实状态（V2.2 §九）。
    `stable=true` 也只是 **no known Shadow write during observation**，
    不代表 UI 已经静止（V2.4 §八）。

    只读性由 `device.adb.is_read_only` 结构化声明（V2.7 P1-7）：observer 只调
    截图 / `wm size` / `dumpsys` / `uiautomator dump`，都是只读采集，不改设备状态。

    V3.2 §七：同时注册 **GET**，理由同 `/screenshot`。
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
            "generation_meaning": GENERATION_MEANING,
            "stable": before == after,
            "stable_meaning": STABLE_MEANING,
        }
    )
    return payload


@app.post("/actions")
def execute_action(req: ActionRequest, request: Request, device_serial: str | None = None):
    """执行一个 Action。走与 `/tap` 等**同一个**内核（V3.2 §一 / §五）。"""
    try:
        action_type = ActionType(req.type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"未知 action_type: {req.type}")
    try:
        action = Action(type=action_type, target=req.target, value=req.value)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Action 参数非法: {exc}") from exc

    session_item = resolve_manual_device(req.device_serial or device_serial, request)
    # 动态分发（wait / done 这类无设备副作用的 action 也会进来）→ 不传 operation，
    # 走保守加锁。is_read_only 的结构化校验只对操作名确定的端点
    # （/tap、/text、/back）生效。
    outcome = run_manual_action(action, session_item, request=request)

    payload = outcome.post.model_dump(exclude={"ui_tree"})
    payload["verification"] = outcome.verdict.model_dump()
    payload["risk"] = outcome.assessment.effective.value
    payload["risk_detail"] = outcome.assessment.describe()
    payload["device"] = session_item.serial
    payload["generation"] = session_item.generation
    payload["generation_meaning"] = GENERATION_MEANING
    payload["execution_id"] = outcome.execution.execution_id
    payload["execution_status"] = outcome.execution.status
    return payload


@app.get("/executions")
def list_executions(request: Request, limit: int = 50):
    """最近的执行记录，新的在前（V4 §一/§五）。

    手工端点每次调用都有一条——**被拒绝的也有一条**（`REFUSED`），
    因为「谁想点付款、被判成危险、被拒」正是要能回答的问题。

    设备范围会裁剪：受限令牌只能看到自己被授权设备上的执行（与 `/tasks` 同口径）。
    """
    principal = current_principal(request)
    window = max(1, min(limit, 200))
    records = [
        record
        for record in execution_store.recent(limit=window)
        if principal.may_use_device(record.device_id)
    ]
    return {"ok": True, "executions": [record.to_dict() for record in records]}


@app.get("/executions/{execution_id}")
def get_execution(execution_id: str, request: Request):
    """一次执行的完整链条：身份与结果 + 它的事件流（V4 §五）。

    审核要的那条链——**谁 → 什么时候 → 哪台手机 → 执行了什么 → 什么风险 → 为什么允许 →
    结果如何**——由三份事实拼成：

    - 执行记录（`ExecutionStore`）：身份、风险、结果；
    - 事件流（`EventLog`，按 `execution_id` 存）：过程中每一步的判定与留痕；
    - 审计日志（HTTP 层，按 `request_id` 关联）：谁调了哪个接口、被拒的请求也留痕。

    设备范围外的执行统一 404——与损坏任务同口径，不泄露存在性。
    """
    principal = current_principal(request)
    record = execution_store.load(execution_id)
    if record is None or not principal.may_use_device(record.device_id):
        raise HTTPException(status_code=404, detail="执行记录不存在")
    return {
        "ok": True,
        "execution": record.to_dict(),
        "events": [event.to_dict() for event in event_log.read(execution_id, limit=200)],
    }


# ---- 任务端点 ----


def _wait_for(task_id: str, timeout: float) -> dict:
    """`wait=true` 时的同步等待。

    V4 §8：从「`time.sleep` 轮询」改成**事件驱动**——进程内用调度器的条件变量阻塞，
    任务到终态时 worker `notify_all`，等待者被唤醒，不再每 100ms 空转一次。

    **跨进程仍然要兜底**（与 V3.3 §四 同一件事）：任务可能被另一个进程的 worker 推进，
    进程内的条件变量收不到那种进度。所以这里在「进程内事件等待」之外，保留一个
    较慢的「回查磁盘」节拍——`scheduler.wait_terminal` 返回 None（超时或进程内没找到）
    时，用 `manager.freshest` 再确认一次；只要磁盘已经终态就返回，不空等到超时。

    两条腿的分工：进程内靠事件（快、省 CPU）、跨进程靠 `freshest`（正确、可兜底）。
    """
    deadline = time.monotonic() + timeout
    while True:
        # 事件驱动：进程内等通知（最省事的那条腿）
        terminal = scheduler.wait_terminal(task_id, timeout=min(0.5, max(0.01, deadline - time.monotonic())))
        if terminal is not None:
            return terminal.model_dump(mode="json")

        # 跨进程兜底：查最新事实（V3.3 §四）
        task = manager.freshest(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.is_terminal:
            return task.model_dump(mode="json")
        if time.monotonic() >= deadline:
            break

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

    # V4 §三：整段放进**一个事务**——「预占票据 → 改 Task 状态 → 写 CONFIRMED 事件 →
    # 作废票据」要么全成功、要么全回滚。审核点出的两种跨存储状态因此都不可能出现：
    #
    #     Task 已确认 / Token 未消费      → 不可能（同一事务）
    #     Token 已消费 / Task 没确认      → 不可能（同上）
    #
    # 内层会**加入**这个事务而不是各开一个（`Database.transaction()` 可重入）：
    # `manager.resolve_confirmation` 里最终落到 `TaskStore.save`，写 `CONFIRMED`
    # 又是安全关键事件（fail-closed）——写不下就整笔回滚，正是我们要的。
    #
    # 409 那条路径也不再需要显式 `release`：**回滚本身就把预占一起撤销**。
    # （`release` 仍然保留给事务之外的调用方，见 `storage/confirmation_store.py`。）
    with db.transaction():
        if auth.enabled():
            pending = runtime.pending_confirmation(task_id)
            # 没有 pending 动作时，指纹取确认类型（goal / recovery）——它们同样要绑进令牌，
            # 否则「完成裁定令牌」和「崩溃恢复放行令牌」可以互换（V2.6 §七）
            fingerprint = (
                pending.fingerprint
                if pending is not None
                else manager.confirmation_kind(task_id)
            )
            # V3.3 §六 的两阶段：先**预占**（不作废），业务状态改成功之后才 commit。
            # V4 §三 起这两步与「改状态」共处一个事务，中途失败由回滚兜底。
            ok, reason, jti = auth.reserve_confirmation_token(
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
                        f"确认未通过（{reason}）：请先 POST /tasks/{{id}}/confirmation-token "
                        "申请令牌，再把返回的 token 传到这里"
                    ),
                )
        else:
            jti = ""

        # 状态写入全部收口到 TaskManager（V2.2 §九 / §十）：API 不再自己 task.mark(...)
        resolved = manager.resolve_confirmation(task_id, approved=req.approved)
        if resolved is None:
            raise HTTPException(status_code=409, detail="该任务当前没有可处理的待确认事项")

        if jti and not auth.commit_confirmation_token(jti):
            # 同一个事务里「状态改了但票据没作废」在逻辑上不该发生；真出现就留痕，
            # 事务会照常提交（因为业务状态确实变了，回滚它反而更糟）。
            logger.warning(
                "确认令牌预占提交失败（同一事务内）：jti=%s task=%s", jti, task_id
            )

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
@app.get("/healthz")
def health():
    """探活端点：**只回 `{"ok": true}`**（V3.3 §九）。

    它是唯一不需要鉴权的入口——运维探活不该还要带密钥（容器探针、负载均衡、
    systemd watchdog 都只该问「活着吗」）。所以它**不能**顺带回答别的：

    - `auth`：等于告诉匿名者「这个实例有没有开鉴权」，也就告诉他值不值得试；
    - `host` / `device_backend`：暴露部署形态（ADB 还是手机本机端点）；
    - `principals_error`：暴露「配置坏了、此刻所有请求都 401」。

    详细诊断搬到 `GET /health/detail`，**需要鉴权**。拆分的理由是读者不同：
    探活的是机器，诊断的是运维本人。
    """
    return {"ok": True}


@app.get("/health/detail")
def health_detail():
    """诊断端点（需要鉴权）：把「我们到底跑成什么样子」一次说清楚。

    V3.3 §九 从这里拆出来的——以前这些字段挂在匿名 `/health` 上。

    - `confirmation_consumption`：确认令牌消费记录的后端类名（V3.2 §二）。
      **只有它不是 `InMemoryConsumption` 时**，「一次性 Token」才跨重启成立。
      把名字暴露出来，是因为「我们到底配的是哪个实现」应该能被查到，
      而不是靠人记住几个月前启动时设了什么环境变量。
    - `device_backend`（V3.3 §1）：一句就能确认「现在是谁在控制设备」——
      手机上部署时最常问的就是「跑的是 ADB 后端还是本机后端」，不该靠翻日志。
    - `principals_error`：非 null 说明 `SHADOW_API_PRINCIPALS` 配错了，
      此刻**所有请求都在被 401**。必须能查到——否则运维只看到「全部 401」，
      原因却只在一行日志里。
    """
    return {
        "ok": True,
        "auth": "token" if auth.enabled() else "disabled",
        "host": HOST,
        "confirmation_consumption": auth.consumption_backend(),
        "device_backend": describe_backend(resolve_device_serial()),
        "principals_error": auth.config_error(),
    }


if __name__ == "__main__":
    import uvicorn

    refusal = auth.bare_bind_refused(HOST)
    if refusal:
        raise SystemExit(refusal)
    # 配错了 principals 就**不要启动**：否则服务会安静地 401 掉所有请求，
    # 而「为什么全都 401」要翻日志才知道（V3.2 §六）。
    if auth.config_error():
        raise SystemExit(
            "SHADOW_API_PRINCIPALS 配置有误，拒绝启动：\n  "
            + str(auth.config_error())
        )
    if not auth.enabled():
        logger.warning(
            "未配置 SHADOW_API_TOKEN，API 无鉴权（仅建议在 127.0.0.1 本机使用）"
        )
    uvicorn.run(app, host=HOST, port=PORT)
