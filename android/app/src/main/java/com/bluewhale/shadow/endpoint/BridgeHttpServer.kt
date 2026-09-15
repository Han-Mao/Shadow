package com.bluewhale.shadow.endpoint

import android.content.Context
import android.util.Log
import com.bluewhale.shadow.BuildConfig
import com.bluewhale.shadow.device.AndroidBridgeImpl
import com.bluewhale.shadow.device.ScreenCapture
import com.bluewhale.shadow.device.ShadowAccessibilityService
import com.bluewhale.shadow.device.ShadowActionFailed
import com.bluewhale.shadow.device.ShadowServiceUnavailable
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.Inet4Address
import java.net.NetworkInterface
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketException
import java.security.MessageDigest
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.concurrent.thread

/**
 * 设备端点：把 [AndroidBridgeImpl] 的 12 个能力暴露成一个极小的 HTTP 服务
 * （部署路线 B，协议见 `device/remote.py`）。
 *
 * ━━━ 为什么不用框架 ━━━
 *
 * 只需要「收一个 JSON、调一个方法、回一个 JSON」，为此引入一个 HTTP 框架
 * 会让 APK 多出几 MB 和一堆版本约束，而这段代码总共不到 200 行。
 * 依赖越少，能在越老的机器上构建。
 *
 * ━━━ 这个服务是**危险**的 ━━━
 *
 * 它等于「操作这台手机」的能力：
 *
 *     POST /bridge/tap  {"x":680,"y":1200}
 *
 * 就能点到屏幕上任意位置。所以有三道闸：
 *
 *   1. 必须带 `X-Shadow-Token`（在应用里随机生成一次，存在本机设置里）；
 *   2. 只在用户主动点「启动设备端点」时才监听，前台服务一停就关；
 *   3. 界面上明确写着「只在你信任的局域网里开启」。
 *
 * 这与仓库里 API 侧的安全口径一致（V3.2 §六/§七、[31]「部署安全三道闸」）：
 * 一个能改设备状态的入口不能默认开着，也不能只靠「别人不知道地址」。
 *
 * ━━━ 协议细节（与 `device/remote.py` 逐条对应）━━━
 *
 * | 情形 | HTTP | 回包 |
 * |---|---|---|
 * | 成功 | 200 | `{"ok":true,"value":...}` |
 * | 截图成功 | 200 | `Content-Type: image/png` + 裸 PNG 字节 |
 * | 权限没给 | 200 | `{"ok":false,"error":"...","code":"service_disabled"}` |
 * | 这次没成功 | 200 | `{"ok":false,"error":"...","code":"action_failed"}` |
 * | 令牌不对 | 401 | 不回业务内容 |
 * | 方法不存在 | 404 | `{"ok":false,"code":"unknown_method"}` |
 *
 * 「业务失败也回 200」是刻意的：HTTP 状态码表达的是**传输层**的结果，
 * 而「辅助功能没开」不是传输错误。把它塞进状态码会让客户端不得不同时解析
 * 状态码和 body 才能判断，而且 4xx/5xx 语义会被挤成一团。
 */
