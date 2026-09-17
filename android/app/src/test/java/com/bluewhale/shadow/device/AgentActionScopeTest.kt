package com.bluewhale.shadow.device

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * `AgentActionScope` 的契约（V5 P1④，审查 §六）。
 *
 * ━━━ 为什么这组用例值得存在 ━━━
 *
 * 这个作用域防的是一个**自己把自己冻住**的静默退化：
 *
 *     Agent 动作（launch / 手势 / 返回…）
 *        ↓
 *     引发 AccessibilityEvent（`TYPE_WINDOW_STATE_CHANGED` 等）
 *        ↓
 *     `UserActivityMonitor` 把它记成「用户刚操作过」
 *        ↓
 *     下一轮 runtime 看到 `user_active=true` → 暂停任务
 *
 * 结果是 Agent 每动一下就暂停自己、任务永远推进不了，而日志上看起来像
 * 「用户一直在操作手机」。它**不是崩溃**，所以可靠的复现只能靠这里的用例把它
 * 钉住——真机上要靠日志倒推，Python 侧的契约测试又只扫方法名与参数个数，
 * 看不到「计数有没有正确归零」。
 *
 * 被测对象是 [AgentActionScope] 而不是 `ShadowAccessibilityService` 的转发方法：
 * 后者的 android 父类在 JVM 上加载不了（android.jar 里是 `Stub!`），
 * 所以真正的逻辑被刻意拆成了一个纯 JVM 类。
 *
 * 跑法：`python android/tools/verify_kotlin_compile.py`
 */
class AgentActionScopeTest {

    @After
    fun cleanup() {
        // 计数是 object 单例里的状态，用例之间会互相污染。
        AgentActionScope.reset()
    }

    @Test
    fun `作用域内标记为在飞，退出后归零`() {
        assertFalse("初始不该有动作在飞", AgentActionScope.isInFlight())

        val inside = AgentActionScope.run("测试动作") { AgentActionScope.isInFlight() }

        assertTrue("作用域内必须报告「有动作在飞」", inside)
        assertFalse(
            "出了作用域必须归零——否则此后真实的用户操作都会被过滤掉",
            AgentActionScope.isInFlight(),
        )
    }

    @Test
    fun `异常路径也必须复位`() {
        // 整组用例里最要紧的一条：动作抛异常时若不复位，标记会**永久**留在「在飞」，
        // 于是 UserActivityMonitor 再也不记用户活动，Agent 会在用户手里一直点。
        var raised = false
        try {
            AgentActionScope.run("会抛的动作") {
                throw ShadowActionFailed("故意失败")
            }
        } catch (exc: ShadowActionFailed) {
            raised = true
        }

        assertTrue("异常应当照常抛出去（作用域不吞异常）", raised)
        assertFalse(
            "异常路径也必须把标记复位——留在「在飞」会让真实用户操作被永久忽略",
            AgentActionScope.isInFlight(),
        )
    }

    @Test
    fun `嵌套时按计数归零，内层退出不影响外层`() {
        // 用布尔的话，内层先退出就把外层的标记一起清掉了，
        // 于是外层剩余期间**真实的用户操作会被误判成 Agent 动作**。
        // 那是方向相反的错（漏报用户），比误报用户在场危险得多。
        AgentActionScope.run("外层") {
            assertTrue("外层在飞", AgentActionScope.isInFlight())

            AgentActionScope.run("内层") {
                assertTrue("内层在飞", AgentActionScope.isInFlight())
            }

            assertTrue(
                "内层退出后外层**仍然**在飞——这正是用计数而不是布尔的原因",
                AgentActionScope.isInFlight(),
            )
        }

        assertFalse("全部退出后才归零", AgentActionScope.isInFlight())
    }

    @Test
    fun `连续多轮之后状态不漂移`() {
        // 这条盯的是「有没有残留累积」。累积起来的表现同样是静默的：
        // 计数始终 > 0 之后，作用域再也关不上，用户活动从此全被过滤。
        repeat(3) {
            AgentActionScope.run("正常动作") { }
        }
        assertFalse("多余的退出不该把计数压成负数或留下残值", AgentActionScope.isInFlight())

        val states = mutableListOf<Boolean>()
        repeat(5) {
            AgentActionScope.run("连续动作") { states.add(AgentActionScope.isInFlight()) }
            states.add(AgentActionScope.isInFlight())
        }
        assertEquals(
            "每轮都应当是「在飞 → 不在飞」",
            List(10) { it % 2 == 0 },
            states,
        )
    }

    @Test
    fun `多余的退出是幂等的，不会把计数压成负数`() {
        // AgentActionScope.exit() 里做了 coerceAtLeast(0)。这条用例钉住它：
        // 计数一旦变成负数，`get() > 0` 会永久为 false，作用域从此完全失效。
        // 正常路径不会多退出，但「不该发生」不等于「不会发生」——
        // 而它一旦发生的表现是静默失效，不是报错。
        AgentActionScope.exit()
        AgentActionScope.exit()
        assertFalse("多余的退出不该让计数变负", AgentActionScope.isInFlight())

        // 失效的判据：此后 enter 一次就应当立刻恢复「在飞」。
        AgentActionScope.enter()
        assertTrue("enter 一次就该在飞（说明计数没被压成负）", AgentActionScope.isInFlight())
        AgentActionScope.exit()
        assertFalse(AgentActionScope.isInFlight())
    }
}
