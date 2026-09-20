#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
命令行贴纸工具的单元测试。

这里最值钱的一条是 `TestAgreesWithGateway`：把 `make_sticker.wan_url()`
与网关真正在用的 `Handler._public_url()` **拉到一起对拍**。

为什么值得单独钉住：这两处是同一个部署的「公网码」的两个产出面 ——
管理页印一张、命令行印一张。它们一旦分头演化（比如一边去尾斜杠、一边不去，
或者一边拼 `?t=` 一边拼 `&t=`），症状是「两张贴纸扫出来不一样」，
而这种问题只有人真的拿手机去扫才会暴露。对拍能把它变成一条红灯的测试。
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import make_sticker                                           # noqa: E402
import pg_sticker                                             # noqa: E402
import print_gateway as pg                                    # noqa: E402

try:
    from PIL import Image                                     # noqa: F401
    HAS_PIL = True
except ImportError:                                           # pragma: no cover
    HAS_PIL = False


class TestWanUrl(unittest.TestCase):
    """公网链接的拼法。"""

    def test_embeds_token_as_query(self):
        self.assertEqual(
            make_sticker.wan_url("https://a.example.com", "s3cret"),
            "https://a.example.com/?t=s3cret")

    def test_strips_trailing_slash(self):
        self.assertEqual(
            make_sticker.wan_url("https://a.example.com/", "tok"),
            "https://a.example.com/?t=tok")

    def test_no_token_gives_bare_url(self):
        self.assertEqual(
            make_sticker.wan_url("https://a.example.com", ""),
            "https://a.example.com/")

    def test_surrounding_spaces_ignored(self):
        self.assertEqual(
            make_sticker.wan_url("  https://a.example.com  ", "tok"),
            "https://a.example.com/?t=tok")


class TestAgreesWithGateway(unittest.TestCase):
    """与网关 `_public_url()` 对拍 —— 贴纸与管理页必须印出同一个链接。"""

    @staticmethod
    def _handler(public_url, token):
        h = pg.Handler.__new__(pg.Handler)
        h.public_url_override = public_url
        h.token = token
        return h

    def _gateway_public_url(self, public_url, token):
        return self._handler(public_url, token)._public_url({})

    def test_same_string_for_typical_case(self):
        for pub, tok in (("https://gw.example.com", "0123456789abcdef"),
                         ("https://a.example.com/", "tok"),
                         ("https://a.example.com", ""),
                         ("  https://a.example.com  ", "tok")):
            with self.subTest(public_url=pub, token=tok):
                self.assertEqual(make_sticker.wan_url(pub, tok),
                                 self._gateway_public_url(pub, tok))

    def test_agrees_on_a_16_hex_token(self):
        """隧道域名 + 16 位十六进制口令这一组形态也钉一下。

        注意：这里用的是**假值**。真实域名与口令不进仓库 —— 本仓库是公开的，
        贴纸里印的就是这两项的原文，写进来等于把公网入口公开。
        """
        pub = "https://gw.example.com"
        tok = "0123456789abcdef"
        self.assertEqual(self._gateway_public_url(pub, tok),
                         "https://gw.example.com/?t=0123456789abcdef")
        self.assertEqual(make_sticker.wan_url(pub, tok),
                         "https://gw.example.com/?t=0123456789abcdef")


class TestNetlocDisplay(unittest.TestCase):
    """贴纸小字行里的主机名。"""

    def test_https_default_port_dropped(self):
        self.assertEqual(
            make_sticker._netloc_display("https://a.example.com:443/?t=x"),
            "a.example.com")

    def test_http_default_port_dropped(self):
        self.assertEqual(
            make_sticker._netloc_display("http://192.168.1.110:80/"),
            "192.168.1.110")

    def test_non_default_port_kept(self):
        self.assertEqual(
            make_sticker._netloc_display("http://192.168.1.110:8080/"),
            "192.168.1.110:8080")

    def test_https_non_default_port_kept(self):
        self.assertEqual(
            make_sticker._netloc_display("https://a.example.com:8443/"),
            "a.example.com:8443")

    def test_bare_host_without_scheme(self):
        self.assertEqual(make_sticker._netloc_display("a.example.com"), "")


