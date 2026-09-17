package com.bluewhale.shadow.device

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Shadow 的设备能力本体（V3.3 §3/§4/§5/§6）。方案文档里「手机化最关键的一步」就是它：
 *
 *     ADB tap / text / back / screenshot        ← 旧路：需要 PC 宿主 + 调试授权 + ADB Keyboard
 *     AccessibilityService                       ← 新路：手机自己就能做到
 *
 * 能力对照（方案文档 §3–§6）：
 *
 * | 能力 | 实现 |
 * |---|---|
 * | UI 树 | `rootInActiveWindow` → [NodeInfoAdapter] → [UiTreeSerializer]（uiautomator 同构） |
 * | 点击 / 长按 / 滑动 | `dispatchGesture`（[tap] / [longPress] / [swipe]） |
 * | 返回 / 回桌面 | `performGlobalAction(GLOBAL_ACTION_BACK / HOME)` |
 * | 输入文字 | 焦点节点的 `ACTION_SET_TEXT`（[setText]），**不经过输入法** |
 * | 当前页面 | 最近一次 `TYPE_WINDOW_STATE_CHANGED`（[focus]） |
 *
 * 为什么「输入」要写在这里而不是用输入法：`ACTION_SET_TEXT` 是把整段文本直接写进
 * 焦点节点，中文和英文走同一条路。ADB 侧之所以要「ASCII 走 input text、其余走
 * ADB Keyboard 广播」，那是 **ADB 通道的限制**，不该变成所有后端都要遵守的规则——
 * 手机侧因此不需要装 ADB Keyboard、也不需要切输入法。
 */
class ShadowAccessibilityService : AccessibilityService() {

    companion object {
        private const val TAG = "ShadowAccessibility"

        /** 点击手势的时长。真机上 0 会抛异常；太短在部分机型上会被当成滑动。 */
        private const val TAP_DURATION_MS = 60L

        /** 手势回调的等待上界（毫秒）= 手势自身时长 + 这个余量。 */
        private const val GESTURE_GRACE_MS = 5_000L

        private const val MAX_EDITABLE_SEARCH_NODES = 600

        /** 服务实例。系统负责创建/销毁它，进程内其它组件靠这个引用找到它。 */
        @Volatile
        var instance: ShadowAccessibilityService? = null
            private set

        fun isConnected(): Boolean = instance != null

        /**
         * 拿服务实例，没连上就抛 [ShadowServiceUnavailable]。
         *
         * 抛而不是返回 null：`null` 会在调用点被写成 `?.let{}` 或 `!!`，
         * 于是「用户没开辅助功能」这件事被翻译成一句 NullPointerException，
         * 用户和排查的人都看不出该干什么。
         */
        fun requireService(): ShadowAccessibilityService = instance ?: throw ShadowServiceUnavailable(
            "辅助功能服务未连接。请在系统「设置 → 无障碍 / 辅助功能」里开启「Shadow 设备端点」，" +
                "然后回到 Shadow 应用重新启动设备端点。"
        )

        /**
         * 当前是不是**我们自己**正在发手势（V5 §十）。
         *
         * ━━━ 为什么这个标记是必须的 ━━━
         *
         * `dispatchGesture` 发出的人造手势同样会触发 `onAccessibilityEvent`。
         * 如果 `UserActivityMonitor` 不区分来源，就会发生这个循环：
         *
         *     Agent 点一下 → 产生 TYPE_VIEW_CLICKED 事件 → 被记成「用户刚操作过」
         *                  → 运行时在下一个安全点看到「用户在场」→ 暂停任务
         *
         * 结果是 **Agent 每动一下就立刻把自己冻住**，任务永远推进不了，
         * 而日志上看起来是「用户在一直操作手机」——极难定位。
         *
         * 所以这里用一个显式标记把「机器手势」排除掉。用 [AtomicBoolean] 而不是
         * `@Volatile var`：读在无障碍事件线程、写在调用线程，需要真正的可见性与
         * 原子性（`@Volatile` 只能保证可见性）。
         *
         * 注意它**只覆盖我们自己的手势**：用户此时真的碰屏幕，事件照样会被记成用户操作
         * （那个事件的手势来源不是我们）。这正是我们要的。
         */
        private val dispatchingGesture = AtomicBoolean(false)

        /** 见 [dispatchingGesture]。 */
        fun isDispatchingGesture(): Boolean = dispatchingGesture.get()

        private fun beginGestureDispatch(): Unit = dispatchingGesture.set(true)

        private fun endGestureDispatch(): Unit = dispatchingGesture.set(false)
    }

