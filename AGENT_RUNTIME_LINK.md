# Agent 链路说明

---

## 1. 定位

`agent/` 是任务级 Agent Runtime 的**执行与判定层**。整体链路是：

```
API → TaskManager → Classifier → Scheduler → AgentRuntime → Vision / Device
```

四句话概括职责边界（越界就是设计事故）：

| 环节 | 它回答的唯一问题 |
|---|---|
| `TaskManager` | 任务**是什么状态**、用户的新指令**怎么并进现有任务** |
| `Classifier` | 新指令与现有任务**是什么关系** |
| `Scheduler` | **谁**用设备、谁让位、谁等恢复 |
| `AgentRuntime` | **怎么**把这一个任务跑完 |
| `planner` | 下一步**做什么**（不产坐标） |
| `executor` | **怎么操作**设备（永不抛异常） |
| `verifier` | 这一步**成没成**（不改任何状态） |

---

## 2. 模块地图

### 2.1 调度侧

| 文件 | 关键入口（行号） | 职责 |
|---|---|---|
| `agent/task_manager.py` | `create` 125 · `inject` 359 · `resolve_confirmation` 295 | 任务生命周期、指令注入、**唯一能改 `Task.status` 的出口** |
| `agent/classifier.py` | `classify` 194 | 判定新指令与现有任务的关系 |
| `agent/scheduler.py` | `submit` 469 · `_lane_loop` 1019 · `_execute` 1061 · `_handle_outcome` 1126 | 排队、抢占、设备分配、结果处置 |

### 2.2 执行侧（AgentRuntime 及其 mixin）

`AgentRuntime` 由 `runtime.py` 的主类与 5 个 mixin 组成（`runtime.py:110` 起）。mixin 用 `self` 共享能力，
零行为改动——拆分的目的是**让每个子域能被单独读懂和单独测试**，不是改变语义。

| 文件 | 关键入口 | 子域 |
|---|---|---|
| `agent/runtime.py` | `safe_point` 197 · `refresh_user_context` 434 · `_save_checkpoint` 612 · `_persist` 704 | 装配、安全点、事件与持久化原语 |
| `agent/_execution.py` | `run` 78 · `_run_loop` 340 · `_execute` 1065 · `_verify` 1077 | 主循环与五个阶段 |
| `agent/_goal.py` | `_request_finish` 28 · `_confirm_goal_locked` 151 | 完成申请与裁定 |
| `agent/_confirm.py` | `confirm` 37 · `_confirm_locked` 42 | 三类「等人拍板」的统一入口 |
| `agent/_reconcile.py` | `_settle_reconciliation` 32 · `_reconcile_pending_effect` 124 | 效果未知对账 |
| `agent/_recovery.py` | `_gate_crash_recovery` 83 · `_sync_durable_state` 64 | 崩溃恢复门禁 |
| `agent/_runtime_types.py` | `RunOutcome` 21 · `RuntimeState` 41 · `ApprovalGrant` 243 | 共享类型（拆出以避免循环导入） |

### 2.3 被调用的服务

| 文件 | 关键入口 | 子域 |
|---|---|---|
| `agent/planner.py` | `generate_plan` 70 · `plan_next_action` 85 · `replan` 116 | 调 VLM 产出计划与决策 |
| `agent/observer.py` | `observe` 37 | 截图 + 屏幕尺寸 + 焦点 + UI 树（带**总预算**） |
| `agent/executor.py` | `execute` 60 | Action → 设备端口调用 |
| `agent/verifier.py` | `verify_action` 83 · `_no_independent_evidence` 250 | 多级验证，产出三层事实 |
| `agent/evidence.py` | `EvidenceLevel` 33 · `screen_delta` 149 · `success_evidence` 218 | 本地证据链 |
| `agent/reconciliation.py` | `reconcile` 71 · `needs_reconciliation` 61 | 对账判定 |
| `agent/risk_gate.py` | `ActionRiskGate.assess` 125 | 危险动作判定（**唯一入口**） |
| `agent/goal_policy.py` | `resolve_mode` | 任务画像 → 完成严格度 |
| `agent/goal_oracle.py` | `achieved_by_plan_alone` | 目标谓词 |
| `agent/goal_verifier.py` | `verify_goal` 117 | 完成申请的独立裁定 |
| `agent/execution/service.py` | `ExecutionService` 99 · `start` 112 · `settle` 228 | 动作级执行记录的唯一写入口 |

---

## 3. 上游：任务怎么被叫进 `runtime.run()`

### 3.1 调用链

