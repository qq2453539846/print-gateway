"""
打印拼版引擎 —— 布局计算与 PDF 合成。

设计原则
--------
* 所有几何计算都在「点」(1/72 英寸) 下进行，与 PDF 原生单位一致。
* `build_plan()` 是纯函数：输入规格与源页尺寸，输出每张纸的放置方案，
  不触碰文件系统、不调用外部程序 —— 因此可以完整单元测试。
* `compose()` 只负责按方案把图贴到纸面（reportlab，Flate 无损嵌入）。

被有意避开的两条路
------------------
* CUPS 的 pdftopdf 拼版：实测 booklet 会把内容转 90°，且 `booklet-signature`
  在 cups-filters 1.28.17 上直接 SIGABRT 崩溃 —— 无法支持装订位置与子集。
* 坐标原点：PDF 与 reportlab 都用左下角为原点，本模块统一沿用，
  行号 row=0 表示「最上面一行」，转换只在一处发生，避免上下颠倒。
"""

from __future__ import annotations

import math

# ------------------------------------------------------------------ 纸张
# 单位：点。数值取自 ISO 216 与 ANSI 标准，与 CUPS/gs 一致。
PAPERS: dict[str, tuple[float, float]] = {
    "A3": (841.89, 1190.55),
    "A4": (595.28, 841.89),
    "A5": (419.53, 595.28),
    "B5": (498.90, 708.66),
    "Letter": (612.0, 792.0),
    "Legal": (612.0, 1008.0),
}

MM = 72.0 / 25.4                      # 1 毫米 = 2.8346 点

# 每版页数 -> (列数, 行数, 是否旋转纸面为横向)
# 2 版横排：纸面转成横向，左右各一页，这是通行做法（也符合 WPS/Word 的 2 版）
NUP_GRID: dict[int, tuple[int, int, bool]] = {
    1: (1, 1, False),
    2: (2, 1, True),
    4: (2, 2, False),
    6: (2, 3, False),
    9: (3, 3, False),
    16: (4, 4, False),
}

# 每版排列顺序：把「第 n 版」映射到网格位置
ORDER_NAMES = ("lrtb", "btlr", "rlbt", "tblr")

# 裁剪：从每页四边各切掉多少毫米。上限与页边距保持一致。
CROP_MAX_MM = 50.0
# 分割（海报打印）：一页放大到 rows x cols 张纸。上限压在 4，
# 再大就不是"贴墙上"而是"糊满一面墙"了，且纸数按平方涨。
SPLIT_MAX = 4
# 栅格渲染分辨率上限。拉伸铺满时源页可能远小于纸张（scale 会很大，
# 例如 100pt 见方铺到 A4 是 8.4 倍），不设上限会渲染出一张巨型位图把
# 内存吃光。900 已高于常见激光机 600dpi 的物理分辨率。
MAX_RENDER_DPI = 900


# ------------------------------------------------------------------ 页面范围
def parse_page_range(text: str, total: int) -> list[int]:
    """
    解析页码范围，返回升序去重后的 1-based 页号列表。

    支持 `1-3,5`、`2`、`1-` (到末页) 等形式；非法输入抛 ValueError。
    只接受数字、逗号、连字符与空白 —— 这是安全边界，杜绝把用户输入
    拼进命令行时被解释成选项或注入。
    """
    import re
    text = (text or "").strip()
    if not text:
        return list(range(1, total + 1))

    if not _PAGE_RANGE_OK.match(text):
        raise ValueError("页码范围只能包含数字、逗号、连字符和空格")

    pages: set[int] = set()
    for part in re.sub(r"\s+", "", text).split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            start = int(a) if a else 1
            end = int(b) if b else total
            if start > end:
                start, end = end, start
            for p in range(start, end + 1):
                if 1 <= p <= total:
                    pages.add(p)
        else:
            p = int(part)
            if 1 <= p <= total:
                pages.add(p)
    return sorted(pages)


def _page_range_ok_re():
    r"""页码范围白名单。刻意用显式字符类而非 \s —— \s 会把换行也放进来。"""
    import re
    return re.compile(r"^[0-9,\- 	]+$")


_PAGE_RANGE_OK = _page_range_ok_re()


def apply_page_set(pages: list[int], page_set: str) -> list[int]:
    """按奇偶页筛选。page_set 取 all / odd / even。"""
    if page_set == "odd":
        return [p for p in pages if p % 2 == 1]
    if page_set == "even":
        return [p for p in pages if p % 2 == 0]
    return list(pages)


# ------------------------------------------------------------------ 网格
def parse_crop(spec: dict) -> dict:
    """读取四边裁剪量（毫米）。非法值一律归零，越界夹到上限。"""
    raw = spec.get("crop_mm")
    out = {}
    for k in ("top", "right", "bottom", "left"):
        v = raw.get(k, 0.0) if isinstance(raw, dict) else 0.0
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = 0.0
        if v != v or v in (float("inf"), float("-inf")):      # NaN / inf
            v = 0.0
        out[k] = max(0.0, min(CROP_MAX_MM, v))
    return out


def has_crop(crop: dict) -> bool:
    return any(float(v) > 0.01 for v in crop.values())


