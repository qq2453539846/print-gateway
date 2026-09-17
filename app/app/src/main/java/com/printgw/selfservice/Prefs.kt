package com.printgw.selfservice

import android.content.Context

/**
 * 服务器地址与访问口令的本地存储。
 *
 * 只存 host[:port]，协议固定 http —— 打印网关只在局域网提供 HTTP，
 * 让用户填协议只会多出一个出错的地方。
 */
class Prefs(context: Context) {

    private val sp = context.applicationContext
        .getSharedPreferences("print_gateway", Context.MODE_PRIVATE)

    fun host(): String = sp.getString(KEY_HOST, "").orEmpty()

    fun token(): String = sp.getString(KEY_TOKEN, "").orEmpty()

    /** 是否已经填过地址。没填过就在启动时弹设置框，而不是拿一个空地址去加载。 */
    fun configured(): Boolean = sp.getBoolean(KEY_CONFIGURED, false) && host().isNotBlank()

    /** 规范化基地址，如 http://192.168.1.100:8080（无末尾斜杠）。 */
    fun baseUrl(): String {
        var h = host().trim().removeSuffix("/")
        for (p in listOf("http://", "https://", "HTTP://", "HTTPS://")) {
            if (h.startsWith(p)) { h = h.substring(p.length); break }
        }
        return "http://$h"
    }

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
         * 把二维码里的「连接码」解析成 host[:port] + 口令。
         *
         * 支持的形态（服务端 deploy_gateway.py 生成的即第一种）：
         *   http://192.168.1.100:8080/
         *   http://192.168.1.100:8080/?t=ABC123
         *   https://…（同样解析，baseUrl() 会再固定成 http）
         * 也兼容裸 host:port（没写协议）与 `?token=` 两种口令参数名。
         *
         * 解析失败（既不是 http(s) URL 也不是 host:port 裸串）返回 null，
         * 让调用方回退到「不是有效连接码」提示。纯函数，便于单测。
         */
        fun parseConnectUrl(raw: String): Pair<String, String>? {
            var s = raw.trim()
            if (s.isEmpty()) return null

            val proto = when {
                s.startsWith("http://", true) -> s.substring(7)
                s.startsWith("https://", true) -> s.substring(8)
                else -> s
            }
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
            return hostPort to token
        }
    }
}
