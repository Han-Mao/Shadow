#  Shadow Phone — V2

> **Shadow 是一个面向 Android 的任务级 Agent Runtime。**
> 它通过任务关系识别、动态调度、Checkpoint 与抢占恢复，让 Agent 在连续执行复杂手机任务的同时，
> 还能处理用户实时插入的新任务。

- V1（M1–M3）已完成：设备控制、视觉感知、Observe → Think → Act → Verify 闭环
- V2 在此之上补齐 **任务管理层**：任务模型、关系识别、调度抢占、检查点恢复
- V2.1 分五轮落地《v2.1审核建议》的 21 项改造（预算/版本门控/重试策略/对账/指纹/回放/多设备）
- V2.2 修复《2.1存在的问题》点出的 12 项缺陷，重点在**安全与正确性**：
  危险动作不绕过确认、未知效果不当成功、完成需独立验证、多设备不互相干扰

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
                                                    │
                                        ┌───────────┴───────────┐
                                        ↓                       ↓
                              DeviceController(adb)   DeviceController(android)
                                PC 通过 ADB 控制         手机本机控制自己
```

**决定这套系统上限的不是 VLM，而是 `TaskManager + Scheduler + Checkpoint + Runtime` 这四件套。**
VLM 决定「下一步点哪里」，调度与检查点决定「多件事怎么排队、被打断了怎么接着做」。

V3.3 起 `Vision / Device` 这一层的左边是 `DeviceController` **协议**（`device/controller.py`）。
两个后端（ADB / Android）可以互换，上面所有东西一行都不用改——这正是「手机化」的落点。

## 目录结构

```text
├── agent/
│   ├── runtime.py          # AgentRuntime：闭环执行（调度能力已移出）
│   ├── planner.py          # 语义级规划 + 结构化 Re-plan
│   ├── observer.py         # 采集 Observation（截图 + UI 树 + 上下文）
│   ├── executor.py         # Action → DeviceController（承诺永不抛异常）
│   ├── verifier.py         # 多级验证：Device → 导航 → 结构 → 目标元素 → VLM
│   ├── evidence.py         # 多级证据：页面变没变、依据哪一层（V2.2 §六）
│   ├── goal_verifier.py    # 目标验证：完成是「申请」，由独立证据裁定（V2.2 §四）
│   ├── task_manager.py     # 任务生命周期与指令注入
│   ├── classifier.py       # 任务关系识别（三层融合）
│   ├── risk_gate.py        # 统一风险门禁（策略风险 = 下限，模型只能抬不能降）
│   ├── reconciliation.py   # 动作对账：EFFECT_UNKNOWN → 继续/重做/重规划/找人
│   ├── replay.py           # 任务回放：事件流 → 时间轴 + 「值得注意的地方」
│   ├── scheduler.py        # 优先级队列 / 抢占 / 恢复
│   └── execution/          # 动作级执行（v4.1 §九）
│       ├── state.py        #   执行状态机：合法迁移表 + 「崩溃后该落到哪个终态」
│       ├── service.py      #   ExecutionService：状态迁移与它的事件同一个事务
│       └── recovery.py     #   启动恢复：进程被杀后留下的非终态执行怎么收
├── models/
│   ├── task.py             # Task + 11 态状态机（含 DEGRADED / DEVICE_UNAVAILABLE / CANCEL_REQUESTED）+ 优先级 + 预算 + 版本号 + revision
│   ├── task_step.py        # TaskStep：计划是可追踪的状态机（只描述计划）
│   ├── step_attempt.py     # StepAttempt：一次尝试的经过（动作/结果/错误分类/证据层）
│   ├── task_relation.py    # TaskRelation：5 种任务关系
│   ├── checkpoint.py       # Checkpoint：恢复所需的最小状态 + 版本门控
│   ├── action.py           # Action + 风险等级（策略下限）+ 指纹 + 动作效果状态
│   ├── budget.py           # TaskBudget：三独立预算（动作步数 / 观察 / 模型调用）
│   ├── retry.py            # ErrorClass + RetryPolicy：错误分类与重试的唯一真相源
│   ├── verification.py     # ActionDispatch / ActionEffect / GoalVerification 三概念
│   └── state.py            # Observation / StepOutcome
├── storage/                # 各 store + SQLite 地基（database / migrations / event_store）
│                           # 任务 / 恢复点 / 事件 / 票据 / 执行记录同库（v4.1 §二）
├── device/
│   ├── controller.py       # DeviceController 端口 + DeviceError 家族（V3.3 §1：核心只依赖它）
│   ├── factory.py          # 按 SHADOW_DEVICE_BACKEND 装配后端（adb / android）
│   ├── adb.py              # ADB 后端（PC 侧）：adb -s <serial> ...
│   ├── android.py          # Android 后端（手机侧）：AndroidBridge 协议 + AndroidDeviceController
│   ├── remote.py           # 桥的远程传输：设备端点 HTTP 客户端（V3.3 §1）
│   ├── screenshot.py accessibility.py emulator.py
│   ├── session.py          # DeviceSession：设备所有权与抢占交接
│   ├── pool.py             # DevicePool：serial → 会话的注册表（多设备）
│   └── input.py            # InputProvider：ADB（ASCII / 中文广播）与 Android（ACTION_SET_TEXT）
├── vision/                 # vlm / grounding / parser
│   ├── fingerprint.py      # UI 结构指纹：恢复校验的 L2（比 package 细、比 VLM 便宜）
│   └── target.py           # 把动作目标还原成 UI 节点（风险判定与效果验证共用）
├── api/
│   ├── server.py           # FastAPI
│   └── auth.py             # 令牌 / 只读 / 设备范围 / 人工确认令牌（V2.2 §九）
├── android/                # 手机侧设备层 + 设备端点（Kotlin，V3.3）
│   ├── README.md           # 部署步骤 / 两条路线 / 权限 / 排障 / 无 SDK 的编译验证
│   ├── tools/              # 无 SDK 环境下的构建工具（见 android/README.md）
│   │                        #   verify_kotlin_compile.py：真编译 + JVM 单测
│   │                        #   build_apk.py：aapt2/d8/zipalign/apksigner → 可安装 APK
│   └── app/src/main/java/com/bluewhale/shadow/
│       ├── device/         # AccessibilityService / MediaProjection / Intent 启动 / 12 个桥方法
│       └── endpoint/       # 极小的 HTTP 设备端点（前台服务）
├── scripts/
│   ├── demo_preemption.py  # 抢占恢复演示（离线可跑）
│   └── replay_task.py      # 命令行回放一个任务的事件流
└── tests/                  # 719 个离线用例
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

# 回放某个任务（先 --list 看有哪些）
python scripts/replay_task.py --list
python scripts/replay_task.py 20260914_093100_demo01
```

### 3. 每一步都有据可查

- `TaskStep` 记录计划步骤的状态、依赖与重试上限；**每一次尝试**（动作、结果、错误分类、证据层）进 `StepAttempt`，只追加不覆盖
- `Checkpoint` 保存「恢复所需的最小状态」（当前页、package/activity、结构指纹、已用预算、最近轨迹）
- 每个执行完的步骤都进 `TrajectoryStore`，供下一步决策取上下文
- 关键事件进 `EventLog`（`GET /tasks/{id}/events`）：抢占、对账、危险动作等待、失败原因
- 事件流可以**回放**（`GET /tasks/{id}/replay` 或 `scripts/replay_task.py`）：
  按时间轴还原「它当时干了什么」，并把抢占/对账/失败这些异常帧单独挑出来

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

- **风险分级**：`safe / caution / dangerous`。判定走 `ActionRiskGate`（唯一入口），
  结果是 `max(策略风险, 模型声明风险)`，其中策略风险由**三路证据**合成：
  动作类型 + 动作文本关键词 + **UI 树上目标元素的真实文本**（见 V2.2 §一），
  再叠加敏感 App 页面下限。模型的风险表态进 `risk_hint`，只能抬不能降，
  降级尝试会被记录并忽略。
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

**仍未做（下一批）**：§24 目录重构（此前已确认不改）、多设备 Lease。
（Replay 已在第五轮完成。）

## V2.1 第四轮改造

第三轮补了可追溯性，第四轮收掉两个一直挂着的硬骨头：**执行记录被覆盖**和
**一次采集耗时没有上界**（后者直接决定抢占延迟）。

| # | 审核项 | 改动 | 落点 |
|---|---|---|---|
| 16 | §20 计划 / 尝试拆分 | 新增 `models/step_attempt.py`。`TaskStep` 只描述计划，执行记录全进 `attempts`；`retry_count` / `last_action` / `last_error` 改为派生属性 | `models/step_attempt.py`、`models/task_step.py`、`runtime.py` |
| 17 | §14 补：采集总预算 | `AdbController.deadline_budget()` + 读写超时分离（写 15s / 采集 6s）；`observe()` 整段包在 12s 总预算里 | `device/adb.py`、`agent/observer.py` |

### 1. 执行历史不再被覆盖

`TaskStep` 原来是「计划 + 执行记录」的混合体，而执行记录是**单值字段**：
同一个步骤失败三次，`last_action` / `last_error` 只剩第三次的内容。
可排查 Agent 问题最需要的恰恰是「它试过哪些没用的办法」——那些全被盖掉了。

拆开之后每次尝试都是独立的 `StepAttempt`（第几次、什么动作、结果、错误分类、证据来自哪层），
历史只追加不覆盖。调用方写法不变，`retry_count` / `last_action` / `last_error` 保留原名，
改成从 `attempts` 派生的只读视图。附带的好处：失败时也记下动作，
Re-plan 的上下文里终于有「上一个动作」可用了。

### 2. 一次采集终于有耗时上界

我上一轮说「要靠给 ADB 调用加命令级超时来解决抢占延迟」——**那句是错的**，
`AdbController` 一直有 `timeout=15.0`。真正的问题是 `observe()` 内部要跑 **6 条** adb 命令
（截图 + `wm size` + `dumpsys window` + rm + uiautomator dump + cat），
每条各自等 15s 累起来就是 90 秒，而采集正是 runtime 循环里最容易卡住的一步——
它的耗时直接等于高优任务要等多久。

修法是**总预算**而不是再加超时：

```python
with adb.deadline_budget(12):      # 整段采集共用 12 秒
    ...                            # 每条命令的超时 = min(自己的超时, 剩余预算)
```

预算耗尽就直接 `AdbBudgetExhausted`（继承 `AdbError`，上层失败收敛逻辑不用改），
不再启动下一条命令。于是采集耗时的上界从「6 条累加」降到「预算 + 1 条」，可论证。

顺带把超时分成两档：**采集类 6s、写类 15s**。采集卡住时继续干等没有收益——
任务不会因为多等几秒就拿到页面，只会把安全点一直往后拖；而 `am start` 拉冷启动 App
确实可能慢，等一等是有意义的。

## V2.1 第五轮：回放

`EventLog` 落盘之后，回放是它的第一个真正用途。

| # | 审核项 | 改动 | 落点 |
|---|---|---|---|
| 18 | §23 Replay | 新增 `agent/replay.py`：事件流 → 时间轴 + 异常帧 + Markdown 报告 + 动作计划 | `agent/replay.py`、`api/server.py`、`scripts/replay_task.py` |
| 19 | 事件自足性 | 事件补上动作细节（坐标 / 输入值 / 指纹 / 截图路径 / 证据层），使事件流**脱离内存态也能回放** | `agent/runtime.py`、`storage/event_log.py` |

### 为什么数据源必须是 EventLog

`TrajectoryStore` 是内存态、只留最近 200 条、进程一重启就没了。
而最需要回放的时刻，恰恰是**任务失败或进程崩溃之后**——那时轨迹已经没了。
所以回放只认落盘的事件流，并且事件必须**自足**：

```python
# 只记动作类型的话，回放看不出它当时点在哪
self._emit(task.id, ACTION_DISPATCHED, action=action.type.value, ...)
# 补上细节后才回放得出来
self._emit(task.id, ACTION_DISPATCHED, ..., target={"x": 540.0, "y": 1613.0})
```

### 报告长什么样

```
$ python scripts/replay_task.py 20260914_093100_demo01
# 任务回放 `20260914_093100_demo01`

- 事件数：14  时长：17.9s  动作：2（其中危险动作 1）

## 值得注意的地方
1. `t+3.900s` **preempt_requested** —— 被 ...ff00 抢占（high > normal）
2. `t+4.170s` **suspended** —— 让出设备（原因 preemption，已执行 1 步）
3. `t+9.700s` **recovered** —— 重启后恢复（queued(from paused)）
4. `t+10.500s` **waiting** —— 命中危险动作 tap（dangerous），等待人工确认
5. `t+15.300s` **reconciled** —— 动作对账 → ask_human：页面无变化，危险动作可能未生效…
6. `t+17.900s` **failed** —— 失败：无法判断上次动作是否生效，等待人工确认超时
```

「值得注意的地方」放在最前面是刻意的：跑成功的任务没什么好看的，
**出问题的那几帧才是**。完整时间轴在下面，按「生命周期 / 动作 / 恢复 / 人工」分了阶段。

### 动作重放默认不执行

`agent/replay.py` 也提供动作序列重放，但**默认 `dry_run=True`，一个动作都不执行**。
要真执行必须同时满足三件事：显式 `dry_run=False`、传入 `execute` 回调、
（若含危险动作）`allow_dangerous=True`，缺一就抛 `ReplayRefused`。

理由很直接：手机上的动作有真实副作用，盲目重放一个「提交订单」比不重放危险得多。
而且重放前必须自己确保设备处在对应恢复点的状态，否则页面上下文对不上、结果没有参考价值——
**生产环境要复现问题，用观测回放 + 恢复点，不要重放动作。**

## V2.1 第六轮：轨迹落盘 + 多设备

这两件事的共同点：都在拆掉 V2 里「单设备 + 内存态」的默认假设。

| # | 审核项 | 改动 | 落点 |
|---|---|---|---|
| 20 | 轨迹落盘 | `TrajectoryStore` 支持 `root`：JSONL 追加 + 定期紧凑化，重启后轨迹还在 | `storage/trajectory_store.py` |
| 21 | §十三 多设备 Lease | 新增 `device/pool.py`；`Task.device_serial`；**Scheduler 车道化**（每设备一条车道 + 一个 worker）；Runtime 按绑定取会话 | `device/pool.py`、`models/task.py`、`agent/scheduler.py`、`agent/runtime.py` |

### 轨迹落盘：两个刻意取舍

长跑任务重启后最难受的不是「恢复点丢了」，而是**恢复点在、模型却失忆了**——
前面几步干了什么全没了，只能盯着当前一屏重新猜，跟从零开始差不多。

1. **不存 `ui_tree`**。它是单条观察里最大的字段（几十 KB），而决策**根本不读它**
   （进 prompt 的是 `to_prompt_dict()`，字段白名单里没有 ui_tree）。
   存一条记录从几十 KB 降到几百字节。要看页面有截图路径，或 Checkpoint 里的快照。
2. **JSONL 追加 + 定期紧凑化**。每步重写整个文件是 O(n²)，长跑任务越跑越慢；
   纯追加又会无限增长，所以每追加 `max_entries` 条就重写成最后 `max_entries` 条。

### 多设备：车道模型

```
Scheduler
 ├── lane("emu-1")  ← DeviceSession(emu-1) + 就绪队列 + 挂起区 + running + worker 线程
 └── lane("emu-2")  ← DeviceSession(emu-2) + 就绪队列 + 挂起区 + running + worker 线程