def split_grid(spec: dict) -> tuple[int, int]:
    """读取分割行列数，夹到 1..SPLIT_MAX。"""
    def one(key):
        try:
            v = int(str(spec.get(key, 1)).strip())
        except (TypeError, ValueError):
            return 1
        return max(1, min(SPLIT_MAX, v))
    return one("split_rows"), one("split_cols")


def grid_for(per_sheet: int) -> tuple[int, int, bool]:
    """每版页数 -> (列, 行, 纸面横向?)。"""
    if per_sheet not in NUP_GRID:
        raise ValueError("每版页数只支持 %s" % "/".join(str(k) for k in sorted(NUP_GRID)))
    return NUP_GRID[per_sheet]


def fill_order(cols: int, rows: int, order: str) -> list[tuple[int, int]]:
    """
    返回「第 n 版 -> (列, 行)」的填充顺序，row=0 为最上面一行。

    坐标约定（col 从左 0 起，row 从上 0 起）：
      lrtb  从左到右、从上到下（逐行）—— 常规阅读顺序
      btlr  从下到上、从左到右（逐列，自左下起）
      rlbt  从右到左、从上到下
      tblr  从上到下、从左到右（逐列，自左上起）
    """
    if order not in ORDER_NAMES:
        raise ValueError("排列顺序只支持 %s" % "/".join(ORDER_NAMES))
    if order == "lrtb":
        return [(c, r) for r in range(rows) for c in range(cols)]
    if order == "btlr":
        return [(c, rows - 1 - r) for c in range(cols) for r in range(rows)]
    if order == "rlbt":
        return [(cols - 1 - c, r) for r in range(rows) for c in range(cols)]
    return [(c, r) for c in range(cols) for r in range(rows)]        # tblr


# ------------------------------------------------------------------ 小册子
def booklet_sides(total: int, binding: str = "left", subset: str = "both") -> list[dict]:
    """
    小册子拼版：返回每张纸每一面的配对页号。

    total 会向上补齐到 4 的倍数（补空白页）。返回项：
        {"kind": "front"|"back", "left": 页号或 None, "right": 页号或 None,
         "sheet": 第几张纸(1-based)}

    配对规律（P = 补齐后总页数，i 为偶数递增）：
        正面 = (P - i, 1 + i)      背面 = (2 + i, P - 1 - i)
    以 8 页为例：正面 (8,1)、背面 (2,7)、正面 (6,3)、背面 (4,5)。
    """
    if binding not in ("left", "right"):
        raise ValueError("装订位置只支持 left / right")
    if subset not in ("both", "front", "back"):
        raise ValueError("小册子子集只支持 both / front / back")

    padded = int(math.ceil(total / 4.0)) * 4 if total else 0
    sides: list[dict] = []
    i = 0
    sheet_no = 1
    while i < padded // 2:
        front = (padded - i, i + 1)
        back = (i + 2, padded - i - 1)
        if binding == "right":                       # 右装订：左右互换
            front = (front[1], front[0])
            back = (back[1], back[0])
        for kind, pair in (("front", front), ("back", back)):
            sides.append({
                "sheet": sheet_no,
                "kind": kind,
                "left": _valid_page(pair[0], total),
                "right": _valid_page(pair[1], total),
            })
        i += 2
        sheet_no += 1

    if subset == "front":
        sides = [s for s in sides if s["kind"] == "front"]
    elif subset == "back":
        sides = [s for s in sides if s["kind"] == "back"]
    return sides


def _valid_page(p: int, total: int):
    """超出真实页数的补位返回 None（空白页）。"""
    return p if 1 <= p <= total else None


# ------------------------------------------------------------------ 放置方案
def _inner_box(sheet_w: float, sheet_h: float, margins_mm: dict) -> tuple[float, float, float, float]:
    """按四边页边距算出可打印区域 (x, y, w, h)，单位点，左下原点。"""
    left = margins_mm.get("left", 0.0) * MM
    right = margins_mm.get("right", 0.0) * MM
    top = margins_mm.get("top", 0.0) * MM
    bottom = margins_mm.get("bottom", 0.0) * MM
    x = left
    y = bottom
    w = sheet_w - left - right
    h = sheet_h - top - bottom
    if w <= 1 or h <= 1:
        raise ValueError("页边距过大，纸张已无可打印区域")
    return x, y, w, h


def _fit(page_w: float, page_h: float, cell_w: float, cell_h: float,
         shrink_only: bool = True) -> float:
    """等比缩放比例，默认只缩小不放大（避免把小页拉糊）。"""
    sx = cell_w / page_w
    sy = cell_h / page_h
    s = min(sx, sy)
    return min(s, 1.0) if shrink_only else s


