package com.bluewhale.shadow.device

import android.content.Context

/**
 * `AndroidBridge` 的 Kotlin 实现——**手机侧唯一需要被实现的东西**（V3.3 §3，[94]）。
 *
 * 它把 `AndroidBridge` 的 12 个方法逐个落到「谁去干」上，自己不做任何加工：
 *
 * ```
 * AgentRuntime → executor → AndroidDeviceController → AndroidBridge → 本类
 *                                                                      ├── ShadowAccessibilityService（树/手势/全局动作/输入）
 *                                                                      ├── ScreenCapture（MediaProjection）
 *                                                                      └── AppLauncher（PackageManager）
 * ```
 *
 * ━━━ 为什么方法名是 snake_case ━━━
 *
 * 因为**方法名就是协议的一部分**，而协议是跨语言的：
 *
 *   - 同进程（路线 A）：Chaquopy 按名字找方法，名字对不上一调就 AttributeError；
 *   - 远程（路线 B）：HTTP 端点把它映射成 `/bridge/<name>`。
 *
 * 改成 Kotlin 习惯的 camelCase 会让「协议」和「编码风格」这两件事纠缠在一起——
 * 每次有人手滑改名，症状是「某一类动作在真机上莫名失败」，而不是编译错误。
 * 保持与 Python 侧逐字同名，`tests/test_android_bridge_contract.py` 才能用
 * 一条机械的断言把 12 个名字钉住。
 *
 * ━━━ 出错时抛什么 ━━━
 *
 * 一律抛 [ShadowServiceUnavailable]（权限/服务没就绪）或 [ShadowActionFailed]（这次没成功）。
 * **消息必须自带可读的中文原因**：核心侧会把它显示给用户，而两条路线下异常的
 * 类型名不一定透传得过来（Chaquopy 会包一层）。
 */
class AndroidBridgeImpl(private val context: Context) {

    // ---- Observe ----

    /**
     * 屏幕尺寸，必须是**实际渲染尺寸**。
     *
     * 返回 `List` 而不是 `Pair`：同进程路线（Chaquopy）下这个返回值会被交给 Python，
     * 而 Python 侧是按 `size[0]` / `size[1]` 读的（`device/android.py`）。
     * Kotlin 的 `Pair` 被 Chaquopy 包成 Java 对象后**不支持下标**，
     * 会以一个莫名其妙的 TypeError 失败；`List` 可以。
     * 这不是风格问题，是跨语言边界上的类型选择。
     *
     * 优先用投屏建立时那份尺寸：核心拿它做坐标归一化（`vision/grounding`），
     * 而 VLM 看到的像素来自 `screenshot_bytes`——两者必须来自同一份数字，
     * 否则任务中途旋转屏幕就会让整屏坐标系统性偏移，而现象只是「点了没反应」。
     */
    fun screen_size(): List<Int> =
        (ScreenCapture.frameSize() ?: ScreenCapture.displaySize(context)).toList()

    /**
     * 当前 `[package, activity]`。
     *
     * 辅助功能没连上时返回 `["", ""]` 而**不是抛异常**——这是端口约定
     * （`device/controller.py` 的 `current_focus`）：上层要区分「读不到」与
     * 「读到了但为空」，而这两件事在桥这一侧都是「没有可用的窗口信息」，
     * 统一按「读不到」处理；真正的失败会在 `dump_ui` 那一步被明确报出来。
     */
    fun current_focus(): List<String> =
        (ShadowAccessibilityService.instance?.focus() ?: ("" to "")).toList()

    /** UI 树。格式必须与 `uiautomator dump` 同构（见 [UiTreeSerializer]），读不到就抛。 */
    fun dump_ui(): String = ShadowAccessibilityService.requireService().dumpTree()

    /** 截图（PNG 字节），走 MediaProjection。 */
    fun screenshot_bytes(): ByteArray = ScreenCapture.capturePng()

    /** 设备状态。`"device"` 表示**两个权限都到位、可以执行动作**。 */
    fun state(): String = when {
        !ShadowAccessibilityService.isConnected() -> "service_disabled"
        !ScreenCapture.isReady() -> "no_projection"
        else -> "device"
    }

    /**
     * 用户是不是正在用手机（V5 §十）。
     *
     * 返回一个 `Map`，键与 Python 侧 `UserContext` 逐字对齐
     * （见 `device/android.py` 的 `_user_context_from_bridge`）：
     *
     *     confirmed / active / idle_seconds / foreground_package / screen_locked / reason
     *
     * **返回 `null` 表示辅助功能没连上**（这项能力用不了），
     * 而 `confirmed = false` 是「这次没读到」——两者不同：
     * 前者会让 `supports_user_activity()` 判 False、`SHADOW`/`HYBRID` 任务被拒或降级，
     * 后者只是这一次保守处理。这个区分要留住，否则「能力缺失」会被当成「这一刻不确定」，
     * 于是影子模式在根本不支持它的设备上被放行。
     */
    fun user_activity(): Map<String, Any?>? = UserActivityMonitor.snapshot(context)

    /**
     * 影子执行平面探测（V5 §五 / §六）。
     *
     * 返回 `Map`，键与 Python 侧 `ShadowSession` 逐字对齐
     * （见 `device/android.py` 的 `_shadow_session_from_bridge`）：
     *
     *     available / reason / display_id
     *
     * `available` **当前恒为 `false`**——不是「还没实现所以先给个默认值」，
     * 而是「§六 的两条限制下，现有 API 造不出可独立操作的屏幕，所以如实说不支持」。
     * 调用方（`device/session.py`）据此 fail-closed（`shadow` 任务）或降级到前台
     * （`hybrid` 任务）。
     *
     * **不要**在这里返回 `null` 表示「不支持」：`null` 在协议里已经被
     * `user_activity` 用作「桥声明没有这个能力」，而这里的语义是「探测过了，
     * 结论是不支持，原因在 `reason` 里」。前者该走 `supports_*()` 的判据，
     * 后者是**正常的探测结果**。混起来就没法区分「老 APK 没这个方法」与
     * 「新 APK 探测后说不可用」。
     */
    fun shadow_session(sessionId: String?): Map<String, Any?> =
        ShadowDisplayManager.createSession(context, sessionId ?: "").let { session ->
            mapOf(
                "available" to session.available,
                "reason" to session.reason,
                "display_id" to session.displayId,
            )
        }

    /** 释放影子会话。没登记过时是无操作。 */
    fun shadow_release(sessionId: String?) {
        ShadowDisplayManager.destroySession(sessionId)
    }

    // ---- Act ----

    fun tap(x: Int, y: Int) {
        ShadowAccessibilityService.requireService().tap(x, y)
    }

    fun long_press(x: Int, y: Int, duration_ms: Int) {
        ShadowAccessibilityService.requireService().longPress(x, y, duration_ms)
    }

    fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, duration_ms: Int) {
        ShadowAccessibilityService.requireService().swipe(x1, y1, x2, y2, duration_ms)
    }

    /** 输入文字：焦点控件的 `ACTION_SET_TEXT`（方案文档 §5），不经过输入法。 */
    fun set_text(value: String) {
        ShadowAccessibilityService.requireService().setText(value)
    }

    fun press_back() {
        ShadowAccessibilityService.requireService().pressBack()
    }

    fun press_home() {
        ShadowAccessibilityService.requireService().pressHome()
    }

    /** 启动应用。`activity` 为空时按包名启动主界面（与 ADB 侧语义一致）。 */
    fun launch(package_: String, activity: String?) {
        AppLauncher.launch(context, package_, activity)
    }
}
