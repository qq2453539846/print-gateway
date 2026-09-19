#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端验证：把**管理页真正吐出来**的那张单码 SVG 抓下来，用真解码器读一遍。

不要只验「服务端算出来的矩阵对不对」—— 那只证明了 pg_sticker 没写错，
证明不了浏览器拿到的东西是对的。这里走完整条路：
    带 Cookie 请求 /admin  →  取页面上那张 <img src>  →  抓 SVG  →  栅格化  →  解码

口令从环境变量拿（不落盘、不打印）：
    ADMIN_TOKEN=xxx PRINT_TOKEN=yyy python verify_admin_qr.py
也可以只给 --admin-token / --print-token。

跑法（在本地 Windows 上）：
    ATOK=$(ssh gateway-host 'grep ^ADMIN_TOKEN= /etc/print-gateway/gateway.env | cut -d= -f2-')
    PTOK=$(ssh gateway-host 'grep ^PRINT_TOKEN= /etc/print-gateway/gateway.env | cut -d= -f2-')
    ADMIN_TOKEN="$ATOK" PRINT_TOKEN="$PTOK" python verify_admin_qr.py
"""
import argparse
import http.cookiejar
import os
import re
import sys
import urllib.error
import urllib.request

import cv2
import numpy as np

LAN_URL = "http://192.168.1.100:8080/"
WAN_FMT = "https://print.example.com:8443/?t=%s"
PORT = 8443


def opener():
    """带 Cookie 罐的 opener —— 管理页的鉴权靠 Set-Cookie，不靠每次带口令。"""
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def get(op, path, base):
    req = urllib.request.Request(base + path,
                                 headers={"User-Agent": "verify_admin_qr"})
    try:
        with op.open(req, timeout=20) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise SystemExit("HTTP %s 取 %s：%s" % (e.code, path,
                                              e.read().decode("utf-8", "replace")[:200]))


def svg_to_array(svg: bytes):
    """把 render_svg 那种 'M x y h w v8 h-w z' 的路径还原成黑白位图。"""
    text = svg.decode("utf-8")
    m = re.search(r'viewBox="0 0 (\d+) (\d+)"', text)
    if not m:
        raise SystemExit("SVG 里没有 viewBox")
    w, h = int(m.group(1)), int(m.group(2))
    img = np.full((h, w), 255, dtype=np.uint8)
    rects = re.findall(r"M(\d+) (\d+)h(\d+)v(\d+)h-(\d+)z", text)
    if not rects:
        raise SystemExit("SVG 里没解析到任何模块矩形")
    for x, y, rw, rh, _ in rects:
        x, y, rw, rh = int(x), int(y), int(rw), int(rh)
        img[y:y + rh, x:x + rw] = 0
    return img, len(rects), int(rects[0][3])


def decode(img):
    r = cv2.QRCodeDetector().detectAndDecode(img)
    return r[0] if len(r) > 1 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=LAN_URL.rstrip("/"))
    ap.add_argument("--admin-token", default=os.environ.get("ADMIN_TOKEN", ""))
    ap.add_argument("--print-token", default=os.environ.get("PRINT_TOKEN", ""))
    args = ap.parse_args()

    atok, ptok = args.admin_token, args.print_token
    if not atok or not ptok:
        return int(bool(print("缺少 ADMIN_TOKEN / PRINT_TOKEN（见文件头跑法）")))

    expect = {"lan": LAN_URL, "wan": WAN_FMT % ptok}
    op = opener()

    # 1) 带口令访问一次 → 302 + Set-Cookie；再取页面
    get(op, "/admin?t=%s" % atok, args.base)
    page = get(op, "/admin", args.base).decode("utf-8", "replace")
    ver = re.search(r"(v\d+\.\d+ \d{4})", page)
    print("[1] 管理页 %d 字节  版本=%s  含放大浮层 qrZoom：%s"
          % (len(page), ver.group(1) if ver else "?", 'id="qrZoom"' in page))

    # 缩略图是 JS 在 renderSticker 里插进 DOM 的（要等 /admin/api/status 回来），
    # 静态 HTML 里只有那段模板串 —— 所以查模板串，别指望在源码里找到成品 <img>。
    static_ok = ("/admin/qr.svg?kind=" in page and "qr.svg?url=" not in page)
    print("[2] 预览图走服务端现算的 /admin/qr.svg?kind=（不接受前端传 URL）：%s"
          % static_ok)

    ok = static_ok
    for kind, want in expect.items():
        raw = get(op, "/admin/qr.svg?kind=%s" % kind, args.base)
        img, rects, mod = svg_to_array(raw)
        got = decode(img)
        good = (got == want)
        ok = ok and good
        total_mod = img.shape[0] / mod
        print("[3] %-3s 边长%4dpx 总模块%4.0f 矩形%5d 解码 %s"
              % (kind, img.shape[0], total_mod, rects, "OK" if good else "失败"))
        if not good:
            print("      期望 %s\n      实得 %s" % (want, got))
        print("      132px 缩略图 → %.1f px/模块 ；点开放大 320px → %.1f px/模块"
              % (132.0 / total_mod, 320.0 / total_mod))

    print("\n结论：%s" % ("全部通过 —— 页面上的码内容与扫描端解析目标一致"
                        if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
