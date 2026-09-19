#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二维码贴纸的单元测试。

这一套里真正值钱的是两类断言，别的都只是护栏：

  * `test_grid_roundtrip_all_codes` —— 按 `marks` 记下的位置，把画出来的
    像素**逐模块回读**成矩阵，和 `full_matrix()` 的结果逐位比对。
    它钉住的是「算出来的矩阵」与「画到纸上的图案」一致，也就是渲染环节
    不失真。gen_qr 的编码正确性已由 `verify_qr_ref.py`（与参考库差分）
    和 `verify_qr.py`（真实解码器）证明过，这里补上最后一跳。
  * `test_codes_decode_with_real_decoder` —— 用真实 QR 解码器把每个码解回来。
    这是**唯一**能证明「印出来手机扫得出来」的方法，也是这个项目历史上真踩过
    的坑（见 pitfalls 第 2 条：码看着正常但扫不出）。

    解码器优先用 **zxing-cpp**，OpenCV 只作补充。原因很实在：OpenCV 的
    `QRCodeDetector` 有盲区 —— 实测同一个合法矩阵（payload 只差一个数字，
    于是罚分最优的掩码不同），zxing-cpp 一次就读出来，OpenCV 死活读不出来。
    把它的失败当成「编码器错了」会白改一通，见 pitfalls 第 45 条。

没装解码器时后者自动跳过 —— 跳过不等于通过。装齐：
`pip install opencv-python-headless numpy zxing-cpp`
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gen_qr                                                 # noqa: E402
import pg_decor                                               # noqa: E402
import pg_sticker                                             # noqa: E402

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except ImportError:                                           # pragma: no cover
    HAS_PIL = False

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:                                           # pragma: no cover
    HAS_CV2 = False

try:
    import zxingcpp
    HAS_ZXING = True
except ImportError:                                           # pragma: no cover
    HAS_ZXING = False

NEED_PIL = unittest.skipUnless(HAS_PIL, "需要 PIL")
NEED_FONT = unittest.skipUnless(pg_decor.font_index(),
                                "本机找不到中文字体，贴纸画不出来")
NEED_DECODER = unittest.skipUnless(HAS_CV2 or HAS_ZXING, "需要解码器")


def demo_codes():
    return [
        pg_sticker.Code("lan", "同一 Wi-Fi", "192.168.1.100:8080",
                        "http://192.168.1.100:8080/"),
        pg_sticker.Code("wan", "任意网络", "print.example.com",
                        "https://print.example.com:8443/?t=abcdef123456"),
        pg_sticker.Code("app", "装 App", "扫码安装",
                        "http://192.168.1.100:8080/app"),
    ]


# ---------------------------------------------------------------- 基础

class TestLanIp(unittest.TestCase):

    def test_never_loopback(self):
        """探测失败可以返回空串，但**绝不能**把 127.x 当成内网地址印上去 ——
        那种码扫开会指向手机自己，是最难排查的一类问题。"""
        ip = pg_sticker.lan_ip()
        self.assertIsInstance(ip, str)
        self.assertFalse(ip.startswith("127."), ip)
        if ip:
            self.assertRegex(ip, r"^\d{1,3}(\.\d{1,3}){3}$")


class TestCodeObject(unittest.TestCase):

    def test_fields(self):
        c = pg_sticker.Code("lan", "标签", "小字", "http://x/")
        self.assertEqual((c.kind, c.label, c.sub, c.url),
                         ("lan", "标签", "小字", "http://x/"))

    def test_constants(self):
        self.assertEqual(pg_sticker.CODE_KINDS, ("lan", "wan", "app"))
        self.assertEqual(pg_sticker.LAYOUTS, (1, 2, 4))
        self.assertEqual(sorted(pg_sticker._GRID), [1, 2, 4])


class TestSvgFor(unittest.TestCase):

    def test_returns_svg(self):
        svg = pg_sticker.svg_for("http://192.168.1.100:8080/")
        self.assertTrue(svg.startswith("<?xml"))
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)
        self.assertIn("crispEdges", svg)      # 别让浏览器抗锯齿糊掉模块边缘

    def test_empty_url_rejected(self):
        with self.assertRaises(pg_sticker.StickerError):
            pg_sticker.svg_for("")


