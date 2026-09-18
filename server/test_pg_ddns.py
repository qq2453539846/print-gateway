#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pg_ddns 测试：定时任务入口的退出码与「安静」语义。

定时任务最怕两件事：**该报的错不报**（静默失败）和**没事也刷日志**
（一天 1400 条把真故障淹掉）。这组测试把两者都钉住。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_admin                                                   # noqa: E402
import pg_ddns                                                    # noqa: E402
import pg_dns                                                     # noqa: E402


def _configured():
    data = pg_admin.default_secrets()
    data["record"] = {"root": "example.com", "sub": "print"}
    data["dnspod"] = {"id": "1", "key": "k"}
    return data


class TestDoDdns(unittest.TestCase):
    def test_unconfigured_is_not_an_error(self):
        """没配域名/凭据时静默通过 —— 否则 journal 会被每分钟一条的错误刷满。"""
        with mock.patch.object(pg_admin, "load_secrets",
                               return_value=pg_admin.default_secrets()), \
             mock.patch.object(pg_admin, "sync_ddns") as sync:
            self.assertEqual(pg_ddns.do_ddns(False, True), 0)
        sync.assert_not_called()

    def test_missing_credentials_also_skips(self):
        data = _configured()
        data["dnspod"] = {"id": "", "key": ""}
        with mock.patch.object(pg_admin, "load_secrets", return_value=data), \
             mock.patch.object(pg_admin, "sync_ddns") as sync:
            self.assertEqual(pg_ddns.do_ddns(False, True), 0)
        sync.assert_not_called()

    def test_changed_ip_returns_zero(self):
        with mock.patch.object(pg_admin, "load_secrets", return_value=_configured()), \
             mock.patch.object(pg_admin, "sync_ddns",
                               return_value={"changed": True, "domain": "print.example.com",
                                             "ip": "1.2.3.4", "action": "modified",
                                             "before": "1.1.1.1"}):
            self.assertEqual(pg_ddns.do_ddns(False, True), 0)

    def test_unchanged_ip_returns_zero(self):
        with mock.patch.object(pg_admin, "load_secrets", return_value=_configured()), \
             mock.patch.object(pg_admin, "sync_ddns",
                               return_value={"changed": False, "domain": "print.example.com",
                                             "ip": "1.2.3.4", "action": "skip",
                                             "before": "1.2.3.4"}):
            self.assertEqual(pg_ddns.do_ddns(False, True), 0)

    def test_dns_failure_returns_one_and_records_error(self):
        """失败必须**返回非零**：systemd 靠退出码标记单元失败，否则没人会发现。"""
        with mock.patch.object(pg_admin, "load_secrets", return_value=_configured()), \
             mock.patch.object(pg_admin, "sync_ddns",
                               side_effect=pg_dns.DnsError("网络错误")), \
             mock.patch.object(pg_admin, "save_state") as state:
            self.assertEqual(pg_ddns.do_ddns(False, True), 1)
        saved = state.call_args[0][0]
        self.assertIn("网络错误", saved["last_error"])

    def test_admin_error_also_returns_one(self):
        with mock.patch.object(pg_admin, "load_secrets", return_value=_configured()), \
             mock.patch.object(pg_admin, "sync_ddns",
                               side_effect=pg_admin.AdminError("配置缺失")), \
             mock.patch.object(pg_admin, "save_state"):
            self.assertEqual(pg_ddns.do_ddns(False, True), 1)


class TestDoRenew(unittest.TestCase):
    def test_skipped_renew_returns_zero(self):
        with mock.patch.object(pg_admin, "auto_renew",
                               return_value={"ok": True, "skipped": True,
                                             "message": "剩余 80 天"}):
            self.assertEqual(pg_ddns.do_renew(True), 0)

    def test_successful_renew_returns_zero(self):
        with mock.patch.object(pg_admin, "auto_renew",
                               return_value={"ok": True, "message": "续期完成"}):
            self.assertEqual(pg_ddns.do_renew(True), 0)

    def test_failed_renew_returns_one(self):
        with mock.patch.object(pg_admin, "auto_renew",
                               return_value={"ok": False, "message": "续期失败"}):
            self.assertEqual(pg_ddns.do_renew(True), 1)

    def test_exception_returns_one(self):
        with mock.patch.object(pg_admin, "auto_renew",
                               side_effect=pg_admin.AdminError("acme.sh 缺失")):
            self.assertEqual(pg_ddns.do_renew(True), 1)


class TestCli(unittest.TestCase):
    def test_default_mode_is_ddns(self):
        with mock.patch.object(pg_ddns, "do_ddns", return_value=0) as inner:
            self.assertEqual(pg_ddns.main([]), 0)
        inner.assert_called_once()

    def test_renew_mode(self):
        with mock.patch.object(pg_ddns, "do_renew", return_value=0) as inner:
            self.assertEqual(pg_ddns.main(["--mode", "renew", "--quiet"]), 0)
        inner.assert_called_once()

    def test_status_mode_prints_json(self):
        with mock.patch.object(pg_admin, "load_secrets",
                               return_value=pg_admin.default_secrets()), \
             mock.patch.object(pg_admin, "collect_status",
                               return_value={"domain": "print.example.com"}), \
             mock.patch("builtins.print") as printer:
            self.assertEqual(pg_ddns.main(["--mode", "status"]), 0)
        self.assertIn("print.example.com", printer.call_args[0][0])

    def test_quiet_flag_is_forwarded(self):
        with mock.patch.object(pg_ddns, "do_ddns", return_value=0) as inner:
            pg_ddns.main(["--quiet", "--force"])
        self.assertEqual(inner.call_args[0], (True, True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
