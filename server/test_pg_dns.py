#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pg_dns 测试：公网 IP 判定、多源兜底、DNSPod 两套 API 的读写语义。

全部走 mock，不发真实请求 —— 这里要验证的是**逻辑**（什么时候写、写什么、
失败怎么报），真实 API 的联调在设备上用 `/admin` 的「检测凭据」按。

最要紧的两条：
  * `set_ip` 在值相同的时候**必须不发写请求**（省配额、也避免无谓的记录变更）
  * `verify_credentials` **绝不能有写操作** —— 它挂在「检测凭据」按钮上，
    用户点一下不该改 DNS
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hashlib                                                    # noqa: E402
import hmac                                                       # noqa: E402

import pg_dns                                                     # noqa: E402


class _Resp:
    """够用的假响应：urlopen 的上下文管理器 + read(n)。"""

    def __init__(self, data: bytes):
        self._data = data

    def read(self, n=None):
        return self._data[:n] if n else self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestPublicIpv4(unittest.TestCase):
    def test_accepts_real_public_addresses(self):
        for ip in ("1.2.3.4", "8.8.8.8", "1.1.1.1", "223.5.5.5"):
            self.assertTrue(pg_dns.is_public_ipv4(ip), ip)

    def test_rejects_private_and_special(self):
        for ip in ("192.168.1.100", "10.0.0.1", "172.16.5.5",
                   "127.0.0.1", "169.254.1.1", "0.0.0.0",
                   "224.0.0.1", "255.255.255.255"):
            self.assertFalse(pg_dns.is_public_ipv4(ip), ip)

    def test_rejects_carrier_grade_nat(self):
        """100.64/10 是最容易误判成公网的一类 —— 拿到它说明根本没公网 IP。"""
        for ip in ("100.64.0.1", "100.100.100.100", "100.127.255.254"):
            self.assertFalse(pg_dns.is_public_ipv4(ip), ip)

    def test_rejects_non_ipv4(self):
        for text in ("", "abc", "2001:db8:1:2::1", "1.2.3", "999.1.1.1"):
            self.assertFalse(pg_dns.is_public_ipv4(text), text)


class TestDetectWanIp(unittest.TestCase):
    def _with(self, results):
        """results: [(异常或bytes), ...] 按顺序对应 WAN_SOURCES。"""
        def fake(urlopen, *a, **kw):                          # noqa: ARG001
            item = results.pop(0)
            if isinstance(item, Exception):
                raise item
            return _Resp(item)
        return mock.patch.object(pg_dns.urllib.request, "urlopen", fake)

    def test_first_source_wins(self):
        """第一个源就拿到公网 IP 时，不该再去问后面的源。"""
        calls = []

        def fake(req, **kw):
            calls.append(req.full_url)
            return _Resp(b"\xef\xbb\xbf" + "当前 IP：1.2.3.4".encode("utf-8"))

        with mock.patch.object(pg_dns.urllib.request, "urlopen", fake):
            self.assertEqual(pg_dns.detect_wan_ip(), "1.2.3.4")
        self.assertEqual(len(calls), 1)

    def test_falls_back_on_failure(self):
        with self._with([OSError("boom"), b"1.2.3.4\n"]):
            self.assertEqual(pg_dns.detect_wan_ip(), "1.2.3.4")

    def test_skips_source_returning_private_ip(self):
        """源被劫持/被墙到内网地址时，不能把私网 IP 当成公网 IP 写进 DNS。"""
        with self._with([b"192.168.1.1", b"1.2.3.4"]):
            self.assertEqual(pg_dns.detect_wan_ip(), "1.2.3.4")

    def test_all_sources_failing_raises_with_reasons(self):
        with self._with([OSError("a"), OSError("b"), OSError("c"), OSError("d")]):
            with self.assertRaises(pg_dns.DnsError) as ctx:
                pg_dns.detect_wan_ip()
        self.assertIn("无法获取公网 IPv4", str(ctx.exception))


