package com.printgw.selfservice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Prefs 的两个纯函数测试（不依赖 Android 框架，JVM 上跑）。
 *
 * - `parseConnectUrl`：把二维码里的连接码还原成 `[scheme://]host[:port]` + 口令，
 *   是「扫码连接」的核心解析。解析错一步，用户扫出来的就是错地址/丢口令/丢协议。
 * - `normalizeBaseUrl`：把存储里的地址规范成真正去请求的基地址。
 *
 * **协议这两个字必须守住**：网关内网是明文 8080、公网是 TLS 8443。曾经这两处
 * 都把 scheme 丢掉 / 钉死成 http，于是扫「公网访问链接」之后 WebView 拿明文去打
 * TLS 端口 —— 握手失败、连接被关、整页黑屏，而内网用手输 IP 又一切正常，
 * 表现为「只有外网不能用」。下面的 https 用例就是那次事故的回归。
 */
class PrefsTest {

    // ------------------------------------------------------------ 连接码解析

    @Test
    fun bareHostPort() {
        // 没写协议：原样返回，由 normalizeBaseUrl 补 http（内网扫码即用）
        assertEquals("192.168.1.100:8080" to "", Prefs.parseConnectUrl("192.168.1.100:8080"))
    }

    @Test
    fun httpSchemeIsKept() {
        assertEquals("http://192.168.1.100:8080" to "",
            Prefs.parseConnectUrl("http://192.168.1.100:8080"))
    }

    @Test
    fun httpWithTrailingSlash() {
        assertEquals("http://192.168.1.100:8080" to "",
            Prefs.parseConnectUrl("http://192.168.1.100:8080/"))
    }

    @Test
    fun httpWithTokenT() {
        assertEquals("http://192.168.1.100:8080" to "ABC123",
            Prefs.parseConnectUrl("http://192.168.1.100:8080/?t=ABC123"))
    }

    @Test
    fun httpWithTokenName() {
        assertEquals("http://10.0.0.5:8080" to "x9",
            Prefs.parseConnectUrl("http://10.0.0.5:8080/?token=x9"))
    }

    @Test
    fun httpsSchemeIsKept() {
        // 回归：以前这里会被削成 "print.example.com:8443"，然后被补成 http://
        assertEquals("https://print.example.com:8443" to "abc123",
            Prefs.parseConnectUrl("https://print.example.com:8443/?t=abc123"))
    }

    @Test
    fun httpsSchemeWithoutToken() {
        assertEquals("https://print.example.com:8443" to "",
            Prefs.parseConnectUrl("https://print.example.com:8443"))
    }

    @Test
    fun schemeCaseInsensitiveIsNormalised() {
        // 大小写混写要认，且输出统一小写（WebView 对 HTTPS:// 并不稳妥）
        assertEquals("https://print.example.com:8443" to "k",
            Prefs.parseConnectUrl("  HTTPS://print.example.com:8443/?t=k  "))
    }

    @Test
    fun bareHostPortWithQueryNoProtocol() {
        // 没写协议但带 ?token= 的裸串：host 不能被查询串污染
        assertEquals("1.2.3.4:8080" to "k", Prefs.parseConnectUrl("1.2.3.4:8080?token=k"))
    }

    @Test
    fun whitespaceTolerated() {
        assertEquals("http://192.168.1.100:8080" to "T",
            Prefs.parseConnectUrl("  http://192.168.1.100:8080/?t=T\n "))
    }

    @Test
    fun extraQueryParamsStillFindToken() {
        assertEquals("http://1.1.1.1:8080" to "tok",
            Prefs.parseConnectUrl("http://1.1.1.1:8080/?a=1&t=tok&b=2"))
    }

    @Test
    fun pathAfterPortStripped() {
        assertEquals("http://192.168.1.100:8080" to "",
            Prefs.parseConnectUrl("http://192.168.1.100:8080/index.html"))
    }

    @Test
    fun emptyIsRejected() {
        assertNull(Prefs.parseConnectUrl(""))
        assertNull(Prefs.parseConnectUrl("   "))
    }

    @Test
    fun garbageWithoutDotsOrColonIsRejected() {
        // 既不是 http URL 又没 host:port 特征的串（如 "hello"）不当地址
        assertNull(Prefs.parseConnectUrl("hello"))
    }

    // ------------------------------------------------------------ 基地址规范化

    @Test
    fun baseUrlDefaultsToHttpWhenNoScheme() {
        // 老配置（只有 IP:端口）行为必须一点不变
        assertEquals("http://192.168.1.100:8080",
            Prefs.normalizeBaseUrl("192.168.1.100:8080"))
    }

    @Test
    fun baseUrlStripsTrailingSlashAndWhitespace() {
        assertEquals("http://192.168.1.100:8080",
            Prefs.normalizeBaseUrl("  192.168.1.100:8080/  "))
    }

    @Test
    fun baseUrlKeepsExplicitHttp() {
        assertEquals("http://192.168.1.100:8080",
            Prefs.normalizeBaseUrl("http://192.168.1.100:8080/"))
    }

    @Test
    fun baseUrlKeepsExplicitHttps() {
        // 回归核心：公网 TLS 端口不能被降级成 http
        assertEquals("https://print.example.com:8443",
            Prefs.normalizeBaseUrl("https://print.example.com:8443"))
        assertEquals("https://print.example.com:8443",
            Prefs.normalizeBaseUrl("https://print.example.com:8443/"))
    }

    @Test
    fun baseUrlLowercasesSchemeOnly() {
        assertEquals("https://print.example.com:8443",
            Prefs.normalizeBaseUrl("HTTPS://print.example.com:8443/"))
        assertEquals("http://192.168.1.100:8080",
            Prefs.normalizeBaseUrl("HTTP://192.168.1.100:8080"))
    }

    @Test
    fun scannedHttpsCodeSurvivesEndToEnd() {
        // 端到端：扫 admin 页那条「公网访问链接」，规范化后必须还是 https
        val parsed = Prefs.parseConnectUrl("https://print.example.com:8443/?t=abc123")
        assertEquals("https://print.example.com:8443" to "abc123", parsed)
        assertEquals("https://print.example.com:8443",
            Prefs.normalizeBaseUrl(parsed!!.first))
    }
}
