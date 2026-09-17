package com.bluewhale.shadow.endpoint

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import com.bluewhale.shadow.BuildConfig
import com.bluewhale.shadow.MainActivity
import com.bluewhale.shadow.R
import com.bluewhale.shadow.device.ScreenCapture

/**
 * 让设备端点**常驻**的前台服务（部署路线 B），同时承担投屏的建立。
 *
 * 为什么必须是前台服务而不是一个普通线程：
 *
 *  - 应用切到后台后，普通进程随时可能被系统回收。设备端点一断，Shadow Core 那边
 *    正在跑的任务会突然发现设备消失，被结算成 `DEVICE_UNAVAILABLE`；
 *    更糟的是「动作下发到一半」——那是一次**效果未知**的副作用（[51]/[27]）。
 *  - 前台服务带一条通知，用户随时能看到「现在 Shadow 正在被谁控制」。
 *    这不是仪式感：这个端点等于「操作这台手机」的能力，它不该在用户不知情时开着。
 *
 * 服务停止时**同时停掉投屏**（[ScreenCapture.stop]）：投屏是系统级资源，
 * 留着它会让状态栏一直显示「正在录制/投屏」，用户会以为应用还在工作。
 *
 * ━━━ 为什么投屏也归这个服务管（2026-09-16 真机上踩到）━━━
 *
 * targetSdk ≥ 34 时，Android 14 要求：**调 `getMediaProjection()` 之前必须先有一个
 * `mediaProjection` 类型的前台服务在运行**，否则抛 `SecurityException`。
 * 原来的写法是 `MainActivity.onActivityResult` 直接调 `ScreenCapture.start()` ——
 * 那一刻服务要么没启动、要么以 `dataSync` 类型在跑，两种都不满足这条要求。
 * 现象就是应用弹一句「建立屏幕捕获失败」，而链路上没有任何一处说出真正的原因。
 *
 * 所以投屏建立被搬进服务：先 `startForeground(…, mediaProjection)`，再建立投屏。
 * **顺序不能反** —— 这正是这条 Android 规则的全部要求。
 */
class DeviceEndpointService : Service() {