def build_plan(spec: dict, page_sizes: list[tuple[float, float]], page_count: int) -> dict:
    """
    生成整份作业的放置方案。

    spec 关键字段
        paper            目标纸张名（PAPERS 的键）
        orientation      auto / portrait / landscape
        per_sheet        每版页数
        layout_order     排列顺序
        booklet          是否小册子
        booklet_binding  小册子装订位置
        booklet_subset   小册子子集
        margins          四边页边距（毫米）
        crop_mm          四边裁剪量（毫米）
        stretch          是否拉伸铺满（非等比拉满版心，忽略源页宽高比）
        split_rows/cols  分割打印的行列数（1 表示不分割）
        border           是否画版边框
        mirror           是否镜像（反片）

    返回
        {
          "paper": 纸张名, "sheet_w": 宽, "sheet_h": 高,
          "margins": {...}, "border": bool, "crop": {...}, "split": [r, c],
          "sheets": [ {"faces": [ {"kind":..., "placements":[ ... ]} ]} ],
          "need_raster": bool,      # 是否需要走栅格拼版
          "pages_used": int,
        }

    placements 每项：
        {"page": 源页号或 None, "key": 渲染单元键, "src": [x, y, w, h] 源区域,
         "x", "y", "w", "h": 放置矩形(点，左下原点), "rotate": 0/90/180/270}

    src 是**未裁剪页**坐标系里的矩形（点，左下原点）。裁剪、分割都只改这个
    矩形，下游渲染与合成完全不用关心二者的差别。
    """
    paper = spec.get("paper", "A4")
    if paper not in PAPERS:
        raise ValueError("不支持的纸张：%s" % paper)

    margins = spec.get("margins") or {}
    booklet = bool(spec.get("booklet"))
    per_sheet = int(spec.get("per_sheet", 1))
    order = spec.get("layout_order", "lrtb")
    border = bool(spec.get("border"))
    mirror = bool(spec.get("mirror"))
    auto_center = bool(spec.get("auto_center", True))
    auto_rotate = bool(spec.get("auto_rotate", False))
    stretch = bool(spec.get("stretch"))

    crop = parse_crop(spec)
    srows, scols = split_grid(spec)
    splitting = srows * scols > 1
    if splitting:
        # 分割打印：每个瓦片独占一张纸，因此与拼版/小册子天然互斥。
        # 这里直接忽略而不是报错 —— 界面上已禁用，API 直调时也不必卡死。
        booklet, per_sheet, order = False, 1, "lrtb"

    if booklet:
        per_sheet = 2
        order = "lrtb"

    cols, rows, rotate_sheet = grid_for(per_sheet)
    base_w, base_h = PAPERS[paper]

    orientation = spec.get("orientation", "auto")
    if booklet:
        rotate_sheet = True                                  # 小册子固定横向对折
    if orientation == "landscape":
        rotate_sheet = True
    elif orientation == "portrait":
        rotate_sheet = False
    # auto：沿用上述由版数/小册子推导的方向

    sheet_w, sheet_h = (base_h, base_w) if rotate_sheet else (base_w, base_h)

    ix, iy, iw, ih = _inner_box(sheet_w, sheet_h, margins)
    cell_w = iw / cols
    cell_h = ih / rows
    if cell_w <= 1 or cell_h <= 1:
        raise ValueError("版数过多，单版尺寸过小")

    # 裁剪后的内容才是「页面」，后续缩放/fit/分割都以它为准
    crop_boxes: list[tuple[float, float, float, float]] = []
    for i in range(max(1, page_count)):
        pw, ph = page_sizes[i] if i < len(page_sizes) else page_sizes[-1]
        cw = pw - (crop["left"] + crop["right"]) * MM
        ch = ph - (crop["top"] + crop["bottom"]) * MM
        if cw <= 6 or ch <= 6:
            raise ValueError(
                "裁剪过多（上下共 %gmm、左右共 %gmm），页面已无有效区域"
                % (crop["top"] + crop["bottom"], crop["left"] + crop["right"]))
        crop_boxes.append((crop["left"] * MM, crop["bottom"] * MM, cw, ch))

    # 镜像、裁剪、分割、拉伸都必须走栅格：
    #   * 镜像 —— gs 的三条页面钩子在 pdfwrite 上对多页文档实测全失效（见 pitfall 10），
    #            由本模块的合成器翻转，行为确定且可测试；
    #   * 裁剪 —— 要按像素取块渲染，本来就得栅格化；
    #   * 分割 —— 瓦片需非等比铺满纸格，PDF 内容流层面做不到；
    #   * 拉伸 —— 两个方向各自拉满版心（非等比），gs 的 -dPDFFitPage 只会
    #            等比缩放，所以同样只能落到栅格层。
    need_raster = bool(per_sheet > 1 or booklet or border
                       or mirror or _has_margin(margins)
                       or splitting or has_crop(crop) or stretch)

    def placement(page_no, col, row, extra_rotate=0, tile=None):
        """
        把一个源页（或它的一个瓦片）放进指定网格位置。

        page_no 为 None 表示空白格；tile 为 (r, c) 时只放该瓦片且必须铺满整格
        （r=0 是最上面一行）。
        """
        cx = ix + col * cell_w
        cy = iy + (rows - 1 - row) * cell_h            # row=0 在最上面
        if page_no is None:
            return {"page": None, "key": None, "src": None,
                    "x": cx, "y": cy, "w": cell_w, "h": cell_h,
                    "rotate": 0, "cell": [col, row]}

        bx, by, cw, ch = crop_boxes[page_no - 1]

        if tile is not None:
            # 分割：瓦片严格铺满纸格，靠合成阶段的非等比拉伸实现。
            # 等比缩放会让瓦片比例与纸面不符，拼起来就有错位白边。
            r, c = tile
            tw, th = cw / scols, ch / srows
            sx = bx + c * tw
            sy = by + (srows - 1 - r) * th
            return {"page": page_no, "key": "%d@%d,%d" % (page_no, r, c),
                    "src": [sx, sy, tw, th],
                    "x": cx, "y": cy, "w": cell_w, "h": cell_h,
                    "rotate": 0, "cell": [col, row]}

        if stretch:
            # 拉伸铺满：两个方向各自拉满版心，允许非等比变形 —— 这正是它
            # 与「适应纸张」的差别。自动旋转在这里没有意义：两个方向反正
            # 都会被拉满，变形量一样，强行转 90° 只会把内容放倒。
            return {"page": page_no, "key": str(page_no),
                    "src": [bx, by, cw, ch], "cell": [col, row],
                    "rotate": extra_rotate % 360,
                    "x": cx, "y": cy, "w": cell_w, "h": cell_h}

        pw2, ph2 = cw, ch
        rot = extra_rotate % 360
        if auto_rotate and rot == 0:
            # 横竖自适应：比较两种朝向的可用比例，选占位更大的那个。
            # 用不放大版的 _fit 做比较，否则两者都会被夹到 1.0 而无法区分。
            if _fit(ch, cw, cell_w, cell_h, shrink_only=False) > \
                    _fit(cw, ch, cell_w, cell_h, shrink_only=False):
                rot = 90
        if rot in (90, 270):
            pw2, ph2 = ph2, pw2
        s = _fit(pw2, ph2, cell_w, cell_h)
        w, h = pw2 * s, ph2 * s
        if auto_center:
            x = cx + (cell_w - w) / 2.0
            y = cy + (cell_h - h) / 2.0
        else:                                          # 不居中则贴单元格左上
            x = cx
            y = cy + (cell_h - h)
        return {"page": page_no, "key": str(page_no), "src": [bx, by, cw, ch],
                "cell": [col, row], "rotate": rot, "x": x, "y": y, "w": w, "h": h}

    faces: list[dict] = []

    if splitting:
        # 一张瓦片一张纸。顺序先页、再行、后列 —— 与纸堆叠顺序一致，
        # 拼的时候从左上到右下按纸序贴回去即可。
        for p in range(1, page_count + 1):
            for r in range(srows):
                for c in range(scols):
                    faces.append({"kind": "tile", "sheet": len(faces) + 1,
                                  "placements": [placement(p, 0, 0, tile=(r, c))]})
    elif booklet:
        for side in booklet_sides(page_count, spec.get("booklet_binding", "left"),
                                 spec.get("booklet_subset", "both")):
            faces.append({
                "kind": side["kind"],
                "sheet": side["sheet"],
                "placements": [
                    placement(side["left"], 0, 0),
                    placement(side["right"], 1, 0),
                ],
            })
    else:
        slots = fill_order(cols, rows, order)
        # 每张纸一「面」，一张纸装 per_sheet 页
        for start in range(0, page_count, per_sheet):
            group = list(range(start + 1, min(start + per_sheet, page_count) + 1))
            placements = []
            for idx, (col, row) in enumerate(slots):
                p = group[idx] if idx < len(group) else None
                placements.append(placement(p, col, row))
            faces.append({"kind": "single", "sheet": len(faces) + 1,
                          "placements": placements})

    if not faces:                                            # 空文档兜底
        faces.append({"kind": "single", "sheet": 1, "placements": [placement(None, 0, 0)]})

    return {
        "paper": paper,
        "sheet_w": sheet_w,
        "sheet_h": sheet_h,
        "margins": margins,
        "border": border,
        "mirror": mirror,
        "crop": crop,
        "split": [srows, scols],
        "cols": cols,
        "rows": rows,
        "cell_w": cell_w,
        "cell_h": cell_h,
        "faces": faces,
        "need_raster": need_raster,
        "pages_used": page_count,
        "_page_sizes": list(page_sizes),
        "_crop_boxes": crop_boxes,
    }


