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
│   ├── verifier.py         # 多级验证：Device → 导航 → 结构 → 目标元素 → VLM
│   ├── evidence.py         # 多级证据：页面变没变、依据哪一层（V2.2 §六）
│   ├── goal_verifier.py    # 目标验证：完成是「申请」，由独立证据裁定（V2.2 §四）
│   ├── task_manager.py     # 任务生命周期与指令注入
│   ├── classifier.py       # 任务关系识别（三层融合）
│   ├── risk_gate.py        # 统一风险门禁（策略风险 = 下限，模型只能抬不能降）
│   ├── reconciliation.py   # 动作对账：EFFECT_UNKNOWN → 继续/重做/重规划/找人
│   ├── replay.py           # 任务回放：事件流 → 时间轴 + 「值得注意的地方」
│   └── scheduler.py        # 优先级队列 / 抢占 / 恢复
├── models/
│   ├── task.py             # Task + 8 态状态机 + 优先级 + 预算 + 版本号
│   ├── task_step.py        # TaskStep：计划是可追踪的状态机（只描述计划）
│   ├── step_attempt.py     # StepAttempt：一次尝试的经过（动作/结果/错误分类/证据层）
│   ├── task_relation.py    # TaskRelation：5 种任务关系
│   ├── checkpoint.py       # Checkpoint：恢复所需的最小状态 + 版本门控
│   ├── action.py           # Action + 风险等级（策略下限）+ 指纹 + 动作效果状态
│   ├── budget.py           # TaskBudget：三独立预算（动作步数 / 观察 / 模型调用）
│   ├── retry.py            # ErrorClass + RetryPolicy：错误分类与重试的唯一真相源
│   ├── verification.py     # ActionDispatch / ActionEffect / GoalVerification 三概念
│   └── state.py            # Observation / StepOutcome
├── storage/                # TaskStore / CheckpointStore / TrajectoryStore / EventLog / AuditLog
├── device/
│   ├── adb.py screenshot.py accessibility.py emulator.py
│   ├── session.py          # DeviceSession：设备所有权与抢占交接
│   ├── pool.py             # DevicePool：serial → 会话的注册表（多设备）
│   └── input.py            # InputProvider：ASCII 与中文输入通道
├── vision/                 # vlm / grounding / parser
│   ├── fingerprint.py      # UI 结构指纹：恢复校验的 L2（比 package 细、比 VLM 便宜）
│   └── target.py           # 把动作目标还原成 UI 节点（风险判定与效果验证共用）
├── api/
│   ├── server.py           # FastAPI
│   └── auth.py             # 令牌 / 只读 / 设备范围 / 人工确认令牌（V2.2 §九）
├── scripts/
│   ├── demo_preemption.py  # 抢占恢复演示（离线可跑）
│   └── replay_task.py      # 命令行回放一个任务的事件流
└── tests/                  # 389 个离线用例
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
绑定的设备不在池里（拔线/换机）时改派，否则这条任务永远没人取走、悄悄变成僵尸。

单设备时只有一条车道，与旧实现逐字等价——这一点由当时的 257 个既有测试守着
（V2.2 修复轮之后合计 389 个）。

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
| `POST` | `/tasks/{id}/confirm` | 危险动作的人工确认 / 完成裁定。启用鉴权时需带 `token` |
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

## 测试

```powershell
pip install -r requirements.txt
python -m pytest -q
```

**389 个用例，全部离线**：不需要 adb、模拟器或 API Key。

| 文件 | 覆盖 |
|---|---|
| `test_models.py` | 任务状态机、步骤依赖、动作风险（策略下限）/指纹、Checkpoint、预算、版本号、**尝试历史** |
| `test_device.py` | ADB 封装、输入通道（含中文）、设备会话所有权与并发、**命令超时分级与总预算**、**多设备 serial 解析** |
| `test_device_pool.py` | **DevicePool**：注册/查找、未知设备报错、空闲筛选、产物按设备分目录 |
| `test_trajectory_store.py` | **轨迹落盘**：重启可读、ui_tree 不落盘、窗口裁剪、紧凑化、坏行容错 |
| `test_vision.py` | UI 树容错、坐标落点、VLM 重试、prompt 构造、**严格验证枚举**、**风险建议与完成声明解析** |
| `test_evidence.py` | **多级证据**：结构指纹、导航变化、目标元素状态、树坏掉时判「不可比」 |
| `test_risk_gate.py` | **风险门禁**：策略⊕模型、降级被拒并留痕、**UI 节点文本抬升风险**、敏感页下限 |
| `test_goal_verifier.py` | **目标验证**：可核验声明命中/矛盾、计划完成、页面无推进时驳回、严格/关闭模式 |
| `test_verifier.py` | **验证三概念** + **认不出的 VLM 结论不当成功**、危险动作无证据不放过 |
| `test_event_log.py` | 事件日志：顺序、按任务隔离、limit、截断行容错、写失败不抛异常 |
| `test_replay.py` | **回放**：时间轴顺序与偏移、异常帧挑选、Markdown 报告、动作计划、**重放的安全默认** |
| `test_classifier.py` | 三层关系判定、相似度否决、**分关系阈值**、二次确认标记、**语义相似度** |
| `test_scheduler.py` | 优先级、暂停/取消、**抢占与恢复**、组合式中断、**启动恢复**（含审批前重启）、**抢占延迟观测**、设备占用、**多设备并行/绑定/改派** |
| `test_multi_device_e2e.py` | **双设备跨设备干扰**、逐设备 running 视图、**SUBTASK 注入 + 崩溃恢复**（含 `plan_version`）、恢复后跑完 |
| `test_runtime.py` | 闭环执行、异常收敛、死循环、HITL、Checkpoint 恢复、**三预算门控**、**动作对账**、**效果未知在线对账**、**完成申请驳回/转人工**、**风险门禁接入闭环**、事件自足性、按绑定设备取会话 |
| `test_api.py` | HTTP 契约、状态码语义、错误脱敏、危险动作拦截、SUPER_TASK 改写与二次确认、依赖链迁移、版本门控、`/events`、`/replay`、**预算入参** |
| `test_api_auth.py` | **鉴权/只读/设备范围/确认令牌/请求审计**、`/health` 公开、**非回环裸绑定拒绝启动** |
| `test_goal_policy.py` | **任务画像 → 验证严格度**：导航/副作用/纯查询/未知分类与优先级（副作用 > 导航）、默认按画像分层、显式 `GOAL_VERIFY_MODE` 覆盖、同类情形按任务类型给出不同裁定 |
| `test_api_authz.py` | **授权边界**：设备范围裁剪（读 / inject / devices / 调度快照）、越界设备 403 而非 500、确认令牌绑定操作者、否决危险动作不杀任务 |

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
- 存储层默认是 JSON 文件（可读、便于演示），接口是窄方法集，
  换成 SQLite / PostgreSQL 只需替换实现类。
