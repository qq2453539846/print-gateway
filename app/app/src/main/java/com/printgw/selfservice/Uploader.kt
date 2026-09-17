package com.printgw.selfservice

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import org.json.JSONObject
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL

/** 与服务端 /api/upload 的返回一一对应。 */
data class UploadResult(
    val id: String,
    val filename: String,
    val pages: Int,
    val files: Int
)

/**
 * 把 content:// 的文件 POST 到打印网关的 /api/upload。
 *
 * 用 HttpURLConnection 手写 multipart，不引 OkHttp —— 整个 App 只有这一个
 * 网络请求，少一个依赖就少一份体积和版本冲突。
 *
 * 两个必须遵守的约束（都是服务端实现决定的，写错就静默失败）：
 *
 * 1. **必须带 Content-Length**。网关的 `_body()` 只读 `Content-Length`，
 *    不解析 `Transfer-Encoding: chunked`。所以绝不能用
 *    `setChunkedStreamingMode()`。这里优先用 `setFixedLengthStreamingMode()`
 *    精确预告长度并发流式送，拿不到文件大小时退回默认缓冲模式
 *    （HttpURLConnection 会自己算好 Content-Length）。
 * 2. **文件名必须用 UTF-8 写**。`DataOutputStream.writeBytes()` 只取每个
 *    字符的低 8 位，中文文件名会直接变成乱码，故一律用
 *    `String.toByteArray(UTF_8)` + `write()`。
 */
object Uploader {

    private const val CONNECT_TIMEOUT_MS = 15_000
    private const val READ_TIMEOUT_MS = 300_000
    private const val COPY_BUF = 64 * 1024

    fun upload(
        ctx: Context,
        baseUrl: String,
        token: String,
        uris: List<Uri>,
        onProgress: (Int) -> Unit = {}
    ): UploadResult {
        if (uris.isEmpty()) throw IOException("没有要上传的文件")

        val boundary = "----printgw${System.currentTimeMillis()}"
        val parts = uris.map { uri ->
            val name = displayName(ctx, uri)
            val mime = ctx.contentResolver.getType(uri) ?: guessMime(name)
            val head = ("--$boundary\r\n" +
                    "Content-Disposition: form-data; name=\"file\"; " +
                    "filename=\"${sanitize(name)}\"\r\n" +
                    "Content-Type: $mime\r\n\r\n").toByteArray(Charsets.UTF_8)
            Part(uri, head)
        }
        val tail = "--$boundary--\r\n".toByteArray(Charsets.US_ASCII)

        val conn = (URL("$baseUrl/api/upload").openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            doOutput = true
            connectTimeout = CONNECT_TIMEOUT_MS
            readTimeout = READ_TIMEOUT_MS
            setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
            if (token.isNotBlank()) setRequestProperty("X-Token", token)
        }

        try {
            // 所有文件都能拿到大小才敢预定长度 —— 少一个就必须退回缓冲模式，
            // 否则实际写入字节数与预告不符，HttpURLConnection 会抛协议错。
            val sizes = parts.map { sizeOf(ctx, it.uri) }
            val total: Long? = if (sizes.all { it != null && it >= 0 })
                parts.indices.sumOf { i -> parts[i].head.size.toLong() + sizes[i]!! + 2L } +
                        tail.size
            else null

            if (total != null) conn.setFixedLengthStreamingMode(total)

            val raw = conn.outputStream
            val out: OutputStream =
                if (total != null) CountingOutputStream(raw, total, onProgress) else raw

            for (p in parts) {
                out.write(p.head)
                val input: InputStream = ctx.contentResolver.openInputStream(p.uri)
                    ?: throw IOException("无法读取文件：${p.uri.lastPathSegment ?: p.uri}")
                input.use {
                    val buf = ByteArray(COPY_BUF)
                    while (true) {
                        val n = it.read(buf)
                        if (n <= 0) break
                        out.write(buf, 0, n)
                    }
                }
                out.write(CRLF)
            }
            out.write(tail)
            out.flush()
            onProgress(100)

            val code = conn.responseCode
            val body = (if (code in 200..299) conn.inputStream else conn.errorStream)
                ?.bufferedReader()?.use { r -> r.readText() }.orEmpty()

            if (code !in 200..299) throw IOException(errorOf(body, code))

            val jo = JSONObject(body)
            return UploadResult(
                id = jo.optString("id"),
                filename = jo.optString("filename"),
                pages = jo.optInt("pages"),
                files = jo.optInt("files", uris.size)
            )
        } finally {
            conn.disconnect()
        }
    }

