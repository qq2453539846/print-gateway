#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_e2e.py —— 打印网关端到端验证（在设备上运行）

设计思路
--------
1. 用 reportlab 生成「可识别」的源 PDF：每页在 2列×4行 的槽位网格中，
   只在第 (页码-1) 个槽位画一个实心黑块。于是渲染后只要找出最暗的槽位，
   就能唯一反推该位置放的是哪一页 —— 这样 N-up / 小册子 / 反向打印
   的「页序与位置」都能被机器精确判定，而不是靠肉眼看。

2. 通过 HTTP 调网关的 /api/upload -> /api/print，把结果送进 GW_TEST
   落盘队列（该队列无 PPD，CUPS 原样透传），因此 /tmp/gwtest/*.prn
   就是「排版引擎的真实产物」，可直接用 pdfinfo / pdftoppm 复核。

3. 逐项断言：页数、纸张尺寸、灰度、栅格/矢量路径、页序位置、边距内缩、
   边框存在、镜像翻转。

用法
----
    python3 verify_e2e.py --setup --teardown     # 推荐：临时建队列，跑完删掉
    python3 verify_e2e.py                        # 队列已存在时直接用
    python3 verify_e2e.py --only 小册子          # 只跑标题含该子串的用例
    python3 verify_e2e.py --keep                 # 保留临时目录便于复查

`GW_TEST` 平时**不留在设备上** —— 它会被 CUPS 广播成 `GW_TEST @ <主机名>`，
也出现在网关网页面板的队列下拉里（见 TEST_QUEUE 处的注释）。
`--setup` 按需创建，`--teardown` 跑完清理。
"""

import argparse
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

try:
    from PIL import Image
except ImportError:                                            # pragma: no cover
    sys.stderr.write("需要 Pillow：apt-get install -y python3-pil\n")
    raise

try:
    from reportlab.pdfgen import canvas                        # noqa: F401
except ImportError:                                            # pragma: no cover
    sys.stderr.write("需要 reportlab：apt-get install -y python3-reportlab\n")
    raise

A4W, A4H = 595.276, 841.89            # A4 点尺寸（与引擎 PAPERS 一致）
SRC_PAGES = 8                          # 源文档页数（8 页刚好覆盖小册子 2 张）
TOL = 2.5                              # 尺寸容差（点）

WORK = "/tmp/gwverify"
SPOOL = os.environ.get("GW_TEST_OUTDIR", "/tmp/gwtest")

PASS, FAIL = [], []
ONLY = ""            # --only 过滤串；模块级因为 run_case 要用
SKIPPED: list = []

# ------------------------------------------------------------------ 基础工具
def sh(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ------------------------------------------------------------------ 落盘队列
# GW_TEST 队列**平时不留在设备上**：它 Shared=Yes，会被 CUPS 广播成
# `GW_TEST @ <主机名>`，同时出现在网关网页面板的队列下拉里
# （list_printers() 用 `lpstat -p -d` 抓全部队列，前端不过滤）。
# 用户手滑选中它，作业会落进 /tmp/gwtest：CUPS 报成功、页数也对，就是物理不出纸。
# 所以改为「跑 E2E 时按需创建、跑完删掉」。后端文件 gwtest 本身不广播、不出现在
# 任何列表里，留在设备上零副作用，下次建队列一条命令即可。
TEST_QUEUE = "GW_TEST"
TEST_BACKEND = "/usr/lib/cups/backend/gwtest"


def has_test_queue() -> bool:
    """落盘队列当前是否已注册。用 lpstat 直查，不依赖网关服务在跑。"""
    return TEST_QUEUE in sh(["lpstat", "-v"], timeout=20).stdout


def setup_test_queue() -> bool:
    """按需创建落盘队列，返回是否可用。"""
    if has_test_queue():
        return True
    if not os.path.exists(TEST_BACKEND):
        print("落盘后端缺失：%s" % TEST_BACKEND)
        print("  从开发机装：scp test_backend/gwtest <主机>:%s" % TEST_BACKEND)
        print("  再执行：    chmod 700 %s" % TEST_BACKEND)
        return False
    # -m raw 必须带：不带 PPD，CUPS 才不跑滤镜，落盘的 .prn 就是引擎的真实产物。
    # 漏了它会变成「尽力自动配驱动」的队列，观测到的东西就不可信了。
    r = sh(["lpadmin", "-p", TEST_QUEUE, "-E", "-v", "gwtest:/test", "-m", "raw"],
           timeout=60)
    if r.returncode != 0:
        print("创建 %s 失败：%s" % (TEST_QUEUE, (r.stderr or r.stdout).strip()[:200]))
        return False
    return has_test_queue()


def teardown_test_queue() -> None:
    """删除落盘队列；后端文件保留，供下次 --setup 重建。"""
    if not has_test_queue():
        return
    r = sh(["lpadmin", "-x", TEST_QUEUE], timeout=60)
    if r.returncode == 0:
        print("已移除 %s 队列（后端 %s 保留，下次 --setup 重建）"
              % (TEST_QUEUE, TEST_BACKEND), flush=True)
    else:
        print("移除 %s 失败：%s"
              % (TEST_QUEUE, (r.stderr or r.stdout).strip()[:200]), flush=True)


def url_open(req, timeout=600):
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(base, path, obj):
    body = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(base + path, data=body,
                                 headers={"Content-Type": "application/json"})
    return url_open(req)


def post_multipart(base, path, files):
    """files 可以是单个路径，也可以是路径列表（批量上传用）。"""
    if isinstance(files, str):
        files = [files]
    b = "----gwverify" + uuid.uuid4().hex
    chunks = []
    for fp in files:
        with open(fp, "rb") as fh:
            data = fh.read()
        chunks.append(
            ('--%s\r\nContent-Disposition: form-data; name="file"; '
             'filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n'
             % (b, os.path.basename(fp))).encode("utf-8") + data + b"\r\n")
    body = b"".join(chunks) + ("--%s--\r\n" % b).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/upload", data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % b})
    return url_open(req)

def pdf_pages(path):
    m = re.search(r"^Pages:\s+(\d+)", sh(["pdfinfo", path]).stdout, re.M)
    return int(m.group(1)) if m else -1


def pdf_size(path):
    m = re.search(r"^Page size:\s+([\d.]+) x ([\d.]+)",
                  sh(["pdfinfo", "-box", path]).stdout, re.M)
    return (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)


def pdf_first_size(path):
    """逐页尺寸可能不同，取第一页。"""
    return pdf_size(path)


def render_page(path, page, dpi=72):
    stem = os.path.join(WORK, "pg")
    for old in glob.glob(stem + "*.png"):
        os.remove(old)
    sh(["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi),
        "-png", path, stem])
    files = sorted(glob.glob(stem + "*.png"))
    if not files:
        raise RuntimeError("pdftoppm 没渲染出第 %d 页" % page)
    return Image.open(files[0]).convert("RGB")


def ink_mean(img):
    """平均暗度：0=纯黑，255=纯白。"""
    hist = img.convert("L").histogram()
    total = sum(hist) or 1
    return sum(i * n for i, n in enumerate(hist)) / float(total)


def cell_box(img, cols, rows, col, row):
    w, h = img.size
    return (int(col * w / cols), int(row * h / rows),
            int((col + 1) * w / cols), int((row + 1) * h / rows))


def slot_scores(img, cols=2, rows=4):
    """把图切成 cols×rows，返回每格的平均暗度（越小越黑）。"""
    w, h = img.size
    out = []
    for row in range(rows):
        for col in range(cols):
            box = (int(col * w / cols), int(row * h / rows),
                   int((col + 1) * w / cols), int((row + 1) * h / rows))
            out.append(ink_mean(img.crop(box)))
    return out


def decode_markers(img, cols=2, rows=4):
    """
    从槽位暗度反推「页码」与「朝向」。

    每页画两个标记：主标记占满整个槽位 s，副标记只占槽位 (s+1) mod n
    的中央一小块。两者必然相邻，而「副标记跟在主标记后面」还是
    「排在主标记前面」正好区分了朝向 —— 于是即使整页被旋转 180°，
    读出的页码依然正确。

    （只有一个标记时做不到：槽位 s 转 180° 恰好落在槽位 n-1-s 上，
      第 8 页会被误判成第 1 页。这个坑真的踩过。）

    返回 (页码, 主标记暗度, 副标记暗度)；判定不出时页码为 -1。
    """
    n = cols * rows
    if min(img.size) < 8:
        return -1, 255.0, 255.0
    sc = slot_scores(img, cols, rows)
    order = sorted(range(n), key=lambda i: sc[i])
    b, m = order[0], order[1]              # b=主标记所在槽，m=副标记所在槽
    if m == (b + 1) % n:
        return b + 1, sc[b], sc[m]          # 正立：主标记在前
    if b == (m + 1) % n:
        return n - b, sc[b], sc[m]          # 转过 180°：主标记在后
    return -1, sc[b], sc[m]                 # 两个标记不相邻 -> 无法判定


def identify_page(img):
    """只取页码，判定不出时为 -1。"""
    return decode_markers(img, 2, 4)[0]


# ------------------------------------------------------------------ 生成源文件
def make_marker_pdf(path, n_pages, n_slot=8):
    """
    第 i 页画两个黑块：主块占满槽位 s=(i-1)，副块只占槽位 (s+1) 的中央小块。

    两个相邻标记让「页码」与「朝向」可以同时判定，详见 decode_markers。
    """
    c = canvas.Canvas(path, pagesize=(A4W, A4H))
    cw, ch = A4W / 2.0, A4H / 4.0

    def slot_xy(s):
        col, row = s % 2, s // 2                    # row=0 在最上面
        return col * cw, A4H - (row + 1) * ch       # PDF 原点在左下

    for i in range(1, n_pages + 1):
        s = (i - 1) % n_slot
        c.setFillColorRGB(0, 0, 0)
        x0, y0 = slot_xy(s)
        c.rect(x0 + 12, y0 + 12, cw - 24, ch - 24, stroke=0, fill=1)
        x1, y1 = slot_xy((s + 1) % n_slot)
        c.rect(x1 + cw * 0.36, y1 + ch * 0.36, cw * 0.28, ch * 0.28,
               stroke=0, fill=1)
        c.showPage()
    c.save()


def make_color_pdf(path):
    """彩色块，用于验证灰度转换是否真的去色。"""
    c = canvas.Canvas(path, pagesize=(A4W, A4H))
    for rgb, xy in (((1, 0, 0), (50, 600)), ((0, 1, 0), (300, 600)),
                    ((0, 0, 1), (50, 400)), ((1, 1, 0), (300, 400))):
        c.setFillColorRGB(*rgb)
        c.rect(xy[0], xy[1], 220, 170, stroke=0, fill=1)
    c.showPage()
    c.save()


def make_tone_pdf(path):
    """
    四个象限填不同灰度的 A4 页。

    给分割打印用：分割后每张纸应当是一整片纯色，灰度正好等于它取自的
    那个象限 —— 于是「哪个瓦片来自哪里、顺序对不对」一眼可判，比看图案
    可靠得多（图案映射反了不容易发现，灰度值反了就立刻现形）。
    """
    c = canvas.Canvas(path, pagesize=(A4W, A4H))
    quads = [(0, A4H / 2, 0), (A4W / 2, A4H / 2, 64),      # 左上 / 右上
             (0, 0, 128), (A4W / 2, 0, 192)]               # 左下 / 右下
    for x, y, tone in quads:
        v = tone / 255.0
        c.setFillColorRGB(v, v, v)
        c.rect(x, y, A4W / 2, A4H / 2, stroke=0, fill=1)
    c.showPage()
    c.save()


def make_edge_pdf(path, band_mm=5):
    """页面最外缘一圈黑条 + 中心参考块 —— 用于验证裁剪真把边缘切掉了。"""
    c = canvas.Canvas(path, pagesize=(A4W, A4H))
    b = band_mm * 72.0 / 25.4
    c.setFillColorRGB(0, 0, 0)
    c.rect(0, 0, A4W, b, stroke=0, fill=1)
    c.rect(0, A4H - b, A4W, b, stroke=0, fill=1)
    c.rect(0, 0, b, A4H, stroke=0, fill=1)
    c.rect(A4W - b, 0, b, A4H, stroke=0, fill=1)
    c.rect(A4W / 2 - 70, A4H / 2 - 70, 140, 140, stroke=0, fill=1)
    c.showPage()
    c.save()


def make_blank_pdf(path, pages=2):
    """全白页 —— 专给装饰类用例当画布，免得与源内容混淆。"""
    c = canvas.Canvas(path, pagesize=(A4W, A4H))
    for _ in range(pages):
        c.showPage()
    c.save()


def make_square_pdf(path, side=420.0, band_mm=5):
    """
    正方形页面 + 四周黑框。

    宽高比（1:1）与 A4（0.707:1）差异明显，正好用来区分两种缩放：
    「适应纸张」等比缩放后上下必然留白，「拉伸铺满」非等比拉满、四边贴纸边。
    """
    c = canvas.Canvas(path, pagesize=(side, side))
    b = band_mm * 72.0 / 25.4
    c.setFillColorRGB(0, 0, 0)
    c.rect(0, 0, side, b, stroke=0, fill=1)
    c.rect(0, side - b, side, b, stroke=0, fill=1)
    c.rect(0, 0, b, side, stroke=0, fill=1)
    c.rect(side - b, 0, b, side, stroke=0, fill=1)
    c.rect(side / 2 - 60, side / 2 - 60, 120, 120, stroke=0, fill=1)
    c.showPage()
    c.save()


# ------------------------------------------------------------------ 断言
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %-46s %s%s" % (name, "ok" if cond else "FAIL",
                            "" if cond else "   <-- " + str(detail)), flush=True)


def size_ok(got, want):
    return abs(got[0] - want[0]) <= TOL and abs(got[1] - want[1]) <= TOL


def wait_new_prn(before):
    """等 CUPS 写出新的 .prn，最多 20 秒。"""
    deadline = time.time() + 20
    while time.time() < deadline:
        for f in glob.glob(os.path.join(SPOOL, "*.prn")):
            if f not in before:
                return f
        time.sleep(0.4)
    newest = None
    for f in glob.glob(os.path.join(SPOOL, "*.prn")):
        if newest is None or os.stat(f).st_mtime > os.stat(newest).st_mtime:
            newest = f
    return newest


# ------------------------------------------------------------------ 用例执行
def run_case(base, job_id, printer, title, spec, checks):
    """
    checks: list of (name, callable(pdf_path, result_json) -> (bool, detail))
    """
    if ONLY and ONLY not in title:
        SKIPPED.append(title)
        return None
    print("\n== %s ==  队列=%s" % (title, printer), flush=True)
    before = set(glob.glob(os.path.join(SPOOL, "*.prn")))
    s = dict(spec)
    s["printer"] = printer
    res = post_json(base, "/api/print", {"job": job_id, "spec": s})
    prn = wait_new_prn(before)
    check(title + " / 返回", True,
          "页数=%s 路径=%s" % (res.get("pages"), res.get("mode")))
    if not prn or not os.path.exists(prn):
        check(title + " / 落盘", False, "没有生成 .prn（后端未写出）")
        return None
    for cname, fn in checks:
        try:
            ok, detail = fn(prn, res)
        except Exception as exc:                               # noqa: BLE001
            ok, detail = False, "异常: %r" % (exc,)
        check(cname, ok, detail)
    return prn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    ap.add_argument("--only", default="",
                    help="只跑标题含该子串的用例（调试用，省去重跑全部）")
    ap.add_argument("--setup", action="store_true",
                    help="先创建 %s 落盘队列（需 lpadmin 权限）" % TEST_QUEUE)
    ap.add_argument("--teardown", action="store_true",
                    help="跑完删除 %s 队列，设备上不留痕迹" % TEST_QUEUE)
    args = ap.parse_args()

    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    # 健康检查
    try:
        with urllib.request.urlopen(args.base + "/healthz", timeout=10) as r:
            print("健康检查:", r.read().decode().strip()[:120], flush=True)
    except Exception as exc:                                   # noqa: BLE001
        print("服务不可达：%r" % (exc,))
        return 2

    if args.setup and not setup_test_queue():
        return 2

    printers = url_open(urllib.request.Request(args.base + "/api/printers"))
    names = [p["name"] for p in printers.get("printers", [])]
    print("可用队列:", names, flush=True)
    if TEST_QUEUE not in names:
        print("缺少 %s 落盘队列，无法验证。二选一：" % TEST_QUEUE)
        print("  本脚本自动建：python3 verify_e2e.py --setup --teardown")
        print("  手工建（设备上）：lpadmin -p %s -E -v gwtest:/test" % TEST_QUEUE)
        if not os.path.exists(TEST_BACKEND):
            print("  注意：后端 %s 不存在，需先装 test_backend/gwtest" % TEST_BACKEND)
        if args.teardown:
            teardown_test_queue()        # 别把刚建好的队列留成孤儿
        return 2

    # 生成源文件并上传
    marker = os.path.join(WORK, "markers.pdf")
    make_marker_pdf(marker, SRC_PAGES)
    color = os.path.join(WORK, "colors.pdf")
    make_color_pdf(color)

    up = post_multipart(args.base, "/api/upload", marker)
    mjob = up["id"]
    print("已上传标记文档：%s（%d 页）" % (mjob, up["pages"]), flush=True)
    upc = post_multipart(args.base, "/api/upload", color)
    cjob = upc["id"]
    print("已上传彩色文档：%s（%d 页）" % (cjob, upc["pages"]), flush=True)

    tone = os.path.join(WORK, "tones.pdf")
    make_tone_pdf(tone)
    tjob = post_multipart(args.base, "/api/upload", tone)["id"]
    print("已上传四色阶文档：%s" % tjob, flush=True)

    edge = os.path.join(WORK, "edges.pdf")
    make_edge_pdf(edge)
    ejob = post_multipart(args.base, "/api/upload", edge)["id"]
    print("已上传黑边文档：%s" % ejob, flush=True)

    blank = os.path.join(WORK, "blank.pdf")
    make_blank_pdf(blank, 2)
    bjob = post_multipart(args.base, "/api/upload", blank)["id"]
    print("已上传空白文档：%s" % bjob, flush=True)

    square = os.path.join(WORK, "square.pdf")
    make_square_pdf(square)
    sqjob = post_multipart(args.base, "/api/upload", square)["id"]
    print("已上传方形文档：%s" % sqjob, flush=True)

    ap_ = (A4W, A4H)
    al = (A4H, A4W)

    global ONLY
    ONLY = args.only
    del SKIPPED[:]

    def T(name, fn):
        return (name, fn)

    def c_pages(want):
        return T("页数 = %d" % want,
                 lambda p, r, w=want: (pdf_pages(p) == w, "实得 %d" % pdf_pages(p)))

    def c_size(want, label):
        return T("纸张 %s" % label,
                 lambda p, r, w=want: (size_ok(pdf_size(p), w),
                                       "实得 %.1f x %.1f" % pdf_size(p)))

    def c_mode(want):
        return T("路径 = %s" % want,
                 lambda p, r, w=want: (r.get("mode") == w, "实得 %s" % r.get("mode")))

    def c_ident_page(page_no, sheet_page, cols, rows, col, row):
        """校验输出第 sheet_page 面的 (col,row) 格里放的是源第 page_no 页。"""
        def fn(p, r, pn=page_no, sp=sheet_page, c=cols, rr=rows,
               cc=col, rw=row):
            img = render_page(p, sp)
            box = cell_box(img, c, rr, cc, rw)
            sub = img.crop(box)
            got, dark, second = decode_markers(sub)
            return (got == pn,
                    "期望第%d页 实得第%d页 (主%.0f/副%.0f)" % (pn, got, dark, second))
        return T("第%d面 格(%d,%d) = 源第%d页" % (sheet_page, col, row, page_no), fn)

    def c_booklet(pairs):
        """
        校验小册子的骑马订配对：每面左右两格分别放的是哪一页。

        pairs：[(左页, 右页), ...]，与 pg_layout.booklet_sides 的约定一致。
        小册子的版心会被旋转 90°，所以用带角度试探的识别函数。
        """
        def fn(p, r):
            got, bad = [], []
            for i, (want_l, want_r) in enumerate(pairs, start=1):
                img = render_page(p, i)
                ids = []
                for col in (0, 1):
                    sub = img.crop(cell_box(img, 2, 1, col, 0))
                    ids.append(identify_page(sub))
                got.append(tuple(ids))
                if tuple(ids) != (want_l, want_r):
                    bad.append("第%d面 期望(%s,%s) 实得(%s,%s)"
                               % (i, want_l, want_r, ids[0], ids[1]))
            return (not bad, " | ".join(bad) if bad else "实得 %s" % (got,))
        return T("小册子配对顺序", fn)

    def c_gray(expect):
        def fn(p, r, e=expect):
            img = render_page(p, 1)
            px = img.load()
            w, h = img.size
            worst = 0
            for y in range(0, h, max(1, h // 120)):
                for x in range(0, w, max(1, w // 120)):
                    rr, gg, bb = px[x, y][:3]
                    worst = max(worst, abs(rr - gg), abs(gg - bb), abs(rr - bb))
            return ((worst <= 6) == e,
                    "最大通道差 %d（期望去色=%s）" % (worst, e))
        return T("灰度校正" if expect else "彩色保留", fn)

    def c_mirror():
        """
        源第 1 页的黑块在【左上】，水平镜像后应落在【右上】。

        用象限判定而非简单左右比较：既确认翻转确实发生，
        又确认只翻了水平方向（竖直方向没有被一起翻过去）。
        """
        def fn(p, r):
            img = render_page(p, 1)
            w, h = img.size
            quads = {"左上": (0, 0, w // 2, h // 2),
                     "右上": (w // 2, 0, w, h // 2),
                     "左下": (0, h // 2, w // 2, h),
                     "右下": (w // 2, h // 2, w, h)}
            means = {k: ink_mean(img.crop(v)) for k, v in quads.items()}
            darkest = min(means, key=lambda k: means[k])
            detail = " ".join("%s=%.0f" % (k, v) for k, v in means.items())
            return (darkest == "右上", "最暗象限=%s | %s" % (darkest, detail))
        return T("镜像：黑块由左上移到右上", fn)

    def c_inset(mm_side):
        """有边距时，内容应内缩：四角附近应为空白。"""
        def fn(p, r, m=mm_side):
            img = render_page(p, 1)
            w, h = img.size
            band = int(min(w, h) * (m / 210.0) * 0.6) or 2
            corners = [img.crop((0, 0, band, band)),
                       img.crop((w - band, 0, w, band)),
                       img.crop((0, h - band, band, h)),
                       img.crop((w - band, h - band, w, h))]
            means = [ink_mean(c) for c in corners]
            return (min(means) > 245,
                    "四角均值 %s（应接近 255）" % ",".join("%.0f" % v for v in means))
        return T("页边距内缩（四角留白）", fn)

    def c_ink_zone(zone, expect=True, thr=200):
        """指定区域是否有墨。zone 见下方 boxes。"""
        def fn(p, r, z=zone, e=expect, t=thr):
            img = render_page(p, 1, 100)
            w, h = img.size
            boxes = {"top": (0, 0, w, h // 12),
                     "bottom": (0, h - h // 12, w, h),
                     "center": (w // 3, h // 3, 2 * w // 3, 2 * h // 3),
                     "left": (0, 0, w // 8, h),
                     "right": (w - w // 8, 0, w, h),
                     "corner-tl": (0, 0, w // 8, h // 12),
                     "corner-br": (w - w // 8, h - h // 12, w, h)}
            hist = img.crop(boxes[z]).convert("L").histogram()
            n = sum(hist[:t])
            return (n > 20) == e, "%s 暗像素=%d（期望有内容=%s）" % (z, n, e)
        return T("%s 区域%s" % (zone, "有内容" if expect else "空白"), fn)

    def c_edge_band(has_ink):
        """页面最外缘那一圈是否有黑条 —— 裁剪前后应当相反。"""
        def fn(p, r, want=has_ink):
            img = render_page(p, 1, 100)
            w, h = img.size
            band = max(2, int(min(w, h) * 0.02))
            edges = [ink_mean(img.crop((0, 0, w, band))),
                     ink_mean(img.crop((0, h - band, w, h))),
                     ink_mean(img.crop((0, 0, band, h))),
                     ink_mean(img.crop((w - band, 0, w, h)))]
            darkest = min(edges)
            got = darkest < 140
            return (got == want,
                    "四边最暗均值 %.0f（期望有黑边=%s）" % (darkest, want))
        return T("边缘黑条%s" % ("仍在" if has_ink else "已裁掉"), fn)

    def c_split_tones(order):
        """
        分割打印：第 i 张纸整张的灰度应当等于源页第 order[i] 个象限的灰度。

        源文档四象限是 0 / 64 / 128 / 192，所以每张纸的输出是一整片纯色，
        灰度值直接指出它来自哪个象限。
        """
        def fn(p, r, o=order):
            got = []
            for i in range(1, len(o) + 1):
                got.append(int(ink_mean(render_page(p, i, 50).convert("L"))))
            bad = ["第%d张 期望%d 实得%d" % (i + 1, o[i], got[i])
                   for i in range(len(o)) if abs(got[i] - o[i]) > 20]
            return (not bad, " | ".join(bad) if bad else "灰度 %s" % got)
        return T("分割瓦片映射与顺序", fn)

    def c_border():
        def fn(p, r):
            img = render_page(p, 1)
            w, h = img.size
            edge = ink_mean(img.crop((0, 0, w, 3)))       # 顶边
            mid = ink_mean(img.crop((0, h // 2, w, h // 2 + 3)))
            return (edge < mid - 2,
                    "顶边暗%.1f vs 中部%.1f" % (edge, mid))
        return T("版框线存在", fn)

    # ---------------- 用例 ----------------
    print("\n########## 第一组：基础与页码范围 ##########")

    run_case(args.base, mjob, "GW_TEST", "1版 A4 矢量直出",
             {"paper": "A4", "per_sheet": 1, "scale_mode": "fit"},
             [c_pages(SRC_PAGES), c_size(ap_, "A4 纵向"), c_mode("vector"),
              c_ident_page(1, 1, 1, 1, 0, 0)])

    run_case(args.base, mjob, "GW_TEST", "页码范围 1-2,5",
             {"paper": "A4", "page_range": "1-2,5"},
             [c_pages(3), c_ident_page(1, 1, 1, 1, 0, 0)])

    run_case(args.base, mjob, "GW_TEST", "仅奇数页",
             {"paper": "A4", "page_set": "odd"},
             [c_pages(4), c_ident_page(1, 1, 1, 1, 0, 0)])

    run_case(args.base, mjob, "GW_TEST", "仅偶数页",
             {"paper": "A4", "page_set": "even"},
             [c_pages(4)])

    run_case(args.base, mjob, "GW_TEST", "反向打印",
             {"paper": "A4", "reverse": True},
             [c_pages(SRC_PAGES), c_ident_page(SRC_PAGES, 1, 1, 1, 0, 0)])

    print("\n########## 第二组：色彩与镜像 ##########")

    run_case(args.base, cjob, "GW_TEST", "彩色原样",
             {"paper": "A4"},
             [c_gray(False)])

    run_case(args.base, cjob, "GW_TEST", "灰度转换",
             {"paper": "A4", "grayscale": True},
             [c_gray(True)])

    run_case(args.base, mjob, "GW_TEST", "镜像翻转",
             {"paper": "A4", "mirror": True},
             [c_mode("raster"), c_mirror()])

    print("\n########## 第三组：拼版（每版页数） ##########")

    run_case(args.base, mjob, "GW_TEST", "2版/张 横向并排",
             {"paper": "A4", "per_sheet": 2, "layout_order": "lrtb"},
             [c_pages(4), c_size(al, "A4 横向"), c_mode("raster"),
              c_ident_page(1, 1, 2, 1, 0, 0),
              c_ident_page(2, 1, 2, 1, 1, 0),
              c_ident_page(3, 2, 2, 1, 0, 0),
              c_ident_page(8, 4, 2, 1, 1, 0)])

    run_case(args.base, mjob, "GW_TEST", "4版/张 从左到右从上到下",
             {"paper": "A4", "per_sheet": 4, "layout_order": "lrtb"},
             [c_pages(2), c_size(ap_, "A4 纵向"), c_mode("raster"),
              c_ident_page(1, 1, 2, 2, 0, 0),
              c_ident_page(2, 1, 2, 2, 1, 0),
              c_ident_page(3, 1, 2, 2, 0, 1),
              c_ident_page(4, 1, 2, 2, 1, 1)])

    run_case(args.base, mjob, "GW_TEST", "4版/张 从下到上从左到右",
             {"paper": "A4", "per_sheet": 4, "layout_order": "btlr"},
             [c_pages(2),
              c_ident_page(1, 1, 2, 2, 0, 1),
              c_ident_page(2, 1, 2, 2, 0, 0)])

    print("\n########## 第四组：小册子 ##########")

    run_case(args.base, mjob, "GW_TEST", "小册子 8 页",
             {"paper": "A4", "booklet": True},
             [c_pages(4), c_size(al, "A4 横向"), c_mode("raster"),
              c_booklet([(8, 1), (2, 7), (6, 3), (4, 5)])])

    print("\n########## 第五组：页面装饰 ##########")

    run_case(args.base, mjob, "GW_TEST", "页边距 20mm",
             {"paper": "A4", "margins": {"top": 20, "bottom": 20,
                                         "left": 20, "right": 20}},
             [c_mode("raster"), c_inset(20)])

    run_case(args.base, mjob, "GW_TEST", "版边框",
             {"paper": "A4", "border": True},
             [c_mode("raster"), c_border()])

    print("\n########## 第六组：裁剪 ##########")

    run_case(args.base, ejob, "GW_TEST", "不裁剪时黑边保留",
             {"paper": "A4"},
             [c_mode("vector"), c_edge_band(True)])

    run_case(args.base, ejob, "GW_TEST", "裁剪 15mm",
             {"paper": "A4", "crop_mm": {"top": 15, "bottom": 15,
                                         "left": 15, "right": 15}},
             [c_mode("raster"), c_edge_band(False), c_ink_zone("center", True)])

    run_case(args.base, ejob, "GW_TEST", "裁剪 30mm",
             {"paper": "A4", "crop_mm": {"top": 30, "bottom": 30,
                                         "left": 30, "right": 30}},
             [c_mode("raster"), c_edge_band(False)])

    print("\n########## 第七组：分割打印 ##########")

    run_case(args.base, tjob, "GW_TEST", "分割 2x2",
             {"paper": "A4", "split_rows": 2, "split_cols": 2},
             [c_pages(4), c_size(ap_, "A4 纵向"), c_mode("tile"),
              c_split_tones([0, 64, 128, 192])])

    run_case(args.base, tjob, "GW_TEST", "分割 1x2",
             {"paper": "A4", "split_rows": 1, "split_cols": 2},
             [c_pages(2), c_mode("tile"),
              c_split_tones([64, 128])])

    run_case(args.base, tjob, "GW_TEST", "分割 2x1",
             {"paper": "A4", "split_rows": 2, "split_cols": 1},
             [c_pages(2), c_mode("tile"),
              c_split_tones([32, 160])])

    print("\n########## 第八组：水印与页码 ##########")

    run_case(args.base, bjob, "GW_TEST", "页码 页脚居中",
             {"paper": "A4", "decor": {"pn_enabled": True,
                                       "pn_position": "bottom-center"}},
             [c_mode("raster"), c_ink_zone("bottom", True),
              c_ink_zone("top", False)])

    run_case(args.base, bjob, "GW_TEST", "页码 页眉居右",
             {"paper": "A4", "decor": {"pn_enabled": True,
                                       "pn_position": "top-right"}},
             [c_ink_zone("top", True), c_ink_zone("bottom", False)])

    run_case(args.base, bjob, "GW_TEST", "页码 首页不显示",
             {"paper": "A4", "decor": {"pn_enabled": True,
                                       "pn_skip_first": True}},
             [c_ink_zone("bottom", False)])

    run_case(args.base, bjob, "GW_TEST", "页眉页脚文字",
             {"paper": "A4", "decor": {"hf_header": "内部资料 {page}/{total}",
                                       "hf_footer": "机密文件"}},
             [c_mode("raster"), c_ink_zone("top", True),
              c_ink_zone("bottom", True), c_ink_zone("center", False)])

    # 阈值说明：水印默认色 (185,190,198) 以 0.5 透明度合成到白底后约 220 灰阶，
    # 用默认的 thr=200「暗像素」判据检不出来（220 > 200），必须放宽到 250。
    # 半透明浅色装饰一律用 250，深色页码/页眉页脚用默认值即可。
    run_case(args.base, bjob, "GW_TEST", "水印平铺",
             {"paper": "A4", "decor": {"wm_enabled": True, "wm_text": "机密",
                                       "wm_opacity": 0.5, "wm_tile": True}},
             [c_mode("raster"), c_ink_zone("center", True, 250),
              c_ink_zone("left", True, 250), c_ink_zone("right", True, 250)])

    run_case(args.base, bjob, "GW_TEST", "水印居中且浅",
             {"paper": "A4", "decor": {"wm_enabled": True, "wm_text": "样张",
                                       "wm_tile": False, "wm_opacity": 0.25}},
             [c_ink_zone("center", True, 250)])

    run_case(args.base, bjob, "GW_TEST", "装饰与拼版同时生效",
             {"paper": "A4", "per_sheet": 2,
              "decor": {"pn_enabled": True, "pn_position": "bottom-left"}},
             [c_mode("raster"), c_pages(1), c_ink_zone("bottom", True)])

    print("\n########## 第九组：批量多文件 ##########")

    upb = post_multipart(args.base, "/api/upload", [marker, color])
    check("批量上传 / 返回文件数", upb.get("files") == 2, "实得 %s" % upb.get("files"))
    check("批量上传 / 页数累加", upb.get("pages") == SRC_PAGES + 1,
          "实得 %s（期望 %d）" % (upb.get("pages"), SRC_PAGES + 1))
    check("批量上传 / 文件名提示", "2 个文件" in (upb.get("filename") or ""),
          "实得 %r" % upb.get("filename"))

    run_case(args.base, upb["id"], "GW_TEST", "批量合并后可正常打印",
             {"paper": "A4"},
             [c_pages(SRC_PAGES + 1),
              c_ident_page(1, 1, 1, 1, 0, 0),
              c_ident_page(SRC_PAGES, SRC_PAGES, 1, 1, 0, 0)])

    # ---------------- 第十组：拉伸铺满 ----------------
    print("\n########## 第十组：拉伸铺满 ##########")

    # 无页边距、1 版/张：内容应当非等比拉满整张纸，连最外缘都是黑的
    run_case(args.base, sqjob, "GW_TEST", "拉伸铺满 A4（方形源）",
             {"paper": "A4", "scale_mode": "stretch"},
             [c_mode("raster"),
              c_ink_zone("top", True), c_ink_zone("bottom", True),
              c_ink_zone("left", True), c_ink_zone("right", True),
              c_edge_band(True)])

    # 同一份源、只换缩放模式：上下留白正是「适应纸张」的特征
    run_case(args.base, sqjob, "GW_TEST", "适应纸张对照（不拉伸，上下留白）",
             {"paper": "A4", "scale_mode": "fit"},
             [c_mode("vector"),
              c_ink_zone("top", False), c_ink_zone("bottom", False),
              c_ink_zone("center", True)])

    # 与拼版叠加：2 版/张时纸张转横向、两格左右并排，第 1 页在左格。
    # 左格应当整格贴边（铺满），右格是占位空格、必须完全空白。
    run_case(args.base, sqjob, "GW_TEST", "拉伸 + 2 版/张",
             {"paper": "A4", "scale_mode": "stretch", "per_sheet": 2},
             [c_mode("raster"), c_pages(1),
              c_ink_zone("left", True), c_ink_zone("right", False)])

    # 有页边距时铺满的是「版心」而非纸面，最外缘必须仍是白的
    run_case(args.base, sqjob, "GW_TEST", "拉伸 + 10mm 页边距",
             {"paper": "A4", "scale_mode": "stretch",
              "margins": {"top": 10, "right": 10, "bottom": 10, "left": 10}},
             [c_mode("raster"), c_ink_zone("center", True),
              c_edge_band(False)])

    # ---------------- 汇总 ----------------
    total = len(PASS) + len(FAIL)
    print("\n" + "=" * 62)
    print("端到端验证：%d 项通过 / %d 项失败（共 %d 项）"
          % (len(PASS), len(FAIL), total), flush=True)
    if SKIPPED:
        print("（因 --only=%s 跳过 %d 个用例）" % (ONLY, len(SKIPPED)))
    if FAIL:
        print("失败明细：")
        for f in FAIL:
            print("  - " + f)
    print("=" * 62)

    if not args.keep:
        shutil.rmtree(WORK, ignore_errors=True)
    if args.teardown:
        teardown_test_queue()
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
