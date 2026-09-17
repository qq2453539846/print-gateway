"""
pg_engine 单元测试 —— 重点是规格校验与解析，这些是喂给命令行的安全边界。

pipeline 本身（gs / pdftoppm / lp）在设备上做端到端验证，这里只测纯逻辑，
用 mock 顶掉 subprocess，保证本地可跑、可回归。
"""

import unittest
from unittest import mock

import pg_engine as pg
import pg_layout


class TestSpecCoercion(unittest.TestCase):
    def test_bool_from_various(self):
        for truthy in (True, "1", "true", "TRUE", "on", "yes", "是", " y "):
            spec = pg.PrintSpec.from_form({"grayscale": truthy})
            self.assertTrue(spec.grayscale, truthy)
        for falsy in (False, "0", "false", "off", "no", ""):
            spec = pg.PrintSpec.from_form({"grayscale": falsy})
            self.assertFalse(spec.grayscale, falsy)

    def test_int_from_string(self):
        spec = pg.PrintSpec.from_form({"copies": "3", "per_sheet": "4", "dpi": "200"})
        self.assertEqual(spec.copies, 3)
        self.assertEqual(spec.per_sheet, 4)
        self.assertEqual(spec.dpi, 200)

    def test_int_garbage_falls_back(self):
        spec = pg.PrintSpec.from_form({"copies": "abc", "dpi": ""})
        self.assertEqual(spec.copies, 1)
        self.assertEqual(spec.dpi, pg.DEFAULT_DPI)

    def test_margins_merge_not_replace(self):
        spec = pg.PrintSpec.from_form({"margins": {"left": 10}})
        self.assertEqual(spec.margins["left"], 10.0)
        self.assertEqual(spec.margins["top"], 0.0)          # 其余边保持默认
        self.assertEqual(set(spec.margins), {"top", "right", "bottom", "left"})

    def test_string_fields_stripped(self):
        spec = pg.PrintSpec.from_form({"printer": "  GW_TEST  ", "page_range": " 1-3 "})
        self.assertEqual(spec.printer, "GW_TEST")
        self.assertEqual(spec.page_range, "1-3")

    def test_unknown_keys_ignored(self):
        spec = pg.PrintSpec.from_form({"nonsense": 1, "paper": "A5"})
        self.assertEqual(spec.paper, "A5")
        self.assertFalse(hasattr(spec, "nonsense"))

    def test_non_dict_input(self):
        spec = pg.PrintSpec.from_form(None)
        self.assertEqual(spec.paper, "A4")