    private val CRLF = "\r\n".toByteArray(Charsets.US_ASCII)

    private class Part(val uri: Uri, val head: ByteArray)

    /** 文件字节数；ContentProvider 不提供时返回 null，交由调用方退回缓冲模式。 */
    private fun sizeOf(ctx: Context, uri: Uri): Long? {
        if (uri.scheme == "file") {
            val p = uri.path ?: return null
            return java.io.File(p).takeIf { it.isFile }?.length()
        }
        return try {
            ctx.contentResolver.query(
                uri, arrayOf(OpenableColumns.SIZE), null, null, null
            )?.use { c ->
                if (c.moveToFirst()) {
                    val i = c.getColumnIndex(OpenableColumns.SIZE)
                    if (i >= 0 && !c.isNull(i)) c.getLong(i) else null
                } else null
            }
        } catch (_: Exception) {
            null
        }
    }

    private fun displayName(ctx: Context, uri: Uri): String {
        if (uri.scheme == "file") {
            return uri.lastPathSegment ?: "upload.pdf"
        }
        try {
            ctx.contentResolver.query(
                uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null
            )?.use { c ->
                if (c.moveToFirst()) {
                    val i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (i >= 0) c.getString(i)?.takeIf { it.isNotBlank() }?.let { return it }
                }
            }
        } catch (_: Exception) {
            // 落到下面的兜底
        }
        return uri.lastPathSegment?.substringAfterLast('/')?.takeIf { it.isNotBlank() }
            ?: "upload.pdf"
    }

    /** multipart 头里不能出现引号和换行 —— 会直接破坏分帧解析。 */
    private fun sanitize(name: String): String =
        name.replace('"', '_').replace('\r', '_').replace('\n', '_')
            .ifBlank { "upload.pdf" }

    private fun guessMime(name: String): String =
        when (name.substringAfterLast('.', "").lowercase()) {
            "pdf" -> "application/pdf"
            "jpg", "jpeg" -> "image/jpeg"
            "png" -> "image/png"
            "webp" -> "image/webp"
            "bmp" -> "image/bmp"
            "gif" -> "image/gif"
            "tif", "tiff" -> "image/tiff"
            else -> "application/octet-stream"
        }

    /** 服务端错误体形如 {"error": "..."}，取不到就退回状态码。 */
    private fun errorOf(body: String, code: Int): String =
        try {
            JSONObject(body).optString("error").ifBlank { "HTTP $code" }
        } catch (_: Exception) {
            "HTTP $code"
        }

    /** 统计已发送字节并回调百分比，用于界面上的进度条。 */
    private class CountingOutputStream(
        private val out: OutputStream,
        private val total: Long,
        private val onProgress: (Int) -> Unit
    ) : OutputStream() {
        private var written = 0L
        private var lastPct = -1

        override fun write(b: Int) {
            out.write(b); bump(1L)
        }

        override fun write(b: ByteArray, off: Int, len: Int) {
            out.write(b, off, len); bump(len.toLong())
        }

        private fun bump(n: Long) {
            written += n
            val pct = ((written * 100) / total).toInt().coerceIn(0, 100)
            if (pct != lastPct) {
                lastPct = pct
                onProgress(pct)
            }
        }

        override fun flush() = out.flush()
        override fun close() = out.close()
    }
}
