package com.bluewhale.shadow.device

import android.content.Context
import android.os.PowerManager
import android.view.accessibility.AccessibilityEvent

/**
 * 用户是不是正在用这台手机（V5 §十，依据 `问题修复.md`）。
 *
 * ━━━ 为什么必须有它 ━━━
 *
 * Shadow 交给真机使用后，用户和 Agent 是**共用同一块屏幕**的两个人。
 * 没有这个监测，「Agent 正在淘宝搜索」与「用户拿起手机打开微信」之间的冲突
 * 只能靠抢占解决——而抢占的正确语义是「让 Agent 停下、把屏幕让给用户」，
 * 于是用户的正常使用会把任务一次次打断。
 *
 * 有了它，运行时才能做对的事：**检测到用户操作 → 立即 checkpoint → 暂停；
 * 用户停手 N 秒 → 重新观察 → 恢复**（§十四 第一阶段）。这是「Agent 不抢用户的
 * 当前操作」，也是影子平面（§十五）之前第一步就能交付的价值。
 *
 * ━━━ Kotlin 侧比 ADB 侧强在哪 ━━━
 *
 * ADB 侧（`device/adb.py`）只能靠在两次采样之间**比对前台窗口是否变化**来近似
 * 「用户动过没有」——那是下限证据：变了说明用户在动，没变说明不了什么。
 *
 * 这里能拿到的是**事件流**：`AccessibilityService.onAccessibilityEvent` 会收到
 * `TYPE_VIEW_SCROLLED` / `TYPE_WINDOW_STATE_CHANGED` 等等，而且我们能直接读到
 * `SystemClock.uptimeMillis()` 记录「最后一次有事件是什么时候」。
 * 所以本实现给出的 `idle_seconds` 是**精确**的，`confirmed` 可以老实地说 true。
 *
 * ━━━ 一条硬约束：读不到 ≠ 用户不在 ━━━
 *
 * `snapshot()` 在服务未连上时返回 `confirmed = false`（不知道），
 * **不是**「用户不在」。把「我不知道」说成「用户不在」的代价是
 * Agent 会在用户正在看屏幕的时候发手势——正好是本模块要防的事。
 *
 * ━━━ 隐私边界 ━━━
 *
 * 这里只记「什么时候有过事件」和「前台包名」，**不记录事件内容、
 * 不记录用户输入了什么、不落盘任何用户数据**。前台包名本来就由
 * `current_focus()` 提供给核心（风险门禁需要它），所以这里没有引入新的信息面。
 */
object UserActivityMonitor {

    /** 「刚操作过」的时间窗（毫秒）。与 Python 侧 `DEFAULT_IDLE_THRESHOLD` 对齐。 */
    const val DEFAULT_IDLE_THRESHOLD_MS = 2_000L

    /**
     * 最后一次观察到用户输入类事件的时刻（`SystemClock.uptimeMillis()`）。
     *
     * 用 `uptimeMillis` 而不是 `currentTimeMillis`：后者会被用户改时间和 NTP 校正
     * 影响，算出来的「空闲了多久」可能出现负数。`uptimeMillis` 单调，只用于计时。
     *
     * `0L` = 还没有任何记录（**不是**「很久以前」）。
     */
    @Volatile
    private var lastInputAtMs: Long = 0L

    /** 最后一次观察到的前台包名（我们自己维护，而不是去问系统）。 */
    @Volatile
    private var foregroundPackage: String? = null

