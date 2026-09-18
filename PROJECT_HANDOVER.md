# Shadow 项目技术交接文档

## 一、项目概述

### 1.1 项目名称与定位

| 项 | 内容 | 依据 |
|---|---|---|
| 项目名 | Bluewhale Shadow Phone（仓库目录 `Shadow`） | `pyproject.toml` 的 `name = "bluewhale-shadow-phone"` |
| HTTP 服务标识 | `BlueWhale Shadow Phone Agent`，版本 `0.3.2` | `api/server.py` `FastAPI(title=..., version="0.3.2")` |
| 一句话定位 | **面向 Android 的、以「长任务执行可靠性」为核心的任务级 Agent Runtime** | `README.md` 首段 |
| 不是什么 | 不是「LLM → click() → 结束」的一发式脚本；**不是微服务**；**不是** RAG/知识库应用；**没有** MCP Server | 全仓库无相关依赖与模块（见 3.4） |

### 1.2 业务目标与要解决的问题

Shadow 要解决的问题是：**让一个 LLM 驱动的 Agent 可靠地在真手机上完成多步骤任务**。它的技术赌注是
「决定上限的不是 VLM 而是任务基建」——`README.md` 原文：

> **决定上限的不是 VLM，而是 `TaskManager + Scheduler + Checkpoint + Runtime`**：
> VLM 决定「下一步点哪里」，这四件套决定「多件事怎么排队、被打断怎么接着做、崩溃怎么不重蹈覆辙」。

因此仓库的主要代码量不在 prompt 与模型调用上（`vision/vlm.py` 674 行），而在**状态机、调度、恢复、
风险门禁、多级证据**上（`agent/` 8,389 行 + `models/` 3,272 行 + `storage/` 2,658 行）。

### 1.3 目标用户与典型使用场景

| 用户 | 场景 |
|---|---|
| 开发者 / 研究者 | 在 PC 上用 ADB 控制模拟器或真机，跑任务、看事件流、回放失败任务（默认 `SHADOW_DEVICE_BACKEND=adb`） |
| 产品形态使用者 | 手机自身作为 Agent 主机，装一个「设备端点」APK，Core 跑在 PC/局域网（`SHADOW_DEVICE_BACKEND=android`） |
| 演示 / 答辩 | 跑两个**完全离线**的脚本 `scripts/demo_preemption.py`、`scripts/replay_task.py`（不需要 adb / 模拟器 / API Key） |

### 1.4 项目当前状态（**这一节请务必读完再动手**）

仓库自己在 `README.md` 给出了一份**诚实的定位声明**，这份声明与代码一致（已逐条核对）：

> **到目前为止做到的是**：面向长任务的任务调度、抢占恢复、用户活动感知与执行平面抽象——
> 用户一碰手机，Agent 会在安全点落检查点并暂停，用户停手后自动恢复，
> 且**永远不会**和用户在同一个屏幕上抢动作。
>
> **还没有做到的是**：真正独立的 Shadow Display，因此第三方 App 的
> **后台并行执行仍处于架构预留阶段**（不是「已实现」）。

代码层面的对应证据：

- `device/controller.py:shadow_plane_available()` **缺省返回 `None`**（不知道），
  注释明确写「当前的真实现状是 `False`，这是**如实**而不是保守」。
- Android 侧 `ShadowDisplayManager.kt` 是骨架，如实返回 `available=false`。
- 所有 `shadow_*` 动作方法（`shadow_tap` / `shadow_swipe` / …）默认实现**一律抛
  `ShadowActionUnsupported`**，**绝不回落到前台同名方法**。

**已完成并经过实测的部分**：

| 能力 | 状态 | 依据 |
|---|---|---|
| 任务状态机 + 合法迁移表 | ✅ 已实现 | `models/task.py` |
| 动作级执行状态机（含 `UNKNOWN` 禁止重试） | ✅ 已实现 | `models/execution.py`、`agent/execution/state.py` |
| 崩溃恢复（先确认再执行） | ✅ 已实现 | `agent/_recovery.py`、`agent/execution/recovery.py` |
| 危险动作门禁 + 一次性人工确认票据 | ✅ 已实现 | `agent/risk_gate.py`、`api/auth.py` |
| 多级证据验证（不只看 VLM） | ✅ 已实现 | `agent/evidence.py`、`agent/verifier.py` |
| 多设备调度（每设备一条 lane） | ✅ 已实现 | `agent/scheduler.py`、`device/pool.py` |
| 两条设备后端（ADB / Android）可互换 | ✅ 已实现 | `device/factory.py` |
| 用户活动感知 + 安全点让位 | ✅ 已实现 | `device/user_activity.py`、`agent/runtime.py:safe_point()` |
| 真机装机与启动 | ✅ 已实测 | `android/README.md`「已知限制 6」：vivo V2352A / Android 16 装机启动通过 |
| **真机交互行为**（手势坐标 / 投屏帧率 / 后台存活） | ❌ **未验证** | 同上，「仍未验证：交互行为」 |
| **真影子平面**（独立 VirtualDisplay 上可操作） | ❌ **未实现**（架构预留） | `ShadowDisplayManager.kt` 返回 `available=false` |
| Docker / CI/CD / K8s 部署 | ❌ **不存在** | 全仓库扫描无 `Dockerfile`/`docker-compose`/`.github/`/`k8s/` |

**当前测试基线**：`pytest` → **799 passed**（本次交接实测，38.69 秒，全离线）。

---

## 二、项目当前状态

### 2.1 版本演进线

版本号可在 `bluewhale-shadow-phone/`（开发文档目录，**不入库**）与代码注释中追溯：

| 版本 | 主题 | 代码落点 |
|---|---|---|
| V1 | 最小闭环 | `agent/runtime.py` 雏形 |
| V2.x | 任务管理 / 并发 / 多设备 | `agent/task_manager.py`、`agent/scheduler.py`、`device/pool.py` |
| V3 | 正确性工程（门禁、令牌、TOCTOU、证据缺口） | `agent/risk_gate.py`、`api/auth.py`、`vision/target.py` |
| V3.1 | 保守化（未知语义抬级、注释不超实现） | `agent/risk_gate.py` |
| V3.2 | 门禁 / 令牌 / 配置 fail-closed | `api/auth.py`、`api/server.py` |
| V3.3 | 手机部署（设备端口 + Android 后端） | `device/controller.py`、`device/factory.py`、`android/` |
| V4 | 存储换代：单一 `shadow.db` | `storage/database.py`、`storage/migrations/`（`user_version = 4`） |
| V4.1 | 动作级执行状态机 | `agent/execution/{state,service,recovery}.py`、`storage/execution_store.py` |
| V4.2 | 内容风险 + 拆出 `_recovery.py` | `agent/risk_gate.py`、`agent/_recovery.py` |
| V4.3–4.5 | 计划层（`TaskStep` 带状态） | `models/task_step.py`、`models/task_plan.py` |
| **V5** | **执行平面（foreground / shadow / hybrid）** | `models/execution_mode.py`、`device/session.py`、`agent/runtime.py:safe_point()` |

### 2.2 代码规模（2026-09-17 实测，**不要引用记忆里的旧值**）

| 部分 | 文件数 | 行数 |
|---|---|---|
| `agent/` | 26 | 8,389 |
| `api/` | 3 | 2,414 |
| `device/` | 13 | 3,830 |
| `models/` | 17 | 3,272 |
| `storage/` | 12 | 2,658 |
| `vision/` | 6 | 1,273 |
| `scripts/` | 5 | 845 |
| **Python 运行时合计** | **82** | **22,681** |
| `tests/`（**不入库**，见 `.gitignore`） | 38 | 17,543 |
| `android/` Kotlin | 16（14 main + 2 test） | 2,922 |

其他实测值：APK **796.6 KB**（`android/README.md` 记 793 KB，构建间有浮动）、Kotlin 真编译
**34 个 class** + **15 条 JVM 单测**。

### 2.3 交接时最需要知道的三件事

1. **`tests/` 和 `docs/`、`DEVLOG.md`、`bluewhale-shadow-phone/` 都在 `.gitignore` 里，不进仓库。**
   换机器 clone 下来后**没有测试**。取回方式：`git checkout <commit> -- tests`
   （见 `.gitignore` 注释）。这意味着**你接手的第一件事就是把 tests 恢复出来并跑通**，
   否则你改任何一行都没有安全网。
2. **单进程是硬前提。** `api/server.py:_guard_single_process()` 在检测到
   `WEB_CONCURRENCY` / `UVICORN_WORKERS` / `GUNICORN_WORKERS` > 1 时**直接拒绝启动**。
   这不是「建议」，是启动期硬闸。原因见 4.4。
3. **`bluewhale-shadow-phone/*.md`（29 份）是审核意见与方案文档，不是实现说明。**
   它们记录的是**要求**，代码里可能只落了一部分。读它们时必须与代码交叉验证——
   本仓库有明确的先例：设计稿里的「计划实现」被误读成「已实现」。

---

## 三、技术栈

全部依据 `requirements.txt` / `requirements-dev.txt` / `pyproject.toml` / `android/build.gradle.kts`。

### 3.1 后端运行时

| 技术 | 版本 | 用途 | 依据 |
|---|---|---|---|
| Python | `>=3.11` | 运行时 | `pyproject.toml:requires-python` |
| FastAPI | `==0.141.1` | HTTP 契约 | `requirements.txt`（**钉死版本**） |
| uvicorn[standard] | `==0.52.4` | ASGI 服务器 | 同上 |
| pydantic | `==2.13.5` | 全部领域模型（`Task`/`Action`/`Checkpoint`/`Budget`…） | 同上 |
| httpx | `==0.28.1` | 调用 VLM 的 OpenAI 兼容接口 | 同上 |
| pytest | `==9.1.1` | 测试（**dev 依赖，单独文件**） | `requirements-dev.txt` |
| SQLite | 标准库 `sqlite3` | 持久化（WAL + `synchronous=FULL` + `busy_timeout=5000`） | `storage/database.py` |

> `requirements.txt` 头部注释写明「从 `>=` 改成 `==`」的理由：**这个仓库跑的是一个会操作真实手机的
> Agent，「上周还能跑、这周复现不出来」的成本远高于手动升级的成本。**
> 完整依赖图（含传递依赖）锁在 `uv.lock`，可用 `uv sync` 装出一模一样的环境。

### 3.2 被显式打包的包

`pyproject.toml`：`[tool.setuptools] packages = ["agent","api","device","models","vision"]`。
**注意 `storage/` 不在里面**——【代码推断】这是遗漏，`api/server.py` 明确
`from storage import ...`，若真的走 `pip install .` 会缺包。当前所有人都是「源码目录里直接跑」，
所以没暴露。**建议修复。**

### 3.3 Android 侧工具链

| 技术 | 版本 / 位置 | 说明 |
|---|---|---|
| Kotlin | kotlinc（Maven Central 下载） | **零 androidx 依赖**，只用 `android.*` / `java.*` / `org.json` |
| JDK | 17+（本机用 PyCharm 自带 `jbr`） | 可由 `SHADOW_JAVA` 覆盖 |
| `android.jar` | `platform-35`（dl.google.com） | 与 `compileSdk = 35` 一致 |
| build-tools | r35（含 `aapt2`/`d8`/`zipalign`/`apksigner`/`dexdump`） | 约 60 MB |
| Gradle（可选） | `build.gradle.kts`，AGP 8.9.1 | **有 SDK 时**才用；仓库**无 gradle wrapper** |

**关键事实**：本工程**不需要 Android Studio / Android SDK / Gradle 也能真编译 + 打 APK**，
靠 `android/tools/` 下三个脚本手工走
`aapt2 → javac → kotlinc → d8 → zipalign → apksigner`。

### 3.4 明确**没有**使用的技术（避免脑补）

| 常见架构组件 | 本项目 | 依据 |
|---|---|---|
| Redis / Memcached | ❌ 无 | `requirements.txt` 无；无相关模块 |
| 消息队列（Kafka/RabbitMQ/Celery） | ❌ 无 | 任务队列是**进程内的 `queue.PriorityQueue`**（`agent/scheduler.py:_DeviceLane`） |
| PostgreSQL / MySQL | ❌ 无 | 存储是 SQLite |
| 向量数据库 / embedding | ❌ 无 | `vision/` 只有 VLM 与解析，无 embedding 调用 |
| RAG / 知识库 | ❌ 无 | 无检索链、无 chunk、无 rerank |
| MCP Server / MCP Tool | ❌ 无 | 全仓库无 MCP 相关代码 |
| LangChain / LangGraph / LlamaIndex | ❌ 无 | `requirements.txt` 无 |
| Docker / Kubernetes / Helm | ❌ 无 | 全仓库扫描无相关文件 |
| CI/CD（GitHub Actions / GitLab CI / Jenkins） | ❌ 无 | 无 `.github/`、`.gitlab-ci.yml`、`Jenkinsfile` |
| Nginx / 网关 | ❌ 无 | 无配置文件 |

---

## 四、系统整体架构

### 4.1 分层与职责边界

```
                    FastAPI (api/server.py, 1785 行)
                     │  HTTP 契约 + 统一访问控制 + 审计
                     ▼
              TaskManager (agent/task_manager.py, 596 行)
                     │  生命周期 / 指令注入 / 唯一状态写入口
                     ▼
             TaskClassifier (agent/classifier.py, 380 行)
                     │  规则 + 相似度 + LLM 三层关系判定
                     ▼
              TaskScheduler (agent/scheduler.py, 1206 行)
                     │  排队 / 优先级 / 抢占 / 恢复（每设备一条 lane）
                     ▼
              AgentRuntime (agent/runtime.py + 5 个 mixin)
                     │  Observe → Think → Act → Verify → Checkpoint
        ┌────────────┴────────────┐
        ▼                         ▼
   Vision / Grounding      DeviceController（**协议 / 端口**）
   (vision/)                       │
                     ┌─────────────┴─────────────┐
                     ▼                           ▼
             ADB 后端（PC 控制）        Android 后端（手机自控）
             device/adb.py              device/android.py
                                        └→ device/remote.py（HTTP 桥）
```

**职责边界（都写进了代码注释，改动时必须遵守）**：

| 模块 | 回答的问题 |
|---|---|
| `scheduler` | **谁用设备**（不懂页面） |
| `task_manager` | 任务生命周期 + 指令注入 |
| `classifier` | 两条指令是什么关系 |
| `runtime` | **怎么完成一个任务** |
| `planner` | 下一步做什么（**不产坐标**） |
| `executor` | 怎么操作设备（**不抛异常**） |
| `verifier` | 这步成没成（**不改状态**） |
| `risk_gate` | 这个动作有多危险（`assess` 唯一入口） |
| `goal_verifier` | 任务目标达成没有（**独立证据裁定**） |
| `device/pool` | 设备注册表 |
| `storage/lease_store` | 跨进程唯一执行者（SQLite 租约） |

### 4.2 真实请求流（异步任务，默认）

以 `POST /tasks` 为例，**逐段对应代码**：

```
客户端
  │ POST /tasks {"instruction":"打开设置"}
  ▼
api/server.py: create_task()
  ├─ current_principal(request)            → 解析令牌 → Principal
  ├─ allowed_devices_for(request)          → 该身份可用设备集合
  ├─ require_device_access(serial, ...)    → 显式指定的设备要授权
  ▼
TaskManager.create()
  ├─ 构造 Task（含 budget / priority / device_serial / execution_mode）
  ├─ TaskStore.save(task)                  → shadow.db tasks 表（revision+1）
  └─ scheduler.submit(task)                → 排入对应设备的 lane 队列
  ▼
TaskScheduler._lane_loop()（每设备一个 worker 线程）
  ├─ _pop_next()                           → 按优先级取下一个任务
  ├─ _maybe_preempt()                      → 需要时请求当前任务让出设备
  └─ _execute(task)
       ├─ session.acquire(task.id)         → 拿到该设备的锁
       └─ runtime.run(task)                → 进入执行循环
  ▼
ExecutionMixin.run() → _run_loop()   ← 核心循环
  ├─ refresh_user_context()           → 问设备「用户在不在用手机」
  ├─ safe_point()                     → 安全点判定（5 步，见 6.2）
  ├─ _observe()                       → 截图 + dump UI 树 + 焦点
  ├─ _think() → planner → vision.vlm.decide_next_action()
  ├─ ActionRiskGate.assess()          → 风险判定（见 6.5）
  ├─ _toctou_guard()                  → 执行前复查页面是否还是决策时那一屏
  ├─ _execute() → executor.execute() → DeviceController.tap()/swipe()/...
  ├─ _verify()                        → 多级证据验证
  └─ _save_checkpoint()               → 落检查点（供恢复）
  ▼
HTTP 响应 {"mode":"background", ...task}
（客户端随后 GET /tasks/{id} 轮询）
```

**同步模式**（`"wait": true`）：`create_task` 走 `_wait_for(task.id, timeout)` →
`scheduler.wait_terminal()` 轮询到终态，响应带 `{"mode":"sync"}`。
**默认不同步的理由写在代码里**：「同步等一整个 loop 会长期占用 worker 线程」。

### 4.3 单进程硬前提（**必读**）

```python
def _guard_single_process() -> None:
    raw = (os.getenv("WEB_CONCURRENCY", "") or os.getenv("UVICORN_WORKERS", "")
           or os.getenv("GUNICORN_WORKERS", "")).strip()
    ...
    raise RuntimeError(
        f"检测到 {workers} 个 worker（...），但 Shadow 目前只支持单进程："
        "TaskStore 的 revision CAS 与写锁都只在进程内有效，"
        "多 worker 下任务文档会互相覆盖（跨进程 TaskLease 只保证不双执行，不保证不丢写）。"
        "请用 --workers 1 启动；若确有需要并已自行承担状态一致性风险，"
        "可显式设置 SHADOW_ALLOW_MULTI_PROCESS=1 跳过本检查。"
    )
_guard_single_process()   # ← 注意：在**模块导入期**执行
```

**技术原因**：`TaskStore` 的 revision CAS 与 `TaskManager._mutation_lock` 都是**进程内**机制。
跨进程的「一个任务同一时刻最多一个执行者」由 SQLite `TaskLease` 兜住
（`storage/lease_store.py`，用独立的 `lease.db`），但那只保证**不双执行**，
保证不了**任务文档不被互相覆盖**。

### 4.4 存储边界（诚实说明）

`Database.transaction()` 回滚的是**存储**；`Runtime` / `Scheduler` 持有的内存对象**不受它管辖**。
这不是遗漏，而是「single-writer」这件事还没做完的部分——`agent/task_manager.py` 有一段长篇注释
把「已经是的」和「还不是的」分开写清楚了，**不要把它读成「已完成」**。

另外：`SHADOW_CONFIRM_DB` 指向**另一个文件**时，跨库事务不成立，
`/confirm` 的原子性（校验票据 + 改任务状态 + 写事件 + 作废票据）只在默认布局下成立。

### 4.5 执行平面（V5 新增的一层概念）

| 概念 | 回答的问题 |
|---|---|
| `ExecutionMode`（`foreground`/`shadow`/`hybrid`） | 这个任务**声明**跑在哪个平面 |
| `TaskPlane` / `resolve_task_plane()` | 这个任务**实际**跑在哪个平面（含降级与原因） |
| `ExecutionSession` / `ShadowSession` | 这次执行**实际**落在哪块屏幕上 |
| `ShadowActionRouter` | 这一步动作往哪块屏幕发 |
| `UserContext` | 用户此刻在不在用手机 |
| `ExecutionTarget` | 执行到这一步时「该在哪执行」 |

**「声明」与「实际」必须分开，这是本层最要紧的一条。**
用户提交 `execution_mode=hybrid` 是**声明**；影子不可用时它**实际**会落到前台。
把两者混在一起曾导致两个任务都以为自己独占屏幕——真机表现是两串点击互相打断、且都不报错。
现在全链路只认一个入口 `resolve_task_plane()`：调度器的抢占判定、Runtime 的用户让位判定、
Checkpoint 落盘读的都是它，`task.execution_mode` 只作为**输入**。

影子可用性判定是**三态**（`True`/`False`/`None`），`None` 与 `False` 同判不可用
（但 `reason` 里写明是「未知」而非「不可用」）。
**该判定跑在设备锁内 → 必须零 I/O**（`device/controller.py:shadow_plane_available()` 的实现约束）。

---

## 五、项目目录结构

### 5.1 顶层目录