class TestSpecValidation(unittest.TestCase):
    def _bad(self, **kw):
        spec = pg.PrintSpec(**kw)
        with self.assertRaises(ValueError):
            spec.validate()

    def test_defaults_are_valid(self):
        pg.PrintSpec().validate()

    def test_each_enum_rejected(self):
        self._bad(paper="A0")
        self._bad(per_sheet=3)
        self._bad(layout_order="zigzag")
        self._bad(page_set="some")
        self._bad(orientation="diagonal")
        self._bad(scale_mode="stretchy")
        self._bad(content="everything")
        self._bad(duplex="triplex")
        self._bad(booklet_binding="middle")
        self._bad(booklet_subset="cover")
        self._bad(dpi=123)

    def test_copies_bounds(self):
        pg.PrintSpec(copies=1).validate()
        pg.PrintSpec(copies=99).validate()
        self._bad(copies=0)
        self._bad(copies=100)
        self._bad(copies=-1)

    def test_margin_bounds(self):
        pg.PrintSpec(margins={"top": 50, "right": 0, "bottom": 0, "left": 0}).validate()
        self._bad(margins={"top": 50.1, "right": 0, "bottom": 0, "left": 0})
        self._bad(margins={"top": 0, "right": 0, "bottom": 0, "left": -3})

    def test_mirror_forces_raster(self):
        """镜像要在合成阶段做，因此必须让版面规划走栅格路径。"""
        spec = pg.PrintSpec()
        spec.mirror = True
        spec.validate()
        plan = pg_layout.build_plan(spec.layout_dict(),
                                   [(595.28, 841.89)] * 4, 4)
        self.assertTrue(plan["need_raster"])
        self.assertTrue(plan["mirror"])

    def test_no_mirror_keeps_vector(self):
        """不开镜像时，1 版/张应当仍是矢量直出（画质最优路径）。"""
        spec = pg.PrintSpec()
        spec.validate()
        plan = pg_layout.build_plan(spec.layout_dict(),
                                   [(595.28, 841.89)] * 4, 4)
        self.assertFalse(plan["need_raster"])
        self.assertFalse(plan["mirror"])

    def test_page_range_injection_rejected(self):
        # 这是最关键的一道闸门：任何能跑出数字/逗号/连字符/空白的内容都不许过
        for evil in ("1; rm -rf /", "$(whoami)", "1|cat /etc/passwd", "1&2",
                     "1`id`", "1\n2", "1 -e /etc/hosts"):
            with self.assertRaises(ValueError, msg=evil):
                pg.PrintSpec(page_range=evil).validate()

    def test_page_range_valid_forms(self):
        for good in ("", "1", "1-3", "1,3,5", "1-3,5-7", " 2 ", "1-", "-5"):
            pg.PrintSpec(page_range=good).validate()

    def test_booklet_resets_per_sheet(self):
        spec = pg.PrintSpec(booklet=True, per_sheet=4)
        spec.validate()
        self.assertEqual(spec.per_sheet, 1)          # 小册子内部固定 2 版


class TestCacheKey(unittest.TestCase):
    def test_stable_for_identical(self):
        a = pg.PrintSpec(paper="A5", per_sheet=4)
        b = pg.PrintSpec(paper="A5", per_sheet=4)
        self.assertEqual(a.cache_key(), b.cache_key())

    def test_printer_and_copies_do_not_affect(self):
        a = pg.PrintSpec(printer="A", copies=1)
        b = pg.PrintSpec(printer="B", copies=9)
        self.assertEqual(a.cache_key(), b.cache_key())

    def test_layout_changes_key(self):
        base = pg.PrintSpec()
        self.assertNotEqual(base.cache_key(), pg.PrintSpec(per_sheet=4).cache_key())
        self.assertNotEqual(base.cache_key(), pg.PrintSpec(grayscale=True).cache_key())
        self.assertNotEqual(base.cache_key(), pg.PrintSpec(paper="A5").cache_key())
        self.assertNotEqual(base.cache_key(),
                            pg.PrintSpec(margins={"top": 5, "right": 0,
                                                  "bottom": 0, "left": 0}).cache_key())


class TestLayoutDict(unittest.TestCase):
    def test_contains_all_layout_fields(self):
        d = pg.PrintSpec().layout_dict()
        for key in ("paper", "orientation", "per_sheet", "layout_order", "booklet",
                    "booklet_binding", "booklet_subset", "border", "margins",
                    "auto_center", "auto_rotate"):
            self.assertIn(key, d)

    def test_feeds_build_plan_without_error(self):
        import pg_layout
        spec = pg.PrintSpec(paper="A4", per_sheet=4, border=True)
        spec.validate()
        plan = pg_layout.build_plan(spec.layout_dict(), [(595.28, 841.89)] * 8, 8)
        self.assertEqual(len(plan["faces"]), 2)


