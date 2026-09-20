#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
命令行生成二维码贴纸 —— 不开管理页、不开浏览器、不经过任何 HTTP 关口。

为什么需要它
------------
管理页是生成贴纸的正常入口，但有一种部署**必须**绕开它：

    内网穿透 / 反向代理（DDNSTO、frp、Cloudflare Tunnel…）

这类部署下网关自己跑明文 HTTP，公网地址由隧道边缘提供，于是配置里会有
`--public-url`。而管理页的三道关里第一道是

    if not pg_admin.is_lan_addr(self.client_address[0]):   # 公网来源 → 404

判的是**源 IP**。隧道客户端偏偏就跑在同一台设备的局域网里 —— 它转发过来的
请求源 IP 是内网地址，所以这道关会被**反向**绕过：一旦给公网实例配了
`--admin-token`，管理页就真的对全网开放了，只剩口令一层。

结论是公网实例的管理页**必须关着**。但「公网码」这个值又只有知道
`--public-url` 的那个实例算得出来 —— 于是就卡住了：算得出来的不敢开管理页，
开着管理页的不知道公网地址。

这个脚本把「算码」和「开管理页」解耦：它是个普通命令行程序，
`build_codes()` 与网关 `Handler._sticker_avail()` 用同一套规则拼 URL，
但不经过 HTTP、不查源 IP、不比对口令。想什么时候重出贴纸都行 ——
换了域名、换了口令、换台机器，都不用为了印一张纸去动公网暴露面。

用法
----
    # 只有内网（不出公网码，其余两张照常）
    python3 make_sticker.py --host 192.168.1.110 --port 8080 --out sticker.pdf

    # 内网 + 内网穿透：出三张码，公网码里嵌好口令
    python3 make_sticker.py --host 192.168.1.110 --port 8080 \
        --public-url https://gw.example.com \
        --token <你的公网口令> --out sticker.pdf

    # 只看内容不落盘（确认 URL 拼得对不对）
    python3 make_sticker.py --host 192.168.1.110 --public-url https://x.example.com \
        --token abc --dry-run

上面的 `--public-url` 与 `--token` 都是**占位符**：请填你自己的隧道域名与口令。
贴纸里印的就是这两项的原文，别把真实值写进本文件（本仓库是公开的）。

公网链接与网关保持一致：`<public-url 去尾斜杠>/?t=<token>`，
和 `Handler._public_url()` 的 --public-url 分支逐字符相同 ——
否则从管理页印的和从这里印的两张贴纸会不一样。
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pg_sticker                                             # noqa: E402


class StickerCliError(ValueError):
    """参数组合不成立 / 拼不出可用的码。"""


# ---------------------------------------------------------------- URL 拼接

def _netloc_display(url: str) -> str:
    """
    取一个适合印在贴纸小字行的主机名。

    贴纸那一行只有十来个字符的宽度，`https://` 和默认端口都是噪声：
    浏览器只在端口非默认时才显示它，贴纸也不该比浏览器更啰嗦。
    """
    net = urllib.parse.urlparse(url).netloc
    if not net:
        return ""
    host, _, port = net.rpartition(":")
    if not host:                          # 没有端口
        return net
    if urllib.parse.urlparse(url).scheme == "https" and port == "443":
        return host
    if urllib.parse.urlparse(url).scheme == "http" and port == "80":
        return host
    return net


def wan_url(public_url: str, token: str) -> str:
    """
    公网入口 + 口令 —— **与网关 `Handler._public_url()` 的 --public-url 分支
    逐字符对齐**：去掉尾斜杠再拼 `/?t=`。

    两边一旦不同步，同一个部署就会印出两张不一样的贴纸，而这件事
    只有人真的去扫才会发现。
    """
    base = (public_url or "").strip().rstrip("/")
    tail = "?t=%s" % token if token else ""
    return "%s/%s" % (base, tail)