# ---------------------------------------------------------------- 排布决策

class TestPickGrid(unittest.TestCase):
    """
    `_pick_grid` 是纯函数，可以直接喂各种区域尺寸。
    这里不测"画得好不好看"，只钉住两条会出事的行为。
    """

    def test_single_code_is_one_cell(self):
        cols, rows, _cw, _chh, size = pg_sticker._pick_grid(
            1, 1000, 1000, 30, 60, 45, 300)
        self.assertEqual((cols, rows), (1, 1))
        self.assertGreater(size, 0)

    def test_prefers_full_grid_when_big_enough(self):
        """区域够大时必须选满排布 —— 3 个码排成 2+1 会在第四格留个空洞，
        A4 上看着像排版错了。"""
        cols, rows, _cw, _chh, size = pg_sticker._pick_grid(
            3, 2000, 2000, 30, 60, 45, 300)
        self.assertEqual(cols * rows, 3, "选了留孤格的排布")
        self.assertGreater(size, 300)

    def test_wide_area_lays_out_in_a_row(self):
        cols, rows, _cw, _chh, _size = pg_sticker._pick_grid(
            3, 2400, 500, 30, 60, 45, 300)
        self.assertEqual((cols, rows), (3, 1))

    def test_tall_area_lays_out_in_a_column(self):
        cols, rows, _cw, _chh, _size = pg_sticker._pick_grid(
            3, 600, 2400, 30, 60, 45, 300)
        self.assertEqual((cols, rows), (1, 3))

    def test_allows_orphan_cell_only_when_needed(self):
        """
        满排布把码压到扫不出来时，才允许留孤格换更大的码。
        这组参数就是 4 联 A6 的情形：够宽放 3 列、但只有 2 行高度 ——
        横排 96.7mm 是假的（高度放不下两行的标签），而 2+1 能实打实拿到
        130mm。落在 min_size（120）两侧，正好卡在这条规则上。
        """
        cols, rows, _cw, _chh, size = pg_sticker._pick_grid(
            3, 350, 500, 30, 60, 45, 120)
        self.assertEqual(cols * rows, 4, "应退到 2+1（2列2行，空一格）")
        self.assertGreaterEqual(size, 120)

    def test_rejects_area_too_small(self):
        with self.assertRaises(pg_sticker.StickerError):
            pg_sticker._pick_grid(3, 20, 20, 30, 60, 45, 300)


# ---------------------------------------------------------------- 出图

