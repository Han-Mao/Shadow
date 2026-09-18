package com.bluewhale.shadow.device

/**
 * 「指令里的应用」→ 已安装的包名。
 *
 * ━━━ 为什么需要它（2026-09-17 真机任务踩到）━━━
 *
 * 任务「打开拼多多搜索手机」第一步就失败：
 *
 *     找不到 拼多多 的启动入口：应用可能未安装，或它的启动 Activity 不响应 MAIN/LAUNCHER。
 *
 * 而拼多多**装着、也有启动入口**。模型给的是应用名（`拼多多`），
 * `getLaunchIntentForPackage` 要的是包名（`com.xunmeng.pinduoduo`）。
 * 这条链路上没有任何一处出错——两个名字都真实存在，只是**没有人负责翻译**。
 * 于是失败被报成「应用可能未安装」，指向了完全错误的方向。
 *
 * ━━━ 为什么是纯 JVM 类（与 `AgentActionScope` 同一条理由）━━━
 *
 * `PackageManager` 在 JVM 上是 `Stub!`，碰一下就炸。所以：
 *
 *     「怎么选」留在本文件（纯逻辑，可在 verify_kotlin_compile.py 里跑）
 *     「去哪问」留在 AppLauncher（拿着 Context 去 PackageManager 取候选）
 *
 * 拆开的收益很直接：**选错应用这件事没有任何运行期报错**——启动成功了，
 * 只是启动的是另一个 App，后面每一步都跟着错。这类缺陷只能靠用例钉住。
 */
object AppResolver {

    /** 一个「装了、且有启动入口」的应用。 */
    data class AppEntry(val packageName: String, val label: String)

    /**
     * 把 [target] 解析成 [apps] 里的包名，解析不出来就抛 [ShadowActionFailed]。
     *
     * 匹配分三档，**顺序即优先级**：
     *
     *     ① 应用名精确相同           → 用它
     *     ② 应用名互相包含           → 只有一个候选时用它，多个则报歧义
     *     ③ 都不中                   → 报「不认识这个名字」，并附上机器上有什么
     *
     * ②里「多个就报错」是刻意的：`微信` 与 `微信输入法` 同时存在时，
     * 猜错的代价是**打开了错误的 App 并继续往下操作**，而失败的代价只是重试一次。
     * 这与项目里「目标不唯一 = 证据缺口」是同一条判断（宁可停，不要猜）。
     */
    fun resolve(target: String, apps: List<AppEntry>): String {
        val wanted = normalize(target)
        if (wanted.isEmpty()) {
            throw ShadowActionFailed("启动应用需要应用名或包名，但拿到的是空字符串。")
        }

        val exact = apps.filter { normalize(it.label) == wanted }
        if (exact.size == 1) return exact[0].packageName
        if (exact.size > 1) throw ambiguous(target, exact)

        // 双向包含：模型爱写「拼多多应用」这类带后缀的名字，也可能只写「拼多多」
        // 而应用名是「拼多多商家版」。两个方向都算命中。
        val loose = apps.filter {
            val label = normalize(it.label)
            label.isNotEmpty() && (label in wanted || wanted in label)
        }
        if (loose.size == 1) return loose[0].packageName
        if (loose.size > 1) throw ambiguous(target, loose)

        throw ShadowActionFailed(
            "找不到应用「$target」：它既不是已安装的包名，也不匹配任何应用名。" +
                "这台机器上可启动的应用有 ${apps.size} 个${sample(apps)}"
        )
    }

    private fun normalize(value: String): String = value.trim().lowercase()

    private fun ambiguous(target: String, hits: List<AppEntry>): ShadowActionFailed =
        ShadowActionFailed(
            "应用名「$target」匹配到 ${hits.size} 个应用：" +
                hits.joinToString("、") { "${it.label}(${it.packageName})" } +
                "。请改用包名指定，或把应用名写全——猜一个的代价是操作了错误的 App。"
        )

    /**
     * 报错里附几个已装应用，让读日志的人（和模型）一眼知道**这台机器上有什么**。
     *
     * 只列前 8 个不是省事：全量列表会把报错顶成几百行，反而没人看。
     */
    private fun sample(apps: List<AppEntry>): String {
        if (apps.isEmpty()) return "（一个都没有——辅助功能/包可见性可能没生效）"
        val shown = apps.take(8).joinToString("、") { "${it.label}(${it.packageName})" }
        return if (apps.size > 8) "，例如：$shown …" else "：$shown"
    }
}
