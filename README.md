#  Shadow Phone — V2

> **Shadow 是一个面向 Android 的任务级 Agent Runtime。**
> 它通过任务关系识别、动态调度、Checkpoint 与抢占恢复，让 Agent 在连续执行复杂手机任务的同时，
> 还能处理用户实时插入的新任务。

- V1（M1–M3）已完成：设备控制、视觉感知、Observe → Think → Act → Verify 闭环
- V2 在此之上补齐 **任务管理层**：任务模型、关系识别、调度抢占、检查点恢复
- 依据：《蓝色鲸鱼 Agent · 方案三》§09/§10 + 《V2 审核建议》

## 架构

```text
                        ┌──────────────────────┐
                        │      FastAPI API     │   HTTP 契约
                        └──────────┬───────────┘
                                   ↓
                        ┌──────────────────────┐
                        │     TaskManager      │   生命周期 / 指令注入
                        └──────────┬───────────┘
                                   ↓
                        ┌──────────────────────┐
                        │    TaskClassifier    │   规则 + 相似度 + LLM 融合
                        └──────────┬───────────┘
                                   ↓
                        ┌──────────────────────┐
                        │      Scheduler       │   排队 / 优先级 / 抢占 / 恢复
                        └──────────┬───────────┘
                                   ↓
                        ┌──────────────────────┐
                        │    AgentRuntime      │   Observe-Think-Act-Verify
                        │  + CheckpointStore   │   检查点写入与恢复校验
                        └──────────┬───────────┘
                                   ↓
                  ┌────────────────┴────────────────┐
                  ↓                                 ↓
            Vision / Grounding                  Device / Session
```

**决定这套系统上限的不是 VLM，而是 `TaskManager + Scheduler + Checkpoint + Runtime` 这四件套。**
VLM 决定「下一步点哪里」，调度与检查点决定「多件事怎么排队、被打断了怎么接着做」。

## 目录结构

```text
├── agent/
│   ├── runtime.py          # AgentRuntime：闭环执行（调度能力已移出）
│   ├── planner.py          # 语义级规划 + 结构化 Re-plan
│   ├── observer.py         # 采集 Observation（截图 + UI 树 + 上下文）
│   ├── executor.py         # Action → DeviceController（承诺永不抛异常）
│   ├── verifier.py         # 多级验证：Device → UI Tree → VLM
│   ├── task_manager.py     # 任务生命周期与指令注入
│   ├── classifier.py       # 任务关系识别（三层融合）
│   ├── risk_gate.py        # 统一风险门禁（策略风险 = 下限，模型只能抬不能降）
│   ├── reconciliation.py   # 动作对账：EFFECT_UNKNOWN → 继续/重做/重规划/找人
│   └── scheduler.py        # 优先级队列 / 抢占 / 恢复
├── models/
│   ├── task.py             # Task + 8 态状态机 + 优先级 + 预算 + 版本号
│   ├── task_step.py        # TaskStep：计划是可追踪的状态机，不是字符串列表
│   ├── task_relation.py    # TaskRelation：5 种任务关系
│   ├── checkpoint.py       # Checkpoint：恢复所需的最小状态 + 版本门控
│   ├── action.py           # Action + 风险等级（策略下限）+ 指纹 + 动作效果状态
│   ├── budget.py           # TaskBudget：三独立预算（动作步数 / 观察 / 模型调用）
│   ├── retry.py            # ErrorClass + RetryPolicy：错误分类与重试的唯一真相源
│   ├── verification.py     # ActionDispatch / ActionEffect / GoalVerification 三概念
│   └── state.py            # Observation / StepOutcome
├── storage/                # TaskStore / CheckpointStore / TrajectoryStore / EventLog
├── device/
│   ├── adb.py screenshot.py accessibility.py emulator.py
│   ├── session.py          # DeviceSession：设备所有权与抢占交接
│   └── input.py            # InputProvider：ASCII 与中文输入通道
├── vision/                 # vlm / grounding / parser
│   └── fingerprint.py      # UI 结构指纹：恢复校验的 L2（比 package 细、比 VLM 便宜）
├── api/server.py           # FastAPI
├── scripts/demo_preemption.py   # 抢占恢复演示（离线可跑）
└── tests/                  # 154 个离线用例
```