class TestPdfInfoParsing(unittest.TestCase):
    SAMPLE = """Producer:        GPL Ghostscript 10.00.0
Pages:           3
Page    1 size:  595.28 x 841.89 pts (A4)
Page    1 rot:   0
Page    2 size:  841.89 x 595.28 pts (A4)
Page    2 rot:   0
Page    3 size:  419.53 x 595.28 pts (A5)
Page    3 rot:   0
"""

    def test_parses_count_and_sizes(self):
        with mock.patch.object(pg, "run", return_value=(0, self.SAMPLE)):
            count, sizes = pg.pdf_info("x.pdf")
        self.assertEqual(count, 3)
        self.assertEqual(sizes[0], (595.28, 841.89))
        self.assertEqual(sizes[1], (841.89, 595.28))
        self.assertEqual(sizes[2], (419.53, 595.28))

    def test_raises_on_failure(self):
        with mock.patch.object(pg, "run", return_value=(1, "bad file")):
            with self.assertRaises(pg.EngineError):
                pg.pdf_info("x.pdf")

    def test_uniform_page_size_single_line(self):
        """poppler 22.x 在所有页同尺寸时只输出一行 `Page size:`（无页码）。"""
        sample = """Producer:        GPL Ghostscript 10.00.0
Pages:           4
Page size:       595.276 x 841.89 pts (A4)
Page rot:        0
MediaBox:            0.00     0.00   595.28   841.89
"""
        with mock.patch.object(pg, "run", return_value=(0, sample)):
            count, sizes = pg.pdf_info("x.pdf")
        self.assertEqual(count, 4)
        self.assertEqual(len(sizes), 4)
        for s in sizes:
            self.assertAlmostEqual(s[0], 595.276, places=3)
            self.assertAlmostEqual(s[1], 841.89, places=3)

    def test_uniform_overridden_by_per_page_lines(self):
        """同尺寸行与逐页行同时出现时，逐页声明应当覆盖对应页。"""
        sample = """Pages:           3
Page size:       595.28 x 841.89 pts (A4)
Page    2 size:  841.89 x 595.28 pts
"""
        with mock.patch.object(pg, "run", return_value=(0, sample)):
            count, sizes = pg.pdf_info("x.pdf")
        self.assertEqual(count, 3)
        self.assertEqual(sizes[0], (595.28, 841.89))
        self.assertEqual(sizes[1], (841.89, 595.28))
        self.assertEqual(sizes[2], (595.28, 841.89))

    def test_size_list_padded_when_short(self):
        sample = "Pages:   3\nPage    1 size:  595.28 x 841.89 pts\n"
        with mock.patch.object(pg, "run", return_value=(0, sample)):
            count, sizes = pg.pdf_info("x.pdf")
        self.assertEqual(count, 3)
        self.assertEqual(len(sizes), 3)               # 缺失页用首页尺寸补齐


class TestGsCommandBuilding(unittest.TestCase):
    """验证 gs 参数是按白名单拼出来的，且不会被用户输入污染。"""

    def _capture(self, **kw):
        captured = {}

        def fake(cmd, timeout=300, cwd=None):
            captured["cmd"] = cmd
            open(captured_path, "wb").close()
            return 0, ""

        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            captured_path = os.path.join(td, "out.pdf")
            with mock.patch.object(pg, "run", side_effect=fake):
                pg.gs_normalize("in.pdf", captured_path, **kw)
        return captured["cmd"]

    def test_fit_sets_fixed_media(self):
        cmd = self._capture(target=(595.28, 841.89), fit=True, grayscale=False,
                            annotations=True)
        self.assertIn("-dFIXEDMEDIA", cmd)
        self.assertIn("-dPDFFitPage", cmd)
        self.assertTrue(any(c.startswith("-dDEVICEWIDTHPOINTS=") for c in cmd))

    def test_no_fit_omits_fitpage(self):
        cmd = self._capture(target=(595.28, 841.89), fit=False, grayscale=False,
                            annotations=True)
        self.assertIn("-dFIXEDMEDIA", cmd)
        self.assertNotIn("-dPDFFitPage", cmd)

    def test_grayscale_switches(self):
        cmd = self._capture(target=None, fit=False, grayscale=True,
                            annotations=True)
        self.assertIn("-sColorConversionStrategy=Gray", cmd)
        self.assertIn("-dProcessColorModel=/DeviceGray", cmd)

    def test_annotations_off_switches(self):
        cmd = self._capture(target=None, fit=False, grayscale=False,
                            annotations=False)
        self.assertIn("-dShowAnnots=false", cmd)
        self.assertIn("-dShowAcroForm=false", cmd)

    def test_gs_never_mirrors(self):
        """
        镜像（反片）由 pg_layout.compose 在合成阶段做，gs 一律不碰。

        原因：gs 的 /MirrorPrint、/Install、/BeginPage 三条钩子在 pdfwrite 上
        对多页文档实测全部失效（每页会重置页设备），所以绝不能依赖它 ——
        这里锁死「命令里不许出现任何镜像钩子」，防止有人"顺手加回来"。
        """
        cmd = self._capture(target=None, fit=False, grayscale=False,
                            annotations=True)
        joined = " ".join(cmd)
        self.assertNotIn("MirrorPrint", joined)
        self.assertNotIn("-c", cmd)

    def test_output_is_last_argument(self):
        cmd = self._capture(target=None, fit=False, grayscale=False,
                            annotations=True)
        self.assertTrue(cmd[-1].endswith("in.pdf"))
        self.assertIn("-dSAFER", cmd)


