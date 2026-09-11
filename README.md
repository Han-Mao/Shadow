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
│   └── emulator.py         # 模拟器生命周期管理
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
pip install -r requirements.txt

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
| `ARTIFACT_DIR` | 截图落盘目录 | `artifacts/shots` |
| `PORT` | API 端口 | `8010` |

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
# 创建任务并自动启动 Agent Loop（同步返回最终状态）
$task = curl -X POST http://127.0.0.1:8010/tasks -H "Content-Type: application/json" -d '{"instruction":"打开设置并开启飞行模式","max_steps":8}' | ConvertFrom-Json

# 查看任务
$task.task.id
curl http://127.0.0.1:8010/tasks/$($task.task.id)

# 获取某一步截图
curl http://127.0.0.1:8010/tasks/$($task.task.id)/shots/1

# 单次观察
curl -X POST http://127.0.0.1:8010/observe

# 执行单个 Action
curl -X POST http://127.0.0.1:8010/actions -H "Content-Type: application/json" -d '{"type":"tap","target":{"x":360,"y":600}}'
```

截图落在 `artifacts/shots/`（已被 .gitignore 忽略）。

## 注意事项

- `/text` 仅接受安全 ASCII（字母、数字及 `_.@,/?!`），空格会被转义为 `%s`；中文输入待 M4 通过 ADB Keyboard 广播方案实现。
- Action Schema 已按 §3.2 全量落地：`tap` / `long_press` / `type` / `swipe` / `back` / `home` / `launch` / `wait` / `done`。
- 多设备寻址：优先 `ADB_SERIAL`，其次自动检测单一可用设备，否则回退默认。