## 职责边界

| 模块 | 职责 | 不做什么 |
|---|---|---|
| `scheduler.py` | 谁现在用设备：排队、优先级、抢占、恢复 | 不理解页面，不执行动作 |
| `task_manager.py` | 任务生命周期与指令注入落点 | 不做调度决策 |
| `classifier.py` | 新指令与在跑任务是什么关系 | 不碰设备 |
| `runtime.py` | 怎么完成一个任务 | 不做调度 |
| `planner.py` | 下一步该做什么 | 不产出坐标 |
| `executor.py` | 怎么操作设备 | 不抛异常 |
| `verifier.py` | 这一步成功了没有 | 不改任务状态 |
| `checkpoint.py` | 中断后怎么接着做 | 不做决策 |
| `device/` | 真正控制 Android | 不暴露给上层业务 |

## 核心能力

### 1. 任务关系识别，而不是「一次 LLM 定生死」

```text
用户输入 → 规则预筛 → 相似度 → LLM 判定 → 融合 → 相似度否决
```

- **规则层**：「先……」「其中……」「马上……」这类引导词意图明确，可解释、零成本
- **相似度层**：中英混合 token 的 Jaccard 相似度
- **相似度否决**：规则说「这是子任务」，但两句话毫无交集时（例如「先帮我打开微信发消息」
  对「淘宝搜索运动鞋」）必须否决——否则会把无关任务塞进当前计划
- **LLM 层**：可选。没有 API Key 时自动退化为前两层，本地与 CI 都能跑

### 2. 抢占与恢复（V2 的核心 Demo）

```text
A 正在执行（淘宝搜索）
   ↓  用户插入 B：先帮我打开微信发消息
TaskClassifier  →  unrelated + HIGH
   ↓
A 落 Checkpoint → 让出设备 → 挂起
   ↓
B 执行完成
   ↓
A 恢复：先重新 Observe，比对恢复点
   ├─ 页面没变 → 接着原计划继续
   └─ 页面变了 → Re-plan（绝不盲目恢复）
```

跑一遍看效果：

```powershell
python scripts/demo_preemption.py
```

### 3. 每一步都有据可查

- `TaskStep` 记录每个计划步骤的状态、重试次数、最后一次动作与错误
- `Checkpoint` 保存「恢复所需的最小状态」（当前页、package/activity、结构指纹、已用预算、最近轨迹）
- 每个执行完的步骤都进 `TrajectoryStore`，供下一步决策取上下文
- 关键事件进 `EventLog`（`GET /tasks/{id}/events`）：抢占、对账、危险动作等待、失败原因

**轨迹与事件日志是两回事，不要合并**：轨迹给「下一步怎么决策」看，所以只留最近几条、会被裁剪；
事件日志给「事后到底发生了什么」看，所以只追加不裁剪。合成一个必然两头不讨好。

### 4. 服务重启后自愈

任务队列在内存里，进程一退出就没了；但任务本身持久化在 `storage/`。
所以 `Scheduler.start()` 的第一步是 **从磁盘重建队列**：

```text
启动 → TaskStore.list_active()
        ├─ running   → 上次进程被杀，动作已中断 → 重新入队，交给 Checkpoint 校验后重跑
        ├─ queued    → 重新入队
        ├─ waiting   → 重新入队（重启后重新决策一次，比沿用旧确认更安全）
        ├─ paused(被抢占) → 自动恢复，但仍排在抢占者之后
        └─ paused(用户暂停) → 保持暂停，不替用户做决定
```

没有这一步，重启后的任务会变成「从 `/tasks/{id}` 看还活着、但永远没人执行」的僵尸——
比直接失败更难排查。`Task.paused_reason` 就是为此而加：它区分「临时让位」和「用户暂停」。

### 5. 执行安全

