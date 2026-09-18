package com.bluewhale.shadow.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * `AppResolver` 的契约（2026-09-17 真机任务「打开拼多多搜索手机」暴露）。
 *
 * ━━━ 为什么这组用例值得存在 ━━━
 *
 * 这个类防的不是崩溃，而是**静默选错**：
 *
 *     模型给「拼多多」→ 解析成 com.xunmeng.pinduoduo   ← 期望
 *     模型给「拼多多」→ 解析成 com.xunmeng.pinduoduo.merchant（商家版）← 灾难
 *
 * 后者启动**会成功**，界面也长得像，于是「以为在操作拼多多、其实在操作拼多多商家版」
 * 这件事在日志里毫无痕迹。跟它相比，「什么都没启动」反而是好的。
 *
 * 所以规则本身要能被逐条钉住，尤其是**优先级**——而优先级这种东西，
 * 只有把两个都会命中的名字同时放进候选集才测得出来。
 *
 * 跑法：`python android/tools/verify_kotlin_compile.py`
 */
class AppResolverTest {

    /**
     * 取自真机（vivo V2352A）的 `pm list packages -3`，不是编的。
     *
     * 「微信输入法」与「搜狗输入法」是有意同时放进去的：它们唯一的作用是
     * 让「输入法」这个模糊名字**同时命中两个候选**——歧义判定全靠这一对才测得出来
     * （第一版只放了微信输入法，于是「输入法」唯一命中，用例当场自己失败）。
     */
    private val apps = listOf(
        AppResolver.AppEntry("com.xunmeng.pinduoduo", "拼多多"),
        AppResolver.AppEntry("com.tencent.mm", "微信"),
        AppResolver.AppEntry("com.tencent.wetype", "微信输入法"),
        AppResolver.AppEntry("com.sohu.inputmethod.sogou", "搜狗输入法"),
        AppResolver.AppEntry("com.eg.android.AlipayGphone", "支付宝"),
        AppResolver.AppEntry("com.android.chrome", "Chrome"),
    )

    @Test
    fun `中文应用名解析成包名`() {
        // 这条就是真机上的原样复现：模型给「拼多多」，链路上没人翻译，
        // 于是被送进 getLaunchIntentForPackage → 报「应用可能未安装」。
        assertEquals(
            "com.xunmeng.pinduoduo",
            AppResolver.resolve("拼多多", apps),
        )
    }

    @Test
    fun `带后缀的名字靠包含匹配兜住`() {
        // 模型很爱写「XX应用」「XX图标」这类名字。
        assertEquals("com.xunmeng.pinduoduo", AppResolver.resolve("拼多多应用", apps))
        assertEquals("com.xunmeng.pinduoduo", AppResolver.resolve("拼多多图标", apps))
    }

    @Test
    fun `精确匹配优先于包含，微信不会被微信输入法抢走`() {
        // 整组用例里最要紧的一条。两个候选**都会命中包含匹配**，
        // 只有优先级能决定结果；而选错的代价是操作了一个完全不同的 App。
        assertEquals("com.tencent.mm", AppResolver.resolve("微信", apps))
        // 反过来也要对：指名道姓写全了，就得给输入法。
        assertEquals("com.tencent.wetype", AppResolver.resolve("微信输入法", apps))
    }

    @Test
    fun `大小写与首尾空白不敏感`() {
        assertEquals("com.android.chrome", AppResolver.resolve("chrome", apps))
        assertEquals("com.android.chrome", AppResolver.resolve("  CHROME  ", apps))
    }

    @Test
    fun `多个候选时报歧义而不是猜一个`() {
        // 「输入法」同时命中「微信输入法」与「搜狗输入法」——用户完全可能这么说。
        // 猜一个的代价是操作了错误的 App 并继续往下走；报错的代价只是重试。
        var message = ""
        try {
            AppResolver.resolve("输入法", apps)
            fail("匹配到多个应用时必须抛错，不能猜")
        } catch (exc: ShadowActionFailed) {
            message = exc.message ?: ""
        }
        assertTrue("报错要点出第一个候选：$message", "微信输入法" in message)
        assertTrue("报错要点出第二个候选：$message", "搜狗输入法" in message)
        assertTrue("报错要点出候选包名：$message", "com.sohu.inputmethod.sogou" in message)
    }

    @Test
    fun `认不出时报错并说明机器上有什么`() {
        // 失败信息是给「下一个排查的人」看的：只写「找不到」等于让人去猜
        // 是名字错了、还是包没装、还是包可见性没生效。
        var message = ""
        try {
            AppResolver.resolve("不存在的应用", apps)
            fail("认不出的名字必须抛错")
        } catch (exc: ShadowActionFailed) {
            message = exc.message ?: ""
        }
        assertTrue("要说清是哪个名字没认出来：$message", "不存在的应用" in message)
        assertTrue("要报出候选数量：$message", "可启动的应用有 ${apps.size} 个" in message)
        assertTrue("要给出机器上装了什么的线索：$message", "拼多多" in message)
    }

    @Test
    fun `空名字直接报错，不进入匹配`() {
        var raised = false
        try {
            AppResolver.resolve("   ", apps)
        } catch (exc: ShadowActionFailed) {
            raised = true
        }
        assertTrue("空名字应当立刻失败（否则会匹配上任意一个包含空串的候选）", raised)
    }

    @Test
    fun `候选集为空时说清是候选集的问题`() {
        // 这一条对着包可见性：QUERY_ALL_PACKAGES / <queries> 没生效时，
        // queryIntentActivities 会安静地返回空列表。此时的报错必须点向
        // 「候选集是空的」，而不是「你名字写错了」。
        var message = ""
        try {
            AppResolver.resolve("拼多多", emptyList())
            fail("候选集为空时必须抛错")
        } catch (exc: ShadowActionFailed) {
            message = exc.message ?: ""
        }
        assertTrue("要点出「一个都没有」这类线索：$message", "一个都没有" in message)
    }
}
