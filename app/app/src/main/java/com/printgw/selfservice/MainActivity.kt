package com.printgw.selfservice

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.util.Log
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.webkit.ConsoleMessage
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.printgw.selfservice.databinding.ActivityMainBinding
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 自助打印 App 的宿主 Activity。
 *
 * 承担两件事：
 *  1. WebView 打开网关的自助打印网页（扫码/图标进入的日常用法）；
 *  2. 作为系统「打开方式」的接收方 —— 拿到其它 App 传来的 content://
 *     后由原生读流上传，再把 WebView 导航到 `/?job=<id>` 让用户确认设置。
 *
 * 第 2 点是纯 WebView 壳做不到的：系统不会把 PDF 交给浏览器类组件，
 * WebView 里的 JS 也没有读 content:// 的权限。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var b: ActivityMainBinding
    private lateinit var prefs: Prefs

    /** 网页里 <input type="file"> 的回调，WebView 不会自动弹选择器。 */
    private var fileCb: ValueCallback<Array<Uri>>? = null

    /** 首次启动还没配地址就被分享进来时，先把文件存这里，配好再传。 */
    private var queuedUris: List<Uri> = emptyList()

    private val uploading = AtomicBoolean(false)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityMainBinding.inflate(layoutInflater)
        setContentView(b.root)
        prefs = Prefs(this)
        setupWeb()
        dispatch(intent)
    }

    /** singleTask 模式下再次被调用时走这里，不重建 Activity。 */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        dispatch(intent)
    }

    // ------------------------------------------------------------ 入口分发

    private fun dispatch(intent: Intent?) {
        val uris = pickUris(intent)
        if (uris.isEmpty()) {
            if (prefs.configured()) loadHome() else askServer(first = true)
            return
        }
        Log.i(TAG, "收到 ${uris.size} 个文件：${intent?.action}")
        if (!prefs.configured()) {
            queuedUris = uris
            toast(getString(R.string.need_server_first))
            askServer(first = true)
            return
        }
        upload(uris)
    }

    private fun pickUris(intent: Intent?): List<Uri> {
        if (intent == null) return emptyList()
        val raw: List<Uri> = when (intent.action) {
            Intent.ACTION_VIEW -> listOfNotNull(intent.data)
            Intent.ACTION_SEND -> listOfNotNull(streamOf(intent))
            Intent.ACTION_SEND_MULTIPLE -> streamsOf(intent)
            else -> emptyList()
        }
        return raw.filter { it.scheme == "content" || it.scheme == "file" }
    }

    @Suppress("DEPRECATION")
    private fun streamOf(i: Intent): Uri? = i.getParcelableExtra(Intent.EXTRA_STREAM)

    @Suppress("DEPRECATION")
    private fun streamsOf(i: Intent): List<Uri> =
        i.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM) ?: emptyList()

    // ------------------------------------------------------------ 上传

    private fun upload(uris: List<Uri>) {
        if (!uploading.compareAndSet(false, true)) {
            toast(getString(R.string.busy))
            return
        }
        showMask(getString(R.string.uploading, uris.size))
        Thread {
            try {
                val r = Uploader.upload(this, prefs.baseUrl(), prefs.token(), uris) { pct ->
                    if (pct < 100) runOnUiThread {
                        if (b.mask.visibility == View.VISIBLE)
                            b.maskText.text = getString(R.string.uploading_pct, pct)
                    }
                }
                runOnUiThread {
                    hideMask()
                    val url = withToken(prefs.baseUrl() + "/?job=" + Uri.encode(r.id))
                    Log.i(TAG, "上传完成 ${r.id}（${r.pages} 页）→ $url")
                    b.web.loadUrl(url)
                }
            } catch (e: Exception) {
                Log.w(TAG, "上传失败", e)
                runOnUiThread {
                    hideMask()
                    showError(e.message ?: e.javaClass.simpleName, uris)
                }
            } finally {
                uploading.set(false)
            }
        }.start()
    }

    private fun showError(msg: String, retry: List<Uri>) {
        AlertDialog.Builder(this)
            .setTitle(R.string.upload_failed)
            .setMessage(msg)
            .setPositiveButton(R.string.retry) { _, _ -> upload(retry) }
            .setNeutralButton(R.string.server_title) { _, _ -> askServer(false) }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    // ------------------------------------------------------------ WebView

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWeb() {
        b.web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            loadWithOverviewMode = true
            useWideViewPort = true
            setSupportZoom(true)
            builtInZoomControls = false
            // 与服务端返回的 no-store 保持一致：网关改版后不该被 WebView 缓存住
            // （服务端那次「改了没生效」就是页面被缓存导致的）
            cacheMode = WebSettings.LOAD_NO_CACHE
        }

        b.web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                v: WebView, req: WebResourceRequest
            ): Boolean {
                // 网关自身的导航留在 WebView 内；外部链接才交给系统浏览器
                val host = req.url.host ?: return false
                val mine = runCatching { Uri.parse(prefs.baseUrl()).host }.getOrNull()
                if (mine != null && host != mine) {
                    runCatching { startActivity(Intent(Intent.ACTION_VIEW, req.url)) }
                    return true
                }
                return false
            }
        }

        b.web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                v: WebView,
                cb: ValueCallback<Array<Uri>>,
                params: FileChooserParams
            ): Boolean {
                fileCb?.onReceiveValue(null)
                fileCb = cb
                return try {
                    val i = params.createIntent()
                    i.addCategory(Intent.CATEGORY_OPENABLE)
                    startActivityForResult(i, REQ_FILE)
                    true
                } catch (_: ActivityNotFoundException) {
                    fileCb = null
                    false
                }
            }

            override fun onProgressChanged(v: WebView, p: Int) {
                b.bar.progress = p
                b.bar.visibility = if (p in 1..99) View.VISIBLE else View.GONE
            }

            /** 页面里的 console 转发到 logcat，手机上排查不用开 USB 调试面板。 */
            override fun onConsoleMessage(m: ConsoleMessage): Boolean {
                Log.d(TAG, "JS ${m.message()} @${m.lineNumber()}")
                return true
            }
        }
    }

    @Suppress("DEPRECATION")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == REQ_FILE) {
            val cb = fileCb
            fileCb = null
            cb?.onReceiveValue(
                if (resultCode == RESULT_OK && data != null)
                    WebChromeClient.FileChooserParams.parseResult(resultCode, data)
                else null
            )
            return
        }
        if (requestCode == REQ_SCAN) {
            if (resultCode == RESULT_OK) {
                val raw = data?.getStringExtra(ScanActivity.EXTRA_SCAN) ?: ""
                handleScanResult(raw)
            }
            return
        }
        super.onActivityResult(requestCode, resultCode, data)
    }

    /** 扫码回传的原始文本，解析成 host+口令后一步配好。 */
    private fun handleScanResult(raw: String) {
        Log.i(TAG, "扫码结果: $raw")
        val parsed = Prefs.parseConnectUrl(raw)
        if (parsed == null) {
            toast(getString(R.string.scan_bad_qr))
            askServer(first = false)
            return
        }
        val (host, token) = parsed
        prefs.save(host, token)
        toast("已连接 $host" + (if (token.isNotEmpty()) "（含口令）" else ""))
        val q = queuedUris
        queuedUris = emptyList()
        if (q.isNotEmpty()) upload(q) else loadHome()
    }

    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        if (b.web.canGoBack()) b.web.goBack() else super.onBackPressed()
    }

    // ------------------------------------------------------------ 菜单 / 设置

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menu.add(0, ID_SETTINGS, 0, R.string.server_title)
        menu.add(0, ID_RELOAD, 1, R.string.reload)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean = when (item.itemId) {
        ID_SETTINGS -> { askServer(first = false); true }
        ID_RELOAD -> { b.web.reload(); true }
        else -> super.onOptionsItemSelected(item)
    }

    private fun askServer(first: Boolean) {
        val pad = (20 * resources.displayMetrics.density).toInt()
        val hostBox = EditText(this).apply {
            hint = Prefs.DEFAULT_HOST
            setText(prefs.host())
            setSingleLine()
        }
        val tokenBox = EditText(this).apply {
            hint = getString(R.string.token_hint)
            setText(prefs.token())
            setSingleLine()
        }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad / 2, pad, 0)
            addView(hostBox)
            addView(tokenBox)
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.server_title)
            .setMessage(R.string.server_msg)
            .setView(box)
            .setCancelable(!first)
            .setPositiveButton(R.string.save_connect) { _, _ ->
                val h = hostBox.text.toString().trim()
                if (h.isBlank()) {
                    toast(getString(R.string.host_empty))
                    return@setPositiveButton
                }
                prefs.save(h, tokenBox.text.toString())
                val q = queuedUris
                queuedUris = emptyList()
                if (q.isNotEmpty()) upload(q) else loadHome()
            }
            .setNeutralButton(R.string.scan_btn) { _, _ -> launchScan() }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    /** 打开扫码页，识别到「带口令连接码」后自动配好 host+口令。 */
    private fun launchScan() {
        startActivityForResult(Intent(this, ScanActivity::class.java), REQ_SCAN)
    }

    private fun loadHome() {
        // 配置了口令时，首页 URL 必须带 ?t= —— 否则开 --token 的服务会把
        // WebView 首页直接 401 拦下（页面内的 API 也都会 401）。
        b.web.loadUrl(withToken(prefs.baseUrl() + "/"))
    }

    /** 给已带查询串的 URL 补上 ?t= 口令（若配置了）。首页、?job= 直达共用。 */
    private fun withToken(url: String): String {
        val tok = prefs.token()
        if (tok.isEmpty()) return url
        val sep = if (url.contains('?')) "&" else "?"
        return url + sep + "t=" + Uri.encode(tok)
    }

    // ------------------------------------------------------------ 小工具

    private fun showMask(text: String) {
        b.maskText.text = text
        b.mask.visibility = View.VISIBLE
    }

    private fun hideMask() {
        b.mask.visibility = View.GONE
    }

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_SHORT).show()

    companion object {
        private const val TAG = "PrintSelfService"
        private const val REQ_FILE = 1001
        private const val REQ_SCAN = 1002
        private const val ID_SETTINGS = 1
        private const val ID_RELOAD = 2
    }
}