- **风险分级**：`safe / caution / dangerous`。`resolved_risk()` 取 **策略风险（下限）与模型声明风险的较大值**：
  命中「发送/支付/删除/下单」等关键词即升级为危险动作；模型可以显式把风险抬到更高，
  **但绝不允许把策略判定的危险动作降级成安全**（防止模型乱标 SAFE 绕过 HITL）。
- **HITL 门禁**：危险动作挂起任务等人工确认，批准才放行；被否决的动作进黑名单，
  下次再出现直接换策略，不会陷入「请求确认 → 否决 → 再请求」的空转
- **死循环检测**：连续 3 次做出语义相同的动作（坐标容差 24px）即强制换策略，
  而不是继续 retry 同一个动作

## V2.1 第一轮改造（依据 `v2.1审核建议.md`）

审核文档共 27 节、分三轮。本轮只落地**第一轮 6 项「必须先改」**的缺口，刻意不做第二轮/第三轮的大改写
（文档本身也警告「不要一次改所有东西」）。变更全部向后兼容：公开 API 仍暴露 `max_steps`，内部映射为 `budget.max_action_steps`。

| # | 审核项 | 改动 | 落点文件 |
|---|---|---|---|
| 1 | §2 步数拆成三种预算 | 新增 `TaskBudget`（动作步数 / 观察次数 / 模型调用 三个独立上限）；Runtime 分别计数与熔断 | `models/budget.py`、`models/task.py`、`agent/runtime.py` |
| 2 | §17 任务版本号 | `Task.version` 单调递增；SUPER_TASK 改写目标时 `+1`，用于使旧计划/旧检查点失效 | `models/task.py`、`agent/task_manager.py` |
| 3 | §18 检查点版本门控 | `Checkpoint.task_version`；`validate()` 先比对版本，不一致直接 `STALE`，旧恢复点**绝对不续用** | `models/checkpoint.py`、`storage/checkpoint_store.py` |
| 4 | §5 动作效果状态 | `ActionEffectStatus`（NOT_STARTED/DISPATCHED/EFFECT_UNKNOWN/VERIFIED_*）；恢复时若上一步只 DISPATCHED，判 `EFFECT_UNKNOWN`、清空计划重规划，**绝不盲目重试** | `models/action.py`、`agent/runtime.py` |
| 5 | §6/§7 SUPER_TASK 真重构 | 注入 `SUPER_TASK` 时改写 `instruction`、+1 版本、清空计划与检查点、升 HIGH 优先级；运行中则请求抢占让出，下一安全点落 Checkpoint 后挂起 | `agent/task_manager.py`、`agent/scheduler.py` |
| 6 | §10/§11 统一风险门禁 | 新增 `AgentRiskGate`；`resolved_risk()` = `max(策略风险, 模型声明风险)`，**模型只能抬不能降**；`/actions` 危险动作直接 403 需人工确认 | `agent/risk_gate.py`、`models/action.py`、`api/server.py` |

**刻意推迟（第二轮前）**：UI 指纹 L3 语义校验、StepAttempt/PlanStep 拆分（§20）、
多设备 Lease、Replay、目录重构（§24）。

## V2.1 第二轮改造

第一轮把「不能错」的骨架补齐后，第二轮处理「语义含糊」的部分。

| # | 审核项 | 改动 | 落点 |
|---|---|---|---|
| 7 | §12/§13 重试语义 | 新增 `models/retry.py`：`ErrorClass`（TRANSIENT / ACTION_REJECTED / PARSE_ERROR / USER_DENIED / UNKNOWN / FATAL）+ `RetryPolicy` 唯一真相源。步骤级 `max_retries` 从策略派生，不再 Runtime 写 3、TaskStep 写 2 | `models/retry.py`、`task_step.py`、`runtime.py` |
| 8 | §五 完整对账 | 新增 `agent/reconciliation.py`：EFFECT_UNKNOWN 拆成 **已成功→继续 / 未成功→重做 / 页面不符→重规划 / 无法判断→找人** 四条路，替代原来只有「清空计划重规划」一条 | `agent/reconciliation.py`、`runtime.py` |
| 9 | §四 L2 结构指纹 | 新增 `vision/fingerprint.py`：取 class/text/content-desc/resource-id 做指纹，**刻意不含 bounds**（同页重绘 bounds 必然抖动，硬比会让 L2 永远判「变了」） | `vision/fingerprint.py` |
| 10 | §9 分关系阈值 | `is_actionable` 从统一 0.5 改为按关系查表：DUPLICATE 0.90 / SUPER_TASK 0.85 / INTERRUPT 0.80 / SUBTASK 0.65。INTERRUPT、SUPER_TASK 带二次确认标记 | `models/task_relation.py`、`task_manager.py`、`api/server.py` |

