# Shadow Android —— 手机侧设备层

这个目录实现方案文档（`../bluewhale-shadow-phone/手机部署方案.md`）§3–§6 的那一层：

```
AccessibilityService    →  UI 树（AccessibilityNodeInfo）/ 手势 / 全局动作 / 输入文字
MediaProjection         →  截图
PackageManager + Intent →  启动应用
```

**它替代的是 ADB**。方案文档 §1 的核心判断是「手机自己当 Agent 主机时不该再通过 ADB
控制自己」——因为 ADB 需要 PC 宿主、需要调试授权、还要装 ADB Keyboard。

---

## 两条路线：Python Core 放哪儿

手机端的设备层是同一份代码；不同的只是 **Shadow Core（`TaskManager` / `Scheduler` /
`AgentRuntime` / `RiskGate` / `Checkpoint`）跑在哪儿、怎么跟这一层说话**。

| | 路线 A：同进程（Chaquopy） | **路线 B：设备端点（本工程默认）** |
|---|---|---|
| 拓扑 | APK 里同时有 Kotlin 设备层和 Python Core | 手机只当设备端点；Core 跑在 PC / 局域网 |
| 设备层怎么被调用 | Chaquopy 直接把 Kotlin 对象注册给 Python | HTTP（`/bridge/<方法名>`），Core 侧用 `device/remote.py` |
| 手机侧需要 | Chaquopy + Python 全家桶（含 pydantic） | 只有这个 App |
| 现在能不能跑 | **不能，见下面「路线 A 的硬依赖」** | 能 |
| Python 侧要改什么 | 无（`register_android_bridge` 已经就绪） | 无（`SHADOW_ANDROID_BRIDGE_URL` 指过来即可） |

两条路线的**接缝是 `AndroidBridge` 这条协议**（`../device/android.py` 顶部有完整表格），
协议只有一份，所以手机端的代码不因为选哪条而白写。

### 路线 A 的硬依赖（这是本工程默认走路线 B 的原因）

把 Python Core 打进 APK 需要 `pydantic` 2.x，而 `pydantic` 2.x 的核心
`pydantic-core` 是 **Rust 扩展**，PyPI 上没有 Android 轮子。Chaquopy 官方仓库
（<https://chaquo.com/pypi-13.1/>）只收录他们已经构建好的原生包：

- Chaquopy 维护者的原话是 **「Pydantic version 2 isn't currently available for Chaquopy」**
  （chaquo/chaquopy#1160，2024-11，此后无更新）；
- 另有一份专门为 Chaquopy 构建 `pydantic-core` 的尝试，卡在 PyO3 的 abi3 特性上
  （pydantic/pydantic-core#1607，2025-01）。

而 Shadow 的 `models/`（`Task` / `TaskStep` / `Action` / `Checkpoint` / `Budget` …）
**每一个模型都是 pydantic `BaseModel`**——`agent/`、`api/`、`storage/` 全都依赖它。
所以这不是「换个包」的问题，而是「核心的模型层要不要重写」的问题。

**先验证再决定**（这一步就能给你答案，不需要改任何代码）：

```bash
cd android
./gradlew :app:assembleDebug          # 路线 B，应当成功
# 打开路线 A 之后（见下），同一个命令会失败在 generateDebugPythonRequirements，
# 错误里若出现 `Failed to install pydantic-core==...`，就是这里说的情况。
```

拿到那个错误之后有两条路：① 自己用 `chaquopy/build-wheel.py` + Rust/NDK 给 Android
构建 `pydantic-core` 轮子，然后 `pip { install("wheels/pydantic_core-....whl") }`；
② 把 Core 的模型层从 pydantic 迁到 dataclasses（大改动，要先评估
`model_copy` / `model_dump` / 校验器那几处语义）。**都没有捷径。**

> 这份「不能」是查证得到的结论，不是猜测：本工程因此默认实现路线 B，
> 并且把路线 A 需要的两处配置写在下面「开启路线 A」里 —— 等你验证完再打开。

### 路线 B 反而更贴合方案文档

方案文档 §2 自己就写着「**Android 上不要直接依赖 Python 全套 Runtime**」，§10 把产品
形态分成 `shadow-core` / `shadow-desktop` / `shadow-android`，并画了
「手机 A → Shadow Cloud ← 手机 B/C」——那正是路线 B。多台手机各跑一个这个 App，
把各自的端点注册到同一个 Core，就得到了 §10 的第三形态（也是 `DevicePool` /
`DeviceSession` 那套多设备抽象第一次有真实落点）。

