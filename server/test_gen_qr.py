#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_qr 二维码编码器单元测试。

重点是**格式信息摆放**的回归防护 —— 曾经把左上角那一副本的横竖两组接反，
导致 15 位格式信息位置错乱。当时数据区完全正确，只有 r=8/c=8 上的位有问题，
症状是"二维码看起来正常但扫不出来"，非常隐蔽。

因此这里不测"看起来对不对"，而是按标准位置**反向读取**格式信息再比对。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_qr                                                  # noqa: E402


def read_format_bits(mat, size):
    """
    按 ISO/IEC 18004 的位置从矩阵里取回 15 位格式信息。
    位置规则与 gen_qr.apply_mask_full 的写入规则互为逆运算。
    """
    fmt = 0
    for i in range(15):
        if i < 6:
            bit = mat[i][8]
        elif i < 8:
            bit = mat[i + 1][8]
        else:
            bit = mat[size - 15 + i][8]
        if bit:
            fmt |= 1 << i
    return fmt


def read_format_bits_horizontal(mat, size):
    """从横向副本读取，应与竖向副本一致（互为冗余校验）。"""
    fmt = 0
    for i in range(15):
        if i < 8:
            bit = mat[8][size - i - 1]
        elif i < 9:
            bit = mat[8][15 - i]
        else:
            bit = mat[8][15 - i - 1]
        if bit:
            fmt |= 1 << i
    return fmt


class TestFormatBitsValue(unittest.TestCase):
    """BCH(15,5) 格式信息编码的已知标准值。"""

    def test_m_mask0(self):
        self.assertEqual(gen_qr.format_bits_masked("M", 0), 0x5412)

    def test_l_mask0(self):
        self.assertEqual(gen_qr.format_bits_masked("L", 0), 0x77C4)

    def test_m_mask1(self):
        self.assertEqual(gen_qr.format_bits_masked("M", 1), 0x5125)

    def test_l_mask1(self):
        self.assertEqual(gen_qr.format_bits_masked("L", 1), 0x72F3)

    def test_all_32_values_are_15bit(self):
        for ec in ("L", "M", "Q", "H"):
            for mask in range(8):
                v = gen_qr.format_bits_masked(ec, mask)
                self.assertTrue(0 <= v < (1 << 15), "%s/%d -> %x" % (ec, mask, v))

    def test_ec_levels_distinct(self):
        seen = {}
        for ec in ("L", "M", "Q", "H"):
            v = gen_qr.format_bits_masked(ec, 0)
            self.assertNotIn(v, seen, "%s 与 %s 的格式值相同" % (ec, seen.get(v)))
            seen[v] = ec


class TestVersionBits(unittest.TestCase):
    """版本信息 BCH(18,6) 的已知标准值。"""

    KNOWN = {7: 0x07C94, 8: 0x085BC, 9: 0x09A99, 10: 0x0A4D3}

    def test_known_values(self):
        for v, expected in self.KNOWN.items():
            self.assertEqual(gen_qr.version_bits(v), expected,
                             "版本 %d 应为 0x%05X" % (v, expected))


class TestFormatInfoPlacement(unittest.TestCase):
    """
    格式信息摆放的回归测试 —— 这是曾经出 bug 的地方。

    对每个纠错等级与每条数据，生成矩阵后按标准位置读回格式信息，
    竖向副本与横向副本都必须等于 format_bits_masked(ec, 实际掩码)。
    """

    CASES = [
        ("http://192.168.1.100:8080", "M"),
        ("http://192.168.1.100:8080", "L"),
        ("http://192.168.1.100:8080/?t=abc", "Q"),
        ("A", "H"),
        ("HELLO WORLD", "M"),
        ("1234567890", "L"),
        ("中文内容测试", "H"),
        ("http://192.168.1.100:8080/?t=" + "a" * 60, "M"),
    ]

    def _matrix_and_mask(self, text, ec):
        """返回矩阵与其中实际使用的掩码。"""
        data = text.encode("utf-8")
        version = gen_qr.pick_version(len(data), ec)
        size = version * 4 + 17
        cws = gen_qr.build_bitstream(data, version, ec)
        final = gen_qr.interleave(cws, version, ec)
        mat, reserved = gen_qr.make_matrix(version, ec, size)

        bits = []
        for cw in final:
            for i in range(7, -1, -1):
                bits.append((cw >> i) & 1)
        idx, up = 0, True
        col = size - 1
        while col > 0:
            if col == 6:
                col -= 1
            rows = range(size - 1, -1, -1) if up else range(size)
            for r in rows:
                for c in (col, col - 1):
                    if not reserved[r][c]:
                        mat[r][c] = bool(bits[idx]) if idx < len(bits) else False
                        idx += 1
            up = not up
            col -= 2

        auto = gen_qr.full_matrix(text, ec)
        mask = None
        for m in range(8):
            if gen_qr.apply_mask_full(mat, reserved, m, size, ec) == auto:
                mask = m
                break
        return auto, size, mask

    def test_vertical_copy_matches_expected(self):
        for text, ec in self.CASES:
            mat, size, mask = self._matrix_and_mask(text, ec)
            self.assertIsNotNone(mask, "无法确定 %r 使用的掩码" % text[:30])
            expected = gen_qr.format_bits_masked(ec, mask)
            got = read_format_bits(mat, size)
            self.assertEqual(
                got, expected,
                "竖向格式信息错误 [%s] %r\n  期望 0x%04X 实得 0x%04X"
                % (ec, text[:30], expected, got))

    def test_horizontal_copy_matches_expected(self):
        for text, ec in self.CASES:
            mat, size, mask = self._matrix_and_mask(text, ec)
            expected = gen_qr.format_bits_masked(ec, mask)
            got = read_format_bits_horizontal(mat, size)
            self.assertEqual(
                got, expected,
                "横向格式信息错误 [%s] %r\n  期望 0x%04X 实得 0x%04X"
                % (ec, text[:30], expected, got))

    def test_two_copies_agree(self):
        """两处格式信息必须互为冗余，值相同。"""
        for text, ec in self.CASES:
            mat, size, _ = self._matrix_and_mask(text, ec)
            self.assertEqual(read_format_bits(mat, size),
                             read_format_bits_horizontal(mat, size),
                             "两处格式信息不一致 [%s] %r" % (ec, text[:30]))

    def test_dark_module_set(self):
        """固定暗模块 (size-8, 8) 必须为暗。"""
        for text, ec in self.CASES:
            mat, size, _ = self._matrix_and_mask(text, ec)
            self.assertTrue(mat[size - 8][8], "固定暗模块未设置 [%s]" % ec)

    def test_timing_patterns_untouched(self):
        """定时图案必须保持交替，不能被格式信息覆盖。"""
        for text, ec in self.CASES:
            mat, size, _ = self._matrix_and_mask(text, ec)
            for i in range(8, size - 8):
                self.assertEqual(mat[6][i], i % 2 == 0,
                                 "横向定时图案被破坏 r=6 c=%d [%s]" % (i, ec))
                self.assertEqual(mat[i][6], i % 2 == 0,
                                 "纵向定时图案被破坏 r=%d c=6 [%s]" % (i, ec))

    def test_format_bits_differ_per_mask(self):
        """不同掩码必须产生不同的格式信息（否则掩码号没写进去）。"""
        text, ec = "http://192.168.1.100:8080", "M"
        seen = set()
        for m in range(8):
            mat, size, _ = self._matrix_and_mask(text, ec)
            del mat, size
            seen.add(gen_qr.format_bits_masked(ec, m))
        self.assertEqual(len(seen), 8, "掩码号未正确编入格式信息")