```

一台设备 = 一条车道，各自持有自己的队列和运行槽。把这三样从 Scheduler 的全局字段
下沉下来，是多设备能成立的关键——否则两台设备会共用一个 `running`，互相覆盖状态。

**任务一旦开始执行就绑定设备**（`task.device_serial`）：中途换设备会让页面上下文对不上，
等于把任务丢到一台陌生手机上接着做。未绑定的任务由调度器派给最闲的一台；
绑定的设备不在池里（拔线/换机）时**绝不改派**——任务转 `DEVICE_UNAVAILABLE`
等原设备回来（V2.3 起），改派等于把任务上下文悄悄丢到另一台手机上。

单设备时只有一条车道，与旧实现逐字等价——这一点由当时的 257 个既有测试守着
（V2.7 修复轮之后合计 448 个）。

### 多设备暴露出的两个正确性问题

改这一轮时发现两处**不修就是 bug** 的地方：

1. **Runtime 必须按任务绑定的设备取会话。** 它原来持有单个 `self._session`，
   多设备下第二个设备的任务会被发到第一台上执行——「多设备」成了摆设，
   真实场景下等于**去操作了错误的手机**。现在按 `task.device_serial` 现查（刻意不缓存：
   缓存即共享可变状态，而共享状态正是并发 bug 的来源）。
2. **Runtime 现在会被多个 worker 线程并发调用。** `_states` 是共享可变状态，
   读写必须加锁。

### 顺带修正的一处顺序

`submit()` 原来先唤醒 worker、再落盘。多设备改造中把它改成**先落盘、再唤醒**——
反过来的话 worker 可能在任务还没持久化时就开始跑，进程恰在此刻崩溃就会把任务整个丢掉
（队列是内存的，磁盘上没记就等于没提交过）。

**仍未做**：§24 目录重构（已确认不改）、真正的负载均衡（当前只是「挑最闲的一条」，没考虑设备异构性）、
多设备的截图/产物分目录（`device.pool.storage_hint` 已备好，尚未接线）。

## V2.2 修复轮（依据 `2.1存在的问题.md`）

这一轮不堆功能，只修一个静态审查文档点出的 12 个问题。审查是针对**更早的提交**做的，
所以第一步是逐条核对现状：**5 项已在 V2.1 各轮中修过**（风险门禁、动作效果状态、
检查点版本门控、部分对账、分关系阈值），**7 项仍然真实存在**，包括审查标为 P0 的两项。

| # | 审查项 | 现状核对 | 改动 | 落点 |
|---|---|---|---|---|
| 1 | P0 风险门禁只是 `resolved_risk()` 的包装 | 部分存在：`max(policy, model)` 已实现，但**门禁拿不到页面上下文** | 门禁拆出显式的 `policy_risk` / `model_risk` / `declared_risk`，并把 **UI 树上目标元素的真实文本**纳入策略风险 | `agent/risk_gate.py`、`models/action.py`、`vision/target.py` |
| 2 | P1 `preempt_running()` 不带 task_id | **存在** | 传 `current.id`；不带参时打 warning；自身抢占也计入延迟观测 | `agent/task_manager.py`、`agent/scheduler.py` |
| 3 | P1 重新观察失败被当成 OK | **存在** | 落 `EFFECT_UNKNOWN`，并在**下一个安全点就地在线对账**（继续/重做/找人） | `agent/runtime.py`、`agent/reconciliation.py` |
| 4 | P1 `DONE` 是一句模型就能结束任务 | **存在** | 新增 `GoalVerifier`：计划、页面推进、可核验声明三条独立证据；有反证才驳回。新增动作别名 `DONE_REQUEST` | `agent/goal_verifier.py`、`agent/runtime.py`、`agent/verifier.py` |
| 5 | P1 预算新旧语义混用 | **存在** | API 新增 `budget`（三个独立上限）；`max_steps` 保留为兼容层并写死映射规则 | `api/server.py`、`models/budget.py` |
| 6 | P2 UI 树变化判断只比 clickable label 集合 | **存在** | 换成多级证据：L2 导航 / L3 结构指纹 / L4 目标元素状态，并记录「结论依据哪一层」 | `agent/evidence.py`、`agent/verifier.py` |
| 7 | P2 VLM 未知结论被默认当成功 | **存在** | `VerifyResult` 严格枚举；解析不出抛 `VlmVerifyError`，走「证据不足」分支 | `vision/vlm.py` |
| 8 | P2 风险关键词漏判 | **存在** | 关键词扩表 + 目标元素文本/resource-id + 敏感 App 页面下限 | `models/action.py`、`agent/risk_gate.py` |
| 9 | P2 API 几乎没有认证授权 | **存在** | 令牌鉴权 + 只读令牌 + 设备级权限 + 人工确认令牌 + 请求审计；非回环绑定且无令牌时拒绝启动 | `api/auth.py`、`storage/audit_log.py`、`api/server.py` |
| 10 | P2 单设备兼容代码残留 | 部分存在 | `running_tasks()` 取代「唯一 running」；`_running` 降级为兼容视图并标注 | `agent/scheduler.py`、`agent/task_manager.py` |
| 11 | §11 SUBTASK 注入不更新版本号 | **存在** | `_merge_subtask` 递增 `plan_version`；`Checkpoint` 记录 `plan_version` 便于回溯 | `agent/task_manager.py`、`models/checkpoint.py` |
| 12 | 缺端到端状态机测试 | 部分存在 | 新增双设备跨设备干扰、SUBTASK 注入 + 崩溃恢复、效果未知对账、完成申请驳回等 57 条用例 | `tests/test_multi_device_e2e.py`、`test_risk_gate.py`、`test_goal_verifier.py`、`test_evidence.py`、`test_api_auth.py` |

### 四个必须修的，各自到底改了什么

**1. 风险判定现在看得见页面。** 之前门禁只是 `resolved_risk()` 的一层包装，
它只看得到动作自己。于是这种情形一路绿灯：

```json
{"action_type": "tap", "target": "点击红色按钮", "risk_hint": "safe"}
```

那个红色按钮实际叫「立即购买」——**这个信息在 UI 树里，不在动作里**。
现在门禁把目标坐标还原成 UI 节点（取包含该点的**最小**节点），
拿它的 text / content-desc / resource-id 一起参与关键词判定。
模型的风险表态改名为 `risk_hint`（建议），与 `risk`（权威标注）分开存，
降级尝试会被记录并忽略——审计能回答「这级风险到底是谁定的」。

**2. 效果未知不再是成功。** 旧路径：

```text
点击发送 → ADB 成功 → 截图失败 → 「未验证的 OK」→ 下一轮模型看到页面没变 → 再点一次 → 重复发送
```

现在 `EFFECT_UNKNOWN` 会触发**在线对账**：下一个安全点拿到新观察后，
比对该动作发出前的页面结构，四条路分别是继续 / 重做一次 / 重新规划 / 转人工。
危险动作在缺乏强证据时一律转人工——宁可多问一次人，也不能重复扣款。

**3. 完成变成「申请」。** `DONE` 不再直接结束任务。`GoalVerifier` 用三条**不依赖模型自述**
的证据裁定：计划是否跑完、页面是否真的推进过、模型给的可核验声明是否与真实页面相符。

```text
VLM → DONE_REQUEST → GoalVerifier → 确认 / 打回继续做 / 转人工裁定
```

诚实地说清这条边界：**证据不足 ≠ 有反证**。没有证据的完成申请会被如实记为
`uncertain` 并放行（否则 Agent 会变得不可用），只有拿到反证才驳回。
驳回结果全部进事件流（`goal_requested` / `goal_rejected` / `goal_confirmed`），
所以「它凭什么说完成了」事后查得出来。

**4. API 有了四层防护。** 令牌、只读、设备范围、确认令牌，外加请求审计。
刻意保住「本地开发零配置」：不配令牌就不鉴权；一旦配上，四层同时生效。
另外有一条硬约束——**绑定非回环地址却没有令牌时直接拒绝启动**，
防止「图省事改个 HOST 就裸奔上线」。

### 行为变化提醒

- **完成变得更保守**：模型声称完成时若既没走完计划、页面一次都没推进、又没给理由，
  会被驳回并塞一个 Re-plan 理由继续做；连续驳回超过 `MAX_GOAL_REJECTIONS`(2) 次转人工。
  严格度默认**按任务画像自动分层**（见环境变量表）：纯查询走 `advisory`，
  导航 / 副作用 / 改设置走 `strict`。显式设 `GOAL_VERIFY_MODE` 会**整体覆盖**该分层。
- **拿不到验证观察时会重做一次动作**（普通动作）。这是有意的：页面结构完全没变
  说明上次很可能没生效；危险动作不在其列。
- **风险告警不再误报**：只有模型**明确声明**了更低的风险才算降级尝试，
  「没表态」不等于「说了 safe」。
- **`ActionType.DONE_REQUEST`** 是 `DONE` 的别名，模型可以直接用；
  两者都只表示「申请完成」。

## V2.4 修复轮（依据 `v2.4审查建议.md`）

这一轮审查同样是针对**上一版 main** 做的静态分析，所以照例先逐条核对再动手。
结论：文档列了 11 小节、其中 10 条是可核对的技术项，**1 条已在 V2.3 修掉**
（状态枚举补齐，#5 依赖它，随之消解），**其余 8 条仍然存在**。

### 那一轮把状态机补到 10 态

```text
CREATED → QUEUED → RUNNING ─┬→ DONE / FAILED / CANCELLED   （终态，不可逆）
                            ├→ PAUSED      （PAUSED_BY_USER 保持暂停；被抢占则自动恢复）
                            ├→ WAITING     （等人工确认，不进任何队列）
                            ├→ DEGRADED    （终态：关键持久化失败，禁止再产生副作用）
                            └→ DEVICE_UNAVAILABLE → QUEUED   （等原设备回来，绝不改派）
```

`DEGRADED` 与 `DEVICE_UNAVAILABLE` 是 V2.3 引入的**故障 / 恢复态**，
那一版只补齐了枚举与迁移表，这一轮把它们真正接进了并发与失败语义。

| # | 审查项 | 现状核对 | 改动 | 落点 |
|---|---|---|---|---|
| 1 | P0 `TaskStatus` 缺 `DEVICE_UNAVAILABLE` / `DEGRADED`，故障路径 `AttributeError` | **已在 V2.3 修掉**：10 态 + 迁移表 + 终态集合齐备 | 本轮只更正与实现不一致的注释与 README | `models/task.py`、`README.md` |
| 2 | P0 `DONE` 任务仍可被并发 `inject` 改写 instruction / plan | **存在**：`is_terminal` 只检查一次，检查与 `save()` 之间无保护（TOCTOU） | 改写改为「内存取最新 + revision CAS」，失败就退化成另起新任务 | `models/exceptions.py`、`storage/task_store.py`、`agent/task_manager.py` |
| 3 | P1 版本围栏挡不住「终态任务被重新描述」 | **存在**（围栏只管 Runtime 侧） | 同上：CAS 落在持久化层，与谁在写无关 | `storage/task_store.py` |
| 4 | P1 `SUBTASK` / `INTERRUPT` / `DUPLICATE` 同样有并发窗口 | **存在** | 所有「改当前任务」的入口共用一把 `_mutation_lock` | `agent/task_manager.py` |
| 5 | P1 入队后落盘失败 → 依赖 `DEGRADED` 才能降级 | 依赖项已在 V2.3 补齐 | 无需额外改动，补了回归用例 | `tests/test_models.py` |
| 6 | P1 `Runtime.run()` 首次 `_persist()` 不在 `try` 里，会逃成 `FAILED` | **存在** | 首次落盘纳入同一处理，走 `_degrade()` | `agent/runtime.py` |
| 7 | P1 Worker 兜底 `except Exception → mark(FAILED)` | **存在** | 按异常分流：`PersistenceError → DEGRADED`、`DeviceUnavailableError → DEVICE_UNAVAILABLE`、其余 → `FAILED` | `agent/scheduler.py` |
| 8 | P2 `stable` 容易被读成「UI 已静止」 | **存在**（只有 `before == after`） | docstring 精确化 + 响应新增 `stable_meaning` 字段 | `api/server.py` |
| 9 | P2 确认令牌是「能力票据」而非一次性审批 | **存在**（无 `jti`，TTL 内可反复使用） | 令牌加 `jti`，`/confirm` 改为**消费式**校验，同一张票据第二次提交被拒 | `api/auth.py`、`api/server.py` |
| 10 | P2 README 的「8 态」与代码分叉 | **存在** | README 与代码注释同步为 10 态 | `README.md`、`models/task.py` |

### 并发改写：为什么加的是 CAS，而不是只加一把锁

审查描述的场景很具体——任务刚被判 `DONE`，而 `inject()` 还拿着「之前读到的
`RUNNING`」准备改写目标，最后磁盘上出现：

```json
{ "status": "done", "instruction": "搜索京东上的手机" }
```

状态机发现不了，因为 `status` 根本没变，但任务语义已经被改掉了。三层防护：

```text
① _mutation_lock   把本模块内「读 - 判 - 改 - 存」串起来（inject / complete / fail /
                   resolve_confirmation / pause / resume / cancel 共用）
② 终态双检         内存与磁盘两边都确认「不是终态」——调度器内存那份可能比磁盘新
                   （刚判 RUNNING 还没落盘），只看磁盘会把它误判成「还没开始」
③ revision CAS     落盘时校验写入序号；期间被人写过就抛 ConcurrentModificationError，
                   本次改写不生效并回滚内存改动 —— 任务已结束则**另起一个新任务**
```

锁挡不住 Runtime（它拿的是同一批 Task 实例，却不经过这把锁），所以真正的兜底是 ③。

### 失败语义：`DEGRADED` 与 `FAILED` 不是一回事

```text
关键持久化失败 → DEGRADED           「状态落不了盘，别再产生副作用」
                                    （继续跑的话，崩溃恢复后会重复执行）
设备绑定不可用 → DEVICE_UNAVAILABLE  「等原设备回来」，绝不改派
真正的未知异常 → FAILED             「这条任务做不下去了」
```

以前 Worker 的兜底 `except Exception: mark(FAILED)` 会把前两者一起压成 `FAILED`，
于是「这条任务到底为什么停了」只能去日志里猜。

### 行为变化提醒

- **`/confirm` 的令牌只能用一次**：确认成功后同一张票据再次提交返回 `403`
  （原因写明「已被使用过」）。连续 `GET /tasks/{id}` 拿到的多张票据里，
  只有第一张能真正完成确认。
- **`SUPER_TASK` 不再改写已结束的任务**：任务已终态（或被并发改过）时，
  新指令会**新建一个任务**，而不是改掉旧任务的 `instruction`；
  返回的 `action` 相应为 `spawned`，不再是 `superseded`。
- **持久化失败的落状态会看到 `degraded`** 而不是 `failed`，
  这类任务不会被调度器重新捡起执行（防重复副作用）。
- **`/observe` 与 `/screenshot` 多了 `stable_meaning` 字段**，
  固定为 `no_known_shadow_write_during_observation`，用于消除对 `stable` 的误读。

### API 变更

| 变更 | 说明 |
|---|---|
| `stable_meaning`（新增响应字段） | `/observe`、`/screenshot` 新增，说明 `stable` 的语义，向后兼容 |
| `/confirm` 令牌格式 | 由 `<expires>.<sig>` 变为 `<expires>.<jti>.<sig>`；**旧格式令牌立即失效**（TTL 最长 300 秒，重新读取待确认事项即可） |
| `GET /tasks/{id}` 对损坏任务的响应 | 数据已隔离的任务不再返回 404，改为 200 + `status=recovery_error`（附 `quarantined_as` / `recoverable`）。**「不存在」仍然是 404**——两种故障语义被分开 |
| `GET /tasks` 新增 `corrupt` 字段 | 列出已隔离（损坏）的任务 id，避免它们从系统里静默消失 |

## V2.5 修复轮（依据 `v2.5审查建议.md`）

这一轮审的是**并发正确性**，10 条里 9 条成立（另 1 条是上一轮我自己埋的坑，见 #4）。

| # | 审查项 | 现状核对 | 改动 | 落点 |
|---|---|---|---|---|
| 1 | P0 CAS 不是原子的，可能两个写者都通过 | **存在**：`read()` 与 `write()` 各加各的锁，中间留了窗口 | 新增 `JsonStore.update_atomic()`，把「读 - 比较 - 递增 - 原子替换」放进同一临界区 | `storage/json_store.py`、`storage/task_store.py` |
| 2 | P0 改写后 save 失败不回滚内存 | **存在**：只捕获 `ConcurrentModificationError` | 任何 save 异常都先按 `model_copy(deep=True)` 备份回滚，再抛出去 | `agent/task_manager.py` |
| 3 | P1 损坏任务「第一次 404、第二次才 recovery_error」 | **存在**：隔离是惰性的，而索引只活在内存 | 启动时扫描 `quarantine/` 重建索引 + 单次请求内兜底复查 | `storage/task_store.py`、`api/server.py` |
| 4 | P1 `recovery_error` 检查在授权之前 → 存在性泄露 | **存在**（上一轮引入的） | 只有不受设备范围限制的令牌能看；受限令牌统一 404，`corrupt` 清单同样收敛 | `api/server.py` |
| 5 | P1 `GET /tasks` 读磁盘、`GET /tasks/{id}` 读内存 | **存在** | 新增 `Scheduler.tracked_tasks()`；`TaskManager.list_all()` 合并 live 优先 | `agent/scheduler.py`、`agent/task_manager.py` |
| 6 | P1 `complete()` / `fail()` 可绕过 Runtime 生命周期 | **存在** | 两者对 RUNNING 任务拒绝改写（返回 None）；Runtime 安全点补终态自检 | `agent/task_manager.py`、`agent/runtime.py`、`agent/scheduler.py` |
| 7 | P1 `CANCELLED ≠ 副作用已经停止` | **存在** | 新增 `CANCEL_REQUESTED`：运行中先落请求，安全点才落 `CANCELLED`；重启时直接落定 | `models/task.py`、`agent/scheduler.py`、`agent/runtime.py` |
| 8 | P1 `DEVICE_UNAVAILABLE` 缺恢复触发 | **存在** | `DevicePool.subscribe()` + `Scheduler.on_device_available(serial)`：设备上线自动恢复**它名下**的任务，必要时补建车道 | `device/pool.py`、`agent/scheduler.py` |
| 9 | P2 `_drop_from_queues` 不清理 `_device_unavailable` | **存在** | 一起清 | `agent/scheduler.py` |
| 10 | P2 `_completed` 会重复追加 | **存在**（而且无限增长） | 换成 `deque(maxlen=200)` + `_mark_completed()` 去重 | `agent/scheduler.py` |

### 状态机现在是 11 态

```text
CREATED → QUEUED → RUNNING ─┬→ DONE / FAILED / CANCELLED   （终态，不可逆）
                            ├→ PAUSED
                            ├→ WAITING
                            ├→ CANCEL_REQUESTED → CANCELLED / DONE
                            ├→ DEGRADED                （终态）
                            └→ DEVICE_UNAVAILABLE → QUEUED
```

`CANCEL_REQUESTED` 是这一轮新增的**意图态**：强杀线程不可能安全地「在动作执行到一半」
停下，所以「请求取消」和「已经停止」必须是两个状态。点击「发送」之后立刻取消时，
消息其实已经发出去了——状态停在 `CANCEL_REQUESTED` 才如实表达了这一点。

### 行为变化提醒

- **`POST /tasks/{id}/cancel` 对运行中的任务返回 `cancel_requested`**，等 Runtime 走到
  安全点才变 `cancelled`。指望「点一下就立刻没有任何副作用」是不可能的，状态如实反映。
- **`complete()` / `fail()` 不再接受 RUNNING 任务**（内部 API，当前没有 HTTP 入口）。
  要让运行中的任务结束，请走 `/cancel`。
- **设备重新上线会自动恢复它名下等待的任务**（`DEVICE_UNAVAILABLE → QUEUED`），
  不再依赖重启服务或人工 resume。
- **`GET /tasks` 与 `GET /tasks/{id}` 的状态现在一致**（都优先取调度器内存的实时状态）。

### API 变更

| 变更 | 说明 |
|---|---|
| `GET /tasks/{id}` 对损坏任务的可见性 | 只有**不受设备范围限制**的令牌能看到 `status=recovery_error`；受限令牌统一 404 |
| `GET /tasks` 的 `corrupt` 字段 | 对受限令牌返回空列表（同上理由） |
| `POST /tasks/{id}/cancel` | 运行中的任务状态变为 `cancel_requested`，安全点后（或重启时）落定 `cancelled` |

## V2.6 修复轮（依据 `v2.6审查建议.md`）

这轮审核是对上一轮的**反向验证**，专盯并发。9 条技术项里，**5 条已在 V2.5 修掉**
（#1 / #2 / #3 / #5 / #6——文档引用的是 V2.4 时期的写法，那段代码在本轮开始前就已不存在），
**2 条真实存在、本轮修掉**（#4 / #7），1 条架构建议本轮未做（#8），1 条是补测试（#9）。

| # | 审查项 | 现状核对 | 结论 |
|---|---|---|---|
| 1 | P0 CAS 不是原子的 | `TaskStore.save` 已走 `JsonStore.update_atomic()`——读 - 比较 - 递增 - 原子替换在同一把锁里 | **V2.5 已修** |
| 2 | P0 `expected_revision` + 记录不存在时反而允许写 | 现实现把「文件不存在」折算成 `actual = 0`，与 `expected=15` 不等即抛 `ConcurrentModificationError` | **V2.5 已修** |
| 3 | P0 损坏文件能被 CAS 写入「救活」 | 损坏时 `_read_unlocked` 抛 `CorruptDataError`，写入根本不会发生 | **V2.5 已修** |
| 4 | P1 运行中降级会提前清空 `lane.running` | **存在**：`_drop_from_queues` 无条件清运行槽 | **本轮修掉**（见下） |
| 5 | P1 `_persist_or_degrade` 重复追加 `_completed` | 已改为 `_mark_completed()` 去重 | **V2.5 已修** |
| 6 | P1 `get()` 与 `list_all()` 视图不一致 | `list_all()` 已合并 `Scheduler.tracked_tasks()`，live 优先 | **V2.5 已修** |
| 7 | P1 RUNNING 恢复可能重复副作用 | **存在**：无恢复点时直接重新规划执行 | **本轮修掉**（见下） |
| 8 | P2 Runtime/Scheduler 的 `save()` 不带 revision | 属架构演进建议 | 本轮未做（见文末） |
| 9 | 该补的故障注入测试 | 本轮补齐删除竞态 / 损坏竞态 / 运行中降级 / 崩溃恢复四条 | **本轮补上** |

### 「持久化失败」不等于「立即释放运行槽」

`cancel` 与持久化降级都可能发生在 Runtime **仍在执行**的那一瞬间。以前
`_drop_from_queues()` 会顺手把 `lane.running` 置空，于是调度器以为设备空了——
`running_tasks()`、抢占判断、API 状态同时失真，而真实世界里那条任务还在点手机。

```text
cancel 或 persist 失败
      ↓