def _has_margin(margins: dict) -> bool:
    return any(float(margins.get(k, 0) or 0) > 0.01 for k in ("top", "right", "bottom", "left"))


def render_jobs(plan: dict, target_dpi: int, min_dpi: int = 100) -> list[dict]:
    """
    算出每个「渲染单元」该怎么渲染。

    一个渲染单元 = 某个 placement 的源区域。不拼版时它就是整页；分割打印时
    它是其中一个瓦片。**逐块渲染是这里的关键设计**：A4@300dpi 的 3x3 分割若
    先渲染整页大图再切，那张图是 7441x10524 = 78MP（约 235MB），这台 988MB
    内存的板子会直接趴下；逐块渲染时内存占用与分割数无关。pdftoppm 的
    -x/-y/-W/-H 正好能按像素取块。

    分辨率反算：源页渲染成像素后按 scale 贴到纸上，纸上等效分辨率 = dpi/scale。
    要让纸上等效分辨率为 target_dpi，就需要 dpi = target_dpi * scale。这正是
    拼版能省时间的来源 —— 4 版时每页只需约 1/2 分辨率。

    返回 [{"key","page","dpi","region","full","offset"}]，都是像素、左上原点：
        region  本次该渲染源页的哪一块
        full    该页「裁剪后整页」的像素尺寸，装饰按这个坐标系定位
        offset  本块在 full 中的偏移，装饰据此落到正确位置
    """
    page_sizes = plan["_page_sizes"]
    crop_boxes = plan["_crop_boxes"]
    jobs: dict[str, dict] = {}
    order: list[str] = []

    for face in plan["faces"]:
        for pl in face["placements"]:
            key = pl.get("key")
            if not key or key in jobs:
                continue
            p = pl["page"]
            sx, sy, sw, sh = pl["src"]
            # 非等比（分割打印、拉伸铺满）时取较大的一侧，宁多勿糊
            scale = max(pl["w"] / sw, pl["h"] / sh)
            dpi = min(MAX_RENDER_DPI,
                      max(min_dpi, int(round(target_dpi * scale))))

            page_w, page_h = page_sizes[p - 1]
            bx, by, bw, bh = crop_boxes[p - 1]
            pp = dpi / 72.0                                 # 每点多少像素

            region = (max(0, int(round(sx * pp))),
                      max(0, int(round((page_h - sy - sh) * pp))),
                      max(1, int(round(sw * pp))),
                      max(1, int(round(sh * pp))))
            full = (max(1, int(round(bw * pp))),
                    max(1, int(round(bh * pp))))
            # 本块顶边到裁剪框顶边的距离（PDF 是左下原点，图片是左上原点）
            offset = (int(round((sx - bx) * pp)),
                      int(round((by + bh - sy - sh) * pp)))

            jobs[key] = {"key": key, "page": p, "dpi": dpi, "region": region,
                         "full": full, "offset": offset}
            order.append(key)

    return [jobs[k] for k in order]


