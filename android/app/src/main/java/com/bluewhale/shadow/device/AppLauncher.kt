package com.bluewhale.shadow.device

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager

/**
 * 启动应用：`PackageManager` + `startActivity`（V3.3 §6）。
 *
 * 方案文档 §6 的对照很直白：
 *
 *     adb shell monkey -p com.tencent.mm 1     ← 旧路：要 PC 宿主
 *     PackageManager + Intent                  ← 新路：手机自己就能做到
 *
 * 语义与 ADB 侧**刻意对齐**（`device/adb.py` 的 `launch`）：
 *
 *     launch(pkg, activity)   pkg/activity 显式启动
 *     launch(pkg, null)       按包名启动主界面（= ADB 侧的 `monkey -p pkg 1`）
 *
 * 对齐的原因不是好看：模型给出的 `Action(LAUNCH, "com.taobao.taobao/.DetailActivity")`
 * 在两个后端上必须打开**同一样东西**，否则「换个后端」就会变成「换个行为」，
 * 而验证步骤、风险判定都建立在这个行为之上。
 */
object AppLauncher {

    fun launch(context: Context, packageName: String, activity: String?) {
        if (packageName.isBlank()) {
            throw ShadowActionFailed("启动应用需要包名")
        }
        // 模型给的常常是**应用名**（「拼多多」）而不是包名。在这层翻译存在之前，
        // 它会被原样送进 getLaunchIntentForPackage，失败后报「应用可能未安装」——
        // 而应用明明装着，于是排查方向被彻底带偏（2026-09-17 真机任务踩到）。
        val resolved = resolvePackageName(context, packageName)
        val intent = if (activity.isNullOrBlank()) {
            launchIntentForPackage(context, resolved)
        } else {
            Intent(Intent.ACTION_MAIN).setComponent(
                ComponentName(resolved, qualify(resolved, activity))
            )
        }

        // FLAG_ACTIVITY_NEW_TASK：设备层不是 Activity，从非 Activity 上下文启动必须加它，
        // 否则抛 AndroidRuntimeException。这不是可选项。
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
        try {
            context.startActivity(intent)
        } catch (exc: Exception) {
            throw ShadowActionFailed(
                "启动 $packageName${activity?.let { "/$it" } ?: ""} 失败：${exc.message}。" +
                    "常见原因：应用未安装、该 Activity 未导出、或组件名写法不对。"
            )
        }
    }

    /**
     * 把「模型嘴里的名字」解析成真包名。
     *
     * **先认包名，再认应用名**——顺序写死而不是碰运气。反过来的话，一个恰好与应用名
     * 同形的包名会被丢进模糊匹配，命中一堆候选后报歧义。现实中罕见，
     * 但「先查什么」在两可时必须是一条明确规则，而不是看谁先被想到。
     *
     * 已经是装好的包名就**原样返回**，不做任何规范化：包名是唯一标识，
     * 对它做「聪明的修正」只会引入第二种失败方式。
     */
    private fun resolvePackageName(context: Context, target: String): String {
        if (isInstalled(context, target)) return target
        return AppResolver.resolve(target, launcherApps(context))
    }

    private fun isInstalled(context: Context, packageName: String): Boolean =
        try {
            context.packageManager.getPackageInfo(packageName, 0)
            true
        } catch (exc: PackageManager.NameNotFoundException) {
            false
        }

    /**
     * 候选集 = **有启动入口**的应用，不是全部已安装包。
     *
     * 拿全部包会让「设置」「日历」这类系统组件参与匹配——它们没有 LAUNCHER 入口、
     * 启动了也必然失败；更要紧的是会让重名判定（歧义）被一堆用户根本看不见的包污染，
     * 于是「微信」这种本该唯一的名字被判成歧义。
     *
     * `QUERY_ALL_PACKAGES` + `<queries>` 已在 manifest 里声明，
     * 否则 Android 11+ 这里会安静地返回空列表（见 AndroidManifest.xml 顶部说明）。
     */
    private fun launcherApps(context: Context): List<AppResolver.AppEntry> {
        val pm = context.packageManager
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        return pm.queryIntentActivities(intent, 0).map {
            AppResolver.AppEntry(it.activityInfo.packageName, it.loadLabel(pm).toString())
        }
    }

    private fun launchIntentForPackage(context: Context, packageName: String): Intent =
        context.packageManager.getLaunchIntentForPackage(packageName)
            ?: throw ShadowActionFailed(
                "找不到 $packageName 的启动入口：应用可能未安装，或它的启动 Activity 不响应 MAIN/LAUNCHER。"
            )

    /**
     * 把 `activity` 补成完整类名，与 `am start -n pkg/act` 的处理一致：
     *
     *     .DetailActivity   → com.taobao.taobao.DetailActivity   （相对写法，最常见）
     *     DetailActivity    → com.taobao.taobao.DetailActivity
     *     com.x.y.Detail    → 原样
     *
     * 不做这一步的后果很具体：模型给出的常常是 `/.ui.LauncherUI` 这种相对写法，
     * 直接塞进 ComponentName 会得到一个不存在的组件 → 启动静默失败。
     */
    private fun qualify(packageName: String, activity: String): String {
        val trimmed = activity.trim()
        return when {
            trimmed.startsWith(".") -> packageName + trimmed
            trimmed.contains(".") -> trimmed
            else -> "$packageName.$trimmed"
        }
    }
}