class TestDnsPodToken(unittest.TestCase):
    """旧版 DNSPod：表单 POST + login_token。"""

    def _client(self):
        return pg_dns.DnsPodToken("12345", "abcdef", ttl=600)

    def _records_response(self, value=None, record_id="999"):
        recs = []
        if value is not None:
            recs = [{"id": record_id, "name": "print", "type": "A", "value": value}]
        return {"status": {"code": "1", "message": "Action completed successful"},
                "domain": {"id": 555}, "records": recs}

    def test_get_ip_returns_none_when_record_absent(self):
        with mock.patch.object(pg_dns, "_post",
                               return_value=self._records_response()) as post:
            self.assertIsNone(self._client().get_ip("example.com", "print"))
        payload = post.call_args[0][1]
        self.assertIn(b"Record.List", post.call_args[0][0].encode())
        self.assertIn(b"format=json", payload)
        self.assertIn(b"login_token=12345%2Cabcdef", payload)

    def test_get_ip_reads_a_record(self):
        with mock.patch.object(pg_dns, "_post",
                               return_value=self._records_response("1.2.3.4")):
            self.assertEqual(self._client().get_ip("example.com", "print"),
                             "1.2.3.4")

    def test_get_ip_ignores_non_a_records(self):
        """子域名下可能同时有 CNAME / TXT（acme 会写 TXT），别抓错。"""
        resp = {"status": {"code": "1", "message": "ok"},
                "records": [{"id": "1", "name": "print", "type": "CNAME",
                             "value": "example.com"}]}
        with mock.patch.object(pg_dns, "_post", return_value=resp):
            self.assertIsNone(self._client().get_ip("example.com", "print"))

    def test_set_ip_creates_when_missing(self):
        with mock.patch.object(pg_dns, "_post",
                               return_value=self._records_response()) as post:
            action = self._client().set_ip("example.com", "print", "1.2.3.4")
        self.assertEqual(action, "created")
        payload = post.call_args[0][1].decode("utf-8")
        self.assertIn("Record.Create", post.call_args[0][0])
        self.assertIn("value=1.2.3.4", payload)
        self.assertIn("ttl=600", payload)
        # 线路必须是 URL 编码后的「默认」，否则 API 报错
        self.assertIn("%E9%BB%98%E8%AE%A4", payload)

    def test_set_ip_modifies_when_different(self):
        with mock.patch.object(pg_dns, "_post",
                               return_value=self._records_response("1.1.1.1")) as post:
            action = self._client().set_ip("example.com", "print", "2.2.2.2")
        self.assertEqual(action, "modified")
        self.assertIn("Record.Modify", post.call_args[0][0])
        self.assertIn("record_id=999", post.call_args[0][1].decode("utf-8"))

    def test_set_ip_skips_write_when_unchanged(self):
        """值没变就不发写请求 —— 这条是省配额的关键。"""
        with mock.patch.object(pg_dns, "_post",
                               return_value=self._records_response("1.1.1.1")) as post:
            action = self._client().set_ip("example.com", "print", "1.1.1.1")
        self.assertEqual(action, "unchanged")
        self.assertEqual(post.call_count, 1)             # 只有那次 Record.List
        self.assertIn("Record.List", post.call_args[0][0])

    def test_api_error_raises_dns_error(self):
        resp = {"status": {"code": "3", "message": "令牌未生效"}}
        with mock.patch.object(pg_dns, "_post", return_value=resp):
            with self.assertRaises(pg_dns.DnsError) as ctx:
                self._client().get_ip("example.com", "print")
        self.assertIn("令牌未生效", str(ctx.exception))

    def test_empty_credentials_rejected_early(self):
        for sid, key in (("", "x"), ("x", ""), ("", "")):
            with self.assertRaises(pg_dns.DnsError):
                pg_dns.DnsPodToken(sid, key)

    def test_credentials_are_whitespace_trimmed(self):
        """从网页粘贴凭据最容易带上首尾空格或换行。"""
        client = pg_dns.DnsPodToken("  12345\n", " abcdef\t")
        self.assertEqual(client.id, "12345")
        self.assertEqual(client.key, "abcdef")