@NEED_PIL
@NEED_FONT
class TestBuildImage(unittest.TestCase):

    def test_page_is_a4_at_target_dpi(self):
        for layout in pg_sticker.LAYOUTS:
            img = pg_sticker.build_image(demo_codes(), layout=layout)
            mm = pg_sticker.DEFAULT_DPI / 25.4
            self.assertEqual(img.size, (round(210 * mm), round(297 * mm)),
                             "版式 %d 的页面不是 A4" % layout)

    def test_page_is_not_blank(self):
        img = pg_sticker.build_image(demo_codes(), layout=1)
        dark = sum(img.histogram()[:128])
        self.assertGreater(dark, 10000, "页面上几乎没有黑色，等于印了张白纸")

    def test_marks_cover_every_cell(self):
        codes = demo_codes()
        for layout in pg_sticker.LAYOUTS:
            marks = []
            pg_sticker.build_image(codes, layout=layout, marks=marks)
            self.assertEqual(len(marks), layout * len(codes))
            # 每张贴纸里的码按顺序出现
            self.assertEqual([m[0] for m in marks[:len(codes)]],
                             [c.kind for c in codes])

    def test_every_code_big_enough_to_scan(self):
        """印出来的码不能小于下限 —— 这条用 marks 反推**实际**画出来的尺寸，
        而不是信布局算出来的那个数。"""
        for layout in pg_sticker.LAYOUTS:
            marks = []
            pg_sticker.build_image(demo_codes(), layout=layout, marks=marks)
            for kind, _cx, _cy, total, n in marks:
                unit = total / float(n + 2 * pg_sticker.QR_QUIET)
                unit_mm = unit * 25.4 / pg_sticker.DEFAULT_DPI
                self.assertGreaterEqual(
                    unit_mm, pg_sticker.MIN_MODULE_MM,
                    "版式 %d 的 %s 码模块只有 %.2fmm" % (layout, kind, unit_mm))

    def test_grid_roundtrip_all_codes(self):
        """
        把画出来的像素逐模块读回来，和矩阵逐位比对。

        这是渲染环节的硬断言：只要有一个模块画错位置（半像素偏移、
        相邻模块连成一片、静区没留够），这里就会炸。
        """
        codes = demo_codes()
        mats = {c.kind: gen_qr.full_matrix(c.url, "M") for c in codes}
        marks = []
        img = pg_sticker.build_image(codes, layout=1, marks=marks)

        quiet = pg_sticker.QR_QUIET
        checked = 0
        for kind, cx, cy, total, n in marks:
            unit = total // (n + 2 * quiet)
            ox, oy = cx - total / 2.0, cy - total / 2.0
            mat = mats[kind]
            for r in range(n):
                for c in range(n):
                    px = int(ox + (quiet + c) * unit + unit / 2)
                    py = int(oy + (quiet + r) * unit + unit / 2)
                    got = img.getpixel((px, py)) < 128
                    self.assertEqual(
                        got, mat[r][c],
                        "%s 的模块 (%d,%d) 画错了" % (kind, r, c))
                    checked += 1
        self.assertGreater(checked, 3 * 21 * 21, "回读的模块数太少")

    def test_empty_codes_rejected(self):
        with self.assertRaises(pg_sticker.StickerError):
            pg_sticker.build_image([], layout=1)

    def test_bad_layout_rejected(self):
        for bad in (0, 3, 8, -1):
            with self.assertRaises(pg_sticker.StickerError):
                pg_sticker.build_image(demo_codes(), layout=bad)

    def test_module_too_small_rejected(self):
        """硬塞一个巨大的静区和很小的框，应当明确报错而不是画出糊码。"""
        d = ImageDraw.Draw(Image.new("L", (10, 10), 255))
        mat = gen_qr.full_matrix("http://192.168.1.100:8080/", "M")
        with self.assertRaises(pg_sticker.StickerError):
            pg_sticker._paste_qr(d, mat, 5, 5, 8 * pg_sticker.MIN_MODULE_MM,
                                 pg_sticker.QR_QUIET, pg_sticker.DEFAULT_DPI)


# ---------------------------------------------------------------- 真解码