| 目录 / 文件 | 作用 | 能不能改 | 说明 |
|---|---|---|---|
| `agent/` | Agent 运行时、调度、规划、验证、风险门禁 | ⚠️ 核心，改动需极度谨慎 | 26 文件 / 8,389 行 |
| `api/` | HTTP 契约与鉴权 | ⚠️ 契约层，改接口要同步改调用方 | `server.py` 1,785 + `auth.py` 628 |
| `device/` | 设备抽象（端口 + 两个后端 + 会话） | ⚠️ 端口协议被测试钉住 | 13 文件 / 3,830 行 |
| `models/` | 领域模型与状态机（**纯数据，无行为**） | ⚠️ 改枚举/迁移表会牵动全仓库 | 17 文件 / 3,272 行 |
| `storage/` | SQLite 持久化与迁移 | ⚠️ 改 schema 必须写迁移 | 12 文件 / 2,658 行 |
| `vision/` | UI 树解析、目标定位、指纹、VLM 调用 | ⚠️ VLM prompt 是核心资产 | 6 文件 / 1,273 行 |
| `scripts/` | 演示与运维脚本 | ✅ 可改 | `demo_preemption` / `replay_task` 是**门面**，随仓库发布 |
| `tests/` | 测试（38 文件 / 799 用例） | ✅ **但不在仓库里** | `.gitignore` 排除，需 `git checkout` 取回 |
| `android/` | Kotlin 设备层 + 无 SDK 构建工具链 | ⚠️ 见 5.3 | 16 Kotlin 文件 |
| `artifacts/` | 运行产物（截图 / 数据库 / 状态） | ✅ 可删 | `.gitignore` 排除；含**演示环境变量文件**（见 11.5） |
| `docs/` | 交付文档（4 份中文 md） | 📄 文档 | **`.gitignore` 排除** |
| `bluewhale-shadow-phone/` | 29 份审核意见 / 方案文档 + 1 个 HTML | 📄 文档 | **`.gitignore` 排除**；读它必须与代码交叉验证 |
| `DEVLOG.md` | 开发历程（2,270 行） | 📄 文档 | **`.gitignore` 排除** |
| `_ref_extract/` | 参考资料抽取副本 | 📄 参考 | `.gitignore` 排除 |
| `.workbuddy/` | 工作区数据（记忆、会话） | ⚠️ 不要删 | 存项目长期笔记 |
| `pyproject.toml` | 包名 / Python 版本 / 依赖 / 打包配置 | ⚠️ | |
| `requirements.txt` / `requirements-dev.txt` / `uv.lock` | 依赖锁定 | ⚠️ | 版本钉死 |
| `README.md` | 对外说明（**定位声明的权威来源**） | 📄 | |
| `.gitignore` | 排除规则 | ⚠️ | 改它会改变仓库发布边界 |

### 5.2 配置 / 业务 / 基础设施 / 测试 的划分

- **配置**：全部走**环境变量**，无 `.env` 文件、无 `.env.example`、无 YAML 配置。
  读取点分散在 `api/server.py`、`api/auth.py`、`vision/vlm.py`、`device/*`、`agent/*`、
  `android/tools/_toolchain.py`。完整清单见第十一章。
- **业务**：`agent/` + `models/` + `vision/`。
- **基础设施**：`storage/`（持久化）、`device/`（设备）、`api/`（HTTP）。
- **测试**：`tests/`（**不在仓库**）。

### 5.3 `android/` 目录

```
android/
├── .gitignore
├── README.md                      ← **必读**：两条路线、权限、排障、无 SDK 编译
├── build.gradle.kts / settings.gradle.kts / gradle.properties
├── app/build.gradle.kts
├── app/src/main/AndroidManifest.xml
├── app/src/main/java/com/bluewhale/shadow/
│   ├── MainActivity.kt                    权限引导 + 端点控制 + 连接信息
│   ├── device/
│   │   ├── UiNodeAdapter.kt               UI 树最小抽象（让序列化器可被 JVM 单测）
│   │   ├── UiTreeSerializer.kt            → uiautomator 同构 XML（**最不能错的一处**）
│   │   ├── NodeInfoAdapter.kt             AccessibilityNodeInfo → UiNodeAdapter
│   │   ├── ShadowAccessibilityService.kt  树 / 手势 / 全局动作 / ACTION_SET_TEXT
│   │   ├── ScreenCapture.kt               MediaProjection 截图
│   │   ├── AppLauncher.kt                 PackageManager + Intent 启动
│   │   ├── AndroidBridgeImpl.kt           12 个协议方法
│   │   ├── ShadowDisplayManager.kt        影子显示（**骨架，如实返回不可用**）
│   │   ├── UserActivityMonitor.kt         用户活动探测
│   │   ├── AgentActionScope.kt            动作作用域计数器（V5 修复轮）
│   │   └── DeviceErrors.kt                两类失败：权限没给 / 这次没成功
│   └── endpoint/
│       ├── BridgeHttpServer.kt            极小的 HTTP 端点（ServerSocket，零依赖）
│       └── DeviceEndpointService.kt       前台服务
├── app/src/main/res/…                     资源
├── app/src/test/java/…                    2 个 JVM 单测
└── tools/
    ├── _toolchain.py                      JDK / android.jar / build-tools 定位与下载
    ├── verify_kotlin_compile.py           无 SDK 环境下真编译 + JVM 单测
    └── build_apk.py                       无 SDK 环境下打出可安装 debug APK
```

**`android/` 里最不能改坏的一条约定**：UI 树必须与 `uiautomator dump` 同构。守住它，
Python 侧的 `vision/parser.py` / `vision/target.py` / `vision/grounding.py` /
`agent/evidence.py` / `agent/risk_gate.py` **一行都不用改**；守不住就得再写一整套
Android 专用解析与坐标映射。

### 5.4 Android 桥协议（12 个能力）

方法名就是协议的一部分（同进程按名字反射、远程映射成 `/bridge/<name>`），
由 `tests/test_android_bridge_contract.py` 钉住。

| 方法 | 参数 | 返回 | 手机侧实现 |
|---|---|---|---|
| `screen_size` | — | `[w, h]` | 投屏尺寸，未授权时退回 `WindowMetrics` |
| `current_focus` | — | `[package, activity]` | 最近一次 `TYPE_WINDOW_STATE_CHANGED` |
| `dump_ui` | — | uiautomator 同构 XML | `AccessibilityNodeInfo` 树序列化 |
| `screenshot_bytes` | — | PNG 字节 | `MediaProjection` + `ImageReader` |
| `state` | — | `"device"` 等 | 两个权限是否都到位 |
| `tap` | `x, y` | — | `dispatchGesture` |
| `long_press` | `x, y, duration_ms` | — | `dispatchGesture` |
| `swipe` | `x1,y1,x2,y2,duration_ms` | — | `dispatchGesture` |
| `set_text` | `value` | — | 焦点节点 `ACTION_SET_TEXT` |
| `press_back` | — | — | `GLOBAL_ACTION_BACK` |
| `press_home` | — | — | `GLOBAL_ACTION_HOME` |
| `launch` | `package, activity` | — | `PackageManager` + `startActivity` |

`resource-id` 有值的前提是辅助功能配置打开了 `flagReportViewIds`——
**少了它不会报错**，只会让「靠 id 认出来的按钮」全部认不出来，风险判定跟着变松。

---

## 六、核心业务流程

### 6.1 任务状态机（**改之前先读这一节**）

`models/task.py`。

**状态枚举 `TaskStatus`**：
`CREATED` / `QUEUED` / `RUNNING` / `PAUSED` / `WAITING` / `DEGRADED` /
`DEVICE_UNAVAILABLE` / `CANCEL_REQUESTED` / `DONE` / `FAILED` / `CANCELLED`

**终态 vs 活跃**：

```python
TERMINAL_STATUSES = frozenset({DONE, FAILED, CANCELLED, DEGRADED})
ACTIVE_STATUSES   = frozenset({QUEUED, RUNNING, PAUSED, WAITING,
                               DEVICE_UNAVAILABLE, CANCEL_REQUESTED})
```

> `DEGRADED` 在终态集合里——它是「任务结束但过程有降级」的终态，不是中间态。
> `CANCEL_REQUESTED` **明确不是终态**（它只表示「请求已下达」）。

**事件到状态只有一处映射**：`_EVENT_TO_STATUS`。
**非法迁移的处置分级**（这是刻意的设计，不是 bug）：

- 从**终态**再迁出 → 抛 `InvalidTransitionError`（**fail closed**）。
- 非终态之间的非法迁移 → **照做 + 打 warning，返回 False**（避免卡死）。

**暂停原因**：`PAUSED_BY_USER = "user"`、`PAUSED_BY_PREEMPTION = "preemption"`；
`RESUMMABLE_PAUSED_REASONS = frozenset({PAUSED_BY_PREEMPTION})`
——**`user` 原因的暂停不能靠 `resume` 恢复**，要走用户停手后的自动恢复判定（见 6.3）。
`RECOVERY_ERROR = "recovery_error"` 是**数据损坏**的 API 专用返回值，**不是** `TaskStatus`。

**优先级**：`LOW < NORMAL < HIGH < CRITICAL`（`_PRIORITY_RANK`）。

### 6.2 执行循环与安全点（核心中的核心）

`agent/_execution.py:_run_loop()` 是主循环。每一轮顺序：

```
1. _sync_durable_state(task, state)        同步必须跨重启记住的东西（如否决黑名单）
2. refresh_user_context(task, state, session)   ★ 唯一一次「碰设备读用户活动」
3. safe_point(task, state, session)         ★ 安全点判定，stop=True 就退出
4. 版本围栏：task.version != state.run_version → 作废决策上下文、重新规划
5. _observe()                              → 截图 + dump UI 树 + 焦点
   └─ state.decision_epoch = ObservationEpoch.capture(...)  ← 记下决策依据那一屏
6. _reconcile_pending_effect()             上一步 EFFECT_UNKNOWN 就地把它对掉
7. _prepare()（首次）
8. _think() → planner → VLM               决策
9. action.is_completion_request → _request_finish()（模型说 done 只是申请）
10. 死循环检测 _note_action()              连续同动作 → _replan 换策略
11. ActionRiskGate.assess()                ★ 风险判定（唯一入口）
    └─ DANGEROUS / downgrade_blocked → 发 RISK_ASSESSED 事件（安全关键，写不下就 DEGRADED）
    └─ requires_confirmation → 转人工（发 WAITING，等 /confirm）
12. _toctou_guard()                        执行前复查页面
13. _execute() → executor.execute() → DeviceController.xxx()
14. _verify()                              多级证据
15. _save_checkpoint()
```

**`safe_point()` 的 5 步判定顺序**（`agent/runtime.py`，**顺序是有理由的，不要重排**）：

| 顺序 | 判定 | 返回的 `kind` | 为什么在这个位置 |
|---|---|---|---|
| 1 | 任务已是终态 | `terminal` | 最高优先，不该再动设备（最后一道闸） |
| 2 | `CANCEL_REQUESTED` | `cancel` | 用户明确要求停，优先于其它让位理由 |
| 3 | `PAUSED` / 被抢占让位 | `paused` / `preemption` | 外部要求停；抢占是**硬**让位 |
| 4 | 用户在场 | `user_active` / `user_pause_limit` | **软**让位（可以等几秒再看），故在硬让位之后 |
| 5 | 预算耗尽 | `budget_observations` / `budget_model_calls` | 自己跑不动了 |

**`SafePoint` 返回的不是 bool 而是带 `reason` 与 `kind` 的对象**，理由是调用方需要知道
**为什么**停下（好发对事件、落对状态、选对 `RunOutcome`）——返回 bool 会让调用方再猜一次，
而那正是「Runtime 说暂停了、Scheduler 说还在跑」这类语义冲突的来源。
**未处置的 `kind` 一律 fail-closed**。

**TOCTOU 守卫**（`_toctou_guard`）：
`session.generation` 是免费的，一定检查（但它只记 Shadow 自己的写入，看不到用户手动点击 /
通知栏 / App 异步刷新 / 另一个 adb client）。要拦那些必须额外读一次 `current_focus`
——比一次完整观察便宜得多，所以默认开（`SHADOW_TOCTOU_GUARD=0` 可关，
代价是外部改动拦不住）。
`TOCTOU_TIMEOUT_SECONDS = 4.0`；连续 `MAX_STALE_OBSERVATIONS = 3` 次判 stale
就按瞬时故障处理，**不再空转**（必须有上界，否则判定抖动会变成死循环）。

### 6.3 用户在场时的让位与恢复（V5 核心行为）

**让位判定**（`_user_presence_verdict`）：

- **只在 `state.user_confirmed and state.user_active` 都为真时才让位**。
- 只对**会占用用户屏幕**的任务生效：`effective_execution_mode()` 解析出不是前台 → 直接返回 `None`。
- 有上界：`state.user_pauses >= MAX_USER_PAUSES(=5)` → 转人工失败，
  而不是无限「暂停→恢复→又暂停」。

**`UserContext` 的三态纪律**（`device/user_activity.py`）：

```
active=True     有证据表明用户刚操作过
active=False    **有证据**表明用户没在操作
confirmed=False 读不到（设备不支持 / 命令超时 / 后端没实现）→ 保守取 active=True
```

**「不知道」在两个位置保守方向相反**（**非常容易被后人「修正」错，务必保留**）：

| 位置 | 保守方向 | 代价 |
|---|---|---|
| 动作层（`UserContext`） | 宁可说「用户在操作」（`active=True`，`confirmed=False`） | 任务慢一点 |
| 任务层（`_user_presence_verdict`） | 宁可**继续跑**（`confirmed=False` → 不让位） | 打扰用户 |

后者选「继续跑」的理由是**代价不对称**：「探测能力缺失」是**恒定**条件（本端永远不支持），
若把它当「用户在场」，任务会被**永久**暂停且用户无法修复——比打扰更糟。

**恢复判定**（`user_resume_ready`）三条缺一不可：
1. 用户停手够久（优先用设备实测的 `UserContext.idle_seconds`，退回本地计时；
   `DEFAULT_USER_IDLE_SECONDS = 2.0`）；
2. **重新观察过一次页面**（`observation is not None`）；
3. **页面指纹与暂停前一致**（`ObservationEpoch` 比对，复用决策快照那套）。

> 第 3 条要求**两帧都有指纹**。任一帧读不到（`None`/空串）→ **不恢复**。
> 注意这里的保守方向与「任务层让位判定」**又相反**——因为代价变成了「点错地方」。

### 6.4 执行状态机（动作级，v4.1）

`models/execution.py` + `agent/execution/state.py`。

```
CREATED ──→ RISK_CHECKED ──→ DISPATCHED ──→ RUNNING ──→ SUCCEEDED
                  ↑  └(自环：重新判风险)                     FAILED
                  │                                          REFUSED
                  └────────────────────────────────────────  UNVERIFIED
                                                             UNKNOWN
```

**关键常量**：

| 常量 | 值 | 意义 |
|---|---|---|
| `DEVICE_REACHED_STATUSES` | `frozenset({RUNNING})` | **只有 `RUNNING`** 代表「设备被调用过」 |
| `NO_AUTO_RETRY_STATUSES` | `frozenset({UNVERIFIED, UNKNOWN})` | **禁止自动重试** |
| `TERMINAL_EXECUTION_STATUSES` | `{SUCCEEDED, FAILED, REFUSED, UNVERIFIED, UNKNOWN}` | 终态不可改写 |
| `NON_TERMINAL_EXECUTION_STATUSES` | `{CREATED, RISK_CHECKED, DISPATCHED, RUNNING}` | |

`DISPATCHED` **刻意不算**「碰过设备」——它是「记录意图」，设备调用发生在它**之后**。
`RISK_CHECKED` 有一个**自环**（允许重新判风险）。

**`recovery_target(status)`**（`agent/execution/state.py`）：

```
终态     → None
RUNNING  → UNKNOWN   （可能已产生副作用，禁止自动重试）
其余     → FAILED    （设备从未被调用，动作确定没发生，可安全重做）
```

> `UNKNOWN` **不是**「保守一点的失败」。它意味着「手机那边可能已经付款了」，
> 所以它在 `LEGAL_TRANSITIONS` 里**没有出边**——没有任何代码路径能把它改回可重试状态。

**事务边界（`agent/execution/service.py` 模块 docstring，照抄审核流程图但改了一处顺序）**：

```
BEGIN  → 建执行记录(CREATED)                     → COMMIT   受理
BEGIN  → 状态 RISK_CHECKED + RISK_ASSESSED 事件  → COMMIT   判风险
BEGIN  → 状态 DISPATCHED + ACTION_DISPATCHED 事件 → COMMIT   记录意图
──────── 这里才调用设备（executor.execute）────────
BEGIN  → 状态 RUNNING                            → COMMIT   交给设备
BEGIN  → 状态终态 + ACTION_VERIFIED 事件          → COMMIT   记录结果
```

两个「为什么」：① 设备调用**必须在事务外**——ROLLBACK 抹掉的是数据库，
抹不掉手机上已经发生的点击；② 「先记录意图、再产生副作用」这条写前日志纪律要求
`DISPATCHED` 在设备调用**之前**。

**「合法」与「竞争」分开表达**：
- 迁移**不合法**（想从终态复活）→ 抛 `InvalidExecutionTransition`（调用方写错了）。
- 迁移**合法但守卫落空**（并发派发同一条执行）→ 返回 `False`，不抛异常。

### 6.5 风险判定（五维）

`agent/risk_gate.py:ActionRiskGate.assess()` 是**唯一入口**，返回
`strictest(type_risk, text_risk, evidence_risk, page_risk, content_risk)`。

| 维度 | 判什么 | 关键实现 |
|---|---|---|
| 1. **类型** | 动作类型本身 | `SAFE_ACTION_TYPES={BACK,HOME,WAIT,DONE,DONE_REQUEST}`；`CAUTION_ACTION_TYPES={TAP,LONG_PRESS,TYPE,SWIPE,LAUNCH}` |
| 2. **语义角色** | 动作/控件的语义（purchase/delete/authorize/submit…） | `infer_role(action._policy_haystack())` + **目标节点文本 / resource-id** |
| 3. **目标证据** | UI 树读不到 / 解析失败 / 找不到 / **多个候选** | `TargetResolution.is_evidence_gap` |
| 4. **页面敏感度** | 敏感**应用**（静态包名）或敏感**屏**（动态内容） | `_is_sensitive_package` / `_screen_sensitivity_hint` |
| 5. **输入内容** | `TYPE` 的 `value` 里的身份/资金凭据 | `sensitive_content()`；**只有 `TYPE` 参与** |

**维度 2 里最有价值的那个能力**（代码注释原文）：
「动作描述里没有危险词，但被点到的那个控件本身叫『立即购买』——只有查 UI 树才知道」。

**维度 3 的两档处置（代价不对称）**：

- 普通应用：抬到 `CAUTION`（**只让审计看得见，不改现状**——因为
  TAP/LONG_PRESS/TYPE/SWIPE/LAUNCH 的类型下限本来就是 CAUTION）。
- **敏感应用 / 敏感屏**：直接抬到 `DANGEROUS` → 转人工。理由：我们不知道点在哪、
  也不知道那底下是什么按钮，而这一下可能就是付款。

**维度 5 的两档同理**：敏感侧写凭据 → `DANGEROUS`；普通屏写 → `CAUTION`（不改现状）。
**不去全量转人工的理由**：「随手把卡号记进备忘录也该由用户自己决定，全拦会变成噪声，
然后被绕过」——这条经验来自 V3.1 P0-3 的教训。

**两个容易搞错的字段**：

- `downgrade_blocked`：**只在模型显式声明了更低风险**时才为真（否则是噪声）。
- `requires_confirmation = effective is DANGEROUS`。

**`RiskContext` 刻意不含 `instruction` 用于关键词匹配**（否则「帮我在淘宝下单」里的每个点击
都会被判 DANGEROUS = 什么都拦 = 什么都不拦）。

**几组相关常量**（`models/action.py` / `models/semantic.py`）：

- `ActionRisk` = `SAFE` / `CAUTION` / `DANGEROUS`，配 `RISK_ORDER` / `risk_rank` / `strictest`。
- `SENSITIVE_PACKAGE_MARKERS`：包名含 `pay`/`bank`/`wallet`/`alipay`/`tenpay`/`unionpay`/
  `credit`/`money`/`securities`/`stock`/`insurance`/`billing` 等即视为敏感应用。
- `SENSITIVE_SCREEN_MARKERS`：如「确认支付」「立即支付」「确认转账」「提交订单」
  「绑定银行卡」「验证码」「输入支付密码」等（命中即视为敏感屏）。
- `SideEffectClass`：`READ_ONLY` / `IDEMPOTENT_WRITE` / `NON_IDEMPOTENT_WRITE` / `IRREVERSIBLE`。
- `MUTATING_ACTION_TYPES == CAUTION_ACTION_TYPES`。
- `DURATION_RANGE_MS = (1, 60_000)`。
- `DANGEROUS_KEYWORDS` 是 **V3 M2 之前的遗留物**，现在语义角色才是主路径。

### 6.6 多级证据验证

`agent/evidence.py` 的 `EvidenceLevel`（**独立证据，逐级加码**）：

```
L1_DEVICE         设备层
L2_NAVIGATION     导航层（package / activity 变了）
L3_STRUCTURE      结构层（UI 指纹变了）
L4_TARGET         目标层（目标元素消失 / 文本变了）
L5_SUCCESS_MARKER 本地终态文案命中
L5_VLM            VLM 语义判定
L6_GOAL           GoalVerifier 裁定
NONE
```

`strongest_layer` 会记录**结论依据的是哪一层**——审计能回答「这次成功是谁说的」。

**为什么不能只用 VLM**（`agent/verifier.py` 模块 docstring）：
「它贵、慢，而且会为一次根本没生效的点击编出合理化的理由」。
另外两个已修的洞：VLM 返回非法枚举（如 `{"result":"banana"}`）以前会被当成功；
「页面变没变」以前只比可点击 label 集合（toast / dialog 会骗过它）。

**贯穿原则**：**危险动作在缺少独立证据时，绝不按成功处理。**

**目标定位口径**（`vision/target.py`，全系统只此一份）：
- `TargetState`：`UNKNOWN` / `ABSENT_BEFORE` / `GONE` / `CHANGED` / `UNCHANGED`，
  `is_positive_evidence = GONE | CHANGED`。
- `TargetResolution`：`OK` / `NO_TREE` / `PARSE_ERROR` / `NOT_FOUND` / `AMBIGUOUS` / `NO_TARGET`，
  `is_evidence_gap = NO_TREE | PARSE_ERROR | NOT_FOUND | AMBIGUOUS`。