class TestTencentDns(unittest.TestCase):
    """腾讯云：TC3-HMAC-SHA256 签名手写实现。"""

    def _client(self):
        return pg_dns.TencentDns("AKIDexample", "SecretKeyExample", ttl=600)

    def _capture(self, response):
        holder = {}

        def fake(url, data, headers, timeout):                # noqa: ARG001
            holder["url"] = url
            holder["body"] = data
            holder["headers"] = headers
            return response
        return holder, mock.patch.object(pg_dns, "_post", fake)

    def test_signature_header_shape(self):
        holder, patched = self._capture({"Response": {"RecordList": []}})
        with patched:
            self._client().get_ip("example.com", "print")
        auth = holder["headers"]["Authorization"]
        self.assertTrue(auth.startswith("TC3-HMAC-SHA256 Credential=AKIDexample/"))
        self.assertIn("SignedHeaders=content-type;host;x-tc-action", auth)
        self.assertIn("Signature=", auth)
        self.assertEqual(holder["url"], "https://dnspod.tencentcloudapi.com")
        self.assertEqual(holder["headers"]["X-TC-Action"], "DescribeRecordList")
        self.assertEqual(holder["headers"]["X-TC-Version"], "2021-03-23")

    def test_secret_key_never_leaves_in_headers(self):
        """密钥只能出现在签名里（HMAC 摘要），绝不能作为明文头发出去。"""
        holder, patched = self._capture({"Response": {"RecordList": []}})
        with patched:
            self._client().get_ip("example.com", "print")
        blob = repr(holder["headers"])
        self.assertNotIn("SecretKeyExample", blob)

    def test_signature_is_deterministic_for_fixed_time(self):
        """同一时刻、同一请求 → 同一签名。签名不稳说明规范化步骤有随机因素。"""
        sigs = []
        for _ in range(2):
            holder, patched = self._capture({"Response": {"RecordList": []}})
            with mock.patch.object(pg_dns.time, "time", return_value=1789000000):
                with patched:
                    self._client().get_ip("example.com", "print")
            sigs.append(holder["headers"]["Authorization"].split("Signature=")[1])
        self.assertEqual(sigs[0], sigs[1])
        self.assertEqual(len(sigs[0]), 64)
        int(sigs[0], 16)                                  # 必须是合法十六进制

    def test_signature_matches_independent_recompute(self):
        """
        独立复算一遍 TC3 签名（不调用被测代码的私有方法），确认协议实现正确。
        时间戳钉死，否则结果不可复现。
        """
        ts = 1789000000
        secret_id, secret_key = "AKIDexample", "SecretKeyExample"
        payload = '{"Domain":"example.com","RecordType":"A","Subdomain":"print"}'
        date = time.strftime("%Y-%m-%d", time.gmtime(ts))
        hashed = hashlib.sha256(payload.encode()).hexdigest()
        canonical = "\n".join([
            "POST", "/", "",
            "content-type:application/json; charset=utf-8\n"
            "host:dnspod.tencentcloudapi.com\nx-tc-action:describerecordlist\n",
            "content-type;host;x-tc-action", hashed,
        ])
        scope = "%s/dnspod/tc3_request" % date
        sts = "\n".join(["TC3-HMAC-SHA256", str(ts), scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])

        def hm(key, msg):
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        expected = hmac.new(hm(hm(hm(("TC3" + secret_key).encode(), date),
                                  "dnspod"), "tc3_request"),
                            sts.encode(), hashlib.sha256).hexdigest()

        holder = {}

        def fake(url, data, headers, timeout):                # noqa: ARG001
            holder["headers"] = headers
            return {"Response": {"RecordList": []}}

        with mock.patch.object(pg_dns.time, "time", return_value=ts):
            with mock.patch.object(pg_dns, "_post", fake):
                self._client()._call("DescribeRecordList", {
                    "Domain": "example.com", "RecordType": "A", "Subdomain": "print"})
        self.assertIn(expected, holder["headers"]["Authorization"])

    def test_api_error_object_raises(self):
        holder, patched = self._capture({"Response": {"Error": {
            "Code": "AuthFailure.SignatureFailure", "Message": "签名错误"}}})
        with patched:
            with self.assertRaises(pg_dns.DnsError) as ctx:
                self._client().get_ip("example.com", "print")
        self.assertIn("AuthFailure.SignatureFailure", str(ctx.exception))

    def test_set_ip_creates_and_modifies(self):
        holder, patched = self._capture({"Response": {"RecordList": []}})
        with patched:
            self.assertEqual(self._client().set_ip("example.com", "print", "1.2.3.4"),
                             "created")
        self.assertEqual(holder["headers"]["X-TC-Action"], "CreateRecord")
        self.assertIn('"TTL":600', holder["body"].decode())

        resp = {"Response": {"RecordList": [
            {"RecordId": 42, "Name": "print", "Type": "A", "Value": "9.9.9.9"}]}}
        holder, patched = self._capture(resp)
        with patched:
            self.assertEqual(self._client().set_ip("example.com", "print", "1.2.3.4"),
                             "modified")
        self.assertEqual(holder["headers"]["X-TC-Action"], "ModifyRecord")
        self.assertIn('"RecordId":42', holder["body"].decode())

    def test_set_ip_unchanged_skips_create(self):
        resp = {"Response": {"RecordList": [
            {"RecordId": 42, "Name": "print", "Type": "A", "Value": "1.2.3.4"}]}}
        holder, patched = self._capture(resp)
        with patched:
            self.assertEqual(self._client().set_ip("example.com", "print", "1.2.3.4"),
                             "unchanged")
        self.assertEqual(holder["headers"]["X-TC-Action"], "DescribeRecordList")