```
POST /tasks                      api/server.py
 → TaskManager.create            task_manager.py:125
    ├ Task(...) + apply_event(CREATED)
    ├ store.save(task)
    └ Scheduler.submit           scheduler.py:469
       ├ _lane_for()             按权限解析/绑定量车道
       ├ apply_event(SUBMITTED) → QUEUED
       ├ lane.push_ready()
       ├ _persist_or_degrade()   先落盘
       └ notify_all() → _maybe_preempt()
 → worker 线程 _lane_loop         scheduler.py:1019
 → _pop_next(lane)                scheduler.py:978
 → _execute(task, lane)           scheduler.py:1061
    ├ lease_store.claim(task.id)      跨进程唯一执行者
    ├ lane.session.acquire(task.id)   进程内设备锁
    ├ apply_event(DISPATCHED) → RUNNING
    └ runtime.run(task)               _execution.py:78
 → _handle_outcome(...)           scheduler.py:1126
```

### 3.2 为什么是两把锁

`_execute`（`scheduler.py:1061`）里的顺序不能变：

1. **`lease_store.claim`** —— 跨进程租约（SQLite）。同一台设备在同一时刻只能有一个 worker 在跑，
   token 不匹配必须立刻停，不能「猜自己还是持有者」。
2. **`lane.session.acquire`** —— 进程内设备会话锁。同一个进程里多个 worker 线程也要排他。

拿到两把锁之后才 `DISPATCHED`，**最后**才 `runtime.run(task)`。所以：

> **Runtime 拿到的任务，设备已经被锁住了。**
> 测试里直接调 `runtime.run(task)` 而不先 `session.acquire()`，会以 `DeviceBusyError` 收场，
> 用例看起来「通过」却一次设备都没碰。判据是 `session.controller.events` 非空。

### 3.3 Classifier：关系判定与它对调度的影响

输入 `classify(instruction, *, current, candidates)`（`classifier.py:194`），输出 `TaskRelationResult`
（`models/task_relation.py:49`）。五类关系与置信门槛：

| 关系 | 门槛 | 处置 |
|---|---|---|
| `UNRELATED` | 0.50 | 建新任务 |
| `SUBTASK` | 0.65 | 并入当前计划（插在下一待执行步前） |
| `SUPER_TASK` | 0.85 | 改写目标 + 触发抢占 |
| `DUPLICATE` | 0.90 | 不新建 |
| `INTERRUPT` | 0.80 | 建新任务，优先级默认 HIGH，再触发抢占 |

**为什么每类门槛不一样**（`RELATION_THRESHOLDS`，`models/task_relation.py:34`）：
不同关系判错的代价差了几个数量级，所以**代价越高、要求越严**。

| 关系 | 判错的后果 | 可恢复吗 |
|---|---|---|
| `DUPLICATE` | 用户的新指令被**直接吞掉**，什么都不会发生 | 最难被发现 |
| `SUPER_TASK` | 正在执行的任务**目标被改写** | 不可恢复 |
| `INTERRUPT` | 抢占并挂起别的任务 | 可恢复（有 checkpoint） |
| `SUBTASK` | 只是往计划里多插一步 | 代价最小 |

`INTERRUPT` 与 `SUPER_TASK` 还会额外要求调用方**显式放行**（`RELATIONS_NEEDING_CONFIRMATION`，
`models/task_relation.py:44`）——它们会改写或打断已在跑的任务。

**结构化优于文本**：即便判成 `DUPLICATE`，只要**实体冲突**（时间/对象/金额对不上）就否决——
「明天提醒我开会」和「今天提醒我开会」字面 0.9 相似，但不是同一件事。

### 3.4 调度循环与抢占

每设备一个线程一条车道（`_DeviceLane`，`scheduler.py:142`）。`_lane_loop`（1019）循环
`_pop_next` → `_execute` → `_handle_outcome`。

- **取谁上**（`_pop_next` 978）：挂起队列优先，但**不能压过抢占者**——比较 `suspended[0]` 与
  `ready[0]` 的优先级再决定。
- **抢占**（`_maybe_preempt_by_id` 790）：先判**实际执行平面**是否占用用户屏幕（`_occupies_user_display`
  80，HYBRID 在影子不可用时算作占前台），再判 `running.interruptible` 与优先级，最后
  `request_preempt`。**协作式，没有硬杀线程**——强杀不可能安全地停在「动作执行到一半」。
- **设备不可用**：抛 `DeviceUnavailableError` → 进 `_device_unavailable`，由
  `on_device_available`（911）恢复。

---

## 4. 进门三道关

`run()`（`_execution.py:78`）在进入循环前做三件事，都在循环**外**，因为只该发生一次。

### 4.1 已取消就跳过（80–87）

已 `CANCELLED` / `CANCEL_REQUESTED` 的任务直接返回 `RunOutcome.CANCELLED`。
不用 `mark(RUNNING)` 把状态改回去——那会造出「刚取消又变成运行中」的假象。

### 4.2 崩溃恢复门禁（`_recovery.py:83`）

任务停在 `RUNNING` 被掐断，说明上次执行是**半途消失**的：

- **有恢复点** → 放行，交给既有的对账路径（它用 `validate` + `needs_reconciliation` 比对
  `action_effect` / `attempt_id`）。
