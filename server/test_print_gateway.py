#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
print_gateway 核心逻辑测试。
不依赖设备，纯本地跑。重点验证手写 multipart 解析器与配置决策逻辑。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import re                                                     # noqa: E402
import tempfile                                               # noqa: E402

import print_gateway as pg                                    # noqa: E402
import pg_decor                                               # noqa: E402
import pg_engine                                              # noqa: E402
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
