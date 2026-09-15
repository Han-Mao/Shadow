package com.bluewhale.shadow.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * `UiTreeSerializer` 的契约（V3.3 §3，[92]）。
 *
 * 为什么这组用例值得存在：序列化出来的 XML 是**跨语言契约**——
 * Python 侧的 `vision/parser.py`、`vision/target.py`、`vision/grounding.py`、
 * `agent/evidence.py`、`agent/risk_gate.py` 全按 uiautomator 的约定解析它。
 * 没有这组用例的话，这个契约只能靠在真机上试一次来确认，而「这一次对了」
 * 并不能说明「属性名没写错、转义没漏」——那些错误在真机上往往表现为
 * 「某个按钮找不到」或「某个 action 被当成安全动作」，都是很难倒推的现象。
 *
 * 跑法：`./gradlew :app:test`（普通 JVM，不需要真机/模拟器）。
 *
 * golden 文件（`src/test/resources/golden_ui_tree.xml`）同时被仓库根的 pytest
 * 用例读取并喂给**真实的** `vision/target.resolve_target` —— 也就是这个格式的
 * 消费方。两头钉住，格式漂移会在其中一侧立刻暴露。
 */
class UiTreeSerializerTest {

    private fun golden(): String {
        // 只对 golden 做换行归一：它是**磁盘上的文件**，会被 git 的 core.autocrlf
        // 转换（本仓库就是 LF→CRLF）。序列化器的输出不做任何归一——那才叫逐字比较。
        // 契约里 XML 的换行必须与 uiautomator 一致（LF），这一点由这里挡住。
        return javaClass.classLoader!!.getResource("golden_ui_tree.xml")!!
            .readText()
            .replace("\r\n", "\n")
    }

    // ---- 假树：与 tests/test_android_adapter.py 里的 UI_TREE 同构 ----

    private class FakeNode(
        override val index: Int,
        override val className: String,
        override val text: String = "",
        override val resourceId: String = "",
        override val packageName: String = "com.taobao.taobao",
        override val contentDescription: String = "",
        override val left: Int = 0,
        override val top: Int = 0,
        override val right: Int = 0,
        override val bottom: Int = 0,
        private val flags: Set<UiFlag> = emptySet(),
        private val children: List<UiNodeAdapter> = emptyList(),
    ) : UiNodeAdapter {
        override fun flag(flag: UiFlag): Boolean = flag in flags
        override val childCount: Int get() = children.size
        override fun child(index: Int): UiNodeAdapter? = children.getOrNull(index)
    }

    private fun shoppingPage(): UiNodeAdapter = FakeNode(
        index = 0,
        className = "android.widget.FrameLayout",
        right = 1080,
        bottom = 1920,
        flags = setOf(UiFlag.ENABLED),
        children = listOf(
            FakeNode(
                index = 0,
                className = "android.widget.EditText",
                text = "搜索",
                resourceId = "com.taobao.taobao:id/search",
                right = 400,
                bottom = 120,
                flags = setOf(UiFlag.CLICKABLE, UiFlag.ENABLED, UiFlag.FOCUSABLE),
            ),
            FakeNode(
                index = 1,
                className = "android.widget.Button",
                text = "立即购买",
                resourceId = "com.taobao.taobao:id/buy",
                left = 600,
                top = 1150,
                right = 760,
                bottom = 1250,
                flags = setOf(UiFlag.CLICKABLE, UiFlag.ENABLED, UiFlag.FOCUSABLE),
            ),
        ),
    )

    // ---- 契约 ----

    @Test
    fun `serialised tree matches the golden file byte for byte`() {
        assertEquals(golden(), UiTreeSerializer.serialize(shoppingPage()))
    }

    @Test
    fun `every attribute the python parser reads is present`() {
        // vision/parser.py 的 UiNode 只读这些字段；少一个就是静默降级
        // （例如没有 bounds → 坐标为 (0,0,0,0) → 点击落到屏幕左上角）。
        val xml = UiTreeSerializer.serialize(shoppingPage())

        for (attribute in listOf(
            "index", "text", "resource-id", "class", "package", "content-desc", "bounds",
            "clickable", "enabled",
        )) {
            assertTrue("XML 里缺少属性 $attribute：$xml", xml.contains(" $attribute=\""))
        }
        // 十个布尔属性的完整集合也要在（Vision 的其它模块会用到各自的那个）
        for (flag in UiFlag.entries) {
            assertTrue("XML 里缺少布尔属性 ${flag.attribute}", xml.contains(" ${flag.attribute}=\""))
        }
    }

