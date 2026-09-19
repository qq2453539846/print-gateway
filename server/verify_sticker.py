#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.9 验收：管理页的二维码贴纸。

在**设备上**跑（要走内网 8080 的 /admin，并借真 poppler / ghostscript 渲染）。
验的正是用户在管理页点「生成贴纸并预览」之后会走的每一步：

    单码预览 SVG → 生成贴纸 PDF → 注册成作业 → /api/job 读回 → 预览图渲染成 PNG

外加两项只有本机能做的复核：

  * 用 poppler 读回贴纸 PDF 的**真实页面尺寸** —— 必须是 A4。差一点点，
    打印驱动就会按「适应页面」把整页重采样一遍，二维码的模块边界跟着糊；
  * 用 ghostscript 把贴纸渲成 PNG —— 出纸链路上 gs 是必经的一环，
    它要吃不消这份 PDF（比如解不了 CCITT G4），后面全都白搭。

用法:
    python3 verify_sticker.py            # 只验，不出纸
    python3 verify_sticker.py --print    # 额外送一份到 GW_TEST
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"
ENV = "/etc/print-gateway/gateway.env"
JOB_DIR = "/var/spool/print-gateway"
TEST_QUEUE = "GW_TEST"

FAIL = []


def ok(msg):
    print("  [OK]   %s" % msg)


def bad(msg):
    print("  [FAIL] %s" % msg)
    FAIL.append(msg)


def admin_token():
    for line in open(ENV, encoding="utf-8", errors="replace"):
        m = re.match(r"\s*ADMIN_TOKEN\s*=\s*(\S+)", line)
        if m:
            return m.group(1)
    raise SystemExit("没在 %s 里找到 ADMIN_TOKEN" % ENV)


TOK = admin_token()


def get(path, timeout=60):
    req = urllib.request.Request(BASE + path, headers={"X-Admin-Token": TOK})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post_json(path, obj, timeout=120):
    body = json.dumps(obj).encode()
    req = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": "application/json",
                 "Content-Length": str(len(body)), "X-Admin-Token": TOK})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except ValueError:
            return e.code, {}


# ---------------------------------------------------------------- 1. 单码预览
print("[1] 管理页的单码预览 /admin/qr.svg")

AVAIL = {}
for kind in ("lan", "wan", "app"):
    status, body = get("/admin/qr.svg?kind=%s" % kind)
    if status == 200 and b"<svg" in body and b"</svg>" in body:
        ok("%-3s 码 200，SVG %d 字节" % (kind, len(body)))
        AVAIL[kind] = True
    else:
        bad("%-3s 码 HTTP %d（前 60 字节 %r）" % (kind, status, body[:60]))
        AVAIL[kind] = False

if not any(AVAIL.values()):
    raise SystemExit("一个码都出不来，后面不用验了")

status, body = get("/admin/qr.svg?kind=not-a-kind")
if status == 404:
    ok("未知 kind → 404（不是任意二维码接口）")
else:
    bad("未知 kind 返回 %d，应当是 404" % status)

# ---------------------------------------------------------------- 2. 生成贴纸
print("\n[2] 生成贴纸作业 /admin/api/sticker")

kinds = [k for k, v in AVAIL.items() if v]
JOBS = {}
for layout in (1, 2, 4):
    status, obj = post_json("/admin/api/sticker",
                            {"kinds": kinds, "layout": layout})
    if status != 200 or "id" not in obj:
        bad("%d 联生成失败：HTTP %s %s" % (layout, status, obj))
        continue
    JOBS[layout] = obj
    ok("%d 联 → 作业 %s（%s，%s 页，codes=%s skipped=%s）"
       % (layout, obj["id"], obj["filename"], obj["pages"],
          ",".join(obj.get("codes") or []), obj.get("skipped") or []))

if not JOBS:
    raise SystemExit("三种版式全失败")

# 选了但不可用的码要如实报回来
status, obj = post_json("/admin/api/sticker",
                        {"kinds": ["lan", "wan", "app"], "layout": 1})
miss = [k for k in ("lan", "wan", "app") if k not in (obj.get("skipped") or [])
        and k not in (obj.get("codes") or [])]
if miss:
    bad("既没印也没报告为跳过：%s" % miss)
else:
    ok("不可用的码如实进了 skipped：%s" % (obj.get("skipped") or "无"))

# 非法版式必须拒绝
status, _ = post_json("/admin/api/sticker", {"kinds": kinds, "layout": 7})
if status == 400:
    ok("非法版式 → 400")
else:
    bad("非法版式返回 %d，应当是 400" % status)

# ---------------------------------------------------------------- 3. 真实页面尺寸
print("\n[3] 回读贴纸 PDF 的页面尺寸与内容")