- **「多个候选」也算证据缺口**（手机 Agent 最容易犯的错不是点不到按钮，
  而是**点错了那个同名按钮**）。
- `AMBIGUOUS` 会返回**文档序第一个候选**——保证「风险判定看到的元素 == 真正被点的元素」。

### 6.7 目标裁定（GoalOracle + GoalVerifier）

`agent/goal_oracle.py` 把两个概念拆开：

```
plan_finished(plan)     → 计划还有没有 pending 步骤（本地事实，弱证据）
goal_achieved(evidence) → 独立于模型计划的世界证据（可核验声明命中 / 页面推进）
```

**裁定规则**：**「计划跑完」是完成的门槛，不是完成的依据。**
按画像分层（`agent/goal_policy.py`）：

| 画像 | 裁定 |
|---|---|
| `strict`（navigation / side_effect） | 计划跑完 **+** 页面推进过 → 完成；计划跑完 + 无页面推进 → 不确定 |
| `advisory`（read_only） | 计划跑完 → 完成（纯查询够用） |

`TaskProfile` 的优先级判定：`SIDE_EFFECT > NAVIGATION > READ_ONLY > UNKNOWN`。
`MAX_GOAL_REJECTIONS = 2`（`agent/goal_verifier.py`）。
**`expected_state` 只进计划、不进裁定**（裁定只认 `goal_verifier` 的独立证据）。

**已知的有理由延期**（写在注释里，不是 bug）：
`page_seen_changed` 仍是「页面变过」，不是「目标状态成立」。反例：
「在淘宝搜索 iPhone 并进入详情页」时模型点了「搜索」→ 页面变成搜索结果页 →
模型错误给出 DONE → `pending_steps` 恰好 0 → `page_seen_changed=True` → CONFIRMED。

### 6.8 崩溃恢复

两处，**判据都是同一条**：设备有没有被调用过。

**动作级**（`agent/execution/recovery.py` + `api/server.py:recover_stale_executions()`）：

```
CREATED / RISK_CHECKED / DISPATCHED  → FAILED   设备从未被调用，动作确定没发生
RUNNING                              → UNKNOWN  可能已产生副作用，禁止自动重试
```

- **宽限窗口默认 0**：单进程下「启动时看到非终态执行 = 死进程留下的」这个结论成立。
  开了 `SHADOW_ALLOW_MULTI_PROCESS` 时默认 300 秒（另一个进程可能正拿着它在跑）。
  由 `SHADOW_EXECUTION_STALE_SECONDS` 覆盖。
- **幂等**：恢复过的记录已是终态，下次启动不会再被选中。
- 计数字典 `{unknown, failed, skipped, young}` 会进启动日志与 `/health/detail`。
- 单条恢复失败**不挡住其余，也不挡住启动**。

**任务级**（`agent/_recovery.py` + `runtime._gate_crash_recovery()`）：
崩溃恢复**必须在 `mark(RUNNING)` 之前**——先确认「上次那个动作到底发出去没有」。
确认不了就**不进入执行循环**，走 `WAITING` 转人工。
恢复后会把 `task.recovery_note` 转成 Re-plan 理由（「请先核验当前页面状态，
不要直接重复上一个动作」），避免不可逆动作被做第二遍。

**孤儿恢复点清理**（`_prune_orphan_checkpoints()`）：
「写恢复点 → 改指针 → 落盘任务」三步，崩在最后一步前会留下**孤儿恢复点**
（文件在盘上，任务指针没提交）。
两个刻意的选择：用 `list_all()` 而**不是** `list_active()`（终态任务同样可能有被提交过的
恢复点，只按活跃任务算会**误删**）；**只在启动时扫**（运行期扫会与正在写恢复点的 worker 抢）。
清理失败**绝不能挡住启动**。

### 6.9 指令注入与任务关系（classifier）

`agent/classifier.py`，三层打分：`LLM_WEIGHT=0.6` / `RULE_WEIGHT=0.3` / `SIMILARITY_WEIGHT=0.1`。

```
POST /tasks/{id}/inject {"instruction":"..."}
  ▼
TaskManager.inject()
  ├─ TaskClassifier.classify(新指令, 当前任务) → TaskRelation
  └─ 按关系分派为 InjectAction：
       MERGED              并入当前任务（子任务）
       SPAWNED             新起一个任务
       PREEMPTED           抢占（高优先级）
       SUPERSEDED          改写当前任务目标（SUPER_TASK，不可逆）
       DUPLICATE_IGNORED   重复指令，忽略
       NEEDS_CONFIRMATION  置信度达标但会改写/打断 → 需调用方显式放行
```

**`SUPER_TASK` 必须显式放行**：`InjectRequest.allow_disruptive` 默认 `False`，
命中时先返回 `needs_confirmation`，调用方确认后再带 `true` 重发。
改写当前任务是**不可逆**的，走 CAS（`_rewrite_authoritative` 是唯一入口）。

**授权必须在 `manager.inject()` 之前**（`api/server.py` 注释原文）：
这不是「位置不优雅」，而是一个真实的 **Authorization after side effect** 漏洞
——`inject()` 会在一次调用里改 instruction / version / plan / checkpoint_id / priority，
若先执行它再返回 403，**任务已经被改掉了**。

**规则层的关键短语**（都是中文启发式，改之前想清楚）：
- `SUBTASK_LEADERS`：`先` / `先帮我` / `先给我` / `其中` / `顺便` / `然后` / `接着` /
  `再帮我` / `把这个`
- `INTERRUPT_MARKERS`：`马上` / `立刻` / `立即` / `紧急` / `现在就要` / `赶紧` /
  `停一下` / `打断一下` / `别管了`
- `SUPER_TASK_MARKERS`：`改成` / `换成` / `不要了` / `重来` / `重新来` / `取消刚才` /
  `我说的不是`
- `RULE_MIN_SCORE = 0.4`、`DUPLICATE_SIMILARITY = 0.85`、`AFFINITY_FLOOR = 0.35`、
  `LLM_DEPENDENT_CONFIDENCE = 0.6`

### 6.10 人工确认（HITL）完整链路

```
1. Runtime 判定 assessment.requires_confirmation（effective = DANGEROUS）
   → 发 WAITING 事件 → 任务进 WAITING 状态
2. 调用方 GET /tasks/{id}
   → 拿到 pending_confirmation 的**元数据**（**不含 token**）
   → 附 token_endpoint 与 token_expires_in_seconds
3. 调用方 POST /tasks/{id}/confirmation-token → 显式申请一次性令牌
4. 调用方 POST /tasks/{id}/confirm {"approved":true,"token":"..."}
   ▼ 整段在一个事务里：
   ├─ auth.reserve_confirmation_token()   预占（不作废）
   ├─ manager.resolve_confirmation()      改 Task 状态
   ├─ 写 CONFIRMED 事件（安全关键，fail-closed）
   └─ auth.commit_confirmation_token()    作废票据
5. 两种语义的落点**一致：都重新入队继续做**
   - 危险动作：批准 = 放行；否决 = 换策略继续
   - 完成裁定：批准 = 认定完成；否决 = 还没做完，继续做
6. **只有 POST /tasks/{id}/cancel 才结束任务**
```

**为什么令牌不随 GET 下发**：「拿到令牌就能放行真实危险动作，而 GET 响应会被写进日志、
前端状态、代理缓存和调试工具」。

**一次性**靠 `consumed_confirmation` 表的 `jti` 主键（跨重启 + 跨进程）。
`/health/detail` 的 `confirmation_consumption` 字段报出后端类名——
**只有它不是 `InMemoryConsumption` 时，「一次性」才跨重启成立**。

**放行凭据绑「动作 + 页面」**：`ApprovalGrant.matches()` 会传入
**放行那一刻**的 `package` / `activity` 与批准时记下的比对——批准之后页面被换掉，凭据作废。
**一次性：放行即消费，绝不复用。**

### 6.11 回放（`agent/replay.py`）

| 项 | 值 |
|---|---|
| 数据类型 | **观察回放**（只读事件流） vs **动作重放**（真发动作） |
| 默认行为 | **dry-run**（`replay(..., dry_run=True)`） |
| 危险动作 | **必须显式放行**，否则拒绝 |
| 数据源 | **必须是 `EventLog`**，不是 `TrajectoryStore` |
| 原因 | `TrajectoryStore` 的观察会被**裁剪**（为下一步决策服务）；回放需要完整只追加流 |
| 4 个阶段 | `ReplayPhase`：LIFECYCLE / ACTION / RECOVERY / CONTROL |
| HTTP 接口 | `GET /tasks/{id}/replay?format=json\|markdown`（markdown 开头是「值得注意的地方」） |

---

## 七、核心模块说明

| 模块 | 文件 | 行数 | 职责 | 关键入口 |
|---|---|---|---|---|
| HTTP 契约 | `api/server.py` | 1,785 | 路由、鉴权中间件、审计、装配 | `app`、`create_task`、`health_detail` |
| 鉴权与确认票据 | `api/auth.py` | 628 | Principal / 令牌 / 确认票据签发与消费 | `authenticate`、`issue_confirmation_token`、`reserve_confirmation_token` |
| 任务生命周期 | `agent/task_manager.py` | 596 | 创建/注入/暂停/恢复/取消/确认 | `create`、`inject`、`resolve_confirmation` |
| 调度 | `agent/scheduler.py` | 1,206 | 每设备一条 lane、优先级、抢占、恢复 | `submit`、`preempt_running`、`start` |
| 指令关系判定 | `agent/classifier.py` | 380 | 规则 + 相似度 + LLM 三层 | `classify` |
| 运行时 | `agent/runtime.py` | 724 | 装配 + 事件/持久化原语 + 安全点 | `run`、`safe_point`、`user_resume_ready` |
| 执行循环 | `agent/_execution.py` | 1,290 | Observe→Think→Act→Verify→Checkpoint | `run`、`_run_loop` |
| 目标控制 | `agent/_goal.py` | 181 | 完成申请与被否决后的继续 | `_request_finish` |
| 对账 | `agent/_reconcile.py` / `reconciliation.py` | 139 / 203 | `EFFECT_UNKNOWN` 的四条出路 | `_reconcile_pending_effect` |
| 确认 | `agent/_confirm.py` | 145 | 危险动作的人工确认流转 | `_ask_human` |
| 恢复 | `agent/_recovery.py` | 144 | 崩溃恢复门禁（唯一会把任务交给人的路） | `_gate_crash_recovery` |
| 执行状态机 | `agent/execution/{state,service,recovery}.py` | 129 / 396 / 144 | 迁移表 + 唯一写入口 + 启动恢复 | `can_transition`、`recover_executions` |
| 观察 | `agent/observer.py` | 59 | 截图 + UI 树 + 焦点（端口化） | `observe` |
| 规划 | `agent/planner.py` | 128 | 生成 / 重规划（**不产坐标**） | `plan`、`replan` |
| 决策 | `agent/runtime.py:_think/_decide` | — | 调 VLM 出 `Decision` | `decide_next_action` |
| 执行 | `agent/executor.py` | 142 | Action → 设备端口调用（**不抛异常**） | `execute` |
| 验证 | `agent/verifier.py` | 352 | 多级证据 | `verify` |
| 风险门禁 | `agent/risk_gate.py` | 398 | 五维风险判定（唯一入口） | `ActionRiskGate.assess` |
| 目标裁定 | `goal_verifier.py` / `goal_oracle.py` / `goal_policy.py` | 295 / 117 / 167 | 独立证据裁定完成 | `GoalVerifier` |
| 回放 | `agent/replay.py` | 520 | 观察回放 / 动作重放（默认 dry-run） | `replay`、`build_timeline` |
| 设备端口 | `device/controller.py` | 438 | 协议基类 + 三态能力声明 + 影子方法 | `DeviceController` |
| 设备装配 | `device/factory.py` | 181 | 按 `SHADOW_DEVICE_BACKEND` 选后端 | `build_controller` |
| ADB 后端 | `device/adb.py` | 514 | PC 侧控制 | `AdbController` |
| Android 后端 | `device/android.py` | 825 | 手机侧（`AndroidBridge`，12 方法） | `AndroidDeviceController` |
| 远程 HTTP 桥 | `device/remote.py` | 340 | 路线 B 的 HTTP 端点客户端 | `build_remote_bridge` |
| 会话与平面 | `device/session.py` | 833 | `TaskPlane` / `DeviceSession` / `ExecutionSession` / `ShadowActionRouter` | `resolve_task_plane` |
| 用户活动 | `device/user_activity.py` | 257 | `UserContext` 三态 | `user_context` |
| 设备池 | `device/pool.py` | 131 | 注册表 + 可用性订阅 | `DevicePool` |
| 存储底座 | `storage/database.py` | 177 | 连接 + PRAGMA + **可重入事务** | `Database.transaction` |
| 迁移 | `storage/migrations/__init__.py` | 195 | `PRAGMA user_version`，当前 **v4** | `migrate` |
| 任务存储 | `storage/task_store.py` | 262 | `revision` CAS + 损坏隔离 | `save`、`_quarantine` |
| 事件日志 | `storage/event_log.py` | 319 | 只追加事件流 + 安全关键分级 | `emit`、`read` |
| 恢复点存储 | `storage/checkpoint_store.py` | 242 | 与任务指针同事务 | `save`、`prune_orphans` |
| 执行记录 | `storage/execution_store.py` | 462 | 受保护迁移 | `transition(expect=…, to=…)` |
| 确认票据存储 | `storage/confirmation_store.py` | 334 | 两阶段 `reserve`→`commit`/`release` | `reserve`、`commit` |
| 租约存储 | `storage/lease_store.py` | 196 | 跨进程唯一执行者（独立 `lease.db`） | `TaskLease` |
| 轨迹存储 | `storage/trajectory_store.py` | 189 | 会裁剪的观察轨迹（仍是文件） | `append`、`history` |
| 请求审计 | `storage/audit_log.py` | 65 | JSONL 审计（写失败永不抛异常） | `record` |
| VLM 调用 | `vision/vlm.py` | 674 | prompt + 调用 + 重试 + 解析 | `generate_plan`、`decide_next_action`、`verify_transition` |
| UI 树解析 | `vision/parser.py` | 179 | Xml → `UiNode`，元素定位 | `parse`、`find_clickable`、`rank_by_text` |
| 目标定位 | `vision/target.py` | 240 | 目标状态与解析结果（**全系统唯一口径**） | `resolve_target`、`target_state` |
| 坐标换算 | `vision/grounding.py` | 117 | 归一化坐标 → 像素 | `resolve_target` |
| 页面指纹 | `vision/fingerprint.py` | 62 | `ui_fingerprint` | `ui_fingerprint` |

**设备端口的完整方法集**（`device/controller.py`，注意两类）：

- **必需**（`_REQUIRED_METHODS`，装配期校验）：`screen_size`、`current_focus`、`dump_ui`、
  `screenshot_bytes`、`screenshot`、`state`、`tap`、`long_press`、`swipe`、`back`、`home`、
  `launch`、`launch_app`、`wait`、`deadline_budget`、`build_input_provider`
- **可选**：`supports_user_activity`、`user_context`、`shadow_plane_available`
- **影子动作**（**不设默认实现、也不进必需集合**，由 `SHADOW_ACTION_METHODS` 单独列出）：
  `shadow_screenshot_bytes`、`shadow_dump_ui`、`shadow_tap`、`shadow_long_press`、
  `shadow_swipe`、`shadow_set_text`、`shadow_back`、`shadow_home`、`shadow_launch`
  ——**全部抛 `ShadowActionUnsupported`，绝不回落**。
  用 `shadow_` 前缀的新名字而不是给 `tap` 加 `display_id=` 参数，理由是：
  **静默回落要变成不可能**（带前缀的新名字在没实现时就是没实现）；
  两种后端的实现路径本来就不同。**名字本身就是防线。**

`READ_ONLY_OPERATIONS = frozenset({'screenshot','screenshot_bytes','dump_ui','screen_size',
'current_focus','state','read_shell','shell'})`。
`IncompleteDeviceController` 是装配期完整性校验的产物。

---

## 八、AI / Agent / RAG / MCP 架构

### 8.1 结论先行

| 能力 | 本项目状态 |
|---|---|
| LLM / VLM 调用 | ✅ **有**，走 HTTP 的 OpenAI 兼容接口 |
| Prompt 体系 | ✅ **有**，5 个带版本号的 prompt |
| Agent / Workflow | ✅ **有**，自研循环（**不是 LangGraph**） |
| Retry / Fallback | ✅ **有**，分级重试策略 + 指数退避 |
| Human-in-the-loop | ✅ **有**，完整的一次性令牌链路 |
| RAG | ❌ **没有**。无数据源、无解析、无 chunk、无 embedding、无向量库、无检索、无 rerank、无引用 |
| MCP | ❌ **没有**。无 MCP Server、无 MCP Tool |
| 多 Agent 协作 | ❌ **没有**。只有一个 Runtime 循环（`Scheduler` 的多任务并发**不是** Multi-Agent） |

> **【重要】** 本项目**没有** RAG 与 MCP。设计文档里提到过 embedding grounding 这类设想，
> 但代码里**一行都没有**，并且注释明确标注「不实现，触发条件见注释」。
> 不要因为这是一个「AI Agent 项目」就默认它有这些组件。

### 8.2 模型接入

`vision/vlm.py`。

| 项 | 值 |
|---|---|
| 接口协议 | **OpenAI 兼容** `/chat/completions` |
| 默认 base URL | `https://api.openai.com/v1`（`DEFAULT_BASE_URL`） |
| 默认模型 | `gpt-4o`（`DEFAULT_MODEL`） |
| 环境变量 | `VLM_BASE_URL` / `VLM_API_KEY` / `VLM_MODEL`（**每次调用时读**，支持热改） |
| 部署形态 | **云端 HTTP**（**明确不放手机本地**，第一版约定） |
| 超时 | `VLM_TIMEOUT_SECONDS = 60.0` |
| 最大尝试 | `VLM_MAX_ATTEMPTS = 3` |
| 退避基数 | `VLM_BACKOFF_SECONDS = 0.8` → 第 n 次失败后等 `0.8 * 2^(n-1)` 秒 |
| 可重试状态码 | `RETRYABLE_STATUS_CODES = {408,409,425,429,500,502,503,504}` |
| 非可重试 4xx | **立即上抛**，不重试 |
| `max_tokens` | **硬编码 512**（`_call_vlm()` 的 payload 里） |
| `temperature` | **未显式设置**（用服务端默认）【代码推断】 |
| 图像细节档位 | `_DEFAULT_IMAGE_DETAIL = {"plan":"low","decide":"high","verify":"low"}` |
| 失败行为 | 重试耗尽 → `VlmError("VLM 请求失败（已重试 3 次）")` → API 层映射 **503** |

**模型可替换性**：只认 OpenAI 兼容接口，所以**云端 Qwen / 本地 vLLM / Ollama 都只是三个
环境变量的事**。仓库里实测在用的模型是 `qwen3.5-omni-flash`（写在 `artifacts/vlm_env.sh`，
该文件不在仓库内），实测约 2.1 秒出决策。

**响应归一化**：多模态 `content` 可能是 list 形式，`_call_vlm()` 里做了归一化处理。

### 8.3 Prompt 体系

5 个 prompt，**都带版本号常量**
（`tests/test_vision.py::test_prompt_versions_are_declared_for_every_prompt` 会检查
每个 prompt 都有版本号——**改 prompt 必须同步改版本号**）：

| 版本常量 | 值 | 用途 | 出参 |
|---|---|---|---|
| `PROMPT_VERSION_PLAN` | `plan_v3` | 从任务指令生成计划 | 计划步骤列表 |
| `PROMPT_VERSION_DECIDE` | `decide_v4` | 每一步的下一步动作 | `Decision`（含 `Action`） |
| `PROMPT_VERSION_REPLAN` | `replan_v1` | 执行方式失败后换策略 | 新的 `Action` |
| `PROMPT_VERSION_VERIFY` | `verify_v1` | 验证动作是否生效 | `VerifyResult` |
| `PROMPT_VERSION_RELATION` | `relation_v1` | 两条指令的关系判定 | `TaskRelation` |

**Prompt 的影响面（改它之前必须知道）**：
- `decide_v4` 改动会影响**所有任务的每一步**，是风险最高的一个。
- `verify_v1` 改动会影响「这步成没成」的判定，从而影响后续所有决策。
- `plan_v3` 只影响首次规划。
- 构建函数：`build_plan_prompt` / `build_decision_prompt` / `build_replan_prompt` /
  `RELATION_PROMPT`。
- **计划的渲染**（`agent/planner.py:format_plan`）会把每个步骤的**状态**与
  `expected_state` 一起写进 prompt——所以「模型能看到哪几步已经做完」。
- `generate_plan` 在模型不按 prompt 输出时会**降级而不是报错**。

### 8.4 Agent / Workflow（自研循环，**不是图**）

本项目**没有**用 LangGraph 或任何编排框架，是**手写的 Python 循环**。
把它当成「状态机 + 循环」而不是「图」来理解。

**`RuntimeState`（约 40 个字段，全部有文档）**：

```
task / step / observation / last_observation
decision_epoch（决策快照，用于 TOCTOU）
approval（人工放行凭据，一次性）
denied_fingerprints（被否决的动作，跨重启存活）
failed_strategies（试过的失败策略，喂给 Re-plan）
retry counters（transient / replan / unknown）
user_active / user_confirmed / user_pauses / foreground_package
last_user_pause_at / pause_epoch（用户暂停恢复的判据）
execution_mode / session_id / display_id / shadow_state_id（执行平面）
```