class BridgeHttpServer(
    private val context: Context,
    private val port: Int,
    private val token: String,
) {

    companion object {
        private const val TAG = "ShadowEndpoint"

        /** 一次请求的读取上界。请求体只有 JSON，正常几百字节。 */
        private const val MAX_BODY_BYTES = 256 * 1024

        private const val MAX_HEADER_BYTES = 8 * 1024

        /** 单连接的超时。必须大于最慢的一次设备操作（手势等待上界约 10s）。 */
        private const val SOCKET_TIMEOUT_MS = 30_000

        const val CODE_SERVICE_DISABLED = "service_disabled"
        const val CODE_ACTION_FAILED = "action_failed"
        const val CODE_BRIDGE_ERROR = "bridge_error"
        const val CODE_UNKNOWN_METHOD = "unknown_method"

        /** 本机在局域网里的 IPv4 地址；拿不到就退回回环（同机部署仍可用）。 */
        fun lanAddress(): String {
            return try {
                NetworkInterface.getNetworkInterfaces()
                    .toList()
                    .filter { it.isUp && !it.isLoopback }
                    .flatMap { it.inetAddresses.toList() }
                    .filterIsInstance<Inet4Address>()
                    .firstOrNull { !it.isLoopbackAddress }
                    ?.hostAddress
                    ?: "127.0.0.1"
            } catch (exc: Exception) {
                Log.w(TAG, "取本机局域网地址失败，退回 127.0.0.1：${exc.message}")
                "127.0.0.1"
            }
        }
    }

    private val bridge = AndroidBridgeImpl(context)

    // 线程池随 start/stop 一起建/销毁：否则反复启停会持续留下闲置的 daemon 线程。
    // 用 cached 而不是固定大小：并发请求本来就少（核心侧按设备串行化），
    // 固定池只会在某个慢手势上互相阻塞。
    @Volatile private var pool: ExecutorService? = null

    @Volatile private var serverSocket: ServerSocket? = null
    @Volatile private var acceptThread: Thread? = null

    @Volatile var lastError: String? = null
        private set

    val isRunning: Boolean get() = serverSocket != null

    fun url(): String = "http://${lanAddress()}:$port"

    /**
     * 开始监听。端口被占用等错误**直接抛**，由调用方呈现给用户——
     * 静默失败的话，用户会看到一个「启动成功」的界面，然后怎么都连不上。
     */
    fun start() {
        if (isRunning) return
        val socket = ServerSocket(port)
        val workers = Executors.newCachedThreadPool { runnable ->
            Thread(runnable, "shadow-endpoint-worker").apply { isDaemon = true }
        }
        serverSocket = socket
        pool = workers
        lastError = null
        acceptThread = thread(name = "shadow-endpoint-accept", isDaemon = true) {
            Log.i(TAG, "设备端点已监听 $port")
            while (serverSocket != null) {
                val client = try {
                    socket.accept()
                } catch (exc: SocketException) {
                    break // stop() 关掉了 socket
                } catch (exc: Exception) {
                    lastError = exc.message
                    continue
                }
                runCatching { workers.execute { serve(client) } }
            }
        }
    }

    fun stop() {
        val socket = serverSocket ?: return
        serverSocket = null
        runCatching { socket.close() }
        acceptThread = null
        // shutdown 而不是 shutdownNow：正在执行的手势不该被半路掐掉
        // （那会让设备停在半个动作上，核心侧看到的是「效果未知」）。
        pool?.shutdown()
        pool = null
        Log.i(TAG, "设备端点已停止")
    }

    // ---- 连接处理 ----

    private fun serve(socket: Socket) {
        try {
            socket.soTimeout = SOCKET_TIMEOUT_MS
            socket.use { client ->
                val input = BufferedInputStream(client.getInputStream())
                val output = BufferedOutputStream(client.getOutputStream())

                val head = readHead(input) ?: return
                val (requestLine, headers) = parseHead(head)
                val (method, path) = parseRequestLine(requestLine) ?: run {
                    writeJson(output, 400, failure("请求行无法解析", CODE_BRIDGE_ERROR))
                    return
                }

                if (!tokenMatches(headers["x-shadow-token"])) {
                    Log.w(TAG, "拒绝一次令牌不匹配的请求：$method $path")
                    writeJson(output, 401, failure("令牌不匹配", "unauthorized"))
                    return
                }

                val body = readBody(input, headers)
                if (body == null) {
                    writeJson(output, 413, failure("请求体过大", CODE_BRIDGE_ERROR))
                    return
                }

                when {
                    method == "GET" && path == "/health" -> writeJson(output, 200, health())
                    method == "POST" && path.startsWith("/bridge/") ->
                        dispatch(output, path.removePrefix("/bridge/"), body)
                    else -> writeJson(output, 404, failure("没有这个接口：$method $path", CODE_UNKNOWN_METHOD))
                }
            }
        } catch (exc: Exception) {
            // 一个连接出错不能影响后续连接；记下来，方便在界面上显示最后一次错误。
            lastError = "${exc.javaClass.simpleName}: ${exc.message}"
            Log.w(TAG, "处理连接时出错：$lastError")
        }
    }

    private fun health(): JSONObject {
        val payload = JSONObject()
            .put("state", bridge.state())
            .put("accessibility", ShadowAccessibilityService.isConnected())
            .put("projection", ScreenCapture.isReady())
            .put("screen", screenSizeJson())
            .put("port", port)
            .put("protocol", 1)
        return success(payload)
    }

    private fun screenSizeJson(): JSONObject {
        val (width, height) = bridge.screen_size()
        return JSONObject().put("width", width).put("height", height)
    }

    /**
     * 一个方法一处映射，且**写死名字**——不做基于反射的自动分发。
     *
     * 反射看起来更优雅，但它会把「协议」藏进运行时：名字拼错时不会编译失败，
     * 而是收到 404 或者更糟的静默默认值。这里每个名字都出现在两张表里
     * （本 when、Python 侧 `remote.py` 的调用点），两边都有测试钉住。
     */
    private fun dispatch(output: OutputStream, method: String, body: ByteArray) {
        try {
            val payload = if (body.isEmpty()) JSONObject() else JSONObject(String(body, Charsets.UTF_8))
            val value: Any? = when (method) {
                "screen_size" -> bridge.screen_size()
                // 读不到窗口信息时回 ["", ""]（端口约定：**不抛异常**，
                // 让上层自己区分「读不到」与「读到了但为空」）。
                // 不要回 null——那会让 Python 侧多出一条「null 也算异常」的路径，
                // 而端口约定恰恰要求它不抛。
                "current_focus" -> bridge.current_focus()
                "dump_ui" -> bridge.dump_ui()
                "screenshot_bytes" -> {
                    // 截图是唯一的大包，走裸字节而不是 base64（省 33% 传输量）
                    writePng(output, bridge.screenshot_bytes())
                    return
                }
                "state" -> bridge.state()
                "tap" -> {
                    bridge.tap(payload.getInt("x"), payload.getInt("y")); null
                }
                "long_press" -> {
                    bridge.long_press(payload.getInt("x"), payload.getInt("y"), payload.getInt("duration_ms")); null
                }
                "swipe" -> {
                    bridge.swipe(
                        payload.getInt("x1"), payload.getInt("y1"),
                        payload.getInt("x2"), payload.getInt("y2"),
                        payload.getInt("duration_ms"),
                    ); null
                }
                "set_text" -> {
                    bridge.set_text(payload.getString("value")); null
                }
                "press_back" -> {
                    bridge.press_back(); null
                }
                "press_home" -> {
                    bridge.press_home(); null
                }
                "launch" -> {
                    val activity = if (payload.isNull("activity")) null else payload.optString("activity")
                    bridge.launch(payload.getString("package"), activity); null
                }
                else -> {
                    writeJson(output, 404, failure("设备端点没有这个方法：$method", CODE_UNKNOWN_METHOD))
                    return
                }
            }
            writeJson(output, 200, success(value))
        } catch (exc: ShadowServiceUnavailable) {
            // 权限没给：这不是「重试就好」，必须让用户去手机上开权限。
            // 用独立的 code 表达，客户端才能把它映射成「需要用户处理」而不是「设备故障」。
            writeJson(output, 200, failure(exc.message ?: "设备未就绪", CODE_SERVICE_DISABLED))
        } catch (exc: ShadowActionFailed) {
            writeJson(output, 200, failure(exc.message ?: "设备操作失败", CODE_ACTION_FAILED))
        } catch (exc: Exception) {
            writeJson(output, 200, failure("${exc.javaClass.simpleName}: ${exc.message}", CODE_BRIDGE_ERROR))
        }
    }

    // ---- HTTP 细节 ----

    private fun readHead(input: InputStream): String? {
        val buffer = ByteArrayOutputStream(512)
        var matched = 0
        while (buffer.size() < MAX_HEADER_BYTES) {
            val byte = input.read()
            if (byte < 0) return if (buffer.size() == 0) null else buffer.toString("ISO-8859-1")
            buffer.write(byte)
            matched = when {
                matched == 0 && byte == 13 -> 1
                matched == 1 && byte == 10 -> 2
                matched == 2 && byte == 13 -> 3
                matched == 3 && byte == 10 -> return buffer.toString("ISO-8859-1")
                else -> 0
            }
        }
        return buffer.toString("ISO-8859-1")
    }

    private fun parseHead(head: String): Pair<String, Map<String, String>> {
        val lines = head.split("\r\n").filter { it.isNotEmpty() }
        val requestLine = lines.firstOrNull().orEmpty()
        val headers = lines.drop(1).mapNotNull { line ->
            val index = line.indexOf(':')
            if (index <= 0) null
            else line.substring(0, index).trim().lowercase() to line.substring(index + 1).trim()
        }.toMap()
        return requestLine to headers
    }

    private fun parseRequestLine(line: String): Pair<String, String>? {
        val parts = line.split(' ')
        if (parts.size < 2) return null
        return parts[0].uppercase() to parts[1].substringBefore('?')
    }

    private fun readBody(input: InputStream, headers: Map<String, String>): ByteArray? {
        val length = headers["content-length"]?.toIntOrNull() ?: 0
        if (length <= 0) return ByteArray(0)
        if (length > MAX_BODY_BYTES) return null
        val body = ByteArray(length)
        var read = 0
        while (read < length) {
            val count = input.read(body, read, length - read)
            if (count < 0) break
            read += count
        }
        return if (read == length) body else body.copyOf(read)
    }

    private fun tokenMatches(provided: String?): Boolean {
        val expected = token
        if (expected.isEmpty()) return false
        if (provided == null) return false
        // 定长比较，避免「响应时间随匹配前缀变化」这种低成本的旁路。
        // 局域网上这不是主要威胁，但代价只有一行。
        return MessageDigest.isEqual(provided.toByteArray(), expected.toByteArray())
    }

    private fun success(value: Any?): JSONObject =
        JSONObject().put("ok", true).put("value", jsonValue(value))

    private fun failure(error: String, code: String): JSONObject =
        JSONObject().put("ok", false).put("error", error).put("code", code)

    /** Kotlin 的数组/基本类型要先转成 org.json 认识的东西。 */
    private fun jsonValue(value: Any?): Any = when (value) {
        null -> JSONObject.NULL
        is JSONObject -> value
        is IntArray -> JSONArray(value.toList())
        is Array<*> -> JSONArray(value.toList())
        is List<*> -> JSONArray(value)
        else -> value
    }

    private fun writeJson(output: OutputStream, status: Int, body: JSONObject) {
        val bytes = body.toString().toByteArray(Charsets.UTF_8)
        writeResponse(output, status, "application/json; charset=utf-8", bytes)
    }

    private fun writePng(output: OutputStream, png: ByteArray) {
        writeResponse(output, 200, "image/png", png)
    }

    private fun writeResponse(output: OutputStream, status: Int, contentType: String, body: ByteArray) {
        val header = StringBuilder()
            .append("HTTP/1.1 ").append(status).append(' ').append(statusText(status)).append("\r\n")
            .append("Content-Type: ").append(contentType).append("\r\n")
            .append("Content-Length: ").append(body.size).append("\r\n")
            .append("Cache-Control: no-store\r\n")
            // 每次请求一条连接：省掉 keep-alive 的状态机，而这点开销在局域网上无所谓。
            .append("Connection: close\r\n\r\n")
            .toString()
        output.write(header.toByteArray(Charsets.ISO_8859_1))
        output.write(body)
        output.flush()
    }

    private fun statusText(status: Int): String = when (status) {
        200 -> "OK"
        400 -> "Bad Request"
        401 -> "Unauthorized"
        404 -> "Not Found"
        413 -> "Payload Too Large"
        else -> "Error"
    }

    /** 给界面用的自检：端点是否可达、两个权限是否就绪。 */
    fun describe(): String {
        val state = bridge.state()
        val parts = mutableListOf<String>()
        parts += if (isRunning) "运行中" else "已停止"
        parts += "辅助功能=" + if (ShadowAccessibilityService.isConnected()) "开" else "关"
        parts += "投屏=" + if (ScreenCapture.isReady()) "开" else "关"
        parts += "state=$state"
        parts += "构建 ${BuildConfig.VERSION_NAME}(${BuildConfig.VERSION_CODE})"
        return parts.joinToString("，")
    }
}