---

## 快速开始（路线 B）

### 1. 构建

两条路，产出同一个东西（`app/build/outputs/apk/debug/app-debug.apk`）：

```bash
# ① 有 Android Studio / SDK / Gradle：常规路径
cd android
./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk

# ② 什么都没有（不需要 Android Studio / Gradle / SDK）：本仓库自带的脚本
python android/tools/build_apk.py      # 首次会下约 125MB 工具链到 ~/.workbuddy/binaries/
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
```

第 ② 条不是「凑合」——它把 `assembleDebug` 那一步拆开手工走一遍，每一步都是官方工具：

```
aapt2 compile   res/ → .flat
aapt2 link      清单 + 资源 + android.jar → base.apk（含 resources.arsc）+ R.java
javac           R.java + BuildConfig.java
kotlinc         src/main 的 Kotlin 源码
d8              27 个 class → classes.dex
zip 装包        base.apk + classes.dex
zipalign        -p 4
apksigner       调试密钥签名（v2 + v3）
```

产出与 Android Studio 的 debug 构建同形：`debuggable=true`、`BuildConfig.DEBUG=true`、
调试密钥签名。脚本最后会自己校验签名、包名、版本、权限，并逐条核对
**「清单里声明的每个组件类都在 dex 里」**——那正是 `ClassNotFoundException` 的替身
（AGP 会把这类问题留到安装/启动时才炸）。

> 需要 JDK 17（AGP 8 的要求；本机没有 JDK 时脚本会用 PyCharm 自带的 `jbr`）。
> 有 SDK 的机器上仍然推荐第 ① 条：`./gradlew` 会顺带跑 lint 与资源合并的完整流程。

### 2. 在手机上授权（两件事，都要做）

1. 打开应用 → **去开启辅助功能** → 在系统「无障碍 / 辅助功能」里开启「Shadow 设备端点」。
2. 回到应用 → **授权屏幕捕获** → 在系统弹窗里选「立即开始」。

两个都到位后，页面上的「辅助功能服务」「屏幕捕获」都会显示「已就绪」。
**少任何一个都不能工作**：辅助功能负责动作与 UI 树，投屏负责截图（VLM 要靠截图决策）。

页面最上面那行**就绪状态**（v4.2 §三 P1）把三项压成一句话，缺哪项直接点名：
「还不能工作：缺 辅助功能服务、屏幕捕获」／「权限已就绪，但端点没启动」／
「就绪：权限齐、端点在跑，Core 现在可以连过来」。
它刻意**不检查悬浮窗权限**——本应用从不申请 `SYSTEM_ALERT_WINDOW`，
设备层只走 AccessibilityService 与 MediaProjection（见 `AndroidManifest.xml` 顶部
「刻意不申请的权限」）。列一项自己根本不需要的权限，只会诱导用户去开一个没用的开关。

「Core 连得上吗」在手机侧**无法自检**：连接是 Core 主动发起的，手机只是监听。
能自检的是「端点有没有在监听」——端口被占等原因会显示在设备端点那一行下面。

### 3. 启动设备端点

点「启动设备端点」。此时：

- 状态栏出现常驻通知（前台服务，防止应用切后台后被系统回收）；
- 页面上显示 `地址` 与 `令牌`，点「复制」拿到 Core 侧要填的三行。

### 4. 在跑 Shadow Core 的机器上

```bash
export SHADOW_DEVICE_BACKEND=android
export SHADOW_ANDROID_BRIDGE_URL=http://192.168.1.20:8765     # 手机页面上显示的地址
export SHADOW_ANDROID_BRIDGE_TOKEN=<手机页面上显示的令牌>

# VLM（方案文档 §8：模型不放手机本地，走 HTTP）
export VLM_BASE_URL=https://your-gateway/v1
export VLM_API_KEY=...
export VLM_MODEL=qwen-vl-max

python -m uvicorn api.server:app --host 0.0.0.0 --port 8000
# 或者直接跑一个任务：
#   POST /tasks {"instruction": "打开淘宝，搜索机械键盘"}
```

自检：`curl http://192.168.1.20:8765/health`（需要令牌头）会回
`{"ok":true,"value":{"state":"device",...}}`；`state` 不是 `device` 就说明权限还没齐。