**循环终止条件（`RunOutcome`）**：
`DONE` / `FAILED` / `SUSPENDED`（被抢占）/ `SUSPENDED_BY_USER`（用户在场让位）/
`CANCELLED` / `AWAITING_CONFIRMATION`（等人工）。

**失败处理（`models/retry.py`）**：

```
ErrorClass（瞬时可重试 / 需换策略 / 需人工 / 未知）
  ▼
RetryPolicy(max_transient=3, max_replan=2, max_unknown=1)
  ▼
RetryAction：RETRY | REPLAN | ASK_HUMAN | ABORT
```

**死循环防护**（`agent/runtime.py`）：
`LOOP_WINDOW = 4`、`LOOP_REPEAT_THRESHOLD = 3` —— 连续多次执行同一个动作 →
**换策略（Re-plan）而不是继续重试**（`_note_action()` 返回 True 触发）。

**Re-plan 时给模型的备选路径**（`agent/planner.py:DEFAULT_ALTERNATIVES`）：
「改用 UI 树里的元素文本定位」「改用 content-desc / resource-id 定位」
「先返回上一页再重新进入」「滚动页面后再试」「改用文本输入替代逐级点击」
「等待页面加载完成后重试」——目的是**避免它把同一个动作原样重发一遍**。

**有上界的几个计数**：

| 常量 | 值 | 位置 | 超限后果 |
|---|---|---|---|
| `MAX_STALE_OBSERVATIONS` | 3 | `_execution.py` | 连续 3 次「决策时那一屏已变」→ 按瞬时故障处理 |
| `MAX_EFFECT_RECONCILIATIONS` | 2 | `runtime.py` | 对账次数上限 |
| `MAX_USER_PAUSES` | 5 | `runtime.py` | 用户持续占用 → 转人工失败 |
| `MAX_GOAL_REJECTIONS` | 2 | `goal_verifier.py` | 完成申请被否决上限 |
| 三类预算 | 10 动作 / 60 观察 / 40 模型调用 | `models/budget.py` | `safe_point` 第 5 步判停 |

**工具调用**：本项目**没有**「Tool / Function Calling」抽象。设备操作是
`Action`（`models/action.py`）——一个强类型枚举：
`TAP` / `LONG_PRESS` / `SWIPE` / `TYPE` / `BACK` / `HOME` / `LAUNCH` / `WAIT` /
`DONE` / `DONE_REQUEST`。
VLM 返回的是**结构化 JSON**（`_parse_action` 解析），不是 function call。
`DONE_REQUEST` 表示「模型请求完成」——**最终由 `goal_verifier` 裁定**。

### 8.5 多级验证链

见 6.6。核心点：**VLM 只是最后一层，且危险动作缺独立证据时绝不按成功处理。**

### 8.6 对账（`agent/reconciliation.py`）

问题：动作已经发出去了，但「它到底生效了没有」是未知的。
对账要把它拆成四条路：

```
已成功    → CONTINUE   继续原计划
未成功    → RETRY      重做该动作（**危险动作除外**）
页面不符  → REPLAN     重新规划
无法判断  → ASK_HUMAN  转人工确认
```

**关键安全约束**：危险动作（付款/发送/删除）在无法确认成功时，**绝不 RETRY**，
也不接受「页面变了」这种弱证据。宁可多问一次人，也不能重复扣款、重复下单。

---

## 九、数据库设计

### 9.1 数据库类型与文件布局

| 项 | 值 | 依据 |
|---|---|---|
| 类型 | **SQLite**（标准库 `sqlite3`） | `storage/database.py` |
| 主库 | `<STORAGE_DIR>/shadow.db`，默认 `artifacts/state/shadow.db` | `api/server.py` |
| 跨进程租约库 | 独立的 `lease.db` | `storage/lease_store.py` |
| 轨迹 | `<STORAGE_DIR>/trajectories/*.jsonl`（**仍是文件**，会被裁剪） | `storage/trajectory_store.py` |
| 请求审计 | `<AUDIT_DIR>/YYYY-MM-DD.jsonl`，默认 `artifacts/state/audit/` | `storage/audit_log.py` |
| PRAGMA | `journal_mode=WAL`、`synchronous=2(FULL)`、`busy_timeout=5000`、`foreign_keys=0` | 对**活动库**实测 |
| 版本 | `PRAGMA user_version = 4` | 实测 |
| 迁移方式 | `storage/migrations/` 用 `user_version` 记版本 | |
| 历史 JSON 导入 | 首次打开时一次性导入旧 `*.json`/`*.jsonl`/`confirmations.db`，**保留 revision / 时间戳 / 状态** | `storage/task_store.py:_import_legacy_json` |

> **`foreign_keys=0`**：SQLite 的 FK 约束**未启用**。表间关系是**逻辑关系**（靠应用层保证），
> 不是数据库强制的外键。这是事实，不要以为有 FK 保护。

**`lease_store` 为什么单独一个库**（其 docstring）：JSON + `RLock` 是**进程内**的，
锁不住跨进程；而 M3 阶段只需要「跨进程原子 claim」这一件事，所以用一个独立的
`lease.db` 而不是把租约混进主库。

### 9.2 核心表（**DDL 为实测导出，非推测**）

| 表名 | 用途 | 核心字段 | 关键约束 / 索引 |
|---|---|---|---|
| `tasks` | 任务文档 | `task_id`(PK), `revision`, `status`, `created_at`, `payload`(JSON) | PK `task_id`；`idx_tasks_status`、`idx_tasks_created` |
| `checkpoints` | 恢复点 | `task_id`, `checkpoint_id`, `created_at`, `payload`(JSON) | **PK `(task_id, checkpoint_id)`**；`idx_checkpoints_task` |
| `events` | 只追加事件流 | `event_id`(PK), `task_id`, `execution_id`, `principal`, `device_id`, `kind`, `created_at`, `sequence`, `payload` | **UNIQUE `(task_id, sequence)`**；`idx_events_task`、`idx_events_execution`、`idx_events_kind` |
| `executions` | 动作级执行记录 | `execution_id`(PK), `task_id`, `principal`, `device_id`, `channel`, `action_type`, `action_payload`, `risk_level`, `status`, `request_id`, `created_at`, `started_at`, `finished_at`, `result`, `note`, `revision` | `idx_execution_status`、`idx_execution_task`、`idx_execution_device`、`idx_execution_principal` |
| `consumed_confirmation` | 已消费的确认票据 | `jti`(PK), `task_id`, `principal`, `fingerprint`, `consumed_at`, `expires_at`, `state`, `reserved_at` | PK `jti`（**一次性靠它**）；`idx_confirmation_state` |

**字段默认值（实测）**：
- `executions.channel` 默认 `'manual'`（Agent 路径写 `'agent'`）
- `executions.action_payload` 默认 `'{}'`；`executions.result` 默认 `'{}'`
- `consumed_confirmation.state` 默认 `'consumed'`
- `events.execution_id` / `principal` / `device_id` 默认 `''`（**可空字符串，不是 NULL**）

### 9.3 ER 关系图（由代码推导，非数据库 FK）

```
                      ┌──────────────┐
                      │    tasks     │  task_id (PK)
                      │  revision    │  ← 唯一推进 revision 的是 TaskStore.save
                      └──────┬───────┘
              ┌──────────────┼───────────────┬─────────────────┐
              │              │               │                 │
              ▼              ▼               ▼                 ▼
      ┌─────────────┐ ┌───────────┐  ┌────────────┐  ┌──────────────────────┐
      │ checkpoints │ │  events   │  │ executions │  │ consumed_confirmation│
      │(task_id,    │ │ task_id   │  │ task_id    │  │ task_id              │
      │ checkpoint_ │ │ execution_│◄─┤execution_id│  │ fingerprint          │
      │ id) PK      │ │ id (软链) │  │            │  │ (动作指纹，非 FK)     │
      └─────────────┘ └───────────┘  └────────────┘  └──────────────────────┘
```

- `events.execution_id` → `executions.execution_id`：**软关联**（可空字符串）。
  Agent 路径的执行事件带 `execution_id`；**已知残留**：危险动作的 `RISK_ASSESSED` 事件
  **不带** `execution_id`。
- `consumed_confirmation.fingerprint` 是**动作指纹或确认类型**，不是外键。
- `tasks.revision` 是**乐观锁版本号**，唯一写入口是 `TaskStore.save`。

### 9.4 主键 / 唯一约束 / 索引汇总

**主键**：`tasks.task_id`、`checkpoints.(task_id, checkpoint_id)`、`events.event_id`、
`executions.execution_id`、`consumed_confirmation.jti`。

**唯一约束**：`events.UNIQUE(task_id, sequence)` —— 保证同一任务的事件序号不重复
（幂等重放的基础）。

**显式索引 11 个**（`sqlite_master` 实测）：
`idx_checkpoints_task`、`idx_confirmation_state`、`idx_events_execution`、`idx_events_kind`、
`idx_events_task`、`idx_execution_device`、`idx_execution_principal`、`idx_execution_status`、
`idx_execution_task`、`idx_tasks_created`、`idx_tasks_status`。

### 9.5 状态 / 枚举字段

| 字段 | 取值来源 |
|---|---|
| `tasks.status` | `models/task.py:TaskStatus`（11 个值，见 6.1） |
| `events.kind` | `storage/event_log.py` 的 22 个常量（见 9.6） |
| `executions.status` | `models/execution.py:ExecutionStatus`（8 个值，见 6.4） |
| `executions.risk_level` | `models/action.py:ActionRisk`（`safe`/`caution`/`dangerous`） |
| `executions.channel` | 默认 `'manual'`；Agent 路径为 `'agent'`（实测数据可见） |
| `consumed_confirmation.state` | 默认 `'consumed'`；另有预占态 |

**注意：这些字段在数据库里是 TEXT，没有 CHECK 约束。**
取值合法性由 Python 侧枚举保证。

### 9.6 事件类型全集（`storage/event_log.py`）

**生命周期**：`CREATED` / `QUEUED` / `STARTED` / `CHECKPOINT_SAVED` / `RECONCILED` /
`WAITING` / `SUSPENDED` / `RESUMED` / `DONE` / `FAILED` / `CANCELLED` / `RECOVERED`

**动作与风险**：`ACTION_DISPATCHED` / `ACTION_VERIFIED` / `RISK_ASSESSED` /
`PREEMPT_REQUESTED` / `OBSERVATION_STALE` / `EFFECT_UNKNOWN`

**人工与目标**：`CONFIRMED` / `GOAL_REQUESTED` / `GOAL_CONFIRMED` / `GOAL_REJECTED`

**数据层**：`TASK_CORRUPTED` / `EXECUTION_RECOVERED`

**安全关键集合（`SAFETY_CRITICAL_KINDS`）**：
`{ACTION_DISPATCHED, RISK_ASSESSED, CONFIRMED, GOAL_CONFIRMED}`

> 这四类事件采用 **fail-closed**：写不进 durable store 就**不能继续往下走**。理由：
> 继续意味着危险动作可能在**没有判定记录**的情况下被放行，
> 事后审计无法回答「为什么让它过了」。

分级判定在 `EventLog.emit` **唯一写入口**：**先落盘、再生效**。

### 9.7 数据生命周期

| 数据 | 生命周期 | 说明 |
|---|---|---|
| 任务 | **永不自动删除** | 只有 `POST /cancel` 让它进 `CANCELLED`；`TaskStore.delete` 存在但未被 API 调用 |
| 恢复点 | 每任务保留最新；**孤儿在启动时清理** | `_prune_orphan_checkpoints()` |
| 事件 | **只追加，永不删除** | 回放的数据源 |
| 执行记录 | **永不删除** | `UNKNOWN` 需要人工处理，删了就不知道「手机被操作过」 |
| 确认票据 | 有 `expires_at`，但**记录不删** | 靠 `jti` 主键保证一次性 |
| 轨迹 | **会被裁剪** | 服务下一步决策，不是审计源 |
| 请求审计 | JSONL 按天分文件 | 写失败**永不抛异常** |

**实测数据量**（`artifacts/state/shadow.db`，303 KB）：`tasks=2`、`events=18`、
`executions=2`、`consumed_confirmation=2`、`checkpoints=0`。
两条任务都是 `failed`，两条执行都是 `FAILED`
（note 原因：`未在 UI 树中找到可点击元素: '设置图标（位于Dock栏第二个位置）'`）。
事件分布：`risk_assessed=3`、`started=3`、`action_dispatched=2`、`action_verified=2`、
`checkpoint_saved=2`、`failed=2`、`queued=2`、`observation_stale=1`、`recovered=1`。

**另有历史遗留库**（不是当前架构的一部分，可删）：
`artifacts/state/confirmations.db`（12 KB，V4 之前确认票据走独立文件）
与 `artifacts/state/events/events.db`（28 KB，空表）。

### 9.8 迁移机制

`storage/migrations/__init__.py`，用 `PRAGMA user_version` 记版本。**当前 v4**。
**新增表 / 改列必须写迁移**，不要只改 `CREATE TABLE` 语句——已有部署不会重建表。

---

## 十、API 接口

### 10.1 鉴权与访问控制（先读这一节）

**中间件**：`api/server.py:@app.middleware("http") async def access_control(...)`
——**统一入口**，所有请求都过它（含审计）。

**读取类操作白名单**（注意：**按操作能力判，不按 HTTP 方法判**）：

```python
_READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}
_READ_ONLY_ALLOWED_POST_PATHS = {"/screenshot", "/observe"}
```

理由：`POST /screenshot` 只是「截个图」，不是写操作。用 HTTP 方法判会把只读令牌挡在门外。

**身份模型**：`api/auth.py:Principal(name, read_only, devices)`，frozen dataclass。

`Principal` 的三个方法：
- `allowed_serials()` → 可用设备集；`None` 表示不限。**未指定设备时只能从这里挑**
  （以前只检查「指定的 serial 在不在列表里」，于是不指定就等于绕过）。
- `may_use_device(serial)` → `serial=None` 在受限令牌下返回 **False**
  （`None` 不等于「哪台都行」，而是「还不知道会是哪台」）。
- `may_access_task(task)` → **对象级**授权（读侧也要检查：
  `GET /tasks/{id}` 会带出待确认动作的信息）。

**两种配置方式**：
- **推荐**：`SHADOW_API_PRINCIPALS`（JSON）——每个 principal 有**自己的设备范围**：

  ```json
  {"alice":   {"token": "<令牌①>", "devices": ["phone-001"]},
   "bob":     {"token": "<令牌②>", "devices": ["phone-002"]},
   "auditor": {"token": "<令牌③>", "read_only": true, "devices": ["*"]}}
  ```

  `devices` 省略 / 空数组 / `["*"]` 都表示不限。

- **legacy**：`SHADOW_API_TOKEN` / `SHADOW_API_READONLY_TOKEN` + `SHADOW_API_DEVICE_ALLOW`
  （只能表达「所有令牌共用一份设备白名单」，保底兼容，新部署请用上面那种）。
  两者同时配置时**以 principals 为准，legacy 被忽略并告警**。

**配置错误必须 fail-closed**：`_parse_principals` 解析失败 → **整表作废**（所有请求 401），
而不是退回「没配鉴权」（那会把「配错了」变成「谁都能进」）。
原因出现在 `/health/detail` 的 `principals_error` 与启动日志里。

**启动期三道闸**（`api/server.py:__main__`）：
1. `auth.bare_bind_refused(HOST)` → 非回环地址且无令牌 → `SystemExit`。
2. `auth.config_error()` → principals 配错 → `SystemExit`。
3. 未启用鉴权 → **warning**（不阻止，但提醒仅建议本机）。

**令牌提取**（`extract_token`）：`X-API-Token` 头 或 `Authorization: Bearer xxx`，
也**容错接受裸令牌**（有些客户端不方便加 scheme）。
比较用 `hmac.compare_digest`（定长比较，防时序侧信道）。

**确认票据签名密钥**：配了 `SHADOW_API_TOKEN` / `SHADOW_CONFIRM_SECRET` 时稳定，
否则用**进程随机密钥** `secrets.token_hex(32)`——**重启即失效**。
本地场景这比「用固定弱密钥」安全得多。

### 10.2 核心 API 一览（实测提取的完整路由表）

| 方法 | 路径 | 用途 | 权限 | 主要参数 | 返回 |
|---|---|---|---|---|---|
| GET | `/devices` | 列出设备池 | 读 | — | 设备列表 |
| POST | `/tap` | 单步点击（调试） | 写 | `x`,`y`,`device_serial?` | ok |
| POST | `/text` | 单步输入（调试） | 写 | `value`,`device_serial?` | ok |
| POST | `/back` | 单步返回（调试） | 写 | `device_serial?` | ok |
| GET/POST | `/screenshot` | 截图 | **读** | `device_serial?` | PNG |
| GET/POST | `/observe` | 观察（截图+UI 树） | **读** | `device_serial?` | 观察结果 |
| POST | `/actions` | 执行单个动作（带执行记录） | 写 | `type`,`target?`,`value?`,`device_serial?` | 执行记录 |
| GET | `/executions` | 列出执行记录 | 读 | `limit=50`,`status=''` | 列表 |
| GET | `/executions/{id}` | 执行详情 | 读 | — | 记录 |
| POST | `/tasks` | **创建任务** | 写 | 见下 | Task（`mode`=sync/background） |
| GET | `/tasks` | 列出**有权访问**的任务 | 读 | — | `{tasks,count,corrupt}` |
| GET | `/tasks/{id}` | 任务详情 | 读（对象级） | — | Task + 附加字段（见下） |
| POST | `/tasks/{id}/pause` | 暂停 | 写 | — | Task |
| POST | `/tasks/{id}/resume` | 恢复 | 写 | — | Task |
| POST | `/tasks/{id}/cancel` | **取消（唯一结束任务的入口）** | 写 | — | Task |
| POST | `/tasks/{id}/confirmation-token` | **申请一次性确认令牌** | 写 | — | `{token,expires_in_seconds,kind,task_id}` |
| POST | `/tasks/{id}/confirm` | **人工处理待确认事项** | 写 | `approved`,`token?` | Task |
| POST | `/tasks/{id}/inject` | **执行中注入指令** | 写 + 设备授权 | `instruction`,`priority?`,`max_steps?`,`budget?`,`allow_disruptive?` | `InjectResult` |
| GET | `/tasks/{id}/history` | 观察轨迹（会被裁剪） | 读 | `limit=50` | 轨迹 |
| GET | `/tasks/{id}/events` | **审计事件流** | 读 | `limit=200` | 事件列表 |
| GET | `/tasks/{id}/replay` | 回放 | 读 | `format=json\|markdown`,`limit=1000` | 时间轴 / 人读报告 |
| GET | `/tasks/{id}/checkpoint` | 最新恢复点 | 读 | — | 恢复点摘要 |
| GET | `/tasks/{id}/shots/{n}` | 某步截图 | 读 | — | 图片文件 |
| GET | `/scheduler` | 调度器状态 | 读（按设备范围过滤） | — | 快照 |
| GET | `/health` `/healthz` | **探活（唯一匿名入口）** | **公开** | — | **只回 `{"ok":true}`** |
| GET | `/health/detail` | 诊断 | 读 | — | 见 10.4 |

**`POST /tasks` 的完整请求体**（`TaskRequest`）：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `instruction` | str | **必填** | 任务指令 |
| `context` | str | `""` | 附加上下文 |
| `max_steps` | int? (1–200) | None | **兼容旧调用方**，只映射到「动作步数」上限 |
| `budget` | `BudgetRequest?` | None | **推荐**：`max_action_steps` / `max_observations` / `max_model_calls` |
| `priority` | TaskPriority | `NORMAL` | |
| `wait` | bool | `False` | `True` = 同步等结果 |
| `wait_timeout` | float (1–600) | `120.0` | |
| `device_serial` | str? | None | 不填由调度器派给最闲的一台 |
| `execution_mode` | ExecutionMode | `FOREGROUND` | **严格枚举**（拼错 422） |
| `requires_user` | bool | `False` | 必须真人参与（生物识别/OTP/系统授权） |

> ⚠️ **`max_steps` 与 `budget` 的语义陷阱**（代码注释原文）：
> `max_steps` **只**决定动作步数，观察/模型调用取默认值 60 / 40。
> 「调用方以为『10 步 = 最多循环 10 次』，实际拿到的是 10 个动作 + 60 次观察 +
> 40 次模型调用」。**新代码请直接写 `budget`，别再用 `max_steps`。**

**`GET /tasks/{id}` 的附加字段**：`plan_progress`、`pending_confirmation`（**不含 token**，
附 `token_endpoint` 与 `token_expires_in_seconds`）、`confirmation_kind`、
`goal_verification`。
它还会**先探一次** `manager.get()`（隔离是惰性的，可能就发生在这次读取里），
否则第一次请求会 404、第二次才 `recovery_error`，前后不一致。

**`GET /tasks` 返回额外带 `corrupt`**：损坏任务不会出现在 `tasks` 里（反序列化不出来），
但必须让调用方知道「有这么一条」，否则它就从系统里**静默消失**了。
**受限令牌看不到这份清单**（无法判断归属，宁可少给）。

**`/health` 刻意只回 `{"ok":true}`**（代码注释）：
- `auth`：等于告诉匿名者「这个实例有没有开鉴权」，也就告诉他值不值得试；
- `host` / `device_backend`：暴露部署形态（ADB 还是手机本机端点）；
- `principals_error`：暴露「配置坏了、此刻所有请求都 401」。

详细诊断搬到 `GET /health/detail`，**需要鉴权**。
拆分的理由是**读者不同**：探活的是机器，诊断的是运维本人。

### 10.3 关键接口的调用链