class TestFactory(unittest.TestCase):
    def test_provider_selects_class(self):
        self.assertIsInstance(
            pg_dns.make_dns({"provider": "dnspod",
                             "dnspod": {"id": "a", "key": "b"}}),
            pg_dns.DnsPodToken)
        self.assertIsInstance(
            pg_dns.make_dns({"provider": "tencent",
                             "tencent": {"secret_id": "a", "secret_key": "b"}}),
            pg_dns.TencentDns)

    def test_defaults_to_dnspod(self):
        client = pg_dns.make_dns({"dnspod": {"id": "a", "key": "b"}})
        self.assertIsInstance(client, pg_dns.DnsPodToken)

    def test_unknown_provider_rejected(self):
        with self.assertRaises(pg_dns.DnsError):
            pg_dns.make_dns({"provider": "cloudflare"})

    def test_ttl_flows_through(self):
        client = pg_dns.make_dns({"provider": "dnspod", "ttl": 3600,
                                  "dnspod": {"id": "a", "key": "b"}})
        self.assertEqual(client.ttl, 3600)

    def test_unconfigured_credentials_rejected(self):
        with self.assertRaises(pg_dns.DnsError):
            pg_dns.make_dns({"provider": "dnspod", "dnspod": {"id": "", "key": ""}})


class TestVerifyCredentials(unittest.TestCase):
    """
    「检测凭据」是**只读**动作。用户点它只是想确认令牌能用，
    要是顺手把记录改了，就是典型的「按钮行为超出预期」。
    """

    def _secrets(self):
        return {"provider": "dnspod", "record": {"root": "example.com", "sub": "print"},
                "dnspod": {"id": "1", "key": "k"}}

    def test_never_writes(self):
        with mock.patch.object(pg_dns.DnsPodToken, "get_ip",
                               return_value="1.2.3.4") as getter:
            with mock.patch.object(pg_dns.DnsPodToken, "set_ip") as setter:
                result = pg_dns.verify_credentials(self._secrets(), "example.com", "print")
        self.assertTrue(result["ok"])
        getter.assert_called_once()
        setter.assert_not_called()

    def test_reports_existing_record(self):
        with mock.patch.object(pg_dns.DnsPodToken, "get_ip", return_value="1.2.3.4"):
            result = pg_dns.verify_credentials(self._secrets(), "example.com", "print")
        self.assertEqual(result["current"], "1.2.3.4")
        self.assertIn("1.2.3.4", result["message"])

    def test_explains_missing_record_is_not_an_error(self):
        """还没建记录 ≠ 凭据有问题。首次部署时就是这样，不该报错。"""
        with mock.patch.object(pg_dns.DnsPodToken, "get_ip", return_value=None):
            result = pg_dns.verify_credentials(self._secrets(), "example.com", "print")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["current"])
        self.assertIn("还没有 A 记录", result["message"])

    def test_bad_credentials_reported_not_raised(self):
        with mock.patch.object(pg_dns.DnsPodToken, "get_ip",
                               side_effect=pg_dns.DnsError("令牌未生效")):
            result = pg_dns.verify_credentials(self._secrets(), "example.com", "print")
        self.assertFalse(result["ok"])
        self.assertIn("令牌未生效", result["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