### 两个关键语义变化

**1. 错误先分类，再决定重试。** 以前 ADB 超时、付款被拒、JSON 解析失败、用户拒绝全都 `retry_count += 1`：

| 错误类别 | 应对 | 理由 |
|---|---|---|
| TRANSIENT（超时/离线） | 同一动作再试 | 抖动重试通常能过 |
| ACTION_REJECTED（付款被拒） | 换策略 | 重试同一动作只会重复触发副作用 |
| PARSE_ERROR（解析失败） | 重规划 | 要的是新决策，不是重发旧动作 |
| USER_DENIED（人工否决） | 换策略 | 绝不能换个说法再问一次 |
| UNKNOWN | 试探 1 次后转人工 | 判不出类别就别空转 |
| FATAL | 立即放弃 | 不可逆 |

**2. 危险动作在无法确认成功时，绝不自动重试。** 对账若发现「页面没变、动作可能没生效」，
普通动作重做一次；但危险动作（付款/发送/删除）一律转人工确认——
宁可多问一次人，也不能重复扣款、重复下单。

### 行为变化提醒

- **无 LLM 时关系判定更保守**：纯规则融合上限约 0.60（公式 `(0.3×rule + 0.1×sim)/0.4`），
  够不到 SUBTASK 的 0.65 门槛，因此**不会**并入在跑任务的计划，改为各跑各的。
  配置了 VLM 后融合分可达 0.78+，正常并入。这是刻意的——证据不足时不擅自改动用户的计划。
- **SUPER_TASK 需显式放行**：改写正在执行的任务目标不可逆，
  未带 `allow_disruptive=true` 时返回 `needs_confirmation`，一个字都不会改。

## V2.1 第三轮改造

第二轮解决「语义含糊」，第三轮补**可追溯性与可观测性**——
出问题时能回答「它到底做过什么、为什么这么做」。

| # | 审核项 | 改动 | 落点 |
|---|---|---|---|
| 11 | §21 数据结构补全 | `Task` 加 `plan_version` / `active_step_id` / `relation_meta`；`Checkpoint` 加 `action_attempt_id` / `screen_fingerprint` / `semantic_state` / `budget_used` | `models/task.py`、`models/checkpoint.py` |
| 12 | §19 验证三概念 | 新增 `models/verification.py`：`ActionDispatch`（发出）/ `ActionEffect`（效果）/ `GoalVerification`（目标）三元组 | `models/verification.py`、`agent/verifier.py`、`runtime.py` |
| 13 | §8 语义相似度 | `TaskClassifier` 可注入 `embedder`；启用后算 `semantic_similarity`，与 Jaccard 取较大值作为相关性。**不注入时行为不变**（离线零依赖） | `agent/classifier.py` |
| 14 | §23 事件日志 | 新增 `storage/event_log.py`（每任务一个 JSONL，只追加）+ `GET /tasks/{id}/events` | `storage/event_log.py`、`scheduler.py`、`runtime.py`、`api/server.py` |
| 15 | §14 抢占延迟 | 记录「请求让出 → 真正挂起」的耗时并暴露到 `/scheduler`，超阈值告警 | `agent/scheduler.py` |

### 三个值得单说的点

**1. 验证拆成三层后，`EFFECT_UNKNOWN` 才真正生效。**
以前 VLM 说「成功」就一律记 `VERIFIED_SUCCESS`。现在 `UI 树没变 + 动作本该改页面`
会被判 `EFFECT_UNKNOWN`——之后若进程崩溃，从这个恢复点续跑时会触发对账，
而不是把一次可能根本没生效的点击当成已完成。