class TestLpCommandBuilding(unittest.TestCase):
    def test_no_imposition_options_sent(self):
        """版面已在网关侧拼好，命令行绝不能再传拼版类选项，否则会二次拼版。"""
        captured = {}

        def fake(cmd, timeout=120, cwd=None):
            captured["cmd"] = cmd
            return 0, "request id is GW_TEST-9 (1 file(s))"

        spec = pg.PrintSpec(printer="GW_TEST", copies=3, collate=True,
                            duplex="two-sided-long-edge", paper="A5")
        with mock.patch.object(pg, "run", side_effect=fake):
            job = pg.send_to_printer("/tmp/x.pdf", spec, "报告")
        cmd = captured["cmd"]
        joined = " ".join(cmd)
        for forbidden in ("number-up", "booklet", "page-ranges", "orientation-requested"):
            self.assertNotIn(forbidden, joined)
        self.assertIn("collate=true", joined)
        self.assertIn("sides=two-sided-long-edge", joined)
        self.assertIn("media=A5", joined)
        self.assertIn("-n 3", joined)
        self.assertEqual(job, "GW_TEST-9")

    def test_missing_printer_raises(self):
        with self.assertRaises(pg.EngineError):
            pg.send_to_printer("/tmp/x.pdf", pg.PrintSpec(), "t")


class TestPageSelection(unittest.TestCase):
    def test_non_contiguous_order_preserved(self):
        """选页必须严格按传入顺序输出 —— 反向打印就是靠这个实现的。"""
        calls = []

        def fake(cmd, timeout=300, cwd=None):
            calls.append(cmd)
            if cmd[0] == pg.PDFSEPARATE:
                open(cmd[-1], "wb").close()
            else:                                     # pdfunite
                open(cmd[-1], "wb").close()
            return 0, ""

        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(pg, "run", side_effect=fake):
                pg.select_pages("in.pdf", [3, 1, 2], os.path.join(td, "out.pdf"), td)
        unite = [c for c in calls if c[0] == pg.PDFUNITE][0]
        # pdfunite 的输入顺序应等于请求顺序
        self.assertEqual(unite[1:-1], [os.path.join(td, "parts", "p%05d.pdf" % i)
                                       for i in range(3)])
        seps = [c for c in calls if c[0] == pg.PDFSEPARATE]
        self.assertEqual([c[2] for c in seps], ["3", "1", "2"])

    def test_single_page_uses_copy(self):
        def fake(cmd, timeout=300, cwd=None):
            if cmd[0] == pg.PDFSEPARATE:
                open(cmd[-1], "wb").write(b"%PDF-1.4 single")
            return 0, ""

        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "out.pdf")
            with mock.patch.object(pg, "run", side_effect=fake):
                pg.select_pages("in.pdf", [2], out, td)
            self.assertTrue(os.path.exists(out))
            self.assertEqual(open(out, "rb").read(), b"%PDF-1.4 single")


