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
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.ChecksumException
import com.google.zxing.DecodeHintType
import com.google.zxing.FormatException
import com.google.zxing.MultiFormatReader
import com.google.zxing.NotFoundException
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.printgw.selfservice.databinding.ActivityScanBinding
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * 相机扫码：识别打印网关的「带口令连接码」（QR 或普通条形码），
 * 把原始文本回传给调用方（RESULT_OK + EXTRA_SCAN），由 MainActivity
 * 解析成 host + 口令并存进 Prefs。
 *
 * 设计取舍：
 *  - 识别走 **ZXing**（纯 Java，Apache-2.0），本地解码、不联网、不依赖 Google 服务，
 *    也不引入任何专有 SDK —— 整个依赖树都能对上开源许可；
 *  - 本 Activity 只负责「拍到条码」，回传的是**原始文本**；解析规则集中在
 *    `Prefs.parseConnectUrl`（纯函数、可单测），本页只用它做「合格判定」——
 *    不合格的结果继续扫，不打断用户；
 *  - 识别到一次即停，避免连续识别刷屏。
 *
 * API 注意（按实际 jar 校验过，别再改回去）：
 *  - 预览用例是 androidx.camera.core.Preview（不是 camera.view 里那个，
 *    camera.view 只有 PreviewView 这个控件）；
 *  - CameraX 1.3 把 STRATEGY_KEEP_LATEST 改名成 STRATEGY_KEEP_ONLY_LATEST；
 *  - **addListener 的 executor 必须是主线程的**（ContextCompat.getMainExecutor）。
 *    这个参数只决定「listener 在哪跑」，不影响识别；但 bindToLifecycle 内部
 *    第一件事就是 Threads.checkMainThread()，传后台 Executor 会必然失败。
 *
 * ZXing 接入注意（踩过的坑，逐条写在对应代码旁）：
 *  - Y 平面的 `rowStride` **通常大于**图像宽度（行尾有填充），直接整块丢给
 *    ZXing 会把画面撕成斜条纹，什么都解不出来 —— 必须按行重排成紧凑数组；
 *  - `PlanarYUVLuminanceSource` **不支持旋转**（`rotateCounterClockwise()`
 *    直接抛 UnsupportedOperationException），所以旋转得自己做；
 *  - `MultiFormatReader` 是**有状态**的，用 `decodeWithState` 必须先 `reset()`；
 *    这里改用无状态的 `decode(bitmap, hints)`，每帧新建 reader，省掉这类状态坑；
 *  - 默认不限制码制会把资源浪费在无关格式上，显式给 POSSIBLE_FORMATS。
 */
class ScanActivity : AppCompatActivity() {

    private lateinit var b: ActivityScanBinding
    private val cameraExecutor: ExecutorService by lazy {
        Executors.newSingleThreadExecutor()
    }

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

                // 分析帧：每帧丢给 ZXing。
                // 用 ImageProxy，处理完必须 close() 才能拿到下一帧。
                //
                // 不需要防重入标志：analyzer 跑在单线程 executor 上，且这里全程
                // 同步解码，同一时刻只可能在处理一帧。KEEP_ONLY_LATEST 会自动
                // 丢弃积压的旧帧，不会因为解码慢而堆积。
                val analyzer = ImageAnalysis.Builder()
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .build()

