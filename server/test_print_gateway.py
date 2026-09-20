#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
print_gateway 核心逻辑测试。
不依赖设备，纯本地跑。重点验证手写 multipart 解析器与配置决策逻辑。
"""

import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import re                                                     # noqa: E402
import tempfile                                               # noqa: E402
import urllib.parse                                           # noqa: E402

import print_gateway as pg                                    # noqa: E402
import pg_admin                                               # noqa: E402
import pg_decor                                               # noqa: E402
import pg_engine                                              # noqa: E402
import pg_layout                                              # noqa: E402
import pg_sticker                                             # noqa: E402
from pg_engine import PrintSpec                               # noqa: E402


def build_multipart(boundary, fields=None, files=None):
    """构造真实的 multipart/form-data 请求体。"""
    parts = []
    for k, v in (fields or {}).items():
        parts.append(
            b"--" + boundary.encode() + b"\r\n"
            b'Content-Disposition: form-data; name="' + k.encode() + b'"\r\n\r\n'
            + str(v).encode() + b"\r\n"
        )
    for name, filename, content in (files or []):
        parts.append(
            b"--" + boundary.encode() + b"\r\n"
            b'Content-Disposition: form-data; name="' + name.encode()
            + b'"; filename="' + filename.encode() + b'"\r\n'
            b"Content-Type: application/octet-stream\r\n\r\n"
            + content + b"\r\n"
        )
    parts.append(b"--" + boundary.encode() + b"--\r\n")
    return b"".join(parts)


class TestMultipart(unittest.TestCase):
    BOUNDARY = "----WebKitFormBoundaryABC123xyz"

    def _ctype(self):
        return "multipart/form-data; boundary=%s" % self.BOUNDARY

    @staticmethod
    def _triples(files):
        """v2 的 files 是 dict 列表（多了 content_type），折成三元组让断言好读。"""
        return [(f["name"], f["filename"], f["data"]) for f in files]

    def test_fields_only(self):
        body = build_multipart(self.BOUNDARY, fields={"copies": "3", "pages": "1-2"})
        fields, files = pg.parse_multipart(body, self._ctype())
        self.assertEqual(fields["copies"], "3")
        self.assertEqual(fields["pages"], "1-2")
        self.assertEqual(files, [])

    def test_single_file(self):
        content = b"%PDF-1.4 fake pdf content here"
        body = build_multipart(self.BOUNDARY,
                               fields={"copies": "1"},
                               files=[("file", "test.pdf", content)])
        fields, files = pg.parse_multipart(body, self._ctype())
        self.assertEqual(len(files), 1)
        name, filename, data = self._triples(files)[0]
        self.assertEqual(name, "file")
        self.assertEqual(filename, "test.pdf")
        self.assertEqual(data, content, "文件内容必须逐字节一致")
        self.assertEqual(fields["copies"], "1")

    def test_binary_file_integrity(self):
        """二进制内容不能被破坏 —— 曾经最容易踩的坑。"""
        content = bytes(range(256)) * 40
        body = build_multipart(self.BOUNDARY, files=[("file", "blob.bin", content)])
        _, files = pg.parse_multipart(body, self._ctype())
        self.assertEqual(self._triples(files)[0][2], content)
        self.assertEqual(len(self._triples(files)[0][2]), 10240)

    def test_content_with_crlf_inside(self):
        """文件内容里含有 \\r\\n 时不能被误切分。"""
        content = b"line1\r\nline2\r\nline3\r\n" * 50
        body = build_multipart(self.BOUNDARY, files=[("file", "a.txt", content)])
        _, files = pg.parse_multipart(body, self._ctype())
        self.assertEqual(self._triples(files)[0][2], content)

    def test_content_containing_boundary_like_text(self):
        """内容里出现类似 boundary 的字符串也不能破坏解析。"""
        content = b"----WebKitFormBoundaryXYZ not the real one"
        body = build_multipart(self.BOUNDARY, files=[("file", "a.pdf", content)])
        _, files = pg.parse_multipart(body, self._ctype())
        self.assertEqual(self._triples(files)[0][2], content)

    def test_multiple_files(self):
        body = build_multipart(self.BOUNDARY, files=[
            ("file", "one.pdf", b"AAA"),
            ("file", "two.pdf", b"BBB"),
        ])
        _, files = pg.parse_multipart(body, self._ctype())
        self.assertEqual(len(files), 2)

    def test_chinese_filename(self):
        content = b"hello"
        body = build_multipart(self.BOUNDARY, files=[("file", "测试文档.pdf", content)])
        _, files = pg.parse_multipart(body, self._ctype())
        self.assertEqual(self._triples(files)[0][1], "测试文档.pdf")
        self.assertEqual(self._triples(files)[0][2], content)

    def test_empty_file_skipped(self):
        """
        浏览器未选文件时会提交一个 filename 与内容都为空的 part，必须忽略。

        v2 重写解析器时漏了这条判断，症状是用户「明明选了文件」却收到
        「文件 是空的」—— 空 part 把真实文件挤掉了。这里钉住它。
        """
        body = build_multipart(self.BOUNDARY, fields={"copies": "1"},
                               files=[("file", "", b"")])
        fields, files = pg.parse_multipart(body, self._ctype())
        self.assertEqual(files, [], "空 part 不应被当作文件")
        self.assertEqual(fields["copies"], "1")

    def test_empty_content_skipped(self):
        """有文件名但内容为 0 字节 —— 同样不该当成可打印文件。"""
        body = build_multipart(self.BOUNDARY, files=[("file", "a.pdf", b"")])
        _, files = pg.parse_multipart(body, self._ctype())
        self.assertEqual(files, [])

    def test_empty_part_does_not_hide_real_file(self):
        """空 part 与真文件同时提交时，真文件必须留下来。"""
        body = build_multipart(self.BOUNDARY, files=[
            ("file", "", b""),
            ("file", "real.pdf", b"%PDF-1.4 real"),
        ])
        _, files = pg.parse_multipart(body, self._ctype())
        self.assertEqual(len(files), 1)
        self.assertEqual(self._triples(files)[0][1], "real.pdf")
        self.assertEqual(self._triples(files)[0][2], b"%PDF-1.4 real")

    def test_quoted_boundary(self):
        body = build_multipart(self.BOUNDARY, fields={"a": "b"})
        ctype = 'multipart/form-data; boundary="%s"' % self.BOUNDARY
        fields, _ = pg.parse_multipart(body, ctype)
        self.assertEqual(fields["a"], "b")

    def test_quoted_boundary_with_extra_params(self):
        """带 charset 等额外参数 + 引号 boundary 的完整头部。"""
        body = build_multipart(self.BOUNDARY,
                               fields={"copies": "2"},
                               files=[("file", "x.pdf", b"%PDF-1.4")])
        ctype = ('multipart/form-data; boundary="%s"; charset=UTF-8' % self.BOUNDARY)
        fields, files = pg.parse_multipart(body, ctype)
        self.assertEqual(fields["copies"], "2")
        self.assertEqual(self._triples(files)[0][2], b"%PDF-1.4")

    def test_boundary_with_spaces_around_equals(self):
        body = build_multipart(self.BOUNDARY, fields={"k": "v"})
        ctype = "multipart/form-data;boundary=%s" % self.BOUNDARY
        fields, _ = pg.parse_multipart(body, ctype)
        self.assertEqual(fields["k"], "v")

    def test_no_boundary_returns_empty(self):
        fields, files = pg.parse_multipart(b"garbage", "text/plain")
        self.assertEqual(fields, {})
        self.assertEqual(files, [])

    def test_field_with_chinese_value(self):
        body = build_multipart(self.BOUNDARY, fields={"note": "双面打印"})
        fields, _ = pg.parse_multipart(body, self._ctype())
        self.assertEqual(fields["note"], "双面打印")


class TestMergePdfs(unittest.TestCase):
    """批量打印：多个文件按顺序合并成一份。"""

    def test_single_file_just_copied(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.pdf")
            out = os.path.join(d, "out.pdf")
            with open(src, "wb") as fh:
                fh.write(b"%PDF-1.4 hello")
            pg_engine.merge_pdfs([src], out)
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(), b"%PDF-1.4 hello")

    def test_single_file_same_path_is_noop(self):
        """
        上传的就是 PDF 时，parts[0] 与 target 是同一个路径。

        早期版本在这里直接 copyfile，抛 SameFileError 让上传接口 500 ——
        这条用例专门钉住它。
        """
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.pdf")
            with open(src, "wb") as fh:
                fh.write(b"%PDF-1.4 x")
            pg_engine.merge_pdfs([src], src)              # 不该抛异常
            with open(src, "rb") as fh:
                self.assertEqual(fh.read(), b"%PDF-1.4 x")

    def test_empty_list_raises(self):
        with self.assertRaises(pg_engine.EngineError):
            pg_engine.merge_pdfs([], "out.pdf")

    def test_missing_file_raises(self):
        with self.assertRaises(pg_engine.EngineError):
            pg_engine.merge_pdfs([os.path.join("no", "such", "a.pdf")], "out.pdf")


class TestUploadLimits(unittest.TestCase):
    """上传相关的上限常量要与界面文案对得上。"""

    def test_max_files_sane(self):
        self.assertGreaterEqual(pg.MAX_FILES, 2)
        self.assertLessEqual(pg.MAX_FILES, 50)

    def test_extension_sets_disjoint(self):
        self.assertEqual(pg.PDF_EXTS & pg.IMAGE_EXTS, set())

    def test_no_executable_extensions_allowed(self):
        for bad in (".exe", ".sh", ".php", ".js", ".html", ".zip", ".docx"):
            self.assertNotIn(bad, pg.PDF_EXTS | pg.IMAGE_EXTS, bad)


class TestPageTemplate(unittest.TestCase):
    """
    界面模板的回归测试。

    这里钉的是最容易悄悄坏掉的东西：**控件 id 与 JS 取值、字段名与引擎**。
    一旦脱节，用户改了设置却被静默忽略 —— 没有报错、没有日志，只有
    「怎么设了没反应」。所以宁可让测试来守。
    """

    def test_placeholders_present(self):
        self.assertIn("__MAXMB__", pg.PAGE)
        self.assertIn("__MAXFILES__", pg.PAGE)

    def test_new_controls_present(self):
        ids = ("wmEnabled", "wmText", "wmSize", "wmColor", "wmOpacity",
               "wmAngle", "wmTile", "pnEnabled", "pnPosition", "pnFormat",
               "pnSize", "pnStart", "pnSkipFirst", "hfHeader", "hfFooter",
               "hfPosition", "hfSize", "hfMargin",
               "cropTop", "cropBottom", "cropLeft", "cropRight",
               "splitRows", "splitCols")
        for cid in ids:
            self.assertIn('id="%s"' % cid, pg.PAGE, "界面缺少控件 " + cid)

    def test_file_input_accepts_multiple(self):
        self.assertRegex(pg.PAGE, r'<input type="file"[^>]*\bmultiple\b')

    def test_sections_present(self):
        for title in ("水印与页码", "裁剪与分割"):
            self.assertIn("<summary>%s</summary>" % title, pg.PAGE, title)

    def _spec_body(self):
        body = pg.PAGE.split("function spec(){", 1)[1]
        return body.split("\n}", 1)[0]

    def test_spec_keys_are_engine_fields(self):
        """spec() 顶层键必须都是 PrintSpec 的字段，否则后端会静默丢弃。"""
        keys = set(re.findall(r"^    ([a-z_]+):", self._spec_body(), re.M))
        self.assertTrue(keys, "没能从 spec() 里解析出字段")
        unknown = keys - set(PrintSpec.__dataclass_fields__)
        self.assertEqual(unknown, set(), "界面提交了引擎不认识的字段：%s" % unknown)

    def test_decor_keys_are_decor_fields(self):
        """decor 里的键必须都是 pg_decor.Decor 的字段。"""
        body = self._spec_body()
        i = body.index("decor: {")
        j = body.index("\n    },", i)
        keys = set(re.findall(r"^\s+([a-z_]+):", body[i:j], re.M))
        self.assertTrue(keys, "没能从 decor 块里解析出字段")
        unknown = keys - set(pg_decor.Decor.__dataclass_fields__)
        self.assertEqual(unknown, set(), "界面提交了装饰模块不认识的字段：%s" % unknown)

    def test_every_control_is_bound(self):
        """
        每个带 id 的输入控件都应当出现在绑定清单里。

        漏一个的后果是「改了不刷新预览」，很容易在加控件时忘掉。
        """
        listed = re.search(r"var ctrl = \[(.*?)\];", pg.PAGE, re.S)
        self.assertIsNotNone(listed, "找不到控件绑定清单")
        bound = set(re.findall(r"'([A-Za-z]+)'", listed.group(1)))
        for cid in ("wmEnabled", "wmText", "cropTop", "splitRows", "hfFooter"):
            self.assertIn(cid, bound, "控件 %s 没有绑定刷新" % cid)

    def test_split_disables_imposition_controls(self):
        """分割打印与每版页数/小册子互斥，界面上必须真的禁用。"""
        body = pg.PAGE.split("function syncUI(){", 1)[1].split("\n}", 1)[0]
        self.assertIn("$('#perSheet').disabled", body)
        self.assertIn("$('#booklet').disabled", body)

    def test_booklet_uses_icon_buttons_not_selects(self):
        """
        小册子的两个选项由下拉改成了图标按钮。

        参数名必须仍是 bookletBinding / bookletSubset —— 后端、缓存键、控件
        绑定清单都按这两个名字取值，换了名字会「界面能点但实际不生效」。
        """
        self.assertIn('id="bookletBinding" value="left"', pg.PAGE)
        self.assertIn('id="bookletSubset" value="both"', pg.PAGE)
        self.assertNotIn('<select id="booklet', pg.PAGE)
        self.assertEqual(pg.PAGE.count('data-for="bookletBinding"'), 2)
        self.assertEqual(pg.PAGE.count('data-for="bookletSubset"'), 3)

    def test_booklet_buttons_write_back_and_refresh(self):
        """图标按钮要把值写回隐藏输入框并触发 change，否则改了不刷新预览。"""
        self.assertIn("bkPick(b.getAttribute('data-for')", pg.PAGE)
        self.assertIn("sel.dispatchEvent(new Event('change'))", pg.PAGE)
        self.assertIn("bkPaint();", pg.PAGE.split("function syncUI(){", 1)[1])

    def test_booklet_summary_matches_engine_pairing(self):
        """
        缩略示意的页号必须与引擎实际拼版一致。

        两边各算一遍的话，图迟早开始骗人 —— 那比没有图更糟。
        """
        for n in (1, 2, 4, 6, 8, 14, 40):
            s = pg.booklet_summary(n)
            sides = pg_layout.booklet_sides(n, "left", "both")
            self.assertEqual(s["padded"], -(-n // 4) * 4)
            self.assertEqual(s["sheets"], s["padded"] // 4)
            self.assertEqual(s["blank"], s["padded"] - n)
            self.assertEqual(
                s["faces"],
                [{k: f[k] for k in ("sheet", "kind", "left", "right")}
                 for f in sides[:2]], "页数 %d" % n)
        self.assertEqual(pg.booklet_summary(0),
                         {"padded": 0, "sheets": 0, "blank": 0, "faces": []})

    def test_right_binding_is_just_a_swap(self):
        """
        右装订 = 每面左右对调。

        前端只做这个「对调」，配对公式一概留在后端 —— 这条测试就是那份等价
        性的证明，前端可以放心不做计算。
        """
        for n in (4, 8, 12):
            left = pg.booklet_summary(n)["faces"]
            right = [{k: f[k] for k in ("sheet", "kind", "left", "right")}
                     for f in pg_layout.booklet_sides(n, "right", "both")[:2]]
            swapped = [{"sheet": f["sheet"], "kind": f["kind"],
                        "left": f["right"], "right": f["left"]} for f in left]
            self.assertEqual(swapped, right, "页数 %d" % n)

    def test_mode_labels_cover_all_engine_modes(self):
        """引擎会返回 vector / raster / tile 三种模式，界面都得认得。"""
        for mode in ("vector", "raster", "tile"):
            self.assertIn("%s:" % mode, pg.PAGE, mode)




class TestPrinterSelection(unittest.TestCase):
    """
    面板「基础设置」默认选中哪台打印机。

    优先级：显式 --printer（锁定）> CUPS 系统默认 > 列表第一项。
    """

    def _handler(self, locked=False, default=""):
        h = pg.Handler.__new__(pg.Handler)      # 不跑 __init__，避免真开端口
        h.default_queue = default
        h.printer_locked = locked
        return h

    def _patch(self, printers, cups):
        return (
            mock.patch.object(pg_engine, "list_printers", return_value=printers),
            mock.patch.object(pg_engine, "cups_default_printer", return_value=cups),
        )

    def _run(self, h, printers, cups):
        a, b = self._patch(printers, cups)
        with a, b:
            return h._printers_payload()

    def test_follows_cups_default_not_first(self):
        """回归：GW_TEST 按字母序排第一，但不该被选中。"""
        h = self._handler(default="GW_TEST")
        got = self._run(h, [{"name": "GW_TEST"}, {"name": "PrinterB"}],
                        "PrinterB")
        self.assertEqual(got["defaultQueue"], "PrinterB")

    def test_explicit_cli_printer_wins(self):
        h = self._handler(locked=True, default="GW_TEST")
        got = self._run(h, [{"name": "GW_TEST"}, {"name": "PrinterB"}], "PrinterB")
        self.assertEqual(got["defaultQueue"], "GW_TEST")

    def test_no_cups_default_falls_back_to_startup_value(self):
        h = self._handler(default="GW_TEST")
        got = self._run(h, [{"name": "GW_TEST"}], "")
        self.assertEqual(got["defaultQueue"], "GW_TEST")

    def test_default_not_in_list_is_ignored(self):
        """CUPS 默认指向已删除的队列时，不能把不存在的名字送出去。"""
        h = self._handler(default="GW_TEST")
        got = self._run(h, [{"name": "GW_TEST"}], "Deleted_Queue")
        self.assertEqual(got["defaultQueue"], "GW_TEST")

    def test_no_printers_gives_empty_default(self):
        h = self._handler(default="")
        got = self._run(h, [], "")
        self.assertEqual(got["defaultQueue"], "")
        self.assertEqual(got["printers"], [])

    def test_empty_spec_printer_follows_cups(self):
        h = self._handler(default="GW_TEST")
        a, b = self._patch([{"name": "KYO"}], "KYO")
        with a, b:
            spec = h._load_spec({"spec": {"paper": "A4"}})
        self.assertEqual(spec.printer, "KYO")

    def test_explicit_spec_printer_wins(self):
        h = self._handler(default="GW_TEST")
        a, b = self._patch([{"name": "KYO"}], "KYO")
        with a, b:
            spec = h._load_spec({"spec": {"paper": "A4", "printer": "OTHER"}})
        self.assertEqual(spec.printer, "OTHER")



class TestPageCachePolicy(unittest.TestCase):
    """
    页面与队列接口的缓存策略。

    回归背景：HTML 响应最初**没有** Cache-Control，手机浏览器据此缓存了整页，
    用户拿到的仍是旧版界面（旧 JS 只选「列表第一项」），表现成
    「服务端改了、面板没变」。这类问题最难查 —— 服务端一切正常。
    """

    def _html_response(self):
        """只在内存里跑 HTML 分支，不开真实 socket。"""
        h = pg.Handler.__new__(pg.Handler)
        h.path = "/"
        sent = {}

        def fake_send(code, body, ctype, extra=None):
            sent.update(code=code, body=body, ctype=ctype, extra=extra or {})

        h._send = fake_send
        h.do_GET()
        return sent

    def test_html_forbids_caching(self):
        sent = self._html_response()
        self.assertEqual(sent["code"], 200)
        self.assertIn("text/html", sent["ctype"])
        self.assertIn("no-store", sent["extra"].get("Cache-Control", ""))

    def test_html_placeholders_all_replaced(self):
        """占位符没替换掉会直接把 __XXX__ 展示给用户。"""
        body = self._html_response()["body"].decode("utf-8")
        self.assertNotIn("__MAXMB__", body)
        self.assertNotIn("__MAXFILES__", body)
        self.assertNotIn("__VERSION__", body)
        self.assertIn(pg.VERSION, body)

    def test_printers_fetch_bypasses_cache(self):
        """默认队列是实时读的，前端 fetch 必须绕开 HTTP 缓存。"""
        self.assertRegex(
            pg.PAGE,
            r"fetch\(api\('/api/printers'\)\s*,\s*\{[^}]*cache:\s*'no-store'")

    def test_printer_select_opts_out_of_autofill(self):
        """否则浏览器会恢复上次选中的队列，盖掉「跟随 CUPS 默认」。"""
        self.assertRegex(pg.PAGE,
                         r'<select id="printer"[^>]*autocomplete="off"')

    def test_default_is_reapplied_after_autofill_restore(self):
        """选中值要在下一帧再确认一次（表单状态恢复的时机可能更晚）。"""
        self.assertIn("function pickDefault", pg.PAGE)
        self.assertIn("setTimeout(function(){ if (dq) pickDefault(sel, dq); }, 0)",
                      pg.PAGE)

    def test_bfcache_restore_refetches(self):
        self.assertIn("addEventListener('pageshow'", pg.PAGE)
        self.assertIn("e.persisted", pg.PAGE)


class TestJobDirectEntry(unittest.TestCase):
    """
    /?job=<id> 直达设置页。

    回归背景：安卓 App 从系统「打开方式」拿到 PDF 后由**原生层**上传
    （WebView 里的 JS 读不到 content://），上传完只拿到一个作业号。
    页面必须能靠这个作业号把作业取回来并直接铺开设置界面，
    否则用户还得再手动选一次文件 —— 那这个入口就白做了。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pgjob-")
        self.store = pg.JobStore(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _handler(self, path):
        """只跑路由分支，不开真实 socket。"""
        h = pg.Handler.__new__(pg.Handler)
        h.path = path
        h.store = self.store
        h.token = ""                    # 空口令 => _authed() 直接放行
        sent = {}
        h._json = lambda obj, code=200: sent.update(code=code, obj=obj)
        h._err = lambda msg, code=400: sent.update(code=code, obj={"error": msg})
        return h, sent

    def _make_job(self, filename="合同.pdf", pages=3, files=2):
        job = self.store.create()
        job.filename = filename
        job.pages = pages
        job.files = files
        return job

    def test_route_returns_job(self):
        job = self._make_job()
        h, sent = self._handler("/api/job?id=%s" % job.id)
        h.do_GET()
        self.assertEqual(sent["code"], 200)
        self.assertEqual(sent["obj"], {"id": job.id, "filename": "合同.pdf",
                                       "pages": 3, "files": 2})

    def test_unknown_job_rejected(self):
        h, sent = self._handler("/api/job?id=000000000000")
        h.do_GET()
        self.assertNotEqual(sent["code"], 200)
        self.assertIn("error", sent["obj"])

    def test_missing_id_rejected(self):
        h, sent = self._handler("/api/job")
        h.do_GET()
        self.assertNotEqual(sent["code"], 200)

    def test_bad_id_shape_rejected(self):
        """作业号有格式校验，别让它变成路径穿越。"""
        h, sent = self._handler("/api/job?id=../../etc/passwd")
        h.do_GET()
        self.assertNotEqual(sent["code"], 200)

    def test_page_reads_job_param(self):
        self.assertIn("QS.get('job')", pg.PAGE)
        self.assertIn("'/api/job?id='", pg.PAGE)

    def test_page_forwards_token_to_apis(self):
        """服务端开 --token 时，页面自己能打开不代表接口能调 ——
        页面内所有 API 请求都得带上口令，否则每个都 401。"""
        self.assertIn("QS.get('t')", pg.PAGE)
        self.assertIn("function api(p)", pg.PAGE)
        self.assertNotIn("fetch('/api/printers'", pg.PAGE)
        self.assertNotIn("fetch('/api/upload'", pg.PAGE)

    def test_both_entries_share_after_job(self):
        """上传与直达必须共用同一段铺开逻辑，否则迟早跑偏。"""
        self.assertIn("function afterJob(j)", pg.PAGE)
        self.assertGreaterEqual(pg.PAGE.count("afterJob(j)"), 3)

    def test_job_starts_with_zero_files(self):
        self.assertEqual(self.store.create().files, 0)

    def test_upload_records_file_count(self):
        import inspect
        src = inspect.getsource(pg.Handler._api_upload)
        self.assertIn("job.files = len(items)", src)


class TestAppDownload(unittest.TestCase):
    """
    GET /app —— 网关托管安卓 App 包下载。

    背景：手机扫码/手填进网页后，要能直接点「下载 App」装上，
    从此 App 可被系统「打开方式」唤起做原生直打。
    关键点：**必须放在鉴权检查之前** —— 扫码进来的用户没有口令，
    也还没装 App，被 401 拦住就没法自助下载了。
    """

    def _handler(self, path, apk_path, apk_file=None):
        h = pg.Handler.__new__(pg.Handler)
        h.path = path
        h.token = "secret"                 # 故意设口令，验证 /app 不走鉴权
        h.apk_path = apk_path
        h.apk_name = "x.apk"
        sent = {}
        h._err = lambda msg, code=400: sent.update(code=code, obj={"error": msg})
        h._send_file = lambda p: sent.update(code=200, obj={"file": p})
        return h, sent

    def test_app_without_apk_config_is_404(self):
        h, sent = self._handler("/app", apk_path="")
        h.do_GET()
        self.assertEqual(sent["code"], 404)
        self.assertIn("error", sent["obj"])

    def test_app_with_missing_file_is_404(self):
        h, sent = self._handler("/app", apk_path="/nonexistent/never.apk")
        h.do_GET()
        self.assertEqual(sent["code"], 404)

    def test_app_with_apk_serves_file(self):
        d = tempfile.TemporaryDirectory(prefix="pgapp-")
        try:
            apk = os.path.join(d.name, "print-selfservice.apk")
            with open(apk, "wb") as fh:
                fh.write(b"APKDATA")
            h, sent = self._handler("/app", apk_path=apk)
            h.do_GET()
            self.assertEqual(sent["code"], 200)
            self.assertEqual(sent["obj"]["file"], apk)
        finally:
            d.cleanup()

    def test_app_bypasses_auth(self):
        """/app 在 _authed() 之前返回 —— 即便口令不符也要能下载。"""
        d = tempfile.TemporaryDirectory(prefix="pgapp-")
        try:
            apk = os.path.join(d.name, "a.apk")
            with open(apk, "wb") as fh:
                fh.write(b"X")
            h, sent = self._handler("/app", apk_path=apk)
            # 若 /app 走了鉴权分支，_authed 会因给定口令为空而 401
            h.do_GET()
            self.assertEqual(sent["code"], 200)
        finally:
            d.cleanup()

    def test_healthz_reports_app_key(self):
        import inspect
        src = inspect.getsource(pg.Handler.do_GET)
        self.assertIn('"app":', src)
        self.assertIn("os.path.exists", src)

    def test_page_has_download_button(self):
        self.assertIn('id="appWrap"', pg.PAGE)
        self.assertIn('/healthz', pg.PAGE)
        self.assertIn("j.app", pg.PAGE)

    def test_main_wires_apk_arg(self):
        import inspect
        src = inspect.getsource(pg.main)
        self.assertIn('Handler.apk_path = args.apk', src)


class TestPreviewBuildSplit(unittest.TestCase):
    """
    预览档与出纸档分开构建：预览走 PREVIEW_BUILD_DPI（快），出纸走 spec.dpi（清晰）。

    两档必须各有各的缓存槽与工作目录 —— 最坏的失败形态是「先预览再打印」时
    把 144dpi 的产物送进打印队列：纸照样出得来，只是糊，没人会当场发现。
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pgw_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        saved = pg.Handler.store
        pg.Handler.store = pg.JobStore(self.root)
        self.addCleanup(setattr, pg.Handler, "store", saved)
        pg._CACHE.clear()
        self.addCleanup(pg._CACHE.clear)

        self.h = object.__new__(pg.Handler)      # 只借 _build，不用起 socket
        self.job = pg.Handler.store.create()
        self.job.source_pdf = os.path.join(self.root, "src.pdf")
        open(self.job.source_pdf, "wb").close()
        self.calls = []

    def _patch(self):
        def fake_build(spec, src, workdir, preview=False):
            self.calls.append({"preview": preview, "work": workdir,
                               "dpi": pg_engine.PREVIEW_BUILD_DPI if preview
                               else spec.dpi})
            os.makedirs(workdir, exist_ok=True)
            pdf = os.path.join(workdir, "final.pdf")
            open(pdf, "wb").close()
            return {"pdf": pdf, "plan": {}, "mode": "raster", "pages": 6,
                    "source_pages": 10, "sheet": (842.0, 595.0), "notes": []}

        def fake_preview(pdf, workdir, *a, **kw):
            out = []
            for n in (1, 2):
                p = os.path.join(workdir, "preview-%d.png" % n)
                open(p, "wb").close()
                out.append(p)
            return out

        mock.patch.object(pg_engine, "build_final",
                          side_effect=fake_build).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(pg_engine, "make_preview",
                          side_effect=fake_preview).start()
        self.addCleanup(mock.patch.stopall)

    def _spec(self, dpi=300):
        spec = PrintSpec.from_form({"paper": "A4", "dpi": dpi})
        spec.validate()
        return spec

    def test_preview_build_is_flagged_preview(self):
        self._patch()
        self.h._build(self.job, self._spec(), with_preview=True)
        self.assertEqual([c["preview"] for c in self.calls], [True])
        self.assertEqual(self.calls[0]["dpi"], pg_engine.PREVIEW_BUILD_DPI)

    def test_print_build_uses_full_dpi(self):
        self._patch()
        self.h._build(self.job, self._spec(), with_preview=False)
        self.assertEqual([c["preview"] for c in self.calls], [False])
        self.assertEqual(self.calls[0]["dpi"], 300)

    def test_preview_then_print_builds_twice_in_separate_dirs(self):
        self._patch()
        spec = self._spec()
        pv = self.h._build(self.job, spec, with_preview=True)
        pr = self.h._build(self.job, spec, with_preview=False)

        self.assertEqual([c["preview"] for c in self.calls], [True, False])
        self.assertNotEqual(self.calls[0]["work"], self.calls[1]["work"])
        self.assertNotEqual(pv["pdf"], pr["pdf"])
        self.assertTrue(pv["images"])
        self.assertFalse(pr["images"])

    def test_print_never_hands_back_the_preview_file(self):
        """先预览再打印，进队列的必须是出纸档 —— 这条错了就是「能出纸但糊」。"""
        self._patch()
        spec = self._spec()
        preview_pdf = self.h._build(self.job, spec, with_preview=True)["pdf"]
        print_pdf = self.h._build(self.job, spec, with_preview=False)["pdf"]
        self.assertNotEqual(print_pdf, preview_pdf)
        self.assertIn("final.pdf", print_pdf)
        self.assertNotIn(pg._PV, print_pdf)

    def test_each_slot_is_cached_independently(self):
        self._patch()
        spec = self._spec()
        self.h._build(self.job, spec, with_preview=True)
        self.h._build(self.job, spec, with_preview=True)
        self.h._build(self.job, spec, with_preview=False)
        self.h._build(self.job, spec, with_preview=False)
        self.assertEqual([c["preview"] for c in self.calls], [True, False])

    def test_preview_dir_matches_the_fallback_serve_image_uses(self):
        """
        /img 在缓存被挤掉时会直接去磁盘上找 build-<slot>/preview-N.png，
        目录名必须和这里落盘的一致，否则预览会整片 404。
        """
        self._patch()
        spec = self._spec()
        self.h._build(self.job, spec, with_preview=True)
        key = spec.cache_key()
        pv_dir = os.path.join(self.job.dir, "build-" + key + pg._PV)
        self.assertTrue(os.path.exists(os.path.join(pv_dir, "preview-1.png")))
        self.assertTrue(os.path.exists(os.path.join(pv_dir, "final.pdf")))

        # 出纸档落在不带后缀的目录里，就是 /img 回退时先找的那个
        self.h._build(self.job, spec, with_preview=False)
        self.assertTrue(os.path.exists(
            os.path.join(self.job.dir, "build-" + key, "final.pdf")))

    def test_preview_slot_key_is_not_a_valid_url_key(self):
        """缓存槽后缀只存在于服务端：URL 里的 k 必须仍是纯 16 位十六进制。"""
        self._patch()
        spec = self._spec()
        entry = self.h._build(self.job, spec, with_preview=True)
        self.assertTrue(pg.HASH_RE.match(spec.cache_key()))
        self.assertFalse(pg.HASH_RE.match(spec.cache_key() + pg._PV))
        self.assertTrue(entry["images"])


class TestAdminAccess(unittest.TestCase):
    """
    `/admin` 的三道关：**只许内网** → **必须配了口令** → **口令校验**。

    这个页面能改 DNS、能签证书、能看到域名配置，暴露在公网上等于把
    DNSPod 令牌交出去。所以判据要一条条钉住。
    """

    def _handler(self, path, client="192.168.1.50", admin_token="s3cret",
                 cookie=None, header=None, body=None):
        h = pg.Handler.__new__(pg.Handler)         # 不跑 __init__，避免真开端口
        h.path = path
        h.client_address = (client, 43210)
        h.admin_token = admin_token
        h.tls_enabled = True
        h.tls_port = 8443
        h.headers = {}
        if cookie is not None:
            h.headers["Cookie"] = cookie
        if header is not None:
            h.headers["X-Admin-Token"] = header
        sent = {}
        h._send = lambda code, b, ctype, extra=None: sent.update(
            code=code, body=b, ctype=ctype, extra=extra or {})
        h._err = lambda msg, code=400: sent.update(code=code, obj={"error": msg})
        h._json = lambda obj, code=200: sent.update(code=code, obj=obj)
        h._json_body = lambda: (body or {})
        return h, sent

    def _good_cookie(self, token="s3cret"):
        h, _ = self._handler("/admin", admin_token=token)
        return "pg_admin=" + h._admin_cookie()

    # ---------------------------------------------------- 第一道关：内网
    def test_public_source_gets_404_even_with_correct_token(self):
        """公网来源一律 404 —— 连「这里有管理页」都不该被知道。"""
        for client in ("1.2.3.4", "8.8.8.8", "2001:4860:4860::8888"):
            h, sent = self._handler("/admin?t=s3cret", client=client)
            h._admin_dispatch("/admin", False)
            self.assertEqual(sent["code"], 404, client)
            self.assertIn("error", sent["obj"])

    def test_public_source_gets_404_on_api_too(self):
        h, sent = self._handler("/admin/api/status", client="1.2.3.4",
                                cookie=self._good_cookie())
        h._admin_dispatch("/admin/api/status", False)
        self.assertEqual(sent["code"], 404)

    def test_lan_source_is_allowed_through(self):
        h, sent = self._handler("/admin", cookie=self._good_cookie())
        h._admin_dispatch("/admin", False)
        self.assertEqual(sent["code"], 200)

    # ---------------------------------------------------- 第二道关：配了口令
    def test_disabled_when_no_admin_token_configured(self):
        """没配口令 = 功能关闭，不是「无口令可进」。"""
        h, sent = self._handler("/admin", admin_token="")
        h._admin_dispatch("/admin", False)
        self.assertEqual(sent["code"], 404)
        self.assertIn("--admin-token", sent["obj"]["error"])

    def test_disabled_even_with_matching_empty_query(self):
        h, sent = self._handler("/admin?t=", admin_token="")
        h._admin_dispatch("/admin", False)
        self.assertEqual(sent["code"], 404)

    # ---------------------------------------------------- 第三道关：口令
    def test_no_credentials_prompts_401(self):
        h, sent = self._handler("/admin")
        h._admin_dispatch("/admin", False)
        self.assertEqual(sent["code"], 401)
        self.assertIn("口令", sent["body"].decode("utf-8"))

    def test_wrong_token_rejected(self):
        h, sent = self._handler("/admin?t=wrong")
        h._admin_dispatch("/admin", False)
        self.assertEqual(sent["code"], 401)

    def test_query_token_sets_cookie_and_redirects(self):
        h, sent = self._handler("/admin?t=s3cret")
        h._admin_dispatch("/admin", False)
        self.assertEqual(sent["code"], 302)
        self.assertEqual(sent["extra"]["Location"], "/admin")
        cookie = sent["extra"]["Set-Cookie"]
        self.assertIn("pg_admin=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_cookie_never_contains_plaintext_token(self):
        """Cookie 被同步/被日志记录时不该泄露口令。"""
        h, sent = self._handler("/admin?t=s3cret")
        h._admin_dispatch("/admin", False)
        self.assertNotIn("s3cret", sent["extra"]["Set-Cookie"])

    def test_cookie_grants_access(self):
        h, sent = self._handler("/admin", cookie=self._good_cookie())
        h._admin_dispatch("/admin", False)
        self.assertEqual(sent["code"], 200)

    def test_cookie_of_another_token_rejected(self):
        h, sent = self._handler("/admin", admin_token="s3cret",
                                cookie=self._good_cookie("other-token"))
        h._admin_dispatch("/admin", False)
        self.assertEqual(sent["code"], 401)

    def test_cookie_with_extra_pairs_still_parsed(self):
        """浏览器会带上其它 Cookie（主页面也种了一个），别被前面的干扰。"""
        h, sent = self._handler("/admin",
                                cookie="other=1; " + self._good_cookie() + "; x=2")
        h._admin_dispatch("/admin", False)
        self.assertEqual(sent["code"], 200)

    def test_header_token_grants_access(self):
        h, sent = self._handler("/admin", header="s3cret")
        h._admin_dispatch("/admin", False)
        self.assertEqual(sent["code"], 200)

    def test_unknown_admin_path_404(self):
        """路径白名单：别把 Handler 的私有方法暴露成路由。"""
        h, sent = self._handler("/admin/../etc/passwd", cookie=self._good_cookie())
        h._admin_dispatch("/admin/../etc/passwd", False)
        self.assertEqual(sent["code"], 404)

    def test_admin_api_post_not_reachable_by_get(self):
        h, sent = self._handler("/admin/api/ddns", cookie=self._good_cookie())
        h._admin_dispatch("/admin/api/ddns", False)
        self.assertEqual(sent["code"], 404)

    def test_admin_page_post_not_reachable(self):
        h, sent = self._handler("/admin", cookie=self._good_cookie(), body={})
        h._admin_dispatch("/admin", True)
        self.assertEqual(sent["code"], 404)

    def test_hidden_path_looks_like_404(self):
        """被拒时的响应要与普通 404 无差别，别泄露「有管理页」。"""
        public, sent1 = self._handler("/admin?t=s3cret", client="8.8.8.8")
        public._admin_dispatch("/admin", False)
        nopath, sent2 = self._handler("/definitely-not-here")
        nopath._err("未找到", 404)
        self.assertEqual(sent1["obj"], sent2["obj"])


class TestAdminRouting(unittest.TestCase):
    """admin 必须走**独立口令**，因此在打印鉴权之前分流。"""

    def test_admin_dispatched_before_print_auth_in_get(self):
        import inspect
        src = inspect.getsource(pg.Handler.do_GET)
        # 用 self._authed() 而不是 _authed —— 注释里也提到了这个名字
        self.assertLess(src.index("_admin_dispatch(path, False)"),
                        src.index("self._authed()"))

    def test_admin_dispatched_before_print_auth_in_post(self):
        import inspect
        src = inspect.getsource(pg.Handler.do_POST)
        self.assertLess(src.index("_admin_dispatch(path, True)"),
                        src.index("self._authed()"))

    def test_admin_page_has_expected_controls(self):
        for marker in ('id="root"', 'id="sub"', 'id="provider"',
                       'id="bIssue"', 'id="bSync"', 'id="bVerify"',
                       'id="bSaveCred"', 'id="certLog"'):
            self.assertIn(marker, pg_admin.ADMIN_PAGE, marker)

    def test_admin_page_posts_to_its_own_apis(self):
        for route in ("/admin/api/config", "/admin/api/verify",
                      "/admin/api/ddns", "/admin/api/cert"):
            self.assertIn(route.split("/api/")[1], pg_admin.ADMIN_PAGE, route)

    def test_healthz_reports_tls(self):
        import inspect
        src = inspect.getsource(pg.Handler.do_GET)
        self.assertIn("tls", src)


class TestTlsWiring(unittest.TestCase):
    """TLS 监听：证书缺失不算致命，但**公网端口免密是硬拒绝**。"""

    def test_main_wires_tls_args(self):
        import inspect
        src = inspect.getsource(pg.main)
        for arg in ("--tls-port", "--tls-cert", "--tls-key", "--tls-bind",
                    "--admin-token"):
            self.assertIn(arg, src)

    def test_main_refuses_tls_without_token(self):
        """启用 TLS 端口却没给口令时，必须拒绝启动而不是「先开着再说」。"""
        import inspect
        src = inspect.getsource(pg.main)
        self.assertIn("必须同时设置 --token", src)
        idx = src.index("必须同时设置 --token")
        # 拒绝的那段里得有 return 2（非零退出）
        self.assertIn("return 2", src[idx: idx + 400])

    def test_missing_cert_is_warning_not_fatal(self):
        """还没签证书时，内网明文那条路必须照常服务（不能因为没证书就退出）。"""
        import inspect
        src = inspect.getsource(pg.main)
        idx = src.index('LOG.warning("证书缺失')        # 证书判断分支本身
        end = src.index("server_cls", idx)              # 到「起监听」之间
        self.assertNotIn("return 2", src[idx:end])

    def test_dualstack_binds_ipv6(self):
        import socket as _socket
        self.assertEqual(pg.DualStackServer.address_family, _socket.AF_INET6)

    def test_dualstack_disables_v6only(self):
        import inspect
        src = inspect.getsource(pg.DualStackServer.server_bind)
        self.assertIn("IPV6_V6ONLY", src)

    def test_make_tls_context_minimum_version(self):
        """TLS 上下文只认 1.2+，且**不再碰监听套接字**（握手归每连接线程）。"""
        import inspect
        src = inspect.getsource(pg.make_tls_context)
        self.assertIn("TLSv1_2", src)
        self.assertIn("load_cert_chain", src)
        self.assertNotIn("httpd.socket", src)
        self.assertNotIn("wrap_socket", src)

    def test_main_never_wraps_the_listening_socket(self):
        """
        回归 2026-09-19 事故：把 TLS 套在监听套接字上，accept 循环会在握手处阻塞，
        一条「连上不发 ClientHello」的扫描器连接就能永久打死公网入口。
        """
        import inspect
        src = inspect.getsource(pg.main)
        self.assertNotIn("wrap_socket(", src)
        self.assertNotIn("wrap_tls(", src)
        self.assertIn("tls_context", src)

    def test_handler_has_tls_fields(self):
        self.assertTrue(hasattr(pg.Handler, "tls_enabled"))
        self.assertTrue(hasattr(pg.Handler, "tls_port"))


@unittest.skipUnless(socket.has_ipv6, "本机无 IPv6")
class TestDualStackSockets(unittest.TestCase):
    """真起一个双栈监听，确认 IPv4 与 IPv6 都连得上。"""

    def test_ipv4_and_ipv6_both_reach_the_socket(self):
        try:
            srv = pg.DualStackServer(("::", 0), pg.Handler)
        except OSError as exc:
            self.skipTest("无法绑定 IPv6 通配地址：%s" % exc)
        self.addCleanup(srv.server_close)
        port = srv.server_address[1]
        srv.socket.listen(4)
        for family, addr in ((socket.AF_INET, "127.0.0.1"),
                             (socket.AF_INET6, "::1")):
            try:
                conn = socket.socket(family, socket.SOCK_STREAM)
                conn.settimeout(3)
                conn.connect((addr, port))
                conn.close()
            except OSError as exc:
                self.fail("%s 连不上双栈监听：%s" % (addr, exc))


class TestPublicUrlWiring(unittest.TestCase):
    """
    `--public-url` 的启动接线。

    核心判据：**公网可达就必须有口令** —— 与 `--tls-port` 是同一条铁律的两种形态。
    隧道把 TLS 挪到了边缘，但「公网免密」的性质一点没变。
    """

    def test_main_wires_public_url_arg(self):
        import inspect
        self.assertIn("--public-url", inspect.getsource(pg.main))

    def test_main_refuses_public_url_without_token(self):
        import inspect
        src = inspect.getsource(pg.main)
        idx = src.index("指定 --public-url 时必须同时设置 --token")
        # 拒绝的那段里必须有非零退出，而不是「先开着再说」
        self.assertIn("return 2", src[idx: idx + 200])

    def test_bare_host_gets_https_scheme(self):
        """用户常只填域名 —— 得补协议，否则拼出来的是个相对路径。"""
        import inspect
        self.assertIn('"https://" + args.public_url',
                      inspect.getsource(pg.main))

    def test_warns_admin_is_exposed_through_tunnel(self):
        """
        隧道客户端就在局域网内 ⇒ /admin 的「仅限内网」判据会被**反向**绕过
        （判的是源 IP 是否私网，而源 IP 恰好是私网）。这一点必须在启动日志里
        说出来，否则用户会以为三道关还在。
        """
        import inspect
        src = inspect.getsource(pg.main)
        self.assertIn("建议不要设 --admin-token", src)

    def test_override_is_assigned_to_handler(self):
        import inspect
        self.assertIn("Handler.public_url_override = args.public_url",
                      inspect.getsource(pg.main))


def _self_signed_cert(tmpdir):
    """用 openssl 现造一张自签证书。没有 openssl 就返回 None（测试自动跳过）。"""
    exe = shutil.which("openssl")
    if not exe:
        return None
    crt = os.path.join(tmpdir, "t.crt")
    key = os.path.join(tmpdir, "t.key")
    proc = subprocess.run(
        [exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", crt, "-days", "2", "-subj", "/CN=localhost"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if proc.returncode != 0 or not (os.path.isfile(crt) and os.path.isfile(key)):
        return None
    return crt, key


class _EchoHandler(pg.BaseHTTPRequestHandler):
    """只管回 200，不碰作业存储 —— 让测试聚焦在监听与握手行为上。"""

    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):                                  # noqa: A003
        pass


class TestTlsHandshakeOffAcceptLoop(unittest.TestCase):
    """
    TLS 握手必须在**每连接线程**里做，不能在 accept 循环里做。

    回归 2026-09-19 的事故：TLS 套在监听套接字上时，标准库的
    `SSLSocket.accept()` 是「accept + 同步握手」且没有超时 ——
    一条「TCP 连上但不发 ClientHello」的扫描器连接（66.132.x.x / 115.231.x.x），
    就把公网 8443 永久卡死了：`ss -lnt` 的 Recv-Q 顶到 backlog 不归零，
    连设备本机都连不上自己的 8443。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="gw-tls-")
        got = _self_signed_cert(cls.tmp)
        if got is None:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest("本机没有 openssl，无法现造自签证书")
        cls.crt, cls.key = got

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _start(self):
        srv = pg.tls_server_class(False)(("127.0.0.1", 0), _EchoHandler)
        srv.tls_context = pg.make_tls_context(self.crt, self.key)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def _silent(self, port, delay=0.4):
        """连上就不说话 —— 端口扫描器的标准动作。"""
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(conn.close)
        if delay:
            time.sleep(delay)
        return conn

    def _tls_get(self, port, timeout=6.0):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        with raw, ctx.wrap_socket(raw, server_hostname="localhost") as tls:
            tls.settimeout(timeout)
            tls.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            return tls.recv(65536)

    def test_silent_connection_does_not_wedge_the_loop(self):
        """核心回归：一条静默连接不能挡住后面的正常请求。"""
        port = self._start()
        self._silent(port)
        body = self._tls_get(port)
        self.assertIn(b" 200 ", body.split(b"\r\n")[0])

    def test_garbage_handshake_is_dropped_and_server_survives(self):
        """发垃圾字节（不是 ClientHello）也要被丢掉，且不影响后续服务。"""
        port = self._start()
        junk = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(junk.close)
        junk.sendall(b"\x16\x03\x01" + b"not-a-clienthello" * 4)
        time.sleep(0.4)
        body = self._tls_get(port)
        self.assertIn(b" 200 ", body.split(b"\r\n")[0])

    def test_many_silent_connections_still_serve(self):
        """一堆扫描器同时挂住，正常用户仍要立刻拿到页面。"""
        port = self._start()
        for _ in range(6):
            self._silent(port, delay=0.0)
        time.sleep(0.5)
        body = self._tls_get(port)
        self.assertIn(b" 200 ", body.split(b"\r\n")[0])

    def test_handshake_is_bounded_by_timeout(self):
        """静默连接占用的是「限时的」线程，不是「无限期的」accept 循环。"""
        import inspect
        src = inspect.getsource(pg.TLSHandshakeMixin.process_request_thread)
        self.assertIn("settimeout(TLS_HANDSHAKE_TIMEOUT)", src)
        self.assertGreater(pg.TLS_HANDSHAKE_TIMEOUT, 0)

    def test_get_request_only_accepts(self):
        """get_request 只做 accept，源码里不许出现 wrap_socket。"""
        import inspect
        src = inspect.getsource(pg.TLSHandshakeMixin.get_request)
        self.assertIn("self.socket.accept()", src)
        self.assertNotIn("wrap_socket", src)