    companion object {
        private const val TAG = "ShadowEndpointService"
        private const val CHANNEL_ID = "shadow_endpoint"
        private const val NOTIFICATION_ID = 1001

        const val ACTION_START = "com.bluewhale.shadow.action.START_ENDPOINT"
        const val ACTION_STOP = "com.bluewhale.shadow.action.STOP_ENDPOINT"
        const val ACTION_START_PROJECTION = "com.bluewhale.shadow.action.START_PROJECTION"
        const val EXTRA_PORT = "shadow_port"
        const val EXTRA_TOKEN = "shadow_token"
        const val EXTRA_RESULT_CODE = "shadow_result_code"
        const val EXTRA_RESULT_DATA = "shadow_result_data"

        @Volatile
        var server: BridgeHttpServer? = null
            private set

        val isRunning: Boolean get() = server?.isRunning == true

        /** 最近一次用过的端口——单独建立投屏时，通知里也该有个像样的数字。 */
        @Volatile
        private var lastPort: Int = BuildConfig.DEFAULT_ENDPOINT_PORT

        fun start(context: Context, port: Int, token: String) {
            val intent = Intent(context, DeviceEndpointService::class.java)
                .setAction(ACTION_START)
                .putExtra(EXTRA_PORT, port)
                .putExtra(EXTRA_TOKEN, token)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        /**
         * 用系统弹窗给的授权建立投屏 —— 调用方是 `MainActivity.onActivityResult`。
         *
         * 走服务而不是直接调 `ScreenCapture`：Android 14+ 要求 `getMediaProjection()`
         * 之前已有 `mediaProjection` 类型的前台服务在跑，而「让服务进入前台」这件事
         * 只有服务自己能做到。见类注释。
         */
        fun startProjection(context: Context, resultCode: Int, data: Intent) {
            val intent = Intent(context, DeviceEndpointService::class.java)
                .setAction(ACTION_START_PROJECTION)
                .putExtra(EXTRA_RESULT_CODE, resultCode)
                .putExtra(EXTRA_RESULT_DATA, data)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, DeviceEndpointService::class.java).setAction(ACTION_STOP)
            )
        }
    }

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                shutdown()
                stopSelf()
                return START_NOT_STICKY
            }

            ACTION_START_PROJECTION -> return handleProjection(intent)

            else -> {
                val port = intent?.getIntExtra(EXTRA_PORT, 0)?.takeIf { it in 1..65535 }
                    ?: return START_NOT_STICKY
                val token = intent.getStringExtra(EXTRA_TOKEN).orEmpty()
                if (token.isEmpty()) {
                    Log.e(TAG, "没有令牌，拒绝启动端点")
                    stopSelf()
                    return START_NOT_STICKY
                }

                // 必须先 startForeground 再干活：Android 给 startForegroundService 之后的
                // 处理时间只有几秒，超时会 ANR/被杀。
                promoteToForeground(port, withProjection = false)

                try {
                    val created = server ?: BridgeHttpServer(applicationContext, port, token).also { server = it }
                    created.start()
                } catch (exc: Exception) {
                    // 端口被占用这类错误必须让用户看见——静默失败会让用户以为启动成功，
                    // 然后一直在 Core 那边排查「为什么连不上」。
                    Log.e(TAG, "设备端点启动失败：${exc.message}")
                    stopSelf()
                    return START_NOT_STICKY
                }
            }
        }
        // START_NOT_STICKY：不要在系统重启服务时自动拉起——用户没点「启动」就不该
        // 有监听端口存在。安全性优先于可用性。
        return START_NOT_STICKY
    }

    /**
     * 建立投屏。**这里的顺序就是那条 Android 规则的全部内容**：
     * 先把服务提到前台（带 `mediaProjection` 类型），再调 `getMediaProjection()`。
     */
    private fun handleProjection(intent: Intent): Int {
        val resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, 0)
        @Suppress("DEPRECATION") // getParcelableExtra(String) 的新签名要 API 33+，这里兼容低版本
        val data: Intent? = intent.getParcelableExtra(EXTRA_RESULT_DATA)
        if (data == null) {
            Log.e(TAG, "投屏授权数据缺失，无法建立投屏")
            stopSelf()
            return START_NOT_STICKY
        }

        promoteToForeground(lastPort, withProjection = true)

        return try {
            ScreenCapture.start(applicationContext, resultCode, data)
            Log.i(TAG, "投屏已建立（前台服务类型含 mediaProjection）")
            START_NOT_STICKY
        } catch (exc: Exception) {
            // 不再让它静默：界面那边只能看到一个 toast，真正的原因必须留在这里。
            Log.e(TAG, "建立屏幕捕获失败：${exc.javaClass.simpleName}: ${exc.message}", exc)
            START_NOT_STICKY
        }
    }

    /**
     * 进入前台并声明**当前实际用途**对应的类型。
     *
     * 两个类型各自对应一件事，Android 会校验它们与实际用途是否相符，所以不能一律
     * 都写上：端点在跑才要 `dataSync`，建立投屏时必须带 `mediaProjection`。
     * 两件事同时发生时两个都声明。
     */
    private fun promoteToForeground(port: Int, withProjection: Boolean) {
        lastPort = port
        val notification = buildNotification(port)
        val types = foregroundTypes(withProjection)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, types)
        } else {
            // Q 以下没有「带类型的前台服务」这回事，投屏在那些版本上也不需要前置条件。
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun foregroundTypes(withProjection: Boolean): Int {
        var types = 0
        if (isRunning) types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
        if (withProjection) types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
        // 兜底：types 为 0 时 startForeground 会抛，而走到这里说明总有一件事要做。
        return if (types != 0) types else ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
    }

    override fun onDestroy() {
        shutdown()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun shutdown() {
        server?.stop()
        server = null
        ScreenCapture.stop()
        stopForeground(STOP_FOREGROUND_REMOVE)
    }

    private fun createChannel() {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.notification_channel_name),
            // LOW：端点在跑是常态，不该每次启停都响一声。
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = getString(R.string.notification_channel_description)
        }
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(port: Int): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(getString(R.string.notification_text, port))
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setOngoing(true)
            .setContentIntent(open)
            .build()
    }
}
