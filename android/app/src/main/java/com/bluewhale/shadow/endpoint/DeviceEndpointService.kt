package com.bluewhale.shadow.endpoint

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log
import com.bluewhale.shadow.MainActivity
import com.bluewhale.shadow.R
import com.bluewhale.shadow.device.ScreenCapture

/**
 * 让设备端点**常驻**的前台服务（部署路线 B）。
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
 */
class DeviceEndpointService : Service() {

    companion object {
        private const val TAG = "ShadowEndpointService"
        private const val CHANNEL_ID = "shadow_endpoint"
        private const val NOTIFICATION_ID = 1001

        const val ACTION_START = "com.bluewhale.shadow.action.START_ENDPOINT"
        const val ACTION_STOP = "com.bluewhale.shadow.action.STOP_ENDPOINT"
        const val EXTRA_PORT = "shadow_port"
        const val EXTRA_TOKEN = "shadow_token"

        @Volatile
        var server: BridgeHttpServer? = null
            private set

        val isRunning: Boolean get() = server?.isRunning == true

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
                startForeground(NOTIFICATION_ID, buildNotification(port))

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
