package com.printgw.selfservice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Prefs.parseConnectUrl 纯函数测试（不依赖 Android 框架，JVM 上跑）。
 *
 * 这是「扫码连接」的核心解析：把二维码里的连接码还原成 host[:port] + 口令。
 * 解析错一步，用户扫出来的就是错地址/丢口令，扫码就白做了。
 */
class PrefsTest {

    @Test
    fun bareHostPort() {
        assertEquals("192.168.1.100:8080" to "", Prefs.parseConnectUrl("192.168.1.100:8080"))
    }

    @Test
    fun httpNoPath() {
        assertEquals("192.168.1.100:8080" to "", Prefs.parseConnectUrl("http://192.168.1.100:8080"))
    }

    @Test
    fun httpWithTrailingSlash() {
        assertEquals("192.168.1.100:8080" to "", Prefs.parseConnectUrl("http://192.168.1.100:8080/"))
    }

    @Test
    fun httpWithTokenT() {
        // 服务端 deploy_gateway.py 开 --token 时生成的正是这种
        assertEquals("192.168.1.100:8080" to "ABC123",
            Prefs.parseConnectUrl("http://192.168.1.100:8080/?t=ABC123"))
    }

    @Test
    fun httpWithTokenName() {
        assertEquals("10.0.0.5:8080" to "x9",
            Prefs.parseConnectUrl("http://10.0.0.5:8080/?token=x9"))
    }

    @Test
    fun httpsUsesSameHostPort() {
        assertEquals("192.168.1.100:8080" to "", Prefs.parseConnectUrl("https://192.168.1.100:8080"))
    }

    @Test
    fun bareHostPortWithQueryNoProtocol() {
        // 没写协议但带 ?token= 的裸串：host 不能被查询串污染
        assertEquals("1.2.3.4:8080" to "k", Prefs.parseConnectUrl("1.2.3.4:8080?token=k"))
    }

    @Test
    fun whitespaceTolerated() {
        assertEquals("192.168.1.100:8080" to "T", Prefs.parseConnectUrl("  http://192.168.1.100:8080/?t=T\n "))
    }

    @Test
    fun extraQueryParamsStillFindToken() {
        assertEquals("1.1.1.1:8080" to "tok",
            Prefs.parseConnectUrl("http://1.1.1.1:8080/?a=1&t=tok&b=2"))
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

    @Test
    fun pathAfterPortStripped() {
        assertEquals("192.168.1.100:8080" to "",
            Prefs.parseConnectUrl("http://192.168.1.100:8080/index.html"))
    }
}