                analyzer.setAnalyzer(cameraExecutor) { imageProxy ->
                    try {
                        onFrameDecoded(decodeFrame(imageProxy), imageProxy.imageInfo.rotationDegrees)
                    } catch (e: Exception) {
                        // 单帧解码异常不该让预览中断，记一笔就继续
                        Log.w(TAG, "解码帧失败", e)
                    } finally {
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

    // ── 解码 ────────────────────────────────────────────────────────────

    /**
     * 把一帧 YUV_420_888 转成 ZXing 能吃的灰度图并解码。
     *
     * 返回解出的原始文本；没扫到（或扫到但解码失败）返回 null。
     * 只取**第一个解出结果的那张码** —— 连接码只有一张，多解无意义。
     */
    private fun decodeFrame(imageProxy: ImageProxy): String? {
        val srcW = imageProxy.width
        val srcH = imageProxy.height
        if (srcW <= 0 || srcH <= 0) return null

        val plane = imageProxy.planes.firstOrNull() ?: return null
        val buf = plane.buffer
        val rowStride = plane.rowStride
        val pixelStride = plane.pixelStride
        if (rowStride <= 0 || pixelStride <= 0) return null

        // Y 平面按行重排成紧凑灰度数组。
        // rowStride 常大于 srcW（硬件按 16/64 字节对齐填行尾），直接把整块 buffer
        // 当 w×h 用会让每一行都错位，画面变成斜条纹 —— 表现是「明明是清楚的二维码
        // 却永远识别不出来」，且不会有任何报错。
        val packed = ByteArray(srcW * srcH)
        if (rowStride == srcW && pixelStride == 1) {
            buf.rewind()
            buf.get(packed, 0, minOf(buf.remaining(), packed.size))
        } else {
            val row = ByteArray(rowStride)
            var out = 0
            for (y in 0 until srcH) {
                val base = y * rowStride
                if (base >= buf.limit()) break
                buf.position(base)
                val n = minOf(rowStride, buf.remaining())
                buf.get(row, 0, n)
                var x = 0
                while (x < srcW && x < n) {
                    packed[out++] = row[x]
                    x += pixelStride
                }
            }
        }

        val deg = ((imageProxy.imageInfo.rotationDegrees % 360) + 360) % 360
        val (data, w, h) = rotateGray(packed, srcW, srcH, deg)

        val source = PlanarYUVLuminanceSource(data, w, h, 0, 0, w, h, false)
        val bitmap = BinaryBitmap(HybridBinarizer(source))
        return try {
            MultiFormatReader().decode(bitmap, HINTS).text
        } catch (e: NotFoundException) {
            null                       // 这一帧里没有可识别的码 —— 常态，不记日志
        } catch (e: FormatException) {
            null                       // 检测到码但内容不合规（损坏/截断）
        } catch (e: ChecksumException) {
            null                       // 校验位不过，多半是模糊
        }
    }

    /**
     * 按 CameraX 给的旋转角度把灰度图转正。
     *
     * ZXing 的 `PlanarYUVLuminanceSource` 自己不提供旋转（调
     * `rotateCounterClockwise()` 会抛 UnsupportedOperationException），
     * 而 90°/270° 时图像是横躺的 —— 二维码识别对方向不敏感还好，
     * 一维码就完全解不出来，所以必须自己转。
     *
     * 映射关系（src 为 w×h）：
     *   90° ：顺时针，新图 h×w，dst[y][x] = src[h-1-x][y]
     *   270°：逆时针，新图 h×w，dst[y][x] = src[x][w-1-y]
     */
    private fun rotateGray(src: ByteArray, w: Int, h: Int, deg: Int): Triple<ByteArray, Int, Int> {
        when (deg) {
            90 -> {
                val dst = ByteArray(w * h)
                for (y in 0 until w) {                 // 新图高 = 原宽
                    val rowBase = y * h                // 新图宽 = 原高
                    for (x in 0 until h) {
                        dst[rowBase + x] = src[(h - 1 - x) * w + y]
                    }
                }
                return Triple(dst, h, w)
            }
            180 -> {
                val dst = ByteArray(w * h)
                for (y in 0 until h) {
                    val rowBase = y * w
                    val srcBase = (h - 1 - y) * w
                    for (x in 0 until w) {
                        dst[rowBase + x] = src[srcBase + (w - 1 - x)]
                    }
                }
                return Triple(dst, w, h)
            }
            270 -> {
                val dst = ByteArray(w * h)
                for (y in 0 until w) {                 // 新图高 = 原宽
                    val rowBase = y * h                // 新图宽 = 原高
                    for (x in 0 until h) {
                        dst[rowBase + x] = src[x * w + (w - 1 - y)]
                    }
                }
                return Triple(dst, h, w)
            }
            else -> return Triple(src, w, h)
        }
    }

    // ── 结果处理 ────────────────────────────────────────────────────────

    /**
     * 拿到一帧的解码结果后做判定。跑在相机线程上，碰 UI 一律切主线程。
     *
     * 光判「解出来了」是不够的：画面里的文字、海报上的图案都可能被当成码解出
     * 一串垃圾。判定标准是「**能不能解析成连接码**」：
     *   - 没解出东西            → 什么都不做，静静继续扫；
     *   - 解出但解析不出 host   → 在提示条上显示扫到了什么，继续扫；
     *   - 解析成功              → 立刻回传。
     * 一个都不合格也**不报错、不退出** —— 用户还有下一次机会。
     *
     * 把实际内容显示出来是刻意的：这类问题只能靠现场信息定位，而不是靠猜。
     */
    private fun onFrameDecoded(raw: String?, rotation: Int) {
        val text = raw?.trim().orEmpty()
        if (text.isEmpty()) return

        if (Prefs.parseConnectUrl(text) != null) {
            runOnUiThread { onScanned(text) }
            return
        }

        Log.i(TAG, "忽略不可用结果：rotation=$rotation raw=$text")
        val shown = text.take(60)
        runOnUiThread {
            // 只在内容变化时更新，避免同一串垃圾每帧刷一次
            val line = getString(R.string.scan_seen_not_conn, shown)
            if (b.msg.text.toString() != line) b.msg.text = line
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

        /**
         * 只认连接码可能用到的码制。给死范围既省电也提准确率 ——
         * 不限制时 ZXing 会把每种一维码都试一遍。
         *
         * TRY_HARDER 让它在弱光 / 倾斜 / 小尺寸时多试几种二值化与旋转策略，
         * 代价是单帧耗时上升；帧率由 KEEP_ONLY_LATEST 兜住，不会堆积。
         */
        private val HINTS: Map<DecodeHintType, Any> = mapOf(
            DecodeHintType.POSSIBLE_FORMATS to listOf(
                BarcodeFormat.QR_CODE,
                BarcodeFormat.DATA_MATRIX,
                BarcodeFormat.CODE_128,
                BarcodeFormat.CODE_39,
                BarcodeFormat.EAN_13,
            ),
            DecodeHintType.TRY_HARDER to true,
        )
    }
}
