package com.printgw.selfservice

import android.content.Context

/**
 * 服务器地址与访问口令的本地存储。
 *
 * 存 `[scheme://]host[:port]`，例如：
 *   - `192.168.1.100:8080`                内网（没写协议 = 默认 http）
 *   - `https://print.example.com:8443`      公网（网关的 TLS 端口）
 *
 * **协议必须跟着地址走，不能统一钉死成 http。** 网关内网是明文 8080、公网是
 * TLS 8443，把 https 写成 http 就是拿明文去打一个只会说 TLS 的端口，
 * 握手失败、连接被关，WebView 拿不到任何响应 —— 表现为整页黑屏。
 */
class Prefs(context: Context) {

    private val sp = context.applicationContext
        .getSharedPreferences("print_gateway", Context.MODE_PRIVATE)

    fun host(): String = sp.getString(KEY_HOST, "").orEmpty()

    fun token(): String = sp.getString(KEY_TOKEN, "").orEmpty()

    /** 是否已经填过地址。没填过就在启动时弹设置框，而不是拿一个空地址去加载。 */
    fun configured(): Boolean = sp.getBoolean(KEY_CONFIGURED, false) && host().isNotBlank()

    /**
     * 规范化基地址，如 `http://192.168.1.100:8080`（无末尾斜杠）。
     *
     * 实现见 [normalizeBaseUrl]（纯函数，JVM 可测；这里只是取数外壳）。
     */
    fun baseUrl(): String = normalizeBaseUrl(host())

    fun save(host: String, token: String) {
        sp.edit()
            .putString(KEY_HOST, host.trim())
            .putString(KEY_TOKEN, token.trim())
            .putBoolean(KEY_CONFIGURED, true)
            .apply()
    }

    companion object {
        private const val KEY_HOST = "host"
        private const val KEY_TOKEN = "token"
        private const val KEY_CONFIGURED = "configured"

        /** 输入框的预填值，不代表此刻一定可达。 */
        const val DEFAULT_HOST = "192.168.1.100:8080"

        /**
         * 把存储里的地址规范成可用的基地址（去掉末尾斜杠）。
         *
         * 规则：**显式写了协议就照用**（大小写不敏感，输出统一小写），没写才按
         * 内网默认补 `http://`。所以：
         *   `192.168.1.100:8080`            → `http://192.168.1.100:8080`
         *   `HTTPS://print.example.com:8443/` → `https://print.example.com:8443`
         *
         * 第二条是关键：网关公网端口（8443）是 **TLS**，而内网 8080 是明文。
         * 以前这里把协议统一钉成 `http://`，于是扫公网连接码后 WebView 拿明文
         * 去打 TLS 端口 —— 握手失败、连接被关、整页黑屏。老配置（没写协议）
         * 走的是第一条分支，行为完全不变。
         *
         * 纯函数，JVM 上可测。
         */
        fun normalizeBaseUrl(host: String): String {
            val h = host.trim().removeSuffix("/")
            for (p in listOf("http://", "https://")) {
                if (h.startsWith(p, ignoreCase = true)) {
                    // 协议统一成小写：用户/AI 复制粘贴来的 `HTTPS://` 交给 WebView
                    // 并不稳妥，Uri.parse 也不会帮你归一化
                    return p + h.substring(p.length)
                }
            }
            return "http://$h"
        }

        /**
         * 把二维码里的「连接码」解析成 `[scheme://]host[:port]` + 口令。
         *
         * 支持的形态（网关 `/admin` 页「公网访问链接」生成的就是第三条）：
         *   http://192.168.1.100:8080/
         *   http://192.168.1.100:8080/?t=ABC123
         *   https://print.example.com:8443/?t=ABC123    ← 公网，协议原样保留
         * 也兼容裸 host:port（没写协议，交由 baseUrl() 补 http）与
         * `?token=` 两种口令参数名。
         *
         * **scheme 必须保留**：公网那条路是 TLS，丢掉 https 就等于把它降级
         * 成明文去打一个只会说 TLS 的端口，必然打不通（曾表现为扫码后黑屏）。
         *
         * 解析失败（既不是 http(s) URL 也不是 host:port 裸串）返回 null，
         * 让调用方回退到「不是有效连接码」提示。纯函数，便于单测。
         */
        fun parseConnectUrl(raw: String): Pair<String, String>? {
            val s = raw.trim()
            if (s.isEmpty()) return null

            val scheme = when {
                s.startsWith("https://", true) -> "https://"
                s.startsWith("http://", true) -> "http://"
                else -> ""
            }
            val proto = s.substring(scheme.length)
            // 去掉 scheme 后，host:port 可能带路径（/、/?t=…）或查询串（裸 host:port?token=x）
            var cut = proto.indexOf('/')
            val qm = proto.indexOf('?')
            if (qm >= 0 && (cut < 0 || qm < cut)) cut = qm
            val hostPort = if (cut >= 0) proto.substring(0, cut) else proto
            // 校验 hostPort 至少像 host 或 host:port（含点或冒号，避免把垃圾当地址）
            if (!hostPort.contains('.') && !hostPort.contains(':')) return null
            if (hostPort.isEmpty()) return null

            // 口令：从原始串的 query 里取 ?t= 或 ?token=
            var token = ""
            val q = s.indexOf('?')
            if (q >= 0) {
                val query = s.substring(q + 1)
                for (kv in query.split('&')) {
                    val eq = kv.indexOf('=')
                    if (eq < 0) continue
                    val k = kv.substring(0, eq).trim().lowercase()
                    val v = kv.substring(eq + 1).trim()
                    if (k == "t" || k == "token") token = v
                }
            }
            return (scheme + hostPort) to token
        }
    }
}