**2. `version` 与 `plan_version` 分开。**
`version` 是**任务目标**的版本（SUPER_TASK 改写目标才 +1），
`plan_version` 是**计划**的版本（每次 Re-plan / 插入步骤都 +1）。
不分开的话，「目标没变、只是重新规划过」无法表达，旧 Checkpoint 只能一律作废。

**3. 抢占延迟是**度量**，不是硬性上限。**
单条 ADB 命令已经在设备上跑起来时，Python 侧没有安全的方式掐断它——
强行杀掉 adb 子进程会留下半截设备状态，比多等一会儿更糟。
所以这里做的是「记录真实延迟 + 超过 `MAX_PREEMPTION_LATENCY_SECONDS`(2s) 打 warning」，
让「高优任务被长命令堵住」这件事能被看见。真正的有界延迟需要给 ADB 调用加命令级超时。

**仍未做（下一批）**：§20 PlanStep/StepAttempt 拆分、§24 目录重构（此前已确认不改）、
多设备 Lease、Replay（事件日志已就位，它是 Replay 的前置）。

## 快速开始

```powershell
# 方式一：只装依赖
pip install -r requirements.txt

# 方式二（推荐）：装成可编辑包，之后从任意目录都能运行
pip install -e .

# 配置 VLM（OpenAI 兼容接口）
$env:VLM_BASE_URL="https://api.openai.com/v1"
$env:VLM_API_KEY="sk-..."
$env:VLM_MODEL="gpt-4o"

python -m api.server    # 监听 127.0.0.1:8010
```

### 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `ADB_SERIAL` | 目标设备 serial | `emulator-5554` |
| `VLM_BASE_URL` | VLM 接口地址 | `https://api.openai.com/v1` |
| `VLM_API_KEY` | VLM API Key；不设置则关系判定退化为纯规则 | 未设置 |
| `VLM_MODEL` | VLM 模型名 | `gpt-4o` |
| `VLM_DETAIL_PLAN` / `_DECIDE` / `_VERIFY` | 各阶段图片精度 | `low` / `high` / `low` |
| `ARTIFACT_DIR` | 截图落盘目录 | `artifacts/shots` |
| `STORAGE_DIR` | 任务与检查点持久化目录 | `artifacts/state` |
| `PORT` | API 端口 | `8010` |
| `SHADOW_DEBUG` | 置 1 时 500 响应回传异常摘要（默认脱敏） | 未设置 |

## API

### 任务

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/tasks` | 创建任务。默认后台执行，`wait=true` 同步等待（超时 504） |
| `GET` | `/tasks` | 列出全部任务 |
| `GET` | `/tasks/{id}` | 查询详情（含计划进度与待确认动作） |
| `POST` | `/tasks/{id}/pause` | 暂停 |
| `POST` | `/tasks/{id}/resume` | 恢复 |
| `POST` | `/tasks/{id}/cancel` | 取消 |
| `POST` | `/tasks/{id}/inject` | **执行中注入新指令**，由任务关系决定并入/排队/抢占 |
| `POST` | `/tasks/{id}/confirm` | 危险动作的人工确认 |
| `GET` | `/tasks/{id}/history` | 执行轨迹（给下一步决策看，会被裁剪） |
| `GET` | `/tasks/{id}/events` | **审计事件流**（只追加，含抢占/对账/失败原因） |
| `GET` | `/tasks/{id}/checkpoint` | 最新恢复点 |
| `GET` | `/tasks/{id}/shots/{n}` | 某一步的截图 |
| `GET` | `/scheduler` | 调度器状态（running / ready / paused / suspended + 设备归属） |

### 设备直连（单步调试）

`GET /devices` · `POST /tap` · `/text` · `/back` · `/screenshot` · `/observe` · `/actions`

`/text` 会自动选择通道：ASCII 走 `input text`，中文走 ADB Keyboard 广播，调用方不需要关心区别。

### 冒烟

```powershell
# 后台执行 + 轮询
$task = curl -X POST http://127.0.0.1:8010/tasks -H "Content-Type: application/json" -d '{"instruction":"打开设置并开启飞行模式","max_steps":8}' | ConvertFrom-Json
curl http://127.0.0.1:8010/tasks/$($task.id)

