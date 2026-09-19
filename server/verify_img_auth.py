#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.8 修复验收：外网预览图的鉴权闭环。

在**设备上**跑（要连本机 8443 的 TLS 端口，并用真 CUPS/poppler 生成预览）。
放 /opt/print-gateway/ 下临时用，验收完可删。

验的是这条链路，任何一环断了用户在公网看到的就是裂图：

    上传 → /api/preview 返回的 images[] → 原样 GET → 必须 200 + 真 PNG

三个判据：
  1. 服务端给出的 URL **自己带了口令**（&t=…）；
  2. 拿这个 URL **原样**请求（不额外加口令）→ 200 且是 PNG；
  3. 把口令摘掉再请求 → 401（证明 /img 仍在鉴权之后，没有为了修 bug 放宽安全）。
"""
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
import uuid

HOST = "127.0.0.1"
PORT = 8443
ENV = "/etc/print-gateway/gateway.env"
PDF = sys.argv[1] if len(sys.argv) > 1 else "/opt/print-gateway/sample_multipage.pdf"

FAIL = []


def ok(msg):
    print("  [OK]   %s" % msg)


def bad(msg):
    print("  [FAIL] %s" % msg)
    FAIL.append(msg)


def token():
    for line in open(ENV, encoding="utf-8", errors="replace"):
        m = re.match(r"\s*PRINT_TOKEN\s*=\s*(\S+)", line)
        if m:
            return m.group(1)
    raise SystemExit("没在 %s 里找到 PRINT_TOKEN" % ENV)


CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

BASE = "https://%s:%d" % (HOST, PORT)
TOK = token()
print("目标 %s（口令 %s…）" % (BASE, TOK[:6]))


def post_form(path, fields, files, headers=None):
    """手搓 multipart：服务端只认 Content-Length，不支持 chunked。"""
    b = "----v38" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (b, k, v)).encode())
    for name, fn, data in files:
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; "
                      "filename=\"%s\"\r\nContent-Type: application/pdf\r\n\r\n"
                      % (b, name, fn)).encode())
        parts.append(data)
        parts.append(b"\r\n")
    parts.append(("--%s--\r\n" % b).encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + b,
                 "Content-Length": str(len(body)), "X-Token": TOK})
    with urllib.request.urlopen(req, context=CTX, timeout=90) as r:
        return json.loads(r.read().decode())


def post_json(path, obj):
    body = json.dumps(obj).encode()
    req = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": "application/json",
                 "Content-Length": str(len(body)), "X-Token": TOK})
    with urllib.request.urlopen(req, context=CTX, timeout=120) as r:
        return json.loads(r.read().decode())


def get(path, with_token=False, timeout=60):
    url = BASE + path
    if with_token:
        url += ("&" if "?" in path else "?") + "t=" + urllib.parse.quote(TOK, safe="")
    try:
        with urllib.request.urlopen(url, context=CTX, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---------------------------------------------------------------- 1. 页面版本
print("\n[1] 页面版本与前端那道保险")
status, body = get("/?t=" + urllib.parse.quote(TOK, safe=""))
page = body.decode("utf-8", "replace")
if status == 200:
    ok("首页 200")
else:
    bad("首页 %d" % status)
m = re.search(r"v3\.(\d+) \d{4}", page)
print("  页面版本：%s" % (m.group(0) if m else "未识别"))
# 别把版本号写死成发布当时那一个 —— v3.8 之后每发一版这条都会误报失败，
# 而它真正想验的是「版本 >= 带这道修复的那版」，不是「等于 v3.8」。
if m and int(m.group(1)) >= 8:
    ok("页面版本 v3.%s（>= v3.8，含本项修复）" % m.group(1))
else:
    bad("页面版本低于 v3.8（浏览器可能拿到缓存，或部署没生效）")
if "img.src = api(src)" in page:
    ok("前端 <img src> 走 api() 补口令")
else:
    bad("前端 <img src> 没走 api()")
if "img.src = src;" in page:
    bad("仍存在裸 img.src = src;")

# ---------------------------------------------------------------- 2. 上传
print("\n[2] 上传")
data = open(PDF, "rb").read()
up = post_form("/api/upload", {"note": "v38-verify"}, [("file", "v38.pdf", data)])
jid = up["id"]
ok("作业 %s（%s 页）" % (jid, up.get("pages")))

# ---------------------------------------------------------------- 3. 预览
print("\n[3] /api/preview 返回的图片 URL")
pv = post_json("/api/preview", {
    "job": jid,
    "spec": {"paper": "A4", "orientation": "portrait", "per_sheet": 1,
             "dpi": 300, "printer": "GW_TEST"}})
urls = pv.get("images") or []
if urls:
    ok("拿到 %d 个预览 URL" % len(urls))
else:
    bad("images 为空：%s" % json.dumps(pv, ensure_ascii=False)[:300])
    raise SystemExit(1)

for u in urls[:3]:
    print("     %s" % u)

if all("&t=" in u for u in urls):
    ok("每个 URL 都自带口令")
else:
    bad("有 URL 没带口令：%s" % [u for u in urls if "&t=" not in u][0])
    raise SystemExit(1)

# ---------------------------------------------------------------- 4. 原样取图
print("\n[4] 原样请求（不再额外补口令）")
u0 = urls[0]
status, body = get(u0, with_token=False)
if status == 200 and body[:8] == b"\x89PNG\r\n\x1a\n":
    ok("HTTP 200，PNG，%d 字节" % len(body))
else:
    bad("HTTP %d，前 8 字节 %r" % (status, body[:8]))

# ---------------------------------------------------------------- 5. 反面对照
print("\n[5] 反面对照：摘掉口令必须 401")
stripped = re.sub(r"[?&]t=[^&]*", "", u0).replace("?&", "?").rstrip("?&")
if stripped.endswith("/img"):
    stripped = stripped.replace("/img", "/img?x=1")
print("     摘掉口令后：%s" % stripped)
status, body = get(stripped, with_token=False)
if status == 401:
    ok("HTTP 401（/img 仍在鉴权之后，安全边界未被放宽）")
elif status == 200 and body[:8] == b"\x89PNG\r\n\x1a\n":
    bad("/img 已不需要口令 —— 安全边界被放宽了")
else:
    bad("预期 401，实际 %d" % status)

# ---------------------------------------------------------------- 6. 内网免密
print("\n[6] 内网明文端口仍然免密（老用户不受影响）")
try:
    with urllib.request.urlopen("http://%s:8080/healthz" % HOST, timeout=15) as r:
        hz = json.loads(r.read().decode())
    if hz.get("ok"):
        ok("8080 /healthz 免密 200，version=%s" % hz.get("version"))
    else:
        bad("healthz 内容异常：%s" % hz)
except Exception as exc:                                        # noqa: BLE001
    bad("8080 免密访问失败：%s" % exc)

print("\n" + "=" * 46)
if FAIL:
    print("验收未通过，%d 项失败：" % len(FAIL))
    for f in FAIL:
        print("  - %s" % f)
    sys.exit(1)
print("全部通过：外网预览图链路已闭环，安全边界未放宽")
