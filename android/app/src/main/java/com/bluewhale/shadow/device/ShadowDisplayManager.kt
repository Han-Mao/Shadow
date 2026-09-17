package com.bluewhale.shadow.device

import android.content.Context
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.os.Build
import android.util.Log
import android.view.Display

/**
 * 影子显示平面（V5 §五 / §六，依据 `问题修复.md`）。
 *
 * ━━━ 这个类要解决的问题 ━━━
 *
 * 现在的 Shadow 已经实现了「Agent 任务之间的抢占/恢复」，但没有实现
 * 「Agent 在后台执行，同时用户继续正常使用手机」——因为用户和 Agent
 * **物理上共用同一块屏幕**（`Display 0`）。`DeviceSession._owner = task_id`
 * 回答的是「哪个 Agent 持有设备」，它回答不了「用户在用设备时 Agent 在哪执行」。
 *
 * 影子平面就是那个「另一块屏幕」：Agent 的 Session 跑在自己的虚拟显示上，
 * 用户的 Display 0 不受影响，两者不再互相排队。
 *
 * ━━━ 硬约束（§六）：不许假装 ━━━
 *
 * 这是本文件最重要的一段。§六 明确列了两条**不能**靠想象绕过的限制：
 *
 * **① `MediaProjection` ≠ 后台独立屏幕。**
 * `MediaProjection` 只能**捕获**显示内容（并且要用户每次授权）。它给的是
 * 「我能看到这块屏幕」，不是「我能创建另一块屏幕」。想用投影 API 去得到
 * 一个可独立操作的屏幕，是把「看」当成了「有」。
 *
 * **② `AccessibilityService` ≠ 后台独立 App 实例。**
 * 辅助功能服务**没有能力**在后台启动一个应用的独立实例。一个 App 进程
 * 只有一个主 Activity 栈（在 Display 0 上）；`launch()` 到另一块显示上，
 * 对绝大多数应用来说等于「把这个 App 拉到前台」——用户会看到自己的手机
 * 突然跳到另一个应用。
 *
 * 所以 §十五 明确把「真正的 Shadow Display 并行执行」放在**第二阶段**，
 * 本文件在这一轮只交付**骨架 + 契约**：能力探测如实、不支持时明确说
 * 不支持、调用方（`device/session.py` 的 `ShadowSession`）照着 fail-closed 处置。
 *
 * ━━━ 为什么先写骨架而不是等真能做了再写 ━━━
 *
 * 因为**协议要先立**，「能力缺失」得是一等事实（[80]/[92] 的一贯方向：
 * 读不到 ≠ 空）。`ShadowSession` 需要知道「这块屏幕现在能不能用」，
 * 才能决定 fail-closed（shadow 任务）还是降级（hybrid 任务）。
 * 如果等实现完整了才有这个接口，那么在此之前所有调用点只能靠猜。
 *
 * ━━━ 当前真实能力（诚实清单）━━━
 *
 * | 能力 | 状态 |
 * |---|---|
 * | `createSession` 探测并登记一次会话 | 已实现（不自欺地声称有了虚拟屏） |
 * | `destroySession` 释放会话 | 已实现 |
 * | `screenshot` / `dumpUi` / `tap` / `swipe` | **未实现**，如实返回 `UNSUPPORTED` |
 * | `launch` 在影子屏上起应用 | **未实现**，如实返回 `UNSUPPORTED` |
 *
 * 未实现的那几个**不做假动作**（不去悄悄改打到 Display 0 上）——
 * 「假装成功」比「明确不支持」危险得多：调用方会以为动作发到了影子屏，
 * 而实际上点在了用户正在看的屏幕上。
 *
 * ━━━ 将来要真做得靠什么（留给第二阶段的线索）━━━
 *
 * §六 的两条限制决定了「真影子屏」的可行路径只有几条，都需要**系统级**权限
 * 或**应用自身配合**，而不是靠现有两个 API 拼出来：
 *
 * 1. **`DisplayManager.createVirtualDisplay` + 一个真正的宿主 Activity**
 *    —— 需要一个真实存在、正在渲染的 Surface 作为 output，而这要求一个
 *    可启动的、跑在目标显示上的 Activity（即回到限制②）；
 * 2. **设备厂商的「应用分身 / 平行空间」能力** —— 部分 ROM 提供多实例，
 *    但那是厂商私有 API，需按机型适配；
 * 3. **多用户 / work profile** —— 系统级隔离，需要 `DeviceOwner`（DPM）；
 * 4. **自建 App 内部渲染**（只对自家 App 有效）—— 不解决第三方 App 的问题。
 *
 * 走通任何一条的前提都是「先在一台真机上验证到 `createVirtualDisplay`
 * 真的返回了一块可交互的显示」。在那之前，本文件保持 `UNSUPPORTED`。
 */
object ShadowDisplayManager {

    private const val TAG = "ShadowDisplay"

    /** 创建影子会话失败 / 不支持时的原因码。与 Python 侧 `ShadowSessionUnavailable` 对齐。 */
    const val REASON_UNSUPPORTED = "shadow_display_unsupported"
    const val REASON_NO_DISPLAY_MANAGER = "no_display_manager"
    const val REASON_NO_ACCESSIBILITY = "accessibility_not_connected"
    const val REASON_NO_PROJECTION = "projection_not_granted"