---

## 协议：12 个能力

方法名就是协议的一部分（同进程按名字反射、远程映射成 `/bridge/<name>`），
两侧逐字同名，由 `../tests/test_android_bridge_contract.py` 钉住。

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
| `launch` | `package, activity` | — | `PackageManager` + `startActivity`（`activity` 为空＝主界面） |

### UI 树必须与 `uiautomator dump` 同构

这是整个改造里最要紧的一条约定。守住了，Python 侧的 `vision/parser.py` /
`vision/target.py` / `vision/grounding.py` / `agent/evidence.py` / `agent/risk_gate.py`
**一行都不用改**；守不住，就得再写一整套 Android 专用的 UI 解析与坐标映射。

```xml
<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="立即购买" resource-id="com.taobao.taobao:id/buy"
        class="android.widget.Button" package="com.taobao.taobao" content-desc=""
        checkable="false" checked="false" clickable="true" enabled="true"
        focusable="true" focused="false" scrollable="false" long-clickable="false"
        password="false" selected="false" bounds="[600,1150][760,1250]" />
</hierarchy>
```

`resource-id` 有值的前提是辅助功能配置里打开了 `flagReportViewIds`——**少了它不会报错**，
只会让「靠 id 认出来的按钮」全部认不出来，风险判定跟着变松（`com.taobao:id/pay_confirm`
这类按钮正是靠 id 判出 purchase 语义的）。

---

## 目录

```
android/
├── app/src/main/java/com/bluewhale/shadow/
│   ├── MainActivity.kt                      权限引导 + 端点控制 + 连接信息
│   ├── device/
│   │   ├── UiNodeAdapter.kt                 UI 树的最小抽象（让序列化器可被 JVM 单测）
│   │   ├── UiTreeSerializer.kt              → uiautomator 同构 XML（**最不能错的一处**）
│   │   ├── NodeInfoAdapter.kt               AccessibilityNodeInfo → UiNodeAdapter
│   │   ├── ShadowAccessibilityService.kt    树 / 手势 / 全局动作 / ACTION_SET_TEXT
│   │   ├── ScreenCapture.kt                 MediaProjection 截图
│   │   ├── AppLauncher.kt                   PackageManager + Intent 启动
│   │   ├── AndroidBridgeImpl.kt             12 个方法（协议实现）
│   │   └── DeviceErrors.kt                  两类失败：权限没给 / 这次没成功
│   └── endpoint/
│       ├── BridgeHttpServer.kt              极小的 HTTP 端点（ServerSocket，零依赖）
│       └── DeviceEndpointService.kt         前台服务（端点是常驻的）
├── app/src/test/java/.../UiTreeSerializerTest.kt   序列化器的格式契约（JVM 可跑）
└── tools/
    ├── _toolchain.py                        JDK / android.jar / build-tools 的定位与下载（两个脚本共用）
    ├── verify_kotlin_compile.py             无 SDK 环境下真编译 + JVM 单测
    └── build_apk.py                         无 SDK 环境下打出可安装的 debug APK
```

## 测试与编译验证

### 静态契约（每次提交都跑，零下载）

```bash
cd .. && python -m pytest tests/test_android_bridge_contract.py -q
```

Kotlin 侧的方法名 / 参数个数 / HTTP 路由 / 序列化属性集合 / 清单权限 / `R.string` 引用,
都在这里与 Python 侧比对。其中 `R.*` 那两条是 AAPT 的 `error: resource not found` 的替身
——没有 SDK 的环境同样能挡住「改了 `strings.xml` 忘了改代码」和「代码引用了没写的资源」。

### 真编译（不需要 Android Studio，一次性下载约 125MB 工具链）

本工程**零 androidx 依赖**，Kotlin 侧只用 `android.*` / `java.*` / `org.json`，
所以带着一个 `android.jar` 就能编——不需要 AGP、不需要模拟器、不需要真机：

```bash
python android/tools/verify_kotlin_compile.py
# [1/3] android.jar 就位
# [2/3] 生成 R/BuildConfig 桩（string=27 id=9 layout=1）
# [3/3] 编译全部 Kotlin 源码 … 编译通过：27 个 class
# 运行 JVM 单测 … OK (10 tests)
```