**`POST /tasks`**：

```
客户端 → api/server.py:create_task
  → current_principal + allowed_devices_for + require_device_access  [鉴权 3 步]
  → TaskManager.create(..., execution_mode=req.execution_mode, requires_user=...)
      → TaskStore.save  →  shadow.db tasks 表
      → TaskScheduler.submit  →  设备 lane 队列
  → 若 wait：_wait_for(task.id, timeout) → scheduler.wait_terminal()
  → 响应 Task（mode=sync|background）
```

**`POST /tasks/{id}/confirm`（最有代表性的一个）**：

```
客户端
  → require_task_access(task_id, request)                    对象级授权
  → manager.confirmation_kind(task_id)                       确认类型
  → if auth.enabled(): auth.reserve_confirmation_token(...)  两阶段：预占（不作废）
  → with db.transaction():                                   ★ 一个事务
      ├─ manager.resolve_confirmation(approved=...)          改 Task 状态（含 CAS）
      ├─ 写 CONFIRMED 事件（安全关键，fail-closed）           写不下整笔回滚
      └─ auth.commit_confirmation_token(jti)                 作废票据
  → 响应 {ok, approved, kind, task}
```

**两种跨存储不一致因此都不可能发生**（注释原文）：
「Task 已确认 / Token 未消费」不可能；「Token 已消费 / Task 没确认」不可能。
409 那条路径**也不再需要显式 `release`**：回滚本身就把预占一起撤销。

**`POST /tasks/{id}/inject`**：
`require_task_access` **必须在** `manager.inject()` **之前**（见 6.9）。
注入结果还会**防御性复核**是否落在未授权设备上——不正常时记 ERROR 并返回 403
（「但它不是唯一的一道，判权限的主战场在前面」）。

### 10.4 `/health/detail` 的字段（排障第一入口）

```json
{
  "ok": true,
  "auth": "token" | "disabled",
  "host": "127.0.0.1",
  "confirmation_consumption": "<后端类名>",
  "device_backend": "adb（PC 端控制，设备 xxx）| android（手机本机运行，设备标识 xxx）",
  "principals_error": null,
  "executions_effect_unknown": 0
}
```

**三个最该看的字段**：
- `confirmation_consumption`：**只有它不是 `InMemoryConsumption` 时，「一次性令牌」才跨重启成立**。
  暴露名字的理由：「我们到底配的是哪个实现」应该能被查到，
  而不是靠人记住几个月前启动时设了什么环境变量。
- `principals_error`：非 null ⇒ **此刻所有请求都在被 401**（配置错了）。
- `executions_effect_unknown`：**长期不为 0 就说明有人在拖着不处理**
  （`GET /executions?status=UNKNOWN` 列出它们）。

### 10.5 错误码约定

| 状态码 | 含义 | 来源 |
|---|---|---|
| 401 | 未通过鉴权 / principals 配置错误 | `access_control` 中间件 |
| 403 | 授权不足（对象级 / 设备范围 / 确认未通过 / 越权注入） | `_audit_denied`、`reserve_confirmation_token` 失败 |
| 404 | 资源不存在 / **受限令牌对损坏任务一律 404**（防存在性泄露） | `require_task_access` |
| 409 | 状态冲突（暂停/恢复/取消时任务已结束；无待确认事项） | 各 handler 显式 `HTTPException` |
| 422 | 请求体不合法（如 `execution_mode` 拼错——**严格枚举，不静默降级**） | pydantic |
| 502 | 设备后端失败 | `device_error_handler`（注册在 `DeviceError` **基类**上，两个后端共用） |
| 503 | VLM 调用失败 | `vlm_error_handler` |
| 500 | 兜底（对外**默认脱敏**，`SHADOW_DEBUG=1` 才回传摘要） | `unhandled_error_handler` |

**502/503 的注册位置是有讲究的**：`device_error_handler` 注册在**端口基类** `DeviceError` 上
——`AdbError` 与 `AndroidBridgeError` 都是它的子类，所以换后端不必动这里。
以前只注册 `AdbError`，Android 后端一出错就会掉进 500 兜底。

---

## 十一、配置说明

### 11.1 配置方式

**全部走环境变量**。仓库里**没有** `.env`、`.env.example`、YAML、TOML 运行时配置文件
（`pyproject.toml` 只管打包与依赖）。
**没有 `.env.example` 可以照抄——这是交接时的一个缺口。**

### 11.2 环境变量全清单（由全仓库 `os.getenv` 扫描得出，含出处）

> 表格里的值都是「变量名 / 用途 / 是否必须 / 示例」。
> **真实密钥一律不写**——仓库中**未发现**任何硬编码的生产密钥。

#### VLM / 模型

| 变量 | 用途 | 必须 | 示例 | 读取位置 |
|---|---|---|---|---|
| `VLM_BASE_URL` | OpenAI 兼容接口地址 | ✅ | `https://<你的网关>/v1` | `vision/vlm.py`、`scripts/vlm_selftest.py` |
| `VLM_API_KEY` | 接口密钥 | ✅（否则 Classifier 退化） | `<请配置>` | `vision/vlm.py`、`api/server.py` |
| `VLM_MODEL` | 模型名 | ✅ | `<模型名>` | `vision/vlm.py`、`scripts/vlm_selftest.py` |

#### 设备

| 变量 | 用途 | 必须 | 示例 | 读取位置 |
|---|---|---|---|---|
| `SHADOW_DEVICE_BACKEND` | `adb`（默认）/ `android`；**写错当场报错** | ❌ | `adb` | `device/factory.py` |
| `ADB_SERIAL` | 目标设备 serial，**逗号分隔多台** | ❌ | `R5CTxxxx,emulator-5554` | `device/emulator.py`、`device/factory.py` |
| `SHADOW_ANDROID_BRIDGE_URL` | 路线 B 设备端点地址 | android 后端必填 | `http://192.168.1.20:8765` | `device/remote.py` |
| `SHADOW_ANDROID_BRIDGE_TOKEN` | 路线 B 端点令牌 | 端点要求时必填 | `<手机页面显示的令牌>` | `device/remote.py` |

#### 存储与产物

| 变量 | 用途 | 必须 | 示例 | 读取位置 |
|---|---|---|---|---|
| `STORAGE_DIR` | 持久化目录（`shadow.db` 所在） | ❌ | `artifacts/state` | `api/server.py`、`scripts/replay_task.py` |
| `ARTIFACT_DIR` | 截图目录 | ❌ | `artifacts/shots` | `agent/runtime.py`、`api/server.py` |
| `AUDIT_DIR` | 请求审计目录 | ❌ | `<STORAGE_DIR>/audit` | `api/server.py` |
| `SHADOW_CONFIRM_DB` | 覆盖确认票据库位置 | ❌ | `<path>/shadow.db` | `api/server.py` |

> ⚠️ **Android 上 `ARTIFACT_DIR` 必须指到应用私有目录**（如 `filesDir/state`），
> **不能是工作目录**——Android 上不可写。

#### HTTP 服务

| 变量 | 用途 | 必须 | 默认 | 读取位置 |
|---|---|---|---|---|
| `HOST` | 监听地址 | ❌ | `127.0.0.1` | `api/server.py` |
| `PORT` | 监听端口 | ❌ | `8010` | `api/server.py` |
| `SHADOW_DEBUG` | 对外回传异常摘要（默认脱敏） | ❌ | 未设 | `api/server.py` |
| `SHADOW_AUDIT` | 请求审计开关（`0` 关闭） | ❌ | `1`（开） | `api/server.py` |

#### 鉴权与确认

| 变量 | 用途 | 必须 | 读取位置 |
|---|---|---|---|
| `SHADOW_API_PRINCIPALS` | **推荐**：JSON 身份表（含每身份设备范围） | 生产必填 | `api/auth.py` |
| `SHADOW_API_TOKEN` | legacy 主令牌 | 二选一 | `api/auth.py` |
| `SHADOW_API_READONLY_TOKEN` | legacy 只读令牌 | 二选一 | `api/auth.py` |
| `SHADOW_API_DEVICE_ALLOW` | legacy 全局设备白名单 | ❌ | `api/auth.py` |
| `SHADOW_API_TOKEN_NAME` | legacy 主令牌的身份名（默认 `operator`） | ❌ | `api/auth.py` |
| `SHADOW_REQUIRE_AUTH` | 置 1 时**无令牌也拒绝一切请求** | ❌ | `api/auth.py` |
| `SHADOW_CONFIRM_SECRET` | 确认票据签名密钥（不设则用进程随机密钥） | ❌ | `api/auth.py` |
| `SHADOW_CONFIRM_TTL_SECONDS` | 确认票据有效期（默认 `300`） | ❌ | `api/auth.py` |
| `SHADOW_CONFIRM_RESERVE_TTL_SECONDS` | 预占超时 | ❌ | `storage/confirmation_store.py` |

#### 并发与恢复

| 变量 | 用途 | 必须 | 默认 | 读取位置 |
|---|---|---|---|---|
| `WEB_CONCURRENCY` / `UVICORN_WORKERS` / `GUNICORN_WORKERS` | worker 数；**>1 拒绝启动** | ❌ | 未设 | `api/server.py` |
| `SHADOW_ALLOW_MULTI_PROCESS` | 显式承担多进程风险，跳过上述硬闸 | ❌ | 未设 | `api/server.py` |
| `SHADOW_EXECUTION_STALE_SECONDS` | 执行恢复宽限窗口 | ❌ | 单进程 0 / 多进程 300 | `api/server.py` |

#### 行为调优

| 变量 | 用途 | 默认 | 读取位置 |
|---|---|---|---|
| `GOAL_VERIFY_MODE` | 完成验证严格度：`auto`/`off`/`advisory`/`strict` | `auto` | `agent/goal_policy.py`、`goal_verifier.py` |
| `SHADOW_TOCTOU_GUARD` | 置 `0` 关闭执行前复查（**不推荐**） | 开 | `agent/_execution.py` |
| `OBSERVE_BUDGET_SECONDS` | 一次采集总预算（秒） | `12` | `agent/observer.py` |
| `ADB_READ_TIMEOUT_SECONDS` | 采集类 ADB 读超时（秒） | `6` | `agent/observer.py` |

**`OBSERVE_BUDGET_SECONDS` 的意义**（注释原文）：采集要连着做若干设备操作
（ADB 后端实际是 6 条命令：截图 / `wm size` / `dumpsys window` / `rm` /
`uiautomator dump` / `cat`），逐条各自卡住就是累加。给了总预算之后，
每条操作的超时取 `min(自己的超时, 剩余预算)`，于是**一次采集的耗时有上界**，
抢占延迟也才有可论证的上界——从「逐条累加」降到「预算 + 1 条」。

#### 其他

| 变量 | 用途 | 读取位置 |
|---|---|---|
| `HOSTNAME` | 租约的持有者标识 | `storage/lease_store.py` |

#### Android 构建（仅打 APK 时需要）

| 变量 | 用途 | 读取位置 |
|---|---|---|
| `SHADOW_SDK_DIR` | Android SDK 目录 | `android/tools/_toolchain.py` |
| `SHADOW_BUILD_TOOLS_DIR` | build-tools 目录 | 同上 |
| `SHADOW_KOTLINC_DIR` | kotlinc 目录 | 同上 |
| `SHADOW_JAVA` | JDK 路径（默认用 PyCharm `jbr`） | 同上 |
| `SHADOW_ANDROID_JAR` | `android.jar` 路径 | 同上 |
| `SHADOW_DOWNLOAD_PROXY` | 工具链下载代理 | 同上 |

### 11.3 敏感信息检查结果

**扫描范围**：`.py` / `.kt` / `.kts` / `.sh` / `*.json` / `*.gradle.kts`。
**结论：仓库中未发现硬编码的生产密钥**（无 API Key、无 Token、无 Password、无私钥、
无 Cookie）。默认值与测试用的都是占位常量。

**但有三处需要注意**（不是泄漏，是「约定」）：

1. `artifacts/vlm_env.sh` 与 `artifacts/phone_env.sh` **含真实密钥与令牌**——
   它们**在 `.gitignore` 内**（`artifacts/` 整个被排除），所以没有进仓库。
   **但这意味着这两个文件不在版本控制里**，换机器/重装就没了，需要重建。见 11.5。
2. 调试签名密钥已挪到 `~/.android/debug.keystore`（AGP 惯例位置），**不在仓库里**
   ——这也是有意的。原因：原先放在构建临时目录 `WORK` 里，而 `main()` 开头就 `rmtree(WORK)`
   ——等于**每次构建都换一把密钥**，`adb install -r` 必然撞
   `INSTALL_FAILED_UPDATE_INCOMPATIBLE`。
3. **`X-Shadow-Token`（手机设备端点令牌）由应用首次启动时随机生成并存本机设置**，
   不在代码里。

### 11.4 配置错误的处置原则（贯穿全项目）

**配置错误一律 fail-closed，绝不静默退化**：
- `SHADOW_DEVICE_BACKEND` 写错 → `UnknownDeviceBackend`，启动期报错。
- android 后端但两条桥都没有 → `AndroidServiceUnavailable`，
  错误信息里**列出两条路线怎么做**（这是运维第一次部署时唯一会看到的东西）。
- `SHADOW_API_PRINCIPALS` 配错 → 整表作废，所有请求 401，原因可查。
- `HOST` 非回环 + 无令牌 → 拒绝启动。
- worker > 1 → 拒绝启动。

**android 后端刻意不「退回 adb」**：「选了 android 后端说明部署者认为手机是本机/端点，
静默换成 adb 会让整条链路去操作一个完全不同的设备（PC 上的 adb 目标）」。

### 11.5 演示环境变量文件（**接管演示必须知道**）

`artifacts/vlm_env.sh` + `artifacts/phone_env.sh`（两文件在 `.gitignore` 内）。
`scripts/start_demo_core.py` 会**逐行解析** `export KEY=VALUE` 并 `strip()`，
**刻意不 `source`**：因为 Windows 上写出的文件可能是 CRLF，
`source` 会把 `\r` 塞进变量值 → 表现是「令牌明明抄对了却一直 401」。

`REQUIRED` 列表（缺任一则该脚本返回码 2）：
`VLM_BASE_URL`、`VLM_API_KEY`、`VLM_MODEL`、`SHADOW_DEVICE_BACKEND`、
`SHADOW_ANDROID_BRIDGE_URL`、`SHADOW_ANDROID_BRIDGE_TOKEN`。

脚本还会打印「在**手机上**发任务」的做法：
`adb -s <序列号> reverse tcp:8000 tcp:8000`，然后手机浏览器开 `http://127.0.0.1:8000/docs`
（走 adb 通道，绕开 Windows 防火墙；adb 一断 reverse 就没了）。

---

## 十二、本地开发环境

### 12.1 环境要求

| 项 | 要求 | 依据 |
|---|---|---|
| OS | Windows / macOS / Linux 均可（**开发主要在 Windows**） | 代码中 `pathlib` + `os.getenv`，无平台特有依赖 |
| Python | **>= 3.11** | `pyproject.toml` |
| ADB（可选） | 用 `adb` 后端时需要，且 `adb` 在 PATH 里 | `device/adb.py` |
| Android 真机（可选） | 开 USB 调试 | `README.md` |
| JDK 17+（仅打 APK） | 或 PyCharm 自带 `jbr` | `android/README.md` |
| 网络 | 调 VLM 需要能访问 `VLM_BASE_URL` | |

**无需**：Docker、数据库服务、Redis、消息队列、Node.js。

### 12.2 依赖安装

```bash
# ① 运行依赖（版本钉死）
pip install -r requirements.txt

# ② 测试依赖（需要跑测试时；它已经 -r requirements.txt）
pip install -r requirements-dev.txt

# 或：用 uv 按 uv.lock 装出一模一样的环境
uv sync
```

### 12.3 测试恢复（**接手第一件事**）

```bash
# tests/ 与 docs/、DEVLOG.md、bluewhale-shadow-phone/ 都在 .gitignore 里
# 换机器后仓库里没有测试。从历史提交取回：
git log --oneline | head -20                    # 找一个含 tests 的提交
git checkout <commit> -- tests

# 确认恢复成功
python -m pytest --collect-only -q | tail -3    # 应看到 "799 tests collected"
python -m pytest -q                             # 应看到 "799 passed"
```

> `scripts/` **是随仓库发布的**（`.gitignore` 只忽略 `scripts/__pycache__/`），
> 所以只需取回 `tests`。

### 12.4 启动命令（逐步可执行）

**方式一：最小启动（本机调试，无需鉴权）**

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 配置 VLM
export VLM_BASE_URL="https://<你的网关>/v1"
export VLM_API_KEY="<你的密钥>"
export VLM_MODEL="<模型名>"
# Windows PowerShell 用：$env:VLM_BASE_URL="..."

# 3. （可选）ADB 后端需要指定设备
adb devices
export ADB_SERIAL="<从上面拿到的 serial>"