# 一条命令拿最终状态
curl -X POST http://127.0.0.1:8010/tasks -H "Content-Type: application/json" -d '{"instruction":"打开设置","wait":true}'

# 执行中插入新任务（演示抢占）
curl -X POST http://127.0.0.1:8010/tasks/$($task.id)/inject -H "Content-Type: application/json" -d '{"instruction":"先帮我打开微信给张三发消息","priority":"high"}'

# 看调度器在干什么
curl http://127.0.0.1:8010/scheduler
```

### 状态码

| 码 | 含义 |
|---|---|
| `409` | 设备忙 / 任务状态不允许该操作（如取消一个已结束的任务） |
| `422` | 请求体参数非法（如 `target` 不是坐标或字符串） |
| `502` / `503` | 设备不可用 / VLM 调用失败 |
| `504` | `wait=true` 超时，任务仍在后台执行，可继续轮询 |

## 测试

```powershell
pip install -r requirements.txt
python -m pytest -q
```

**193 个用例，全部离线**：不需要 adb、模拟器或 API Key。

| 文件 | 覆盖 |
|---|---|
| `test_models.py` | 任务状态机、步骤依赖、动作风险（策略下限）/指纹、Checkpoint、预算、版本号 |
| `test_device.py` | ADB 封装、输入通道（含中文）、设备会话所有权与并发 |
| `test_vision.py` | UI 树容错、坐标落点、VLM 重试、prompt 构造 |
| `test_verifier.py` | **验证三概念**：发出 / 效果 / 目标，含「VLM 说成功但页面没变 → 效果存疑」 |
| `test_event_log.py` | 事件日志：顺序、按任务隔离、limit、截断行容错、写失败不抛异常 |
| `test_classifier.py` | 三层关系判定、相似度否决、**分关系阈值**、二次确认标记、**语义相似度** |
| `test_scheduler.py` | 优先级、暂停/取消、**抢占与恢复**、组合式中断、**启动恢复**（含审批前重启）、**抢占延迟观测**、设备占用 |
| `test_runtime.py` | 闭环执行、异常收敛、死循环、HITL、Checkpoint 恢复、**三预算门控**、**动作对账**（继续/重做）、批准一次性、**事件流** |
| `test_api.py` | HTTP 契约、状态码语义、错误脱敏、危险动作拦截、SUPER_TASK 改写与二次确认、**依赖链迁移**、版本门控、**/events 审计流** |

## 注意事项

- `/text` 仅接受安全 ASCII（字母、数字及 `_.@,/?!`），空格转义为 `%s`；
  中文走 ADB Keyboard 广播（设备需安装 `com.android.adbkeyboard`）。
- 坐标解析：`target` 可为 `{"x":..,"y":..}`、`"x,y"`、`"x1,y1,x2,y2"`、`"[x1,y1][x2,y2]"` 或元素描述文本。
  0~1 之间按归一化比例换算，其余按像素；换算基准取 `wm size` 的 **Override size**（实际渲染尺寸）。
  解析失败会抛出明确错误，不会静默回退到屏幕中心。
- **单设备串行**：全局只有一台目标设备，所有写操作经由 `DeviceSession` 串行化；
  扩展多设备前需要把会话拆成 per-serial。
- 执行器与观察阶段都承诺「不抛异常」，失败统一收敛为 `ERROR` 步骤并计入重试熔断；
  任务一旦启动，任何异常都会先把状态落为失败态，不会留下卡在 `running` 的僵尸任务。
- VLM 调用对 429 / 5xx / 网络错误做 3 次指数退避重试；4xx（除 429）不重试。
- 存储层默认是 JSON 文件（可读、便于演示），接口是窄方法集，
  换成 SQLite / PostgreSQL 只需替换实现类。