    @Test
    fun `bounds keeps the uiautomator bracket format`() {
        val xml = UiTreeSerializer.serialize(shoppingPage())

        // 必须是 [x1,y1][x2,y2]；vision/parser.py 的正则是 \[\d+,\d+\]\[\d+,\d+\]
        assertTrue(xml.contains("bounds=\"[600,1150][760,1250]\""))
    }

    @Test
    fun `negative bounds survive`() {
        // 真机的离屏节点 bounds 可能是负数（vision/parser.py 的注释专门提到过），
        // 序列化不能把它抹成 0——那会让「元素在屏幕外」和「元素在左上角」混为一谈。
        val node = FakeNode(
            index = 0,
            className = "android.view.View",
            left = -1,
            top = -1,
            right = -1,
            bottom = -1,
        )

        assertTrue(UiTreeSerializer.serialize(node).contains("bounds=\"[-1,-1][-1,-1]\""))
    }

    // ---- 转义 ----

    @Test
    fun `text with quotes and ampersands is escaped exactly once`() {
        val node = FakeNode(
            index = 0,
            className = "android.widget.TextView",
            text = "A&B \"quote\"",
            contentDescription = "it's",
        )
        val xml = UiTreeSerializer.serialize(node)

        assertTrue(xml.contains("text=\"A&amp;B &quot;quote&quot;\""))
        assertTrue(xml.contains("content-desc=\"it&apos;s\""))
        // 双重转义是最坏的一种：它在 Python 侧表现为「文本里多出 &amp; 这几个字符」，
        // 于是精确匹配永远失败、模糊匹配也可能错配。
        assertTrue("出现了双重转义：$xml", !xml.contains("&amp;quot;"))
    }

    @Test
    fun `angle brackets are escaped so the tree stays well formed`() {
        // uiautomator 自己**不**转义 < 和 >（只转义 " 和 &），于是页面文本里出现
        // 书名号或比较符号时它输出的其实是非法 XML，Python 侧 ET.fromstring 直接抛错。
        // 那种失败在下游表现为「目标解析失败」，排查要绕一大圈才能回到「原来是有个 <」。
        val node = FakeNode(index = 0, className = "android.widget.TextView", text = "1<2>0")

        val xml = UiTreeSerializer.serialize(node)

        assertTrue(xml.contains("text=\"1&lt;2&gt;0\""))
        assertTrue(!xml.contains("1<2>0"))
    }

    @Test
    fun `control characters do not leak into attributes`() {
        // 属性值里的换行会让 Python 侧拿到的文本带上看不见的换行，
        // vision/parser.match_by_text 的精确匹配（candidate == desc）就永远不成立。
        val node = FakeNode(index = 0, className = "android.widget.TextView", text = "第一行\n第二行")

        val xml = UiTreeSerializer.serialize(node)

        assertTrue(xml.contains("text=\"第一行 第二行\""))
    }

    // ---- 结构 ----

    @Test
    fun `leaf nodes are self closing and parents are not`() {
        val xml = UiTreeSerializer.serialize(shoppingPage())

        // 叶子：<node ... />
        assertTrue(xml.contains("/>"))
        // 父节点：闭合标签
        assertTrue(xml.contains("</node>"))
        // 根节点由 <hierarchy> 承载，且带 rotation
        assertTrue(xml.startsWith("<?xml"))
        assertTrue(xml.contains("<hierarchy rotation=\"0\">"))
        assertTrue(xml.trimEnd().endsWith("</hierarchy>"))
    }

    @Test
    fun `rotation is carried through`() {
        val xml = UiTreeSerializer.serialize(shoppingPage(), rotation = 1)

        assertTrue(xml.contains("<hierarchy rotation=\"1\">"))
    }

    @Test
    fun `an empty hierarchy is still a valid document`() {
        // 页面没有任何可读节点时也要吐一份**格式正确**的文档：
        // 核心侧据此判断「页面确实没有可点击元素」，而不是「我们没读到树」。
        // 这两种情况的处置不同（V3.1 P1-4 的目标证据缺口）。
        val xml = UiTreeSerializer.serialize(FakeNode(index = 0, className = "android.view.View"))

        assertTrue(xml.contains("<hierarchy rotation=\"0\">"))
        assertTrue(xml.contains("class=\"android.view.View\""))
    }
}