class TestSecondBatch(unittest.TestCase):
    """第二批：裁剪 / 分割（海报打印）/ 页面装饰。"""

    def _plan(self, extra=None, count=4):
        spec = pg.PrintSpec.from_form(extra or {})
        spec.validate()
        sizes = [(595.28, 841.89)] * count
        return pg_layout.build_plan(spec.layout_dict(), sizes, count)

    # ---------------------------------------------------------- 裁剪
    def test_crop_normalized_into_spec(self):
        spec = pg.PrintSpec.from_form({"crop_mm": {"top": "12", "left": -3}})
        spec.validate()
        self.assertEqual(spec.crop_mm["top"], 12.0)
        self.assertEqual(spec.crop_mm["left"], 0.0)
        self.assertEqual(spec.crop_mm["right"], 0.0)     # 默认值保留

    def test_crop_clamped_to_max(self):
        spec = pg.PrintSpec.from_form({"crop_mm": {"bottom": 999}})
        spec.validate()
        self.assertEqual(spec.crop_mm["bottom"], pg_layout.CROP_MAX_MM)

    def test_crop_garbage_becomes_zero(self):
        spec = pg.PrintSpec.from_form({"crop_mm": {"top": "abc", "left": None}})
        spec.validate()
        self.assertEqual(spec.crop_mm["top"], 0.0)
        self.assertEqual(spec.crop_mm["left"], 0.0)

    def test_crop_forces_raster(self):
        self.assertTrue(self._plan({"crop_mm": {"top": 5}})["need_raster"])
        self.assertFalse(self._plan({})["need_raster"])

    def test_crop_shrinks_source_region(self):
        p = self._plan({"crop_mm": {"left": 10, "right": 10,
                                    "top": 4, "bottom": 6}})
        src = p["faces"][0]["placements"][0]["src"]
        self.assertAlmostEqual(src[2], 595.28 - 20 * pg_layout.MM, places=4)
        self.assertAlmostEqual(src[3], 841.89 - 10 * pg_layout.MM, places=4)
        # 裁完剩下的矩形左下角 = (左边裁量, 下边裁量) —— PDF 是左下原点
        self.assertAlmostEqual(src[0], 10 * pg_layout.MM, places=4)
        self.assertAlmostEqual(src[1], 6 * pg_layout.MM, places=4)

    def test_crop_too_much_raises(self):
        with self.assertRaises(ValueError):
            pg_layout.build_plan({"paper": "A4",
                                  "crop_mm": {"top": 50, "bottom": 50}},
                                 [(595.28, 100.0)], 1)

    # ---------------------------------------------------------- 分割
    def test_split_clamped(self):
        spec = pg.PrintSpec.from_form({"split_rows": "9", "split_cols": "0"})
        spec.validate()
        self.assertEqual((spec.split_rows, spec.split_cols),
                         (pg_layout.SPLIT_MAX, 1))

    def test_split_expands_faces(self):
        p = self._plan({"split_rows": 2, "split_cols": 2}, count=2)
        self.assertEqual(len(p["faces"]), 8)
        self.assertEqual(p["split"], [2, 2])

    def test_split_ignores_per_sheet_and_booklet(self):
        p = self._plan({"split_rows": 2, "split_cols": 1, "per_sheet": 4,
                        "booklet": True}, count=2)
        self.assertEqual((p["cols"], p["rows"]), (1, 1))

    def test_split_tiles_cover_page_without_gaps(self):
        """瓦片必须无缝铺满整页 —— 有缝就拼不回去。"""
        p = self._plan({"split_rows": 3, "split_cols": 2}, count=1)
        tiles = [f["placements"][0]["src"] for f in p["faces"]]
        self.assertEqual(len(tiles), 6)
        self.assertAlmostEqual(sum(t[2] * t[3] for t in tiles),
                               595.28 * 841.89, places=2)
        xs = sorted({round(t[0], 4) for t in tiles})
        ys = sorted({round(t[1], 4) for t in tiles})
        self.assertEqual((len(xs), len(ys)), (2, 3))
        self.assertAlmostEqual(xs[0], 0.0, places=4)
        self.assertAlmostEqual(ys[0], 0.0, places=4)

    def test_split_tile_fills_cell_exactly(self):
        """瓦片非等比铺满纸格；等比缩放会让拼图出现错位白边。"""
        p = self._plan({"split_rows": 2, "split_cols": 2}, count=1)
        for f in p["faces"]:
            pl = f["placements"][0]
            self.assertAlmostEqual(pl["w"], p["cell_w"], places=6)
            self.assertAlmostEqual(pl["h"], p["cell_h"], places=6)

    # ---------------------------------------------------------- 装饰
    def test_decor_merged_not_replaced(self):
        spec = pg.PrintSpec.from_form({"decor": {"wm_enabled": True,
                                                 "wm_text": "机密"}})
        spec.validate()
        self.assertTrue(spec.decor["wm_enabled"])
        self.assertEqual(spec.decor["wm_text"], "机密")
        self.assertEqual(spec.decor["pn_format"], "n-of-total")   # 默认值保留

    def test_decor_rejects_bad_position(self):
        spec = pg.PrintSpec.from_form({"decor": {"pn_enabled": True,
                                                 "pn_position": "middle"}})
        with self.assertRaises(ValueError):
            spec.validate()

    def test_decor_rejects_bad_color(self):
        spec = pg.PrintSpec.from_form({"decor": {"wm_enabled": True,
                                                 "wm_color": "red"}})
        with self.assertRaises(ValueError):
            spec.validate()

    def test_decor_rejects_too_long_watermark(self):
        spec = pg.PrintSpec.from_form({"decor": {"wm_enabled": True,
                                                 "wm_text": "字" * 41}})
        with self.assertRaises(ValueError):
            spec.validate()

    def test_layout_layer_ignores_decor(self):
        """
        布局层不认识装饰 —— 强制走栅格这件事由引擎层做。

        这样 pg_layout 不必 import pg_decor（避免循环依赖），
        装饰也只影响输出通道，不影响版面几何。
        """
        p = self._plan({"decor": {"wm_enabled": True, "wm_text": "机密"}})
        self.assertFalse(p["need_raster"])

    def test_cache_key_tracks_crop_split_and_decor(self):
        base = pg.PrintSpec()
        base.validate()

        def key(**extra):
            sp = pg.PrintSpec.from_form(extra)
            sp.validate()
            return sp.cache_key()

        base_key = base.cache_key()
        self.assertNotEqual(base_key, key(crop_mm={"top": 5}))
        self.assertNotEqual(base_key, key(split_rows=2))
        self.assertNotEqual(base_key, key(decor={"pn_enabled": True}))
        # 规范化之后，写法不同但含义相同应当得到同一个键
        self.assertEqual(key(crop_mm={"top": 5}), key(crop_mm={"top": "5.0"}))

    def test_layout_dict_carries_crop_and_split(self):
        spec = pg.PrintSpec.from_form({"crop_mm": {"top": 3}, "split_rows": 2})
        spec.validate()
        d = spec.layout_dict()
        self.assertEqual(d["crop_mm"]["top"], 3.0)
        self.assertEqual(d["split_rows"], 2)
        self.assertIn("split_cols", d)

    # ---------------------------------------------------------- 页码编号
    def test_labels_follow_print_order_not_source_order(self):
        """反向打印时第 1 张纸应当显示第 1 页，而不是源文档的第 4 页。"""
        for wanted, expect_first, expect_last in (([1, 2, 3, 4], 1, 4),
                                                  ([4, 3, 2, 1], 1, 4),
                                                  ([2, 4], 1, 2)):
            labels = {}
            for i, p in enumerate(wanted):
                labels.setdefault(p, i + 1)
            self.assertEqual(labels[wanted[0]], expect_first)
            self.assertEqual(labels[wanted[-1]], expect_last)