# 4. 启动（默认 127.0.0.1:8010）
python -m api.server
```

**方式二：用演示脚本启动（自动加载 `artifacts/*.sh`）**

```bash
python scripts/start_demo_core.py                # 默认 127.0.0.1:8000
python scripts/start_demo_core.py --port 8000
python scripts/start_demo_core.py --print-only   # 只检查变量，不启动
```

`start_demo_core.py` 会先 `os.chdir(项目根)` 并把项目根插入 `sys.path`
（否则 `uvicorn.run("api.server:app")` 会报 `ModuleNotFoundError: No module named 'api'`）。

**方式三：有鉴权的启动（生产 / 局域网）**

```bash
export SHADOW_API_TOKEN="<强随机令牌>"
# 或推荐方式：
export SHADOW_API_PRINCIPALS='{"operator":{"token":"<令牌>","devices":["phone-001"]}}'
export HOST=0.0.0.0
export PORT=8010
python -m api.server
```

**方式四：uvicorn 直接跑**

```bash
python -m uvicorn api.server:app --host 0.0.0.0 --port 8000 --workers 1
```

> ⚠️ **不要省掉 `--workers 1`**（或干脆不写，默认就是 1）。
> 写了 `--workers 2` 会被 `_guard_single_process()` 拒绝启动——这是**有意**的。

### 12.5 第一次跑通（验收清单）

```bash
# ① 服务活着（匿名端点）
curl http://127.0.0.1:8010/health          # → {"ok":true}

# ② 看诊断（开了鉴权要带 token）
curl -H "X-API-Token: <令牌>" http://127.0.0.1:8010/health/detail
#    重点看 device_backend 与 executions_effect_unknown

# ③ 看设备
curl http://127.0.0.1:8010/devices

# ④ 起一个任务（后台）
curl -X POST http://127.0.0.1:8010/tasks \
  -H "Content-Type: application/json" \
  -d '{"instruction":"打开设置，查看电池电量"}'
# → 拿到 task_id

# ⑤ 轮询
curl http://127.0.0.1:8010/tasks/<task_id>

# ⑥ 看事件流（排障主入口）
curl http://127.0.0.1:8010/tasks/<task_id>/events | python -m json.tool

# ⑦ 人读回放报告（开头就是「值得注意的地方」）
curl "http://127.0.0.1:8010/tasks/<task_id>/replay?format=markdown"
```

**一个命令拿最终状态**：

```bash
curl -X POST http://127.0.0.1:8010/tasks \
  -H "Content-Type: application/json" \
  -d '{"instruction":"打开设置","wait":true}'
```

**接口文档**：`http://127.0.0.1:8010/docs`（FastAPI 自带 Swagger UI，无需额外配置）。

### 12.6 完全离线的自检（不需要 adb / 模拟器 / API Key）

```bash
# ① 抢占与恢复演示：A 跑到一半，B 插入 → A 让出设备 → B 完成 → A 从恢复点继续
python scripts/demo_preemption.py

# ② 回放某个真实任务的事件流
python scripts/replay_task.py --list
python scripts/replay_task.py <task_id>
python scripts/replay_task.py <task_id> --json

# ③ 跑测试（也是完全离线的）
python -m pytest -q
```

> `replay_task.py` 默认读 `$STORAGE_DIR/events/`，即 `artifacts/state/events`。

**演示 ① 的关键输出**（`README.md` 记录）：

```
用户：#1 帮我在淘宝搜索一双黑色运动鞋       → 任务 A 开始执行
  [设备] tap(300,800) ...
用户：#2 先帮我打开微信给张三发"晚上开会"    → 任务 B（HIGH）插入
  请求任务 A 让出设备，等待方 B
  任务 A 已挂起（让出设备），等待恢复
  开始执行任务 B ... 完成
  开始执行任务 A ... 完成                     → 从恢复点继续
```

**「危险动作会停住等人确认」那条**（发消息是 DANGEROUS、`submit` 语义）
由 `tests/test_scenarios.py` 覆盖——演示与测试用的是同一套 Runtime，
区别只在测试用 `FakeDevice` 断言、脚本用 `PrintedDevice` 打印。

### 12.7 运维脚本（需要真环境）

**VLM 连通性自检**（打真模型，只读，不操作设备）：

```bash
python scripts/vlm_selftest.py                          # 基础连通
python scripts/vlm_selftest.py --image <截图路径>        # 多图能力
python scripts/vlm_selftest.py --model <模型名> --base-url <地址>
```

它测四件事：端点可达、延迟相对 60 秒超时的余量、JSON 纪律、多图支持。

**真机演示预检**（全链路体检，只读）：

```bash
python scripts/phone_demo_preflight.py --bridge-url http://<手机IP>:8765 --token <令牌>
```

检查顺序：adb → APK → 辅助功能 → 投屏授权 → 端点 → Core → VLM。

---

## 十三、启动与运行

### 13.1 启动顺序（`api/server.py` 的实际执行序）

```
模块导入期
  ├─ _guard_single_process()                          ★ 多 worker 直接 RuntimeError
  ├─ build_controller(resolve_device_serial())        ★ 设备后端在这里选定并校验端口完整性
  ├─ DevicePool([DeviceSession(...) for serial ...])  ★ ADB_SERIAL 逗号分隔 → 多设备
  ├─ session = device_pool.first()                    单步调试端点用的「主设备」
  ├─ db = Database(STORAGE_DIR)                       ★ 建连接 + PRAGMA + 跑迁移
  ├─ event_log = EventLog(db)                         ⚠️ 必须**先于** task_store
  ├─ task_store = TaskStore(db, event_log=event_log)
  ├─ checkpoint_store = CheckpointStore(db)
  ├─ execution_store = ExecutionStore(db)
  ├─ execution_service = ExecutionService(execution_store, event_log=event_log)
  ├─ auth.configure_consumption(ConfirmationConsumptionStore(_confirm_db or db))
  ├─ trajectory = TrajectoryStore(root=STORAGE_DIR/"trajectories")
  ├─ audit_log = AuditLog(AUDIT_DIR)
  ├─ runtime = AgentRuntime(device_pool, ...)
  ├─ scheduler = TaskScheduler(runtime, device_pool, ...)
  ├─ classifier = TaskClassifier(llm_judge=... if VLM_API_KEY else None)
  └─ manager = TaskManager(store, scheduler, classifier, runtime)

lifespan（FastAPI 启动）
  ├─ _prune_orphan_checkpoints()    启动清理孤儿恢复点（失败不挡启动）
  ├─ recover_stale_executions()     启动恢复崩溃遗留执行
  └─ scheduler.start()              拉起每台设备一条 lane 的 worker 线程
  ...
  finally: scheduler.stop()
```

> ⚠️ **`event_log` 必须先于 `task_store` 构造**。原因：`TaskStore` 发现损坏任务时
> 要在这里留一条 `TASK_CORRUPTED`。

> ⚠️ **没有 `VLM_API_KEY` 时 `classifier` 自动退化为「规则 + 相似度」两层**——
> 不会报错，只是关系判定变弱。这是设计，不是故障。

### 13.2 关闭顺序

`lifespan` 的 `finally` 里 `scheduler.stop()`。
**没有「优雅关闭 + 等待在跑任务落检查点」的逻辑**——【代码推断】在跑任务被中断后
靠启动恢复兜底（这正是执行状态机存在的理由）。

### 13.3 多设备运行

```bash
export ADB_SERIAL="R5CTxxxx,emulator-5554,emulator-5556"
python -m api.server
```

调度器会为**每台设备**起一条 lane（`_ensure_lane`），各自独立排队与执行。
指定设备：`POST /tasks {"instruction":"...","device_serial":"R5CTxxxx"}`。

**已绑定的任务绝不回退到别的设备**（`runtime._session_for` 注释）：
绑定设备不在池里 → 抛 `DeviceUnavailableError` → 落 `DEVICE_UNAVAILABLE` 等原设备回来。
理由：跨设备上下文污染（A 任务停在微信页面，把 A 的恢复点拿到 B 上接着点，
等于把任务丢进别人的手机）。**只有尚未绑定的任务才允许分配默认设备。**

多设备下的负载均衡现状：**只挑最闲的一条，未考虑异构**；
`device.pool.storage_hint` **未接线**（存在但没被调用）。

### 13.4 Android 后端运行（路线 B）

```bash
# 手机侧：装 APK → 开辅助功能 → 授权屏幕捕获 → 启动设备端点 → 记下地址与令牌
# PC 侧：
export SHADOW_DEVICE_BACKEND=android
export SHADOW_ANDROID_BRIDGE_URL=http://192.168.1.20:8765
export SHADOW_ANDROID_BRIDGE_TOKEN="<手机页面显示的令牌>"
python -m api.server
```

自检：`curl http://192.168.1.20:8765/health`（带令牌头）应回
`{"ok":true,"value":{"state":"device",...}}`；`state` 不是 `device` 说明权限没齐。

**⚠️ 端点等于「操作这台手机」的能力**——`POST /bridge/tap {"x":680,"y":1200}`
就能点到屏幕上任意位置。所以：必须带 `X-Shadow-Token`、
**只在用户主动点「启动设备端点」时监听**（前台服务一停就关，且 `START_NOT_STICKY`）、
**只在可信局域网内使用，绝不映射到公网**。

### 13.5 设备端点的两条承载路线（路线 A vs B）

| | 路线 A：同进程（Chaquopy） | **路线 B：设备端点（当前默认）** |
|---|---|---|
| 拓扑 | APK 里同时有 Kotlin 设备层和 Python Core | 手机只当设备端点；Core 跑在 PC/局域网 |
| 设备层怎么被调用 | Chaquopy 把 Kotlin 对象注册给 Python | HTTP（`/bridge/<方法名>`），Core 侧用 `device/remote.py` |
| 现在能不能跑 | **不能**（见下） | **能** |
| Python 侧要改什么 | 无（`register_android_bridge` 已就绪） | 无 |

**路线 A 不可用的原因（已查证，不是猜测）**：把 Python Core 打进 APK 需要 `pydantic` 2.x，
而 `pydantic-core` 是 **Rust 扩展**，PyPI 上没有 Android 轮子。
Chaquopy 官方仓库只收录他们已构建好的原生包；维护者原话是
「Pydantic version 2 isn't currently available for Chaquopy」。
而 Shadow 的 `models/` **每个模型都是 pydantic `BaseModel`**，
`agent/`、`api/`、`storage/` 全都依赖它。
所以这不是「换个包」的问题，而是「核心的模型层要不要重写」的问题。

**选哪条由 `resolve_android_bridge()` 决定：先同进程、后远程**。
理由是**就近优先**（同进程没有网络这一跳，也不需要在手机上开监听端口，攻击面更小）。
两条都不可用时报错，并把两条路线的做法都写在错误信息里。

---

## 十四、测试

### 14.1 现状

**本项目有系统化自动化测试，共 799 个用例（pytest item），本次交接实测全绿。**

```bash
# 全量（本次实测：799 passed, 2 warnings in 38.69s）
python -m pytest -q

# 只看用例数
python -m pytest --collect-only -q | tail -3     # → 799 tests collected

# 分文件跑
python -m pytest tests/test_execution.py -q
python -m pytest tests/test_api_auth.py -q
python -m pytest tests/test_risk_gate.py -q
```

**关键前提：全离线**。所有测试都用 `tests/fakes.py` 里的 `FakeDevice` / `FakeBridge`
+ 打桩 VLM，**不需要 adb、不需要模拟器、不需要 API Key、不产生真实副作用**。

**那 2 个 warning 是第三方弃用警告**（`starlette/testclient.py` 的
`anyio.abc.BlockingPortal` 别名弃用），与本项目代码无关。

### 14.2 测试分层（38 个文件）

| 类型 | 文件 | 说明 |
|---|---|---|
| 单元 | `test_models.py`、`test_execution_state.py`、`test_risk_gate.py`、`test_semantic.py`、`test_evidence.py`、`test_vision.py` | 纯逻辑 |
| 状态机 | `test_execution.py`、`test_execution_faults.py`、`test_execution_recovery.py` | 迁移、终态守卫、崩溃恢复 |
| 存储 | `test_database.py`、`test_task_store.py`、`test_event_log.py`、`test_checkpoint_store.py`、`test_confirmation_store.py`、`test_lease_store.py`、`test_trajectory_store.py` | 事务、CAS、租约、两阶段 |
| 设备 | `test_device.py`、`test_device_pool.py`、`test_device_port.py`、`test_android_adapter.py`、`test_android_bridge_contract.py`、`test_android_remote_bridge.py` | 端口协议 + Android 契约 |
| API | `test_api.py`、`test_api_auth.py`、`test_api_authz.py` | 接口、鉴权、授权 |
| 运行时 | `test_runtime.py`、`test_scheduler.py`、`test_scenarios.py`、`test_shadow_actions.py` | 循环、调度、端到端场景 |
| 目标与策略 | `test_goal_oracle.py`、`test_goal_policy.py`、`test_goal_verifier.py` | 裁定分层 |
| 回放 | `test_replay.py` | 时间轴与计划 |
| 多设备 | `test_multi_device_e2e.py` | 端到端 |
| 不变量 | `test_invariants.py` | 跨模块不变量 |

**没有的东西**：没有覆盖率门槛配置、没有 `pytest.ini` / `[tool.pytest]` 段、
没有性能测试、没有真机 E2E 自动化测试（真机验证是**手工**的）。

### 14.3 Kotlin 侧测试

```bash
# 静态契约（零下载，每次提交都跑）
python -m pytest tests/test_android_bridge_contract.py -q

# 真编译 + JVM 单测（首次约下载 125MB 工具链）
python android/tools/verify_kotlin_compile.py
#   [1/3] android.jar 就位
#   [2/3] 生成 R/BuildConfig 桩（string=27 id=9 layout=1）
#   [3/3] 编译全部 Kotlin 源码 … 编译通过：34 个 class
#   运行 JVM 单测 … OK（15 条）

# 有 SDK 时
cd android && ./gradlew :app:test && ./gradlew :app:assembleDebug
```

**两边合起来才是闭环**：
- Kotlin 侧证明「序列化器输出的 XML == golden 文件」；
- Python 侧把 golden 文件喂给**真实的** `vision.target` / `agent.evidence` /
  `agent.risk_gate`，证明「这份 XML 是有用的」；
- 再加上方法名 / 参数个数 / 属性集合的静态比对，把「跨语言改名」「漏输出一个属性」
  这类**不会报错只会变松**的漂移挡住。

### 14.4 测试纪律（**这些是踩过坑换来的，请遵守**）

1. **报「测试数」永远报 pytest 的 item 口径。**
   用 `grep '^def test_'` 会**漏掉缩进的嵌套定义**（曾据此得出 770，与 799 矛盾）。
   正解：`pytest --collect-only -q`。
   实测 799 item = **765 个基础函数 + 34 个参数化展开**（41 个 item 带 `[参数]`）。

2. **直接调 `runtime.run(task)` 的用例必须先 `session.acquire(task.id)`**。
   漏了这一步，`_execute` 会以 `DeviceBusyError` 收场，用例**看起来通过**，
   其实**设备一次都没被碰过**。判据：断言过 `session.controller.events` 非空。

3. **改动前后各跑一次全量**，新增用例数应等于 diff 里新 test 函数的个数。
   如果改语义带动了老用例，那是预期不是回归，但要同步更新并在 commit message 里点名。

4. **守卫写完要做变异验证**。V5 修复轮对 5 个关键不变量手工注入变异，
   确认全部被抓住。「新写的守卫」与「有效的守卫」是两件事。

5. **一个位置有两道都会抛的关卡时，`pytest.raises(SomeError)` 测不出其中任何一道**。
   必须用「另一道必然通过」的输入 + 断言错误消息指向被测的那一道。

6. **替身必须复用真实校验逻辑，并如实模拟主代码行为**（高频坑）：
   - `make_adb._run` 要收 `timeout`；
   - `TaskStore.save` 替身要收 `expected_revision`；
   - 主代码开始观察就要给 `FakeDevice` 补 `dump_ui()`；
   - 注册桥要用 `register_android_bridge_object`（**对象不是 callable**）；
   - 假 runtime 若「不改任务状态就返回」，`suspended_by_user` 用例会误判；
   - **替身里的「诱饵记录表」必须在 `__init__` 初始化**，
     否则「没被调过」的断言会以 `AttributeError` 出现（测的成了拼错的属性名）。
     另一面：**构造期守卫能让「不完整替身」的前提根本不可达**——
     改成「构造完整对象后把属性置成不可调用」。

7. **凡「体积 / 行数 / 文件数」这类随版本漂移的数字，落笔前一律实测**，不要信记忆。
   （曾把 APK 写成记忆里的 49 KB，实测 796.6 KB。）

---

## 十五、部署流程

### 15.1 部署现状（**先说清楚：没有容器化，没有 CI/CD**）

**全仓库扫描结果**（已逐项确认不存在）：

| 文件 / 目录 | 状态 |
|---|---|
| `Dockerfile` / `docker-compose.yml` / `docker-compose.yaml` / `.dockerignore` | ❌ 不存在 |
| `.github/`（GitHub Actions） | ❌ 不存在 |
| `.gitlab-ci.yml` / `Jenkinsfile` / `.circleci/` | ❌ 不存在 |
| `k8s/` / `helm/` / `deploy*/` / `ci/` | ❌ 不存在 |
| `Makefile` / `Procfile` / `nginx.conf` | ❌ 不存在 |
| `.env` / `.env.example` / `.env.sample` | ❌ 不存在 |

**因此本项目有两种真实部署方式，没有第三种**：

1. **源码直跑**（开发 / 演示）：`pip install -r requirements.txt` → 设环境变量 →
   `python -m api.server`
2. **手机端点部署**（路线 B）：PC 跑 Core，手机装 APK 当设备端点

### 15.2 方式一：源码直跑（PC 上跑 Core）

```bash
# 1. 取代码
git clone <仓库地址> && cd Shadow

# 2. 装依赖（版本已钉死）
pip install -r requirements.txt

# 3. 配环境变量（见第十一章完整清单）
export VLM_BASE_URL="https://<网关>/v1"
export VLM_API_KEY="<请配置>"
export VLM_MODEL="gpt-4o"
export SHADOW_DEVICE_BACKEND="adb"
export ARTIFACT_DIR="$(pwd)/artifacts"

# 4. 启动（默认 127.0.0.1:8010）
python -m api.server

# 5. 健康检查
curl -s http://127.0.0.1:8010/health
# 期望：{"ok":true}
```

**`python -m api.server` 会在启动时执行三重守卫**（`api/server.py` `__main__` 段，L1697–1786）：

1. `_guard_single_process()` —— 见 4.3，多 worker 直接拒绝启动；
2. `bare_bind_refused()` —— 监听 `0.0.0.0` 且未配置令牌时拒绝启动
   （[V3.2 §六] 裸奔即默认拒绝：无鉴权暴露到局域网 = 谁都能点你的手机）；
3. `device/factory.py` 的 `selected_backend()` —— 后端名不认识时抛
   `UnknownDeviceBackend`，**不退化到 adb**。

**镜像 / 制品名**：本方式**不产出镜像**。产物就是源码目录 + `artifacts/` 运行时数据。

**依赖**：Python ≥3.11（`pyproject.toml` `requires-python`），4 个直接依赖
（`fastapi==0.141.1`、`uvicorn[standard]==0.52.4`、`pydantic==2.13.5`、`httpx==0.28.1`），
另有 `uvicorn[standard]` 带入的 `uvloop`/`httptools`/`watchfiles` 等间接依赖；
测试另需 `pytest==9.1.1`（`requirements-dev.txt`）。

**环境变量**：见第十一章，**无一样有代码内默认密钥**，`VLM_API_KEY` 不配则
`classifier` 走规则路径、`vision/vlm.py` 调用会失败（见 17.3）。

**启动命令**：`python -m api.server`（也可 `uvicorn api.server:app --host 127.0.0.1 --port 8010`，
但**绕过了 `__main__` 的三重守卫**，不推荐）。

**健康检查**：
- 存活：`GET /health` → `{"ok": true}`（唯一一个免鉴权路径，`api/auth.py`
  `is_public_path()` = `{"/health","/healthz"}`）；
- 详细：`GET /health/detail` → 7 个字段，包含 `UNKNOWN` 积压、孤儿 checkpoints、
  设备池状态、单进程前提等（见 10.4）。

**日志位置**：**没有日志文件，全部走 stdout/stderr**（无 `logging.FileHandler`、
无 logrotate 配置）。要持久化必须由外部重定向：
`python -m api.server >> artifacts/server.log 2>&1`。事件类审计在
`<ARTIFACT_DIR>/audit/`（请求审计）与 `shadow.db` 的 `events` 表。

**回滚**：见 15.4。

### 15.3 方式二：手机端点部署（路线 B）

**链路**：PC 跑 Core（`api/server.py`）→ 手机跑 APK（`DeviceEndpointService`
暴露 HTTP 端点）→ PC 侧 `device/remote.py` 走 HTTP 连手机 →
PC 侧 `device/factory.py` 把 `SHADOW_DEVICE_BACKEND=android` 时装配成
Android 后端，串号固定为 `android-local`（`ANDROID_DEFAULT_SERIAL`）。

**完整 5 步**（细节见 `android/README.md`，452 行）：

```bash
# 1. 准备工具链（不需要 Android Studio）
export SHADOW_SDK_DIR="<Android SDK 目录>"
export SHADOW_BUILD_TOOLS_DIR="$SHADOW_SDK_DIR/build-tools/<版本>"
export SHADOW_KOTLINC_DIR="<kotlinc 目录>"
export SHADOW_JAVA="<java 可执行文件>"
export SHADOW_ANDROID_JAR="$SHADOW_SDK_DIR/platforms/<API>/android.jar"

# 2. 编译校验（先编译，再打包；失败会明确指出缺哪个环境变量）
python android/tools/verify_kotlin_compile.py
# 期望：34 classes + 15 JVM 单测全过

# 3. 打 APK（9 步流水线，含 v2+v3 签名）
python android/tools/build_apk.py
# 产物：android/build/...apk，实测 796.6 KB

# 4. 装到手机（USB 或无线调试，见 13.4）
adb install -r <apk 路径>