def build_codes(host: str, port: int = 8080, public_url: str = "",
                token: str = "", wan: str = "", kinds=pg_sticker.CODE_KINDS,
                allow_insecure: bool = False) -> tuple[list, dict]:
    """
    按 `kinds` 拼出可用的 Code 列表，返回 `(codes, reasons)`。

    `reasons` 是「某个 kind 为什么没出」—— 与网关一样，「不可用」是正常状态
    而不是错误：没配公网地址就没有公网码，不该为这个让整张纸印不出来。

    注意这里**不检查** APK 文件是否存在：本机没有 APK 不代表用户不需要那张码
    （APK 可能由另一台实例托管）。要控制就显式用 `kinds` 排除。
    """
    wanted = [k for k in (kinds or ())]
    unknown = [k for k in wanted if k not in pg_sticker.CODE_KINDS]
    if unknown:
        raise StickerCliError("未知的码类型：%s（可选 %s）"
                              % ("、".join(unknown), "/".join(pg_sticker.CODE_KINDS)))
    if not wanted:
        raise StickerCliError("至少要出一种码")

    host = (host or "").strip()
    suffix = "" if int(port) == 80 else ":%d" % int(port)
    base = "http://%s%s" % (host, suffix) if host else ""

    codes, reasons = [], {}

    if "lan" in wanted:
        if not host:
            reasons["lan"] = "探测不到本机局域网地址，且未指定 --host"
        else:
            codes.append(pg_sticker.Code("lan", "同一 Wi-Fi",
                                         "%s%s" % (host, suffix), base + "/"))

    if "wan" in wanted:
        url = (wan or "").strip()
        if not url and public_url:
            url = wan_url(public_url, token)
        if not url:
            reasons["wan"] = "未提供 --public-url（或 --wan）"
        else:
            # 公网码里嵌着口令，明文 HTTP 等于把口令交给沿途每个节点。
            # 网关对「公网可达」的判据是一票否决，这里也保持一致：除非显式放行。
            if url.lower().startswith("http://") and token and not allow_insecure:
                raise StickerCliError(
                    "公网码是明文 http:// 却带着口令 —— 口令会在链路上裸奔。\n"
                    "  要么换成 https:// 的公网入口，要么去掉 --token，\n"
                    "  确实要这么做就加 --allow-insecure（会印出一张会泄口令的贴纸）。")
            codes.append(pg_sticker.Code("wan", "任意网络",
                                         _netloc_display(url) or "公网", url))

    if "app" in wanted:
        if not host:
            reasons["app"] = "探测不到本机局域网地址，且未指定 --host"
        else:
            codes.append(pg_sticker.Code("app", "装 App", "扫码安装", base + "/app"))

    if not codes:
        raise StickerCliError("一个码都拼不出来：%s"
                              % "；".join(sorted(reasons.values())))
    return codes, reasons


# ---------------------------------------------------------------- 命令行

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="make_sticker.py",
        description="生成二维码贴纸 PDF（不经过管理页，适合内网穿透／反向代理部署）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法\n----\n", 1)[-1].split("公网链接与网关")[0])
    ap.add_argument("--host", default="",
                    help="局域网地址（默认自动探测本机地址）")
    ap.add_argument("--port", type=int, default=8080, help="局域网端口（默认 8080）")
    ap.add_argument("--public-url", default="",
                    help="公网入口基地址，如 https://xxx.ddnsto.com。"
                         "给了就出「任意网络」那张码")
    ap.add_argument("--token", default="",
                    help="打印口令，会嵌进公网码里（配 --public-url 时必填）")
    ap.add_argument("--wan", default="",
                    help="直接给出完整公网链接，优先级高于 --public-url")
    ap.add_argument("--kinds", default=",".join(pg_sticker.CODE_KINDS),
                    help="要出哪些码，逗号分隔（默认 %s）"
                         % ",".join(pg_sticker.CODE_KINDS))
    ap.add_argument("--title", default=pg_sticker.DEFAULT_TITLE, help="贴纸顶部标题")
    ap.add_argument("--layout", type=int, default=1, choices=pg_sticker.LAYOUTS,
                    help="一页印几张同样的贴纸（默认 1）")
    ap.add_argument("--out", default="sticker.pdf", help="PDF 输出路径")
    ap.add_argument("--png", default="", help="可选：同时导出一张 PNG 预览")
    ap.add_argument("--dpi", type=int, default=pg_sticker.DEFAULT_DPI)
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印拼出来的链接，不生成文件")
    ap.add_argument("--allow-insecure", action="store_true",
                    help="允许在明文 http:// 公网码里嵌口令（不推荐）")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.public_url and not args.token and not args.wan:
        print("指定了 --public-url 却没给 --token：公网码扫出来会因缺口令被拒。",
              file=sys.stderr)
        print("  免密的公网入口不应该存在；内网那份本来就免密，不必给公网码。",
              file=sys.stderr)
        return 2

    host = args.host or pg_sticker.lan_ip() or ""

    try:
        codes, reasons = build_codes(
            host=host, port=args.port, public_url=args.public_url,
            token=args.token, wan=args.wan,
            kinds=[k.strip() for k in args.kinds.split(",") if k.strip()],
            allow_insecure=args.allow_insecure)
    except StickerCliError as exc:
        print("出不了贴纸：%s" % exc, file=sys.stderr)
        return 2

    print("贴纸上的码：")
    for c in codes:
        print("  %-4s %-10s %s" % (c.kind, c.label, c.url))
    for kind, why in sorted(reasons.items()):
        print("  跳过 %s：%s" % (kind, why))

    if args.dry_run:
        return 0

    try:
        pg_sticker.build_pdf(codes, args.out, layout=args.layout,
                             dpi=args.dpi, title=args.title)
    except (pg_sticker.StickerError, OSError) as exc:
        print("生成 PDF 失败：%s" % exc, file=sys.stderr)
        return 1
    print("已生成 %s（%d 字节，一页 %d 张）"
          % (args.out, os.path.getsize(args.out), args.layout))

    if args.png:
        try:
            img = pg_sticker.build_image(codes, layout=args.layout,
                                         dpi=args.dpi, title=args.title)
            img.save(args.png)
        except (pg_sticker.StickerError, OSError) as exc:
            print("生成 PNG 预览失败：%s" % exc, file=sys.stderr)
            return 1
        print("已生成 %s（%dx%d）" % (args.png, img.width, img.height))
    return 0


if __name__ == "__main__":
    sys.exit(main())
