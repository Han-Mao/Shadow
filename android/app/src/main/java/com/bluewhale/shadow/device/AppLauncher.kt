package com.bluewhale.shadow.device

import android.content.ComponentName
import android.content.Context
import android.content.Intent

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
        val intent = if (activity.isNullOrBlank()) {
            launchIntentForPackage(context, packageName)
        } else {
            Intent(Intent.ACTION_MAIN).setComponent(
                ComponentName(packageName, qualify(packageName, activity))
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