@NEED_PIL
@NEED_FONT
@NEED_DECODER
class TestRealDecoder(unittest.TestCase):
    """
    唯一能证明「印出来扫得出来」的测试。

    两个解码器角色不同：
      * **zxing-cpp** —— 严格的标准实现，**判定以它为准**；
      * **OpenCV** —— 补充证据；它的失败**不单独构成失败**，因为它有盲区
        （见 `_decode` 的说明）。
    两个都没装就整类跳过 —— 跳过不等于通过。
    """

    def _decode(self, img, kind, cx, cy, total):
        """
        把某个码的位置抠出来，交给所有可用解码器各读一遍。

        为什么不能只信 OpenCV：实测一个**完全合法**的矩阵（8 个掩码里只有罚分
        最优的 m2），zxing-cpp 读得出、OpenCV 读不出。两个矩阵的差别仅仅是
        payload 里一个数字 —— 数字变了，罚分最优的掩码就变了，于是撞进它的盲区。
        拿它当唯一判据，测试会随数据飘，还会把人骗去改一个本来正确的编码器
        （本项目 2026-09-19 就差点这么干，详见 pitfalls 第 45 条）。
        """
        half = total / 2.0
        crop = img.crop((int(cx - half), int(cy - half),
                         int(cx + half), int(cy + half)))
        arr = np.array(crop.convert("L"))
        out = {}
        if HAS_ZXING:
            r = zxingcpp.read_barcode(Image.fromarray(arr))
            out["zxing-cpp"] = r.text if r else ""
        if HAS_CV2:
            out["opencv"] = cv2.QRCodeDetector().detectAndDecode(arr)[0] or ""
        return out

    def _assert_readable(self, results, want, tip):
        if "zxing-cpp" in results:
            # zxing-cpp 是标准实现：它读不出来，就是码真的有问题
            self.assertEqual(results["zxing-cpp"], want,
                             "%s —— zxing-cpp 读出来不是原内容" % tip)
            return
        # 只剩 OpenCV 时只能以它为准，但它有盲区，失败信息要把话说明白
        self.assertEqual(
            results.get("opencv"), want,
            "%s —— 本机只有 OpenCV 可用，它读不出来。它**已知有盲区**（合法码也可能"
            "读不出），别急着改编码器，先装 zxing-cpp 复核：pip install zxing-cpp" % tip)

    def test_codes_decode_with_real_decoder(self):
        for layout in pg_sticker.LAYOUTS:
            codes = demo_codes()
            want = {c.kind: c.url for c in codes}
            marks = []
            img = pg_sticker.build_image(codes, layout=layout, marks=marks)
            for kind, cx, cy, total, _n in marks:
                results = self._decode(img, kind, cx, cy, total)
                self._assert_readable(
                    results, want[kind],
                    "版式 %d 的 %s 码（各解码器是否读出：%s）"
                    % (layout, kind, {k: bool(v) for k, v in results.items()}))

    def test_wan_code_with_token_decodes(self):
        """公网码带 ?t=<40 位十六进制>，是最长的一种，最容易因为模块变小
        而出问题 —— 单独钉一条。"""
        code = pg_sticker.Code(
            "wan", "任意网络", "print.example.com",
            "https://print.example.com:8443/?t=" + "a1b2c3d4" * 5)
        marks = []
        img = pg_sticker.build_image([code], layout=1, marks=marks)
        kind, cx, cy, total, _n = marks[0]
        self._assert_readable(self._decode(img, kind, cx, cy, total),
                              code.url, "公网码（含口令）")


# ---------------------------------------------------------------- 出 PDF

@NEED_PIL
@NEED_FONT
class TestBuildPdf(unittest.TestCase):

    def _tmp(self, name):
        import tempfile
        d = tempfile.mkdtemp(prefix="sticker-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return os.path.join(d, name)

    def test_pdf_page_is_a4(self):
        import re
        out = self._tmp("s.pdf")
        pg_sticker.build_pdf(demo_codes(), out, layout=1)
        with open(out, "rb") as fh:
            raw = fh.read()
        self.assertTrue(raw.startswith(b"%PDF"))
        mb = re.search(rb"/MediaBox\s*\[([^\]]+)\]", raw)
        self.assertIsNotNone(mb, "PDF 里找不到 MediaBox")
        x0, y0, x1, y1 = (float(v) for v in mb.group(1).split())
        self.assertAlmostEqual(x1 - x0, 595.276, delta=1.0, msg="页宽不是 A4")
        self.assertAlmostEqual(y1 - y0, 841.89, delta=1.0, msg="页高不是 A4")

    def test_pdf_is_lossless_bitonal(self):
        """二维码必须走无损压缩。PIL 把灰度图存成 JPEG（DCTDecode），
        有损压缩会在模块边缘产生振铃，正是解码器最怕的东西。"""
        out = self._tmp("lossless.pdf")
        pg_sticker.build_pdf(demo_codes(), out, layout=2)
        with open(out, "rb") as fh:
            raw = fh.read()
        self.assertIn(b"/CCITTFaxDecode", raw)
        self.assertNotIn(b"/DCTDecode", raw)

    def test_pdf_pages_option(self):
        import re
        out = self._tmp("multi.pdf")
        pg_sticker.build_pdf(demo_codes(), out, layout=1, pages=3)
        with open(out, "rb") as fh:
            raw = fh.read()
        self.assertEqual(len(re.findall(rb"/Type\s*/Page[^s]", raw)), 3)

    def test_all_layouts_produce_a_pdf(self):
        for layout in pg_sticker.LAYOUTS:
            out = self._tmp("l%d.pdf" % layout)
            pg_sticker.build_pdf(demo_codes(), out, layout=layout)
            self.assertGreater(os.path.getsize(out), 2000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
