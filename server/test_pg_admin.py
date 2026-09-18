#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pg_admin 测试：凭据存储与掩码、admin 内网判据、证书解析、DDNS 同步语义。

最要紧的三条（都对应真实会出事的地方）：

  * **掩码不会被当成新值存回去** —— 页面把敏感项渲染成掩码，用户直接点保存
    会把 `****abcd` 发回来；一旦写进配置，凭据就毁了，而且下次用才发现。
  * **`is_lan_addr` 必须 fail-closed** —— 它是「公网能不能看到管理页」的唯一判据，
    解析不出来时宁可判为非内网。
  * **证书剩余天数按 UTC 算** —— 差 8 小时就能让「还剩 31 天」变成「还剩 30 天」，
    而 30 天正是续期阈值。
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_admin                                                   # noqa: E402
import pg_dns                                                     # noqa: E402


class SecretsCase(unittest.TestCase):
    """把凭据文件重定向到临时目录，别碰真实的 /etc/print-gateway。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pgadmin-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "secrets.json")
        for name, value in (("SECRETS_DIR", self.tmp.name),
                            ("SECRETS_PATH", self.path),
                            ("STATE_DIR", os.path.join(self.tmp.name, "state")),
                            ("DDNS_STATE", os.path.join(self.tmp.name, "state", "ddns.json"))):
            patcher = mock.patch.object(pg_admin, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class TestMask(unittest.TestCase):
    def test_keeps_only_tail(self):
        self.assertTrue(pg_admin.mask("1234567890abcdef").endswith("cdef"))
        self.assertNotIn("1234567890ab", pg_admin.mask("1234567890abcdef"))

    def test_short_value_fully_masked(self):
        """短值如果只留末 4 位就等于全泄露，必须整串打星。"""
        for value in ("abc", "abcd", "a"):
            self.assertEqual(set(pg_admin.mask(value)), {"*"}, value)

    def test_empty_stays_empty(self):
        self.assertEqual(pg_admin.mask(""), "")

    def test_view_never_contains_plaintext(self):
        view = pg_admin.secrets_view({
            "provider": "dnspod",
            "dnspod": {"id": "123456", "key": "supersecretkey9999"},
            "tencent": {"secret_id": "AKIDabcdefghij", "secret_key": "verysecret"},
        })
        blob = json.dumps(view, ensure_ascii=False)
        for secret in ("supersecretkey9999", "AKIDabcdefghij", "verysecret",
                       "123456"):
            self.assertNotIn(secret, blob)
        self.assertTrue(view["dnspod"]["has_key"])
        self.assertTrue(view["tencent"]["has_id"])


class TestApplyChanges(SecretsCase):
    def test_masked_value_is_not_written_back(self):
        """
        核心防线：页面把敏感项渲染成掩码（`********alue`），用户不改直接保存，
        掩码串就会被发回来。**一旦写进配置，凭据就毁了**，而且要到下次用才发现。
        """
        data = pg_admin.default_secrets()
        data["dnspod"] = {"id": "12345", "key": "real-key-value"}
        pg_admin.apply_changes(data, {"dnspod": {"id": "", "key": "********alue"}})
        self.assertEqual(data["dnspod"]["id"], "12345")
        self.assertEqual(data["dnspod"]["key"], "real-key-value")

    def test_every_mask_shape_is_rejected(self):
        """掩码算法换参数（保留位数变化）也不能让掩码漏进配置。"""
        data = pg_admin.default_secrets()
        data["dnspod"] = {"id": "abcdefgh", "key": "abcdefghijkl"}
        for fake in ("****", "****hijkl", "************ijkl",
                     "********ijkl", "*" * 12 + "ijkl"):
            pg_admin.apply_changes(data, {"dnspod": {"key": fake}})
            self.assertEqual(data["dnspod"]["key"], "abcdefghijkl",
                             "掩码 %r 被当成新值写入了" % fake)

    def test_round_trip_of_masked_view_changes_nothing(self):
        """
        模拟真实路径：页面拿到掩码视图 → 用户直接点保存 → 原样发回。
        整套 `secrets_view` → `apply_changes` 走一圈，配置必须**一个字节都不变**。
        """
        original = pg_admin.default_secrets()
        original["provider"] = "tencent"
        original["record"] = {"root": "example.com", "sub": "print"}
        original["dnspod"] = {"id": "12345", "key": "dnspod-key-plain"}
        original["tencent"] = {"secret_id": "AKIDplaintext", "secret_key": "SKplaintext"}

        view = pg_admin.secrets_view(original)
        # 页面就是这么回传的：域名照原样、敏感项是掩码
        payload = {
            "provider": view["provider"],
            "ttl": view["ttl"],
            "record": view["record"],
            "dnspod": {"id": view["dnspod"]["id"], "key": view["dnspod"]["key"]},
            "tencent": {"secret_id": view["tencent"]["secret_id"],
                        "secret_key": view["tencent"]["secret_key"]},
        }
        data = pg_admin.load_secrets()
        data.update(original)
        pg_admin.apply_changes(data, payload)

        self.assertEqual(data["dnspod"]["key"], "dnspod-key-plain")
        self.assertEqual(data["tencent"]["secret_id"], "AKIDplaintext")
        self.assertEqual(data["tencent"]["secret_key"], "SKplaintext")

    def test_looks_masked_detector(self):
        for value in ("****", "****abcd", "********" + "x" * 8):
            self.assertTrue(pg_admin.looks_masked(value), value)
        for value in ("", "abc", "a***b", "*abc", "AKIDexample", "12345"):
            self.assertFalse(pg_admin.looks_masked(value), value)

    def test_real_value_does_overwrite(self):
        data = pg_admin.default_secrets()
        data["dnspod"] = {"id": "12345", "key": "old"}
        pg_admin.apply_changes(data, {"dnspod": {"key": "new-key"}})
        self.assertEqual(data["dnspod"]["key"], "new-key")
        self.assertEqual(data["dnspod"]["id"], "12345")        # 没填的不动

    def test_domain_is_normalized(self):
        data = pg_admin.default_secrets()
        pg_admin.apply_changes(data, {"record": {"root": "  example.com.  ",
                                                 "sub": " Print "}})
        self.assertEqual(data["record"]["root"], "example.com")
        self.assertEqual(data["record"]["sub"], "print")

    def test_invalid_domain_rejected(self):
        for root in ("example", "example.com/evil", "-bad.com", "a..b", "example .xyz"):
            data = pg_admin.default_secrets()
            with self.assertRaises(pg_admin.AdminError, msg=root):
                pg_admin.apply_changes(data, {"record": {"root": root}})

    def test_invalid_subdomain_rejected(self):
        for sub in ("print.extra", "a_b", "-x", "x-", "*"):
            data = pg_admin.default_secrets()
            with self.assertRaises(pg_admin.AdminError, msg=sub):
                pg_admin.apply_changes(data, {"record": {"sub": sub}})

    def test_ttl_below_dnspod_minimum_rejected(self):
        """免费版 DNSPod 的 TTL 下限是 600，写 60 会被 API 顶回来。"""
        with self.assertRaises(pg_admin.AdminError):
            pg_admin.apply_changes(pg_admin.default_secrets(), {"ttl": 60})

    def test_provider_switch_preserves_both_credential_sets(self):
        """换凭据类型不该把另一套抹掉 —— 用户可能只是试试哪种能通。"""
        data = pg_admin.default_secrets()
        data["dnspod"] = {"id": "1", "key": "k"}
        pg_admin.apply_changes(data, {"provider": "tencent",
                                      "tencent": {"secret_id": "AKID", "secret_key": "SK"}})
        self.assertEqual(data["provider"], "tencent")
        self.assertEqual(data["dnspod"]["key"], "k")
        self.assertTrue(pg_admin.has_credentials(data))


class TestSecretsFile(SecretsCase):
    def test_defaults_when_missing(self):
        data = pg_admin.load_secrets()
        self.assertEqual(data["provider"], "dnspod")
        self.assertEqual(data["ttl"], pg_dns.DEFAULT_TTL)

    def test_corrupt_file_falls_back_to_defaults(self):
        """半截 JSON 不该让网关起不来。"""
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{broken json")
        self.assertEqual(pg_admin.load_secrets()["provider"], "dnspod")

    def test_partial_file_gets_missing_keys_filled(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"provider": "tencent"}, fh)
        data = pg_admin.load_secrets()
        self.assertEqual(data["provider"], "tencent")
        self.assertIn("dnspod", data)
        self.assertIn("record", data)

    def test_save_is_atomic_and_600(self):
        data = pg_admin.default_secrets()
        data["dnspod"] = {"id": "1", "key": "k"}
        pg_admin.save_secrets(data)
        if os.name == "posix":
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(self.path + ".tmp"))   # 临时文件已 rename
        self.assertEqual(pg_admin.load_secrets()["dnspod"]["key"], "k")

    def test_clear_credentials_wipes_both(self):
        data = pg_admin.default_secrets()
        data["dnspod"] = {"id": "1", "key": "k"}
        data["tencent"] = {"secret_id": "a", "secret_key": "b"}
        pg_admin.clear_credentials(data)
        self.assertFalse(pg_admin.has_credentials(data))

    def test_has_credentials_follows_provider(self):
        data = pg_admin.default_secrets()
        data["dnspod"] = {"id": "1", "key": "k"}
        data["provider"] = "dnspod"
        self.assertTrue(pg_admin.has_credentials(data))
        data["provider"] = "tencent"                           # 切到没配的那套
        self.assertFalse(pg_admin.has_credentials(data))


class TestFullDomain(unittest.TestCase):
    def test_joins_when_both_present(self):
        self.assertEqual(pg_admin.full_domain(
            {"record": {"root": "example.com", "sub": "print"}}), "print.example.com")

    def test_empty_when_incomplete(self):
        for rec in ({}, {"root": "example.com"}, {"sub": "print"},
                    {"root": "", "sub": ""}):
            self.assertEqual(pg_admin.full_domain({"record": rec}), "")


class TestIsLanAddr(unittest.TestCase):
    """
    admin 的唯一准入判据。**公网来源必须判为 False** —— 判错就是把管理页
    连同 DNS 令牌一起挂到公网上。
    """

    def test_private_and_loopback_are_lan(self):
        for addr in ("192.168.1.100", "10.0.0.5", "172.16.0.9", "127.0.0.1",
                     "169.254.1.1", "::1", "fd00::1", "fe80::1",
                     "192.168.1.100:54321", "[::1]:8080", "[fd00::1]:443"):
            self.assertTrue(pg_admin.is_lan_addr(addr), addr)

    def test_public_addresses_are_not_lan(self):
        for addr in ("1.2.3.4", "8.8.8.8", "1.1.1.1",
                     "1.2.3.4:54321", "[2001:4860:4860::8888]:8443",
                     "2001:4860:4860::8888"):
            self.assertFalse(pg_admin.is_lan_addr(addr), addr)

    def test_garbage_fails_closed(self):
        """解析不出来 → 判为**非内网**。宁可把自己关在外面，也不能敞开。"""
        for addr in ("", "unknown", "not-an-ip", "999.999.999.999", None, "::gg"):
            self.assertFalse(pg_admin.is_lan_addr(addr), repr(addr))


class TestOpensslTime(unittest.TestCase):
    def test_parses_gmt_as_utc_not_local(self):
        """
        2026-09-18 06:30:00 GMT == 1789713000。
        若误用 mktime（按本地时区解释），在 UTC+8 机器上会差 28800 秒。
        """
        parsed = pg_admin._parse_openssl_time("Sep 18 06:30:00 2026 GMT")
        self.assertIsNotNone(parsed)
        self.assertEqual(int(parsed), 1789713000)
        # 再钉一遍：与标准库对同一时刻的换算必须一致
        import datetime
        self.assertEqual(int(parsed),
                         int(datetime.datetime(2026, 9, 18, 6, 30,
                                               tzinfo=datetime.timezone.utc).timestamp()))

    def test_handles_space_padded_day(self):
        """openssl 的日不补零，输出 `Sep  8 ...`（中间两个空格）。"""
        parsed = pg_admin._parse_openssl_time("Sep  8 06:30:00 2026 GMT")
        self.assertIsNotNone(parsed)
        self.assertEqual(int(parsed), 1788849000)

    def test_rejects_garbage(self):
        for text in ("", "not a date", None):
            self.assertIsNone(pg_admin._parse_openssl_time(text))


class TestCertStatus(SecretsCase):
    def setUp(self):
        super().setUp()
        self.tls = os.path.join(self.tmp.name, "tls")
        os.makedirs(self.tls)
        for name, value in (("TLS_DIR", self.tls),
                            ("CERT_CRT", "fullchain.crt"),
                            ("CERT_KEY", "privkey.key")):
            patcher = mock.patch.object(pg_admin, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_missing_files_reports_absent(self):
        status = pg_admin.cert_status()
        self.assertFalse(status["exists"])
        self.assertIsNone(status["days_left"])

    def test_key_without_cert_is_still_absent(self):
        """只有私钥没有证书链，等于没有可用证书。"""
        with open(os.path.join(self.tls, "privkey.key"), "w") as fh:
            fh.write("KEY")
        self.assertFalse(pg_admin.cert_status()["exists"])

    def test_present_cert_computes_days_left(self):
        for name in ("fullchain.crt", "privkey.key"):
            with open(os.path.join(self.tls, name), "w") as fh:
                fh.write("X")
        future = time.strftime("%b %d %H:%M:%S %Y GMT", time.gmtime(time.time() + 45 * 86400))
        with mock.patch.object(pg_admin, "_openssl_cert_info",
                               return_value={"not_after": future, "not_before": None,
                                             "subject": "CN=print.example.com",
                                             "days_left": 45}):
            status = pg_admin.cert_status()
        self.assertTrue(status["exists"])
        self.assertEqual(status["days_left"], 45)
        self.assertFalse(status["needs_renew"])

    def test_needs_renew_below_threshold(self):
        for name in ("fullchain.crt", "privkey.key"):
            with open(os.path.join(self.tls, name), "w") as fh:
                fh.write("X")
        with mock.patch.object(pg_admin, "_openssl_cert_info",
                               return_value={"not_after": "x", "not_before": None,
                                             "subject": "", "days_left": 12}):
            self.assertTrue(pg_admin.cert_status()["needs_renew"])


class TestSyncDdns(SecretsCase):
    def _secrets(self, root="example.com", sub="print"):
        data = pg_admin.default_secrets()
        data["record"] = {"root": root, "sub": sub}
        data["dnspod"] = {"id": "1", "key": "k"}
        return data

    def _run(self, data, before, current="1.2.3.4", force=False):
        """把 detect_wan_ip / DNS 客户端 / 状态落盘全换成桩。"""
        fake_dns = mock.Mock()
        fake_dns.get_ip.return_value = before
        fake_dns.set_ip.return_value = ("modified" if before else "created")
        with mock.patch.object(pg_dns, "detect_wan_ip", return_value=current), \
             mock.patch.object(pg_dns, "make_dns", return_value=fake_dns), \
             mock.patch.object(pg_admin, "save_state"):
            result = pg_admin.sync_ddns(data, force=force)
        return result, fake_dns

    def test_writes_only_when_ip_changed(self):
        result, fake = self._run(self._secrets(), before="1.1.1.1")
        self.assertTrue(result["changed"])
        fake.set_ip.assert_called_once_with("example.com", "print", "1.2.3.4")

    def test_skips_write_when_ip_same(self):
        result, fake = self._run(self._secrets(), before="1.2.3.4")
        self.assertFalse(result["changed"])
        self.assertEqual(result["action"], "skip")
        fake.set_ip.assert_not_called()

    def test_force_writes_even_when_same(self):
        result, fake = self._run(self._secrets(), before="1.2.3.4", force=True)
        fake.set_ip.assert_called_once()

    def test_creates_when_record_missing(self):
        result, fake = self._run(self._secrets(), before=None)
        self.assertTrue(result["changed"])
        self.assertEqual(result["action"], "created")

    def test_only_touches_configured_record(self):
        """所有 DNS 写操作都必须限定在 (root, sub) 上 —— 根域有别人在维护。"""
        _, fake = self._run(self._secrets(root="example.com", sub="print"), before="1.1.1.1")
        args = fake.set_ip.call_args[0]
        self.assertEqual(args[0], "example.com")
        self.assertEqual(args[1], "print")

    def test_missing_domain_raises(self):
        data = self._secrets(root="", sub="")
        with self.assertRaises(pg_admin.AdminError):
            pg_admin.sync_ddns(data)

    def test_missing_credentials_raises(self):
        data = self._secrets()
        data["dnspod"] = {"id": "", "key": ""}
        with self.assertRaises(pg_admin.AdminError):
            pg_admin.sync_ddns(data)

    def test_dns_failure_propagates(self):
        fake_dns = mock.Mock()
        fake_dns.get_ip.side_effect = pg_dns.DnsError("网络错误")
        with mock.patch.object(pg_dns, "detect_wan_ip", return_value="1.2.3.4"), \
             mock.patch.object(pg_dns, "make_dns", return_value=fake_dns), \
             mock.patch.object(pg_admin, "save_state"):
            with self.assertRaises(pg_dns.DnsError):
                pg_admin.sync_ddns(self._secrets())


class TestStateSemantics(SecretsCase):
    """`save_state` 的 update 语义：只有 None 表示删除。"""

    def test_none_deletes_key(self):
        pg_admin.save_state({"a": 1, "b": 2})
        state = pg_admin.save_state({"b": None})
        self.assertEqual(state, {"a": 1})
        self.assertNotIn("b", pg_admin.load_state())

    def test_falsy_values_are_kept_not_deleted(self):
        """False / 0 / "" 都是有效值，不能被当成「要删除」。"""
        pg_admin.save_state({"flag": None, "num": None, "text": None})
        state = pg_admin.save_state({"flag": False, "num": 0, "text": ""})
        self.assertIs(state["flag"], False)
        self.assertEqual(state["num"], 0)
        self.assertEqual(state["text"], "")


class TestStaleErrorClearing(SecretsCase):
    """
    故障恢复后必须把 `last_error` 清掉。

    真实场景：凭据录错 → 页面积下一条 `[10003] 传入的 Token 不存在`；改对之后
    DDNS 每分钟都在正常跑，但那条报错如果赖着不走，用户会以为还是坏的。
    """

    def _sync(self, before, current="1.2.3.4"):
        data = pg_admin.default_secrets()
        data["record"] = {"root": "example.com", "sub": "print"}
        data["dnspod"] = {"id": "1", "key": "k"}
        fake_dns = mock.Mock()
        fake_dns.get_ip.return_value = before
        fake_dns.set_ip.return_value = "modified"
        with mock.patch.object(pg_dns, "detect_wan_ip", return_value=current), \
             mock.patch.object(pg_dns, "make_dns", return_value=fake_dns):
            pg_admin.sync_ddns(data)
        return pg_admin.load_state()

    def test_successful_write_clears_stale_error(self):
        pg_admin.save_state({"last_error": "DNSPod Record.List 失败：[10003] 传入的 Token 不存在",
                             "last_error_at": 1789715693})
        state = self._sync(before="1.1.1.1")
        self.assertNotIn("last_error", state)
        self.assertNotIn("last_error_at", state)
        self.assertEqual(state["ip"], "1.2.3.4")

    def test_skip_path_also_clears(self):
        """IP 没变（每 60 秒的稳态）同样算「跑通了」，也得清。"""
        pg_admin.save_state({"last_error": "旧的失败"})
        state = self._sync(before="1.2.3.4")
        self.assertNotIn("last_error", state)
        self.assertEqual(state["action"], "skip")

    def test_clearing_leaves_other_keys_alone(self):
        pg_admin.save_state({"last_change": 123, "last_error": "旧错"})
        state = self._sync(before="1.2.3.4")     # 走 skip 路径，不动 last_change
        self.assertEqual(state["last_change"], 123)
        self.assertNotIn("last_error", state)


class TestTlsRuntimeDetection(SecretsCase):
    """
    状态页要答的是「HTTPS 现在能不能用」，不是「配置里写没写」。

    之前的实现让 `collect_status` 的 `tls_enabled` 默认 False、调用方又都不传，
    于是证书签好、8443 正常监听的情况下页面依旧显示未启用 —— 会把人带沟里。
    """

    def _unit(self, text):
        path = os.path.join(self.tmp.name, "override.conf")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return (path,)

    def test_parse_accepts_space_and_equals(self):
        self.assertEqual(pg_admin.parse_tls_port("--tls-port 8443"), 8443)
        self.assertEqual(pg_admin.parse_tls_port("--tls-port=9443"), 9443)

    def test_parse_rejects_absent_or_invalid(self):
        for text in ("", None, "--port 8080", "--tls-port 0",
                     "--tls-port 99999", "--tls-port abc"):
            self.assertEqual(pg_admin.parse_tls_port(text), 0, text)

    def test_enabled_only_when_actually_listening(self):
        units = self._unit("ExecStart=... --tls-port 8443")
        with mock.patch.object(pg_admin, "GATEWAY_UNIT_PATHS", units):
            with mock.patch.object(pg_admin, "_port_listening", return_value=True):
                self.assertEqual(pg_admin.detect_tls_runtime(),
                                 {"enabled": True, "port": 8443})
            # 配置说开了、实际连不上（进程崩了/证书读不出来）→ 报未启用
            with mock.patch.object(pg_admin, "_port_listening", return_value=False):
                self.assertEqual(pg_admin.detect_tls_runtime(),
                                 {"enabled": False, "port": 8443})

    def test_disabled_when_not_configured(self):
        units = self._unit("ExecStart=... --port 8080")
        with mock.patch.object(pg_admin, "GATEWAY_UNIT_PATHS", units):
            self.assertEqual(pg_admin.detect_tls_runtime(),
                             {"enabled": False, "port": 0})

    def test_missing_unit_files_do_not_raise(self):
        with mock.patch.object(pg_admin, "GATEWAY_UNIT_PATHS",
                               (os.path.join(self.tmp.name, "nope.conf"),)):
            self.assertEqual(pg_admin.detect_tls_runtime(),
                             {"enabled": False, "port": 0})

    def test_explicit_argument_still_wins(self):
        """显式传参时不做探测（网关进程内自报场景走这条）。"""
        with mock.patch.object(pg_admin, "detect_tls_runtime") as probe:
            status = pg_admin.collect_status(pg_admin.default_secrets(),
                                             tls_enabled=True, tls_port=8443)
        probe.assert_not_called()
        self.assertEqual(status["tls"], {"enabled": True, "port": 8443})


class TestVerifyDelegation(SecretsCase):
    def test_checks_domain_and_credentials_before_calling_out(self):
        data = pg_admin.default_secrets()
        with mock.patch.object(pg_dns, "verify_credentials") as inner:
            result = pg_admin.verify_credentials(data)
        self.assertFalse(result["ok"])
        self.assertIn("域名", result["message"])
        inner.assert_not_called()

    def test_delegates_when_configured(self):
        data = pg_admin.default_secrets()
        data["record"] = {"root": "example.com", "sub": "print"}
        data["dnspod"] = {"id": "1", "key": "k"}
        with mock.patch.object(pg_dns, "verify_credentials",
                               return_value={"ok": True, "current": "1.2.3.4",
                                             "message": "ok"}) as inner:
            result = pg_admin.verify_credentials(data)
        self.assertTrue(result["ok"])
        inner.assert_called_once()


class TestIssueCert(SecretsCase):
    """
    签发的前置检查。真实签发要打 Let's Encrypt，联调在设备上用手动触发。
    """

    def _ready(self):
        data = pg_admin.default_secrets()
        data["record"] = {"root": "example.com", "sub": "print"}
        data["dnspod"] = {"id": "1", "key": "k"}
        return data

    def test_requires_acme_sh(self):
        with mock.patch.object(pg_admin, "acme_bin", return_value=None):
            with self.assertRaises(pg_admin.AdminError) as ctx:
                pg_admin.issue_cert(self._ready())
        self.assertIn("acme.sh", str(ctx.exception))

    def test_requires_domain(self):
        data = self._ready()
        data["record"] = {"root": "", "sub": ""}
        with mock.patch.object(pg_admin, "acme_bin", return_value="/x/acme.sh"):
            with self.assertRaises(pg_admin.AdminError) as ctx:
                pg_admin.issue_cert(data)
        self.assertIn("域名", str(ctx.exception))

    def test_requires_credentials(self):
        data = self._ready()
        data["dnspod"] = {"id": "", "key": ""}
        with mock.patch.object(pg_admin, "acme_bin", return_value="/x/acme.sh"):
            with self.assertRaises(pg_admin.AdminError) as ctx:
                pg_admin.issue_cert(data)
        self.assertIn("凭据", str(ctx.exception))

    def test_env_carries_dnspod_credentials(self):
        env, plugin = pg_admin._acme_env(self._ready())
        self.assertEqual(plugin, "dns_dp")
        self.assertEqual(env["DP_Id"], "1")
        self.assertEqual(env["DP_Key"], "k")
        self.assertEqual(env["HOME"], "/root")

    def test_env_carries_tencent_credentials(self):
        data = self._ready()
        data["provider"] = "tencent"
        data["tencent"] = {"secret_id": "AKID", "secret_key": "SK"}
        env, plugin = pg_admin._acme_env(data)
        self.assertEqual(plugin, "dns_tencent")
        self.assertEqual(env["Tencent_SecretId"], "AKID")
        self.assertEqual(env["Tencent_SecretKey"], "SK")

    def test_issue_passes_domain_and_ecdsa(self):
        with mock.patch.object(pg_admin, "acme_bin", return_value="/x/acme.sh"), \
             mock.patch.object(pg_admin, "_run",
                               return_value=(0, "ok")) as run, \
             mock.patch.object(pg_admin, "cert_status", return_value={"exists": True}):
            result = pg_admin.issue_cert(self._ready())
        self.assertTrue(result["ok"])
        issue_cmd = run.call_args_list[0][0][0]
        self.assertIn("-d", issue_cmd)
        self.assertIn("print.example.com", issue_cmd)
        self.assertIn("ec-256", issue_cmd)
        self.assertIn("--dns", issue_cmd)
        self.assertIn("dns_dp", issue_cmd)
        # 续期靠这个 reloadcmd 让 8443 载入新证书
        install_cmd = run.call_args_list[1][0][0]
        self.assertIn("--install-cert", install_cmd)
        self.assertIn("--reloadcmd", install_cmd)

    def test_issue_failure_reported_not_raised(self):
        with mock.patch.object(pg_admin, "acme_bin", return_value="/x/acme.sh"), \
             mock.patch.object(pg_admin, "_run", return_value=(1, "DNS 验证失败")):
            result = pg_admin.issue_cert(self._ready())
        self.assertFalse(result["ok"])
        self.assertIn("签发失败", result["message"])
        self.assertIn("DNS 验证失败", result["steps"][0]["log"])

    def test_install_failure_reported(self):
        with mock.patch.object(pg_admin, "acme_bin", return_value="/x/acme.sh"), \
             mock.patch.object(pg_admin, "_run",
                               side_effect=[(0, "issued"), (1, "权限不足")]):
            result = pg_admin.issue_cert(self._ready())
        self.assertFalse(result["ok"])
        self.assertIn("安装失败", result["message"])


class TestAutoRenew(SecretsCase):
    def test_skips_when_no_cert(self):
        with mock.patch.object(pg_admin, "cert_status", return_value={"exists": False}):
            result = pg_admin.auto_renew()
        self.assertTrue(result["skipped"])

    def test_skips_when_enough_days_left(self):
        with mock.patch.object(pg_admin, "cert_status",
                               return_value={"exists": True, "needs_renew": False,
                                             "days_left": 60}):
            result = pg_admin.auto_renew()
        self.assertTrue(result["skipped"])
        self.assertIn("60", result["message"])

    def test_renews_when_due(self):
        with mock.patch.object(pg_admin, "cert_status",
                               return_value={"exists": True, "needs_renew": True,
                                             "days_left": 10}), \
             mock.patch.object(pg_admin, "load_secrets", return_value={}), \
             mock.patch.object(pg_admin, "issue_cert",
                               return_value={"ok": True, "message": "续期完成"}), \
             mock.patch.object(pg_admin, "save_state"):
            result = pg_admin.auto_renew()
        self.assertTrue(result["ok"])
        self.assertFalse(result.get("skipped"))


class TestCollectStatus(SecretsCase):
    def test_survives_without_configuration(self):
        """首次打开 admin 时什么都没配，页面也必须能渲染（不能抛异常）。"""
        with mock.patch.object(pg_admin, "acme_bin", return_value=None):
            status = pg_admin.collect_status(pg_admin.default_secrets())
        self.assertEqual(status["domain"], "")
        self.assertFalse(status["has_credentials"])
        self.assertIn("cert", status)
        self.assertIn("tls", status)

    def test_dns_lookup_failure_does_not_break_status(self):
        data = pg_admin.default_secrets()
        data["record"] = {"root": "example.com", "sub": "print"}
        data["dnspod"] = {"id": "1", "key": "k"}
        with mock.patch.object(pg_dns, "make_dns",
                               side_effect=pg_dns.DnsError("超时")), \
             mock.patch.object(pg_admin, "acme_bin", return_value=None):
            status = pg_admin.collect_status(data)
        self.assertIsNone(status["current_ip"])
        self.assertEqual(status["domain"], "print.example.com")

    def test_status_never_leaks_plaintext_secrets(self):
        data = pg_admin.default_secrets()
        data["dnspod"] = {"id": "1234567", "key": "topsecretkey"}
        with mock.patch.object(pg_admin, "acme_bin", return_value=None):
            status = pg_admin.collect_status(data)
        blob = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("topsecretkey", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