- **没有恢复点** → 既不知道动作发没发出去，也没有 attempt 记录可比对。此时重新规划再点一次，
  可能就是把同一条消息发第二遍、同一个订单下第二次。**唯一诚实的做法是停下来交给人。**

这是整条链路里**唯一一条会把任务交给人的路**（半途消失的副作用不能靠重跑猜），
所以它单独一个文件讲清楚。返回 `RunOutcome.AWAITING_CONFIRMATION`，并写 `task.recovery_note`，
批准继续后这个「可能有未决副作用」不会被丢掉。

### 4.3 用户挂起恢复（`_resume_after_user_pause` 152）

上次是「用户在用手机」而让位的，恢复必须**三条同时满足**：

1. 用户停手够久（默认 `DEFAULT_USER_IDLE_SECONDS = 2.0`，`runtime.py:128`）；
2. **重新观察过一次**（`_observe` 拿到新 Observation）；
3. 页面指纹与暂停前**一致**（`_page_fingerprint_matches` 416）。

第 3 条是唯一的安全支柱：用户在场期间可能自己点进了别的 App，此时「按老坐标继续」就是拿 A 屏的
坐标点 B 屏。**恢复不靠「用户停手了」，靠「停手了 且 那一屏还是我认识的那一屏」。**

返回值只有两态——「可以进循环」或「继续挂着」，**不接受「可以按原计划继续」这个第三态**。

> 与抢占恢复的区别：抢占恢复不需要这套。另一个 Agent 换了页面属于「设备状态变了」，
> 由版本围栏 + TOCTOU 门禁处置就够；而**人**的操作没有任务边界，只能靠「停手 N 秒 + 页面没变」去猜。

---

## 5. 主循环

### 5.1 每轮的固定顺序（`_run_loop` 340）

顺序本身是设计，不是随手排的：

| # | 步骤 | 行号 | 为什么在这个位置 |
|---|---|---|---|
| 1 | `_sync_durable_state` | 352 | 该跨重启记住的东西先同步到 Task |
| 2 | `refresh_user_context` | 357 | **唯一碰设备的探测**，必须在安全点前 |
| 3 | `safe_point` | 362 | 纯判定、不碰设备，才好被用例直接调 |
| 4 | 版本围栏 | 367 | 目标/计划被改写 → 旧决策上下文全部作废 |
| 5 | `_observe` | 382 | 拿这一屏 |
| 6 | 对账 | 397 | 上一个动作效果未知，就地拿新观察对掉 |
| 7 | `_prepare` | 401 | 首次进入：生成计划 |
| 8 | `_think` | 409 | 定下一步 |
| 9 | 完成申请分流 | 421 | 模型说 done 只是**申请**，走裁定 |
| 10 | 死循环检测 | 428 | 同一动作重复 → 换策略，不是继续重试 |
| 11 | 危险动作门禁 | 455 | HITL |
| 12 | TOCTOU 门禁 | 539 | 页面还是决策时那一屏吗 |
| 13 | **Act** | 563–604 | 先记录、再动手 |
| 14 | **Verify** | 606–624 | 多级证据 |
| 15 | 按验证结论分流 | 625–686 | DONE / OK / ERROR 三路 |
| 16 | Checkpoint | 653、680 | 落盘 |

### 5.2 安全点（`runtime.py:197`）

**唯一的中断判定入口**。收敛到一处的原因有两个：① 这些条件互相不独立（用户在用手机 ⇒ 不该执行；
被抢占 ⇒ 无论用户在不在都该让位），散着写时顺序靠读代码的人自己推；② 收成纯函数式入口后，
用例可以直接构造 `Task` + `RuntimeState` + `DeviceSession` 调用它，不必跑完整个 loop。

判定顺序与理由：

| 顺序 | 条件 | 返回 `kind` | 为什么在这个位置 |
|---|---|---|---|
| 1 | 终态 | `terminal` | 最高优先，决不能再产生副作用 |
| 2 | 取消请求 | `cancel` | 用户明确要求停，优先于其它让位理由 |
| 3 | 用户暂停 / 抢占让位 | `paused` / `preemption` | 外部要求停；抢占是**硬**让位 |
| 4 | 用户在场 | `user_active` / `user_pause_limit` | **软**让位，可以等几秒再看看 |
| 5 | 预算（观察次数 / 模型调用） | `budget_*` | 自己跑不动了 |

返回 `SafePoint` 而不是 `bool`：调用方需要知道**为什么**停下（好发对事件、落对状态、选对
`RunOutcome`）。返回 `bool` 时调用方只能再猜一次原因，而那正是「Runtime 说暂停了、Scheduler 说还在跑」
这类语义冲突的来源。

### 5.3 用户在场判定里的不对称（`_user_presence_verdict` 275）

只对**会占用用户屏幕**的任务生效：`shadow` 平面上的任务本来就跑在影子屏幕上，用户在用 Display 0
与它无关。这里读的必须是 **resolved** 平面而不是 `task.execution_mode` 的声明值。

