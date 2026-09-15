package com.bluewhale.shadow.device

/**
 * `UiNodeAdapter` → 与 `uiautomator dump` **同构**的 XML（V3.3 §3，[92]）。
 *
 * 这个类是整个 Android 设备层里最不能出错的一处：Python 侧的 `vision/parser.py`、
 * `vision/target.py`、`vision/grounding.py`、`agent/evidence.py`、`agent/risk_gate.py`
 * 全都按 uiautomator 的约定解析这棵树。格式对上，那些模块一行都不用改；
 * 格式对不上，就得再写一整套 Android 专用的 UI 解析与坐标映射——那是白付的成本。
 *
 * 输出形状（与真机 `uiautomator dump` 逐字对齐）：
 *
 * ```xml
 * <?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
 * <hierarchy rotation="0">
 *   <node index="0" text="立即购买" resource-id="com.taobao.taobao:id/buy"
 *         class="android.widget.Button" package="com.taobao.taobao" content-desc=""
 *         checkable="false" checked="false" clickable="true" enabled="true"
 *         focusable="true" focused="false" scrollable="false" long-clickable="false"
 *         password="false" selected="false" bounds="[600,1150][760,1250]" />
 * </hierarchy>
 * ```
 *
 * 两处刻意的偏离，都写在对应代码旁边：
 *  - 转义的范围**比 uiautomator 宽**（它也转义 `<` / `>`，见 [escape]）；
 *  - 空元素用自闭合标签（uiautomator 在部分版本里写成 `<node ...></node>`），
 *    两者对 XML 解析器等价。
 */
object UiTreeSerializer {

    /** Android 侧没有窗口时的 rotation 值，与 uiautomator 一致。 */
    const val DEFAULT_ROTATION = 0

    private const val DECLARATION = "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"

    /**
     * 序列化整棵树。
     *
     * @param root 通常是 `rootInActiveWindow` 对应的节点
     * @param rotation 屏幕旋转，进 `<hierarchy rotation="...">`
     */
    fun serialize(root: UiNodeAdapter, rotation: Int = DEFAULT_ROTATION): String {
        val out = StringBuilder(4096)
        out.append(DECLARATION).append('\n')
        out.append("<hierarchy rotation=\"").append(rotation).append("\">")
        appendNode(out, root, depth = 1)
        out.append("\n</hierarchy>\n")
        return out.toString()
    }

    /**
     * 转义属性值。
     *
     * 转义 `&` 必须排在最前面，否则后面替换出来的 `&quot;` 会再被替换一次，
     * 变成 `&amp;quot;`——那是**双重转义**，文本到了 Python 侧就多出可见的乱码。
     *
     * 比 uiautomator 多转义 `<` 和 `>`：`AccessibilityNodeInfoDumper` 只处理
     * `"` 与 `&`，所以页面文本里出现 `<` 时它输出的其实是**非法 XML**，
     * 而 `vision/parser.py` 用的是 `ET.fromstring`，会直接抛解析错误。
     * 那种失败在下游表现为「目标解析失败」（V3.1 P1-4 的证据缺口），
     * 排查起来要绕一大圈才能回到「原来是有个书名号」。
     */
    fun escape(value: String): String {
        if (value.isEmpty()) return ""
        val out = StringBuilder(value.length + 16)
        for (ch in value) {
            when (ch) {
                '&' -> out.append("&amp;")
                '<' -> out.append("&lt;")
                '>' -> out.append("&gt;")
                '"' -> out.append("&quot;")
                '\'' -> out.append("&apos;")
                // 控制字符（含 \n \t）不该出现在 XML 属性值里：换行会让 Python 侧
                // 拿到的文本带上看不见的换行，做精确匹配（vision/parser.match_by_text
                // 的 `candidate == desc`）时永远匹配不上。统一换成空格。
                else -> if (ch.code < 0x20) out.append(' ') else out.append(ch)
            }
        }
        return out.toString()
    }

    private fun appendNode(out: StringBuilder, node: UiNodeAdapter, depth: Int) {
        val pad = "\n" + "  ".repeat(depth)
        out.append(pad).append("<node")
        attribute(out, "index", node.index.toString())
        attribute(out, "text", node.text)
        attribute(out, "resource-id", node.resourceId)
        attribute(out, "class", node.className)
        attribute(out, "package", node.packageName)
        attribute(out, "content-desc", node.contentDescription)
        for (flag in UiFlag.entries) {
            attribute(out, flag.attribute, if (node.flag(flag)) "true" else "false")
        }
        attribute(
            out,
            "bounds",
            "[${node.left},${node.top}][${node.right},${node.bottom}]",
        )

        if (node.childCount == 0) {
            out.append(" />")
            return
        }
        out.append('>')
        for (index in 0 until node.childCount) {
            val child = node.child(index)
            if (child != null) {
                appendNode(out, child, depth + 1)
            }
        }
        out.append(pad).append("</node>")
    }

    private fun attribute(out: StringBuilder, name: String, value: String) {
        out.append(' ').append(name).append("=\"").append(escape(value)).append('"')
    }
}