# ------------------------------------------------------------------ 合成
def compose(plan: dict, images: dict[int, str], out_path: str,
            image_sizes: dict[int, tuple[int, int]] | None = None) -> None:
    """
    按方案把图片贴到每一面，输出多页 PDF。

    images      {源页号: png 路径}
    image_sizes {源页号: (像素宽, 像素高)}，用于修正贴图比例（旋转时用）

    reportlab 对 PNG 使用 Flate 无损嵌入，不做 JPEG 有损压缩 ——
    文字边缘不会出现振铃，这是刻意选择的。
    """
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    plan = dict(plan)
    mirror = bool(plan.get("mirror"))

    c = canvas.Canvas(out_path, pagesize=(plan["sheet_w"], plan["sheet_h"]),
                      pageCompression=1)
    c.setTitle("Print Gateway Job")

    for face in plan["faces"]:
        for pl in face["placements"]:
            key = pl.get("key")
            if not key or key not in images:
                if plan["border"]:
                    _draw_border(c, pl)
                continue
            img = ImageReader(images[key])
            c.saveState()
            if mirror:
                # 以该版心的竖直中线为轴翻转，即 WPS 的「反片」。
                # 放在变换里而非翻转图片本身，旋转版心也能一并正确处理。
                mid = pl["x"] + pl["w"] / 2.0
                c.translate(mid, 0)
                c.scale(-1, 1)
                c.translate(-mid, 0)
            if pl["rotate"] == 0:
                c.drawImage(img, pl["x"], pl["y"], width=pl["w"], height=pl["h"],
                            preserveAspectRatio=False, mask=None)
            else:
                _draw_rotated(c, img, pl)
            if plan["border"]:
                _draw_border(c, pl)
            c.restoreState()
        c.showPage()
    c.save()


def _draw_rotated(c, img, pl: dict) -> None:
    """把图片旋转后居中放进放置框。"""
    rot = pl["rotate"]
    c.saveState()
    cx = pl["x"] + pl["w"] / 2.0
    cy = pl["y"] + pl["h"] / 2.0
    c.translate(cx, cy)
    c.rotate(rot)
    if rot in (90, 270):
        c.drawImage(img, -pl["h"] / 2.0, -pl["w"] / 2.0, width=pl["h"], height=pl["w"],
                    preserveAspectRatio=False, mask=None)
    else:
        c.drawImage(img, -pl["w"] / 2.0, -pl["h"] / 2.0, width=pl["w"], height=pl["h"],
                    preserveAspectRatio=False, mask=None)
    c.restoreState()


def _draw_border(c, pl: dict) -> None:
    """版边框：画在网格单元格上，而不是内容外框 —— 与 WPS 的「版边框」一致。"""
    c.saveState()
    c.setLineWidth(0.5)
    c.setStrokeColorRGB(0.6, 0.6, 0.6)
    c.rect(pl["x"], pl["y"], pl["w"], pl["h"], stroke=1, fill=0)
    c.restoreState()