**探测失败（`confirmed=False`）时不暂停。** 这与 `device/user_activity.py` 在动作层面的保守方向
**相反**，理由是同一条原则在两地的代价不对称：

- **动作层**：宁可说「用户在操作」——代价是任务慢一点。
- **任务层**（这里）：宁可继续跑——因为「本端不支持探测」是**恒定条件**，当成「用户在场」会让任务
  被永久暂停，用户还无法修复。那比「打扰用户」更糟。

同时必须有**次数上界**（`MAX_USER_PAUSES = 5`，`runtime.py:126`）：用户长时间连续操作时，任务会陷入
「暂停→恢复→又暂停」，表现为「任务永远卡在 running、什么也不做」。到上界就明确失败并告知
「可稍后重试或改用 shadow 模式」，把决定权交还给人——这比无限期等下去诚实。

### 5.4 版本围栏（367）

`task.version != state.run_version` 说明目标或计划被外部改写过（`inject` / re-plan / 人工干预）。
此时必须 `task.plan = []`、`prepared = False`、`checkpoint = None`，**作废旧决策上下文**——
否则会出现「新目标 + 旧 observation + 旧 trajectory」混合规划。

### 5.5 Observe（`_observe` 770 / `observer.py:37`）

一次采集 = 截图 + 屏幕尺寸 + 焦点 + UI 树。走 `DeviceController` **端口**，不认后端
（PC 后端是 adb，Android 后端是 Accessibility + MediaProjection）。

**为什么要有总预算**（`OBSERVE_BUDGET_SECONDS` 默认 12s）：ADB 后端实际是 6 条命令，每条自己有超时，
逐条各自卡住就是累加——而采集是循环里最容易卡住的一步，它直接决定高优先级任务要等多久才能拿到设备。
给了总预算后，每条操作的超时取 `min(自己的超时, 剩余预算)`，预算用完直接退出。

观察失败按**瞬时错误**处理（`ErrorClass.TRANSIENT`），走 `_settle_failure`，不是任务失败。

### 5.6 对账（`_reconcile_pending_effect` 124 / `reconciliation.py:71`）

上一个动作发出去了但效果未知（`pending_effect_reconcile`），现在手上正好有新观察，就地把它对掉。
四条路：

| 结论 | 条件 |
|---|---|
| `CONTINUE` | 页面已跳转 / 目标元素变化 / 结构指纹变化 |
| `RETRY` | 页面无变化 → 重做（**仅限重做等价的动作**） |
| `REPLAN` | 页面切到别的 App（恢复路径）/ 缺少基线 |
| `ASK_HUMAN` | 判断不了 |

两条硬约束：

- **危险动作永不 `RETRY`**。发消息、提交表单、支付、删除这类动作，页面没变**并不代表没生效**——
  重做就是第二条消息、第二笔订单。此时唯一正确的答案是问人。
- **危险动作不看「页面变没变」**。点击付款 → 打开支付 Activity 只能说明**进入了支付流程**，
  不等于付款成功。这类动作只认页面上出现「支付成功 / 已发送」这种**终态文案**
  （`evidence.success_evidence` 218）。

`online` 参数区分两种场景：执行途中（`True`）「页面切到别的 App」是动作生效的强证据；
恢复路径（`False`）不能这么推断——进程死了多久、期间发生了什么都不知道。

**对账有上界**（`MAX_EFFECT_RECONCILIATIONS = 2`）：对账会重做动作，无限对账等于无限重做。

### 5.7 Think 的三条通道（`_think` 852）

顺序不能换：

1. **对账指定的强制动作**（`_forced_decision` 1246）——已确认上次没生效，直接重做，**不惊动模型**。
   再让模型自由发挥就白对了。
2. **Re-plan**（`_replan` 902）——本轮带着 `pending_replan_reason`（完成申请被驳回 / 死循环 / 崩溃恢复
   遗留），必须换策略。`ReplanContext`（`planner.py:43`）是**结构化**输入，关键点是让模型明白
   「刚才是**这种执行方式**失败了」，而不是「任务失败了」——少了这层区分，模型往往原样重发同一个动作。
3. **常规决策**（`_decide` 876）——VLM 依据截图 + UI 树 + 历史 + 计划状态 + 任务级上下文决定下一步。

`planner` 只回答「做什么」，**不产坐标**。坐标由 `executor` 交给 `vision/grounding` 解析。

决策失败（`decision is None`）按 `ErrorClass.PARSE_ERROR` 结算，不是任务失败。

### 5.8 死循环检测（428）

最近 4 个动作（`LOOP_WINDOW`）里同一个指纹出现 3 次（`LOOP_REPEAT_THRESHOLD`）即判定死循环。
处置是**换策略**（走 Re-plan）而不是继续重试。人工否决过的动作指纹（`denied_fingerprints`）
命中时同理——否则任务会在「请求确认 → 被否决 → 再请求确认」之间空转。

### 5.9 危险动作门禁（455，HITL）