- 缺工具链时脚本会直接下载（约 125MB，进 `~/.workbuddy/binaries/`，**不装系统目录**）；
  加 `--no-download` 则只打印手工下载命令。kotlinc 来自 Maven Central，
  `android.jar` 来自 dl.google.com 的 `platform-35`（与 `compileSdk = 35` 一致）；
  JDK 用任意 17+，本机用的是 PyCharm 自带的 `jbr`（可用 `SHADOW_JAVA` 覆盖）。
- `R` / `BuildConfig` 是 AGP 的生成物：脚本从 `res/` 与 `build.gradle.kts` **真解析**出名字
  再生成桩，所以「引用了不存在的资源」会像真实构建那样直接编译失败。
- 有 SDK 的正常路径仍是 `cd android && ./gradlew :app:test`（JVM 单测）与 `assembleDebug`。

### 打包成可安装的 APK（同样不需要 Android Studio）

```bash
python android/tools/build_apk.py
# [1/9] 合并清单（package=com.bluewhale.shadow，debuggable=true）
#      清单声明 3 个组件：MainActivity、ShadowAccessibilityService、DeviceEndpointService
# [2/9] aapt2 compile …  [3/9] aapt2 link …  [4/9] javac …  [5/9] kotlinc …
# [6/9] d8：项目自己的 class + kotlin-stdlib.jar → classes.dex（2.5 MB）
# [7/9] 装包 + zipalign …  [8/9] 签名（调试密钥）…  [9/9] 校验
# 产物：android/app/build/outputs/apk/debug/app-debug.apk（793 KB）
```

它额外下载 build-tools r35（约 60MB，与 `compileSdk` 对齐），并用到其中的
`aapt2` / `d8` / `zipalign` / `apksigner` / `dexdump`。三处**踩过的坑**都写在脚本注释里，
因为它们都会给出误导性的错误信息：

| 现象 | 真正的原因 |
|---|---|
| `resource string/app_name does not override an existing resource`（一大片） | `aapt2 link` 的 `-R` 是**overlay 语义**（help 里写着「最后给出的冲突资源胜出」）。普通资源要走**位置参数** |
| `Unsupported source file type`（d8） | 传了目录，kotlinc 在输出目录里留了 `META-INF/main.kotlin_module`。改成显式列出 `.class` |
| `Couldn't get file size: Bad file descriptor`（dexdump） | `dexdump` 只认裸 `.dex`，给它 APK 不行。把包里那份解出来再验 |

> 这条路抓到过**三处真缺陷**（均已修）。前两处是编译期就现形的：
> `ShadowAccessibilityService.globalAction()` 里的 `require()` 被 Kotlin 解析成了标准库的
> `kotlin.require(Boolean)`（本类没有同名成员，报错信息完全指不到问题）；
> `findFocus(FOCUS_INPUT)` 少了类名限定（`FOCUS_INPUT` 属于 `AccessibilityNodeInfo`）。
> 这两处在真机上只会表现为「返回/回桌面莫名失败」和「输入找不到焦点框」。
> 第三处**只有装到真机点开图标才会现形**：`dex()` 漏了 `kotlin-stdlib.jar`
> ——静态校验全过、包也能装，一启动就是 `NoClassDefFoundError`
> （完整经过见下面「已知限制 6」）。

### 两边合起来才是闭环

- Kotlin 侧证明「序列化器输出的 XML == golden 文件」，Python 侧把 golden 文件喂给
  **真实的** `vision.target` / `agent.evidence` / `agent.risk_gate`，证明「这份 XML 是有用的」；
- 再加上方法名 / 参数个数 / 属性集合的静态比对，把「跨语言改名」「漏输出一个属性」
  这类**不会报错只会变松**的漂移挡住。

---

## 安全：这个端点等于「操作这台手机」的能力

`POST /bridge/tap {"x":680,"y":1200}` 就能点到屏幕上任意位置。所以：

- **必须带 `X-Shadow-Token`**（应用首次启动时随机生成，存本机设置），请求头不对一律 401；
- **只在用户主动点「启动设备端点」时监听**，前台服务一停就关，且 `START_NOT_STICKY`
  （用户没点就不该有监听端口存在）；
- **只在可信局域网里开**。绝不要把 8765 端口映射到公网、也不要放在有公共 Wi-Fi 的环境里；
- 令牌不要外传、不要写进截图或聊天记录。

这套口径与仓库 API 侧一致（`SHADOW_API_TOKEN` / 非回环裸绑定拒绝启动 / 部署安全三道闸）。