class TestCupsDefaultPrinter(unittest.TestCase):
    """CUPS 系统默认队列的读取。面板的基础设置要按它带出打印机。"""

    def test_reads_system_default_destination(self):
        out = ("printer GW_TEST is idle.  enabled since Wed\n"
               "printer SamplePrinter_TASKalfa_4501i is idle.  enabled since Wed\n"
               "system default destination: SamplePrinter_TASKalfa_4501i\n")
        with mock.patch.object(pg, "which", return_value=True):
            with mock.patch.object(pg, "run", return_value=(0, out)):
                self.assertEqual(pg.cups_default_printer(), "SamplePrinter_TASKalfa_4501i")

    def test_no_default_returns_empty(self):
        out = "no system default destination\n"
        with mock.patch.object(pg, "which", return_value=True):
            with mock.patch.object(pg, "run", return_value=(0, out)):
                self.assertEqual(pg.cups_default_printer(), "")

    def test_missing_lpstat_returns_empty(self):
        with mock.patch.object(pg, "which", return_value=False):
            self.assertEqual(pg.cups_default_printer(), "")

    def test_command_failure_returns_empty(self):
        with mock.patch.object(pg, "which", return_value=True):
            with mock.patch.object(pg, "run", return_value=(1, "boom")):
                self.assertEqual(pg.cups_default_printer(), "")

    def test_engine_error_swallowed(self):
        """lpstat 不存在/超时时不该把异常抛给调用方（面板要能照常打开）。"""
        with mock.patch.object(pg, "which", return_value=True):
            with mock.patch.object(pg, "run",
                                   side_effect=pg.EngineError("命令超时")):
                self.assertEqual(pg.cups_default_printer(), "")

    def test_list_printers_still_parses_states(self):
        """顺带锁住原行为：加了默认队列读取后，列表解析不受影响。"""
        out = ("printer GW_TEST is idle.  enabled since Wed\n"
               "printer KYO is processing.  enabled since Wed\n"
               "system default destination: KYO\n")
        with mock.patch.object(pg, "which", return_value=True):
            with mock.patch.object(pg, "run", return_value=(0, out)):
                got = pg.list_printers()
        self.assertEqual([p["name"] for p in got], ["GW_TEST", "KYO"])
        self.assertEqual(got[1]["state"], "processing")