    /**
     * 由 [ShadowAccessibilityService] 在每次 `onAccessibilityEvent` 时调用。
     *
     * 只在该事件**确实源于用户输入**时更新 `lastInputAtMs`——这一点必须做对，
     * 否则 Agent 自己发出的手势（`dispatchGesture`）会触发事件、被记成
     * 「用户刚操作过」，于是 Agent 每动一下就以为用户在场、立刻暂停自己。
     * 那会变成一个「自己把自己冻住」的死循环。
     *
     * 判定依据见 [isUserDriven]。
     */
    fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return
        val now = android.os.SystemClock.uptimeMillis()
        if (isUserDriven(event)) {
            lastInputAtMs = now
        }
        val pkg = event.packageName?.toString()
        if (!pkg.isNullOrEmpty()) {
            foregroundPackage = pkg
        }
    }

    /**
     * 这个事件是不是**用户**引起的。
     *
     * 关键分辨：`AccessibilityEvent` 上拿不到「谁发出的这个手势」这个字段，
     * 所以只能按事件类型 + 来源保守判断：
     *
     * - `TYPE_VIEW_CLICKED` / `TYPE_VIEW_LONG_CLICKED` / `TYPE_VIEW_SCROLLED` /
     *   `TYPE_VIEW_TEXT_CHANGED` / `TYPE_VIEW_FOCUSED`：这些是**触摸/键盘**产生的。
     *   Agent 的 `dispatchGesture` 也会产生其中一部分，但——
     * - 我们用 [ShadowAccessibilityService] 里那个「自己正在发手势」的标记
     *   （见 `isDispatchingGesture()`）把它们排除掉。
     *
     * 保守方向：拿不准时返回 `true`（当作用户在操作）。误判成「用户在操作」
     * 的代价是任务暂停几秒；误判成「用户不在」的代价是手势打到用户脸上。
     */
    private fun isUserDriven(event: AccessibilityEvent): Boolean {
        // V5 P1④（审查 §六）：Agent 动作在飞时，这一批事件都是我们自己造成的。
        //
        // 这里检查的是**作用域**（`isAgentActionInFlight`）而不仅仅是手势标记：
        // `launch(package)` 并不发手势，却会引发 `TYPE_WINDOW_STATE_CHANGED`；
        // 只挡手势的话，Agent 启动应用后就会把自己判成「用户刚操作过」并暂停。
        //
        // 注意这只在**动作执行的那一小段窗口内**生效（`agentActionScope` 的
        // try/finally 之间）。用户此刻真的碰屏幕，那时事件早已落在窗口之外
        // （或手势来源是我们自己，见下），会被正常记成用户操作——这正是我们要的。
        if (ShadowAccessibilityService.isAgentActionInFlight()) return false
        if (ShadowAccessibilityService.isDispatchingGesture()) return false
        return when (event.eventType) {
            AccessibilityEvent.TYPE_VIEW_CLICKED,
            AccessibilityEvent.TYPE_VIEW_LONG_CLICKED,
            AccessibilityEvent.TYPE_VIEW_SCROLLED,
            AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED,
            AccessibilityEvent.TYPE_VIEW_FOCUSED,
            AccessibilityEvent.TYPE_TOUCH_INTERACTION_START,
            AccessibilityEvent.TYPE_TOUCH_INTERACTION_END,
            -> true
            // 窗口切换：可能是用户切的，也可能是 Agent 的 `launch` 触发的。
            // 归到「用户」这一侧是保守的（宁可多暂停）。
            //
            // ⚠️ P1④：正因为归到了「用户」这一侧，Agent 自己的 `launch` 必须由
            // 上面的作用域标记挡掉，否则会形成「Agent 启动应用 → 以为自己被打扰
            // → 暂停自己」的死循环。
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> true
            else -> false
        }
    }

    /** 有没有观察过任何用户输入。 */
    fun hasBaseline(): Boolean = lastInputAtMs > 0L

    /**
     * 读一次快照，返回**与 Python 侧 `UserContext` 逐键对齐**的 dict。
     *
     * 键名与 `device/android.py` 的 `_user_context_from_bridge()` 一致，
     * 少一个键那边就会走降级分支（保守当作用户在场）——这是可接受的失败方向，
     * 但会让「用户停手后自动恢复」失效，所以键名必须对齐。
     *
     * 返回 `null` 表示**本能力不可用**（辅助功能没连上）；这与
     * `confirmed = false` 是两件事，后者是「这次没读到」。
     */
    fun snapshot(context: Context?, idleThresholdMs: Long = DEFAULT_IDLE_THRESHOLD_MS): Map<String, Any?>? {
        val service = ShadowAccessibilityService.instance ?: return null
        val now = android.os.SystemClock.uptimeMillis()
        val last = lastInputAtMs

        if (last <= 0L) {
            // 还没有任何输入事件。**不能说用户不在**——服务可能刚连上，
            // 也可能 ROM 不派发我们监听的事件类型。
            return mapOf(
                "confirmed" to false,
                "active" to true,
                "idle_seconds" to null,
                "foreground_package" to foregroundPackage,
                "screen_locked" to isScreenLocked(context),
                "reason" to "尚未观察到任何用户输入事件",
            )
        }

        val idleMs = (now - last).coerceAtLeast(0L)
        val locked = isScreenLocked(context)
        return mapOf(
            "confirmed" to true,
            // 锁屏时用户显然不在操作（这是我们**确实**读到的）。
            // `locked == true` 而不是 `locked`：`isScreenLocked` 返回 `Boolean?`，
            // null 表示「锁屏状态没读到」——那种情况下不能顺着短路逻辑把它当成
            // 「没锁屏」，否则「不知道锁没锁」会变成「屏幕可用、可以点」。
            // 读不到锁屏状态时退回按空闲时长判断。
            "active" to (locked != true && idleMs < idleThresholdMs),
            "idle_seconds" to (idleMs / 1000.0),
            "foreground_package" to foregroundPackage,
            "screen_locked" to locked,
            "reason" to "",
        )
    }

    /**
     * 锁屏状态。读不到返回 `null`（**不是** false）。
     *
     * `PowerManager.isInteractive` 是「屏幕亮着」，`KeyguardManager.isKeyguardLocked`
     * 是「有锁屏」——两者结合才是我们关心的「用户能看见并操作屏幕吗」。
     * 单独用 `isInteractive` 会把「亮着但锁着」误判成可操作。
     */
    private fun isScreenLocked(context: Context?): Boolean? {
        if (context == null) return null
        return try {
            val keyguard = context.getSystemService(Context.KEYGUARD_SERVICE) as? android.app.KeyguardManager
            val power = context.getSystemService(Context.POWER_SERVICE) as? PowerManager
            if (keyguard == null || power == null) return null
            keyguard.isKeyguardLocked || !power.isInteractive
        } catch (exc: Exception) {
            // 厂商 ROM 上这两个系统服务的行为不完全一致，探测失败只表示「不知道」。
            null
        }
    }

    /** 仅用于测试：清空状态，避免用例之间互相污染。 */
    fun reset() {
        lastInputAtMs = 0L
        foregroundPackage = null
    }
}