---

## 已知限制（每条都写了现象与触发条件）

1. **屏幕旋转**：VirtualDisplay 是按授权那一刻的尺寸建的，任务中途旋转屏幕后
   截图尺寸与 `screen_size` 会不一致，坐标会整体偏移。
   *现象*：动作「点了没反应」。**处理**：任务期间别转屏；要支持它需要在
   `ScreenCapture` 里注册 `DisplayManager.DisplayListener` 重建 VirtualDisplay。
2. **安全界面截不到图**：锁屏、支付密码键盘等 `FLAG_SECURE` 页面，
   `MediaProjection` 拿到的是黑屏或空帧。*现象*：`screenshot` 报「拿不到画面帧」。
   **处理**：这是系统的设计，不是缺陷；这类页面本来也该转人工。
3. **同进程路线（A）下「权限没给」不能结构化区分**：Chaquopy 会把 Java 异常包一层，
   Python 侧拿不到 `ShadowServiceUnavailable` 这个类型名，也读不到它的属性，
   只能靠消息文本。**所以两条路线的错误消息都自带可读的中文原因。**
   *触发条件*：路线 A 落地后若需要结构化区分，就给协议加一个 `readiness()` 方法
   （返回 `device` / `no_accessibility` / `no_projection`），不要在文本上做正则。
4. **`set_text` 依赖焦点**：`ACTION_SET_TEXT` 作用于焦点输入框；页面没有焦点输入框时
   会去找第一个 `Editable`。像某些银行/支付输入框只接受输入法写入，这类会明确报
   「目标输入框拒绝了 ACTION_SET_TEXT」，不会被当成静默成功。
5. **只支持单台手机一个端点**：一个 App 实例一个端点（端口 8765）。
   多台手机＝多台各跑一个，Core 侧用 `ADB_SERIAL`/日志区分标识——这与
   `device/factory.py` 的 `resolve_device_serials`「Android 后端恒为一台」一致。
6. **打包与真机：装机与启动已实测通过；交互行为仍未验证。**
   - **已做**：`python android/tools/build_apk.py` 产出 `app-debug.apk`
     （aapt2 → javac → kotlinc → d8 → zipalign → apksigner，**793 KB**，v2+v3 签名），
     并静态校验了包名 / 版本 / 权限 / 签名 /「清单声明的组件都在 dex 里」/
     辅助功能配置 XML 在包内。清单里的 `minSdk 26 / targetSdk 34 / compileSdk 35`
     与 `build.gradle.kts` 一致。
   - **2026-09-16 真机实测**（vivo V2352A / Android 16 / API 36，经无线调试）：
     装机成功，`MainActivity` 正常启动并完整渲染（就绪状态那行正确显示
     「还不能工作：缺 辅助功能服务、屏幕捕获」），崩溃缓冲区为空。
     **这一轮暴露出两个只在真机上才会现形的缺陷，均已修**：
     ① `dex()` 漏了把 `kotlin-stdlib.jar` 交给 d8。编译期有 `-classpath` 就够，
        运行期却没有实体，于是点开图标即
        `NoClassDefFoundError: Failed resolution of: Lkotlin/jvm/internal/Intrinsics`。
        这个包能打、能签名、能安装，上面那套静态校验**全部通过**，
        **只有真机点开的那一下才崩**。已修（`dex()` 把 `STDLIB_JAR` 加进 d8 输入），
        并在 `verify_contents()` 加了第 ④ 条守卫把这个坑钉死。
     ② 调试密钥原先放在 `WORK` 里，而 `main()` 开头就 `rmtree(WORK)` ——
        等于**每次构建都换一把密钥**，`adb install -r` 必然撞
        `INSTALL_FAILED_UPDATE_INCOMPATIBLE`，只能先卸载再装。已挪到
        `~/.android/debug.keystore`（AGP 的惯例位置，好处是与 `./gradlew assembleDebug`
        共用同一把密钥，两条构建路径产出的包可以互相覆盖安装）。
   - **仍未验证**：交互行为。最需要盯的三条——手势坐标是否被 ROM 缩放、
     投屏帧率与延迟是否够 VLM 用、厂商后台存活策略会不会杀掉前台服务。
     这三条要等权限授权、真跑起任务才能看。