_drop_from_queues()
      ↓
lane.executing 为真？  ← 新增判断：正在执行就不动运行槽
      ↓
运行槽只由 worker 自己在 _execute 的 finally 里释放
```

### 崩溃恢复必须先回答「上次那个动作发出去没有」

进程停在 `RUNNING` 就被掐断，说明上一次执行是半途消失的。以前 `recover()` 把它变回
`QUEUED` 就直接重跑；而**没有恢复点**的任务意味着我们既不知道动作发没发出、也没有
attempt 记录可比对——重跑可能就是第二条消息、第二笔订单。

```text
recover(): 磁盘上是 RUNNING  →  task.recovery_required = True
      ↓
runtime.run(): 有恢复点 → 走既有对账（validate + needs_reconciliation）
               无恢复点 → 转人工：WAITING + 留下原因，不自动执行
      ↓
人工 /confirm: 批准 → 清恢复标记 + 丢弃旧计划重新规划
               否决 → 落 DEGRADED 等人工处置
```

`confirmation_kind` 因此变成三种：**`dangerous_action` / `goal` / `recovery`**；
确认令牌的指纹也相应区分为动作指纹 / `goal` / `recovery`，三类票据不能互换。

### 这一轮没有做的事（如实说明）

审核 §8 指出根因：**CAS 只覆盖了「改写入口」，其他写入路径都是普通 `save()`** ——
整个 Task 写入协议并不统一：

| 写入点 | 是否带 CAS | 说明 |
|---|---|---|
| `TaskManager._rewrite_authoritative` | ✅ `expected_revision` | 改写当前任务（SUBTASK / SUPER_TASK） |
| `TaskManager.create / complete / fail`、恢复否决 | ❌ | 都在 `_mutation_lock` 内；`create` 本来就无旧记录可校验 |
| `Runtime._persist`（`runtime.py:1343`） | ❌ | 执行过程中的状态推进 |
| `Scheduler._persist`（`scheduler.py:670`） | ❌ | 调度过程中的状态推进 |

本轮**没有**做文档建议的 `TaskMutationService` 重构，理由：

- 单进程内这几处持有的是**同一批内存 Task 实例**，`task.revision` 随任一写者推进，
  磁盘序号与内存天然一致；硬加 `expected_revision` 不增加保护，反而会把
  「内存比磁盘新」（调度器先改内存、稍后统一落盘）这类**正常**情形误判成冲突；
- 真正的跨进程保护需要文件锁 / 数据库事务——已在待办里标为「多进程部署前必须先做」。

**这不是「忘了做」，而是带明确复查条件的延期**：两处 `save` 调用点旁边已经就地写了
注释，说明为什么这里不带 CAS、以及什么时候必须改。触发条件只有两个——**多进程 /
多实例部署**，或**存储换成 SQLite**；届时把写入统一收口成一个协议，而不是逐点补参数。

## V2.7 修复轮（依据 `v2.7审查建议.md`）

这轮文档自己声明「无法 clone，结论基于已读代码」，所以**逐条核对比以往更重要**。
文档共 **16 条**技术项：**3 条代码里已经做了**、**6 条本轮修掉**（含同轮补充的关系判定）、
**4 条属有理由的延期**、**3 条是代码质量待办**。

### 本轮修掉的四条

**P0-3 已绑定任务绝不回退默认设备。** `_session_for()` 原来是
`self._pool.get(task.device_serial) or self._default_session` —— 绑定设备消失后会**悄悄**
换成默认设备。这是跨设备上下文污染：A 任务停在微信页面、B 任务停在支付页面，把 A 的
恢复点拿到 B 上接着点，等于把任务丢进别人的手机。现在：已绑定 → 找不到就抛
`DeviceUnavailableError`（交给调度器落 `DEVICE_UNAVAILABLE`、等原设备回来）；
**只有尚未绑定**的任务才允许分配默认设备。两条新用例分别钉住「绑定的不回退」与
「未绑定的照常」。

**P0-2 人工批准只能放行「那一个」动作。** 原来的 `approved_dangerous: bool` 是个开关：
批准「确认付款」之后，**紧接着出现的任何危险动作**都会被静默放行——模型换个策略给出
「删除账户」，也照样直接执行。现在改成 `ApprovalGrant`：绑定**动作指纹 + 目标版本 +
计划版本**，放行前逐项匹配，对不上就作废并重新请求确认，而且**匹配成功即消费**（一次性）。

**P1-2 引入副作用幂等性，与风险等级分开。** 审核这句话是对的：
「风险等级解决*能不能做*，幂等性解决*做过但不知道结果时能不能再做*」。
新增 `SideEffectClass`（`read_only` / `idempotent_write` / `non_idempotent_write` /
`irreversible`）与 `Action.side_effect()`，派生规则保守（拿不准往重里判）。
对账判定 `RETRY` 之前先看它：非幂等 / 不可逆一律转人工，不再「重做一次」。

这条补的是一个真实盲区：`DANGEROUS_KEYWORDS` 覆盖了支付 / 发送 / 删除，但**点赞、关注、
收藏、分享、评论**不在里面——风险不高，可重做一次就是第二条。

**P1-8 确认令牌改为显式申请。** 原来 `GET /tasks/{id}` 会把可用的确认令牌直接放进响应。
那意味着日志、前端状态、代理缓存、浏览器调试工具都可能留下一张「能放行真实危险动作」的
凭据。现在 GET 只给元数据（含 `token_endpoint`），令牌改用新增的
`POST /tasks/{id}/confirmation-token` 显式申请，并且申请本身进审计。

### 代码里已经做了的（审核担心，但不必改）

| 审查项 | 现有实现 |
|---|---|
| P1-1 抢占被长 ADB 阻塞 | `adb.deadline_budget(seconds)` 给整段采集加总预算，每条命令超时取 `min(自己的, 剩余预算)`；采集类 6s、写类 15s。另有抢占延迟观测与超阈值告警 |
| P1-10 完成验证过度依赖页面变化 | `goal_verifier` 的三条独立证据里「页面推进过」只是其中之一；strict 模式还要求计划跑完 + 可核验声明与真实页面相符——页面变化**单独**不足以放行完成 |
| P1-7 只读端点是否真的只读 | **已强化**（见「V2.7 补充（五）」）：新增 `device.adb.is_read_only()` 结构化声明，只读端点调用的底层操作可查证；不再只靠端点名约定 |

### 有理由的延期（附触发条件）

| 审查项 | 为什么先不做 / 打算什么时候做 |
|---|---|
| P0-1 运行时状态只在内存 | **已在后续补充里按「有选择的持久化」做掉**（见下）：`denied_fingerprints` 落盘到 Task；`approval` / `pending_confirmation` / `goal_approved_by_human` **刻意不落盘**，理由写在代码里 |
| P1-5 状态迁移分散 | **已完成**（见「V2.7 补充（五）」）：`Task.apply_event` 事件驱动迁移，33 处 `task.mark(TaskStatus.X)` 全部改为 `apply_event(TaskEvent.X)`，语义映射集中到 `_EVENT_TO_STATUS` |
| P1-6 队列与持久化非原子 | **已完成**（见「V2.7 补充（五）」）：`resume()` / `on_device_available()` 的「入队先于落盘」顺序瑕疵修复，统一为「先落盘、再唤醒 worker」；跨进程 Task Lease 仍留待多进程部署 |
| P1-9 checkpoint 门控 | `task_version` 已门控（不匹配直接 STALE）；`plan_version` **刻意不门控**（页面没变就该能续跑，V2.2 §十一 的取舍）；`action_attempt_id` 通过 `needs_reconciliation` 参与「先对账、再继续」 |
| P2-1 / 2 / 3 | runtime 过大、错误分类依赖文本、fingerprint 语义分层——代码质量项，进待办 |

### V2.7 补充：关系判定（P1-3 / P1-4）

这两条原本被归为「算法改版，不该混进并发轮」，随后单独做掉了：

- 新增 `shared_terms()`：长度 ≥ 2 的实词交集，作为「共享对象」证据。
- 「相关性否决」从一个**单一裁决者**变成三件事一起看：

```text
明确语言标记（先… / 顺便…） 或 LLM 明确判依赖（conf ≥ 0.6）  → 豁免否决（防误杀）
否则：相关性与共享实词至少要有一个说得过去                    → 才承认是同一件事（防误并）
```

- 于是审核的两个反例都处理到位：`规划上海三日游路线` + `先帮我订酒店` 判 **SUBTASK**
  （语言标记撑住，字面几乎零重叠也无妨）；而 LLM 只给 0.5 置信度、两句又零共享时不再并入。
- `AFFINITY_FLOOR` 由 `0.08` 提到 `0.35` 并重新定位：它不再是「唯一否决器」，
  而是「既没有语言标记、也没有共享实词时」的那一档门槛。

### V2.7 补充（二）：确认状态哪些该落盘、哪些故意不落

审核要求「任何影响恢复后是否允许执行动作的状态，都不能只存在内存里」。核对之后结论是
**分而治之**，而不是一股脑全落盘：

| 状态 | 处置 | 理由 |
|---|---|---|
| `denied_fingerprints`（用户否决过的动作） | **落盘**到 `Task.denied_fingerprints` | 丢了只会在重启后又问一遍用户已经拒绝过的动作——骚扰之外，更让人以为系统没记住 |
| `approval`（危险动作放行凭据） | **不落盘** | 批准是针对**当时那一屏**给的；重启后页面可能早就变了，把批准带过重启等于执行一个用户从没真正看过的东西 |
| `pending_confirmation`（待确认动作） | **不落盘** | 同上；`recover()` 会重新决策并再次请求确认 |
| `goal_approved_by_human`（人工认定完成） | **不落盘** | 同样是「当时那一屏」的上下文 |

实现：`run()` 起始把 `task.denied_fingerprints` 载入运行时状态；循环每轮
`_sync_durable_state()` 把新增的否决回写到 Task，随下一次 persist 落盘。
`recover()` 对 WAITING 任务的恢复记录改成 `queued(from waiting, confirmation_reset)`——
让「确认上下文已作废」这件事**可见**，否则排查时会把「确认没了」当成 bug。

### V2.7 补充（三）：错误分类与指纹语义（P2-2 / P2-3）

**P2-2 错误分类改结构化优先。** 以前全靠正则匹配错误文本，而同一个错误在不同层措辞
不同（`device offline` / `adb: device offline` /「设备已离线」），迟早会漏。现在：

- `models.exceptions` 的每个异常自带 `error_class`（`InvalidTransitionError` / `PersistenceError`
  → `fatal`，`DeviceUnavailableError` / `ConcurrentModificationError` → `transient`）；
- executor 收敛异常时把 `classify_exception` 的结果塞进返回的 `error_class` 字段；
- `verifier` 透传到 `ActionDispatch.error_class`，runtime 分类**先读它**，文本只作兜底。

**P2-3 指纹语义显式化。** 核对后确认指纹**没有**被误用为全局动作身份——它只在本任务的
运行态里比较（`denied_fingerprints`、`recent_actions` 都挂在按 task_id 隔离的 `RuntimeState`），
死循环检测用的是带容差的 `is_same_as`。缺的是**说清楚**：`Action.fingerprint` 的 docstring
现在明确它回答「做的是不是同一个动作」，**不含**「哪一屏、哪一次尝试」——后者由
`current_attempt_id` 承担，两者分开存，不拿一个字段冒充两种语义。

### V2.7 补充（四）：runtime 拆解 + P1-5 的取舍（P2-1 / P1-5）

**P2-1 完成**：`runtime.py` 从 1475 行拆成 6 个文件，用 mixin 组合、零行为改动：

| 文件 | 职责 | 行数 |
|---|---|---|
| `runtime.py` | 主类骨架 + 设备选择 + 持久化/事件基础设施 | 297 |
| `_runtime_types.py` | `RunOutcome` / `RuntimeState` / `ApprovalGrant` 共享类型 | 124 |
| `_execution.py` | Observe→Think→Act→Verify 主循环 + 阶段方法 + 失败结算 | 816 |
| `_goal.py` | 完成申请与裁定（GoalVerifier 独立证据） | 172 |
| `_reconcile.py` | 效果对账（EFFECT_UNKNOWN → 继续/重做/换策略/问人） | 139 |
| `_confirm.py` | 人工确认（危险动作 / 完成裁定 / 崩溃恢复） | 113 |

拆解方法：**只做物理移动，不改任何行为**。方法之间通过 `self` 互相引用（`_emit` /
`_persist` / `_save_checkpoint` / `_ask_human`），用 mixin 共享同一实例——所以
`AgentRuntime` 的公共 API（`run` / `confirm` / `forget` / `is_goal_decision` /
`last_goal_check` / `pending_confirmation` / `recovery_pending`）逐字不变，448 个用例
零回归就是证明。

**P1-5（事件驱动状态迁移）本轮不做，如实说明**：文档建议把 Runtime / Scheduler 的
`task.mark(...)` 全部替换成「产生事件 → TaskManager.transition」的调用。核对后发现：
当前已有**唯一迁移实现**（`Task.transition_to`）+ **`source` 全量审计** + **终态硬闸**，
「迁移是否合法」这个正确性已经由状态机兜住；缺的只是「单入口」这个工程洁癖。而它牵涉
Runtime / Scheduler / TaskManager 三处全部写入点，回归风险远大于收益。留给真正需要
多进程 / 多写者时再和 V2.6 §8 的 TaskMutationService 一起做。

### V2.7 补充（五）：P1-5 / P1-6 / P1-7 收尾

**P1-5 事件驱动状态迁移**（上文的「不做」被推翻，用户确认要做）：

- 新增 `TaskEvent` 枚举 + `_EVENT_TO_STATUS` 集中映射 + `Task.apply_event(event, source=)`。
- 33 处 `task.mark(TaskStatus.X)` 全部改为 `task.apply_event(TaskEvent.X)`：
  各模块不再指定目标状态，而是声明「发生了什么」（如 `DISPATCHED`、`AWAITING_CONFIRMATION`、
  `DEVICE_LOST`、`PAUSED_BY_PREEMPTION`），目标状态由 `_EVENT_TO_STATUS` 唯一裁决。
- `mark` / `transition_to` 仍是底层原语，**终态硬闸与 source 审计的语义完全不变**——
  事件驱动是「语义收口」，不是「另起炉灶」。

**P1-6 队列/持久化原子性**：`resume()` 和 `on_device_available()` 原来「入队（push_ready）
先于落盘（persist）」，worker 可能在任务还没持久化时就被唤醒取走，进程恰在那刻崩溃会导致
`recover()` 重复投递。统一改为**先落盘、再唤醒 worker**（与 `submit` 同一条纪律）。

**P1-7 只读操作结构化声明**：新增 `device.adb.is_read_only(operation)` 与
`READ_ONLY_OPERATIONS` 集合。只读封装（`screenshot` / `dump_ui` / `screen_size` /
`current_focus` / `state`）返回 True；`shell` / `read_shell` 是万能口、保守返回 False。
「只读端点是否真的只读」从此可从代码查证，而不是靠人记住约定。

### V2.7 补充（六）：8 项复核的逐条处置

审查方复核 HEAD 后指出 8 项仍未达标，逐条核实处置如下：

| 项 | 缺口 | 本轮处置 |
|---|---|---|
| P0-2 | `ApprovalGrant.matches` 只比 fingerprint/version/plan_version，未绑 task_id 与尝试身份 | **已修**：增加 `task_id` + `attempt_seq` 绑定。`attempt_id` 要到 Act 阶段才分配（批准时不存在），用 `attempt_seq` 快照等价锁住「批准的到底是哪一次尝试」——批准后 `attempt_seq` 前进过即作废 |
| P1-1 | 无 cancellation_token、超时未细分、超时→EFFECT_UNKNOWN 衔接不显式 | **部分修**：超时细分已做（`_INPUT_TIMEOUT=5s` / `_LAUNCH_TIMEOUT=15s` / `read_timeout=6s`）。cancellation_token 需 `Popen`+`os.kill` 跨平台改造，侵入面大，未做；超时→EFFECT_UNKNOWN 衔接**已存在**（`_verify` 里动作发出但重新观察失败→EFFECT_UNKNOWN 的完整逻辑） |
| P1-3 / P1-4 | 仍走 relevance=max + 加权融合，未分阶段判定 | **未做**：这是算法改版（同域→共享目标→共享步骤→改目标的分阶段 pipeline），上一轮已做 `shared_terms` + 三证据融合，进一步分阶段需重写 classify 流程，单独一轮 |
| P1-6 | 已做「先落盘再唤醒」，但无 TaskLease/claim token/worker ownership | **未做**：TaskLease 是**跨进程**需求（防两个 worker 拥有同一任务），单进程内已闭环（每 lane 单 worker + `lane.running` 单一 + 事件驱动迁移）。与 V2.6 §8 同触发条件 |
| P1-7 | is_read_only 只停在 docstring，未真正参与加锁决策 | **已修**：`device_access` 加 `operation` 参数，加锁前用 `is_read_only` 真正校验——只读操作误包进加锁路径会抛 500 暴露接线错误；`/tap`/`/text`/`/back` 显式传操作名 |
| P1-9 | validate 只校验 task_version+页面，未校验 plan_version 与 action_attempt_id | **已修**：补 `plan_version` 门控（不匹配 STALE）。`action_attempt_id` 已通过 `needs_reconciliation` 参与「先对账、再继续」，无需重复门控 |
| P2-2 | 结构化字段优先，但执行阶段依赖 dispatch.error_class 是否传入 | **已修**：VLM ERROR 分支（动作发出但页面未达预期）的 dispatch 补 `error_class=action_rejected`，不再回退文本匹配 |

## V2.8 修复轮（依据 `v2.8审查建议.md`）

这份文档**引用的是 V2.7 之前的代码**——它声称「仍存在」的两个 P0（设备回退、`approved_dangerous: bool`）在上一轮已经修掉。逐条核对 HEAD 后，实际待修的是 2 条，其余要么已修、要么是有理由的延期：

| 条目 | 文档判断 | HEAD 实况与处置 |
|---|---|---|
| §二 P0 设备回退 | 仍存在 `pool.get() or default_session` | **已修**（V2.7 P0-3）：已绑定找不到就抛 `DeviceUnavailableError`，文档引用的是旧代码 |
| §三 P0 `approved_dangerous: bool` | 仍使用 bool | **已修**（V2.7 P0-2 + 补强）：已是 `ApprovalGrant`（含 task_id/attempt_seq），文档引用旧代码 |
| §四 P1 提交非原子、队列未清理 | persist 失败后 ready 残留 | **已修**：`_persist_or_degrade` 失败会 `_drop_from_queues` 清理；本轮再补 `_pop_next` ready 分支的终态/取消过滤，与 suspended 分支对齐 |
| §五 P1 execution_epoch | 旧上下文继续产生动作 | **延期**：已由三层兜住（run_version 围栏 + 安全点 status 检查 + `session.owned` 复核）。加 epoch 侵入面大、收益边际，与 TaskLease 同属多进程才需要 |
| §六 P1 抢占 handoff_target | `_preempt_for` 无交接承诺 | **已覆盖**：抢占者先入 ready 再请求抢占，A 让出后 `_pop_next` 的优先级比较保证抢占者优先。`_preempt_for` 只是「通知让出」标记，交接靠优先级排序 |
| §七 P1 EFFECT_UNKNOWN 幂等性 | 危险等级≠幂等性 | **已修**（V2.7 P1-2）：`SideEffectClass` + `is_safe_to_retry`，RETRY 前先过它，非幂等转人工 |
| §八 P1 恢复批准语义 | 「继续」被等价「上次可忽略」 | **已修**：新增 `Task.recovery_note`，崩溃恢复时记「上次动作效果未知」，人工批准后**不清空**，runtime 重新规划时转成 Re-plan 理由让模型先核验 |
| §九 P2 page_seen_changed | 页面变化被当过强证据 | **延期**：`page_seen_changed` 只是 GoalVerifier 三条独立证据之一，strict 模式还要求计划跑完 + 可核验声明。拆 environment/goal_progress 是 P2 增强 |
| §十 P2 UI 语义角色 | 关键词覆盖不足 | **延期**：UI 语义角色（submit/purchase/delete 等）需要 VLM 或 UI 树 role 标注，属模型增强 |

### 本轮真实改动（2 处 + 1 处语义加固）

1. **§四**：`_pop_next` 的 ready 分支补终态/取消过滤（`is_terminal or CANCELLED → continue`），与 suspended 分支对齐——防止「持久化降级后 ready 堆里还残留的 DEGRADED/CANCELLED 任务被取出执行」。
2. **§八**：`Task.recovery_note` 字段 + `_gate_crash_recovery` 写入 + `resolve_confirmation` 批准后保留 + `run()` 转成 Re-plan 理由并消费。把「人工批准继续 ≠ 上次副作用已忽略」从一句注释变成**可持久化、可追溯的事实**。

### 对审核最后五条不变量的对照

| 不变量 | 现状 |
|---|---|
| 一个任务同一时刻最多一个执行者 | ✅ 每设备一条 lane、单 worker、`lane.running` 单一（跨进程需 Lease） |
| 已绑定任务绝不在其他设备执行 | ✅ 本轮修掉：调度层早已硬拒绝改派，现在 runtime 也堵住了回退 |
| 未确认效果前不得盲目重做非幂等动作 | ✅ 本轮修掉（`Action.is_safe_to_retry`） |
| 人工批准只对指定动作尝试有效 | ✅ 本轮修掉（`ApprovalGrant` 指纹 + 双版本绑定） |
| 状态 / Checkpoint / 事件日志能解释同一条历史 | ✅ 事件流自足（`action_dispatched` 带 target/value/fingerprint、`action_verified` 带 layer/screenshot）；`source` 覆盖全部状态迁移 |

## V2.9 修复轮（依据 `v2.9审核建议.md`）

这份文档主要落在「多进程部署边界 + 语义风险/目标验证的架构演进」上。逐条核对 HEAD 后，
真正**本轮动手修**的是 1 条真实功能 bug + 3 处旧语义残留，其余要么已在早前几轮修掉、
要么属于「有理由的延期」（触发条件已写进代码与 MEMORY，见下）。

### 本轮真实改动

| 条目 | 问题 | 改动 |
|---|---|---|
| P1 §六 中文输入链路割裂 | 人工 `/text` 走 `build_default_input` 能输中文，但 Agent 的 `ActionType.TYPE` 直接 `adb.type_text()` 只认安全 ASCII——「给妈妈发消息」会被吞成空 | `agent/executor.py` 的 TYPE 分支改走 `build_default_input(adb).input(value)`，与 `/text` 端点同一条链路（ASCII→input text、非 ASCII→ADB Keyboard 广播） |
| P2 §十二 旧语义残留（1） | `api/server.py` 里 `device_access(timeout=...)` 定义了两遍，第一份引用的是已不存在的 `session`/单设备 `adb` | 删除第一份死代码，只保留真正的 `device_access(session_item, *, timeout, operation)` |
| P2 §十二 旧语义残留（2） | `ConfirmRequest.token` docstring 与 `/confirm` 的 403 错误仍写「从 GET /tasks/{id} 的 pending_confirmation.token 取」 | 改为「先 POST /tasks/{id}/confirmation-token 申请令牌」；第 174 行的授权注释同步更正（V2.7 P1-8 已不随 GET 下发） |

### 有理由的延期（本轮**刻意不修**，触发条件已就地标注）

| 条目 | 处置 | 触发条件 |
|---|---|---|
| P0 TaskLease / 跨进程 CAS | 单进程内 `threading.RLock` 已闭环；跨进程要文件锁/SQLite，与 V2.6 §8 同一触发条件，注释已在 `runtime.py`/`scheduler.py` 就地写清 | **多进程/多实例部署** 或 **存储换 SQLite** |
| P0/P1 风险关键词 → ActionSemanticLayer | `ActionRiskGate.assess` + `SideEffectClass` 已是方向正确的一步；统一的「动作语义层」是重构级 | 与 GoalOracle、多租户一起做 V3 |
| P1 goal_verifier「计划自证」 | 已有 L6-a/b/c 三条独立证据 + 仅「有反证」才 REJECTED；`pending_steps==0` 只是其一 | 进一步拆 GoalOracle 属 V3 |
| P1 stable/TOCTOU、EventLog fail-open、关系分类 | 架构级演进，本轮不动 | 同上 |

### 验证

`pytest -q` → **450 passed（10.0s）**，较上轮 448 新增 **2** 条（executor 中文输入走广播通道、
executor ASCII 输入仍走 input text），零回归。

---

## V3 M1–M4：从「功能实现」进入「正确性工程」

| 里程碑 | 内容 |
|---|---|
| **M1 GoalOracle** | 把「计划跑完」与「目标达成」拆成两个判断：计划跑完只是完成的门槛，真正的信号是**独立于模型计划的世界证据** |
| **M2 ActionSemanticLayer** | 风险与副作用幂等统一从一个 `SemanticRole` 派生，消灭 `DANGEROUS_KEYWORDS` / `IRREVERSIBLE_KEYWORDS` / `NON_IDEMPOTENT_KEYWORDS` 三表各判一个维度的矛盾 |
| **M3 TaskLease** | 跨进程「一个任务同一时刻最多一个执行者」：独立 `lease.db` 上的 SQLite 原子 `claim` / `heartbeat`，JSON 存储保持不变 |
| **M4 EventLog fail-safe** | 定义 `SAFETY_CRITICAL_KINDS`；危险动作的 dispatch 记录写不进 durable store 就不继续 |

## V3.1 修复轮（依据 `v3.1审核建议.md`）

这份文档有 **12 个审查项**。逐条拿它引用的代码片段去核对 HEAD 之后：**7 项成立并已修**、
**3 项早前几轮已经修掉**（审核基于更早的提交）、**2 项属有理由的延期**（触发条件已就地标注）。

### 一、本轮修掉的 7 项

| 审查项 | 现状核对（HEAD 上的事实） | 改动 | 落点 |
|---|---|---|---|
| **一（P0）** EventLog 的 fail-safe 没落地 | **成立**。`emit_critical` 与 `SAFETY_CRITICAL_KINDS` 都在，但只有 `ACTION_DISPATCHED` 一处走 `emit_critical`；`RISK_ASSESSED` / `CONFIRMED` / `GOAL_CONFIRMED` 仍走 fail-open 的 `emit`——常量表说它们「丢失即审计链断裂」，真实行为却是「写不进去也照跑」 | 把分级**搬进唯一的写入口**：`emit()` 按 `is_safety_critical(kind)` 自动分派，安全关键事件写失败抛 `PersistenceError`。三处调用点改为「先落盘、再生效」：风险判定写不下 → 不继续；人工批准写不下 → **不放行**（`confirm` 返回 False）；完成认定写不下 → **不落 DONE** | `storage/event_log.py`、`agent/runtime.py`（`_emit_critical_or`）、`agent/_execution.py`、`agent/_goal.py`、`agent/_confirm.py` |
| **三（P0）** `UNKNOWN` 语义仍是 SAFE + 可重做 | **成立**。`ROLE_SEMANTICS[UNKNOWN] = (SAFE, IDEMPOTENT_WRITE)`，配合「角色未知 → 会改页面的动作一律 `IDEMPOTENT_WRITE`」的类型兜底，`tap(540,1600)` 这种「按钮无文字、UI 也找不到节点」的动作被允许**自动重试**——而那个坐标可能是「确认支付」 | `UNKNOWN` → `(CAUTION, NON_IDEMPOTENT_WRITE)`；类型兜底改成「只读类型 → READ_ONLY，SWIPE → 幂等，TAP/LONG_PRESS/TYPE → **非幂等**」。另加一条边界：`DONE`/`WAIT`/`BACK`/`HOME` 的效果由**动作类型**就完全确定，保守下限不适用于它们，否则「申请完成」会被显示成需要确认的动作 | `models/semantic.py`、`models/action.py`、`agent/risk_gate.py` |
| **五（P1）** Target Resolution 失败仍 fail-open | **成立**。`except Exception: return ResolvedTarget(node=None)` 把「没给树 / 树坏了 / 树里没这个节点 / 动作本就没有目标元素」四种情况压成一个 `node=None`，门禁分不清，只能一律当「没有证据」放行 | 新增 `TargetResolution`（`ok` / `no_tree` / `parse_error` / `not_found` / `no_target`）让失败原因成为一等事实；风险门禁把「会改页面的动作 + 目标证据缺口」抬到 CAUTION 并写进 `reasons`；`RiskAssessment` 暴露 `target_resolution` / `unresolved_target`，并进入 `RISK_ASSESSED` 审计 | `vision/target.py`、`agent/risk_gate.py`、`agent/_execution.py` |
| **六（P1）** `generation` 是设备代次，不是 UI 版本 | **成立**。`DeviceSession.generation` 只记 Shadow **自己**的写入；用户手点、通知栏、App 异步刷新、另一个 adb client 都不推进它 → 「用 A 屏的坐标点 B 屏」的 TOCTOU 窗口一直开着（而且 Think 段可能包含一次几秒的模型调用） | 新增 `ObservationEpoch`（设备代次 + package/activity + UI 结构指纹 + 时刻），决策时快照、**执行前复查**。代次或页面身份对不上 → 放弃本次动作、记 `observation_stale`、重新观察；连续 3 次稳不下来按瞬时故障结算（必须有上界，否则判定抖动会变成死循环）。结构指纹只比结构不比文本，避免时钟/未读数把检查退化成「永远 stale」 | `models/state.py`、`agent/_execution.py`、`storage/event_log.py` |
| **八（P1）** 单进程 CAS 边界没有锁死 | **成立**。`TaskStore` 的 revision CAS 与 `_mutation_lock` 都只在同一进程内成立，但没有任何东西阻止 `--workers 4`，而代码里到处是 `thread-safe` / `atomic` / `CAS` | 启动期硬闸：检测到 `WEB_CONCURRENCY` / `UVICORN_WORKERS` / `GUNICORN_WORKERS` > 1 时**拒绝启动**（M3 的 TaskLease 只保证不双执行，保证不了 Task 文档不被互相覆盖）。显式 `SHADOW_ALLOW_MULTI_PROCESS=1` 可跳过 | `api/server.py` |
| **十（P1）** 关系判定仍是「相似度过强」 | **部分成立**。`shared_terms` / 语言标记 / LLM 置信度已在，但**没有实体冲突**这一层 | 新增 `conflicting_entities()`（按「同维度、不同取值」分组的实体表）。实体互斥时**优先于一切相关性证据**否决：`「给妈妈发微信」+「先给爸爸发微信」` 里「先」命中规则（0.75）、两句又共享「发微 / 微信」，两条豁免会同时放过它——但它们要发的是**两条不同的消息**。DUPLICATE 分支同样加这道闸（判重复的后果是静默不执行） | `agent/classifier.py` |
| **十二（P2）** `fingerprint` 不适合安全授权 | **成立**。`tap(500,800)` 在微信 / 淘宝 / 设置里是三个不同动作，而裸指纹把它们算成同一个 | 新增 `Action.page_bound_fingerprint(package, activity)`；`ApprovalGrant` 用它做放行比对（`matches(..., package, activity)`），批准的页面换掉 → 凭据作废。**裸 `fingerprint` 语义刻意不变**：`denied_fingerprints` 依赖它做「同一屏内的动作身份」，掺进页面维度会让被否决的按钮换个页面出现时又被重问一遍 | `models/action.py`、`agent/_runtime_types.py`、`agent/_confirm.py`、`agent/_execution.py` |

### 二、代码里已经做好的（审核基于更早的提交，本轮不动）

| 审查项 | HEAD 上的事实 |
|---|---|
| **二（P0）** Agent 的 `TYPE` 不走 `InputProvider` | **V2.9 那轮已修**。`agent/executor.py` 的 TYPE 分支已改为 `build_default_input(adb).input(value)`，与 `/text` 端点同一条链路（ASCII → `input text`、非 ASCII → ADB Keyboard 广播）。仍未做的是「允许注入 provider **实例**」（现在每次现构一个），接口层面无害，暂不改 |
| **七（P1）** Task Lease / Fencing 缺失 | **V3 M3 已实现**。`storage/lease_store.py` 用 SQLite 的 `INSERT ... ON CONFLICT ... DO UPDATE ... WHERE expires_at <= ?` 做单条事务内的原子 claim，`heartbeat` 用 `WHERE task_id=? AND token=?`（token 不匹配即必须停止执行）。`Scheduler` 的 lease 为 None 时保持单进程旧行为 |
| **十一（P2）** `Action.side_effect()` 与语义层两套真相 | **V3 M2 已消除**。`policy_risk()` 与 `side_effect()` 都走 `infer_role` → `ROLE_SEMANTICS` 查表；`IRREVERSIBLE_KEYWORDS` / `NON_IDEMPOTENT_KEYWORDS` 已零引用，并就地标注为「遗留材料，新增词请改 `models/semantic`」 |

### 三、有理由的延期（触发条件已就地写进代码）

| 审查项 | 为什么本轮不做 | 触发条件（写在哪） |
|---|---|---|
| **四（P1）** 语义层本质仍是「关键词 → role」 | 把 UNKNOWN 改成保守之后，漏判的后果是「多问一次人 / 不自动重试」而不是「静默放行一个删除」——这是刻意取舍。真正的分类器会引入一个新的判定来源，必须同样受「只能抬不能降」约束、还要可解释可离线测，属独立一轮 | `UNKNOWN` 在真实轨迹里成为高频角色、人工确认被它刷屏时（`models/semantic.py` 模块 docstring） |
| **九（P1 §九）** `page_seen_changed` 仍是「页面变过」而非「目标状态成立」 | 「页面变成了详情页」这种断言需要页面类型识别 + 把自然语言目标编译成谓词的可信链路，两样都不属于修复轮 | `MAX_GOAL_REJECTIONS` 被真实轨迹频繁打满时（`agent/goal_oracle.py` 模块 docstring） |

### 行为变化提醒

- **`EventLog.emit()` 现在会抛异常**——但只对 `SAFETY_CRITICAL_KINDS` 里的 kind。普通事件仍是旁路（写失败只 warning）。自定义调用方如果传的是这四个 kind，需要自己 catch 并决定「记不下还能不能继续」。
- **认不出语义的点击/长按/输入不再自动重试**。`EFFECT_UNKNOWN` 之后从「自动再来一次」变成「转人工」。人工确认的次数会上升——这是拿「多问一次」换「不重复扣款」，是刻意的。
- **`DONE` / `WAIT` / `BACK` / `HOME` 不受影响**，仍判 SAFE：它们的效果由动作类型就完全确定，「不知道这是什么动作」对它们不成立。
- **每步多一次 `dumpsys window`**（TOCTOU 复查）。验证过的设备很慢、或压测吞吐时可用 `SHADOW_TOCTOU_GUARD=0` 关掉，代价是外部改动拦不住。
- **多 worker 现在会拒绝启动**。用 `uvicorn ... --workers 1`；确实要跑多进程且自行承担状态一致性风险时设 `SHADOW_ALLOW_MULTI_PROCESS=1`。
- **人工批准之后页面被换掉，凭据会作废**，需要重新确认（这是 P2-9 的目的）。转人工时若手上没有观察（`_ask_human` / `_settle_failure` 这两条路径），凭据**不绑页面**——否则会造出一张永远匹配不上的凭据，任务卡在「请求确认 → 凭据失效 → 再请求确认」的空转里。
- 内部接口变更：`ActionRiskGate.policy_risk()` 的返回值从 `(risk, reasons)` 变成 `(risk, reasons, resolution)`；`ApprovalGrant` 新增必填字段 `page_bound_fingerprint`。

### 新环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `SHADOW_TOCTOU_GUARD` | 置 0 关闭执行前的页面身份复查（设备代次检查始终生效） | `1`（开启） |
| `SHADOW_ALLOW_MULTI_PROCESS` | 置 1 时跳过「多 worker 拒绝启动」的硬闸（自行承担状态一致性风险） | 未设置 |
| `WEB_CONCURRENCY` / `UVICORN_WORKERS` / `GUNICORN_WORKERS` | 现在会被**读取并检查**：> 1 时拒绝启动 | 未设置 |

### 事件流变更

- 新增事件类型 `observation_stale`：记下一次**被避免的 TOCTOU**（附 `reason` 与连续次数）。刻意**不**放进 `SAFETY_CRITICAL_KINDS`——它丢失只会少一条解释，而它对应的行为（拒绝执行）本身就是最安全的那一侧。
- `risk_assessed` 新增 `target_resolution` / `unresolved_target` 两个字段，审计可以直接回答「这次点击是不是在盲点」。

### API 变更

**没有新增端点。** 行为变化只有一处：`POST /tasks/{id}/confirm` 在「安全事件写盘失败」时不再返回成功——批准 / 否决 / 完成裁定 / 恢复裁定四类确认都是「先留痕、再生效」，记不下来就不放行，返回失败让用户重试。

### 验证

`python -m pytest -q` → **503 passed（11.9s）**，较上轮 482 新增 **21** 条，零回归。
其中 **3 条既有用例随语义变更同步更新**（不是回归，是预期）：

| 用例 | 为什么要改 |
|---|---|
| `test_models.py::test_approval_grant_binds_task_and_attempt` | `ApprovalGrant` 新增页面绑定字段，并补一条「换了页面 → 凭据作废」的断言 |
| `test_runtime.py::test_reconcile_retries_action_when_it_never_took_effect` | 原来用裸坐标 `tap(100,200)`，现在它是**非幂等**的（P0-3），不会自动重做。改用带可识别语义的 `replayable_tap` 保持「重做等价」这一档的覆盖，裸坐标那一档由新用例 `test_unknown_target_tap_effect_unknown_goes_to_human` 覆盖 |
| `test_runtime.py::test_effect_unknown_reconcile_redoes_the_action_once` | 同上 |

新增的 21 条按审查项分布：P0-1（2）、P0-3（6）、P1-4（3）、P1-6（3）、P1-7（3）、P1-8（2）、P2-9（2）。

---

## V3.2 修复轮（依据 `v3.2审查建议.md`）

这份文档有 **8 个审查项**（P0×2、P1×4、P2×2）。逐条核对 HEAD 后：**7 项成立并已修**、
**1 项已被 V3.1 覆盖**。其中一项（Scheduler 多阶段提交）审核自己也说明「重点不是代码错了」
——它是**表述**问题，所以本轮的处理是「把话说准」，见下面「如实说明」。

### 一、本轮改动（按审核给的优先级排序）

| 审查项 | 现状核对（HEAD 上的事实） | 改动 | 落点 |
|---|---|---|---|
| **1（P0）** `/tap`、`/text`、`/back` 直接绕过 RiskGate | **成立**。`/actions` 做了两轮风险判定，但这三个端点的链路是「鉴权 → 设备权限 → Device Lock → `device.tap()`」——**没有 `ActionRiskGate`**。`POST /tap {"x":680,"y":1200}` 能点掉「立即付款」，既不判风险、也不进 HITL、设备侧还留不下审计 | 四个手工端点收口到**同一个执行内核** `run_manual_action()`：两轮门禁（第二轮带 UI 树）→ 危险动作 403 → 执行 → 验证 → **设备侧事件留痕**。`/tap`、`/text`、`/back` 现在会先观察一次，所以「点击红色按钮」其实是「立即购买」也拦得住 | `api/server.py` |
| **2（P0）** Confirmation Token 的「一次性」只存在内存 | **成立**。`_consumed_confirmations` 是进程内 dict，而签名密钥在配置 `SHADOW_API_TOKEN` 时是**稳定**的 → 重启后一张仍在 TTL 内、签名有效的旧票据**重新可用**。实际语义是「进程生命周期内一次性」 | 消费记录搬到 SQLite：`jti` 主键 + 原子 INSERT，就是审核要求的 `consume = CAS / unique constraint`，跨进程跨重启都成立。内存实现保留为默认（单测/嵌入式），但改为显式可替换，且 `/health` 暴露后端类名——名字不是 SQLite 实现时，「一次性」就不跨重启 | `storage/confirmation_store.py`（新增）、`api/auth.py`、`api/server.py` |
| **3（P1）** Checkpoint 与 Task 指针不是原子提交 | **成立**，而且**注释比实现说得多**（原文写着「必须一次提交」，实际是三次写入）。不过它没有审核担心的那个方向的问题，理由见「如实说明」 | ① `JsonStore` 写入补 `fsync`（先刷文件、再 `os.replace`、再刷目录）→ 补上**掉电**场景；② 注释改成如实描述；③ 启动时 `prune_orphans()` 清掉没人认领的恢复点 | `storage/json_store.py`、`storage/checkpoint_store.py`、`agent/runtime.py`、`api/server.py` |
| **4（P1）** Scheduler 的「真实执行」与「持久化状态」有多阶段窗口 | **成立**（`lease claim → 设备 acquire → RUNNING → persist → run()`），但审核自己也说「重点不是代码错了」，且点名「别在项目说明里说成 exactly once」 | 这是**表述**问题 → 本轮只改说法：README 明确写清它是 best-effort recovery + 对账，**不是 exactly-once side-effect execution**（见「如实说明」） | `README.md` |
| **5（P1）** `/actions` 与 Runtime 是两套执行事实 | **成立**。`/actions` 只把 `verification` / `risk` 回给调用方，不进 TaskStep / StepAttempt / Trajectory / EventLog | 手工动作现在写**设备侧事件**（`channel="manual"`，`task_id="__manual__"`）→ `/history`、`/replay` 至少能看到「谁在什么时候点了哪、判成什么风险、验证结果如何」。执行内核统一了，任务编排没有——边界见下 | `api/server.py` |
| **6（P1）** 设备权限是环境变量级，不是 Principal 级 | **成立**。main token 与 readonly token 都读同一个 `SHADOW_API_DEVICE_ALLOW`，只能表达「所有 operator 都能操作 A 和 B」 | 新增 `SHADOW_API_PRINCIPALS`（JSON）：每个 principal 有自己的令牌、只读标记与**自己的设备范围**。配置写错时 **fail-closed**——鉴权仍开启但一个人都认不出（全部 401），原因进 `/health` 与服务日志，`python -m api.server` 直接拒绝启动 | `api/auth.py`、`api/server.py` |
| **7（P2）** readonly token 与 POST 只读接口语义冲突 | **成立**。readonly 被限定为「只能 GET」，而 `/screenshot`、`/observe` 是纯读取却注册成 POST → **只读令牌访问只读接口反而 403** | 两件事一起做：① 这两个端点同时注册 **GET**（正确的读方法），POST 保留不动；② 判定从「HTTP 方法」升级成「**操作能力**」，白名单写死（将来新增端点不会因为「恰好是 POST」被自动放行） | `api/server.py` |
| **8（P2）** `generation` 不是完整的设备状态版本 | **V3.1 已覆盖**：运行时判断「页面还是不是决策时那一屏」用的是 `ObservationEpoch`（代次 + package/activity + 结构指纹 + 时刻），不再指望 generation | 补一条**语义记录**：三个端点的响应加 `generation_meaning`，README 与代码注释都写明它只回答「Shadow 自己动过设备没有」，外部操作（用户手点 / 通知栏 / App 异步刷新 / 另一个 adb client）都不会推进它 | `api/server.py` |

### 二、如实说明（审核指出「说法比实现大」的地方）

审核这一轮最有价值的部分不是列 bug，而是点名了几处**声明强于实现**。逐条说清现在的边界：

1. **不是 exactly-once。** 任务执行是「多个本地步骤 + 崩溃后推断」的组合，因此在
   `ActionEffectStatus` / `attempt_id` / reconciliation 之外，不存在「每一个副作用恰好发生一次」
   的保证。准确的说法是：**best-effort recovery + 对账**——崩溃后先确认「上一次动作发出去了没有」，
   拿不到证据就转人工，而不是假装它没发生过。这是设计取舍（手机侧没有事务），不是待修的缺陷。
2. **Checkpoint 与 Task 指针不是一次事务。** 它们是两次文件写入，没有跨文件事务。
   真实成立的只有两条：① **顺序**（先写恢复点、后写任务）→ 任务**永远不会**指向一个
   不存在或没写完的恢复点；② **单文件原子可见 + fsync**（tmp → fsync → replace → fsync 目录）
   → 新名字出现时内容一定已落盘，且掉电后不会看到一个空文件。代价是反方向仍会出现
   **孤儿恢复点**（文件在、指针没提交），它是无害的，由启动时的 `prune_orphans()` 清掉。
   要真正的跨文件事务就得上 SQLite（`BEGIN; INSERT checkpoint; UPDATE task; COMMIT;`）。
3. **执行内核统一了，任务编排没有。** `/tap`、`/text`、`/back`、`/actions` 现在共用
   同一条「门禁 → 执行 → 验证 → 留痕」链路；但手工动作**不属于任何 Task**，
   所以不进 TaskStep / StepAttempt / Checkpoint / GoalVerification。硬塞进去会把
   「任务事实」和「手工操作」混成一锅。结构性方案（独立 `ExecutionService`，Runtime 与 API
   都依赖它）见下面的延期说明。

### 三、有理由的延期（触发条件已就地写进代码）

| 审查项 | 为什么本轮不做 | 触发条件（写在哪） |
|---|---|---|
| **5 的结构性部分**：抽独立 `ExecutionService`，Runtime 与 API 共用 | 把 Runtime 的 Observe→Think→Act→Verify 主循环抽成可复用的服务，会动到 V2.7 拆分出来的四个 mixin 与全部 runtime 用例；而手工端点的真实风险（绕过门禁、没有留痕）本轮已经堵住。先解决安全问题，再做结构重构 | 手工端点需要**参与任务编排**时（例如想让 `/tap` 也写 StepAttempt、或让手工动作能推进某个任务的计划）；或 Runtime 需要被第二个执行入口复用（如 API 之外的新前端）时（`api/server.py` 的 `run_manual_action` docstring） |

### 行为变化提醒

- **`/tap`、`/text`、`/back` 现在会先观察一次并过风险门禁**。代价是每次多一轮采集；收益是
  「点掉立即付款」会被 403 拦下（`detail` 里会写明判定依据，并指路 `POST /tasks`）。
  这三个端点的**响应只增字段不改字段名**，老调用方不受影响。
- **手工端点新增失败码 503**：设备操作记录写不进 durable store 时**拒绝执行**。
  这与 V3.1 的 P0-1 一脉相承——「手机真的点下去了、审计里没有」不可接受。
- **只读令牌现在能访问 `/screenshot`、`/observe`**（GET 与 POST 都行），会改设备的操作照旧 403。
- **多实例部署**：要把「同一张确认令牌不许用第二次」跨实例成立，必须让所有实例指向
  **同一个 `SHADOW_CONFIRM_DB`**；各写各的文件只保证各自进程的重启安全。
- **配错 `SHADOW_API_PRINCIPALS` 会导致全部请求 401**（fail-closed）。这是刻意的：
  退回「没配鉴权」会把「配错了」变成「谁都能进」。原因在 `/health` 的 `principals_error` 里。
- 启动时会清理**未被任何任务提交过**的恢复点。清理用 `list_all()`（含终态任务），
  所以 `GET /tasks/{id}/checkpoint` 对已结束的任务照样能读出它提交过的那个。
- 内部接口：`CheckpointStore.prune_orphans(committed)`、`auth.configure_consumption(guard)`、
  `auth.config_error()`、`auth.consumption_backend()` 为新增。

### 新环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `SHADOW_API_PRINCIPALS` | 推荐的身份配置（JSON）：每个 principal 有 `token` / `read_only` / `devices`（数组，`["*"]` 表示不限）。设置后 legacy 令牌变量被忽略（会告警） | 未设置 |
| `SHADOW_CONFIRM_DB` | 确认令牌「已消费」记录的 SQLite 路径。多实例部署必须指向同一个文件 | `$STORAGE_DIR/confirmations.db` |

### API 变更

| 变更 | 说明 |
|---|---|
| `GET /screenshot`、`GET /observe` | **新增**（原 POST 保留）。纯读取接口现在有正确的读方法 |
| `GET /health` 新增 `confirmation_consumption`、`principals_error` | 前者是消费记录后端类名（不是 `InMemoryConsumption` 才说明「一次性」跨重启成立）；后者非 null 表示 principals 配错、正在拒绝所有请求 |
| `POST /tap`、`/text`、`/back` 新增 403 / 503 | 403 = 危险动作（需走 `/tasks` + `/confirm`）；503 = 设备操作记录无法落盘，已拒绝执行 |
| 三个手工端点响应新增 `risk` / `risk_detail` / `verification` / `generation_meaning` | 只增不改，老调用方不受影响 |

### 验证

`python -m pytest -q` → **526 passed（11.7s）**，较上轮 503 新增 **23** 条，零回归。

| 新增用例 | 覆盖 |
|---|---|
| `test_confirmation_store.py`（新，7 条） | **换实例（≈重启）后第二次消费必须被拒**、两个连接只有一个能消费（unique constraint = CAS）、审计证据字段、过期清理、auth 层接上 SQLite 后跨重启成立、默认后端名字可查 |
| `test_checkpoint_store.py`（新，4 条） | 孤儿与「被取代的旧恢复点」会被清、已提交的必须留着、空存储是 no-op、无指针任务不构成保留 |
| `test_api.py`（+6 条） | `/text` 危险输入 403、`/tap` 由 UI 树解析出「立即付款」后 403、手工动作写设备侧事件（含 `channel`/`device`/`fingerprint`）、审计写不下去时 503 且**没有真的点下去**、启动清理删孤儿、**终态任务的已提交恢复点不被误删** |
| `test_api_auth.py`（+6 条） | 只读令牌可用 GET/POST 的 `/screenshot`、`/observe`、只读令牌仍不能写、Principal 各自的设备范围、**principals 配错时 fail-closed**、legacy 变量仍有效、`/health` 报销费后端 |

既有用例只改了 **1 处替身**（不是回归）：`tests/fakes.py` 的 `FakeDevice` 补 `dump_ui()`。
`observer.observe` 依赖它，而本轮让 `/tap` 也必须观察——替身不补的话，新门禁在测试里只会
抛 `AttributeError`，**等于没被覆盖**。

---

## V3.3 手机部署（依据 `手机部署方案.md`）

方案文档要解决的事：**让手机自己成为 Agent 主机，不再由 PC 通过 ADB 控制它**。
落地后本仓库的形态是方案文档 §10 想要的那两个产品形态：

```text
Shadow Core（本仓库的 Python 运行时）       TaskManager / Scheduler / Runtime / RiskGate / Checkpoint
├── device/adb.py        ADB 后端            PC 控制手机（开发模式，默认）
└── device/android.py    Android 后端        手机本机的 Accessibility + MediaProjection
                         ↑ device/controller.py 的 DeviceController 协议是两者共用的接缝