风险判定**必须带上下文**：模型完全可以把一次「点击立即购买按钮」描述成「点击红色按钮」，
只有查 UI 树才知道那个控件到底叫什么。所以是
`ActionRiskGate.assess(action, context=RiskContext.from_observation(...))`。

- `RISK_ASSESSED` 是**安全关键事件**：写不进 durable store 就不能继续往下走。继续意味着危险动作
  可能在没有判定记录的情况下被放行，事后审计无法回答「为什么让它过了」。
- 需要确认时：先看 `state.approval`（放行凭据）能否逐项匹配。匹配成功**即消费**，绝不复用。
- 凭据绑定五个维度：`task_id` + 动作指纹 + **页面绑定指纹** + `task_version` / `plan_version` +
  `attempt_seq`（`_runtime_types.py:243` 的 `ApprovalGrant.matches`）。任一项变过就作废、重新请求确认。

**为什么必须绑页面**：`tap((500, 800))` 在微信、淘宝、设置里是三个完全不同的动作。
「批准微信里的发送」不能在别处有同坐标按钮时被误用。

### 5.10 TOCTOU 门禁（`_toctou_guard` 792）

走到这里决策已完成，但动作还没发出去。这中间的窗口里，用户可能自己点了按钮、通知栏可能弹出来、
App 可能异步刷新——**那些变化不会推进 `session.generation`**，只靠代次是看不见的。

判据是 `state.decision_epoch`（决策当时那一屏的快照）与当前页面比对。判过期就
`OBSERVATION_STALE`、丢弃本次决策、重新观察。

**连续计数是必要的**（`MAX_STALE_OBSERVATIONS = 3`，`_execution.py:71`）：判 stale 就 `continue`，
若 stale 判定本身有抖动，不加计数就是死循环。连续多次稳不下来说明页面正在被持续改动，
按瞬时故障处理（计入熔断），硬等没有意义。

### 5.11 Act 的纪律：先记录，再动手（563–604）

顺序是**刻意的**：

```
state.attempt_seq += 1                     570
state.last_action_effect = DISPATCHED      572
_begin_execution(...)                      590   建执行记录 → 与「推进到 DISPATCHED」同事务
_record_dispatch(...)                      596   写 ACTION_DISPATCHED
executor.execute(...)                      599   真正动手
```

- `_begin_execution`（938）失败 → `_degrade`，**不执行**。写不进 durable store 就不动手。
- `_record_dispatch`（967）失败 → 同样 `_degrade`，停止副作用。
- 动作步数超上界（`task.budget.max_action_steps`）→ 先存 checkpoint 再 `_fail`。

`executor.execute`（`executor.py:60`）**永不抛异常**：任何失败都收敛成
`{"ok": False, "error": ..., "error_class": ...}`。否则异常会穿透主循环，让整个任务以 HTTP 500 中断。
`error_class` 是结构化的，因为下游（verifier → retry 策略）宁可读这个字段，也不要从一句中文里猜它属于哪类。

### 5.12 Verify 与三层事实（606–624）

`verify_action`（`verifier.py:83`）把结论拆成**事实分层**与**处置建议**两组——分开之后，
「命令没发出去」和「发出去了但没效果」才不会共用一句「失败」：

- 事实：`dispatch`（发出没）· `effect`（有没效果）· `goal`（目标达成没）
- 建议：`outcome` / `should_retry` / `should_replan`

证据分层（`evidence.py:33`）：

| 层 | 信号 | 强度 |
|---|---|---|
| L1 设备层 | 命令送达并执行成功 | 最弱，但对 BACK/HOME/WAIT 已是能拿到的最强证据 |
| L2 导航层 | package / activity 变了 | 很硬，几乎不可能误判 |
| L3 结构层 | UI 结构指纹变了（含 class / content-desc / resource-id） | 一般 |
| L4 目标层 | 被操作元素消失 / 文本变了 | 最贴近「这个动作有没有用」 |
| L5 成功标志 | 页面文本出现「支付成功 / 已发送」 | 专门回答「不可撤销动作成了没有」 |
| L5 语义层 | VLM 判定 | 贵、慢、会编理由，只做最后一层 |
| L6 目标层 | GoalVerifier 的裁定 | 任务级 |

**VLM 只是最后一层**，不是唯一判据。原实现只比可点击 label 集合，三个反例都很真实：
toast 弹出后集合没变（漏判成功）、页面内部数据变了集合也没变（漏判变化）、
无关 Dialog 弹出却被判成 changed（误判成功）。

**最危险的地方在「没有独立正向证据」时**（`_no_independent_evidence` 250），三条路：

- **危险动作 → 绝不放过**：记 `EFFECT_UNKNOWN`，`should_retry=False`，交人工。
  以前 VLM 返回未知值会落到默认分支 `outcome=OK`，于是一个「确认付款」可能就这么过了。
