package com.bluewhale.shadow

import android.Manifest
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import com.bluewhale.shadow.device.ScreenCapture
import com.bluewhale.shadow.device.ShadowAccessibilityService
import com.bluewhale.shadow.endpoint.BridgeHttpServer
import com.bluewhale.shadow.endpoint.DeviceEndpointService

/**
 * 设备端点页（部署路线 B）。
 *
 * 这一页**刻意不是聊天界面**：路线 B 下手机不接收任务，任务由 Shadow Core 下发
 * （见 `android/README.md` 的「路线选择」）。它要回答三个问题，所以只有三组控件：
 *
 *   1. 权限齐了吗？（辅助功能、屏幕捕获）
 *   2. 端点在跑吗？地址是什么？
 *   3. Core 那边该填哪三行环境变量？
 *
 * 「权限」这块用**跳系统设置 + 回来时自动刷新**，不做状态缓存：
 * 用户可能在设置里开了又关，缓存出来的「已开启」会让他去 Core 那边排查一个
 * 根本不存在的问题。
 */
class MainActivity : Activity() {

    companion object {
        private const val REQUEST_PROJECTION = 1001
        private const val REQUEST_NOTIFICATIONS = 1002
        private const val PREFS = "shadow_endpoint"
        private const val KEY_TOKEN = "token"
    }

    private lateinit var accessibilityState: TextView
    private lateinit var projectionState: TextView
    private lateinit var readinessState: TextView
    private lateinit var endpointState: TextView
    private lateinit var endpointAddress: TextView
    private lateinit var endpointUsage: TextView
    private lateinit var toggleButton: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        accessibilityState = findViewById(R.id.accessibility_state)
        projectionState = findViewById(R.id.projection_state)
        readinessState = findViewById(R.id.readiness_state)
        endpointState = findViewById(R.id.endpoint_state)
        endpointAddress = findViewById(R.id.endpoint_address)
        endpointUsage = findViewById(R.id.endpoint_usage)
        toggleButton = findViewById(R.id.btn_toggle_endpoint)

        findViewById<Button>(R.id.btn_accessibility).setOnClickListener {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }
        findViewById<Button>(R.id.btn_projection).setOnClickListener { requestProjection() }
        toggleButton.setOnClickListener { toggleEndpoint() }
        findViewById<Button>(R.id.btn_copy_usage).setOnClickListener { copyUsage() }

