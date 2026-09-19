package com.printgw.selfservice

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.util.Log
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import com.printgw.selfservice.databinding.ActivityScanBinding
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * 相机扫码：识别打印网关的「带口令连接码」（QR 或普通条形码），
 * 把原始文本回传给调用方（RESULT_OK + EXTRA_SCAN），由 MainActivity
 * 解析成 host + 口令并存进 Prefs。
 *
 * 设计取舍：
 *  - 识别走 ML Kit 离线模型，不联网、不依赖 Google 服务；
 *  - 本 Activity 只负责「拍到条码」，回传的是**原始文本**；解析规则集中在
 *    `Prefs.parseConnectUrl`（纯函数、可单测），本页只用它做「合格判定」——
 *    不合格的结果继续扫，不打断用户；
 *  - 识别到一次即停，避免连续识别刷屏。
 *
 * API 注意（按实际 jar 校验过，别再改回去）：
 *  - 预览用例是 androidx.camera.core.Preview（不是 camera.view 里那个，
 *    camera.view 只有 PreviewView 这个控件）；
 *  - CameraX 1.3 把 STRATEGY_KEEP_LATEST 改名成 STRATEGY_KEEP_ONLY_LATEST；
 *  - ML Kit 17.2 的 getClient 只收 BarcodeScannerOptions，不再收 int；
 *  - 从 ImageProxy 造输入图要走 ImageProxy.image（android.media.Image）
 *    + 旋转角度，而不是把 ImageProxy 本身塞给 fromMediaImage。
 *  - **addListener 的 executor 必须是主线程的**（ContextCompat.getMainExecutor）。
 *    这个参数只决定「listener 在哪跑」，不影响识别；但 bindToLifecycle 内部
 *    第一件事就是 Threads.checkMainThread()，传后台 Executor 会必然失败。
 *  - **不要开 enableAllPotentialBarcodes()**。它会把「无法解码的潜在条码」也
 *    返回，加上画面文字被误检出来的垃圾串，都会污染结果列表。判定必须落在
 *    「能否解析成连接码」上（见 handleCodes），而不是「rawValue 非空」。
 */
class ScanActivity : AppCompatActivity() {

    private lateinit var b: ActivityScanBinding
    private val cameraExecutor: ExecutorService by lazy {
        Executors.newSingleThreadExecutor()
    }

    /**
     * 识别在飞行时置位，避免上一帧还没回来就塞下一帧。
     *
     * 写它的有两个线程：`cameraExecutor`（丢弃帧那条路径）与 ML Kit 的主线程回调。
     * 不加 @Volatile 时两边各看各的缓存，去重可能失效。
     */
    @Volatile
    private var analysing = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityScanBinding.inflate(layoutInflater)
        setContentView(b.root)

        if (cameraPermissionGranted()) {
            startCamera()
        } else {
            ActivityCompat.requestPermissions(
                this, arrayOf(Manifest.permission.CAMERA), REQ_CAMERA
            )
        }

