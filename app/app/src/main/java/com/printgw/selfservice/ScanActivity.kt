package com.printgw.selfservice

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
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
 *  - 本 Activity 只负责「拍到条码」，回传的是**原始文本**，解析规则
 *    集中在 MainActivity（便于集中维护与测试）；
 *  - 识别到一次即停，避免连续识别刷屏。
 *
 * API 注意（按实际 jar 校验过，别再改回去）：
 *  - 预览用例是 androidx.camera.core.Preview（不是 camera.view 里那个，
 *    camera.view 只有 PreviewView 这个控件）；
 *  - CameraX 1.3 把 STRATEGY_KEEP_LATEST 改名成 STRATEGY_KEEP_ONLY_LATEST；
 *  - ML Kit 17.2 的 getClient 只收 BarcodeScannerOptions，不再收 int；
 *  - 从 ImageProxy 造输入图要走 ImageProxy.image（android.media.Image）
 *    + 旋转角度，而不是把 ImageProxy 本身塞给 fromMediaImage。
 */
class ScanActivity : AppCompatActivity() {

    private lateinit var b: ActivityScanBinding
    private val cameraExecutor: ExecutorService by lazy {
        Executors.newSingleThreadExecutor()
    }
    private val mainExecutor: ExecutorService = Executors.newSingleThreadExecutor()

    /** 识别在飞行时置位，避免上一帧还没回来就塞下一帧。 */
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
    }

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
                val options = BarcodeScannerOptions.Builder()
                    .enableAllPotentialBarcodes()
                    .build()
                val scanner = BarcodeScanning.getClient(options)

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
                            val raw = codes.firstOrNull { it.rawValue != null }?.rawValue
                            if (raw != null) {
                                onScanned(raw.trim())
                            }
                            // 没识别到就留在页面，等待下一帧
                        }
                        .addOnFailureListener {
                            analysing = false
                            imageProxy.close()
                        }
                }

                provider.bindToLifecycle(this, selector, preview, analyzer)
            } catch (e: Exception) {
                // 没有可用相机 / 绑定失败：给出提示而不是闪退
                b.msg.text = getString(R.string.scan_no_camera)
                b.preview.visibility = View.GONE
            }
        }, mainExecutor)
    }

    private fun onScanned(raw: String) {
        val data = Intent().putExtra(EXTRA_SCAN, raw)
        setResult(RESULT_OK, data)
        finish()
    }

    override fun onDestroy() {
        cameraExecutor.shutdown()
        mainExecutor.shutdown()
        super.onDestroy()
    }

    companion object {
        const val EXTRA_SCAN = "scan_raw"
        private const val REQ_CAMERA = 4001
    }
}
