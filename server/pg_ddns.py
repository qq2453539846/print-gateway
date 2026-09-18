# -*- coding: utf-8 -*-
"""
DDNS / 证书续期 的定时任务入口（由 systemd timer 调用）。

为什么不用 acme.sh 自带 cron、也不复用爱快的 DDNS：

* **acme.sh 自带 cron 只做续期**，DDNS 还得另找地方；
* **爱快自己的 DDNS 日志停在 2022-08-09**（20 条全失败），已废弃；
* 统一走这一个入口，**判据都在同一份代码里**（`pg_admin`），admin 页面看到的
  状态和后台真正执行的是同一套逻辑 —— 不会出现「页面说没问题、后台其实没跑」。

用法：
    python3 pg_ddns.py                # 同步一次 DNS（IP 没变则跳过）
    python3 pg_ddns.py --mode renew   # 检查证书剩余天数，不足则续期
    python3 pg_ddns.py --mode status  # 打印当前状态（排查用）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

import pg_admin
import pg_dns

LOG = logging.getLogger("pg-ddns")


def do_ddns(force: bool, quiet: bool) -> int:
    # 还没配域名/凭据时不算「失败」—— 否则 timer 每 60 秒往 journal 里
    # 写一条错误，一天 1400 多条，真正的故障反而被淹掉。
    data = pg_admin.load_secrets()
    if not pg_admin.full_domain(data) or not pg_admin.has_credentials(data):
        if quiet:
            LOG.debug("尚未配置域名或凭据，跳过 DDNS")
        else:
            LOG.info("尚未配置域名或凭据，跳过 DDNS（在 /admin 里配置后自动生效）")
        return 0

    try:
        result = pg_admin.sync_ddns(data, force=force)
    except (pg_admin.AdminError, pg_dns.DnsError) as exc:
        LOG.error("DDNS 失败：%s", exc)
        pg_admin.save_state({"last_error": str(exc), "last_error_at": int(time.time())})
        return 1
    if result["changed"]:
        LOG.info("DNS 已更新：%s → %s（%s，原值 %s）",
                 result["domain"], result["ip"], result["action"], result["before"])
    elif not quiet:
        LOG.info("DNS 无需变更：%s 已是 %s", result["domain"], result["ip"])
    return 0


def do_renew(quiet: bool) -> int:
    try:
        result = pg_admin.auto_renew()
    except (pg_admin.AdminError, pg_dns.DnsError) as exc:
        LOG.error("证书续期失败：%s", exc)
        return 1
    if not result.get("skipped"):
        LOG.info("证书续期：%s", result.get("message"))
    elif not quiet:
        LOG.info("%s", result.get("message"))
    return 0 if result.get("ok") else 1


def do_status() -> int:
    data = pg_admin.load_secrets()
    state = pg_admin.collect_status(data)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打印网关 DDNS / 证书续期")
    ap.add_argument("--mode", choices=("ddns", "renew", "status"), default="ddns")
    ap.add_argument("--force", action="store_true",
                    help="即使 IP 没变也写一次 DNS 记录")
    ap.add_argument("--quiet", action="store_true",
                    help="无事发生时保持安静（适合 timer）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.mode == "ddns":
        return do_ddns(args.force, args.quiet)
    if args.mode == "renew":
        return do_renew(args.quiet)
    return do_status()


if __name__ == "__main__":
    sys.exit(main())
