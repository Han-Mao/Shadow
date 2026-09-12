# Bluewhale Shadow Phone — MVP

> 依据：《蓝色鲸鱼 Agent · 方案三 Shadow Phone 开发文档》§09 代码结构、§10 路线图
> 状态：M1/M2/M3 已实现，支持 Observe → Think → Act → Verify 闭环。

## 分层结构

```
├── agent/                  # Agent 决策层
│   ├── planner.py          # 任务 → 步骤计划，失败时 Re-plan
│   ├── observer.py         # 采集 Observation（截图 + UI 树 + 上下文）
│   ├── executor.py         # Action JSON → DeviceController 调用
│   ├── verifier.py         # 执行后重新截图，由 VLM 判断页面是否进入预期状态
│   ├── memory.py           # 历史轨迹与上下文
│   └── loop.py             # Observe → Think → Act → Verify 主循环
├── vision/                 # 视觉感知层
│   ├── vlm.py              # VLM 调用：页面理解、计划生成、决策、执行验证
│   ├── grounding.py        # 元素 → 屏幕坐标（bbox 中心 / UI 树 bounds）
│   └── parser.py           # Accessibility XML 解析
├── device/                 # 设备控制层
│   ├── adb.py              # DeviceController 的 ADB 实现
│   ├── screenshot.py       # 截图采集
│   ├── accessibility.py    # uiautomator dump 封装
│   └── emulator.py         # 设备发现与 serial 解析
├── api/
│   └── server.py           # FastAPI：任务与单步端点
└── models/                 # 数据模型
    ├── task.py
    ├── action.py
    └── state.py
```

## 职责边界

| 模块 | 职责 | 不做什么 |
|---|---|---|
| agent/loop.py | 驱动主循环，维护 AgentState，执行重试与熔断 | 不理解页面，不碰设备 |
| agent/planner.py | 语义级规划与 Re-plan | 不产出坐标 |
| vision/vlm.py | 页面理解、下一步决策 | 不输出 ADB 命令 |
| vision/grounding.py | 把元素目标落成屏幕坐标 | 不做决策 |
| device/adb.py | DeviceController 接口的 ADB 实现 | 不暴露给 Agent |
| api/server.py | 对外 HTTP 契约 | 不含业务逻辑 |

## 里程碑（§10）

- **M1 · 设备控制器** ✅ Python → ADB → Emulator，tap / type / back / screenshot
- **M2 · 视觉感知** ✅ 截图 → VLM → 元素 bbox / 描述 → 屏幕坐标（纯 VLM 路线 A）
- **M3 · Agent 闭环** ✅ 语义级 Plan + Observe/Think/Act/Verify + Accessibility 解析（路线 B）+ Re-plan + 重试熔断
- **M4 · 演示强化** ⏳ Shadow 环境预配置、中文输入、双任务剧本、双端协同雏形

## 运行

```powershell
# 方式一：只装依赖
pip install -r requirements.txt

# 方式二（推荐）：装成可编辑包，之后从任意目录都能 python -m api.server / pytest，无 cwd 依赖
pip install -e .

# 配置 VLM（OpenAI 兼容接口）
$env:VLM_BASE_URL="https://api.openai.com/v1"
$env:VLM_API_KEY="sk-..."
$env:VLM_MODEL="gpt-4o"

# 默认连接 emulator-5554，可用环境变量 ADB_SERIAL 覆盖（§6.4 多设备寻址）
python -m api.server    # 监听 127.0.0.1:8010
```

### 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `ADB_SERIAL` | 目标设备 serial | `emulator-5554` |
| `VLM_BASE_URL` | VLM 接口地址 | `https://api.openai.com/v1` |
| `VLM_API_KEY` | VLM API Key | 必填 |
| `VLM_MODEL` | VLM 模型名 | `gpt-4o` |
| `VLM_DETAIL_PLAN` / `_DECIDE` / `_VERIFY` | 各阶段图片精度 | `low` / `high` / `low` |
| `ARTIFACT_DIR` | 截图落盘目录 | `artifacts/shots` |
| `PORT` | API 端口 | `8010` |
| `SHADOW_DEBUG` | 置 1 时 500 响应回传异常摘要（默认脱敏） | 未设置 |

## 冒烟验证

### M1 单步控制

```powershell
curl http://127.0.0.1:8010/devices
curl -X POST http://127.0.0.1:8010/tap -H "Content-Type: application/json" -d '{"x":360,"y":600}'
curl -X POST http://127.0.0.1:8010/text -H "Content-Type: application/json" -d '{"value":"hello"}'
curl -X POST http://127.0.0.1:8010/screenshot
```

