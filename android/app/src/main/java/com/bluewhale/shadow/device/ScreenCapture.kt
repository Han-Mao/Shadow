package com.bluewhale.shadow.device

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.view.WindowManager
import java.io.ByteArrayOutputStream

/**
 * 屏幕采集：`MediaProjection` + `ImageReader`（V3.3 §4）。方案文档 §4 的那句话——
 * 「手机端可以直接拿屏幕，不需要 ADB」——就落在这个类上。
 *
 * ━━━ 为什么要「常驻一个 VirtualDisplay」，而不是每次截图新建一个 ━━━
 *
 * 直觉写法是「每次截图：getMediaProjection → createVirtualDisplay → 抓一帧 → 释放」。
 * 这在 Android 14+ 上会直接失败：**一次用户授权（一个 `MediaProjection` 令牌）
 * 只能创建一个 VirtualDisplay**，而且必须先 `registerCallback` 再创建，
 * 否则抛 `SecurityException`。
 *
 * 所以这里的形状是：授权时创建一次 VirtualDisplay 并常驻，之后每次截图只是从
 * `ImageReader` 取最新一帧。顺带解决了性能问题——每步观察新建/销毁一个投屏
 * 会明显变慢，而观察是整条链路里最高频的操作。
 *
 * ━━━ 尺寸必须和「上报给核心的屏幕尺寸」是同一份数字 ━━━
 *
 * 核心用 `screen_size` 做坐标归一化（`vision/grounding`），而 VLM 看到的像素来自
 * 这里的截图。两者若不一致（典型场景：任务中途旋转屏幕），**坐标会整体偏移**，
 * 而现象是「点了但没反应」——这是最难倒推回原因的一类失败。
 * 因此 [frameSize] 就是 [start] 时创建 VirtualDisplay 用的那对数字，
 * 桥的 `screen_size` 优先用它（见 `AndroidBridgeImpl`）。
 */
object ScreenCapture {

    private const val TAG = "ShadowScreenCapture"
    private const val VIRTUAL_DISPLAY_NAME = "shadow-capture"

    /** ImageReader 的缓冲数。2 够用：acquireLatestImage 会丢掉更早的帧。 */
    private const val IMAGE_BUFFER_COUNT = 2

    /** 刚授权后第一帧可能要等一会儿才来；这是等待上界与轮询间隔。 */
    private const val FIRST_FRAME_TIMEOUT_MS = 1_500L
    private const val FIRST_FRAME_POLL_MS = 50L

    @Volatile private var ready: Boolean = false
    @Volatile private var width: Int = 0
    @Volatile private var height: Int = 0

    private var projection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var reader: ImageReader? = null
    private var thread: HandlerThread? = null
    private var handler: Handler? = null

    @Synchronized
    fun isReady(): Boolean = ready

    /** 采集用的像素尺寸；未授权时返回 null。 */
    @Synchronized
    fun frameSize(): Pair<Int, Int>? = if (ready) width to height else null

    /**
     * 用用户在系统弹窗里给的授权建立投屏。
     *
     * 调用方是 `MainActivity` 的 `onActivityResult`（`createScreenCaptureIntent` 的结果）。
     */
    @Synchronized
    fun start(context: Context, resultCode: Int, data: Intent) {
        stop()

        val manager = context.getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        val created = manager.getMediaProjection(resultCode, data)
            ?: throw ShadowServiceUnavailable("创建 MediaProjection 失败（授权可能已经过期，请重新授权）")

        val thread = HandlerThread("shadow-capture").also { it.start() }
        val handler = Handler(thread.looper)

        // Android 14+ 要求先注册回调再创建 VirtualDisplay，否则 SecurityException。
        // 回调里主动 stop()：用户从系统状态栏撤回投屏时要立刻反映到 isReady()，
        // 否则我们会一直以为还能截图，直到某一帧超时——那会表现为「设备突然变卡」。
        created.registerCallback(
            object : MediaProjection.Callback() {
                override fun onStop() {
                    Log.i(TAG, "投屏被系统或用户停止")
                    stop()
                }
            },
            handler,
        )

        val (w, h) = displaySize(context)
        val density = context.resources.displayMetrics.densityDpi
        val reader = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, IMAGE_BUFFER_COUNT)
        val display = created.createVirtualDisplay(
            VIRTUAL_DISPLAY_NAME,
            w,
            h,
            density,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader.surface,
            null,
            handler,
        )