    /**
     * 一次影子会话的登记信息。
     *
     * 刻意**不叫** `VirtualDisplay`——那个名字会让人以为它绑着一块真屏幕。
     * 这里的 `displayId` 在未实现时恒为 [Display.DEFAULT_DISPLAY]，含义是
     * 「没有独立显示，用的是默认那块」，这正是我们要如实表达的事实。
     */
    data class ShadowSession(
        val sessionId: String,
        val available: Boolean,
        val reason: String,
        val displayId: Int,
    ) {
        /** 只有 `available` 的会话才允许被拿去执行动作。 */
        fun require(): ShadowSession {
            if (!available) {
                throw ShadowDisplayUnavailableException(
                    "影子平面不可用（原因：$reason），拒绝在默认显示上执行"
                )
            }
            return this
        }
    }

    /** 与 Python 侧 `ShadowSessionUnavailable` 语义一一对应。 */
    class ShadowDisplayUnavailableException(message: String) : IllegalStateException(message)

    private var current: ShadowSession? = null

    /**
     * 探测影子平面的可用性，登记一次会话。
     *
     * **它做的是探测，不是创建。** 返回 `available = false` 时不会留下任何
     * 副作用（没有虚拟显示被创建、没有资源被占用）——所以调用方可以放心地
     * 「先问一句」，不需要配一个清理逻辑。
     *
     * 探测顺序是有讲究的：从「最基础的前置条件」到「最具体的能力」，
     * 因为 `reason` 要让用户能看懂自己该去开什么：
     *
     * 1. 没有 `DisplayManager` → 这不是 Android（或权限被裁）→ `no_display_manager`
     * 2. 辅助功能没连上 → 用户该去开辅助功能 → `accessibility_not_connected`
     * 3. 投影没授权 → 用户该去点那个系统弹窗 → `projection_not_granted`
     * 4. 以上都有，但仍然没有可用的独立显示 → `shadow_display_unsupported`
     *
     * 第 4 条是这一轮的**正常出口**：投影授权给了、辅助功能也开着，
     * 但我们依然**没有**一块可以独立操作的屏幕（§六 的两条限制）。
     * 把它与前三者分开报，是因为它们的处置完全不同——前三条用户点一下就好，
     * 第四条是能力缺失，用户怎么点都不会有，只能等第二阶段实现。
     */
    fun createSession(
        context: Context,
        sessionId: String,
        projectionGranted: Boolean = ScreenCapture.isReady(),
    ): ShadowSession {
        val manager = context.getSystemService(Context.DISPLAY_SERVICE) as? DisplayManager
        val session = when {
            manager == null -> ShadowSession(
                sessionId = sessionId,
                available = false,
                reason = REASON_NO_DISPLAY_MANAGER,
                displayId = Display.DEFAULT_DISPLAY,
            )

            !ShadowAccessibilityService.isConnected() -> ShadowSession(
                sessionId = sessionId,
                available = false,
                reason = REASON_NO_ACCESSIBILITY,
                displayId = Display.DEFAULT_DISPLAY,
            )

            !projectionGranted -> ShadowSession(
                sessionId = sessionId,
                available = false,
                reason = REASON_NO_PROJECTION,
                displayId = Display.DEFAULT_DISPLAY,
            )

            else -> ShadowSession(
                sessionId = sessionId,
                available = false,
                // §六：投影 + 辅助功能**都齐了也造不出**一块可独立操作的屏幕。
                // 这里如实说「不支持」，而不是「正在准备中」——后者会让调用方
                // 抱着希望去重试，而它永远好不了。
                reason = REASON_UNSUPPORTED,
                displayId = Display.DEFAULT_DISPLAY,
            )
        }

        current = session
        Log.i(
            TAG,
            "影子会话 $sessionId 探测结果：available=${session.available} reason=${session.reason} " +
                "displayId=${session.displayId}（SDK ${Build.VERSION.SDK_INT}）"
        )
        return session
    }

    /** 释放会话登记。因为没真的创建过虚拟显示，这里只清引用，无需释放系统资源。 */
    fun destroySession(sessionId: String? = null) {
        val existing = current ?: return
        if (sessionId != null && existing.sessionId != sessionId) {
            // 不匹配时**不动**当前会话：误删别人的会让正在跑的影子任务
            // 下一次探测时才发现自己没了。
            return
        }
        current = null
        Log.i(TAG, "影子会话 ${existing.sessionId} 已释放")
    }

    /** 当前登记的会话（没有则 null）。给健康检查用。 */
    fun currentSession(): ShadowSession? = current

    /** 影子平面此刻能不能用。等价于 `currentSession()?.available == true`。 */
    fun isAvailable(): Boolean = current?.available == true

    /**
     * 试着拿到一块独立的虚拟显示。
     *
     * **当前无条件返回 `null`。** 保留这个函数是为了把「为什么返回 null」
     * 写在一个地方，而不是让调用方各自去猜：
     *
     * 要走到返回非 null，必须先解决 §六 的限制②——`AccessibilityService`
     * 无法在后台起一个 App 的独立实例，而 `createVirtualDisplay` 需要一个
     * 真实渲染着的 Surface 作为 output。没有那个宿主 Activity，
     * 就算创建成功，得到的也是一块**空白的**显示：`screenshot()` 拍出来是黑的，
     * `tap()` 点不到任何应用界面。那样的「成功」比 `null` 更坏——
     * 它会让调用方以为自己在操作一个 App。
     */
    @Suppress("unused")
    private fun tryCreateVirtualDisplay(manager: DisplayManager): VirtualDisplay? {
        return null
    }

    /** 供健康检查展示的人类可读描述。 */
    fun describe(): String {
        val session = current ?: return "未创建影子会话"
        return if (session.available) {
            "影子平面可用（display=${session.displayId}）"
        } else {
            "影子平面不可用：${session.reason}"
        }
    }
}