### M3 任务闭环

```powershell
# 默认后台执行：立即返回 task id，不占用请求线程等整个 loop 跑完
$task = curl -X POST http://127.0.0.1:8010/tasks -H "Content-Type: application/json" -d '{"instruction":"打开设置并开启飞行模式","max_steps":8}' | ConvertFrom-Json

# 轮询直到 task.status 变成 done / failed
curl http://127.0.0.1:8010/tasks/$($task.task.id)

# 演示时想要「一条命令拿到最终状态」，加 wait=true（同步等待，默认超时 120s）
curl -X POST http://127.0.0.1:8010/tasks -H "Content-Type: application/json" -d '{"instruction":"打开设置并开启飞行模式","max_steps":8,"wait":true}'

# 获取某一步截图（step 0 是计划阶段的首屏，可正常取到）
curl http://127.0.0.1:8010/tasks/$($task.task.id)/shots/1

# 单次观察
curl -X POST http://127.0.0.1:8010/observe

# 执行单个 Action
curl -X POST http://127.0.0.1:8010/actions -H "Content-Type: application/json" -d '{"type":"tap","target":{"x":360,"y":600}}'
```

截图落在 `artifacts/shots/`（已被 .gitignore 忽略）。

### 状态码约定

| 码 | 含义 |
|---|---|
| `409` | 设备忙：已有任务或写操作在跑。全局只有一台目标设备，并发驱动会让点击互相交错 |
| `422` | 请求体参数非法（如 `target` 不是坐标/字符串），在进入 handler 前即被拒 |
| `502` / `503` | 设备不可用 / VLM 调用失败 |
| `504` | `wait=true` 超时，任务仍在后台跑，可继续轮询 |

## 测试

```powershell
pip install -r requirements.txt
python -m pytest -q          # pyproject 已配好 testpaths 与 pythonpath，无需在根目录执行
```

用例全部使用假设备（`FakeDevice`）与打桩的 VLM，**不需要** adb、模拟器或 API Key，可直接在 CI 跑。
覆盖：坐标归一化换算、UI 树异常 bounds 容错、执行器参数校验、观察失败收敛、主循环状态机回滚、
Re-plan 路径的 VLM 验证、设备锁与后台任务模型、VLM 重试策略、API 契约与错误脱敏。

## 注意事项

- `/text` 仅接受安全 ASCII（字母、数字及 `_.@,/?!`），空格会被转义为 `%s`；中文输入待 M4 通过 ADB Keyboard 广播方案实现。
- Action Schema 已按 §3.2 全量落地：`tap` / `long_press` / `type` / `swipe` / `back` / `home` / `launch` / `wait` / `done`。
- 多设备寻址：优先 `ADB_SERIAL`，其次自动检测单一可用设备，否则回退默认。**当前实现按单设备设计**，
  所有写操作共用一把设备锁；扩展多设备前需把锁拆成 per-serial。
- 坐标解析：`target` 可为 `{"x":..,"y":..}`、`"x,y"`、`"x1,y1,x2,y2"`、`"[x1,y1][x2,y2]"` 或元素描述文本。
  0~1 之间的数值按归一化比例换算，其余按像素处理；换算基准取 `wm size` 的 **Override size**（实际渲染尺寸）。
  解析失败会抛出明确错误，不会静默回退到屏幕中心。
- 执行器（`agent/executor.py`）对外承诺「永不抛异常」，任何非法参数都收敛为 `{"ok": false, "error": ...}`，
  VLM 返回 `wait: "一会儿"` 这类脏数据不会让整个任务以 HTTP 500 中断。
- 观察阶段同样收敛：截图 / dump 失败记为 `ERROR` 步骤并计入重试熔断，不会 500；任务一旦启动，
  任何异常都会先把状态落为 `failed` 再抛出，不会留下卡在 `running` 的僵尸任务。
- `ARTIFACT_DIR` 同时被 `api/server.py` 与 `agent/loop.py` 读取，保证 `/screenshot` 与 Agent 截图落在同一目录。
- VLM 调用对 429 / 5xx / 网络错误做 3 次指数退避重试；4xx（除 429）不重试。
- 单步端点（`/observe`、`/actions`）刻意使用独立 Memory，不写全局 store——它们是调试入口，
  否则每次试探性点击都会在任务列表里留下一批 `__action__` 假任务。