        // 构建标识常驻在提示条上方（下面那条 msg 留给「扫到了什么」的诊断）。
        // 扫码出问题时第一句话往往是「我装的到底是不是新包」—— 与其来回猜，
        // 不如让这个页面自己报出来。
        b.hint.text = getString(R.string.scan_hint) + "\n\n" + buildLabel()
    }

    private fun buildLabel(): String =
        getString(R.string.app_build_fmt, BuildConfig.VERSION_NAME, BuildConfig.BUILD_STAMP)

    private fun cameraPermissionGranted(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_CAMERA &&
            grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED
        ) {
            startCamera()
        } else {
            b.msg.text = getString(R.string.scan_permission_needed)
        }
    }

    private fun startCamera() {
        val fut = ProcessCameraProvider.getInstance(this)
        fut.addListener({
            try {
                val provider = fut.get()
                val selector = CameraSelector.DEFAULT_BACK_CAMERA

                // 预览：把相机画面渲染到 PreviewView
                val preview = Preview.Builder().build().also {
                    it.setSurfaceProvider(b.preview.surfaceProvider)
                }

                // 分析帧：每帧丢给 ML Kit 扫码。
                // 用 ImageProxy，处理完必须 close() 才能拿到下一帧。
                val analyzer = ImageAnalysis.Builder()
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .build()

                // 全格式识别：连接码可能是二维码，也可能是普通一维码。
                // 默认模式（不加任何 options）就是「全部码制」，不需要显式列格式。
                //
                // **不要开 enableAllPotentialBarcodes()**。官方文档写得很清楚：它会把
                // 「即使无法解码」的潜在条码也返回（"a list containing potential
                // barcodes that were not decoded"）。实测踩过：对着管理页扫时，它
                // 把单码预览旁边的中文小字误检成一维码，解出一串既没有 `.` 也没有
                // `:` 的垃圾，被当成「扫到了东西」直接判失败 —— 用户明明对准了二维码，
                // 却弹「不是有效的连接二维码」。
                val scanner = BarcodeScanning.getClient(
                    BarcodeScannerOptions.Builder().build()
                )

                analyzer.setAnalyzer(cameraExecutor) { imageProxy ->
                    if (analysing) {
                        // 上一帧还在识别，丢弃这一帧
                        imageProxy.close()
                        return@setAnalyzer
                    }
                    analysing = true
                    // getImage() 在部分机型/时序下可能拿不到帧，返回 null：跳过这一帧
                    val mediaImage = imageProxy.image
                    if (mediaImage == null) {
                        analysing = false
                        imageProxy.close()
                        return@setAnalyzer
                    }
                    val input = InputImage.fromMediaImage(
                        mediaImage,
                        imageProxy.imageInfo.rotationDegrees
                    )
                    // process 的回调跑在主线程
                    scanner.process(input)
                        .addOnSuccessListener { codes ->
                            analysing = false
                            imageProxy.close()
                            handleCodes(codes)
                        }
                        .addOnFailureListener {
                            analysing = false
                            imageProxy.close()
                        }
                }

                provider.bindToLifecycle(this, selector, preview, analyzer)
            } catch (e: Exception) {
                // 没有可用相机 / 绑定失败：给出提示而不是闪退
                Log.w(TAG, "相机绑定失败", e)
                b.msg.text = getString(R.string.scan_no_camera)
                b.preview.visibility = View.GONE
            }
            // ↑ 这个 listener 必须跑在**主线程**：`bindToLifecycle` 的第一件事就是
            //   Threads.checkMainThread()，非主线程直接抛 IllegalStateException
            //   ("Not in application's main thread")。以前这里传的是自建的单线程
            //   Executor —— 名字叫 mainExecutor，其实是后台线程，于是每次绑定都
            //   抛异常被下面 catch 吞掉，预览被置 GONE，用户看到的就是全黑取景框，
            //   还被误导成「这台设备没有可用的相机」。
        }, ContextCompat.getMainExecutor(this))
    }

    /**
     * 从识别结果里挑出**真正能用**的那一个。
     *
     * 光判 `rawValue != null` 是不够的。ML Kit 会同时给出两类「有内容但不是
     * 连接码」的结果：
     *   - 解不出内容的潜在条码（rawValue 是空串而非 null）；
     *   - 把画面里的文字/图案误检成条码，解出一串垃圾。
     * 以前取 `firstOrNull { it.rawValue != null }` 再直接 `finish()`，这两种
     * 都会让用户看到「不是有效的连接二维码」—— 明明对准了码，相机也识别到了
     * 东西，却判失败，且没有第二次机会。
     *
     * 判定标准改成「**能不能解析成连接码**」：
     *   - 空的 / 解不出的        → 跳过；
     *   - 解析不出 host[:port]   → 跳过，并在提示条上显示扫到了什么；
     *   - 解析成功的             → 立刻回传。
     * 一个都不合格就静静地继续扫下一帧，**不报错、不退出**。
     *
     * 把实际内容显示出来是刻意的：这类问题只能靠现场信息定位，而不是靠猜。
     */
    private fun handleCodes(codes: List<Barcode>) {
        var seen: String? = null
        for (c in codes) {
            val raw = c.rawValue?.trim().orEmpty()
            if (raw.isEmpty()) continue
            if (Prefs.parseConnectUrl(raw) == null) {
                if (seen == null) seen = raw
                Log.i(TAG, "忽略不可用结果：format=${c.format} raw=$raw")
                continue
            }
            onScanned(raw)
            return
        }
        if (seen != null) {
            b.msg.text = getString(R.string.scan_seen_not_conn, seen.take(60))
        }
    }

    private fun onScanned(raw: String) {
        val data = Intent().putExtra(EXTRA_SCAN, raw)
        setResult(RESULT_OK, data)
        finish()
    }

    override fun onDestroy() {
        // 只关自建的线程池；ContextCompat.getMainExecutor() 是系统主线程的，
        // 不归我们关，也不能关。
        cameraExecutor.shutdown()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "PrintSelfService"
        const val EXTRA_SCAN = "scan_raw"
        private const val REQ_CAMERA = 4001
    }
}
