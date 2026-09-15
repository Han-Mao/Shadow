package com.bluewhale.shadow.device

/**
 * UI 树序列化时**唯一**需要的节点信息（V3.3 §3；`device/android.py` 顶部表格）。
 *
 * 为什么要有这层抽象、而不是直接用 `AccessibilityNodeInfo`：
 *
 *  1. `AccessibilityNodeInfo` 是 Android 框架类，普通 JVM 单测里拿不到（stub 会抛异常），
 *     于是「序列化出来的 XML 到底长什么样」这件事**永远无法被自动验证**——只能靠
 *     在真机上试一次。而这个格式是整个改造里最要紧的一条约定（Python 侧
 *     `vision/parser.py`、`vision/target.py`、`vision/grounding.py`、`agent/evidence.py`、
 *     `agent/risk_gate.py` 全按它解析），它值得被测试守住。
 *  2. 有了这层，序列化器可以在 `src/test` 里用一棵假树跑（`./gradlew :app:test`），
 *     真实实现只负责「把框架属性读出来」这一件机械的事。
 *
 * 与 Python 侧的对照：这跟 `DeviceController` 端口 + `FakeAndroidBridge` 是同一个手法
 * ——**把不可测的东西挤到一层薄薄的适配器里**。
 */
interface UiNodeAdapter {

    /** 同层内的序号，对应 uiautomator 的 `index`。 */
    val index: Int

    val className: String
    val text: String
    val contentDescription: String

    /** 对应 `resource-id`。**只有开启 flagReportViewIds 才有值**（见 res/xml 的说明）。 */
    val resourceId: String

    val packageName: String

    val left: Int
    val top: Int
    val right: Int
    val bottom: Int

    fun flag(flag: UiFlag): Boolean

    val childCount: Int
    fun child(index: Int): UiNodeAdapter?
}

/**
 * 布尔属性，枚举值本身就是 uiautomator 的属性名。
 *
 * 用枚举而不是在序列化器里写死一长串 `attr("clickable")`，是为了让
 * **「uiautomator 的属性名」只出现一次**：写错一个属性名（例如
 * `longClickable` 写成 `long-clickable`）时，不会有一个静默的坑——
 * Python 侧 `vision/parser.py` 读 `attr("clickable", "false")`，名字对不上就是
 * 永远 false，而「永远 false」在下游的表现是「这个按钮点不了」，很难倒推回来。
 */
enum class UiFlag(val attribute: String) {
    CHECKABLE("checkable"),
    CHECKED("checked"),
    CLICKABLE("clickable"),
    ENABLED("enabled"),
    FOCUSABLE("focusable"),
    FOCUSED("focused"),
    SCROLLABLE("scrollable"),
    LONG_CLICKABLE("long-clickable"),
    PASSWORD("password"),
    SELECTED("selected"),
}