# 5. PC 侧切后端并启动
export SHADOW_DEVICE_BACKEND="android"
export SHADOW_ENDPOINT_URL="http://<手机IP>:8765"
export SHADOW_ENDPOINT_TOKEN="<手机端点令牌>"
python -m api.server
```

**端点鉴权**：手机端 `DeviceEndpointService` 监听 **8765**，要求请求头带
`X-Shadow-Token`；令牌在手机 App 内设置。**不要把 8765 暴露到公网。**

**为什么不用路线 A（Chaquopy 把 Python 塞进 APK）**：`android/README.md`
写明了硬阻塞——`pydantic-core` 是 Rust 扩展，Chaquopy 下没有可用 wheel；
文档里给了引用出处。**这是明确的技术决策，不是没做完。**

**已知限制（务必转达）**：`android/README.md` 列了 6 条，重点是
旋转（rotation）、`FLAG_SECURE` 页面无法截图、`set_text` 需要先聚焦、
单端点只能连一台手机。

### 15.4 回滚

**没有自动化回滚机制**（无镜像、无版本标签、无蓝绿）。真实可用的回滚手段：

| 层次 | 手段 | 命令 / 操作 |
|---|---|---|
| 代码 | Git 回退到上一个 commit | `git log --oneline` → `git checkout <commit>` 或 `git revert <commit>` |
| 依赖 | 依赖版本已钉死，`pip install -r requirements.txt` 可复现 | 无独立依赖回滚需求 |
| 运行时数据 | 备份 `artifacts/state/shadow.db` 即可整体回退 | 见下方警告 |
| 手机端 APK | 重新安装旧版 APK | `adb install -r <旧 apk>` |

> ⚠️ **回滚的隐藏地雷**：`shadow.db` 里有 `PRAGMA user_version=4` 的迁移版本。
> 如果把代码回退到**旧于 v4 迁移**的版本，`storage/migrations/` 会认为库版本
> 超前——**旧代码不认识新表结构**。安全做法是回代码时**同时**换回对应的库文件，
> 或先 `cp artifacts/state/shadow.db artifacts/state/shadow.db.bak` 再动。

### 15.5 发布边界（交接时必须知道）

`.gitignore` 明确排除了以下内容，**远程仓库里没有**，换机器时务必注意：

| 被排除的内容 | 影响 | 取回方式 |
|---|---|---|
| `tests/`（38 文件） | **远程没有测试**，新机器 clone 下来跑不了 `pytest` | `git checkout <commit> -- tests scripts` |
| `scripts/` 下的 `__pycache__` | 无影响（脚本本身是发布的） | — |
| `artifacts/` | 无运行时数据（库、事件、截图全没了） | 无法恢复，需重新跑 |
| `DEVLOG.md` | 2270 行开发历程读不到 | 向原作者索取 |
| `bluewhale-shadow-phone/` | 审核/蓝图文档读不到 | 向原作者索取 |
| `docs/`、`.workbuddy/`、`memories/` | 同上 | 向原作者索取 |

**实测远程跟踪文件数：120 个**。也就是说这个项目"能跑起来的全部代码"在远程
是完整的，但"能验证它正确的测试"和"能理解它为什么这么设计的文档"**不在远程**。

---

## 十六、日志与监控

### 16.1 结论先行

**本项目没有日志框架、没有指标系统、没有链路追踪、没有告警。**
全部可观测性由三样东西构成，**都在代码里，不在配置里**：

1. **stdout/stderr**：进程日志（无文件落盘、无级别控制、无格式化配置）
2. **`shadow.db` 的 `events` 表**：结构化事件流（22 种 kind），带 `sequence`
   单调递增与 `UNIQUE(task_id, sequence)` 约束
3. **`GET /health` + `GET /health/detail`**：唯一两个健康检查端点

**没有**：Prometheus / Grafana / ELK / Sentry / OpenTelemetry / Jaeger、
`logging.config` / `structlog` / `loguru`、metrics 端点、`/metrics`。

### 16.2 事件流就是主监控面

`storage/event_log.py`（319 行）定义了 **22 种事件 kind**，按语义分四类：

| 类别 | kind | 含义 |
|---|---|---|
| 生命周期 | `CREATED` `QUEUED` `STARTED` `CHECKPOINT_SAVED` `RECONCILED` `WAITING` `SUSPENDED` `RESUMED` `DONE` `FAILED` `CANCELLED` `RECOVERED` | 任务级生命周期 |
| 动作 | `ACTION_DISPATCHED` `ACTION_VERIFIED` `RISK_ASSESSED` `PREEMPT_REQUESTED` `OBSERVATION_STALE` `EFFECT_UNKNOWN` | 执行层关键节点 |
| 目标 | `CONFIRMED` `GOAL_REQUESTED` `GOAL_CONFIRMED` `GOAL_REJECTED` | 人工确认与目标裁定 |
| 数据 | `TASK_CORRUPTED` `EXECUTION_RECOVERED` | 数据完整性事件 |

**安全关键事件（唯一写入口分级，[79]）**：

```python
SAFETY_CRITICAL_KINDS = frozenset({
    ACTION_DISPATCHED, RISK_ASSESSED, CONFIRMED, GOAL_CONFIRMED,
})
```

`EventLog.emit()` 是**唯一写入口**，遵循「**先落盘、再生效**」——写库成功才
往下走，写失败就抛（[34] 关键持久化不吞异常）。所以 `events` 表里**没有**
记录意味着那件事**确实没发生**，这是它作为监控面的可信基础。

**实测当前库里的 kind 分布**（`artifacts/state/shadow.db`，18 条事件）：
`risk_assessed`=3、`started`=3、`action_dispatched`=2、`action_verified`=2、
`checkpoint_saved`=2、`failed`=2、`queued`=2、`observation_stale`=1、`recovered`=1。

### 16.3 健康检查两个端点的语义差异

| 端点 | 鉴权 | 返回 | 用途 |
|---|---|---|---|
| `GET /health`、`GET /healthz` | **免鉴权**（唯一） | `{"ok": true}` | 存活探针（liveness） |
| `GET /health/detail` | 需鉴权 | 7 个字段 | 就绪/诊断（readiness + 排障） |

`/health/detail` 的 7 个字段（`api/server.py` L1737 起）覆盖：设备池状态、
调度器状态、`UNKNOWN` 执行积压、孤儿 checkpoint、单进程前提、存储健康、
鉴权配置状态。**排障第一站就是它。**

> ⚠️ `/health` 只返回 `{"ok": true}`，**它不代表系统健康**——存储坏了、
> 设备掉线了、`UNKNOWN` 堆积了，`/health` 照样 200。别用它做 readiness。

### 16.4 请求审计

`<ARTIFACT_DIR>/audit/`（由 `api/server.py` 的 `AUDIT_DIR` 决定，
`_AUDIT` 开关）。这是**文件形态**的请求审计，与 `events` 表不同：
- `events` 表 = 领域事件（任务/动作/风险），结构化、可查询
- `audit/` = HTTP 请求审计（谁在什么时候调了哪个接口）

`_MANUAL_OWNER` 用于标记手工触发的动作主体（`channel='manual'` vs `'agent'`，
见 `executions.channel` 字段）。

### 16.5 要监控什么（交接建议）

项目自带能力有限，**新接手的人应该自己加**下面几个（都不难）：

1. **`GET /executions?status=UNKNOWN` 的数量** —— 最重要。
   `UNKNOWN` 是「设备被调用过但结果不明」，[110]/[111] 规定**禁止自动重试**，
   必须人工处置。积压 = 有真实副作用悬着。
2. **`events` 表增长速率** —— 突增说明重试风暴或死循环。
3. **`OBSERVATION_STALE` 频率** —— 高说明页面在剧烈变化或 VLM 太慢。
4. **`shadow.db` 体积与 WAL 大小** —— `-wal` 文件持续不收缩说明长事务没提交。
5. **进程存活** —— 目前无 supervisor，进程挂了不会自愈。

### 16.6 代码里**没有**的监控（避免误找）

| 常见项 | 状态 |
|---|---|
| `/metrics` Prometheus 端点 | ❌ 不存在 |
| 结构化日志（JSON lines） | ❌ 不存在 |
| trace_id / span 传递 | ❌ 不存在 |
| 日志级别开关（`LOG_LEVEL`） | ❌ 不存在 |
| 告警 / 通知 / webhook | ❌ 不存在 |
| 日志轮转 | ❌ 不存在 |

---

## 十七、故障排查

### 17.1 服务启动失败排查清单

**按此顺序逐条排查**（每条都给了明确判据）：

| # | 现象 / 错误 | 根因 | 处置 |
|---|---|---|---|
| 1 | `RuntimeError: 检测到多进程配置…` | `WEB_CONCURRENCY`/`UVICORN_WORKERS`/`GUNICORN_WORKERS` > 1 | 去掉该变量，或确认理解风险后设 `SHADOW_ALLOW_MULTI_PROCESS=1`（见 4.3，**强烈不建议**） |
| 2 | 启动即报「裸奔」相关错误 | `HOST=0.0.0.0` 但未配 `SHADOW_API_TOKENS` | 改回 `127.0.0.1`，或配置令牌（V3.2 §六 JSON 规格） |
| 3 | `UnknownDeviceBackend: ...` | `SHADOW_DEVICE_BACKEND` 值不在 `{adb, android}` | 修正拼写，**不要指望它退化到 adb**（[91]/[93] fail-closed） |
| 4 | `AndroidServiceUnavailable: ...` | `SHADOW_DEVICE_BACKEND=android` 但既没同进程桥、也没配远程桥 | 报错信息里已列出两条路由 + 「若只想用 ADB 控制手机请设为 adb」的逃生口，照做 |
| 5 | `sqlite3.OperationalError: database is locked` | 同库被两个进程打开（常见于遗留的 `confirmations.db`/`events.db` 旧进程） | 确认只有一个 Core 进程；`busy_timeout=5000` 已在 `Database` 里设 |
| 6 | 端口占用 `Address already in use` | 8010 被上一进程占着 | 换端口或杀进程；`PORT` 环境变量可改 |
| 7 | `PersistenceError` → 任务进 `DEGRADED` | 关键落盘失败（磁盘满 / 权限错 / 库损坏） | 检查 `ARTIFACT_DIR` 可写与磁盘空间；[34] 规定**不吞异常**，所以它会显式冒出来 |
| 8 | 导入期报 `ImportError`（缺包） | 依赖没装全 | `pip install -r requirements.txt`；`uvicorn[standard]` 的 extras 别丢 |
| 9 | `TASK_CORRUPTED` 事件出现 | `tasks` 表里某行 payload 反序列化失败 | `TaskStore.load()` 会 `_quarantine()`；用 `GET /tasks` 的 `corrupt` 字段定位（见 17.2） |
| 10 | 启动正常但任务不动 | 设备租约被别人持有 / 调度器空转 | `GET /scheduler` 看状态；`storage/lease_store.py` 的 `lease.db` 里查租约归属 |

### 17.2 API 报错排查

**先看错误码语义**（10.5 有全表）：

| 状态码 | 含义 | 常见根因 |
|---|---|---|
| 401 | 鉴权失败 | 没带 `X-API-Token`/`Bearer`；**或 `_parse_principals()` 解析失败导致整张表作废**（V3.2 §六：一处写错，全部 401） |
| 403 | 权限不足 | 只读主体调了写接口（`_READ_ONLY_METHODS={GET,HEAD,OPTIONS}` + `_READ_ONLY_ALLOWED_POST_PATHS={"/screenshot","/observe"}` 之外）；或该主体无权访问此设备/任务 |
| 404 | 找不到 | 任务 ID 错；**或用户在 `users.py` 式的列对象误用**（那类 bug 已在 memos 项目踩过，本项目用 `task_id` 字符串主键，风险较低） |
| 409 | 状态冲突 | 任务已在终态还想改（`InvalidTransitionError`，[33] 终态再迁出必抛）；或确认票据已被消费 |
| 422 | 参数校验失败 | Pydantic 校验；注意 **`max_steps` 陷阱**（见 10.2）与 `DEFAULT_MAX_STEPS=10` |
| 500 | 服务端异常 | 只有 `_DEBUG_ERRORS` 打开才回详细栈，否则是泛化响应 |
| 503 | 设备不可用 | 设备池空 / 后端未就绪 / 租约被占 |

**定位手法（真实可用）**：

```bash
# 1. 先看健康详情（7 字段，最能说明问题）
curl -H "X-API-Token: <token>" http://127.0.0.1:8010/health/detail

# 2. 看某任务的事件流（时间顺序，sequence 单调）
curl -H "X-API-Token: <token>" "http://127.0.0.1:8010/events?task_id=<tid>"

# 3. 看执行记录（含 status / note，note 常直接写着失败原因）
curl -H "X-API-Token: <token>" "http://127.0.0.1:8010/executions?task_id=<tid>"

# 4. 看调度器状态（谁在占设备）
curl -H "X-API-Token: <token>" http://127.0.0.1:8010/scheduler
```

**实测样例**（本仓库当前 `executions` 表里两条 FAILED 记录，`note` 字段是：
`未在 UI 树中找到可点击元素: '设置图标（位于Dock栏第二个位置）'`）——
**`note` 字段就是最直接的失败原因**，排障时优先读它。

**`TASK_CORRUPTED` 处理**：`GET /tasks` 返回里有 `corrupt` 列表。
这是 [80]/[92] 的体现——「读不到」≠「空」，payload 解析不了会被隔离而不是
当成空任务。**不要直接删库**，先 `cp shadow.db shadow.db.bak` 再动。

### 17.3 LLM / VLM 调用失败

**调用链**：`vision/vlm.py` 的 `_call_vlm()` → `_post_chat_completions()`（HTTP）。

**关键参数**（全部可查代码）：
- `DEFAULT_BASE_URL="https://api.openai.com/v1"`、`DEFAULT_MODEL="gpt-4o"`
- `VLM_TIMEOUT_SECONDS=60.0`、`VLM_MAX_ATTEMPTS=3`、`VLM_BACKOFF_SECONDS=0.8`
- 退避算法：`0.8 * 2^(n-1)` —— 即 0.8s → 1.6s → 3.2s
- 可重试状态码：`RETRYABLE_STATUS_CODES={408,409,425,429,500,502,503,504}`
- `max_tokens: 512` **在代码里硬编码**（不可配，见 19 章 P2）
- `temperature` **未设置**（走服务端默认）
- 环境变量**在调用时读取**（不是导入时），所以改 `VLM_MODEL` 不用重启

**逐条排查**：

| 现象 | 根因 | 处置 |
|---|---|---|
| 每次调用都 401 | `VLM_API_KEY` 没配 / 配错 | 检查环境变量；注意 `classifier` 在没配 key 时会退到纯规则路径（`api/server.py` wiring 段有 `VLM_API_KEY` 判断） |
| 连接超时 | 网关不可达 / 需要代理 | 检查 `VLM_BASE_URL` 是否可达；国内访问 OpenAI 需网关 |
| 60s 后必失败 | `VLM_TIMEOUT_SECONDS` 不够 | 图片大 / 模型慢，调大该变量 |
| 每秒被限流（429） | 配额 | 退避已在跑（最多 3 次），仍失败就降并发或换 key |
| 返回内容解析不了 | 模型没按 JSON 格式回 | `_parse_action()` 解析失败；`prompt` 版本见 8.3，考虑升版本 |
| 重试 3 次仍失败 | 非可重试状态码（如 400/403） | 看响应体，通常是 key 额度或模型名错 |
| 计划阶段正常但决策阶段慢 | `_DEFAULT_IMAGE_DETAIL={"plan":"low","decide":"high","verify":"low"}` | `decide` 用 high detail，token 消耗大；这是**有意设计**（决策最需要细节） |

**降级行为**：VLM 全挂时**不是整个系统挂**——
`classifier` 有规则路径（`RULE_WEIGHT=0.3`、`LLM_WEIGHT=0.6`，权重在 `agent/classifier.py`），
但 **Agent 主循环没有 VLM 就转不动**（`_plan_from_scratch`/`_decide`/`_replan`/`verify_transition`
都依赖它）。所以现象通常是：**任务能创建、能入队、一进 `_think` 就失败**。

### 17.4 设备 / 采集异常

| 现象 | 根因 | 处置 |
|---|---|---|
| `dump_ui()` 返回空 / 抛异常 | 无障碍服务没开、页面 `FLAG_SECURE`、或前台是自家界面 | [80]/[92] 规定**读不到树要抛异常**，不许当空树处理。检查手机无障碍服务开关；`FLAG_SECURE` 页面（银行/密码）**无法截图**，这是 Android 限制不是 bug |
| 动作"执行了但没效果" | 坐标错、页面变了、或 `TAP` 打在不可点区域 | 看 `TargetResolution`：`NOT_FOUND`/`AMBIGUOUS` 都算证据缺口（[115] 目标不唯一也是缺口） |
| 频繁 `OBSERVATION_STALE` | 页面在动 / VLM 慢于页面变化 | `MAX_STALE_OBSERVATIONS=3`，超了会失败；可拉长 `TOCTOU_TIMEOUT_SECONDS`（默认 4.0） |
| 影子动作全部失败 | **这是正确的** | 真机上所有 `shadow_*` 都会抛 `ShadowActionUnsupported`；[V5 修复轮 ⑤] 规定**绝不回落**到同名前台方法（名字带 `shadow_` 前缀本身就是防线）。真要用影子平面必须先实现 Shadow Display |
| `DeviceBusyError` | 设备被别的任务占着 / 直接调 `runtime.run()` 忘了 `session.acquire()` | 走调度器正常排队即可；写测试时注意这个坑（见 14.4） |
| 无线调试连不上（USB 枚举失败） | `USB\SET_ADDRESS_FAILURE` | 见 13.4 / skill `android-wireless-debug` |
| 手机端点 401 | 没带 `X-Shadow-Token` | 检查请求头；令牌在手机 App 里 |
| 手机端点连不上 | 8765 未监听 / 手机与 PC 不同网段 | `adb shell netstat` 查监听；确认同一 Wi-Fi |

### 17.5 RAG 异常

**不适用。** 本项目**没有 RAG**（无 embedding、无向量库、无检索链路），
因此不存在这一节。设计文档里出现过的「embedding grounding」设想在
`bluewhale-shadow-phone/` 文档中**被明确标注为未实现**。

若未来要加，需注意：`vision/vlm.py` 目前只走 chat completions，
加 RAG 等于新增一条完全独立的链路。

### 17.6 Agent 死循环排查

**防护是分层的，逐层检查**：

| 层 | 机制 | 位置 | 值 |
|---|---|---|---|
| 1 | 步数预算 | `api/server.py` `_resolve_budget()` | `DEFAULT_MAX_STEPS=10` |
| 2 | 重试策略 | `models/retry.py` `RetryPolicy` | `max_transient=3`, `max_replan=2`, `max_unknown=1` |
| 3 | 陈旧观测上限 | `agent/_execution.py` | `MAX_STALE_OBSERVATIONS=3` |
| 4 | 目标裁定 | `agent/goal_verifier.py` + `goal_oracle.py` | 独立证据裁定，不看自我声明（[116] `expected_state` 进计划**不进裁定**） |

**死循环的真实形态与判据**：

- **反复 replan 同一动作** → 看 `executions` 表里 `action_payload` 是否高度相似；
  `max_replan=2` 到顶后应该转 `ASK_HUMAN`
- **`DONE` 但目标没达成** → `goal_oracle.py` 区分了 `plan_finished`（计划跑完）与
  `goal_achieved`（目标达成），**这两个不是一回事**；只有 `goal_verifier` 能裁定完成
- **`UNKNOWN` 不被自动重试** → [110]/[111] 明确 `NO_AUTO_RETRY_STATUSES={UNVERIFIED,UNKNOWN}`，
  这是**防死循环的关键**：结果不明就停下来问人，不赌
- **无止境抢占** → `MAX_PREEMPTION_LATENCY_SECONDS=2.0`，`_record_preemption_latency()`
  会记录抢占延迟（见 `agent/scheduler.py`）

**处置**：`POST /tasks/{id}/cancel`，然后 `GET /executions?task_id=<tid>` 看
最后一条 `UNKNOWN` 记录，**人工确认设备实际状态**再决定后续。

### 17.7 队列积压

**本项目没有 MQ**（无 RabbitMQ/Kafka/Redis/Celery）。队列是**进程内内存队列**，
在 `agent/scheduler.py`（1206 行），按**车道（lane）**组织：

- 优先级：`_PRIORITY_RANK={LOW:0, NORMAL:1, HIGH:2, CRITICAL:3}`（`models/task.py`）
- 核心循环：`_lane_loop()`（L1019）→ `_pop_next()`（L978）→ `_execute()`（L1061）→ `_handle_outcome()`（L1126）
- 抢占：`_maybe_preempt()`（L787）/ `_maybe_preempt_by_id()`（L790）/ `preempt_running()`（L597）

**积压排查**：

1. `GET /scheduler` 看当前排队与运行中的任务
2. 如果队列里有任务但 `_lane_loop` 不动 → 检查是否**所有设备都被租约占着**
   （`storage/lease_store.py` 的 `lease.db`），或某个任务卡在 `RUNNING` 却没心跳
3. `ACTIVE_STATUSES={QUEUED,RUNNING,PAUSED,WAITING,DEVICE_UNAVAILABLE,CANCEL_REQUESTED}` ——
   排队中属于正常"积压"，**只有长期不动才是问题**
4. **进程重启后队列清空**（内存队列不持久化）—— 这是设计选择，不是 bug。
   任务本身在 `tasks` 表里活着（状态 `QUEUED`），`scheduler.recover()`（L361）
   会重新拾起

> ⚠️ 因为队列在内存里，**单进程是硬前提**（4.3）。多 worker 会让
> 「队列」这个概念失效——两个 worker 各有一份内存队列，同一任务可能被两边同时拾起。
> 这就是 `_guard_single_process()` 存在的理由。

### 17.8 `UNKNOWN` 状态处置（最高优先级）

这是本项目**最需要人工介入的状态**，单列一节。

**为什么存在**：`agent/execution/state.py` 的 `recovery_target()`：
- 终态 → `None`（不动）
- `RUNNING` → **`UNKNOWN`**（设备被调用过，但不知道成没成）
- 其它非终态 → `FAILED`（设备没被碰过，可安全重做）

**判据是「设备有没有被调用过」**（[108]/[109]）——
`DEVICE_REACHED_STATUSES=frozenset({RUNNING})`，`DISPATCHED` 与 `RUNNING` 的分界
正是这一刻。

**处置流程**：

```bash
# 1. 查积压
curl -H "X-API-Token: <token>" "http://127.0.0.1:8010/executions?status=UNKNOWN"

# 2. 看 /health/detail 里的积压计数
curl -H "X-API-Token: <token>" http://127.0.0.1:8010/health/detail

