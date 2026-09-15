package com.bluewhale.shadow.device

import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo

/**
 * `AccessibilityNodeInfo` → `UiNodeAdapter`（V3.3 §3）。
 *
 * 这一层刻意做得**很笨**：只把框架属性读出来，不做任何判断、过滤或裁剪。
 * 所有「怎么写成 XML」的逻辑都在 `UiTreeSerializer` 里，所有「树该长什么样」的
 * 判断都在它外面——这样序列化器能在 JVM 单测里被完整验证，而这一层薄到
 * 出错也只会是一个属性名拼错（那种错误能靠 `UiTreeSerializerTest` 的属性清单挡住）。
 *
 * 两点刻意的取舍：
 *
 *  1. **不裁剪节点**。不跳过不可见/不可点击/无文本的节点，因为 uiautomator 不裁。
 *     裁剪会引入一类无法解释的现象：同一个页面在真机上「找不到那个按钮」，
 *     而 dump 出来的树里明明有——因为『看起来没用』的容器其实是它的祖先。
 *  2. **不回收节点**。`AccessibilityNodeInfo.recycle()` 自 API 33 起是空操作、
 *     在更早的版本上提前回收会导致后续读取抛 `IllegalStateException`。
 */
class NodeInfoAdapter(
    private val node: AccessibilityNodeInfo,
    override val index: Int,
) : UiNodeAdapter {

    override val className: String get() = text(node.className)
    override val text: String get() = text(node.text)
    override val contentDescription: String get() = text(node.contentDescription)

    /** 只有开启 `flagReportViewIds` 才有值——见 `res/xml/shadow_accessibility_service.xml`。 */
    override val resourceId: String get() = node.viewIdResourceName ?: ""

    override val packageName: String get() = text(node.packageName)

    private val bounds: Rect = Rect().also { node.getBoundsInScreen(it) }

    override val left: Int get() = bounds.left
    override val top: Int get() = bounds.top
    override val right: Int get() = bounds.right
    override val bottom: Int get() = bounds.bottom

    override fun flag(flag: UiFlag): Boolean = when (flag) {
        UiFlag.CHECKABLE -> node.isCheckable
        UiFlag.CHECKED -> node.isChecked
        UiFlag.CLICKABLE -> node.isClickable
        UiFlag.ENABLED -> node.isEnabled
        UiFlag.FOCUSABLE -> node.isFocusable
        UiFlag.FOCUSED -> node.isFocused
        UiFlag.SCROLLABLE -> node.isScrollable
        UiFlag.LONG_CLICKABLE -> node.isLongClickable
        UiFlag.PASSWORD -> node.isPassword
        UiFlag.SELECTED -> node.isSelected
    }

    override val childCount: Int get() = node.childCount

    override fun child(index: Int): UiNodeAdapter? {
        // 树在采集过程中会被页面刷新，getChild 可能对已经失效的节点抛
        // IllegalStateException / NullPointerException（框架里真实存在的行为）。
        // 这里降级成 null：丢掉一个子树，好过整棵树采集失败——
        // 「采集失败」在核心侧会走证据缺口分支，代价比少一个节点大得多。
        val child = runCatching { node.getChild(index) }.getOrNull() ?: return null
        return NodeInfoAdapter(child, index)
    }

    private fun text(value: CharSequence?): String = value?.toString() ?: ""
}