class TestListenBacklog(unittest.TestCase):
    """
    backlog 必须靠**类属性**传到内核 —— 构造之后再赋值，对已经 listen 过的
    套接字毫无作用。2026-09-19 实测原来那样写 Send-Q 一直是默认的 5，
    多台手机同时上传就会被拒。
    """

    def test_backlog_is_a_class_attribute(self):
        self.assertIn("request_queue_size", pg.QueueHTTPServer.__dict__)
        self.assertEqual(pg.QueueHTTPServer.request_queue_size, pg.SERVER_BACKLOG)
        self.assertGreaterEqual(pg.SERVER_BACKLOG, 64)
        self.assertEqual(pg.DualStackServer.request_queue_size, pg.SERVER_BACKLOG)

    def test_backlog_reaches_the_kernel(self):
        """真起监听，用 ss 读回 Send-Q，确认不是默认的 5。"""
        if not shutil.which("ss"):
            self.skipTest("本机没有 ss，无法读回监听队列")
        srv = pg.QueueHTTPServer(("127.0.0.1", 0), _EchoHandler)
        self.addCleanup(srv.server_close)
        port = srv.server_address[1]
        try:
            out = subprocess.run(["ss", "-lnt"], capture_output=True,
                                 text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest("执行 ss 失败：%s" % exc)
        row = [ln for ln in out.splitlines() if (":%d " % port) in ln]
        if not row:
            self.skipTest("ss 输出里没找到端口 %d" % port)
        fields = row[0].split()                    # LISTEN Recv-Q Send-Q Local Peer
        self.assertEqual(int(fields[2]), pg.SERVER_BACKLOG,
                         "backlog 没传到内核，ss 看到的是 %s" % fields[2])


class TestTokenScope(unittest.TestCase):
    """
    口令的作用范围：**公网端口强制、内网明文免密**。

    内网免密的合法性完全建立在「8080 不做 DNAT」上，所以这条边界要钉住 ——
    将来谁把 8080 映射出去，这组测试会提醒他改配置。
    """

    def _handler(self, token="s3cret", on_tls=False, always=False,
                 query="", header=None, path="/api/printers", public_url=""):
        h = pg.Handler.__new__(pg.Handler)
        h.path = path + query
        h.token = token
        h.token_always = always
        h.public_url_override = public_url
        h.headers = {}
        if header is not None:
            h.headers["X-Token"] = header
        h.server = mock.Mock(is_tls=on_tls)
        return h

    def test_lan_plaintext_port_is_open_when_token_set(self):
        """内网明文端口保持免密 —— 否则扫码进来的用户没法用。"""
        self.assertTrue(self._handler(on_tls=False)._authed())

    def test_public_tls_port_requires_token(self):
        self.assertFalse(self._handler(on_tls=True)._authed())

    def test_public_tls_port_accepts_query_token(self):
        self.assertTrue(self._handler(on_tls=True, query="?t=s3cret")._authed())

    def test_public_tls_port_accepts_header_token(self):
        self.assertTrue(self._handler(on_tls=True, header="s3cret")._authed())

    def test_public_tls_port_rejects_wrong_token(self):
        self.assertFalse(self._handler(on_tls=True, query="?t=nope")._authed())

    def test_no_token_configured_means_no_checks_anywhere(self):
        """没配口令时不该因为「少了口令」而拦人（此时公网端口压根起不来）。"""
        self.assertTrue(self._handler(token="", on_tls=True)._authed())

    def test_token_always_covers_plaintext_port(self):
        """--token-always：内网也校验（给「8080 也想映射」的人用）。"""
        self.assertFalse(self._handler(on_tls=False, always=True)._authed())
        self.assertTrue(self._handler(on_tls=False, always=True,
                                      query="?t=s3cret")._authed())

    def test_public_url_covers_plaintext_port(self):
        """
        声明了 --public-url 就等于「公网可达」，明文端口也必须校验口令。

        这条曾经漏掉过：启动校验强制「配了 --public-url 就得配 --token」，
        但 _authed() 的公网判据里没有 public-url ⇒ 内网穿透实例变成**公网免密**，
        正是启动告警里反复提示「绝不能发生」的那件事。
        """
        URL = "https://abc123.kooldns.cn"
        self.assertFalse(self._handler(on_tls=False, public_url=URL)._authed())
        self.assertTrue(self._handler(on_tls=False, public_url=URL,
                                      query="?t=s3cret")._authed())
        self.assertTrue(self._handler(on_tls=False, public_url=URL,
                                      header="s3cret")._authed())
        self.assertFalse(self._handler(on_tls=False, public_url=URL,
                                       query="?t=nope")._authed())

    def test_empty_public_url_keeps_lan_open(self):
        """没声明公网地址时明文端口照旧免密 —— 别顺手把内网那条路也锁上。"""
        self.assertTrue(self._handler(on_tls=False, public_url="")._authed())

    def test_public_url_and_tls_are_equivalent_forms(self):
        """TLS 端口与 --public-url 是同一件事的两种形态，判据要一致。"""
        self.assertEqual(self._handler(on_tls=True)._authed(),
                         self._handler(on_tls=False,
                                       public_url="https://x.kooldns.cn")._authed())

    def test_authed_counts_public_url_as_public(self):
        """防回归：公网判据里必须始终含有 public_url_override。"""
        import inspect
        src = inspect.getsource(pg.Handler._authed)
        self.assertIn("public_url_override", src)

    def test_missing_server_attribute_fails_open_for_lan(self):
        """单元测试/嵌入式调用下没有 server 属性时，按内网处理而不是崩掉。"""
        h = self._handler()
        del h.server
        self.assertTrue(h._authed())

    def test_main_wires_token_scope(self):
        import inspect
        src = inspect.getsource(pg.main)
        self.assertIn("Handler.token_always = bool(args.token_always)", src)
        self.assertIn("tls_server.is_tls = True", src)

    def test_main_warns_about_plaintext_exposure(self):
        """免密这件事必须被说出来，否则没人知道它的前提条件。"""
        import inspect
        src = inspect.getsource(pg.main)
        self.assertIn("不做 DNAT", src)


class TestPublicUrl(unittest.TestCase):
    """公网链接只在 TLS 真起来时才给 —— 否则是条打不开的链接，还不如不给。"""

    def _h(self, tls_enabled=True, tls_port=8443, token="abc123"):
        h = pg.Handler.__new__(pg.Handler)
        h.tls_enabled = tls_enabled
        h.tls_port = tls_port
        h.token = token
        return h

    def _data(self, root="example.com", sub="print"):
        d = pg_admin.default_secrets()
        d["record"] = {"root": root, "sub": sub}
        return d

    def test_builds_full_url(self):
        self.assertEqual(self._h()._public_url(self._data()),
                         "https://print.example.com:8443/?t=abc123")

    def test_omits_port_on_443(self):
        self.assertEqual(self._h(tls_port=443)._public_url(self._data()),
                         "https://print.example.com/?t=abc123")

    def test_empty_when_tls_not_running(self):
        self.assertEqual(self._h(tls_enabled=False)._public_url(self._data()), "")

    def test_empty_without_domain(self):
        self.assertEqual(self._h()._public_url(self._data(root="", sub="")), "")

    def test_no_token_means_no_query(self):
        self.assertEqual(self._h(token="")._public_url(self._data()),
                         "https://print.example.com:8443/")

    # ---------------------------------------------------------- 隧道 / 反代
    # --public-url：TLS 在别处终结（DDNSTO 等内网穿透、反向代理）。
    # 这种部署下网关自己不开 TLS，若仍拿 tls_enabled 当判据，公网码永远是空的。

    def test_override_works_without_local_tls(self):
        h = self._h(tls_enabled=False, tls_port=0)
        h.public_url_override = "https://gw.ddnsto.com"
        self.assertEqual(h._public_url(self._data()),
                         "https://gw.ddnsto.com/?t=abc123")

    def test_override_needs_no_domain(self):
        """隧道不依赖 DNS-01，本来就可以没有域名配置 —— 不能因此不给链接。"""
        h = self._h(tls_enabled=False, tls_port=0)
        h.public_url_override = "https://gw.ddnsto.com"
        self.assertEqual(h._public_url(self._data(root="", sub="")),
                         "https://gw.ddnsto.com/?t=abc123")

    def test_override_strips_trailing_slash(self):
        """用户常带尾斜杠填，不能拼出 //?t= 这种双斜杠。"""
        h = self._h(tls_enabled=False, tls_port=0)
        h.public_url_override = "https://gw.ddnsto.com/"
        self.assertEqual(h._public_url(self._data()),
                         "https://gw.ddnsto.com/?t=abc123")

    def test_override_ignores_surrounding_spaces(self):
        h = self._h(tls_enabled=False, tls_port=0)
        h.public_url_override = "  https://gw.ddnsto.com  "
        self.assertEqual(h._public_url(self._data()),
                         "https://gw.ddnsto.com/?t=abc123")

    def test_override_without_token_gives_bare_url(self):
        h = self._h(tls_enabled=False, tls_port=0, token="")
        h.public_url_override = "https://gw.ddnsto.com"
        self.assertEqual(h._public_url(self._data()), "https://gw.ddnsto.com/")

    def test_override_takes_precedence_over_tls(self):
        """两者同时配时以 --public-url 为准（它是「公网码」指向的那个入口）。"""
        h = self._h(tls_enabled=True, tls_port=8443)
        h.public_url_override = "https://gw.ddnsto.com"
        self.assertEqual(h._public_url(self._data()),
                         "https://gw.ddnsto.com/?t=abc123")

    def test_empty_override_keeps_auto_behaviour(self):
        """不配 --public-url 时行为必须与改动前完全一致。"""
        h = self._h(tls_enabled=True, tls_port=8443)
        h.public_url_override = ""
        self.assertEqual(h._public_url(self._data()),
                         "https://print.example.com:8443/?t=abc123")

    def test_handler_default_override_is_empty(self):
        self.assertEqual(pg.Handler.public_url_override, "")

    def test_status_payload_carries_it(self):
        import inspect
        src = inspect.getsource(pg.Handler._admin_action)
        self.assertIn('status["public_url"]', src)

    def test_copy_helper_is_global_scope(self):
        """按钮是 innerHTML 重建的，onclick 只能找到全局函数。"""
        page = pg_admin.ADMIN_PAGE
        self.assertIn('onclick="copyUrl()"', page)
        # 顶层定义：出现在 render 之前
        self.assertLess(page.index("function copyUrl()"),
                        page.index("function render("))

    def test_page_shows_url_only_when_available(self):
        self.assertIn("st.public_url", pg_admin.ADMIN_PAGE)


class TestPreviewImageAuth(unittest.TestCase):
    """
    预览图 `/img` 的鉴权闭环。

    `/img` 在 `_authed()` **之后**，所以公网端口上「URL 里没带口令」= HTTP 401 =
    用户看到整片裂图（内网免密，所以只在公网暴露）。

    这个坑同时存在于两处 —— 服务端生成 URL 时没拼口令、前端 `<img src>` 没走
    `api()`。两边都钉住，否则修一处漏一处。
    """

    KEY = "81d450118e314005"

    def _job(self, jid="eb9d06cd2a76"):
        return mock.Mock(id=jid)                  # _preview_urls 只用到 id

    def _entry(self, n=2):
        return {"images": ["/tmp/j/build-x/preview-%d.png" % i
                           for i in range(1, n + 1)]}

    def _h(self, token="s3cret", on_tls=True, always=False, query=""):
        h = pg.Handler.__new__(pg.Handler)
        h.path = "/img" + query
        h.token = token
        h.token_always = always
        h.headers = {}
        h.server = mock.Mock(is_tls=on_tls)
        return h

    def test_every_url_carries_the_token(self):
        urls = self._h()._preview_urls(self._job(), self.KEY, self._entry())
        self.assertEqual(len(urls), 2)
        for u in urls:
            self.assertIn("&t=s3cret", u)

    def test_url_keeps_job_key_and_page(self):
        url = self._h()._preview_urls(self._job(), self.KEY, self._entry(1))[0]
        self.assertTrue(url.startswith("/img?job=eb9d06cd2a76&k=" + self.KEY + "&n=1"),
                        url)

    def test_generated_url_passes_its_own_auth(self):
        """核心闭环：服务端自己生成的 URL，必须能过服务端自己的鉴权。"""
        url = self._h()._preview_urls(self._job(), self.KEY, self._entry(1))[0]
        query = "?" + urllib.parse.urlparse(url).query
        self.assertTrue(self._h(query=query)._authed())

    def test_url_without_token_is_401_territory(self):
        """对照组：老版本的 URL 形态在公网端口上就是 401（整片裂图的成因）。"""
        self.assertFalse(
            self._h(query="?job=eb9d06cd2a76&k=%s&n=1" % self.KEY)._authed())

    def test_token_is_url_encoded(self):
        """口令里可能有 + & = 之类字符，不编码会把 query 拆坏。"""
        h = self._h(token="a b&c=d")
        url = h._preview_urls(self._job(), self.KEY, self._entry(1))[0]
        self.assertIn("&t=a%20b%26c%3Dd", url)
        query = "?" + urllib.parse.urlparse(url).query
        self.assertTrue(self._h(token="a b&c=d", query=query)._authed())

    def test_no_token_configured_adds_nothing(self):
        url = self._h(token="")._preview_urls(self._job(), self.KEY, self._entry(1))[0]
        self.assertNotIn("&t=", url)

    def test_lan_port_ignores_token_anyway(self):
        """内网明文端口免密：URL 里拼了口令也不影响，不拼也照样能取图。"""
        self.assertTrue(self._h(on_tls=False, query="?job=x")._authed())
        self.assertTrue(self._h(on_tls=False, query="?job=x&t=s3cret")._authed())

    def test_frontend_img_src_goes_through_api_helper(self):
        """
        前端那道保险：`<img src>` 必须走 api() 补口令。
        别再改回 `img.src = src` —— 那正是「内网好好的、外网预览全裂」的成因。
        """
        self.assertIn("img.src = api(src)", pg.PAGE)
        self.assertNotIn("img.src = src;", pg.PAGE)

    def test_img_route_is_still_behind_auth(self):
        """
        反面对照：不能为了省事把 /img 挪到鉴权之前。
        URL 里的 k 是**无盐**的 spec 哈希，参数组合空间很小，够不上凭证。
        """
        import inspect
        src = inspect.getsource(pg.Handler.do_GET)
        self.assertLess(src.index("self._authed()"), src.index('path == "/img"'))


class TestStickerRoutes(unittest.TestCase):
    """
    管理页的「二维码贴纸」：单码预览 SVG + 生成一个贴纸作业。

    这里把 `lan_ip` 与 `pdf_info` 都替掉了 —— 前者随开发机的网络环境变，
    后者依赖 poppler（开发机上通常没有，设备上才有）。**PDF 本身是真的
    生成的**，只是页数/尺寸这一跳交给 mock。完整的端到端（含 poppler
    读回、真出纸）由设备上的 `verify_sticker.py` 做。
    """

    SECRETS = {"record": {"root": "example.com", "sub": "print"}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sticker-route-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for patcher in (
            mock.patch.object(pg_sticker, "lan_ip",
                              return_value="192.168.1.100"),
            mock.patch.object(pg_admin, "load_secrets",
                              return_value=dict(self.SECRETS)),
            mock.patch.object(pg_engine, "pdf_info",
                              return_value=(1, [(595.276, 841.89)])),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _handler(self, path, body=None, tls=True, token="tok123",
                 apk="", host="192.168.1.100"):
        h = pg.Handler.__new__(pg.Handler)          # 不跑 __init__，避免真开端口
        h.path = path
        h.client_address = ("192.168.1.50", 43210)
        h.admin_token = "s3cret"
        h.tls_enabled = tls
        h.tls_port = 8443 if tls else 0
        h.http_port = 8080
        h.host_display = host
        h.token = token
        h.apk_path = apk
        h.store = pg.JobStore(self.tmp)
        h.headers = {}
        sent = {}
        h._send = lambda code, b, ctype, extra=None: sent.update(
            code=code, body=b, ctype=ctype, extra=extra or {})
        h._err = lambda msg, code=400: sent.update(code=code, obj={"error": msg})
        h._json = lambda obj, code=200: sent.update(code=code, obj=obj)
        h._json_body = lambda: (body or {})
        return h, sent

    # -------------------------------------------------------- 路由与页面
    def test_routes_registered(self):
        self.assertIn("/admin/qr.svg", pg.Handler._ADMIN_GETS)
        self.assertIn("/admin/api/sticker", pg.Handler._ADMIN_POSTS)

    def test_admin_page_has_sticker_controls(self):
        for marker in ('id="stickerBox"', 'id="stLayout"',
                       'id="bSticker"', 'id="stWarn"'):
            self.assertIn(marker, pg_admin.ADMIN_PAGE, marker)

    def test_page_previews_fetch_svg_from_server(self):
        """预览图必须走 /admin/qr.svg（服务端按 kind 现算），
        不能让前端把 URL 当参数传 —— 那就成了一个任意二维码接口。"""
        self.assertIn("/admin/qr.svg?kind=", pg_admin.ADMIN_PAGE)
        self.assertNotIn("qr.svg?url=", pg_admin.ADMIN_PAGE)

    def test_page_zooms_preview_for_scanning(self):
        """
        预览图要能点开放大 —— 否则屏幕上的码根本扫不动。

        公网码内容是 64 个字符 = QR 版本 5（37 模块），加静区 2 共 41 模块；
        管理页把它压在 CSS 132px 里，折算只有 3.2px/模块。实测（本机真解码）
        把这串内容按 96px 栅格化就已经解不出来。放大到 320px（≈7.8px/模块）
        才留出斜拍、隔远的余量。

        两条都要在：图放大；点击处 preventDefault —— 图片在 <label> 里，
        不挡默认行为的话「点一下放大」会顺手把这张码勾选/取消掉。
        """
        page = pg_admin.ADMIN_PAGE
        self.assertIn('id="qrZoom"', page)
        self.assertIn('id="qrZoomImg"', page)
        self.assertIn("min(78vw,320px)", page)
        self.assertIn("e.preventDefault()", page)

    # -------------------------------------------------------- 码的内容
    def test_lan_code_uses_lan_address_not_domain(self):
        h, _ = self._handler("/admin/qr.svg?kind=lan")
        codes, skipped = h._sticker_codes(["lan"])
        self.assertEqual(skipped, [])
        self.assertEqual(codes[0].url, "http://192.168.1.100:8080/")

    def test_wan_code_carries_the_token(self):
        """公网那条路必须带口令 —— 不带就是印一张扫开 401 的废纸。"""
        h, _ = self._handler("/admin/qr.svg?kind=wan")
        codes, _ = h._sticker_codes(["wan"])
        self.assertEqual(codes[0].url, "https://print.example.com:8443/?t=tok123")

    def test_app_code_points_at_download_page(self):
        apk = os.path.join(self.tmp, "x.apk")
        with open(apk, "wb") as fh:
            fh.write(b"apk")
        h, _ = self._handler("/admin/qr.svg?kind=app", apk=apk)
        codes, _ = h._sticker_codes(["app"])
        self.assertEqual(codes[0].url, "http://192.168.1.100:8080/app")

    # -------------------------------------------------------- 可用性
    def test_wan_unavailable_without_tls(self):
        h, sent = self._handler("/admin/qr.svg?kind=wan", tls=False)
        h._admin_action("/admin/qr.svg", False)
        self.assertEqual(sent["code"], 404)

    def test_wan_available_with_tls(self):
        h, sent = self._handler("/admin/qr.svg?kind=wan")
        h._admin_action("/admin/qr.svg", False)
        self.assertEqual(sent["code"], 200)

    def test_app_unavailable_without_apk(self):
        h, sent = self._handler("/admin/qr.svg?kind=app")
        h._admin_action("/admin/qr.svg", False)
        self.assertEqual(sent["code"], 404)
        self.assertIn("APK", h._sticker_avail()["app"][1])

    def test_unknown_kind_is_not_a_code(self):
        h, sent = self._handler("/admin/qr.svg?kind=../../etc/passwd")
        h._admin_action("/admin/qr.svg", False)
        self.assertEqual(sent["code"], 404)

    # -------------------------------------------------------- SVG 预览
    def test_svg_preview_is_served(self):
        h, sent = self._handler("/admin/qr.svg?kind=lan")
        h._admin_action("/admin/qr.svg", False)
        self.assertEqual(sent["code"], 200)
        self.assertIn("image/svg+xml", sent["ctype"])
        self.assertTrue(sent["body"].startswith(b"<?xml"))
        self.assertIn(b"</svg>", sent["body"])

    def test_svg_preview_not_cached(self):
        h, sent = self._handler("/admin/qr.svg?kind=lan")
        h._admin_action("/admin/qr.svg", False)
        self.assertIn("no-store", sent["extra"].get("Cache-Control", ""))

    # -------------------------------------------------------- 生成贴纸
    def _fake_apk(self):
        apk = os.path.join(self.tmp, "fake.apk")
        with open(apk, "wb") as fh:
            fh.write(b"apk")
        return apk

    def test_sticker_creates_a_real_pdf_job(self):
        h, sent = self._handler("/admin/api/sticker",
                                body={"kinds": ["lan", "wan", "app"],
                                      "layout": 1}, apk=self._fake_apk())
        h._admin_action("/admin/api/sticker", True)
        self.assertEqual(sent["code"], 200)

        job = h.store.get(sent["obj"]["id"])
        self.assertIsNotNone(job, "作业没注册进 store，跳转过去会 404")
        self.assertTrue(os.path.exists(job.source_pdf))
        with open(job.source_pdf, "rb") as fh:
            self.assertTrue(fh.read(8).startswith(b"%PDF"))
        self.assertEqual(job.files, 1)
        self.assertEqual(sent["obj"]["codes"], ["lan", "wan", "app"])
        self.assertEqual(sent["obj"]["skipped"], [])

    def test_sticker_records_layout_in_filename(self):
        for layout, want in ((1, "整页 1 张"), (2, "A5 两张"), (4, "A6 四张")):
            h, sent = self._handler("/admin/api/sticker",
                                    body={"kinds": ["lan"], "layout": layout})
            h._admin_action("/admin/api/sticker", True)
            self.assertEqual(sent["code"], 200)
            self.assertIn(want, h.store.get(sent["obj"]["id"]).filename)

    def test_sticker_reports_skipped_kinds(self):
        """选了但没有的码要如实报回来 —— 前端才好提示「App 码没印」。"""
        h, sent = self._handler("/admin/api/sticker",
                                body={"kinds": ["lan", "wan", "app"],
                                      "layout": 2}, apk="")
        h._admin_action("/admin/api/sticker", True)
        self.assertEqual(sent["code"], 200)
        self.assertEqual(sent["obj"]["codes"], ["lan", "wan"])
        self.assertEqual(sent["obj"]["skipped"], ["app"])

    def test_sticker_rejects_bad_layout(self):
        for bad in (0, 3, 8, "x"):
            h, sent = self._handler("/admin/api/sticker",
                                    body={"kinds": ["lan"], "layout": bad})
            h._admin_action("/admin/api/sticker", True)
            self.assertEqual(sent["code"], 400, bad)

    def test_sticker_needs_at_least_one_code(self):
        """探测不到内网地址、又没启 HTTPS、也没放 APK —— 三种码全废，
        这时候必须明说，而不是印一张三个码都扫不开的纸。"""
        with mock.patch.object(pg_sticker, "lan_ip", return_value=""):
            h, sent = self._handler("/admin/api/sticker",
                                    body={"kinds": [], "layout": 1},
                                    tls=False, host="")
            h._admin_action("/admin/api/sticker", True)
        self.assertEqual(sent["code"], 400)
        self.assertIn("没有可用的二维码", sent["obj"]["error"])

    def test_failed_generation_leaves_no_job(self):
        """生成失败必须把作业目录清掉 —— 不然每次失败都在 /tmp 下留一份垃圾。"""
        h, sent = self._handler("/admin/api/sticker",
                                body={"kinds": ["lan"], "layout": 1})
        before = set(os.listdir(self.tmp))
        with mock.patch.object(pg_sticker, "build_pdf",
                               side_effect=pg_sticker.StickerError("boom")):
            h._admin_action("/admin/api/sticker", True)
        self.assertEqual(sent["code"], 400)
        self.assertIn("boom", sent["obj"]["error"])
        self.assertEqual(set(os.listdir(self.tmp)), before)

    # -------------------------------------------------------- status
    def test_status_exposes_sticker_availability(self):
        h, sent = self._handler("/admin/api/status")
        h._admin_action("/admin/api/status", False)
        self.assertEqual(sent["code"], 200)
        info = sent["obj"].get("sticker")
        self.assertIsInstance(info, dict)
        for kind in ("lan", "wan", "app"):
            self.assertIn(kind, info)
            self.assertIn("available", info[kind])
        self.assertTrue(info["lan"]["available"])
        self.assertTrue(info["wan"]["available"])

    def test_status_reports_why_a_code_is_unavailable(self):
        h, sent = self._handler("/admin/api/status", tls=False)
        h._admin_action("/admin/api/status", False)
        wan = sent["obj"]["sticker"]["wan"]
        self.assertFalse(wan["available"])
        self.assertTrue(wan["reason"], "不可用却不给原因，界面上就只剩一句干瞪眼")


if __name__ == "__main__":
    unittest.main(verbosity=2)
