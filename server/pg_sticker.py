#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二维码贴纸 —— 把网关的连接码排成一页纸，交给打印面板出纸。

为什么是 PIL 栅格，不是 reportlab 矢量
--------------------------------------
设备上 reportlab 画不了中文：Noto Sans CJK 是 **CFF/PostScript 轮廓**，
而 reportlab 的 `TTFont` 只认 TrueType 的 `glyf` 表，实测直接抛

    TTFError: TTC file ".../NotoSansCJK-Regular.ttc":
              postscript outlines are not supported

而「挑一组中文字体 + Noto 的 ttc 里简体在 index 2（不是 0）」这件事，
`pg_decor` 已经解决过一遍了。这里直接复用 `pg_decor.load_font()`，
少一套逻辑就少一处会跑偏的地方。

栅格化不但不亏，对二维码反而更稳：二维码最经典的死法就是「矢量细线在
低分辨率光栅化时被抹掉 / 边缘被重采样成灰边导致解码失败」。这里按目标
DPI 把模块画成**整数像素方块**，并用 1-bit + CCITT G4 无损存 PDF，
出纸时不会再经过一次有损重采样。

纸张与坐标
----------
坐标一律以**毫米**计算，落到像素时乘 `dpi / 25.4`。页面按 A4 出，
但存 PDF 时用的 resolution 是反算出来的（见 `build_pdf`），
这样 PDF 页面尺寸正好是标准的 595.276 × 841.89 pt，而不是差 0.03mm 的
近似值 —— 差一点点就够打印驱动做一次全局重采样。
"""

from __future__ import annotations

import socket

import gen_qr
import pg_decor

# ---------------------------------------------------------------- 常量

PAGE_W_MM = 210.0                     # A4 竖向
PAGE_H_MM = 297.0
DEFAULT_DPI = 300

MARGIN_MM = 8.0                       # 页面外边距（躲开打印机不可打印区）
PAD_MM = 4.0                          # 每张贴纸的内边距
GAP_MM = 3.0                          # 贴纸之间、元素之间的间距
QR_QUIET = 4                          # 静区模块数（ISO 要求 4）
MARK_MM = 6.0                         # 四角裁切角标长度
MARK_PX = 3                           # 角标线宽（300dpi 下约 0.25mm，再细印不出来）

# 模块边长下限。手机扫 0.5mm 的模块、距离 20cm 以内没问题；再小就该拦住了。
MIN_MODULE_MM = 0.5

# 整码边长下限：低于这个值就宁可留一个孤格、换更能放大码的排布（见 _pick_grid）
MIN_CODE_MM = 30.0

LAYOUTS = (1, 2, 4)                   # 一页印几张同样的贴纸
CODE_KINDS = ("lan", "wan", "app")

# 联数 -> (列, 行)
_GRID = {1: (1, 1), 2: (1, 2), 4: (2, 2)}

DEFAULT_TITLE = "手机扫码 · 自助打印"
GUIDE_LINES = ("① 扫码打开打印页", "② 选文件 → 预览 → 打印")


class StickerError(ValueError):
    pass


class Code:
    """贴纸上的一个二维码。`label` 是粗体标签，`sub` 是下面那行小字。"""

    __slots__ = ("kind", "label", "sub", "url")

    def __init__(self, kind: str, label: str, sub: str, url: str):
        self.kind = kind
        self.label = label
        self.sub = sub
        self.url = url

    def __repr__(self):                                       # pragma: no cover
        return "Code(%s, %s, %s)" % (self.kind, self.label, self.url)


# ---------------------------------------------------------------- 局域网地址

def lan_ip() -> str:
    """
    探测本机的局域网地址。

    用 UDP socket 问路由表（**不会真的发包**，connect 只用来选路由），
    比 `gethostbyname(gethostname())` 可靠 —— 后者在 systemd 起来的环境里
    经常解析到 127.0.1.1 这种回环地址。

    失败返回空串，由调用方决定要不要降级。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        addr = s.getsockname()[0]
    except OSError:
        addr = ""
    finally:
        s.close()
    if addr and not addr.startswith("127."):
        return addr
    # 退一步：拿主机名解析
    try:
        addr = socket.gethostbyname(socket.gethostname())
    except OSError:
        return ""
    return "" if addr.startswith("127.") else addr


# ---------------------------------------------------------------- 绘制