        requestNotificationPermissionIfNeeded()
        refresh()
    }

    override fun onResume() {
        super.onResume()
        // 从系统设置回来时状态会变（用户刚开了辅助功能），每次都重读。
        refresh()
    }

    // ---- 权限 ----

    private fun requestProjection() {
        val manager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        @Suppress("DEPRECATION")
        startActivityForResult(manager.createScreenCaptureIntent(), REQUEST_PROJECTION)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQUEST_PROJECTION) return
        if (resultCode != RESULT_OK || data == null) {
            toast("没有授权屏幕捕获——没有它 Shadow 看不到屏幕，只能执行动作")
            return
        }
        // 交给服务去建立：Android 14+ 要求 `getMediaProjection()` 之前已经有
        // `mediaProjection` 类型的前台服务在运行，而「进入前台」这件事只有服务
        // 自己能做到（见 `DeviceEndpointService.handleProjection`）。
        // 在这里直接调 `ScreenCapture.start()` 会抛 SecurityException ——
        // 现象就是应用回一句「建立屏幕捕获失败」，而链路上没人说得出原因。
        DeviceEndpointService.startProjection(this, resultCode, data)
        // 服务是「先 startForeground 再建立投屏」，比同步调用慢一步，所以延后刷新。
        toggleButton.postDelayed({ refresh() }, 800)
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) return
        requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQUEST_NOTIFICATIONS)
    }

    // ---- 端点 ----

    private fun toggleEndpoint() {
        if (DeviceEndpointService.isRunning) {
            DeviceEndpointService.stop(this)
            refresh()
            return
        }
        if (!ShadowAccessibilityService.isConnected() && !ScreenCapture.isReady()) {
            // 仍然允许启动（用户可以先把端点起起来、再逐步给权限），但要说清楚。
            toast("还没开辅助功能与屏幕捕获——端点能连上，但任何动作都会失败")
        }
        DeviceEndpointService.start(this, BuildConfig.DEFAULT_ENDPOINT_PORT, token())
        // 前台服务是异步起来的，稍等一下再刷新界面（refresh 会读服务里的真实状态）。
        toggleButton.postDelayed({ refresh() }, 300)
    }

    /** 令牌只在本机生成一次；Core 那边必须填同一串。 */
    private fun token(): String {
        val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
        prefs.getString(KEY_TOKEN, null)?.let { return it }
        val generated = buildString {
            val alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
            repeat(24) { append(alphabet.random()) }
        }
        prefs.edit().putString(KEY_TOKEN, generated).apply()
        return generated
    }

    private fun copyUsage() {
        val text = usageText()
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("shadow endpoint", text))
        toast("已复制到剪贴板")
    }

    private fun usageText(): String {
        val address = endpointUrl()
        return "SHADOW_DEVICE_BACKEND=android\n" +
            "SHADOW_ANDROID_BRIDGE_URL=$address\n" +
            "SHADOW_ANDROID_BRIDGE_TOKEN=${token()}"
    }

    private fun endpointUrl(): String = "http://${BridgeHttpServer.lanAddress()}:${BuildConfig.DEFAULT_ENDPOINT_PORT}"

    // ---- 界面刷新 ----

    /**
     * 一站式结论（v4.2 §三 P1 的 SetupWizard）。
     *
     * 逐项状态是给排查用的，这一行是给「能不能开始用」用的——两者都要有：
     * 演示时最怕的是对着两行「未开启 / 已就绪」自己判断，还判断错。
     *
     * 查**三项**，刻意**不查悬浮窗**（审核的建议里列了 Overlay）：
     * 这个应用从不申请 `SYSTEM_ALERT_WINDOW`——设备层只走 AccessibilityService 与
     * MediaProjection（见 `AndroidManifest.xml` 顶部那段「刻意不申请的权限」）。
     * 列一项自己根本不需要的权限，只会让用户去开一个对本应用毫无作用的东西。
     *
     * 「Core 连得上吗」在这里**无法自检**：连接是 Core 主动发起的（手机只是监听）。
     * 能自检的是「端点有没有在监听」——端口被占等原因由
     * `DeviceEndpointService.server?.lastError` 带出来（见 `refresh` 末尾）。
     */
    private fun readinessText(): String {
        val missing = buildList {
            if (!ShadowAccessibilityService.isConnected()) add(getString(R.string.label_accessibility_state))
            if (!ScreenCapture.isReady()) add(getString(R.string.label_projection_state))
        }
        return when {
            missing.isNotEmpty() -> getString(R.string.state_missing_permissions, missing.joinToString("、"))
            !DeviceEndpointService.isRunning -> getString(R.string.state_ready_endpoint_stopped)
            else -> getString(R.string.state_all_ready)
        }
    }

    private fun refresh() {
        readinessState.text = readinessText()
        accessibilityState.text = getString(R.string.label_accessibility_state) + "：" +
            if (ShadowAccessibilityService.isConnected()) getString(R.string.state_ready) else getString(R.string.state_missing)
        projectionState.text = getString(R.string.label_projection_state) + "：" +
            if (ScreenCapture.isReady()) getString(R.string.state_ready) else getString(R.string.state_missing)

        val running = DeviceEndpointService.isRunning
        endpointState.text = getString(R.string.label_endpoint_state) + "：" +
            if (running) getString(R.string.state_running) else getString(R.string.state_stopped)
        endpointAddress.text = getString(R.string.label_endpoint_address) + "：" + endpointUrl() +
            "\n" + getString(R.string.label_endpoint_token) + "：" + token()
        toggleButton.text = getString(
            if (running) R.string.action_stop_endpoint else R.string.action_start_endpoint
        )

        val usage = getString(R.string.hint_endpoint_usage, endpointUrl(), token())
        endpointUsage.text = usage

        // 端点启动失败（端口被占等）时把原因带出来——否则用户只看到「已停止」。
        DeviceEndpointService.server?.lastError?.let { endpointState.append("\n最近一次错误：$it") }
    }

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }
}