- **会改页面的动作 → 效果未知**：UI 树完全一致 + 无 VLM 判定 = 疑似无效操作，可重试。
- **本来就不保证页面变化的动作**（BACK / HOME / WAIT）→ 按设备层 ACK 收尾。
  这不是「默认成功」，而是「设备层确认已经是能拿到的最强证据」。

执行记录在验证后落终态（`_settle_execution` 1035），三档：
`FAILED / SUCCEEDED / UNVERIFIED`。放在**所有分支之外**统一落，因为动作已经发出去了，
无论走哪条分支「这次执行的结果是什么」都必须记下来——否则它会一直停在 `RUNNING`，
而 `RUNNING` 在崩溃恢复里等于「可能已经点了但不知道成没成」。

### 5.13 Checkpoint（`_save_checkpoint` 612）

存的是**解析后**的执行平面（`state.effective_execution_mode`）而不是任务声明——
两者在 hybrid 降级时会分叉，而恢复时要知道的是真相。同时把「用户**要求**的平面」
（`requested_execution_mode`）也留下，否则「降级过」这个事实在恢复后就永久消失了。

写入顺序：**先写恢复点、后写任务指针**。所以任务**永远**不会指向一个不存在或没写完的恢复点——
这是崩溃恢复最怕的那个方向。代价是反方向仍有窗口：进程在写指针前崩溃会留下**孤儿恢复点**，
它是无害的（没有任何代码路径会读非指针指向的恢复点），由启动时的 prune 清掉。

【诚实说明】这是两个文件、两次写入，**没有跨文件事务**。要真正的事务就得上 SQLite
的 `BEGIN; INSERT checkpoint; UPDATE task; COMMIT;`。

---

## 6. 完成判定链

模型返回 `done` 只是**一次申请**（`DONE_REQUEST`），不是完成。原实现的 `DONE` 是一个过于强的模型权限：
模型一旦返回 `{"action_type": "done"}`，整个任务直接完成，没有独立验证。现实里的失败长这样——
任务「在淘宝搜索 iPhone 17 Pro Max 并进入商品详情页」，模型看到搜索结果页就说 done。

三件套分工：

```
goal_policy.resolve_mode(instruction, context)          goal_policy.py:152  → 任务画像 + 严格度
goal_oracle.achieved_by_plan_alone(profile)             goal_oracle.py:98  → 目标谓词
goal_verifier.verify_goal(...)                          goal_verifier.py:117 → 最终裁定
```

严格度：

| 模式 | 行为 |
|---|---|
| `auto`（默认） | 按任务画像：纯查询 → advisory；导航 / 副作用 / 改设置 → strict |
| `advisory` | 只在拿到**反证**时驳回 |
| `strict` | 计划里还有未完成步骤就驳回 |
| `off` | 不检查 |

裁定三值（`GoalVerdict`）：

| 值 | 含义 | 处置 |
|---|---|---|
| `CONFIRMED` | 有独立证据支持 | 落 DONE |
| `REJECTED` | 拿到**反证**（声称与可核验事实矛盾） | 任务必须继续，走 Re-plan |
| `UNCERTAIN` | 证据不足，既不能确认也不能否证 | **默认放行**，但如实留痕 |

> **证据不足 ≠ 有反证。** 把没有证据的完成一律拦下来会让 Agent 变成不能用；
> 但反过来（有反证还放行）是不可接受的。

独立证据按强度排序：**L6-c 可核验声明**（模型给出 `package` / `activity` / `text` / `resource_id`，
逐条与真实页面比对——这是唯一的强判定）> **L6-a 计划跑完 + L6-b 页面推进过**（strict 画像下两者缺一不可）
> 计划跑完（仅限纯查询画像）。

关键修补（V3 M1）：**「计划跑完」只是执行系统的内部状态，不是真实世界目标状态。**
计划是模型自己拆的——它漏了「进入详情页」这一步，计划照样能跑完，但目标没达成。

`GOAL_CONFIRMED` 是安全关键事件：**写不进 durable store 就不能真的把任务落成 DONE**，
否则会出现「任务显示完成，但审计链里没有完成认定」。

连续被驳回 `MAX_GOAL_REJECTIONS = 2` 次 → 转人工（`AWAITING_CONFIRMATION`），
避免「模型坚持说完成、验证器坚持驳回」的昂贵空转。

---

## 7. 出口：`RunOutcome` 与调度器处置

`runtime.run()` 返回的不是「任务成功」，而是一个**让位/结束的理由**。
`_handle_outcome`（`scheduler.py:1126`）的映射：

| RunOutcome | 后续 | 入队吗 |
|---|---|---|
| `done` | `COMPLETED` → `DONE` | — |
| `cancelled` | `CANCELLED` | — |
| `suspended` | `PAUSED_BY_PREEMPTION` → `PAUSED` | ✅ 推回挂起队列等设备 |
| `suspended_by_user` | `PAUSED(user)` | ❌ 等用户停手或显式 resume |
| `awaiting_confirmation` | `AWAITING_CONFIRMATION` → `WAITING` | ❌ 等人工（**入队会死循环**） |
| `failed` | `FAILED` | — |