def _paste_qr(d, mat, cx, cy, max_box, quiet, dpi):
    """
    在 (cx, cy) 为中心的 max_box 框里画码，返回实际边长。

    模块边长**取整**是关键：小数边长会让相邻模块的边界落在半个像素上，
    栅格化后一边黑一边灰 —— 解码器在采样点踩到灰边就可能判错。
    """
    n = len(mat)
    unit = int(max_box // (n + 2 * quiet))
    if unit < 1:
        raise StickerError("二维码尺寸不足（%d×%d 模块塞不进 %d 像素）"
                           % (n, n, int(max_box)))
    unit_mm = unit * 25.4 / dpi
    if unit_mm < MIN_MODULE_MM:
        raise StickerError(
            "二维码模块只有 %.2fmm，太小了扫不出来 —— 少选一个码，"
            "或把每页联数改小一点" % unit_mm)
    total = (n + 2 * quiet) * unit
    ox = cx - total / 2.0
    oy = cy - total / 2.0
    # 静区白底：贴纸本来就有底，但显式铺一块更保险（防底色不是纯白）
    d.rectangle([ox, oy, ox + total - 1, oy + total - 1], fill=255)
    for r, row in enumerate(mat):
        c = 0
        while c < n:
            if row[c]:
                start = c
                while c < n and row[c]:
                    c += 1
                # 合并连续模块成一条横带，减少绘图调用次数
                d.rectangle([
                    ox + (quiet + start) * unit,
                    oy + (quiet + r) * unit,
                    ox + (quiet + c) * unit - 1,
                    oy + (quiet + r + 1) * unit - 1,
                ], fill=0)
            else:
                c += 1
    return total


def _crop_marks(d, box, m):
    """四角 L 形裁切角标 —— 剪的时候有个准头。`m` 是臂长（像素）。"""
    x0, y0, x1, y1 = box
    m = min(m, (x1 - x0) / 4.0, (y1 - y0) / 4.0)
    for hx, hy, dx, dy in ((x0, y0, 1, 1), (x1, y0, -1, 1),
                           (x0, y1, 1, -1), (x1, y1, -1, -1)):
        d.line([hx, hy, hx + dx * m, hy], fill=0, width=MARK_PX)
        d.line([hx, hy, hx, hy + dy * m], fill=0, width=MARK_PX)


def build_image(codes, layout=1, ec="M", dpi=DEFAULT_DPI, title=DEFAULT_TITLE,
                marks=None):
    """
    排一页 A4 贴纸，返回 PIL Image（灰度 "L"）。

    `codes` 是 Code 列表 —— 每张贴纸都会含**全部**这些码，`layout`
    只是「同样的一张贴纸在一页上排几张」，方便剪开分贴。

    `marks` 是可选的记录列表：给了就把每个码的
    `(kind, 中心x, 中心y, 边长, 模块数)` 追加进去。这是给测试用的 ——
    有了它，测试才能按网格把画出来的像素回读成矩阵、或者裁出来喂给
    真正的解码器。**没有这个就只能测「没抛异常」，那等于没测。**
    """
    if layout not in LAYOUTS:
        raise StickerError("不支持的版式：%s（可选 %s）"
                           % (layout, "/".join(str(x) for x in LAYOUTS)))
    if not codes:
        raise StickerError("至少要选一个二维码")
    if not pg_decor.font_index():
        raise StickerError("系统里找不到中文字体，无法生成贴纸")

    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise StickerError("设备缺少 PIL，无法生成贴纸") from exc

    def mm(v):
        return v * dpi / 25.4

    cols, rows = _GRID[layout]
    wide = layout <= 2                    # 1/2 联是 A4 整宽，4 联只有半宽
    f_title = pg_decor.load_font(18 if wide else 12, dpi, bold=True)
    f_label = pg_decor.load_font(11 if wide else 9, dpi, bold=True)
    f_sub = pg_decor.load_font(7.5 if wide else 6.5, dpi)
    f_guide = pg_decor.load_font(10 if wide else 8, dpi)

    img = Image.new("L", (int(round(mm(PAGE_W_MM))),
                          int(round(mm(PAGE_H_MM)))), 255)
    d = ImageDraw.Draw(img)

    cell_w = (PAGE_W_MM - 2 * MARGIN_MM) / cols
    cell_h = (PAGE_H_MM - 2 * MARGIN_MM) / rows

    # 先算一遍矩阵，避免每个格子重复编码（一页 4 联就是重复 4 次）
    mats = []
    for code in codes:
        try:
            mats.append(gen_qr.full_matrix(code.url, ec))
        except ValueError as exc:
            raise StickerError("内容无法编码成二维码（%s）：%s"
                               % (code.url, exc)) from exc

    for r in range(rows):
        for c in range(cols):
            box = (mm(MARGIN_MM + c * cell_w), mm(MARGIN_MM + r * cell_h),
                   mm(MARGIN_MM + (c + 1) * cell_w),
                   mm(MARGIN_MM + (r + 1) * cell_h))
            _draw_sticker(d, box, codes, mats, mm, ec, dpi,
                          f_title, f_label, f_sub, f_guide, title, marks)
    return img


def _draw_sticker(d, box, codes, mats, mm, ec, dpi,
                  f_title, f_label, f_sub, f_guide, title, marks=None):
    """画一张贴纸。"""
    x0, y0, x1, y1 = box
    _crop_marks(d, box, mm(MARK_MM))

    cx0, cy0 = x0 + mm(PAD_MM), y0 + mm(PAD_MM)
    cx1, cy1 = x1 - mm(PAD_MM), y1 - mm(PAD_MM)
    cw, ch = cx1 - cx0, cy1 - cy0

    # 标题
    title_h = mm(9.0)
    d.text(((cx0 + cx1) / 2.0, cy0 + title_h / 2.0), title,
           font=f_title, fill=0, anchor="mm")

    # 底部指引（两行）
    line_h = mm(6.0)
    guide_h = line_h * len(GUIDE_LINES)
    gy = cy1 - guide_h
    for i, line in enumerate(GUIDE_LINES):
        d.text(((cx0 + cx1) / 2.0, gy + line_h * (i + 0.5)), line,
               font=f_guide, fill=0, anchor="mm")

    # 码区。上下各留一点 —— 不留的话，最后一格的标签会紧贴底部指引，
    # 4 联那种小贴纸上看着就像粘在一起了。
    ax0, ay0 = cx0, cy0 + title_h + mm(3.0)
    ax1, ay1 = cx1, gy - mm(5.0)
    _draw_codes(d, (ax0, ay0, ax1, ay1), codes, mats, mm, dpi,
                f_label, f_sub, marks)


def _pick_grid(n, aw, ah, gap, lab_h, sub_h, min_size):
    """
    挑一种排布，返回 `(cols, rows, cw, chh, size)`。

    规则分两级，因为「码最大」和「版面好看」会打架：

      1. 先在**满排布**（每行都排满，不留孤格）里挑码最大的。
         n=3 时就是 (3,1) 与 (1,3) 之争 —— A4 整宽时竖排能到 71mm，
         A5 半高只有横排可行（59mm vs 24mm）。
      2. 满排布挑出来的码小得没法扫（< `min_size`）才允许留孤格。
         4 联的 A6 半宽就是这样：(3,1) 只有 27.7mm，而「上二下一」的
         (2,2) 能到 42.7mm —— 宁可空一格也要能扫。
    """
    full, loose = None, None
    for c in range(n, 0, -1):
        r = -(-n // c)                    # ceil
        cw = (aw - (c - 1) * gap) / c
        chh = (ah - (r - 1) * gap) / r
        size = min(cw, chh - lab_h - sub_h)
        if size <= 0:
            continue
        cand = (size, c, r, cw, chh)
        if loose is None or size > loose[0]:
            loose = cand
        if r * c == n and (full is None or size > full[0]):
            full = cand

    pick = full
    if full is None or (full[0] < min_size and loose and loose[0] > full[0]):
        pick = loose or full
    if not pick:
        raise StickerError("贴纸区域放不下这些二维码")
    _, cols, rows, cw, chh = pick
    return cols, rows, cw, chh, pick[0]


def _draw_codes(d, area, codes, mats, mm, dpi, f_label, f_sub, marks=None):
    """在区域里排布 n 个码。"""
    ax0, ay0, ax1, ay1 = area
    aw, ah = ax1 - ax0, ay1 - ay0
    gap = mm(GAP_MM)
    lab_h = mm(5.5)                       # 标签行
    sub_h = mm(4.0)                       # 小字行

    cols, rows, cw, chh, size = _pick_grid(
        len(codes), aw, ah, gap, lab_h, sub_h, mm(MIN_CODE_MM))

    for i, (code, mat) in enumerate(zip(codes, mats)):
        r, c = divmod(i, cols)
        # 末行没排满时把这一行居中 —— 「上二下一」里那个孤零零的码
        # 贴在左下角很难看
        in_row = min(cols, len(codes) - r * cols)
        row_w = in_row * cw + (in_row - 1) * gap
        row_x = ax0 + (aw - row_w) / 2.0
        gx = row_x + c * (cw + gap)
        gy = ay0 + r * (chh + gap)
        blk = size + lab_h + sub_h
        top = gy + (chh - blk) / 2.0      # 整格竖直居中
        cx, cy = gx + cw / 2.0, top + size / 2.0
        total = _paste_qr(d, mat, cx, cy, size, QR_QUIET, dpi)
        if marks is not None:
            marks.append((code.kind, cx, cy, total, len(mat)))
        d.text((gx + cw / 2.0, top + size + lab_h * 0.5), code.label,
               font=f_label, fill=0, anchor="mm")
        d.text((gx + cw / 2.0, top + size + lab_h + sub_h * 0.5), code.sub,
               font=f_sub, fill=0, anchor="mm")


# ---------------------------------------------------------------- 网页预览

def svg_for(url, ec="M", module=8, quiet=2):
    """
    单个码的 SVG —— 管理页里内嵌预览用。

    静区只要 2 个模块：这是屏幕上看的，不是贴在机器上等着被磨损的纸，
    留 4 个模块只会让缩略图显得小一圈。
    """
    if not url:
        raise StickerError("二维码内容为空")
    try:
        mat = gen_qr.full_matrix(url, ec)
    except ValueError as exc:
        raise StickerError("内容无法编码成二维码：%s" % exc) from exc
    return gen_qr.render_svg(mat, module=module, quiet=quiet)


# ---------------------------------------------------------------- 出 PDF

def build_pdf(codes, out_path, layout=1, ec="M", dpi=DEFAULT_DPI,
              title=DEFAULT_TITLE, pages=None):
    """
    生成贴纸 PDF。`codes` 是 Code 列表。

    `pages` 给多页用（同一组码印好几页），不传就 1 页。

    存盘时用 **1-bit + 反算的 resolution**：
      * 1-bit 走 CCITT G4，无损且极小（二维码这种黑白图最合适），
        灰度模式会被 PIL 存成 JPEG，那是有损的；
      * resolution 反算成 `W / (210/25.4)` ≈ 299.96，好让 PDF 页面宽度
        正好是 595.276pt 的标准 A4 —— 用 300 这个整数会得到 595.20，
        差 0.03mm，够驱动做一次全局重采样了。
      * PIL 只收单值 resolution（给 tuple 会在 `im.width * 72.0 / res`
        上抛 `TypeError`），所以高度方向留 0.04mm 的余量，可忽略。
    """
    from PIL import Image

    img = build_image(codes, layout=layout, ec=ec, dpi=dpi, title=title)
    pages = max(1, int(pages or 1))

    # 反算：像素数 / (毫米数 / 25.4) = 每英寸像素数
    res = img.width / (PAGE_W_MM / 25.4)

    bit = img.convert("1", dither=Image.Dither.NONE)
    kwargs = {"resolution": res}
    if pages > 1:
        bit.save(out_path, "PDF", save_all=True,
                 append_images=[bit] * (pages - 1), **kwargs)
    else:
        bit.save(out_path, "PDF", **kwargs)
    return out_path


# ---------------------------------------------------------------- 自测

if __name__ == "__main__":                                    # pragma: no cover
    import sys

    ip = lan_ip() or "192.168.1.100"
    demo = [
        Code("lan", "同一 Wi-Fi", "%s:8080" % ip, "http://%s:8080/" % ip),
        Code("wan", "任意网络", "print.example.com",
             "https://print.example.com:8443/?t=DEMO"),
        Code("app", "装 App", "扫码安装", "http://%s:8080/app" % ip),
    ]
    layout = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    out = "sticker-%d.pdf" % layout
    build_pdf(demo, out, layout=layout)
    print("已生成 %s（%d 联）" % (out, layout))