```

### 一、方案 §1–§10 逐条落点

| 方案要求 | 落点 | 说明 |
|---|---|---|
| §1 把 ADB 从核心控制链里拆出去（`AdbDeviceController` / `AndroidDeviceController`） | `device/controller.py`（端口）、`device/adb.py`、`device/android.py`、`device/factory.py` | `executor` 只依赖 `DeviceController`，只 `except DeviceError`；`AdbError` 改为继承它，老捕获点全部保留 |
| §2 不要「Python 全套原封不动塞进手机」 | `device/android.py` 的 `AndroidBridge` + `android/` | Python Core 只保留原样；**FastAPI / uvicorn 不需要上手机**（手机上没人来调 HTTP）；设备层是原生 Kotlin |
| §3 用 AccessibilityService 拿 `AccessibilityNodeInfo` | `android/.../ShadowAccessibilityService.kt` + `NodeInfoAdapter.kt` + `UiTreeSerializer.kt` | 树被序列化成**与 `uiautomator dump` 同构的 XML**，于是 `vision/*` 一行不用改 |
| §4 截图不再走 ADB，改用 MediaProjection | `android/.../ScreenCapture.kt` | 授权一次、常驻一个 VirtualDisplay（Android 14+ 一个令牌只能建一次 VD） |
| §5 输入改用 `ACTION_SET_TEXT`；`InputProvider` 分 ADB / Android 两支 | `device/input.py` 的 `AndroidInputProvider` + 控制器自己声明通道 | 手机上不需要 ADB Keyboard、不需要切输入法、不需要 ASCII/非 ASCII 分流 |
| §6 App 启动改用 `PackageManager` + Intent | `android/.../AppLauncher.kt` | 相对写法 `/.ui.LauncherUI` 的补全与 `am start -n` 对齐 |
| §7 Task/Scheduler/Checkpoint/RiskGate 保留 | 无改动 | 这正是本轮最想验证的一句话，见下面「验证」 |
| §8 LLM 不放手机本地，走 HTTP 网关 | `vision/vlm.py` 现有实现 | `VLM_BASE_URL` / `VLM_API_KEY` / `VLM_MODEL`（OpenAI 兼容），云端 Qwen / 局域网 vLLM / 本地都只是改这三个变量 |
| §9 第一版不追求完全离线 | `android/README.md` | 手机负责 Observe/Action/Verification，模型负责 Plan/Reason/Grounding |
| §10 拆成 core / desktop / android | 目录 + `device/factory.py` | `SHADOW_DEVICE_BACKEND=adb\|android` 选后端；上层拿到同一个协议对象 |

### 二、桥有两条承载路线（**这是本轮唯一需要你决策的地方**）

`AndroidBridge` 是一条协议，协议可以有不同的「谁来承载」：

| | 路线 A：同进程（Chaquopy） | **路线 B：设备端点（`android/` 默认实现）** |
|---|---|---|
| 拓扑 | APK 里同时有 Kotlin 设备层与 Python Core | 手机只当设备端点，Core 跑在 PC / 局域网 |
| 设备层被怎么调用 | Chaquopy 把 Kotlin 对象注册给 Python | HTTP `/bridge/<方法名>`，Core 侧是 `device/remote.py` |
| 现状 | **被一个第三方依赖挡住**，见下 | 可用 |
| Python 侧要改 | 无 | 无（`SHADOW_ANDROID_BRIDGE_URL` 指过来） |

**路线 A 的阻塞点（已查证，不是猜测）**：`models/` 里每一个模型都是 pydantic 2 的
`BaseModel`，而 pydantic 2 的核心 `pydantic-core` 是 Rust 扩展，PyPI 上没有 Android 轮子，
Chaquopy 官方仓库也没有收录它（维护者原话：*Pydantic version 2 isn't currently available
for Chaquopy*，chaquo/chaquopy#1160；另一份专门为 Chaquopy 构建它的尝试卡在 PyO3 的 abi3
特性上，pydantic/pydantic-core#1607）。这不是「换个包」，而是「核心的模型层要不要重写」。

所以：**路线 B 是本轮交付的可用路径**，路线 A 需要的两处 Gradle/Kotlin 配置写在
`android/README.md` 里，等你验证完那条错误信息再打开。方案文档 §2 自己也写着
「不建议把整个 Shadow 原封不动塞进手机」，§10 的第三形态（手机 A/B/C → Shadow Cloud）
就是路线 B——它反而是文档里更长远的那条路。

### 三、本轮改动

**新增**

| 文件 | 作用 |
|---|---|
| `device/controller.py` | 设备**端口**：`DeviceController` 协议 + `DeviceError` 家族 + `READ_ONLY_OPERATIONS` / `is_read_only` |
| `device/android.py` | Android 后端：`AndroidDeviceController` + `AndroidBridge` 协议（12 个方法）+ 注册/取桥 |
| `device/remote.py` | 桥的**远程传输**：HTTP 客户端，实现同一份协议（路线 B） |
| `device/factory.py` | 后端装配：`SHADOW_DEVICE_BACKEND` → 具体控制器；桥按「先同进程、后远程」取 |
| `android/` | Kotlin 设备层 + 设备端点（见 `android/README.md`） |
| `tests/test_android_adapter.py` | 假桥驱动**整条任务链**（含危险动作仍被 RiskGate 拦下） |
| `tests/test_android_remote_bridge.py` | 远程传输：12 个方法的参数名/路径/回包 + 失败语义 |
| `tests/test_android_bridge_contract.py` | 跨语言契约：方法名、参数个数、属性集合、golden UI 树喂给真实解析器、`R.*` 引用与清单 `@string` 必须在 `res/` 里存在 |
| `tests/test_device_port.py` | 端口解耦守卫：核心侧五个模块不许出现 `AdbController`、设备参数必须标 `DeviceController`、用到的能力必须在协议里声明过 |

**改动**

| 文件 | 改动 |
|---|---|
| `device/adb.py` | `AdbError` 改继承 `DeviceError`；`AdbBudgetExhausted` 同时是 `DeviceBudgetExhausted`；`is_read_only` / `DURATION_RANGE_MS` 迁到端口层（旧导入路径 re-export）；补 `launch_app` |
| `device/input.py` | 新增 `AndroidInputProvider`（`ACTION_SET_TEXT`）；`build_default_input` 改为**问控制器要**通道，不再写死 ADB 双通道 |
| `agent/executor.py` | 只依赖 `DeviceController`；参数错误从 `AdbError` 换成核心的 `ActionArgumentError`（不再为了校验一个参数去 import 设备后端）；`LAUNCH` 无 activity 时走 `launch_app` |
| `api/server.py` | 控制器改为 `build_controller(...)`；异常处理器注册在 `DeviceError` 基类上（Android 后端出错不再掉进 500 兜底）；`/devices` 的 `primary` 不再写死 `adb.serial`；`/health` 加 `device_backend` |
| `models/action.py` | `DURATION_RANGE_MS` 从 `device/adb.py` 挪过来（动作参数的合法区间不是后端细节） |
| `models/exceptions.py` | 新增 `ActionArgumentError` |
| `device/__init__.py` | 导出端口层的符号 |
| `agent/observer.py` / `device/screenshot.py` / `device/accessibility.py` / `vision/grounding.py` | 设备参数标注由 `AdbController` 改成端口的 `DeviceController`（运行早已解耦，标注滞后了一层）；`observer` 与 `grounding` 的注释按两个后端的真实预算/查询语义改写，`device/pool.py` 里那句「真机是 AdbController」也一起纠正 |

### 四、验证

`python -m pytest -q` → **611 passed**，较上轮 526 新增 **85** 条，零回归。

| 新增用例 | 覆盖 |
|---|---|
| `test_android_adapter.py`（28 条） | 端口完整性在装配期被检查、桥异常归一成 `DeviceError`、UI 树非 uiautomator 格式被拒、读不到树**抛异常**而不是返回空串、预算耗尽后不再开始新采集、输入走 `ACTION_SET_TEXT`、后端选错 fail-closed、**用假桥把整条任务链跑通**、**危险动作在 Android 后端上照样被 RiskGate 拦下** |
| `test_android_remote_bridge.py`（19 条） | 12 个方法的路径与 JSON 键逐条对齐（`duration_ms` 写成 `duration` 这类错误会被抓住）、令牌随请求发出、截图走裸字节、**权限问题映射成 `AndroidServiceUnavailable`**、连不上/令牌错/回包不是 JSON 的提示各不相同、同进程优先于远程、**两条路线都不通时绝不回退 adb** |
| `test_android_bridge_contract.py`（18 条） | Kotlin 侧方法名/参数个数与协议一致、HTTP 路由表覆盖全 12 个、序列化器属性集合 ⊇ `vision/parser.py` 读取的每一个、golden XML 喂给真实 `vision.target` / `agent.evidence` / `agent.risk_gate` 都能用、清单权限与辅助功能标志齐备、**Kotlin 引用的 `R.string` / `R.id` / `R.layout` 与清单里的 `@string` 都必须在 `res/` 里真实存在**（AAPT `resource not found` 的替身） |
| `test_device_port.py`（20 条） | 核心侧五个模块不出现 `AdbController`（否则手机形态要 fork 代码）、七个设备参数都用 `DeviceController` 标注、AST 扫出用到的设备能力必须都在协议里声明、`_REQUIRED_METHODS` 与协议声明一致（否则装配期检查会静默漏检）、ADB 专属通道**反向**仍保留 `AdbController` 标注 |

`android/` 侧的 Kotlin 也**真编译过**：本轮在**没有 Android Studio** 的环境里编通了全部源码
（PyCharm 自带的 JDK + Maven Central 的 kotlinc + 一个 `android.jar`，共 27 个 class），
并用同一个产物跑了 `UiTreeSerializerTest` 的 10 条 JVM 单测——其中「序列化输出与 golden
文件逐字节相同」把跨语言契约钉死。过程固化成 `android/tools/verify_kotlin_compile.py`，
命令与工具链下载地址写在 `android/README.md`。

那次编译当场抓到两处**只会在真机上暴露**的缺陷，均已修：`globalAction()` 里的
`require()` 被 Kotlin 解析成标准库的 `kotlin.require(Boolean)`（本类没有同名成员，
报错信息完全指不到问题）；`findFocus(FOCUS_INPUT)` 少了类名限定（`FOCUS_INPUT` 属于
`AccessibilityNodeInfo`，不是 `AccessibilityService` 的常量）。

### 五、如实说明

1. **能打包了，但还没上真机。** 本环境没有 Android SDK / Gradle / 真机，所以走的是
   「`android.jar` + kotlinc + build-tools」的路子，分两步：
   `python android/tools/verify_kotlin_compile.py`（全部源码编过 —— 27 个 class +
   JVM 单测 10 条）、`python android/tools/build_apk.py`（aapt2 → javac → kotlinc → d8 →
   zipalign → apksigner，产出 `android/app/build/outputs/apk/debug/app-debug.apk`，
   并静态校验签名 / 包名 / 权限 / 「清单声明的组件都在 dex 里」）。
   **仍未验证**的是真机行为：手势坐标是否被 ROM 缩放、投屏帧率与延迟、
   厂商后台存活策略会不会杀掉前台服务——这三条只有设备在手才能看。
   这一点写进 `android/README.md` 的「已知限制」第 6 条，不留在对话里。
2. **路线 A 目前不可用**，原因是 pydantic 2 没有 Android 轮子（上面 §二 有出处）。
   刻意**没有**用「兜底的假 pydantic」绕过它——那会让核心的模型校验语义悄悄变样，
   而这种偏离在被审核发现时比「还没做」严重得多。
3. **`DeviceError` 刻意不声明 `error_class`。** `models/retry.classify_exception`
   一旦读到 `error_class` 就**不再回退**到按类型/文本判断，而设备错误该归 transient
   还是 fatal 取决于具体原因（掉线是 transient、「未授权」近似 fatal）。
   给一个笼统的值会把 `models/_FATAL` 里已有的文本规则全部屏蔽掉。见
   `device/controller.py` 的类注释。
4. **屏幕旋转会让坐标偏移。** VirtualDisplay 按授权那一刻的尺寸建，
   任务中途旋转后截图尺寸与 `screen_size` 不一致。做法与触发条件写在
   `ScreenCapture` 的类注释与 `android/README.md` 里（需要注册 `DisplayListener` 重建）。
5. **`/bridge` 端点等于「操作这台手机」的能力。** 三道闸：令牌、只在用户主动启动时监听、
   `START_NOT_STICKY`。绝不要映射到公网。与 API 侧的安全口径一致。

### 六、新增环境变量

| 变量 | 作用 |
|---|---|
| `SHADOW_DEVICE_BACKEND` | `adb`（默认）/ `android`。写了不认识的值直接报错，不静默回退 |
| `SHADOW_ANDROID_BRIDGE_URL` | 路线 B：手机设备端点地址，如 `http://192.168.1.20:8765` |
| `SHADOW_ANDROID_BRIDGE_TOKEN` | 路线 B：与手机页面上显示的令牌一致 |

---

## V3.3 修复轮（依据 `v3.3审查建议.md`）

审核这轮给了 11 条（P0×1、P0/P1×1、P1×6、P2×2，外加一条架构评估）。照例**先核对再动手**，
核对结论是：**7 条成立且本轮已修、4 条属架构级延期**（触发条件已就地写进代码）。

### 一、逐条核对与处置

| 审核条目 | 现状核对（改前的真实代码） | 本轮处置 |
|---|---|---|
| §一 P0 安全关键事件不 durable | **成立**。`emit_critical` 只 `handle.write(...)` 就返回，没有 `flush`/`fsync`——它保证的是「write 返回」，不是「durable on disk」 | ✅ 见下「二」 |
| §二 P0/P1 多进程 EventLog 不一致 | **成立**。只有 `threading.Lock`（进程内） | ✅ 部分修 + 写清边界，见「二」 |
| §三 P1 Checkpoint 只是顺序一致 | **已在 V3.2 承认并写全**（`agent/runtime.py:277-291`：成立的只有①写入顺序②单文件原子可见，代价是孤儿恢复点） | ⏸ 无代码改动，出口路径已指向 V4 |
| §四 P1 `/wait` 看不到别的进程的推进 | **成立**。`_wait_for` 用 `manager.get()`，而它**优先返回本进程内存对象** | ✅ 见下「三」 |
| §五 P1 CAS 只覆盖一半（Runtime 不拿 `_mutation_lock`） | **成立且代码里已承认**（`_mutation_lock` 注释写明「Runtime 与 Scheduler 不经这把锁」，兜底是 revision CAS） | ⏸ 保持现状 + 写下出口路径（single-writer state machine）与触发条件 |
| §六 P1 票据已消费但业务确认失败 | **成立**。`/confirm` 先 `consume` 再 `resolve_confirmation` | ✅ 见下「四」 |
| §七 P1 敏感页上目标解析失败只到 CAUTION | **成立**。`requires_confirmation` 只在 `DANGEROUS` 时为真 | ✅ 见下「五」 |
| §八 P1 语义词表覆盖度 / 建议 Policy Engine | **成立**，但其延期理由 V3.1 §四 已写（`models/semantic.py`） | ⏸ 延期，本轮补记「真正缺的两项输入」 |
| §九 P2 `/health` 匿名泄露部署信息 | **成立**（回 `auth`/`host`/`device_backend`/`principals_error`） | ✅ 见下「六」 |
| §十 P2 `HOST=127.0.0.1` 与手机部署 | **成立**（这是部署形态问题，不是配置错误） | ✅ 文档澄清，见「七」 |
| §十一 该换数据库了（建议 V4 Storage Refactor） | **接受**这个判断 | ⏸ 写成本仓库的下一步计划，见「八」 |

### 二、P0 那个洞：`write` 返回 ≠ durable

改法是审核给的那条路，但有两个细节值得写下来：

```python
os.open(path, O_APPEND | O_CREAT | O_WRONLY)  →  单次 os.write  →  （安全关键事件才）os.fsync
```

- **单次 `os.write` + `O_APPEND`**：内核把「定位到文件尾」和「写入」做成一次原子操作，
  所以多进程同时追加时行与行不会交错。它给的是**不撕裂**，**不是串行化**——
  跨进程的先后顺序仍然不承诺。这一点现在写在 `storage/event_log.py` 的模块 docstring 里
  （一张「保证 / 不保证」表），因为审核 §二 的真正要求就是**把一致性模型定义清楚**，
  而不是假装文件日志能当数据库用。
- **`fsync` 失败必须抛**（→ `PersistenceError` → 任务 DEGRADED）。这里与 `JsonStore`
  的取舍**刻意相反**：那边 fsync 失败就降级为「与升级前一致」，因为状态写入迟早会再发生
  一次；而安全关键事件守着的是**已经发生的副作用**——「不 durable 就不放行」才是它的全部意义。
- 只有新建文件时才额外刷一次目录项（掉电可能「内容在、名字没在」），目录 fsync 允许失败
  （Windows 根本不允许打开目录）。
- 普通事件仍然 `fail-open` 且**不 fsync**：它是旁路，为它付热路径的代价换不到任何安全收益。

### 三、`/wait` 现在看「最新事实」而不是「本进程方便的那份」

新增 `TaskManager.freshest(task_id)`：在「调度器内存」与「磁盘」两份之间取 `revision`
较大的那份。判据用写入序号而不是时间戳，因为两个方向都成立——

- 内存领先磁盘（刚改完还没落盘）：revision 相同 → 取内存（这正是 V2.5 §五 要的「live 状态」）；
- 磁盘领先内存（别的进程推进过）：取磁盘（这正是审核 §四 说的「Worker B 完成、Worker A 还在等」）。

`GET /tasks` 的 `list_all()` 也顺带修正了：它的 docstring 早就写着「对齐 revision 较大的那份」，
而实现只是 `setdefault`（内存永远赢）——**注释比实现强**，属于审核最爱抓的那类问题，
这轮把它对齐了。

### 四、确认流程从一步变两步：`reserve → resolve → commit`

审核 §六 的判断是「这不是安全漏洞，反而是 fail-safe，但用户体验会比较糟」——准确。
改法是把它拆成两阶段：

```
reserve(jti)       # 预占：jti 唯一约束保证原子；此刻**还没有**作废票据
    ↓
resolve_confirmation()   # 真正的副作用在这里
    ↓
commit(jti)        # 成功才把票据永久作废；业务失败（返回 None / 抛异常）则 release 退回
```

- 「一次性」没有变弱：`reserve` 与 `commit` 都是单条 SQL，两个并发请求不可能同时占上；
  `commit` 之后任何 `reserve` 都会失败；`release` 只能退**预占**（`WHERE state='reserved'`），
  退不掉已消费的记录。
- 崩溃残留的预占不会把票据永久卡死：超过 `SHADOW_CONFIRM_RESERVE_TTL_SECONDS`（默认 60s）
  允许被同一个 `jti` 接管。
- 老库（V3.2 建的 6 列表）自动迁移：`state` 列的默认值是 `'consumed'`，
  因为 V3.2 只有一种语义（消费即作废），历史行确实都是那个状态。
- `commit` 失败不回滚业务（状态是真的改了），只把票据留在预占状态并记 warning——
  最坏是「重试要等一会儿」，好过「票据已烧、业务没做」。
- 内存守卫（`InMemoryConsumption`）实现了**同一套** `reserve`/`commit`/`release`：
  否则单测跑的不是生产的那条路径。

### 五、敏感页上的「一无所知的点击」

审核 §七 要的是：`付款 App + 目标找不到 + TAP` 应该转人工。现在：

| 场景 | 改前 | 改后 |
|---|---|---|
| 敏感应用 + 会改页面 + **目标证据缺口** | CAUTION（→ 自动执行） | **DANGEROUS（转人工）** |
| 非敏感应用 + 会改页面 + 目标证据缺口 | CAUTION | CAUTION（不变） |
| 敏感应用 + 会改页面 + 目标解析得到 | CAUTION | CAUTION（不变） |

第二、三行是刻意保留的：不加这两条限制，门禁会变成噪声（每次树读不到都问人），
然后被人绕过——V3.1 P0-3 的教训是**保守要保守在代价不对称的那一侧**。
**没做的事**：敏感应用里「角色 UNKNOWN 但有目标」仍然只到 CAUTION，
那部分属于 §八 的 Policy Engine（缺 App 敏感状态与风险历史输入，见 `models/semantic.py`）。

### 六、`/health` 只回答「活着吗」

- `GET /health`（和 `/healthz`）→ `{"ok": true}`。匿名可访问，因为探针不该带密钥；
  但它也**不能顺带告诉匿名者**「这个实例有没有开鉴权」（＝值不值得试）、部署形态、
  以及「配置坏了、此刻全部 401」。
- `GET /health/detail` → 原来那些字段，**需要鉴权**。

拆分的理由是读者不同：探活的是机器，诊断的是运维本人。

### 七、部署形态与 HTTP 面（§十）

审核提醒的是「Android App 里的 `127.0.0.1` 是手机自己，不是你的开发机」。本仓库的形态是：

| 形态 | 谁提供 HTTP 面 | 与 `HOST` 的关系 |
|---|---|---|
| 路线 B（默认，见 `android/README.md`） | 手机只跑**设备端点**（Kotlin，自己的 `/health` 与令牌），Core 跑在 PC/服务器 | Core 的 `HOST` 保持 `127.0.0.1` 即可；Core **主动**去连手机，不需要被手机连 |
| 手机 UI 直连 Core（未来形态） | Core 需要被局域网访问 | 必须同时配 `SHADOW_API_TOKEN`，否则启动即拒绝裸绑定（既有闸门，不是新加的） |

也就是说：**手机上不需要跑 FastAPI**（方案文档 §2 与 §10 也是这个判断），
所以 `HOST=127.0.0.1` 在这个形态下不是障碍，而是正确默认。

### 八、V4 Storage Refactor（下一步，不是本轮）

接受审核 §十一 的判断：JSON / JSONL / SQLite 混合方案已经到边界了。计划是让
`tasks` / `task_steps` / `step_attempts` / `checkpoints` / `events` / `audit_events` /
`confirmations` / `leases` 进同一个事务数据库，用事务、`revision`、唯一约束、外键
一次性解决现在靠注释与多点检查维持的那些不变量（跨文件一致性、孤儿、事件 durability、
多进程顺序）。

**为什么不在本轮做**：那是把整套存储层同时换掉的大爆炸式改动，而本轮审核的 P0 是
「已经不 durable」，那个洞可以在现有结构里堵住——先堵洞、再换地基，比反过来安全。
`confirmation` 与 `lease` 已经在 SQLite 上，是这条路的两个先例。

### 行为变化提醒（会影响到既有调用方与测试）

1. **`/health` 返回体收窄**：只回 `{"ok": true}`；原来那些字段搬到 `/health/detail`（需鉴权）。
   老用例 `test_health_is_public`、`test_health_reports_confirmation_consumption_backend` 已同步更新。
2. **敏感应用 + 目标解析失败的会改页面动作 → 需人工确认**。老用例
   `test_sensitive_app_raises_the_floor_but_not_to_dangerous` 的场景原本靠「没有 UI 树」间接表达
   「不直接判危险」，现在**显式带上 UI 树**——因为「看不见目标」是另一条规则了。
3. **`/confirm` 的票据在业务失败时退回**，可以拿同一张票据重试（以前是一次作废、要重新申请）。
4. `TaskManager.freshest()` 新增；`list_all()` 的行为有一处收紧（真的按 revision 取较新的一份）。

### 新环境变量

| 变量 | 作用 |
|---|---|
| `SHADOW_CONFIRM_RESERVE_TTL_SECONDS` | 确认票据**预占**的有效期，默认 60 秒。只影响「崩溃后多久能接管这张票据」，不影响令牌自身的 TTL |

### API 变更

| 变更 | 说明 |
|---|---|
| `GET /health` | 返回体收窄为 `{"ok": true}` |
| `GET /health/detail` | **新增**：原 `/health` 的诊断字段，需要鉴权 |

### 验证

`python -m pytest -q` → **632 passed**（上轮 611 → +21，零回归）。

| 新增用例 | 覆盖 |
|---|---|
| `test_event_log.py`（+5） | 安全关键事件真的 `fsync`、普通事件不 fsync、**fsync 失败必须抛**（拦住副作用）、一行只走一次 `os.write`、`O_APPEND`、只有新文件才刷目录项 |
| `test_confirmation_store.py`（+8） | 预占不作废票据、预占不可重复（跨实例）、提交后永久不可用、退回可重试、过期预占可接管、**老库自动迁移**、auth 两阶段接线 |
| `test_api_auth.py`（+3） | 匿名 `/health` 只回 `ok`、`/health/detail` 需要令牌、**业务失败时票据可重试**（端到端） |
| `test_risk_gate.py`（+3） | 敏感页 + 目标盲区 → DANGEROUS、非敏感页仍 CAUTION、敏感页但目标可解析不升级 |
| `test_scheduler.py`（+2） | `freshest` 取磁盘上更新的一份、平局时留在内存（两个方向都钉住） |

---

## V4 落地（依据 `v4审查建议.md`）

审核这轮给的不是缺陷清单，而是一份**十阶段重构计划**：统一执行模型 → 存储事务化 →
审计关联 → 工程化收尾。它自己写着「**不要一次全部重写**」，建议按 8 个 commit 推进、
优先 1–4。本仓库按那个顺序做，已完成其中 4 项：

| 审核的 commit | 本仓库落地 | 状态 |
|---|---|---|
| Commit 1 `refactor: introduce Execution model` | `models/execution.py` + `storage/execution_store.py` + 手工端点接线 + `GET /executions/{id}` | ✅ |
| Commit 4 `refactor: replace JSONL EventLog with SQLite EventStore` | `storage/database.py` + `storage/migrations/` + `storage/event_store.py`；`EventLog` 变成兼容层，旧 `.jsonl` 一次性导入 | ✅ |
| Commit 2 `refactor: unify task/checkpoint/event storage` | 任务、恢复点、事件、票据在**同一个 `shadow.db`**，旧的 `*.json` / `*.jsonl` 一次性导入 | ✅ |
| Commit 3 `fix: transactional confirmation` | `/confirm` 的「预占票据 → 改 Task 状态 → 写事件 → 作废票据」在**一个事务**里 | ✅（见「五」的内存边界） |
| Commit 7 `chore: lock dependencies` | `pyproject.toml` / `requirements.txt` 钉死版本、`requirements-dev.txt`、`uv.lock`（29 包） | ✅ |
| Commit 8 `test: crash/concurrency regression suite` + **事件驱动 `/wait`** | 并发确认、并发手工动作、设备忙落定、旧库导入、序号唯一、事务回滚、**`wait_terminal` 事件驱动** | ✅（崩溃恢复那几条上一轮已在） |
| Commit 5 `refactor: single-writer task mutation` | 单进程下已由 `_mutation_lock` + CAS 达成；Task Actor 是触发条件满足时才做的下一步 | ◑ 见「六」 |

### 一、Commit 1：手工操作有了自己的身份（§1 / §5）

改之前：所有手工操作都记在固定假任务 id `__manual__` 下，于是不同请求的记录**混在一条流**里，
事后只能靠逐条字段去拼「这次是哪个请求」。改之后每次调用一个 `execution_id`：

    POST /tap  →  execution_id = exe_xxx
        ├── principal / device_id / action / risk / request_id / created_at / finished_at / result

- **一执行一个文件**（不是共享文件）：手工请求会并发进来，共享文件就要跨请求读-改-写，
  而那正是本仓库花了好几轮才收敛掉的坑；写走 `JsonStore`（tmp + fsync + os.replace）。
- **`REFUSED` 也是一等事实**：被风险门禁拦下的操作同样留一条记录，状态是 `REFUSED`。
  它回答的正是审核最关心的问题——「有没有过一次没被记录的点击尝试」。
  它与 `FAILED`（设备层没成）刻意分开：事后追责里两者含义完全不同。
- **`UNVERIFIED` 单独一档**：「发出去了但效果没确认（dispatched / navigated / ui_changed）」
  绝不能被记成成功——那正是审核最担心的那一类状态。
- **设备锁的 owner 没有跟着改**：`_MANUAL_OWNER = "__manual__"` 仍然是设备锁身份
  （「手工路径整体占用设备」），它不随请求变化；换掉的是**执行归属**。这两件事容易一起改错，
  所以常量注释里写明了。
- `GET /executions/{id}` 把执行记录与它的事件流拼在一起（靠 `execution_id`），
  `/executions` 列出最近若干条；两者都过鉴权与**设备范围**——`execution_id` 是可枚举的短 id，
  范围外的统一 404（与损坏任务同口径，不泄露存在性）。

### 二、Commit 4：事件日志进 SQLite（§4）

V3.3 给 JSONL 补的是**落盘可靠性**（`write → fsync`），审核列的这一串它给不了：
多进程全序、查询、筛选、分页、关联、事务。现在：

```sql
CREATE TABLE events (
    event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, execution_id TEXT NOT NULL DEFAULT '',
    principal TEXT NOT NULL DEFAULT '', device_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL, created_at TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL,
    UNIQUE (task_id, sequence)
)
```

- **序号而不是时间戳**：`UNIQUE(task_id, sequence)` 让「谁先谁后」有权威依据。
  毫秒级 `datetime.now()` 在两个进程同时写时可能相同，而回放与审计要回答的恰恰是顺序问题。
  序号在同事务内用 `MAX(sequence)+1` 算，唯一约束是最后一道保险（撞了就重算一次）。
- **落盘强度交给数据库**：`PRAGMA synchronous=FULL` 之下 **commit 返回即落盘**，
  与 JSONL 版的 `fsync` 是同一个语义，但覆盖整条事务——不必再逐条记住哪条路径该 fsync。
  `journal_mode=WAL` 让读写不互相阻塞，`busy_timeout=5000` 让多进程写**等待**而不是立刻报错。
- **旧数据不丢**：首次打开时把同目录的 `<task_id>.jsonl` 一次性导入，且**保留原来的
  event_id 与时间戳**（都盖成「导入那一刻」的话，回放就再也看不出事件之间的间隔）。
- 接口一个字没改（`emit` / `emit_critical` / `read` / `kinds`）——它有三十多个调用点，
  这次要换的是**存储**不是用法；分级语义（安全关键事件 fail-closed）也原样保留。
- 运维注意：WAL 模式下数据可能在 `events.db-wal` 里，备份要连 `-wal` / `-shm` 一起拷，
  或者用 `VACUUM INTO 'backup.db'`。

### 三、Commit 7：依赖锁定（§9）

- `pyproject.toml` 与 `requirements.txt` 的直接依赖从 `>=` 改成 `==`（取本机实际安装版本：
  fastapi 0.141.1 / uvicorn 0.52.4 / pydantic 2.13.5 / httpx 0.28.1）。
  理由很实际：这个仓库跑的是会操作真实手机的 Agent，「上周能跑、这周复现不出来」的代价
  远高于偶尔手动升级一次。
- 完整依赖图（29 个包）锁进 `uv.lock`：`uv sync` 装出同一套环境；升级走
  `uv lock --upgrade` → 跑全量测试 → 与 lock 一起提交。
- **pytest 移出生产依赖**：新增 `requirements-dev.txt`（`-r requirements.txt` + `pytest==9.1.1`），
  `pyproject` 里放 `[project.optional-dependencies] dev`。

### 四、Commit 8：并发与故障用例（§10）

审核点名要的那几类，现在都有了：

| 场景 | 用例 | 断言 |
|---|---|---|
| 并发 `POST /confirm` × 2 | `test_concurrent_confirms_only_one_succeeds` | 恰好一个 200，另一个 403/409；同一张票据不能再放行 |
| 并发 `/tap` + `/text` | `test_concurrent_manual_actions_get_their_own_records` | 两条记录、执行 id 不串号、**不留 RUNNING 幽灵** |
| 设备被占用 | `test_busy_device_is_recorded_as_failed_not_running` | 409 之后记录落定 `FAILED`，不是悬着的 RUNNING |
| 两个写者写事件 | `test_two_writers_do_not_lose_events_or_duplicate_sequences` | 一条不丢、序号 1..10 连续不重复 |
| `ACTION_DISPATCHED` 后崩溃 | `test_dangerous_effect_unknown_never_auto_retries`（上一轮已有） | 效果未知 → **绝不自动重复点击** |

### 五、Commit 2 + 3：存储统一与事务化确认

上一节落地的 Commit 4 只搬了 events；这一轮把 `tasks` / `checkpoints` 也搬进**同一个
`shadow.db`**（迁移 v2），确认票据表搬进同一库（迁移 v3），于是审核 §3 的「同一事务」成立：

- **Commit 2**：`TaskStore` / `CheckpointStore` 从「一个实体一个 JSON 文件」换成 `tasks` /
  `checkpoints` 表。四条契约没动（损坏隔离 + 记账 + 留痕、CAS 原子、损坏不可覆盖、索引跨重启重建），
  旧的 `tasks/*.json` / `checkpoints/*.json` 一次性导入且**保留 revision / 时间戳**。
- **Commit 3**：`/confirm` 的「预占票据 → 改 Task 状态 → 写 CONFIRMED 事件 → 作废票据」放进
  **一个数据库事务**（`Database.transaction()` 可重入，内层的 `TaskStore.save` 会加入而不是各开一个）。
  于是审核点名的两种跨存储状态——「Task 已确认 / Token 未消费」「Token 已消费 / Task 没确认」——
  在**存储层**都不再可能：中途失败时三者一起回滚（测试里用「CONFIRMED 事件写失败」验证）。

  **诚实的边界**：数据库事务回滚的是**存储**。`Runtime` / `Scheduler` 手里的**内存对象**不受它
  管辖——那正是审核 §7「single-writer state machine」要根治的问题（见下）。所以这里的保证
  表述为「磁盘上三样东西要么一起落下、要么一起没有」，而不是「内存也一致」。

### 六、Commit 8：事件驱动 `/wait`，以及 Commit 5 的诚实判断

**Commit 8（已落地）**：`/wait` 从「每 100ms `time.sleep` 轮询」改成**事件驱动**——
新增 `scheduler.wait_terminal(task_id, timeout)`，用条件变量阻塞，任务到终态时 worker
`notify_all` 唤醒等待者，不再空转。

- 终态路径统一成「**先落盘、再通知**」（与 submit / resume / recover 同一条纪律）：
  等待者被唤醒后查磁盘一定已经是终态，不会空转一轮。
- 终态任务会从 lane 移出，所以 scheduler 额外留一份终态对象（`_terminal`，限长 200），
  `wait_terminal` 才能返回「那个已经结束的任务」而不是让调用方再查磁盘。
- **跨进程仍然兜底**：`_cond` 只能收到本进程通知，别的进程推进的任务靠 `freshest`
  回查磁盘（每 0.5s 一次）。两条腿：进程内靠事件（省 CPU）、跨进程靠 `freshest`（正确）。

**Commit 5（single-writer）——如实说，没有假装做了重构**：

审核 §7 要的「Command Queue → Task Actor → 唯一写者」是架构演进，不是缺陷。现状已经
**单进程内是 single-writer**：所有「改任务状态」的入口都经 `_mutation_lock` 串行化，
落盘走 revision CAS（`_rewrite_authoritative`），冲突回滚内存。还没做的是把
Runtime/Scheduler 对内存对象的**就地修改**也收进显式写者。

所以这一项**不强行引入 Command Queue**（那会违背审核文档自己说的「不要一次全重写」），
而是把判断讲透、写死在 `agent/task_manager.py` 的注释里：什么已经是 single-writer、
什么还不是（内存对象与磁盘的一致性没有兜底——正是 §3 那条「事务回滚不了内存对象」的边界）、
V4 之后「唯一写者」可以落在**数据库事务**上（地基已铺好）、以及**触发条件**
（多路注入成常态 + CAS conflict 成规模 / 多 worker 部署成常态）。

### 七、还没做的（附触发条件）

- **Commit 5 的 Task Actor 化**：触发条件见 `agent/task_manager.py` 注释——多路注入成常态、
  CAS conflict 成规模、或跨进程写同一 Task 成常态时，把 mutation 收口到「数据库事务里的唯一写者」。
- **Policy Engine 的剩余两步**（v3.3 §八 的延期项）：
  ① 「App 敏感状态」已经补了一半——V4 起除了包名静态词表，还从 UI 树认出「确认支付/转账金额/
  验证码」等**敏感屏特征**（`SENSITIVE_SCREEN_MARKERS`）；还缺的是把 `infer_role` 从关键词升级成
  真正的 model classifier。
  ② 「风险历史回路」完全没有（`RISK_ASSESSED` 有落库但无回路）。
  两项的触发条件都写在 `models/semantic.py`：`UNKNOWN` 成为高频角色、人工确认被刷屏时做。

### 八、行为变化提醒

1. 手工端点（`/tap` `/text` `/back` `/actions`）的响应**新增** `execution_id` / `execution_status`
   （只加字段，不删不改名）。
2. 手工操作的事件流**所有者**从 `__manual__` 换成每次请求的 `execution_id`。
   `MANUAL_ACTION_TASK_ID` 仍保留，但已标注废弃——只为兼容旧引用。
3. 新增 `GET /executions`、`GET /executions/{id}`（都需要鉴权，受设备范围约束）。
4. **存储布局统一到 `<存储目录>/shadow.db`**：任务、恢复点、事件、确认票据都在里面。
   旧的 `tasks/*.json` / `checkpoints/*.json` / `events/*.jsonl` / `confirmations.db`
   首次打开时一次性导入；`scripts/replay_task.py --list` 改为问存储。
   `SHADOW_CONFIRM_DB` 仍可覆盖，但指向别的文件时 §3 的跨表事务不成立。
5. 依赖钉死版本：`pip install -r requirements.txt` 装运行依赖，测试用 `requirements-dev.txt`。

### 八、验证

`python -m pytest -q` → **675 passed**（上轮 672 → +3，零回归）。

| 新增用例 | 覆盖 |
|---|---|
| `test_execution.py`（13 条） | 执行 id 唯一、落定不改身份字段、拒绝保留依据、落定走原子读改写、跨实例可见、坏记录读成缺失、未来字段可读、写失败抛 `PersistenceError` |
| `test_api.py`（+4 条） | 一次执行的完整链条（记录 + 事件）、被拒也留痕且 `REFUSED`、并发手工动作各自成记录、设备忙落定 `FAILED` |
| `test_api_auth.py`（+6 条） | 执行记录带调用方身份、`/executions` 需鉴权且未知 id 一律 404、**并发确认只有一个成功**、**确认中途失败三者一起回滚**、成功三者一起落下、票据表与 tasks/events 同库 |
| `test_api_authz.py`（+1 条） | 执行记录也受设备范围约束 |
| `test_database.py`（9 条） | 持久化 PRAGMA 真的生效、迁移版本可查、事务回滚一切/提交一切、**事务可重入**、共享库、两种旧目录传法、坏行隔离、写失败可见 |
| `test_scheduler.py`（+3 条） | **`wait_terminal` 事件驱动**：及时返回终态任务、一直跑则超时返回 None、**先落盘再通知**（醒来时磁盘必已终态） |
| `test_risk_gate.py`（+3 条） | **敏感屏**：普通 App 弹出的收银台页 + 目标不确定 → DANGEROUS、敏感屏上会改页面动作至少 CAUTION、无敏感文案的普通屏不升级 |
| `test_task_store.py` / `test_checkpoint_store.py` / `test_event_log.py` | 旧 JSON/JSONL 迁移保留 revision 与时间戳、坏文件迁移后仍隔离、序号连续、两写者不丢不重、重启可读 |

---

## V4.1 修复轮（依据 `v4.1审核建议.md`）

审核这一份**不是缺陷清单，是十阶段重构计划**（它自己写着「不要大改，按 commit 推进」）。
所以本轮按它给的 commit 顺序分 **6 次提交**落地，每条 commit message 点名对应哪个阶段。

### 一、先说核对：哪些阶段本来就成立

审核文档通常针对更早的提交，所以先拿它的代码片段去对真实源码：

| 阶段 | 核对结果 |
|---|---|
| §八「events 增加 `execution_id` 列」 | **早就做了**。`events.execution_id` 与 `idx_events_execution` 是迁移 **v1** 建的（V4 §四），`EventLog.read_by_execution()` 也在。本轮补的是**另一件事**：Agent 路径的事件以前没有 execution_id 可带——因为那条路径压根没有执行记录 |
| §九「增加统一 ExecutionService」 | **部分成立**。手工端点已有一个执行内核（`run_manual_action`，V3.2 §一），但它不是独立入口，Agent 路径连执行记录都没有。本轮补成 `agent/execution/` |
| §十 测试3「重复确认」 | **早有覆盖**：`test_api_auth.py::test_confirmation_token_is_single_use`（V3.2 §六 起票据是两阶段 reserve/commit + jti 唯一约束） |
| §十 测试4「同一 execution_id 不能 tap 两次」 | 机制原来不存在（文件存储的「读-判断-写」之间有窗口）。本轮用受保护迁移把它做成一条 SQL |
| §五 状态机 | 只有 `RUNNING / SUCCEEDED / FAILED / REFUSED / UNVERIFIED`——**缺全部中间态**，而中间态正是崩溃恢复唯一能用的信息 |
| §一/§二/§三（executions 表 + 换实现） | 确实仍存在：执行记录是**最后一个**还在 JSON 上的存储 |

### 二、逐阶段落地

| 阶段 | commit | 落点 |
|---|---|---|
| §一 executions 表 | `feat(storage): add executions sqlite table` | 迁移 **v4**：表 + 4 条索引（task / principal / device / **status**，最后一条是恢复扫描的入口） |
| §二/§三 换 SQLite 实现 | `refactor: replace JSON ExecutionStore with SQLite` | `storage/execution_store.py` 内部换实现，**接口不变**；新增 `create()` / `transition()`；旧 `executions/*.json` 首次打开导入 |
| §四/§五/§九 事务顺序 + 状态机 + 服务 | `feat: add Execution state machine` | `agent/execution/{state,service}.py`；`CREATED → RISK_CHECKED → DISPATCHED → RUNNING →` 终态 |
| §六/§七 启动恢复 + 禁重试 | `feat: add stale execution recovery` | `agent/execution/recovery.py`；`GET /executions?status=UNKNOWN` + `/health/detail.executions_effect_unknown` |
| §八/§九 统一（含 Agent 路径） | `refactor: unify execution service` | `AgentRuntime(executions=...)`；Act 环节建记录、`action_dispatched` 带 execution_id；`GET /executions/{id}` 改按 execution_id 取事件 |
| §十 故障测试 | `test: add crash recovery tests` | `tests/test_execution_faults.py` |

### 三、与审核的三处**有意偏离**（都写在代码注释里，不是遗漏）

1. **事务顺序**：审核原图把「执行设备」画在 `ACTION_DISPATCHED` **之前**。照那样写，
   「先记录意图、再产生副作用」这条写前日志的纪律就没了——设备调用成功而 `DISPATCHED`
   写失败时，没有任何东西能证明那次点击是本系统发出的。改成
   `受理 → 判风险 → 记意图 → 交给设备 → 记结果`，设备调用在**所有事务之外**
   （Prepare → Effect → Commit，审核 §四 的原则，只是顺序按它自己的原则修正了）。

2. **`DISPATCHED` vs `RUNNING`**：审核的状态集里 `ACTION_DISPATCHED` 是最后一个中间态，
   所以它的测试1只能要求「此后被杀一律记 UNKNOWN」。本实现把这一格拆成两格：

   ```
   DISPATCHED  意图已落盘，设备**还没**被调用   → 崩溃后 FAILED（可安全重做）
   RUNNING     动作已交给设备                  → 崩溃后 UNKNOWN（禁止自动重试）
   ```

   两次写入之间只有 `RUNNING` 一条语句，所以死在 `DISPATCHED` 上等于
   `executor.execute` 从未被调用过——这不是推测，是代码顺序。代价是每条动作多一次
   SQLite 提交；收益是「进程死在按下付款之前」这种最常见的情况不会进人工队列
   （门禁变成噪声之后就会被绕过，V3.1 P0-3 的教训）。

3. **不建 `execution_attempts` 表**：审核建议 `UNIQUE(execution_id, action_attempt)` 拦
   「同一条执行被派发两次」，而 `status` 本身就是那个唯一约束（`UPDATE ... WHERE
   execution_id=? AND status=?`，读-判断-写全在一个 `BEGIN IMMEDIATE` 里）。
   多一张表只会多一份可能与状态不一致的事实。它**拦不住**的是「同一意图被执行两次」
   （两个不同的 execution_id），那属于上层职责（`reconciliation` / `is_safe_to_retry`）。

### 四、行为变化提醒

1. **执行记录搬进 `<存储目录>/shadow.db`**（此前是一执行一个 JSON 文件）。
   旧的 `executions/*.json` 首次打开时一次性导入，**保留 id / 时间戳 / 状态**——
   否则历史记录会看起来全发生在升级那一刻，§六 的恢复扫描会立刻把它们当成刚崩溃的执行。
2. **执行状态多了中间态**：`GET /executions` 里会先看到 `CREATED` / `RISK_CHECKED` /
   `DISPATCHED` / `RUNNING`；终态多一个 **`UNKNOWN`**（进程死在设备调用之后）。
   老的状态名与含义没变，新字段只增不改。
3. **`GET /executions` 现在包含 Agent 路径的记录**（以前只有手工端点有）。
   用 `channel` 区分：`manual` / `agent`。
4. 新增 `GET /executions?status=UNKNOWN`（逗号分隔，状态名写错 400）与
   `/health/detail.executions_effect_unknown`。
5. **启动时会扫描并收掉崩溃遗留**。新环境变量 `SHADOW_EXECUTION_STALE_SECONDS`
   （默认 0；显式开了 `SHADOW_ALLOW_MULTI_PROCESS` 时默认 300）。
6. **被拒绝的手工动作现在也留一条 `RISK_ASSESSED` 事件**（以前什么都没有，
   `GET /executions/{id}` 只有记录字段、没有「凭什么拒」）。
7. **删除了 `storage/json_store.py`**：执行记录是它最后一个使用者。
   此后所有需要「查询 / 受保护迁移 / 与别的事实同事务」的东西都在表里。
8. 每条 Agent 动作多 4 次 SQLite 提交（`create` / `assessed` / `dispatched`+`running` /
   `settle`）。`synchronous=FULL` 之下这是毫秒级，相对一次 VLM 调用可忽略。

### 五、验证

`python -m pytest -q` → **719 passed**（上轮 675 → +44，零回归）。

| 新增/改动用例 | 覆盖 |
|---|---|
| `test_execution_state.py`（12 条） | 状态机：终态无出边、**`UNKNOWN` 只能从 `RUNNING` 进来**、恢复分流（设备有没有被调用过）、非法迁移抛异常而守卫落空只返回 `False`、**事件写失败时状态回滚**、派发窗口、同一执行不可派发两次、`UNKNOWN` 终态不可改写、内存对象与库一致、事件归属 |
| `test_execution_recovery.py`（10 条） | 死在设备前后两种结论、幂等、宽限窗口、恢复留痕的分级、`UNKNOWN` 没有回程、坏行不影响、无任务的手工执行、升级前的老记录 |
| `test_execution_faults.py`（5 条） | **点击进行中 `kill -9`** → `UNKNOWN` 且不可重试、**设备调用前被杀** → `FAILED`、**落定写失败** → 降级 `UNKNOWN`（不留幽灵）、数据库真写不动 → 留给恢复、降级目标由状态机算 |
| `test_execution.py`（+5 条 / 改 4 条） | 状态词汇分区、`DISPATCHED`/`RUNNING` 分界、`create` 拒绝覆盖、受保护迁移、终态不可改写、历史 JSON 一次性导入 |
| `test_database.py`（+2 条） | `executions` 表形状与四条索引、新库一次迁到最新版本 |
| `test_api.py`（+4 条 / 改 2 条） | 启动恢复、`?status=` 过滤与 400、`/health/detail` 计数、宽限默认值 |
| `test_runtime.py`（+4 条） | **Agent 路径的执行身份**与事件链、`action_dispatched` 带 execution_id、只申请完成不建记录、跑完不留非终态执行 |

> 残留：`SHADOW_EXECUTION_STALE_SECONDS` 只在**启动时**扫一次，没有周期性巡检。
> 触发条件是「服务长期不重启但仍出现崩溃遗留」——那时应当把恢复挂到定时任务上。

---

## 快速开始

```powershell
# 方式一：只装运行依赖
pip install -r requirements.txt
# 要跑测试再装 dev 依赖（V4 §9：pytest 不在生产依赖里）
pip install -r requirements-dev.txt

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
| `ADB_SERIAL` | 目标设备 serial；**支持逗号分隔多台**（如 `emu-1,emu-2`） | `emulator-5554` |
| `VLM_BASE_URL` | VLM 接口地址 | `https://api.openai.com/v1` |
| `VLM_API_KEY` | VLM API Key；不设置则关系判定退化为纯规则 | 未设置 |
| `VLM_MODEL` | VLM 模型名 | `gpt-4o` |
| `VLM_DETAIL_PLAN` / `_DECIDE` / `_VERIFY` | 各阶段图片精度 | `low` / `high` / `low` |
| `ARTIFACT_DIR` | 截图落盘目录 | `artifacts/shots` |
| `STORAGE_DIR` | 任务 / 检查点 / 事件持久化目录 | `artifacts/state` |
| `OBSERVE_BUDGET_SECONDS` | 一次采集（截图 + UI 树 + 上下文）的**总**预算，决定抢占延迟上界 | `12` |
| `ADB_READ_TIMEOUT_SECONDS` | 采集类 adb 命令的单条超时（写类固定 15s） | `6` |
| `PORT` | API 端口 | `8010` |
| `HOST` | 监听地址；**非回环且未配令牌时拒绝启动** | `127.0.0.1` |
| `SHADOW_API_TOKEN` | API 访问令牌；不设置则关闭鉴权（仅建议本机） | 未设置 |
| `SHADOW_API_READONLY_TOKEN` | 只读令牌（仅 GET） | 未设置 |
| `SHADOW_API_DEVICE_ALLOW` | 令牌可操作的设备 serial，逗号分隔 | 不限 |
| `SHADOW_REQUIRE_AUTH` | 置 1 时即使没配令牌也拒绝一切请求 | 未设置 |
| `GOAL_VERIFY_MODE` | 完成验证严格度。未设置 / `auto`：**按任务画像**自动判定（纯查询→`advisory`、导航/副作用→`strict`）；`off` / `advisory` / `strict`：**全局覆盖**该判定 | 未设置（按画像） |
| `AUDIT_DIR` | 请求审计目录 | `$STORAGE_DIR/audit` |
| `SHADOW_AUDIT` | 置 0 关闭请求审计 | 开启 |
| `SHADOW_API_PRINCIPALS` | **推荐**的身份配置（JSON）：每 principal 独立 `token` / `read_only` / `devices`。设置后 legacy 令牌变量被忽略 | 未设置 |
| `SHADOW_CONFIRM_DB` | 确认令牌「已消费」记录的 SQLite 路径；**多实例必须指向同一个文件** | `$STORAGE_DIR/confirmations.db` |
| `SHADOW_TOCTOU_GUARD` | 置 0 关闭执行前的页面身份复查（V3.1） | `1`（开启） |
| `SHADOW_DEBUG` | 置 1 时 500 响应回传异常摘要（默认脱敏） | 未设置 |

## API

### 任务

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/tasks` | 创建任务。默认后台执行，`wait=true` 同步等待（超时 504） |
| `GET` | `/tasks` | 列出全部任务 |
| `GET` | `/tasks/{id}` | 查询详情（含计划进度、待确认动作 + 确认令牌、最近一次完成裁定） |
| `POST` | `/tasks/{id}/pause` | 暂停 |
| `POST` | `/tasks/{id}/resume` | 恢复 |
| `POST` | `/tasks/{id}/cancel` | 取消 |
| `POST` | `/tasks/{id}/inject` | **执行中注入新指令**，由任务关系决定并入/排队/抢占 |
| `POST` | `/tasks/{id}/confirmation-token` | **显式申请确认令牌**（V2.7 P1-8）。`GET /tasks/{id}` 只给元数据，不再下发可使用的令牌 |
| `POST` | `/tasks/{id}/confirm` | 危险动作的人工确认 / 完成裁定 / 崩溃恢复放行。启用鉴权时需带 `token` |
| `GET` | `/tasks/{id}/history` | 执行轨迹（给下一步决策看，会被裁剪） |
| `GET` | `/tasks/{id}/events` | **审计事件流**（只追加，含抢占/对账/风险判定/完成驳回） |
| `GET` | `/tasks/{id}/replay` | **回放**：`format=markdown` 给人看，默认 JSON 给程序用 |
| `GET` | `/tasks/{id}/checkpoint` | 最新恢复点 |
| `GET` | `/tasks/{id}/shots/{n}` | 某一步的截图 |
| `GET` | `/scheduler` | 调度器状态（含 `devices` 逐设备详情与 `running_tasks`） |
| `GET` | `/health` | 探活。**唯一不需要鉴权**的端点 |

### 预算怎么传

三种上限互相独立，`max_steps` 只是动作步数的兼容别名：

```json
{
  "instruction": "帮我订一张高铁票",
  "budget": { "max_action_steps": 25, "max_observations": 80, "max_model_calls": 60 }
}
```

`max_steps=10` 等价于 `{"budget": {"max_action_steps": 10}}`，
**观察与模型调用仍是默认的 60 / 40**——这一点以前是含糊的，现在写死在兼容层里。

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

## 测试（不随仓库发布）

> **本仓库只发布运行时。** `tests/` 与 `scripts/` 由开发侧本地维护，不进版本控制——
> 从远程 clone 下来的副本里没有测试代码，下面这条命令是给持有完整开发副本的人看的。
> 需要取回某一轮的测试基线：`git checkout <commit> -- tests scripts`。

```powershell
# 运行依赖（钉死版本；完整依赖图见 uv.lock，可用 `uv sync` 复现）
pip install -r requirements.txt
# 测试依赖（V4 §9：pytest 已移出生产依赖）
pip install -r requirements-dev.txt
python -m pytest -q
```

**719 个用例，全部离线**：不需要 adb、模拟器或 API Key。

| 文件 | 覆盖 |
|---|---|
| `test_confirmation_store.py` | **确认令牌的一次性**：SQLite 消费记录（jti 主键 = 跨进程 CAS）、重启后仍拒绝、证据字段、过期清理 |
| `test_checkpoint_store.py` | **恢复点孤儿清理**：已提交的留着、孤儿与被取代的清掉、无指针任务不构成保留 |
| `test_semantic.py` | **动作语义层**：语义角色优先级（最危险优先）、同一 role 派生 risk 与幂等、`UNKNOWN` 的保守下限 |
| `test_models.py` | 任务状态机、**故障/恢复态（DEGRADED / DEVICE_UNAVAILABLE）回归**、步骤依赖、动作风险（策略下限）/指纹、Checkpoint、预算、版本号、**尝试历史** |
| `test_device.py` | ADB 封装、输入通道（含中文）、设备会话所有权与并发、**命令超时分级与总预算**、**多设备 serial 解析** |
| `test_device_pool.py` | **DevicePool**：注册/查找、未知设备报错、空闲筛选、产物按设备分目录 |
| `test_task_store.py` | **损坏任务**：隔离到 `quarantine/`、`corrupt_ids` 记账、`TASK_CORRUPTED` 留痕、结构漂移同样算损坏 |
| `test_trajectory_store.py` | **轨迹落盘**：重启可读、ui_tree 不落盘、窗口裁剪、紧凑化、坏行容错 |
| `test_vision.py` | UI 树容错、坐标落点、VLM 重试、prompt 构造、**严格验证枚举**、**风险建议与完成声明解析** |
| `test_evidence.py` | **多级证据**：结构指纹、导航变化、目标元素状态、树坏掉时判「不可比」 |
| `test_risk_gate.py` | **风险门禁**：策略⊕模型、降级被拒并留痕、**UI 节点文本抬升风险**、敏感页下限 |
| `test_goal_verifier.py` | **目标验证**：可核验声明命中/矛盾、计划完成、页面无推进时驳回、严格/关闭模式 |
| `test_verifier.py` | **验证三概念** + **认不出的 VLM 结论不当成功**、危险动作无证据不放过 |
| `test_event_log.py` | 事件日志：顺序、按任务隔离、limit、截断行容错、写失败不抛异常 |
| `test_replay.py` | **回放**：时间轴顺序与偏移、异常帧挑选、Markdown 报告、动作计划、**重放的安全默认** |
| `test_classifier.py` | 三层关系判定、相似度否决、**分关系阈值**、二次确认标记、**语义相似度** |
| `test_scheduler.py` | 优先级、暂停/取消、**抢占与恢复**、组合式中断、**启动恢复**（含审批前重启）、**抢占延迟观测**、设备占用、**多设备并行/绑定/改派**、**revision CAS**、**worker 失败分流（DEGRADED / DEVICE_UNAVAILABLE）** |
| `test_multi_device_e2e.py` | **双设备跨设备干扰**、逐设备 running 视图、**SUBTASK 注入 + 崩溃恢复**（含 `plan_version`）、恢复后跑完、**并发改写不碰已结束的任务** |
| `test_runtime.py` | 闭环执行、异常收敛、死循环、HITL、Checkpoint 恢复、**三预算门控**、**动作对账**、**效果未知在线对账**、**完成申请驳回/转人工**、**风险门禁接入闭环**、事件自足性、按绑定设备取会话、**启动期持久化失败降级** |
| `test_api.py` | HTTP 契约、状态码语义、错误脱敏、危险动作拦截、SUPER_TASK 改写与二次确认、依赖链迁移、版本门控、`/events`、`/replay`、**预算入参**、**损坏任务的 `recovery_error` 表达** |
| `test_api_auth.py` | **鉴权/只读/设备范围/确认令牌/请求审计**、`/health` 公开、**非回环裸绑定拒绝启动**、**确认令牌一次性消费** |
| `test_goal_policy.py` | **任务画像 → 验证严格度**：导航/副作用/纯查询/未知分类与优先级（副作用 > 导航）、默认按画像分层、显式 `GOAL_VERIFY_MODE` 覆盖、同类情形按任务类型给出不同裁定 |
| `test_api_authz.py` | **授权边界**：设备范围裁剪（读 / inject / devices / 调度快照）、越界设备 403 而非 500、确认令牌绑定操作者、否决危险动作不杀任务 |
| `test_android_adapter.py` | **Android 后端**：端口完整性在装配期被查、桥异常归一成 `DeviceError`、UI 树非 uiautomator 格式被拒、读不到树抛异常（不是空串）、预算耗尽后不再开始采集、输入走 `ACTION_SET_TEXT`、**假桥驱动整条任务链跑通**、**危险动作照样被 RiskGate 拦下** |
| `test_android_remote_bridge.py` | **桥的远程传输**：12 个方法的路径与 JSON 键逐条对齐、令牌随请求发出、截图走裸字节、权限问题映射成 `AndroidServiceUnavailable`、四种失败各有可操作的提示、同进程优先于远程、**两条路线都不通时绝不回退 adb** |
| `test_android_bridge_contract.py` | **跨语言契约**：Kotlin 方法名与参数个数 == 协议、HTTP 路由表覆盖全 12 个、序列化器属性集合 ⊇ `vision/parser.py` 的读取集合、golden UI 树喂给真实 `vision.target` / `agent.evidence` / `agent.risk_gate` 都能用、清单权限与辅助功能标志齐备、`R.*` 引用与清单 `@string` 必须在 `res/` 里存在（AAPT 的替身） |
| `test_device_port.py` | **设备端口解耦**：核心侧五个模块不出现 `AdbController`、设备参数都标 `DeviceController`、AST 扫出的设备能力全在协议里、`_REQUIRED_METHODS` 与协议声明一致、ADB 专属通道反向保留 ADB 标注 |
| `test_execution_state.py` | **执行状态机与唯一写入口**（v4.1 §四/§五/§九）：迁移合法性、`UNKNOWN` 仅由 `RUNNING` 进入、恢复分流、事件写失败时状态回滚、派发守卫、终态不可改写 |
| `test_execution_recovery.py` | **崩溃遗留的启动恢复**（v4.1 §六/§七）：死在设备前后两种结论、幂等、宽限窗口、`UNKNOWN` 不可自动重试 |
| `test_execution_faults.py` | **故障测试**（v4.1 §十）：点击中被 `kill -9`、落定写失败降级、数据库写不动留给恢复 |
| `test_database.py` | 持久化 PRAGMA、迁移版本、事务可回滚/可重入、**`executions` 表与索引**、共享库、旧目录两种传法、坏行隔离 |

## 注意事项

- `/text` 仅接受安全 ASCII（字母、数字及 `_.@,/?!`），空格转义为 `%s`；
  中文走 ADB Keyboard 广播（设备需安装 `com.android.adbkeyboard`）。
- 坐标解析：`target` 可为 `{"x":..,"y":..}`、`"x,y"`、`"x1,y1,x2,y2"`、`"[x1,y1][x2,y2]"` 或元素描述文本。
  0~1 之间按归一化比例换算，其余按像素；换算基准取 `wm size` 的 **Override size**（实际渲染尺寸）。
  解析失败会抛出明确错误，不会静默回退到屏幕中心。
- **单设备串行**：写操作经由 `DeviceSession` 串行化；多设备时每台一条车道、各自串行。
- **部署提醒**：这个 API 的敏感端点（`/actions`、`/tasks`、`confirm`、`inject`）能直接操作真实手机。
  只在本机用可以零配置；一旦要放到 0.0.0.0 / Docker / 反向代理后面，
  必须配 `SHADOW_API_TOKEN`，否则服务会拒绝启动。`/confirm` 启用鉴权后还需带确认令牌。
- 执行器与观察阶段都承诺「不抛异常」，失败统一收敛为 `ERROR` 步骤并计入重试熔断；
  任务一旦启动，任何异常都会先把状态落为失败态，不会留下卡在 `running` 的僵尸任务。
- VLM 调用对 429 / 5xx / 网络错误做 3 次指数退避重试；4xx（除 429）不重试。
- **存储层已经全部收敛到 SQLite**（V4 §二 + v4.1 §二）：任务、恢复点、事件、确认票据、
  执行记录在同一个 `<存储目录>/shadow.db` 里，靠 `Database` 共享连接与**可重入事务**。
  仍为文件的只有轨迹（会被裁剪）与请求审计（HTTP 层旁路）。
  接口是窄方法集，换成 PostgreSQL 只需替换实现类。
- **执行状态里的 `UNKNOWN` 禁止自动重试**（v4.1 §七）：它表示「手机侧可能已经产生副作用，
  而系统不知道结果」。想继续只能重新观察对账或转人工。
  用 `GET /executions?status=UNKNOWN` 找它们，用 `/health/detail` 看积压数。
