# Shadow — 面向 Android 的任务级 Agent Runtime

一个以**长任务执行可靠性**为核心的 Mobile Agent 框架：动作抽象、多设备调度、
任务状态机、崩溃恢复与执行验证，而不是「LLM → click() → 结束」的一发式脚本。

## 架构

```
                 FastAPI ── HTTP 契约（/tasks /executions /devices /events）
                    │
                 TaskManager ── 生命周期 / 指令注入
                    │
              TaskClassifier ── 规则 + 相似度 + LLM 三层关系判定
                    │
                 Scheduler ── 排队 / 优先级 / 抢占 / 恢复（每设备一条 lane）
                    │
                AgentRuntime ── Observe → Think → Act → Verify → Checkpoint
                    │
        ┌───────────┴───────────┐
        ↓                       ↓
  Vision / Grounding       DeviceController（协议）
                                │
                    ┌───────────┴───────────┐
                    ↓                       ↓
              ADB 后端（PC 控制）       Android 后端（手机自控）
```

- **决定上限的不是 VLM，而是 `TaskManager + Scheduler + Checkpoint + Runtime`**：
  VLM 决定「下一步点哪里」，这四件套决定「多件事怎么排队、被打断怎么接着做、崩溃怎么不重蹈覆辙」。
- `DeviceController` 是唯一设备抽象：ADB / Android 两个后端可互换，上层一行不改。
- 关键状态机：执行 `CREATED→RISK_CHECKED→DISPATCHED→RUNNING→终态`，其中
  `UNKNOWN`（进程死在设备调用之后）**禁止自动重试**——杜绝重复扣款/发送。

## 快速开始

```powershell
pip install -r requirements.txt      # 运行依赖（钉死版本）
$env:VLM_BASE_URL="https://api.openai.com/v1"   # OpenAI 兼容接口
$env:VLM_API_KEY="sk-..."
$env:VLM_MODEL="gpt-4o"
python -m api.server                 # 监听 127.0.0.1:8010
```

```bash
# 后台执行 + 轮询
curl -X POST http://127.0.0.1:8010/tasks -H "Content-Type: application/json" -d '{"instruction":"打开设置"}'
curl http://127.0.0.1:8010/tasks/$id
# 一条命令拿最终状态
curl -X POST http://127.0.0.1:8010/tasks -H "Content-Type: application/json" -d '{"instruction":"打开设置","wait":true}'
```

> 首次跑真机：手机开 USB 调试 → `adb devices` 拿 serial → `$env:ADB_SERIAL="R5CTxxxx"`。

## 演示

两个离线脚本，**不需要 adb / 模拟器 / API Key**，clone 下来就能跑（看「看页面 / 问模型 /
判断结果」被换成脚本，只保留调度与运行时这条真实链路）：

```bash
# ① 抢占与恢复：A 跑到一半，B 插入 → A 落 Checkpoint 让出设备 → B 完成 → A 从恢复点继续
python scripts/demo_preemption.py

# ② 回放某个真实任务的事件流（先 --list 看有哪些任务）
python scripts/replay_task.py --list
python scripts/replay_task.py <task_id>
```

演示 ① 输出的关键几行：

```
用户：#1 帮我在淘宝搜索一双黑色运动鞋       → 任务 A 开始执行
  [设备] tap(300,800) ...
用户：#2 先帮我打开微信给张三发"晚上开会"    → 任务 B（HIGH）插入
  请求任务 A 让出设备，等待方 B
  任务 A 已挂起（让出设备），等待恢复
  开始执行任务 B ... 完成
  开始执行任务 A ... 完成                     → 从恢复点继续
```

真正的「危险动作会停住等人确认」这条（发消息是 DANGEROUS、`submit` 语义），
由 `tests/test_scenarios.py` 覆盖——演示与测试用的是同一套 Runtime，区别只在
测试用 FakeDevice 断言、脚本用 PrintedDevice 打印。

## 部署到手机（可选，路线 B：设备端点）

Core 仍跑在电脑上，手机只装一个「设备端点」APK，两者走局域网 HTTP，**不需要 ADB**：

1. 打 APK：`python android/tools/build_apk.py`（无需 Android Studio/SDK）→
   `android/app/build/outputs/apk/debug/app-debug.apk`，装到手机。
2. 手机：应用里「去开启辅助功能」+「授权屏幕捕获」→「启动设备端点」→ 记下地址与令牌。
3. 电脑：

   ```powershell
   $env:SHADOW_DEVICE_BACKEND="android"
   $env:SHADOW_ANDROID_BRIDGE_URL="http://192.168.1.20:8765"   # 手机页面显示的地址
   $env:SHADOW_ANDROID_BRIDGE_TOKEN="<手机页面显示的令牌>"
   python -m api.server
   ```

   ⚠️ 端点等于「操作这台手机」，只在可信局域网用，令牌别外传、别映射到公网。

> 详见 `android/README.md`（两条路线、权限、排障、无 SDK 编译）。

## 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `ADB_SERIAL` | 目标设备 serial，逗号分隔多台 | `emulator-5554` |
| `VLM_BASE_URL` / `VLM_API_KEY` / `VLM_MODEL` | VLM 接口（OpenAI 兼容） | OpenAI / 未设 / `gpt-4o` |
| `ARTIFACT_DIR` / `STORAGE_DIR` | 截图 / 持久化目录 | `artifacts/shots` / `artifacts/state` |
| `SHADOW_DEVICE_BACKEND` | `adb`（默认）或 `android`；写错直接报错 | `adb` |
| `SHADOW_ANDROID_BRIDGE_URL` / `_TOKEN` | 手机设备端点地址与令牌（路线 B） | 未设 |
| `PORT` / `HOST` | API 端口 / 监听地址（非回环未配令牌拒绝启动） | `8010` / `127.0.0.1` |
| `SHADOW_API_TOKEN` | API 访问令牌；不设则关闭鉴权（仅建议本机） | 未设 |
| `GOAL_VERIFY_MODE` | 完成验证严格度：`auto`（按任务画像）/ `off` / `advisory` / `strict` | `auto` |
| `SHADOW_REQUIRE_AUTH` | 置 1 时无令牌也拒绝一切请求 | 未设 |

其余（只读令牌、设备范围、principal 身份、确认库路径、TOCTOU 守卫、宽限窗口等）
见代码内注释；完整清单在 `DEVLOG.md`（不随仓库发布）。

## 技术选型（为什么是这套）

| 选择 | 理由 |
|---|---|
| **FastAPI + uvicorn** | HTTP 契约清晰、原生异步；`wait=true` 走后台执行 + 轮询，不占请求线程 |
| **pydantic v2** | `Task`/`Action`/`Checkpoint` 全是强类型模型，状态迁移有结构保证 |
| **SQLite（WAL + 单库）** | 任务/恢复点/事件/票据/执行记录**同一份 `shadow.db`**，靠事务 + revision CAS 解决跨文件一致性与多进程顺序；单机部署零依赖 |
| **纯 Kotlin（零 androidx）** | 手机设备层只用 `android.*`/`java.*`/`org.json`，所以无需 Android Studio 也能 `aapt2+kotlinc+d8` 打出 APK |
| **VLM 走 HTTP、可替换** | 只认 OpenAI 兼容接口（`VLM_BASE_URL`），云端 Qwen / 本地 vLLM / Ollama 都只是三个环境变量的事 |
| **无 ADB 依赖的 Android 后端** | `AccessibilityService` + `MediaProjection` 取代 ADB 控制自己，中文输入走 `ACTION_SET_TEXT`，不装 ADB Keyboard |