---

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| Core 报「连不上手机设备端点」 | 端点没启动 / 不同网段 / 手机锁屏省电杀了进程 | 看手机通知栏有没有常驻通知；`curl http://<地址>:8765/health` |
| 401 | 两端令牌不一致 | 复制手机页面上的令牌，重新 export |
| Core 报「辅助功能服务未连接」 | 辅助功能没开或被系统关掉 | 系统设置里重新开启，并**重新启动端点**（服务实例是系统管理的） |
| Core 报「还没有授予屏幕捕获权限」 | 投屏没授权或授权已失效 | 应用里重新点「授权屏幕捕获」（重启应用后授权会失效，这是系统行为） |
| 动作一直「没生效」 | `dispatchGesture` 返回成功但页面没变 | 看 `dump_ui` 是不是空树；确认手势时长（点击 60ms、长按 ≥300ms） |
| 「打开微信」失败 | 包可见性被过滤 / 应用没装 | 确认清单里有 `QUERY_ALL_PACKAGES` 与 `<queries>` |
| 找不到某个按钮 | `flagReportViewIds` 没生效 → `resource-id` 全空 | 确认辅助功能配置 XML；重新开关一次辅助功能服务 |

---

## 开启路线 A（Chaquopy）

默认不开：它会让**每一次**构建都必须解析 chaquo 仓库，而路线 B 不需要。
确认过上面那条 `pydantic-core` 之后，按这两处改：

**`android/build.gradle.kts`**

```kotlin
plugins {
    id("com.android.application") version "8.9.1" apply false   // Chaquopy 16.1 支持 AGP 8.9–8.13
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    id("com.chaquo.python") version "16.1.0" apply false
}
```

**`android/settings.gradle.kts`** 的 `repositories` 里加上：`maven("https://chaquo.com/maven")`

**`android/app/build.gradle.kts`**

```kotlin
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// 把仓库里的六个运行时包同步进 APK（不签入，见 .gitignore）
val syncShadowCore by tasks.registering(Copy::class) {
    from(rootProject.file("..")) {
        include("agent/**", "api/**", "device/**", "models/**", "storage/**", "vision/**")
        exclude("**/__pycache__/**")
    }
    into(layout.projectDirectory.dir("src/main/python"))
}
tasks.named("preBuild") { dependsOn(syncShadowCore) }

chaquopy {
    defaultConfig {
        version = "3.12"
        pip {
            install("httpx")
            // install("pydantic")   ← 就是这一步会卡住，见 README 顶部
        }
    }
}
```

然后在 `MainActivity.onCreate` 里（或一个自定义 `Application`）启动 Python 并注册桥：

```kotlin
Python.start(AndroidPlatform(this))
val py = Python.getInstance()
val environ = py.getModule("os").get("environ")
environ.callAttr("__setitem__", "SHADOW_DEVICE_BACKEND", "android")
environ.callAttr("__setitem__", "ARTIFACT_DIR", filesDir.resolve("state").absolutePath)

// 直接注册桥**实例**（不是工厂）：Chaquopy 把 Kotlin 对象转成 Python 对象之后，
// 那个对象不是可调用的函数，当工厂传会抛 "object is not callable"。
py.getModule("device.android")
    .callAttr("register_android_bridge_object", AndroidBridgeImpl(this))
```

两处与「照直觉写会踩坑」的地方，都已经在代码里就地注释：

- `AndroidBridgeImpl` 的 `screen_size` / `current_focus` 返回 `List` 而不是 Kotlin `Pair`
  ——Chaquopy 包出来的 `Pair` 不支持 `[0]` 下标，而 Python 侧正是这么读的；
  这条由 `tests/test_android_bridge_contract.py` 的一条静态断言钉住。
- `register_android_bridge_object`（而不是 `register_android_bridge`）见上。

Core 在手机上跑时还需要这几个环境变量（都在 `api/server.py` / `vision/vlm.py` 里读）：

| 变量 | 手机上该设成 |
|---|---|
| `SHADOW_DEVICE_BACKEND` | `android` |
| `ARTIFACT_DIR` | 应用私有目录，如 `filesDir/state`（**不能是工作目录**，Android 上不可写） |
| `VLM_BASE_URL` / `VLM_API_KEY` / `VLM_MODEL` | 指向你的模型网关（方案文档 §8） |
| `SHADOW_CONFIRM_DB` | 应用私有目录下的 sqlite 路径（确认令牌的一次性靠它） |