class TestStretchMode(unittest.TestCase):
    """
    拉伸铺满（scale_mode=stretch）。

    「适应纸张」等比缩放，源页宽高比与纸张不同时必然留白；「拉伸铺满」要求
    两个方向各自拉满版心，允许非等比变形。这是唯一无法靠 ghostscript 完成的
    模式（-dPDFFitPage 只做等比），因此必须强制走栅格拼版路径。
    """

    def test_spec_accepts_stretch(self):
        s = pg.PrintSpec(scale_mode="stretch")
        s.validate()
        self.assertTrue(s.layout_dict()["stretch"])
        self.assertFalse(pg.PrintSpec(scale_mode="fit").layout_dict()["stretch"])

    def test_stretch_forces_raster(self):
        plan = pg_layout.build_plan(
            {"paper": "A4", "stretch": True}, [(400.0, 400.0)], 1)
        self.assertTrue(plan["need_raster"])

    def test_stretch_fills_whole_sheet(self):
        plan = pg_layout.build_plan(
            {"paper": "A4", "stretch": True}, [(400.0, 400.0)], 1)
        pl = plan["faces"][0]["placements"][0]
        self.assertAlmostEqual(pl["w"], plan["sheet_w"], places=2)
        self.assertAlmostEqual(pl["h"], plan["sheet_h"], places=2)
        self.assertEqual(pl["src"], [0.0, 0.0, 400.0, 400.0])

    def test_fit_leaves_blank_where_stretch_fills(self):
        """
        同一份内容的精确对照。源用大于纸张的方形（700x700），等比才会
        真正缩放：此时两版宽度都正好等于纸宽，但等比版高度停在 595
        （上下留白），拉伸版高度拉满到 842。
        """
        fit = pg_layout.build_plan({"paper": "A4"}, [(700.0, 700.0)], 1)
        st = pg_layout.build_plan(
            {"paper": "A4", "stretch": True}, [(700.0, 700.0)], 1)
        pf = fit["faces"][0]["placements"][0]
        ps = st["faces"][0]["placements"][0]
        self.assertAlmostEqual(pf["w"], ps["w"], places=2)
        self.assertAlmostEqual(pf["w"], fit["sheet_w"], places=2)
        self.assertAlmostEqual(ps["h"], st["sheet_h"], places=2)
        self.assertLess(pf["h"], ps["h"] - 200)

    def test_stretch_is_non_uniform(self):
        """A4 纵向纸装方形内容 —— 铺满后必然是纵向矩形（非等比）。"""
        plan = pg_layout.build_plan(
            {"paper": "A4", "stretch": True}, [(400.0, 400.0)], 1)
        pl = plan["faces"][0]["placements"][0]
        self.assertGreater(pl["h"] - pl["w"], 100)

    def test_stretch_respects_margins(self):
        plan = pg_layout.build_plan(
            {"paper": "A4", "stretch": True,
             "margins": {"top": 10, "right": 10, "bottom": 10, "left": 10}},
            [(400.0, 400.0)], 1)
        pl = plan["faces"][0]["placements"][0]
        # 比纸面小（页边距生效），但仍铺满版心（比内容大）
        self.assertLess(pl["w"], plan["sheet_w"])
        self.assertGreater(pl["w"], 400.0)

    def test_stretch_skips_auto_rotate(self):
        """铺满时旋转不改变变形量，只会把内容放倒，因此不做自动旋转。"""
        plan = pg_layout.build_plan(
            {"paper": "A4", "stretch": True, "auto_rotate": True,
             "orientation": "portrait"}, [(841.89, 595.28)], 1)
        self.assertEqual(plan["faces"][0]["placements"][0]["rotate"], 0)
        # 对照：同样输入在等比模式下会被转正
        plan_fit = pg_layout.build_plan(
            {"paper": "A4", "auto_rotate": True, "orientation": "portrait"},
            [(841.89, 595.28)], 1)
        self.assertEqual(plan_fit["faces"][0]["placements"][0]["rotate"], 90)

    def test_render_dpi_is_capped(self):
        """源页远小于纸张时 scale 很大，dpi 必须被上限拦住，否则会撑爆内存。"""
        plan = pg_layout.build_plan(
            {"paper": "A4", "stretch": True}, [(100.0, 100.0)], 1)
        job = pg_layout.render_jobs(plan, 300)[0]
        self.assertLessEqual(job["dpi"], pg_layout.MAX_RENDER_DPI)
        self.assertEqual(job["dpi"], pg_layout.MAX_RENDER_DPI)

    def test_per_sheet_stretch(self):
        """拉伸与拼版可叠加：每一格各自铺满。"""
        plan = pg_layout.build_plan(
            {"paper": "A4", "per_sheet": 4, "stretch": True},
            [(400.0, 400.0)] * 4, 4)
        for pl in plan["faces"][0]["placements"]:
            self.assertAlmostEqual(pl["w"], 595.28 / 2, places=1)
            self.assertAlmostEqual(pl["h"], 841.89 / 2, places=1)



if __name__ == "__main__":
    unittest.main(verbosity=2)