# ------------------------------------------------------------------ 自检
def _selftest() -> int:
    fails = 0

    def check(name, got, want):
        nonlocal fails
        ok = got == want
        if not ok:
            fails += 1
        print("  %-46s %s" % (name, "ok" if ok else "FAIL  得到 %r 期望 %r" % (got, want)))

    check("parse 1-3,5 (共8页)", parse_page_range("1-3,5", 8), [1, 2, 3, 5])
    check("parse 空 -> 全部", parse_page_range("", 3), [1, 2, 3])
    check("parse 4- (到末页)", parse_page_range("4-", 6), [4, 5, 6])
    check("parse 越界被裁剪", parse_page_range("7-99", 8), [7, 8])
    check("parse 反序 5-3", parse_page_range("5-3", 6), [3, 4, 5])
    check("parse 非法字符拒绝", _expect_raises(lambda: parse_page_range("1;rm -rf /", 8)), "ValueError")
    check("parse 注入拒绝", _expect_raises(lambda: parse_page_range("$(whoami)", 8)), "ValueError")
    check("parse 拒绝换行", _expect_raises(
        lambda: parse_page_range("1" + chr(10) + "2", 8)), "ValueError")
    # 空白是「可忽略字符」，不是分隔符 —— 逗号才是分隔符。
    check("逗号前后的空白被容忍", parse_page_range("1, " + chr(9) + "3, 5", 8), [1, 3, 5])

    check("奇数页", apply_page_set([1, 2, 3, 4], "odd"), [1, 3])
    check("偶数页", apply_page_set([1, 2, 3, 4], "even"), [2, 4])

    check("lrtb 2x2", fill_order(2, 2, "lrtb"), [(0, 0), (1, 0), (0, 1), (1, 1)])
    check("btlr 2x2", fill_order(2, 2, "btlr"), [(0, 1), (0, 0), (1, 1), (1, 0)])
    check("rlbt 2x2", fill_order(2, 2, "rlbt"), [(1, 0), (0, 0), (1, 1), (0, 1)])

    bs = booklet_sides(8)
    check("小册子 8 页面数", len(bs), 4)
    check("小册子第1面", (bs[0]["left"], bs[0]["right"], bs[0]["kind"]), (8, 1, "front"))
    check("小册子第2面", (bs[1]["left"], bs[1]["right"], bs[1]["kind"]), (2, 7, "back"))
    check("小册子第3面", (bs[2]["left"], bs[2]["right"]), (6, 3))
    check("小册子第4面", (bs[3]["left"], bs[3]["right"]), (4, 5))
    check("小册子右装订首面", (booklet_sides(8, "right")[0]["left"],
                              booklet_sides(8, "right")[0]["right"]), (1, 8))
    check("小册子仅正面", len(booklet_sides(8, subset="front")), 2)
    check("小册子仅背面", len(booklet_sides(8, subset="back")), 2)
    check("小册子 5 页补到 8", len(booklet_sides(5)), 4)
    check("小册子 5 页补齐后页号集合", sorted(
        p for s in booklet_sides(5) for p in (s["left"], s["right"]) if p), [1, 2, 3, 4, 5])
    check("小册子 5 页首面空白位", (booklet_sides(5)[0]["left"],
                              booklet_sides(5)[0]["right"]), (None, 1))

    # 1-up A4 不需要栅格
    p1 = build_plan({"paper": "A4", "per_sheet": 1}, [(595.28, 841.89)] * 4, 4)
    check("1-up 不需栅格", p1["need_raster"], False)
    check("1-up 纸面尺寸", (round(p1["sheet_w"], 2), round(p1["sheet_h"], 2)), (595.28, 841.89))
    check("1-up 面数", len(p1["faces"]), 4)

    # 2-up 应为横向 A4，两版并排
    p2 = build_plan({"paper": "A4", "per_sheet": 2}, [(595.28, 841.89)] * 4, 4)
    check("2-up 转横向", (round(p2["sheet_w"], 2), round(p2["sheet_h"], 2)), (841.89, 595.28))
    check("2-up 面数", len(p2["faces"]), 2)
    check("2-up 第一面两版", [x["page"] for x in p2["faces"][0]["placements"]], [1, 2])
    check("2-up 需栅格", p2["need_raster"], True)
    # 左右两版 x 不同，y 相同
    a, b = p2["faces"][0]["placements"]
    check("2-up 左右并排", (a["y"] == b["y"] and b["x"] > a["x"]), True)

    # 4-up 版数排布
    p4 = build_plan({"paper": "A4", "per_sheet": 4}, [(595.28, 841.89)] * 8, 8)
    check("4-up 面数", len(p4["faces"]), 2)
    check("4-up 第四版在右下", p4["faces"][0]["placements"][3]["cell"], [1, 1])
    check("4-up 第一版在左上", p4["faces"][0]["placements"][0]["cell"], [0, 0])

    # 4-up 渲染分辨率应显著低于目标（省时）
    check("4-up 单页渲染 dpi < 200", render_jobs(p4, 300)[0]["dpi"] < 200, True)
    check("1-up 渲染 dpi 命中目标", render_jobs(p1, 300)[0]["dpi"], 300)

    # ---- 页面装饰的落点信息（src / key）-------------------------------------
    check("未裁剪时 src 就是整页", p1["faces"][0]["placements"][0]["src"],
          [0.0, 0.0, 595.28, 841.89])
    check("未裁剪不触发栅格", p1["need_raster"], False)
    check("渲染单元键即页号", render_jobs(p1, 300)[0]["key"], "1")

    # ---- 裁剪 --------------------------------------------------------------
    pc = build_plan({"paper": "A4",
                     "crop_mm": {"top": 10, "bottom": 10, "left": 5, "right": 5}},
                    [(595.28, 841.89)] * 2, 2)
    pl0 = pc["faces"][0]["placements"][0]
    check("裁剪触发栅格", pc["need_raster"], True)
    check("裁剪后源区宽", round(pl0["src"][2], 2), round(595.28 - 10 * MM, 2))
    check("裁剪后源区高", round(pl0["src"][3], 2), round(841.89 - 20 * MM, 2))
    check("裁剪后源区左下角", (round(pl0["src"][0], 2), round(pl0["src"][1], 2)),
          (round(5 * MM, 2), round(10 * MM, 2)))
    check("裁剪后仍等比", round(pl0["w"] / pl0["h"], 3),
          round(pl0["src"][2] / pl0["src"][3], 3))
    jc = render_jobs(pc, 300)[0]
    check("裁剪后渲染区域不含被切掉的部分",
          jc["region"][0] > 0 and jc["region"][1] > 0, True)
    check("裁剪后装饰参考系 = 裁剪后页面",
          (jc["full"][0] < 2480, jc["full"][1] < 3508), (True, True))

    # 用一个「超扁页面」来触发：A4 有 297mm 高，四边最多各裁 50mm 也裁不到空，
    # 所以这里直接喂一个 35mm 高的页面尺寸
    check("裁剪过多要报错", _expect_raises(lambda: build_plan(
        {"paper": "A4", "crop_mm": {"top": 50, "bottom": 50}},
        [(595.28, 100.0)], 1)), "ValueError")
    check("裁剪量非法归零", parse_crop({"crop_mm": {"top": "abc", "left": -5}}),
          {"top": 0.0, "right": 0.0, "bottom": 0.0, "left": 0.0})
    check("裁剪量越界夹上限", parse_crop({"crop_mm": {"top": 999}})["top"], CROP_MAX_MM)

    # ---- 分割（海报打印）---------------------------------------------------
    ps = build_plan({"paper": "A4", "split_rows": 2, "split_cols": 2},
                    [(595.28, 841.89)] * 2, 2)
    check("分割面数 = 页数 x 瓦片数", len(ps["faces"]), 8)
    check("分割需要栅格", ps["need_raster"], True)
    check("分割忽略每版页数", ps["cols"] * ps["rows"], 1)
    check("分割渲染单元键", [f["placements"][0]["key"] for f in ps["faces"]][:4],
          ["1@0,0", "1@0,1", "1@1,0", "1@1,1"])
    # 瓦片必须铺满纸格（非等比），否则拼起来会有错位白边
    check("分割瓦片铺满纸格",
          all(abs(f["placements"][0]["w"] - ps["cell_w"]) < 1e-9 and
              abs(f["placements"][0]["h"] - ps["cell_h"]) < 1e-9
              for f in ps["faces"]), True)
    tl = ps["faces"][0]["placements"][0]["src"]
    check("左上瓦片取页面上半部", tl[1] + tl[3] / 2 > 841.89 / 2, True)
    check("左上瓦片取页面左半部", tl[0] + tl[2] / 2 < 595.28 / 2, True)

    js = render_jobs(ps, 300)
    check("分割渲染任务数", len(js), 8)
    # 每块渲染出的图 = 一张纸在目标 dpi 下的像素量，**与分割数无关** ——
    # 这正是逐块渲染能顶住内存的原因。
    check("每块渲染 = 一张纸的像素", js[0]["region"][2:], (2480, 3508))
    check("装饰参考系 = 整页铺到全部纸上", js[0]["full"], (4961, 7016))
    check("右上瓦片横向偏移", js[1]["offset"][0], 2480)
    check("左下瓦片纵向偏移", js[2]["offset"][1], 3508)
    big = js[0]["full"][0] * js[0]["full"][1]
    small = js[0]["region"][2] * js[0]["region"][3]
    check("逐块渲染的像素量是大图方案的约 1/4", 0.24 < small / big < 0.26, True)

    # 3x3 时单块像素不变 —— 内存与分割数解耦
    ps3 = build_plan({"paper": "A4", "split_rows": 3, "split_cols": 3},
                     [(595.28, 841.89)], 1)
    js3 = render_jobs(ps3, 300)
    check("3x3 任务数", len(js3), 9)
    check("3x3 每块仍是一张纸的像素", js3[0]["region"][2:], (2480, 3508))
    check("分割行列越界被夹住", split_grid({"split_rows": 99, "split_cols": 0}),
          (SPLIT_MAX, 1))

    # ---- 裁剪与分割可以叠加 ------------------------------------------------
    pcs = build_plan({"paper": "A4", "split_rows": 2, "split_cols": 1,
                      "crop_mm": {"top": 5}},
                     [(595.28, 841.89)], 1)
    jcs = render_jobs(pcs, 300)[0]
    check("裁剪+分割：源区高度已含裁剪",
          round(pcs["faces"][0]["placements"][0]["src"][3], 2),
          round((841.89 - 5 * MM) / 2, 2))

    # 小册子：横向 A4，两面
    pb = build_plan({"paper": "A4", "booklet": True}, [(595.28, 841.89)] * 8, 8)
    check("小册子纸面横向", (round(pb["sheet_w"], 2), round(pb["sheet_h"], 2)), (841.89, 595.28))
    check("小册子面数", len(pb["faces"]), 4)
    check("小册子首面页序", [x["page"] for x in pb["faces"][0]["placements"]], [8, 1])
    check("小册子渲染 dpi 落在 200~300（缩放 1/√2）",
          200 < render_jobs(pb, 300)[0]["dpi"] < 300, True)

    # 页边距
    pm = build_plan({"paper": "A4", "per_sheet": 1, "margins": {"left": 10, "right": 10,
                                                              "top": 10, "bottom": 10}},
                    [(595.28, 841.89)], 1)
    pl = pm["faces"][0]["placements"][0]
    check("页边距左边距 10mm", round(pl["x"], 2), round(10 * MM, 2))
    check("页边距需栅格", pm["need_raster"], True)

    # 版边框
    pbd = build_plan({"paper": "A4", "per_sheet": 4, "border": True}, [(595.28, 841.89)] * 4, 4)
    check("版边框需栅格", pbd["need_raster"], True)

    # 拉伸铺满
    pst = build_plan({"paper": "A4", "stretch": True}, [(400.0, 400.0)], 1)
    check("拉伸需栅格", pst["need_raster"], True)
    pst_pl = pst["faces"][0]["placements"][0]
    check("拉伸铺满纸张宽", round(pst_pl["w"], 2), round(pst["sheet_w"], 2))
    check("拉伸铺满纸张高", round(pst_pl["h"], 2), round(pst["sheet_h"], 2))
    check("拉伸时源区域是整页", pst_pl["src"], [0.0, 0.0, 400.0, 400.0])
    # 对照：用大于纸张的方形源（700x700），等比才会真正缩放。此时等比版
    # 受短边限制宽度正好等于纸宽、高度停在 595（上下留白）；拉伸版则高度
    # 也拉满到 842。两者宽度相同、高度不同 —— 这就是两个模式的分界。
    pfit = build_plan({"paper": "A4"}, [(700.0, 700.0)], 1)
    pfit_pl = pfit["faces"][0]["placements"][0]
    pst_big = build_plan({"paper": "A4", "stretch": True}, [(700.0, 700.0)], 1)
    pst_big_pl = pst_big["faces"][0]["placements"][0]
    check("等比版宽受短边限制=纸宽",
          round(pfit_pl["w"], 2), round(pfit["sheet_w"], 2))
    check("等比版高小于纸高（留白）", pfit_pl["h"] < pfit["sheet_h"] - 200, True)
    check("两模式宽度相同", round(pfit_pl["w"], 2), round(pst_big_pl["w"], 2))
    check("拉伸版高 = 纸高", round(pst_big_pl["h"], 2), round(pst_big["sheet_h"], 2))
    # 拉伸不做自动旋转（旋转不改变变形量，只会放倒内容）
    prot = build_plan({"paper": "A4", "stretch": True, "auto_rotate": True},
                      [(841.89, 595.28)], 1)
    check("拉伸不自动旋转", prot["faces"][0]["placements"][0]["rotate"], 0)
    # 页边距内的拉伸
    pmg = build_plan({"paper": "A4", "stretch": True,
                      "margins": {"top": 10, "right": 10, "bottom": 10, "left": 10}},
                     [(400.0, 400.0)], 1)
    pmg_pl = pmg["faces"][0]["placements"][0]
    check("拉伸版心小于纸面", pmg_pl["w"] < pmg["sheet_w"], True)
    check("拉伸版心大于内容", pmg_pl["w"] > 400.0, True)
    # 分辨率上限
    pcap = build_plan({"paper": "A4", "stretch": True}, [(100.0, 100.0)], 1)
    check("拉伸放大时 dpi 受上限保护",
          render_jobs(pcap, 300)[0]["dpi"], MAX_RENDER_DPI)

    # 自动旋转：横向源页放进纵向单元格时应转 90°
    pr = build_plan({"paper": "A4", "per_sheet": 1, "auto_rotate": True},
                    [(841.89, 595.28)], 1)
    check("自动旋转把横向页转正", pr["faces"][0]["placements"][0]["rotate"], 90)
    pr0 = build_plan({"paper": "A4", "per_sheet": 1, "auto_rotate": False},
                     [(841.89, 595.28)], 1)
    check("关闭自动旋转则不转", pr0["faces"][0]["placements"][0]["rotate"], 0)

    # 自动居中开关
    pc_on = build_plan({"paper": "A4", "per_sheet": 2, "auto_center": True},
                       [(595.28, 841.89)] * 2, 2)
    pc_off = build_plan({"paper": "A4", "per_sheet": 2, "auto_center": False},
                        [(595.28, 841.89)] * 2, 2)
    a_on = pc_on["faces"][0]["placements"][0]
    a_off = pc_off["faces"][0]["placements"][0]
    check("自动居中 x 偏移更大", a_on["x"] > a_off["x"], True)
    check("不居中时 x 贴单元格左边", round(a_off["x"], 2), round(a_off["cell"][0] * 0, 2))

    # 页边距过大应报错
    check("页边距过大拒绝", _expect_raises(lambda: build_plan(
        {"paper": "A5", "margins": {"left": 200, "right": 200}}, [(419.53, 595.28)], 1)), "ValueError")

    # 非法纸张
    check("非法纸张拒绝", _expect_raises(lambda: build_plan({"paper": "A0"}, [(1, 1)], 1)), "ValueError")
    check("非法版数拒绝", _expect_raises(lambda: build_plan({"paper": "A4", "per_sheet": 3},
                                                          [(595.28, 841.89)], 1)), "ValueError")

    print()
    print("  失败 %d 项" % fails)
    return fails


def _expect_raises(fn) -> str:
    try:
        fn()
    except ValueError:
        return "ValueError"
    except Exception as exc:                                  # noqa: BLE001
        return type(exc).__name__
    return "未抛异常"


if __name__ == "__main__":
    import sys
    print("pg_layout 自检")
    sys.exit(1 if _selftest() else 0)