# 3. 逐条人工核实设备实际状态（关键：去手机上看！）
#    因为不知道「点了没」，只能靠现场观察
```

> ❌ **绝对不要自动重试 `UNKNOWN`**。如果那个动作其实执行了（比如转账/发送），
> 重试就是**做第二遍**。`NO_AUTO_RETRY_STATUSES` 是硬编码防线，
> 绕过它等于自己拆掉安全网。

---

## 十八、常见问题 FAQ

**Q1：这个项目是什么？一句话说清。**
面向 Android 的**任务级 Agent Runtime**——你给一句自然语言任务（「打开设置把亮度调低」），
它自己在手机上 Observe → Think → Act → Verify → Checkpoint 地跑完。
FastAPI 标题 `BlueWhale Shadow Phone Agent`，版本 `0.3.2`。

**Q2：它和「用 LLM 写个点击脚本」有什么区别？**
脚本是一次性的；这个项目有**任务生命周期**（11 个状态）、**执行状态机**（7 个状态）、
**崩溃恢复**、**设备租约**、**风险门禁**、**多级证据**、**对账**。README 里那句话最准：
「决定上限的不是 VLM，而是 TaskManager + Scheduler + Checkpoint + Runtime」。

**Q3：能用 Docker 跑吗？**
不能，**没有 Dockerfile / compose**（第十五章有全仓库扫描证据）。
部署只有两种：源码直跑、手机端点部署。

**Q4：数据库是 PostgreSQL 吗？**
不是。**是单个 SQLite 文件** `<STORAGE_DIR>/shadow.db`（`PRAGMA user_version=4`）。
任务、恢复点、事件、确认票据、执行记录**全在同一个库里**。旧版本的
`json_store.py` 已删除，仍有文件的只有轨迹（会被裁剪）与请求审计。

**Q5：有几个微服务？**
**一个进程**。而且是**硬前提**——`_guard_single_process()` 检测到
`WEB_CONCURRENCY`/`UVICORN_WORKERS`/`GUNICORN_WORKERS` > 1 时**直接拒绝启动**。
架构是分层的（API → TaskManager → Classifier → Scheduler → AgentRuntime → Vision/Device），
但**层是模块，不是服务**。

**Q6：有 RAG 吗？**
没有。无 embedding、无向量库、无检索链路。详见 8.1。

**Q7：有 MCP 吗？**
没有 MCP server，也没有 MCP client。工具体系是**自己写的 action 类型**（见 7 章）。

**Q8：用 LangGraph 吗？还是 Multi-Agent？**
都不是。**自己写的单 Agent 循环**，状态放在 `RuntimeState` 里，
用有界计数器（`RetryPolicy`）控制，不走任何编排框架。

**Q9：支持几台手机？**
`adb` 后端理论上支持多台（设备池 `device/pool.py`）；
`android` 后端**只支持一台**（`ANDROID_DEFAULT_SERIAL="android-local"`，
`resolve_device_serials()` 只返回一个）。

**Q10：为什么 `shadow_*` 方法全失败？**
**这是正确的行为**。真机上所有影子动作都会抛 `ShadowActionUnsupported`——
[V5 修复轮 ⑤] 规定影子动作**永不回落到前台同名方法**，而回落是「无声的」，
比失败危险得多。真要落实需要先实现真正的 Shadow Display（见第 2 章「必须知道的三件事」）。

**Q11：测试怎么跑？跑不了怎么办？**
`pip install -r requirements-dev.txt` → `pytest -q`。
**实测 `799 passed in 38.69s`**，全离线（FakeDevice / FakeBridge + 打桩 VLM），
不需要真机、不需要 API key。
**如果跑不了**：因为 `tests/` 在 `.gitignore` 里，**远程仓库没有测试**。
`git checkout <commit> -- tests scripts` 取回。

**Q12：为什么 `.gitignore` 把 `tests/` 排除了？**
代码里没写理由。【待确认】——需向原作者确认（可能是用户不想公开测试，
也可能是误操作）。**影响明确**：新接手的人 clone 下来会以为"没有测试"，
进而在交接文档里写下「未发现系统化自动化测试」的错误结论。

**Q13：`pyproject.toml` 里的 `packages` 少了什么？**
少了 `storage`：

```toml
packages = ["agent", "api", "device", "models", "vision"]   # ← 没有 storage
```

**影响**：如果按 `pyproject.toml` 打 wheel/安装，`storage` 包不会被打进去，
装完就 `ImportError`。**当前不影响源码直跑**（因为直接跑源码目录）。
严重程度 P2，见 19 章。

**Q14：`artifacts/state/` 里那两个旧库文件要紧吗？**
`confirmations.db`（12,288 B，含 2 条 `consumed_confirmation`）和
`events/events.db`（28,672 B，`events` 表 **0 行**）是 **V4 存储换代前的遗留**。
现在所有数据都在 `shadow.db`。**建议备份后删除**，避免以后误读旧库。
严重程度 P3，见 19 章。

**Q15：环境变量在哪配置？有 `.env` 吗？**
**没有 `.env`、没有 `.env.example`**。全部走真实环境变量。
演示场景可用 `scripts/start_demo_core.py`，它按 `ENV_FILES` 顺序读文件
（逐行解析、**不做 shell eval**、会剥掉 CRLF），并校验 6 个必需变量。

**Q16：日志在哪？**
**没有日志文件**，全走 stdout/stderr。要留存必须外部重定向。
结构化事件在 `shadow.db` 的 `events` 表（见第十六章）。

**Q17：任务卡住不动怎么办？**
1. `GET /scheduler` 看它在不在运行
2. `GET /executions?task_id=<tid>` 看最后一条记录与 `note`
3. 如果最后是 `UNKNOWN` → **人工去手机上看**，**不要重试**
4. 确认要放弃 → `POST /tasks/{id}/cancel`
5. 实在没头绪 → `GET /health/detail`

**Q18：API 的 `max_steps` 有坑吗？**
有。见 10.2 的说明：`DEFAULT_MAX_STEPS=10`，`_resolve_budget()` 处理它。
**注意它是「步数预算」不是「超时」**，别拿它当 timeout 用。

---

## 十九、已知问题与技术债务

> 严重程度仅用 **P0–P3**：P0 = 会导致数据损坏/安全问题；P1 = 影响可用性；
> P2 = 影响可维护性/正确性边缘；P3 = 清洁度问题。

### 19.1 问题清单

| # | 问题 | 位置 | 影响 | 严重程度 | 建议 |
|---|---|---|---|---|---|
| 1 | `pyproject.toml` 的 `packages` 漏了 `storage` | `pyproject.toml` | 打包/安装后 `ImportError: No module named 'storage'`；当前源码直跑不受影响 | **P2** | 加上 `"storage"`，并在 CI 里加一条 "打 wheel 后 import 冒烟测试" |
| 2 | 遗留旧库文件未清理 | `artifacts/state/confirmations.db`、`artifacts/state/events/events.db` | 排障时可能误读旧库得出错误结论（`events.db` 里 0 行，容易被当成「没有事件」） | **P3** | 备份后删除，或加一条启动期检查打印警告 |
| 3 | `tests/` 与 `scripts/` 被 gitignore 但脚本又在仓库里 | `.gitignore` | 远程无测试；新接手者极易误判「项目无测试」 | **P1** | 至少把 `tests/` 纳入版本控制；若确有顾虑，在 README 里**显著**写明取回命令 |
| 4 | `DEVLOG.md`（2270 行）、`bluewhale-shadow-phone/`（29 md）、`docs/` 全部 gitignore | `.gitignore` | **设计意图的载体不在远程**——新接手的人只能看代码反推「为什么这么设计」，成本极高 | **P1** | 把设计文档纳入版本控制，或导出为可分享的独立归档 |
| 5 | `VLM` 的 `max_tokens: 512` 硬编码 | `vision/vlm.py` | 复杂页面/长 prompt 可能被截断；无法按模型调优 | **P2** | 提为 `VLM_MAX_TOKENS` 环境变量，保留 512 作默认 |
| 6 | `temperature` 未设置 | `vision/vlm.py` | 依赖服务端默认值（通常 1.0），决策稳定性不可控 | **P2** | 显式设 `temperature=0`（Agent 决策场景几乎总想要确定性） |
| 7 | 没有日志框架 | 全项目 | 无级别控制、无文件落盘、无轮转；生产排障只能靠 `events` 表 | **P2** | 引入 `logging` + 结构化输出；至少加 `LOG_LEVEL` |
| 8 | 没有容器化 / CI-CD | 全项目 | 部署靠手工，无自动化回归 | **P2** | 先加 GitHub Actions 跑 `pytest`（成本最低、收益最大），容器化可缓 |
| 9 | 内存队列不持久化 | `agent/scheduler.py` | 进程重启后排队任务靠 `tasks` 表 + `recover()` 重建，**抢占状态丢失** | **P2** | 已知设计取舍（单进程前提），若要持久化需重新设计锁粒度 |
| 10 | 单进程硬前提 | `api/server.py` `_guard_single_process()` | 无法水平扩展；吞吐上限 = 单进程 | **P2** | 这是**刻意的正确决策**（[78]），不是缺陷；扩展需先解决单写者问题 |
| 11 | `shadow_*` 影子动作在真机全失败 | `device/controller.py` | 影子平面只是**架构预留**，真实并行执行不可用 | **P2** | 已在 README 顶部诚实标注；实现需真 Shadow Display（大工程） |
| 12 | 无 supervisor / 进程自愈 | 部署层 | 进程挂了不自恢复 | **P2** | 外部用 systemd / NSSM 托管 |
| 13 | `foreign_keys=0` | `storage/database.py` 的 PRAGMA | 外键约束**不生效**；引用完整性靠应用层代码保证 | **P2** | 当前靠代码保证（`task_id` 一致性有校验），若要开启需先排查存量数据 |
| 14 | 轨迹会被裁剪 | 轨迹存储 | 长期审计信息不完整 | **P3** | 已有 `events` 表作为完整事件源，可接受 |
| 15 | 无 metrics / 告警 | 全项目 | 靠人肉看库 | **P2** | 见 16.5 的 5 条建议 |
| 16 | `vision/vlm.py` 环境变量在**调用时**读取 | `vision/vlm.py` | 好处是热改不用重启；坏处是**运行中改配置会产生不一致状态**（前几步用一个模型，后几步换另一个） | **P3** | 有意设计，但应在文档里写明 |

### 19.2 明确的「不是问题」（避免误判为债务）

以下几项**看起来像债务，实际是刻意的正确决策**，交接时务必说明：

| 项 | 为什么不是问题 |
|---|---|
| 单进程硬前提 | [78] 明确：跨进程唯一执行者靠 SQLite 租约，多写者会静默破坏状态。**拒绝启动**优于静默出错 |
| 影子动作全失败 | [V5 修复轮 ⑤]：影子动作**永不回落**。真机上失败是**正确**的（会被记账），回落才是无声的灾难 |
| `UNKNOWN` 不自动重试 | [110]/[111]：结果不明时重试 = 可能做第二遍。宁可停下问人 |
| 「读不到」抛异常而非返回空 | [80]/[92]：证据缺口是一等事实。空树和读不到是两件事 |
| `RULE_WEIGHT=0.3 < LLM_WEIGHT=0.6` | 有意让 LLM 主导，规则只做兜底 |
| `_REF_EXTRACT/` 目录 | 参考提取物，gitignore 内，非项目代码 |

---

## 二十、后续开发建议

> 原则：**不为了「看起来先进」而引入微服务 / K8s / Multi-Agent**。
> 下面的建议都是**基于本项目真实技术债**的，不是通用清单。

### 20.1 短期（1–2 周）

**目标：让新接手的人能安全地动代码。**

| # | 事项 | 理由 | 依据 |
|---|---|---|---|
| 1 | **把 `tests/` 纳入版本控制** | 799 个测试是项目最宝贵的资产之一，现在远程没有 | 19.1 #3 |
| 2 | 加一条 GitHub Actions：`pip install -r requirements-dev.txt && pytest -q` | 成本极低（半小时），收益最大：任何 PR 立刻知道有没有回归 | 19.1 #8 |
| 3 | 修 `pyproject.toml` 的 `packages` 加上 `storage` | 一行改动，消除安装即崩的坑 | 19.1 #1 |
| 4 | 清理 `artifacts/state/` 的遗留旧库 | 消除排障误读源 | 19.1 #2 |
| 5 | 引入 `logging`，至少加 `LOG_LEVEL` 与文件落盘 | 现在排障只能读 stdout 和查库 | 19.1 #7 |
| 6 | 把 `max_tokens` / `temperature` 提为环境变量 | 两处 `os.getenv` 就够，立刻可选可调 | 19.1 #5 #6 |
| 7 | README 显著位置写明「测试取回命令」 | 一句话，避免下一个接手者重写这份文档 | 19.1 #4 |

### 20.2 中期（1–3 个月）

**目标：把「架构预留」补成「真实能力」。**

| # | 事项 | 说明 |
|---|---|---|
| 1 | **`UNKNOWN` 处置闭环** | 目前只有「查」和「人工判断」。应加：`POST /executions/{id}/resolve` 接口 + 结算语义（`UNKNOWN` → 人工裁定为 `SUCCEEDED`/`FAILED`），并把裁定入 `events`（安全关键事件）。这是**最影响可用性的一块** |
| 2 | **完善结构化监控** | 按 16.5 的 5 条加：`/metrics` 或至少 `health/detail` 扩展；`UNKNOWN` 积压告警；`events` 增长速率 |
| 3 | **影子平面真落地（第一阶段）** | 实现 `shadow_screenshot_bytes` / `shadow_dump_ui` 的**只读**能力（不需要真 Shadow Display 也能做——虚拟显示的可读快照），让影子动作至少能「观察」。**不要**先做写动作 |
| 4 | 移植到 PostgreSQL 的可行性评估 | 当前 SQLite 的 `busy_timeout=5000` + 单进程是硬约束。若要上多设备规模，需要先评估存储层抽象是否够干净（`storage/` 已被 `Database` 收口，改动面可控） |
| 5 | 补齐 Android 端限制 | 旋转、`FLAG_SECURE`、多端点（`android/README.md` 6 条已知限制） |
| 6 | 风险门禁的可配置化 | `SENSITIVE_PACKAGE_MARKERS` / `DANGEROUS_KEYWORDS` 现在是代码常量，应可外置配置（注意：**外置不能削弱 fail-closed**） |

### 20.3 长期（3 个月以上）

| # | 事项 | 说明 |
|---|---|---|
| 1 | **真 Shadow Display** | 第三方 App 后台并行执行的**唯一前提**。当前是「V5 架构预留完成」，不是「Shadow Execution 完成」 |
| 2 | 多设备规模化 | 需要：持久化队列、分布式租约、水平扩展——**这三件事和「单进程硬前提」直接冲突**，是一次架构级重构而非增量改动 |
| 3 | 记忆与学习 | 现在每次任务都从零开始。可加「同类任务的计划复用」——但**必须走 goal_verifier**，不能因为「上次这么干成功了」就跳过验证 |
| 4 | 更细的权限模型 | 现在 `Principal` 有 `read_only` + `devices`。可加：按动作类型的权限、按 App 的权限、时间窗口权限 |

### 20.4 明确**不建议**做的事

| 不建议 | 为什么 |
|---|---|
| 拆微服务 | 层是模块不是服务；拆开立刻破坏单进程前提与内存队列 |
| 上 K8s | 单进程 + 一台手机的场景，K8s 只带来复杂度 |
| 引 LangGraph / Multi-Agent 框架 | 现在是自写循环 + `RuntimeState`，状态机语义已经很清楚；引框架会把「执行状态机 + 事务边界 + Prepare→Effect→Commit」这套关键设计搅乱 |
| 为「有 RAG」而加 RAG | 当前 UI 树是结构化数据，检索价值存疑；硬加只会增加不确定性和失败面 |
| 打开 `SHADOW_ALLOW_MULTI_PROCESS` | 这是逃生口不是功能。多 worker 会让「内存队列」「单写者」「租约」三件事同时失效 |

---

## 二十一、关键联系人 / 依赖系统

### 21.1 关键联系人

当前代码仓库未提供相关信息。

（全仓库扫描了 `README.md`、`pyproject.toml`、`android/README.md`、
`api/server.py` 头部注释、`scripts/` 各脚本，**未发现**维护者姓名、
邮箱、Slack/企微群、工单系统等联系信息。`origin` 指向
`https://github.com/Han-Mao/Shadow.git`，但**仓库内无对应联系人声明**。）

### 21.2 外部依赖系统

| 系统 | 用途 | 是否必需 | 配置变量 | 备注 |
|---|---|---|---|---|
| **VLM / LLM 服务** | 计划生成、动作决策、重规划、结果验证、任务关系分类 | **必需**（无则 Agent 主循环无法运行） | `VLM_BASE_URL`、`VLM_API_KEY`、`VLM_MODEL` | 任何 OpenAI Chat Completions 兼容网关皆可；默认 `https://api.openai.com/v1` + `gpt-4o` |
| **Android 设备（真机）** | 被操作的目标 | **必需**（测试用 FakeDevice 除外） | `SHADOW_DEVICE_BACKEND`、`SHADOW_ADB_SERIAL` 等 | 需开启无障碍服务 |
| **ADB** | PC 侧设备控制（`adb` 后端） | `adb` 后端下必需 | — | `device/adb.py` |
| **手机端点服务** | 手机侧设备端点（`android` 后端） | `android` 后端下必需 | `SHADOW_ENDPOINT_URL`、`SHADOW_ENDPOINT_TOKEN` | 手机 APK 内 `DeviceEndpointService`，端口 8765 |
| **Android SDK / build-tools / kotlinc / JDK** | 编译打包 APK | 仅在需要重新打 APK 时必需 | `SHADOW_SDK_DIR`、`SHADOW_BUILD_TOOLS_DIR`、`SHADOW_KOTLINC_DIR`、`SHADOW_JAVA`、`SHADOW_ANDROID_JAR` | 不需要 Android Studio；`android/tools/_toolchain.py` |
| 文件系统（本地盘） | SQLite 库、轨迹、请求审计 | **必需** | `ARTIFACT_DIR`（及 `STORAGE_DIR` 等派生子路径） | Android 上必须指向应用私有目录 |

### 21.3 明确**不依赖**的系统

**没有**：Redis、RabbitMQ、Kafka、Celery、MySQL、PostgreSQL、MongoDB、
Elasticsearch、Milvus/Pinecone/Chroma 等向量库、S3/OSS/COS 等对象存储、
Nginx、Consul/Nacos、Prometheus/Grafana、Sentry。

（这些在第十五章 15.1 有全仓库扫描证据，第十一章有环境变量扫描证据——
**没有任何一个变量的名字与这些系统相关**。）

---

# 交接完成度评估

| 维度 | 状态 | 说明 |
|---|---|---|
| **项目理解** | ✅ **完整** | 项目定位、架构分层、各层职责边界均有代码与人证（`README.md` + `api/server.py` wiring + 各模块 docstring）。**但**：设计意图的载体（`DEVLOG.md` 2270 行、`bluewhale-shadow-phone/` 29 份文档）被 gitignore，**只拿到代码的人无法完整理解「为什么这么设计」**——第 2 章「必须知道的三件事」是我从代码和 README 反推的，原设计取舍的完整论证在那些文档里 |
| **本地启动** | ✅ **完整** | 依赖版本钉死（4 个直接依赖）；环境变量清单已按 `os.getenv` 全仓扫描（约 45 个，分 7 表带出处）；三重启动守卫、启动顺序、验收清单均已实测确认。测试可跑：**799 passed in 38.69s** |
| **部署** | ⚠️ **部分** | **无容器化、无 CI/CD、无 IaC**（已全仓扫描确认不存在）。两种真实方式（源码直跑、手机端点部署）的步骤已给全。**缺口**：① 无自动化部署流水线；② 无 supervisor 托管；③ 无日志落盘策略；④ **回滚无自动化**，只能 Git 回退 + 手工换库（15.4 已给出手法与地雷） |
| **数据库** | ✅ **完整** | 实测 `sqlite_master` 得出：`user_version=4`、5 张表、11 个显式索引 + 6 个自动索引，DDL 逐列确认（含 DEFAULT 值）。ER 图、主键/唯一约束/索引、枚举字段、事件 kind（22 种）、数据生命周期、实测行数均已给出。**唯一待确认**：`foreign_keys=0` 的取舍理由（PRAGMA 事实明确，**动机**无代码注释说明） |
| **API** | ✅ **完整** | 28 个路由装饰器经 AST 提取，**无一遗漏**；鉴权链（令牌解析 → 主体表 → 只读判定 → 设备/任务授权）已按代码给出；关键端点的调用链（含 `confirm` 的事务形态与两阶段 reserve/commit）已画出；错误码语义全表 |
| **AI 模块** | ✅ **完整** | **结论明确且经代码验证**：有 VLM 接入（可配网关、可重试、有退避）；有自写 Agent 循环 + 有界计数器 + 四级防死循环；有独立 `goal_verifier` 裁定；**无 RAG、无向量库、无 embedding、无 MCP、无 LangGraph、无 Multi-Agent**。Prompt 版本（`plan_v3`/`decide_v4`/`replan_v1`/`verify_v1`/`relation_v1`）已列表，**未附完整 prompt 正文**（非核心资产，且长） |
| **故障排查** | ✅ **完整** | 启动失败 10 条、API 报错矩阵 + 4 条真实定位命令、VLM 失败 7 种现象、设备/采集 8 种、RAG **不适用**（已明确说明）、Agent 死循环 4 层防护 + 4 种真实形态、队列积压（含「无 MQ」的说明）、`UNKNOWN` 专项处置流程。**关键**：`executions.note` 字段被点明为最直接的失败原因 |

**整体评估**：**代码层面的交接是完整的、可执行的**——依赖、启动、API、数据库、
测试命令、排障路径都有代码依据，且测试实测全绿。**主要缺口集中在「非代码资产」**：
设计文档与测试不在版本控制里，导致「理解为什么」和「验证对不对」这两件事
在有远程仓库的情况下做不到。

---

## 新接手开发人员开始工作前，必须向原开发人员确认的事项

**优先级从高到低**：

1. **`tests/` 为什么被 gitignore？有没有一份可直接取的测试副本？**
   这直接决定你能不能验证自己的改动。当前只能 `git checkout <commit> -- tests scripts`
   猜测一个可用 commit。

2. **`DEVLOG.md`（2270 行）和 `bluewhale-shadow-phone/`（29 份文档）能否提供？**
   这是「为什么这么设计」的唯一载体。没有它，你会反复质疑一些**刻意的正确决策**
   （单进程硬前提、影子动作不回落、`UNKNOWN` 不重试），并可能"修好"它们。

3. **`storage` 从 `pyproject.toml` 的 `packages` 里漏掉，是有意还是笔误？**
   如果是无意，为什么之前没人撞到？（答案大概率是"从没打过 wheel"。）

4. **`artifacts/state/confirmations.db` 和 `events/events.db` 可以删吗？**
   它们是 V4 之前的遗留。确认没有其他工具在读它们之后应清理。

5. **影子平面（V5）的下一阶段计划是什么？**
   现在 `shadow_*` 在真机全失败是设计如此。要知道「什么时候会有真正的
   Shadow Display」才能判断该不该在影子链路上投入。

6. **`foreign_keys=0` 的取舍理由？**
   PRAGMA 事实清楚，但**为什么关**没有注释。开启前需要知道是否踩过什么坑。

7. **VLM 的服务商与配额情况？**
   代码默认指 OpenAI，但实际用的哪个网关、有没有速率限制、
   `gpt-4o` 是不是生产模型——都需确认。另外 `temperature` 未设、
   `max_tokens` 硬编码 512，是否有意为之。

8. **真机测试的环境？**
   有没有一台专用测试手机、什么型号、什么 Android 版本、
   无障碍服务怎么开、`android/README.md` 里那 6 条已知限制是否已验证。

9. **单进程前提有没有被打破过？**
   即有没有人设过 `SHADOW_ALLOW_MULTI_PROCESS=1` 跑过生产。
   如果跑过，`shadow.db` 里可能已有不一致状态。

10. **`origin` 只有一个 `main` 分支且远程无测试，团队有没有内部镜像 / 私有 fork？**
    当前远程 120 个文件，**测试和设计文档都不在里面**。