    private val mainHandler = Handler(Looper.getMainLooper())

    /** 最近一次窗口切换事件 → (package, activity)。 */
    @Volatile
    private var lastWindow: Pair<String, String>? = null

    // ---- 生命周期 ----

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(TAG, "辅助功能服务已连接")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        val e = event ?: return
        // V5 §十：先喂给用户活动监测器。它与下面的窗口跟踪是**两件事**：
        // 那边关心「用户有没有在动」（所有输入类事件），这里只关心「整个窗口换了」。
        UserActivityMonitor.onAccessibilityEvent(e)

        // 只关心「整个窗口换了」这一种。TYPE_WINDOW_CONTENT_CHANGED 太频繁，
        // 而且它不代表页面切换——把它记进来会让「恢复点是否还成立」的判断失真。
        if (e.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) return

        val packageName = e.packageName?.toString().orEmpty()
        if (packageName.isEmpty()) return
        lastWindow = packageName to e.className?.toString().orEmpty()
    }

    override fun onInterrupt() {
        // 系统要求实现；我们的手势都有超时，不需要在这里做额外处理。
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        instance = null
        Log.i(TAG, "辅助功能服务已断开")
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }

    // ---- Observe ----

    /**
     * 当前页面的 (package, activity)。
     *
     * 拿不到时返回 `("", "")` 而**不是抛异常**：端口约定如此（`device/controller.py`），
     * 上层要区分「读不到」与「读到了但为空」，而这两件事在桥这一侧的处置是一样的。
     */
    fun focus(): Pair<String, String> {
        lastWindow?.let { if (it.first.isNotEmpty()) return it }
        val root = runCatching { rootInActiveWindow }.getOrNull()
        val packageName = root?.packageName?.toString().orEmpty()
        return packageName to ""
    }

    /**
     * 采集 UI 树。
     *
     * 读不到树时**抛异常**，绝不返回空串：核心要区分「页面确实没有可点击元素」和
     * 「我们没读到树」（V3.1 P1-4 的目标证据缺口），返回空串会把后者伪装成前者，
     * 风险判定也会跟着降级成「这一屏没什么危险」。
     */
    fun dumpTree(): String {
        val root = runCatching { rootInActiveWindow }.getOrNull()
            ?: throw ShadowServiceUnavailable(
                "读不到当前窗口的节点树（rootInActiveWindow 为空）。可能原因：" +
                    "当前页面是系统安全界面（如锁屏、支付密码键盘），或辅助功能服务刚刚被系统重启。"
            )
        // rotation 如实上报（uiautomator 也会带）。Python 侧目前不读它，但树里
        // 声明一个**假的** 0 是不必要的谎——真要有人靠它判断方向时会被误导。
        return UiTreeSerializer.serialize(
            NodeInfoAdapter(root, index = 0),
            rotation = ScreenCapture.rotation(this),
        )
    }

    /** 当前窗口的根节点（内部用，供输入寻找可编辑控件）。 */
    private fun safeRoot(): AccessibilityNodeInfo? = runCatching { rootInActiveWindow }.getOrNull()

    // ---- Act：手势 ----

    fun tap(x: Int, y: Int) {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        dispatch(listOf(stroke(path, startTimeMs = 0, durationMs = TAP_DURATION_MS)), "点击")
    }

    fun longPress(x: Int, y: Int, durationMs: Int) {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        // 长按的下限取 300ms：低于它多数应用识别不出来，会退化成一次点击——
        // 「长按出了菜单」和「点了一下」是两种结果，静默退化会让验证步骤对不上。
        val duration = durationMs.coerceIn(300, 60_000).toLong()
        dispatch(listOf(stroke(path, startTimeMs = 0, durationMs = duration)), "长按")
    }

    fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Int) {
        val path = Path().apply {
            moveTo(x1.toFloat(), y1.toFloat())
            lineTo(x2.toFloat(), y2.toFloat())
        }
        val duration = durationMs.coerceIn(1, 60_000).toLong()
        dispatch(listOf(stroke(path, startTimeMs = 0, durationMs = duration)), "滑动")
    }

    private fun stroke(path: Path, startTimeMs: Long, durationMs: Long) =
        GestureDescription.StrokeDescription(path, startTimeMs, durationMs)

    /**
     * 下发手势并**等它真的结束**。
     *
     * 为什么不 fire-and-forget：核心侧拿到「这一步做完了」之后会立刻去验证效果、
     * 并在执行前复查观察代次（V3.1 P1-6 的 TOCTOU 门禁）。手势还没落地就返回，
     * 复查看到的会是**动作前的屏幕**，于是动作被误判成「页面没变」。
     * 同步等待把「动作生效」这件事变成一个实点。
     */
    private fun dispatch(strokes: List<GestureDescription.StrokeDescription>, what: String) {
        val service = requireService()
        val totalMs = strokes.maxOf { it.startTime + it.duration }

        val builder = GestureDescription.Builder()
        strokes.forEach { builder.addStroke(it) }
        val gesture = builder.build()

        val latch = CountDownLatch(1)
        // 用 AtomicBoolean 而不是 `@Volatile var`：Kotlin 的 @Volatile 只能用在属性上，
        // 局部变量加它是编译错误。而回调在主线执行、等待在调用线程执行，
        // 这里的可见性必须是真保证，不能靠「反正很快」。
        val completed = AtomicBoolean(false)
        val callback = object : GestureResultCallback() {
            override fun onCompleted(description: GestureDescription?) {
                completed.set(true)
                latch.countDown()
            }

            override fun onCancelled(description: GestureDescription?) {
                completed.set(false)
                latch.countDown()
            }
        }

        // V5 §十：标记「这是我们自己发的手势」，让 `UserActivityMonitor` 不要把
        // 它记成用户操作（否则 Agent 每动一下就以为用户在操作、立刻暂停自己）。
        // 用 try/finally 保证异常路径也把标记复位——留在 true 的后果是此后
        // **真实**的用户操作也被忽略，即「用户拿起手机了但 Agent 还在点」。
        beginGestureDispatch()
        val accepted = try {
            service.dispatchGesture(gesture, callback, mainHandler)
        } finally {
            endGestureDispatch()
        }
        if (!accepted) {
            throw ShadowActionFailed("$what 手势没有下发成功（dispatchGesture 返回 false，可能已有手势在执行）")
        }
        if (!latch.await(totalMs + GESTURE_GRACE_MS, TimeUnit.MILLISECONDS)) {
            throw ShadowActionFailed("$what 手势等待超时（${totalMs + GESTURE_GRACE_MS}ms 内没有回调）")
        }
        if (!completed.get()) {
            // 被取消通常是手势被系统或其它服务打断（来电、弹出系统对话框）。
            // 这是一次「动作效果未知」，不是「动作不可能」——交给核心的对账去判。
            throw ShadowActionFailed("$what 手势被取消（可能被系统弹窗或其它服务打断）")
        }
    }

    // ---- Act：全局动作 ----

    fun pressBack() {
        globalAction(GLOBAL_ACTION_BACK, "返回")
    }

    fun pressHome() {
        globalAction(GLOBAL_ACTION_HOME, "回桌面")
    }

    private fun globalAction(action: Int, what: String) {
        // 必须显式走 `requireService()`：这里若写成 `require()`，Kotlin 会解析到
        // **标准库的** `kotlin.require(Boolean)`（本类里没有同名成员），
        // 报错信息是「没有传 value 参数」——很难联想到是「服务没连上要抛自己的异常」。
        // 走 requireService() 而不是直接 performGlobalAction：服务已解绑时
        // 前者给出的原因（未开辅助功能）才是真实原因，后者只会返回 false。
        val service = requireService()
        // performGlobalAction 的返回值是「有没有受理」，不是「动作生效了没有」。
        // 这一点必须记住：返回 true 只说明系统接下了这次请求。
        if (!service.performGlobalAction(action)) {
            throw ShadowActionFailed("$what 没有下发成功（performGlobalAction 返回 false）")
        }
    }

    // ---- Act：输入 ----

    /**
     * 把整段文本写进当前焦点控件（`ACTION_SET_TEXT`，方案文档 §5）。
     *
     * 与 ADB 侧的差别顺手解决了一个老麻烦：不需要 ADB Keyboard，不需要切输入法，
     * 不需要「ASCII / 非 ASCII 分流」——中文英文同一条路。
     *
     * 写入后**回读校验**，但只把「写了却还是空的」判成失败：
     * 有些控件会把文本规范化（去空格、加占位符），拿规范化后的结果做严格比较
     * 会造出假故障，而假故障会让人不再信任这条校验。
     */
    fun setText(value: String) {
        if (value.isEmpty()) {
            throw ShadowActionFailed("输入文本为空")
        }
        val root = safeRoot()
            ?: throw ShadowServiceUnavailable("读不到当前窗口，无法定位输入框。")

        // `FOCUS_INPUT` 是 `AccessibilityNodeInfo` 的常量，不是 AccessibilityService 的——
        // 不限定类名会报 unresolved reference（真机上则表现为「输入永远找不到焦点框」）。
        val target = findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            ?.takeIf { it.isEditable }
            ?: firstEditable(root)
            ?: throw ShadowActionFailed(
                "当前页面没有可输入的控件（没有焦点输入框，也没有任何 Editable）。" +
                    "通常需要先点一下输入框再输入文字。"
            )

        val arguments = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, value)
        }
        val performed = runCatching {
            target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)
        }.getOrDefault(false)

        if (!performed) {
            throw ShadowActionFailed(
                "目标输入框拒绝了 ACTION_SET_TEXT（该控件可能只接受输入法写入，例如部分银行/密码框）。"
            )
        }

        // 回读：refresh 之后 text 才是写入后的值
        val after = runCatching {
            target.refresh()
            target.text?.toString().orEmpty()
        }.getOrDefault("")
        if (after.isEmpty()) {
            throw ShadowActionFailed(
                "ACTION_SET_TEXT 报告成功，但输入框回读仍为空——这次输入没有真正生效。"
            )
        }
    }

    /** 广度优先找第一个可编辑控件。 */
    private fun firstEditable(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val queue = ArrayDeque<AccessibilityNodeInfo>()
        queue.add(root)
        var visited = 0
        while (queue.isNotEmpty() && visited < MAX_EDITABLE_SEARCH_NODES) {
            val node = queue.removeFirst()
            visited++
            if (node.isEditable) return node
            for (index in 0 until node.childCount) {
                runCatching { node.getChild(index) }.getOrNull()?.let { queue.add(it) }
            }
        }
        return null
    }

    /** 给 `/health` 与「设备状态」用的一句话。 */
    fun describeState(): String =
        if (isConnected()) "device" else "service_disabled"

    /** API 版本相关的说明放进日志，方便排查机型差异。 */
    fun logEnvironment() {
        Log.i(TAG, "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})，辅助功能已连接")
    }
}
