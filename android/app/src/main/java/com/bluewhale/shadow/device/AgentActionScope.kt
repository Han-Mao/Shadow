package com.bluewhale.shadow.device

import java.util.concurrent.atomic.AtomicInteger

/**
 * 「Agent 自己正在做动作」的作用域计数（V5 P1④，审查 §六）。
 *
 * ━━━ 为什么单独一个类，而不是塞在 `ShadowAccessibilityService` 里 ━━━
 *
 * 这套逻辑**完全不依赖 Android**：只是一个线程安全的计数器。把它放在
 * AccessibilityService 里的代价是——想在 JVM 上测它，就必须把整个
 * `AccessibilityService`（以及它的 android 父类）加载起来，而 android.jar 里
 * 的方法体全是 `throw new RuntimeException("Stub!")`。结果是：**这段逻辑
 * 只能靠真机验证，而它在真机上的失败表现是静默的行为退化**（任务莫名暂停），
 * 不是崩溃 —— 最难查的那一类。
 *
 * 拆出来之后它就能被普通 JUnit 直接覆盖（见 `AgentActionScopeTest`），
 * 而 Service 侧只剩一行 `AgentActionScope.enter/exit` 的转发。
 *
 * ━━━ 它防的是什么 ━━━
 *
 * 自己把自己冻住的死循环：
 *
 *     Agent 动作（launch / 手势 / 返回…）
 *        ↓
 *     引发 AccessibilityEvent（`TYPE_WINDOW_STATE_CHANGED` 等）
 *        ↓
 *     `UserActivityMonitor` 把它记成「用户刚操作过」
 *        ↓
 *     下一轮 runtime 看到 `user_active=true` → 暂停任务
 *
 * `dispatchGesture` 那一类老早就有 `dispatchingGesture` 布尔标记挡着了，
 * 但 `launch(package)` **不发手势**却会直接引发窗口切换事件——同一个循环的
 * 第二个入口，此前完全没被挡住。
 *
 * ━━━ 为什么是计数而不是布尔 ━━━
 *
 * 动作可以嵌套（将来大概率会有「动作A 包动作B」的组合）。用布尔的话，
 * 内层先结束就把外层的标记一同清掉，于是外层剩余期间**真实的用户操作会被
 * 误判成 Agent 动作**——那是方向相反的错（漏报用户在操作），
 * 比误报「用户在场」危险得多。
 */
object AgentActionScope {

    /** 当前有几个 Agent 发起的动作在飞。 */
    private val inFlight = AtomicInteger(0)

    /** 有没有 Agent 发起的动作正在执行。 */
    fun isInFlight(): Boolean = inFlight.get() > 0

    /**
     * 进入一个 Agent 动作作用域。必须与 [exit] 配对，且**务必用 try/finally**。
     *
     * 不复位的后果比多暂停几次严重得多：标记会永久留在「在飞」，于是此后
     * `UserActivityMonitor` 再也不记用户活动 —— Agent 就会在用户手里一直点。
     * 用 [run] 更稳妥，它把 try/finally 封在里面。
     */
    fun enter(): Unit {
        inFlight.incrementAndGet()
    }

    /**
     * 退出作用域。
     *
     * `coerceAtLeast(0)` 防的是「多余的退出」。正常情况下不会发生，但它一旦发生，
     * 计数变成负数会让 [isInFlight]（判据是 `> 0`）**永久失效**——
     * 而表现同样是静默的，不是报错。宁可吃掉多余的退出，也不要让作用域悄悄关掉。
     */
    fun exit(): Unit {
        inFlight.updateAndGet { current -> if (current > 0) current - 1 else 0 }
    }

    /**
     * 把动作包在作用域里执行（推荐用法）。
     *
     * **为什么不是 `inline`**：Kotlin 会报 `public-API inline function cannot
     * access non-public-API function` —— `inline` 把函数体展开到调用点，
     * 而调用点不允许访问 `private` 成员。把 [enter]/[exit] 提升成公开 API
     * 来换取 `inline` 是不划算的：那等于把「计数怎么加减」暴露给所有调用方。
     * 何况这是每次动作只走一次的路径，不是热循环。
     */
    fun <T> run(what: String, block: () -> T): T {
        enter()
        return try {
            block()
        } finally {
            exit()
        }
    }

    /** 仅用于测试：把计数归零，避免用例之间互相污染。 */
    fun reset() {
        inFlight.set(0)
    }
}