        this.projection = created
        this.virtualDisplay = display
        this.reader = reader
        this.thread = thread
        this.handler = handler
        this.width = w
        this.height = h
        this.ready = true
        Log.i(TAG, "投屏已建立：${w}x$h @${density}dpi")
    }

    @Synchronized
    fun stop() {
        // 先把引用取到局部、再把字段清空，**顺序不能反**。
        //
        // 原因是一个真实的递归：`projection.stop()` 会回调我们注册的 onStop，
        // 而那个回调里又调 `stop()`。如果此时字段还是非 null，就会一层层再进去，
        // 直到栈溢出——现象是「撤回投屏时应用闪退」，很难联想到是这个顺序。
        val projection = this.projection
        val display = virtualDisplay
        val reader = this.reader
        val thread = this.thread
        this.projection = null
        this.virtualDisplay = null
        this.reader = null
        this.thread = null
        this.handler = null
        ready = false

        runCatching { display?.release() }
        runCatching { reader?.close() }
        runCatching { projection?.stop() }
        thread?.quitSafely()
    }

    /**
     * 抓一帧，返回 PNG 字节。
     *
     * 失败一律抛异常而不是返回 null：核心侧的观察阶段需要区分「设备不可用」
     * 与「页面就是这样」。返回 null 会把它伪装成后者。
     */
    @Synchronized
    fun capturePng(): ByteArray {
        val reader = reader.takeIf { ready } ?: throw ShadowServiceUnavailable(
            "还没有授予屏幕捕获权限。请在 Shadow 应用里点「授权屏幕捕获」，" +
                "并在系统弹窗中选择「立即开始」。"
        )

        val image = acquireFrame(reader) ?: throw ShadowActionFailed(
            "投屏已建立但拿不到画面帧（等待 ${FIRST_FRAME_TIMEOUT_MS}ms 仍为空）。" +
                "常见原因：屏幕处于安全界面（锁屏/支付页面不允许截屏），或投屏刚被系统回收。"
        )

        return try {
            toPng(image)
        } finally {
            image.close()
        }
    }

    private fun acquireFrame(reader: ImageReader): Image? {
        // 刚建立投屏时第一帧还没来，直接 acquireLatestImage 会返回 null。
        // 这里等一小会儿；等不到就是真的有问题（安全界面 / 投屏被回收）。
        val deadline = System.currentTimeMillis() + FIRST_FRAME_TIMEOUT_MS
        while (true) {
            val image = runCatching { reader.acquireLatestImage() }.getOrNull()
            if (image != null) return image
            if (System.currentTimeMillis() >= deadline) return null
            try {
                Thread.sleep(FIRST_FRAME_POLL_MS)
            } catch (interrupted: InterruptedException) {
                Thread.currentThread().interrupt()
                return null
            }
        }
    }

    private fun toPng(image: Image): ByteArray {
        val plane = image.planes[0]
        val pixelStride = plane.pixelStride
        val rowStride = plane.rowStride
        // RGBA_8888 的每一行会被填充到对齐边界，直接按 width*4 解释会得到一个
        // **斜切**的图（每行向右偏移若干个像素）。坐标决策建立在这张图上，
        // 斜切的图会让 VLM 报出的坐标系统性偏移——而且看上去「只是有点糊」，
        // 很容易被当成模型不准，实际上是我们自己把图拼错了。
        val rowPadding = rowStride - pixelStride * image.width
        val paddedWidth = image.width + rowPadding / pixelStride

        val padded = Bitmap.createBitmap(paddedWidth, image.height, Bitmap.Config.ARGB_8888)
        padded.copyPixelsFromBuffer(plane.buffer)

        val cropped = if (paddedWidth == image.width) {
            padded
        } else {
            Bitmap.createBitmap(padded, 0, 0, image.width, image.height).also { padded.recycle() }
        }

        val out = ByteArrayOutputStream(1 shl 20)
        try {
            // PNG 会忽略 quality 参数；用 PNG 而不是 JPEG 是因为坐标决策需要
            // 文字边缘清晰（JPEG 的块效应会让小字号文字糊掉）。
            cropped.compress(Bitmap.CompressFormat.PNG, 100, out)
        } finally {
            cropped.recycle()
        }
        return out.toByteArray()
    }

    /**
     * 当前显示尺寸。
     *
     * 用 `currentWindowMetrics`（API 30+）而不是 `DisplayMetrics`：后者在多窗口/
     * 折叠屏上返回的是**物理屏**尺寸，而真实渲染区可能更小——归一化基准取错，
     * 整屏坐标都会偏。
     */
    fun displaySize(context: Context): Pair<Int, Int> {
        val windowManager = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val bounds = windowManager.currentWindowMetrics.bounds
            if (bounds.width() > 0 && bounds.height() > 0) {
                return bounds.width() to bounds.height()
            }
        }
        @Suppress("DEPRECATION")
        val metrics = context.resources.displayMetrics
        return metrics.widthPixels to metrics.heightPixels
    }

    /** 屏幕旋转。进 `<hierarchy rotation>`，与 uiautomator 的取值一致（0/1/2/3）。 */
    fun rotation(context: Context): Int {
        val windowManager = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        @Suppress("DEPRECATION")
        return windowManager.defaultDisplay.rotation
    }
}