终态统一「**先落盘再 `_mark_completed` + `notify_all`**」，唤醒 `/wait`。
任务已是终态则不再改写（1134–1149）——终态不可逆。

异常路径由 `_lane_loop` 分类兜底：`PersistenceError → DEGRADED`、
`DeviceUnavailableError → DEVICE_LOST`、其它 → `FAILED`。

对应关系是**一一对应**的，这是刻意设计：`suspended` 与 `suspended_by_user` 分开，
因为恢复方式不同（前者等设备可用，后者等用户停手），审计上也是两回事——
合成一个值，日志里就分不出「任务是被别的 Agent 挤掉的」还是「用户拿起手机了」。

---

## 8. 贯穿全链路的五条原则

### 8.1 「读不到」≠「空」

证据缺口是一等事实，不是默认值。

| 场景 | 处置 |
|---|---|
| 页面身份读不到 | 不判「一致」，**不恢复**（代价=多等一轮） |
| UI 树不可比 | `ScreenDelta.known = False`，不判「没变化」 |
| 用户活动探测失败 | 任务层**继续跑**（代价=可能打扰用户，但停了就永久停） |
| 完成申请的声明无法核验 | `UNCERTAIN` 而非 `REJECTED` |

### 8.2 保守方向按**代价**选，不是按「保守」选

同一个「不知道」，在不同位置的正确方向可能相反。判断标准是：**哪一种错更不可挽回。**

### 8.3 先记录，再产生副作用

安全关键事件（`RISK_ASSESSED` / `ACTION_DISPATCHED` / `CONFIRMED` / `GOAL_CONFIRMED`）
写不进 durable store 就**不放行**。落点是 `_emit_critical_or`（返回失败原因）与 `_degrade`。

反过来说：**注释强度不能超过实现**。文档里说「有跨文件事务」而实现是两个文件两次写入，
是比没有注释更糟的事——所以第 5.13 节把这件事写清楚了。

### 8.4 「声明」不等于「事实」

这条在三个地方重复出现，每次的修法都是**把用户/模型的声明与系统观察到的事实分开存**：

| 声明 | 事实 |
|---|---|
| `task.execution_mode` | `state.resolved_execution_mode` |
| 模型说 `done` | `GoalVerifier` 的独立裁定 |
| 模型给的 `risk` | `ActionRiskGate` 结合 UI 树的判定 |

### 8.5 一切等待都要有上界

无限等待表现为「任务卡在 running」，而用户看不出为什么。已设的上界：

| 上界 | 值 | 触发后的行为 |
|---|---|---|
| `MAX_USER_PAUSES` | 5 | 明确失败 + 告知可改用 shadow 模式 |
| `MAX_STALE_OBSERVATIONS` | 3 | 按瞬时故障结算 |
| `MAX_EFFECT_RECONCILIATIONS` | 2 | 转人工 |
| `MAX_GOAL_REJECTIONS` | 2 | 转人工裁定 |
| `budget.max_observations` / `max_model_calls` / `max_action_steps` | 任务级 | 失败 |
| `OBSERVE_BUDGET_SECONDS` | 12 | 采集中断退出 |

---

## 9. 已知边界与取舍

以下是**有意为之**的选择，不是待修的 bug。改之前先读对应注释。

| 事项 | 现状 | 理由 / 触发条件 |
|---|---|---|
| 执行记录 `principal` 为空 | Runtime 不知道谁触发的任务 | 宁可缺一项也不编假值。要补得先让 principal 落到 `Task` 上 |
| 危险动作 `RISK_ASSESSED` 不带 `execution_id` | 那一刻执行记录还不存在 | 记录建在「确定真的要发出这个动作」之后。提前建会让 HITL 中的动作留下记录，每次重启被收成 FAILED，淹没真正的崩溃遗留 |
| `_persist` 不带 `expected_revision` | 刻意 | 单进程内 Runtime / Scheduler / TaskManager 持有同一批内存 `Task`，内存通常比磁盘新；加 CAS 会把「调度器先改内存、稍后统一落盘」这种**正常**情形误判成冲突 |
| `DeviceError` 不声明 `error_class` | 刻意 | 笼统值会屏蔽 `models/retry` 的文本规则 |
| 不把 Runtime 改成 `runtime/` 包 | 刻意 | 收益是目录好看，代价是全仓库 import 与 `git blame` 断裂。触发条件：`runtime.py` 涨到 600 行以上 |
| 强杀线程式取消 | 不做 | 不可能安全地停在「动作执行到一半」，所以 `CANCEL_REQUESTED` 与 `CANCELLED` 是两态 |

**未实现（V5 预留）**：

1. 真正的 Shadow Display —— 当前 Android 侧 `ShadowDisplayManager` 如实声明做不到，
   影子可用性恒为 `False`；只有 `HYBRID` 会在影子不可用时回落到前台。