class TestBuildCodes(unittest.TestCase):
    """能出哪些码、不出哪些码，以及为什么。"""

    def test_all_three_when_everything_given(self):
        codes, reasons = make_sticker.build_codes(
            host="192.168.1.110", port=8080,
            public_url="https://a.example.com", token="tok")
        self.assertEqual([c.kind for c in codes], ["lan", "wan", "app"])
        self.assertEqual(reasons, {})
        self.assertEqual(codes[1].url, "https://a.example.com/?t=tok")
        self.assertEqual(codes[0].url, "http://192.168.1.110:8080/")
        self.assertEqual(codes[2].url, "http://192.168.1.110:8080/app")

    def test_wan_skipped_without_public_url(self):
        """没配公网地址就没公网码 —— 这是正常状态，不该让整页印不出来。"""
        codes, reasons = make_sticker.build_codes(host="192.168.1.110")
        self.assertEqual([c.kind for c in codes], ["lan", "app"])
        self.assertIn("wan", reasons)
        self.assertIn("--public-url", reasons["wan"])

    def test_explicit_wan_wins_over_public_url(self):
        codes, _ = make_sticker.build_codes(
            host="192.168.1.110", public_url="https://ignored.example.com",
            token="tok", wan="https://direct.example.com/?t=given")
        wan = [c for c in codes if c.kind == "wan"][0]
        self.assertEqual(wan.url, "https://direct.example.com/?t=given")
        self.assertEqual(wan.sub, "direct.example.com")

    def test_kinds_subset(self):
        codes, _ = make_sticker.build_codes(
            host="192.168.1.110", public_url="https://a.example.com",
            token="tok", kinds=("wan",))
        self.assertEqual([c.kind for c in codes], ["wan"])

    def test_wan_sub_is_hostname_without_scheme(self):
        codes, _ = make_sticker.build_codes(
            host="192.168.1.110", public_url="https://a.example.com:443",
            token="tok")
        wan = [c for c in codes if c.kind == "wan"][0]
        self.assertEqual(wan.sub, "a.example.com")

    def test_lan_and_app_skipped_without_host(self):
        codes, reasons = make_sticker.build_codes(
            host="", public_url="https://a.example.com", token="tok")
        self.assertEqual([c.kind for c in codes], ["wan"])
        self.assertIn("lan", reasons)
        self.assertIn("app", reasons)

    def test_lan_port_80_has_no_suffix(self):
        codes, _ = make_sticker.build_codes(host="192.168.1.110", port=80,
                                            kinds=("lan",))
        self.assertEqual(codes[0].sub, "192.168.1.110")
        self.assertEqual(codes[0].url, "http://192.168.1.110/")

    def test_unknown_kind_rejected(self):
        with self.assertRaises(make_sticker.StickerCliError):
            make_sticker.build_codes(host="h", kinds=("lan", "nope"))

    def test_empty_kinds_rejected(self):
        with self.assertRaises(make_sticker.StickerCliError):
            make_sticker.build_codes(host="h", kinds=())

    def test_nothing_usable_rejected(self):
        with self.assertRaises(make_sticker.StickerCliError):
            make_sticker.build_codes(host="", kinds=("lan",))

    def test_plaintext_wan_with_token_refused(self):
        """公网码里嵌着口令却走明文 —— 口令会交给沿途每个节点。"""
        with self.assertRaises(make_sticker.StickerCliError) as cm:
            make_sticker.build_codes(
                host="192.168.1.110", public_url="http://plain.example.com",
                token="tok")
        self.assertIn("allow-insecure", str(cm.exception))

    def test_plaintext_wan_allowed_when_explicitly_opted_in(self):
        codes, _ = make_sticker.build_codes(
            host="192.168.1.110", public_url="http://plain.example.com",
            token="tok", allow_insecure=True)
        self.assertEqual([c.kind for c in codes], ["lan", "wan", "app"])

    def test_plaintext_wan_without_token_is_fine(self):
        """内网那份本来就免密，明文不是问题 —— 别把正常用法一起拦了。"""
        codes, _ = make_sticker.build_codes(
            host="192.168.1.110", public_url="http://plain.example.com",
            token="")
        self.assertIn("wan", [c.kind for c in codes])


class TestMain(unittest.TestCase):
    """命令行入口的行为（退出码 + 有没有落盘）。"""

    def test_public_url_without_token_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "s.pdf")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = make_sticker.main(["--host", "192.168.1.110",
                                        "--public-url", "https://a.example.com",
                                        "--out", out])
            self.assertEqual(rc, 2)
            self.assertFalse(os.path.exists(out))

    def test_dry_run_prints_but_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "s.pdf")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = make_sticker.main([
                    "--host", "192.168.1.110",
                    "--public-url", "https://a.example.com", "--token", "tok",
                    "--out", out, "--dry-run"])
            self.assertEqual(rc, 0)
            text = buf.getvalue()
            self.assertIn("https://a.example.com/?t=tok", text)
            self.assertIn("http://192.168.1.110:8080/", text)
            self.assertFalse(os.path.exists(out))

    def test_dry_run_reports_skipped_wan(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = make_sticker.main(["--host", "192.168.1.110", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("跳过 wan", buf.getvalue())

    @unittest.skipUnless(HAS_PIL, "没有 PIL，跳过渲染")
    def test_writes_pdf_and_png(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "s.pdf")
            png = os.path.join(d, "s.png")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = make_sticker.main([
                    "--host", "192.168.1.110",
                    "--public-url", "https://a.example.com", "--token", "tok",
                    "--layout", "4", "--out", out, "--png", png])
            self.assertEqual(rc, 0)
            self.assertGreater(os.path.getsize(out), 1000)
            self.assertTrue(os.path.exists(png))

    @unittest.skipUnless(HAS_PIL, "没有 PIL，跳过渲染")
    def test_pdf_starts_with_pdf_magic(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "s.pdf")
            with redirect_stdout(io.StringIO()):
                make_sticker.main(["--host", "192.168.1.110", "--out", out])
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(5), b"%PDF-")


class TestConstants(unittest.TestCase):
    """默认值别漂 —— 贴纸尺寸和码类型是给用户看的契约。"""

    def test_default_kinds_are_the_three_shipped_codes(self):
        self.assertEqual(tuple(pg_sticker.CODE_KINDS), ("lan", "wan", "app"))

    def test_layouts_supported(self):
        self.assertEqual(tuple(pg_sticker.LAYOUTS), (1, 2, 4))


if __name__ == "__main__":
    unittest.main(verbosity=2)