for layout, obj in sorted(JOBS.items()):
    path = os.path.join(JOB_DIR, obj["id"], "source.pdf")
    if not os.path.exists(path):
        bad("%d 联的 source.pdf 不在 %s" % (layout, path))
        continue
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(5)
    print("     %d 联：%s（%d 字节）" % (layout, path, size))
    if head != b"%PDF-":
        bad("%d 联产出的不是 PDF（头 %r）" % (layout, head))
        continue

    rc = subprocess.run(["pdfinfo", path], capture_output=True, text=True)
    m = re.search(r"Page size:\s*([\d.]+) x ([\d.]+) pts", rc.stdout or "")
    if not m:
        bad("%d 联：pdfinfo 读不出页面尺寸（%s）" % (layout, (rc.stdout or rc.stderr)[:80]))
        continue
    w, h = float(m.group(1)), float(m.group(2))
    if abs(w - 595.276) < 1.0 and abs(h - 841.89) < 1.0:
        ok("%d 联页面 %.2f x %.2f pts ≈ A4" % (layout, w, h))
    else:
        bad("%d 联页面 %.2f x %.2f pts，不是 A4 —— 打印机会重新缩放" % (layout, w, h))

    with open(path, "rb") as fh:
        raw = fh.read()
    if re.search(rb"/CCITTFaxDecode", raw):
        ok("%d 联用 CCITT G4 无损压缩（二维码不该走有损）" % layout)
    else:
        bad("%d 联没用 CCITT G4，可能是有损压缩" % layout)

# ---------------------------------------------------------------- 4. ghostscript
print("\n[4] ghostscript 能不能吃下这份 PDF（出纸链路的必经环节）")

one = sorted(JOBS)[0]
pdf = os.path.join(JOB_DIR, JOBS[one]["id"], "source.pdf")
out = "/tmp/sticker-gs-%d.png" % os.getpid()
rc = subprocess.run(["gs", "-dNOPAUSE", "-dBATCH", "-dQUIET", "-sDEVICE=png16m",
                     "-r150", "-sOutputFile=" + out, pdf],
                    capture_output=True, text=True)
if rc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 2000:
    ok("gs 渲染成功（%d 字节）" % os.path.getsize(out))
    try:
        from PIL import Image
        im = Image.open(out).convert("L")
        dark = sum(1 for p in im.getdata() if p < 128)
        ratio = dark / float(im.width * im.height)
        if 0.005 < ratio < 0.5:
            ok("渲染结果黑色占比 %.3f，看起来是正常的贴纸（不是空白/全黑）" % ratio)
        else:
            bad("渲染结果黑色占比 %.3f，不正常" % ratio)
    except ImportError:
        print("     （没装 PIL，跳过像素检查）")
else:
    bad("gs 渲染失败（exit %s）：%s" % (rc.returncode, (rc.stderr or "")[:200]))
os.path.exists(out) and os.remove(out)

# ---------------------------------------------------------------- 5. 打印面板
print("\n[5] 打印面板能不能读回这个作业")

for layout, obj in sorted(JOBS.items()):
    status, body = get("/api/job?id=%s" % obj["id"])
    if status != 200:
        bad("%d 联 /api/job 返回 %d" % (layout, status))
        continue
    j = json.loads(body.decode())
    if j.get("pages") and j.get("filename"):
        ok("%d 联面板读回：%s / %s 页" % (layout, j["filename"], j["pages"]))
    else:
        bad("%d 联 /api/job 内容不全：%s" % (layout, j))

status, obj = post_json("/api/preview", {
    "job": JOBS[one]["id"],
    "spec": {"paper": "A4", "orientation": "portrait", "per_sheet": 1,
             "dpi": 300, "printer": TEST_QUEUE}})
urls = obj.get("images") or []
if urls:
    ok("预览返回 %d 张图" % len(urls))
    status, png = get(urls[0])
    if status == 200 and png[:8] == b"\x89PNG\r\n\x1a\n":
        ok("预览图 200，PNG %d 字节" % len(png))
    else:
        bad("预览图 HTTP %d，头 %r" % (status, png[:8]))
else:
    bad("预览没有图：%s" % json.dumps(obj, ensure_ascii=False)[:200])

# ---------------------------------------------------------------- 6. 真出纸
if "--print" in sys.argv:
    print("\n[6] 送一份到 %s（会真的出纸）" % TEST_QUEUE)
    rc = subprocess.run(["lp", "-d", TEST_QUEUE, "-t", "sticker-verify", pdf],
                        capture_output=True, text=True)
    if rc.returncode == 0:
        ok("已提交：%s" % (rc.stdout or "").strip())
    else:
        bad("提交失败：%s" % (rc.stderr or "").strip())
else:
    print("\n[6] 跳过真出纸（加 --print 才会打）")

print("\n" + "=" * 46)
if FAIL:
    print("验收未通过，%d 项失败：" % len(FAIL))
    for f in FAIL:
        print("  - %s" % f)
    sys.exit(1)
print("全部通过：贴纸从预览、生成、读回到渲染一路贯通")