2. `ExecutionRequirement` 接进 `ActionRiskGate` —— 需要用户在场的动作目前不由风险门禁处置。
3. 调度器主动扫描恢复 `PAUSED(user)` —— 当前依赖显式 resume。
4. single-writer（V4 Commit 5）—— 单进程硬前提，>1 worker 拒启动。
5. 启动恢复无周期性巡检；Agent 路径执行记录 `principal` 为空。

**定位提醒**：不要把上面这些读成「差一点就完成」。V5 做的是**执行平面抽象**，
真正让第三方 App 跑在非 Display 0 上（V6）才是独立的 Shadow Execution——
难点是真机上如何让第三方 App 不占 Display 0。

---

## 10. 代码索引：想改什么，去哪

| 我想改… | 去 |
|---|---|
| 任务状态、状态迁移规则 | `models/task.py`（`TaskStatus` 20 / `TaskEvent` 41 / `ALLOWED_TRANSITIONS`） |
| 单个任务的执行逻辑 | `agent/_execution.py` |
| 什么情况下该停 | `agent/runtime.py:197` `safe_point` |
| 危险动作判定 | `agent/risk_gate.py:125` `ActionRiskGate.assess` |
| 「这步成功了没」 | `agent/verifier.py` + `agent/evidence.py` |
| 「任务算不算完成」 | `agent/goal_verifier.py` + `goal_policy.py` + `goal_oracle.py` |
| 效果未知怎么对账 | `agent/reconciliation.py` |
| 下一步做什么（prompt） | `agent/planner.py` + `vision/vlm.py` |
| Action → 设备命令 | `agent/executor.py` + `device/controller.py` 端口 |
| 排队 / 抢占 / 设备分配 | `agent/scheduler.py` |
| 新指令怎么并进现有任务 | `agent/task_manager.py:359` `inject` + `classifier.py` |
| HTTP 契约 | `api/server.py` |
| 事件种类与安全分级 | `storage/event_log.py`（`SAFETY_CRITICAL_KINDS` 110） |

---

## 附录 A：任务状态机

`TaskStatus`（`models/task.py:20`）：
`CREATED / QUEUED / RUNNING / PAUSED / WAITING / DEGRADED / DEVICE_UNAVAILABLE / CANCEL_REQUESTED / DONE / FAILED / CANCELLED`

终态 = `DONE / FAILED / CANCELLED / DEGRADED`。**终态不可迁出**，越界抛 `InvalidTransitionError`；
终态自迁移合法（幂等）。

关键迁移路径：

```
CREATED --SUBMITTED--> QUEUED --DISPATCHED--> RUNNING
RUNNING --COMPLETED--> DONE
RUNNING --FAILED--> FAILED
RUNNING --CANCEL_REQUESTED--> CANCEL_REQUESTED --CANCELLED--> CANCELLED
RUNNING --PAUSED_BY_PREEMPTION--> PAUSED --RESUMED--> QUEUED
RUNNING --PAUSED_BY_USER--> PAUSED(user) --RESUMED--> QUEUED
RUNNING --AWAITING_CONFIRMATION--> WAITING --RESUMED--> QUEUED
RUNNING --DEVICE_LOST--> DEVICE_UNAVAILABLE
任意 --DEGRADED--> DEGRADED
```

两条容易踩的：

- `RUNNING` **只能**由 worker 经 `_handle_outcome` 落终态；`TaskManager.complete` / `fail`
  会显式**拒绝** `RUNNING` 状态的任务（`task_manager.py:240`、254）。否则等于「DONE 还能继续跑」。
- `CANCEL_REQUESTED` **不是终态**。它表达「请求已下达」，真正的停止发生在 Runtime 的下一个安全点。
  点击「发送」之后立刻取消时，消息其实已经发出去了——把两者合成一个状态会让 `CANCELLED`
  被误读成「副作用已停止」。

## 附录 B：事件类型

`storage/event_log.py:48` 起：`CREATED` `QUEUED` `STARTED` `ACTION_DISPATCHED` `ACTION_VERIFIED`
`CHECKPOINT_SAVED` `RECONCILED` `WAITING` `CONFIRMED` `PREEMPT_REQUESTED` `SUSPENDED` `RESUMED`
`DONE` `FAILED` `CANCELLED` `RECOVERED` `TASK_CORRUPTED` `GOAL_REQUESTED` `GOAL_CONFIRMED`
`GOAL_REJECTED` `EFFECT_UNKNOWN` `EXECUTION_RECOVERED` `RISK_ASSESSED` `OBSERVATION_STALE`

安全关键子集由 `SAFETY_CRITICAL_KINDS`（110）+ `is_safety_critical`（120）决定，
`EventLog.emit` 是**唯一写入口**，按 kind 自动分级：安全关键事件写盘失败会**抛出**
`PersistenceError`，调用方必须决定「记不下这条，副作用还能不能继续」。

**分级的地方只有这一处**——散落各处会导致「有的地方判危险、有的地方不判」。