class TestMatrixStructure(unittest.TestCase):
    def test_size_matches_version(self):
        for text, ec in (("A", "L"), ("http://x.y", "M"), ("x" * 100, "L")):
            mat = gen_qr.full_matrix(text, ec)
            n = len(mat)
            self.assertEqual((n - 17) % 4, 0, "尺寸不符合 4v+17")
            for row in mat:
                self.assertEqual(len(row), n, "矩阵非方阵")

    def test_finder_patterns(self):
        mat = gen_qr.full_matrix("http://192.168.1.100:8080", "M")
        n = len(mat)
        for (r0, c0) in ((0, 0), (0, n - 7), (n - 7, 0)):
            for r in range(7):
                for c in range(7):
                    edge = r in (0, 6) or c in (0, 6)
                    core = 2 <= r <= 4 and 2 <= c <= 4
                    self.assertEqual(
                        bool(mat[r0 + r][c0 + c]), edge or core,
                        "定位图案错误 at (%d,%d)" % (r0 + r, c0 + c))

    def test_no_none_cells(self):
        """矩阵里不能残留 None（未填充的格子）。"""
        for text, ec in (("A", "H"), ("http://192.168.1.100:8080", "M")):
            mat = gen_qr.full_matrix(text, ec)
            for r, row in enumerate(mat):
                for c, v in enumerate(row):
                    self.assertIsNotNone(v, "单元格 (%d,%d) 未填充" % (r, c))

    def test_chinese_and_ascii_differ(self):
        a = gen_qr.full_matrix("abc", "M")
        b = gen_qr.full_matrix("中文", "M")
        self.assertNotEqual(a, b)

    def test_too_long_raises(self):
        with self.assertRaises(ValueError):
            gen_qr.full_matrix("x" * 5000, "H")


class TestRenderers(unittest.TestCase):
    def setUp(self):
        self.mat = gen_qr.full_matrix("http://192.168.1.100:8080", "M")

    def test_svg_structure(self):
        svg = gen_qr.render_svg(self.mat, module=8, quiet=4)
        self.assertIn("<svg", svg)
        self.assertIn("viewBox", svg)
        self.assertTrue(svg.rstrip().endswith("</svg>"))
        self.assertIn("<path", svg)

    def test_svg_scales(self):
        small = gen_qr.render_svg(self.mat, module=4)
        large = gen_qr.render_svg(self.mat, module=16)
        self.assertGreater(len(large), len(small))

    def test_pbm_header(self):
        blob = gen_qr.render_pbm(self.mat, scale=2, quiet=4)
        self.assertTrue(blob.startswith(b"P4"))
        n = len(self.mat)
        w = (n + 8) * 2
        self.assertIn(("%d %d" % (w, w)).encode(), blob[:40])

    def test_terminal_render(self):
        out = gen_qr.render_terminal(self.mat)
        self.assertIn("\u2588", out)
        self.assertGreater(len(out.splitlines()), len(self.mat))

    def test_quiet_zone_present(self):
        """静区（白边）必须存在，否则扫描器可能无法定位。"""
        svg = gen_qr.render_svg(self.mat, module=8, quiet=0)
        svg_q = gen_qr.render_svg(self.mat, module=8, quiet=4)
        self.assertNotEqual(svg, svg_q)


if __name__ == "__main__":
    unittest.main(verbosity=2)
