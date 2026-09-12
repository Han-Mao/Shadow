# Bluewhale Shadow Phone — V2

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
│   └── scheduler.py        # 优先级队列 / 抢占 / 恢复
├── models/
│   ├── task.py             # Task + 8 态状态机 + 优先级
│   ├── task_step.py        # TaskStep：计划是可追踪的状态机，不是字符串列表
│   ├── task_relation.py    # TaskRelation：5 种任务关系
│   ├── checkpoint.py       # Checkpoint：恢复所需的最小状态
│   ├── action.py           # Action + 风险等级 + 指纹
│   └── state.py            # Observation / StepOutcome
├── storage/                # TaskStore / CheckpointStore / TrajectoryStore
├── device/
│   ├── adb.py screenshot.py accessibility.py emulator.py
│   ├── session.py          # DeviceSession：设备所有权与抢占交接
│   └── input.py            # InputProvider：ASCII 与中文输入通道
├── vision/                 # vlm / grounding / parser
├── api/server.py           # FastAPI
├── scripts/demo_preemption.py   # 抢占恢复演示（离线可跑）
└── tests/                  # 141 个离线用例
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
- `Checkpoint` 保存「恢复所需的最小状态」（当前页、package/activity、最近轨迹）
- 每个执行完的步骤都进 `TrajectoryStore`，供下一步决策取上下文

### 4. 执行安全

- **风险分级**：`safe / caution / dangerous`，命中「发送/支付/删除/下单」等关键词升级为危险动作
- **HITL 门禁**：危险动作挂起任务等人工确认，批准才放行；被否决的动作进黑名单，
  下次再出现直接换策略，不会陷入「请求确认 → 否决 → 再请求」的空转
- **死循环检测**：连续 3 次做出语义相同的动作（坐标容差 24px）即强制换策略，
  而不是继续 retry 同一个动作

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
| `GET` | `/tasks/{id}/history` | 执行轨迹 |
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

**141 个用例，全部离线**：不需要 adb、模拟器或 API Key。

| 文件 | 覆盖 |
|---|---|
| `test_models.py` | 任务状态机、步骤依赖、动作风险与指纹、Checkpoint |
| `test_device.py` | ADB 封装、输入通道（含中文）、设备会话所有权与并发 |
| `test_vision.py` | UI 树容错、坐标落点、VLM 重试、prompt 构造 |
| `test_classifier.py` | 三层关系判定与相似度否决 |
| `test_scheduler.py` | 优先级、暂停/取消、**抢占与恢复**、设备占用 |
| `test_runtime.py` | 闭环执行、异常收敛、死循环、HITL、Checkpoint 恢复 |
| `test_api.py` | HTTP 契约、状态码语义、错误脱敏 |

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
