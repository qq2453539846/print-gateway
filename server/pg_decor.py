"""
页面装饰 —— 水印 / 页码 / 页眉页脚（Pillow 实现）。

为什么放在栅格层
----------------
装饰是「每一页」的，而拼版是「纸张级」的。想在矢量层叠加就得改 PDF 内容流，
但两条常规路子都走不通：

  * ghostscript 的每页钩子（`/BeginPage`、`/Install`、`/MirrorPrint`）在 pdfwrite
    上处理多页文档时会被逐页重置 —— 实测见 pitfalls 第 10 条，不可依赖；
  * pypdf / pikepdf 这类能做矢量叠加的库，目标设备（armhf, Debian）没有装，
    而设备到外网的带宽只有约 50 kB/s，为一个小功能拉依赖不划算。

所以装饰统一在「源页已栅格化、尚未拼版」时用 Pillow 画上去：确定性好、
可测试、不引入新依赖，而且喷墨/激光驱动本来也要把整页栅格化一遍。

代价：启用装饰的作业会从「矢量直出」降为「栅格」，等效分辨率仍是作业设定的
300 dpi，所以文字依旧锐利；这一点会在界面上明确提示。

坐标系约定
----------
所有位置参数都以「**纸上等效点**」为单位计算，再换算成像素：

    像素 = 点 × target_dpi / 72

这里刻意用 target_dpi（纸上等效分辨率）而不是渲染分辨率 dpi_src —— 后者按
每页的缩放比例反算过，直接拿来算字号会让版面缩放时字也跟着变大变小。用
target_dpi 就能保证「设 10pt 就是纸上 10pt」，与是否拼版、缩放到几版无关。

图片坐标系原点在**左上**，与 PDF 的左下原点相反，转换在各自函数里处理。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict

PT_PER_MM = 72.0 / 25.4

# 字体候选：(常规, 粗体, TTC 内的 subfont 索引)
# Noto Sans CJK 的 ttc 里 subfont 顺序是 JP/KR/SC/TC/HK —— 简体是 **索引 2**，
# 不是 0。用 0 会拿到日文字形，中文里的「直」「骨」「今」等字会被画成日式写法。
_FONT_SETS = (
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
     "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 2),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
     "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    # 开发机上验证用（Windows 自带的有简体中文字形）
    ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
)

PN_POSITIONS = ("bottom-left", "bottom-center", "bottom-right",
                "top-left", "top-center", "top-right")
HF_POSITIONS = ("left", "center", "right")
PN_FORMATS = ("n", "n-of-total", "page-n", "page-n-of-total")
WM_ANGLES = (0, 30, 45, 60, 90, 270)
WM_MAX_LEN = 40                      # 水印文字长度上限，防止有人贴一整篇文章
HF_MAX_LEN = 120                     # 页眉/页脚上限
COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Pillow 9.1 起推荐用 Image.Resampling，但旧版的 Image.BICUBIC 别名一直没删。
# 设备上跑的是 9.4、本机是 12.x，两边都得能跑。
try:
    from PIL import Image as _Image

    RESAMPLE_BICUBIC = _Image.Resampling.BICUBIC
except (ImportError, AttributeError):          # pragma: no cover
    RESAMPLE_BICUBIC = None                    # 留到首次绘制时再解析


class DecorError(ValueError):
    pass


# ------------------------------------------------------------------ 字体
_FONT_CACHE: dict = {}
_FONT_INDEX: tuple | None = None
_FONT_PROBED = False


def font_index() -> tuple | None:
    """挑一组可用字体，返回 (常规路径, 粗体路径, ttc 索引)。"""
    global _FONT_INDEX, _FONT_PROBED
    if not _FONT_PROBED:
        _FONT_PROBED = True
        for reg, bold, idx in _FONT_SETS:
            if os.path.exists(reg) and os.path.exists(bold):
                _FONT_INDEX = (reg, bold, idx)
                break
    return _FONT_INDEX


def load_font(pt: float, target_dpi: int, bold: bool = False):
    """按「纸上点」取字体 —— 像素尺寸只跟 target_dpi 有关，与版面缩放无关。"""
    from PIL import ImageFont

    choice = font_index()
    if not choice:
        raise DecorError("系统里找不到可用字体，无法绘制水印/页码")
    path = choice[1] if bold else choice[0]
    px = max(4, int(round(float(pt) * target_dpi / 72.0)))
    key = (path, choice[2], px)
    f = _FONT_CACHE.get(key)
    if f is None:
        try:
            f = ImageFont.truetype(path, px, index=choice[2])
        except OSError:                       # 某些环境不支持 ttc 索引
            f = ImageFont.truetype(path, px)
        _FONT_CACHE[key] = f
    return f


def parse_color(text: str, fallback=(0, 0, 0)) -> tuple[int, int, int]:
    s = (text or "").strip()
    if not COLOR_RE.match(s):
        if s:
            raise DecorError("颜色格式应为 #RRGGBB：%s" % s[:20])
        return fallback
    s = s[1:]
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


# ------------------------------------------------------------------ 选项
@dataclass
class Decor:
    """装饰选项。字段与界面上的「水印与页码」分区一一对应。"""

    # 水印
    wm_enabled: bool = False
    wm_text: str = "机密"
    wm_size: int = 60                  # 纸上点
    wm_color: str = "#b9bec6"
    wm_opacity: float = 0.35           # 0-1
    wm_angle: int = 45
    wm_tile: bool = True               # True 平铺，False 居中单个

    # 页码
    pn_enabled: bool = False
    pn_position: str = "bottom-center"
    pn_start: int = 1
    pn_format: str = "n-of-total"
    pn_size: int = 10
    pn_skip_first: bool = False

    # 页眉页脚
    hf_header: str = ""
    hf_footer: str = ""
    hf_position: str = "center"
    hf_size: int = 9
    hf_color: str = "#333333"
    hf_margin_mm: float = 10.0

    # ---------------------------------------------------------- 构造与校验
    @classmethod
    def from_dict(cls, data) -> "Decor":
        d = cls()
        if not isinstance(data, dict):
            return d
        for key in cls.__dataclass_fields__:
            if key not in data:
                continue
            val = data[key]
            cur = getattr(d, key)
            if isinstance(cur, bool):
                d.__dict__[key] = _to_bool(val)
            elif isinstance(cur, int):
                try:
                    d.__dict__[key] = int(str(val).strip())
                except (TypeError, ValueError):
                    pass
            elif isinstance(cur, float):
                try:
                    d.__dict__[key] = float(str(val).strip())
                except (TypeError, ValueError):
                    pass
            else:
                d.__dict__[key] = "" if val is None else str(val)
        return d

    def validate(self) -> None:
        """严格校验。所有枚举走白名单 —— 颜色/位置最终都会进绘图调用。"""
        if self.pn_position not in PN_POSITIONS:
            raise DecorError("页码位置只支持 %s" % "/".join(PN_POSITIONS))
        if self.hf_position not in HF_POSITIONS:
            raise DecorError("页眉页脚位置只支持 %s" % "/".join(HF_POSITIONS))
        if self.pn_format not in PN_FORMATS:
            raise DecorError("页码格式只支持 %s" % "/".join(PN_FORMATS))
        if self.wm_angle not in WM_ANGLES:
            raise DecorError("水印角度只支持 %s"
                             % "/".join(str(a) for a in WM_ANGLES))
        if not (0.0 <= self.wm_opacity <= 1.0):
            raise DecorError("水印透明度需在 0-1 之间")
        if not (6 <= self.wm_size <= 300):
            raise DecorError("水印字号需在 6-300 之间")
        if not (6 <= self.pn_size <= 72):
            raise DecorError("页码字号需在 6-72 之间")
        if not (6 <= self.hf_size <= 72):
            raise DecorError("页眉页脚字号需在 6-72 之间")
        if not (0 <= self.hf_margin_mm <= 40):
            raise DecorError("页眉页脚边距需在 0-40 毫米之间")
        if not (1 <= self.pn_start <= 9999):
            raise DecorError("起始页码需在 1-9999 之间")
        limits = (("水印", self.wm_text, WM_MAX_LEN),
                  ("页眉", self.hf_header, HF_MAX_LEN),
                  ("页脚", self.hf_footer, HF_MAX_LEN))
        for label, txt, limit in limits:
            if len(txt) > limit:
                raise DecorError("%s文字过长（最多 %d 字）" % (label, limit))
        parse_color(self.wm_color)
        parse_color(self.hf_color)

    @property
    def active(self) -> bool:
        """是否有任何装饰需要绘制 —— 决定作业是否强制走栅格。"""
        return bool(self.wm_enabled and self.wm_text.strip()
                    or self.pn_enabled
                    or self.hf_header.strip() or self.hf_footer.strip())

    def to_dict(self) -> dict:
        return asdict(self)


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "on", "yes", "是", "y")


# ------------------------------------------------------------------ 绘制
def _text_size(draw, text, font, stroke: int = 0):
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    return box[2] - box[0], box[3] - box[1], box


def draw_text_on(base, text, font, xy, anchor, rgb, alpha=255, stroke=0,
                 stroke_rgb=None):
    """
    把一段文字合成到图上。base 可能是 RGB / L / RGBA。

    Pillow 的 text() 不做 alpha 混合，所以先在透明层上画好再 alpha_composite，
    这样做水印半透明时不会把底下的内容吃掉。
    """
    from PIL import Image, ImageDraw

    if not text:
        return base
    mode = base.mode
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(
        xy, text, font=font, fill=tuple(rgb) + (int(alpha),), anchor=anchor,
        stroke_width=stroke,
        stroke_fill=(tuple(stroke_rgb) + (int(alpha),)) if stroke_rgb else None)
    out = Image.alpha_composite(base.convert("RGBA"), layer)
    return out.convert(mode)


def _probe_draw():
    """取一个只用于量文字尺寸的 ImageDraw（不绑定到真实画布）。"""
    from PIL import Image, ImageDraw

    return ImageDraw.Draw(Image.new("L", (1, 1)))


def _tile_image(text, font, rgb, alpha, angle):
    """把文字做成一块可平铺的图（含旋转）。"""
    from PIL import Image, ImageDraw

    w, h, box = _text_size(_probe_draw(), text, font)
    pad = max(4, int(font.size * 0.25))
    tile = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((pad - box[0], pad - box[1]), text, font=font,
                              fill=tuple(rgb) + (int(alpha),))
    if angle % 360:
        tile = tile.rotate(angle, expand=True, resample=_resample())
    return tile


def _resample():
    """Pillow 的新旧取法都兼容一下。"""
    if RESAMPLE_BICUBIC is not None:
        return RESAMPLE_BICUBIC
    from PIL import Image

    return getattr(Image, "BICUBIC", 3)


def draw_watermark(base, dec: Decor, target_dpi: int, full=None):
    """水印。平铺时按块尺寸加间距、隔行错位，斜向铺满整页。"""
    from PIL import Image

    text = dec.wm_text.strip()
    if not dec.wm_enabled or not text:
        return base
    # 定位一律基于「整页」尺寸而非当前块尺寸 —— 分割打印时同一页会被切成
    # 多块分别渲染，各块只有按同一套坐标画，拼起来才不会错位。
    full = full or base.size
    font = load_font(dec.wm_size, target_dpi, bold=True)
    rgb = parse_color(dec.wm_color, (185, 190, 198))
    alpha = int(round(255 * dec.wm_opacity))

    mode = base.mode
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    tile = _tile_image(text, font, rgb, alpha, dec.wm_angle)

    if not dec.wm_tile:
        # 居中单个：直接按中心落位
        layer.alpha_composite(tile, (int((full[0] - tile.size[0]) / 2),
                                     int((full[1] - tile.size[1]) / 2)))
    else:
        tw, th = tile.size
        step_x = int(tw * 1.15) or 1
        step_y = int(th * 1.5) or 1
        y = -th
        row = 0
        while y < full[1] + th:
            offset = 0 if row % 2 == 0 else step_x // 2      # 隔行错位，更像真水印
            x = -tw + offset
            while x < full[0] + tw:
                layer.alpha_composite(tile, (x, y))
                x += step_x
            y += step_y
            row += 1

    out = Image.alpha_composite(base.convert("RGBA"), layer)
    return out.convert(mode)


def format_page_number(dec: Decor, page_no: int, total: int) -> str:
    n = page_no
    t = total
    if dec.pn_format == "n":
        return str(n)
    if dec.pn_format == "n-of-total":
        return "%d / %d" % (n, t)
    if dec.pn_format == "page-n":
        return "第 %d 页" % n
    return "第 %d 页 / 共 %d 页" % (n, t)


def expand_placeholders(text: str, page_no: int, total: int, today: str = "") -> str:
    """页眉页脚里的占位符替换。"""
    if not text:
        return ""
    return (text.replace("{page}", str(page_no))
                .replace("{total}", str(total))
                .replace("{date}", today))


def _anchor_for(position: str) -> tuple[str, float]:
    """
    返回 (Pillow 锚点, 横向位置比例)。

    锚点用 la/lm/ra 这一组：'l'/'m'/'r' 是横向对齐，'a' 是纵向基准（ascender）。
    纵向统一用 ascender，这样页眉与页脚在字号变化时基线不会跳。
    页码位置形如 bottom-left，页眉页脚位置形如 left，两种写法都接受。
    """
    horiz = {"left": ("l", 0.0), "center": ("m", 0.5), "right": ("r", 1.0)}
    suffix = {"-left": "left", "-center": "center", "-right": "right"}
    key = ""
    for tail, name in suffix.items():
        if position.endswith(tail):
            key = name
            break
    if not key:
        key = position if position in horiz else "center"
    h, ratio = horiz[key]
    return h + "a", ratio


def draw_page_number(base, dec: Decor, page_no: int, total: int, target_dpi: int,
                     full=None):
    if not dec.pn_enabled:
        return base
    if dec.pn_skip_first and page_no == 1:
        return base
    text = format_page_number(dec, page_no, total)
    font = load_font(dec.pn_size, target_dpi)
    full = full or base.size
    anchor, ratio = _anchor_for(dec.pn_position)
    x = ratio * full[0]
    gap = 8 * target_dpi / 72.0                     # 距纸边 8pt
    y = gap if dec.pn_position.startswith("top") else full[1] - gap
    return draw_text_on(base, text, font, (x, y), anchor, (60, 64, 70))


def draw_header_footer(base, dec: Decor, page_no: int, total: int, target_dpi: int,
                       today: str = "", full=None):
    """页眉与页脚。两者共用位置设置与边距。"""
    items = []
    header = expand_placeholders(dec.hf_header.strip(), page_no, total, today)
    footer = expand_placeholders(dec.hf_footer.strip(), page_no, total, today)
    if header:
        items.append((header, True))
    if footer:
        items.append((footer, False))
    if not items:
        return base

    full = full or base.size
    font = load_font(dec.hf_size, target_dpi)
    rgb = parse_color(dec.hf_color, (51, 51, 51))
    anchor, ratio = _anchor_for(dec.hf_position)
    x = ratio * full[0]
    m = dec.hf_margin_mm * PT_PER_MM * target_dpi / 72.0
    for text, is_header in items:
        # 纵向按边距定位而非按内容高度，这样跨块被裁一半时位置仍然正确
        y = m if is_header else full[1] - m
        base = draw_text_on(base, text, font, (x, y), anchor, rgb)
    return base


# ------------------------------------------------------------------ 统一入口
def apply(img, dec: Decor, page_no: int, total: int, target_dpi: int,
          full_px: tuple[int, int] | None = None,
          offset_px: tuple[int, int] = (0, 0), today: str = ""):
    """
    给一页图叠加全部装饰。

    full_px    该页「完整纸面」的像素尺寸。分割打印时一页会被切成多块分别渲染，
               各块共用同一套坐标，所以按 full_px 定位、再减去 offset_px 落到
               当前块内；跨越两块的文字会各画一半，拼起来正好完整。
    offset_px  当前块在完整纸面中的左上角像素偏移。
    """
    if not dec.active:
        return img
    full = full_px or img.size
    ox, oy = offset_px
    if (ox, oy) == (0, 0) and full == img.size:
        # 常见情形：整页一次渲染，直接画
        img = draw_watermark(img, dec, target_dpi, full)
        img = draw_header_footer(img, dec, page_no, total, target_dpi, today, full)
        img = draw_page_number(img, dec, page_no, total, target_dpi, full)
        return img

    # 分块渲染：在一张与整页等大的画布上完成装饰，再裁出本块，
    # 这样跨块文字不会错位。画布尺寸就等于本块 + 偏移，省内存。
    from PIL import Image

    bg = 255 if img.mode == "L" else (255, 255, 255)
    canvas = Image.new(img.mode, (ox + img.size[0], oy + img.size[1]), bg)
    canvas.paste(img, (ox, oy))
    canvas = draw_watermark(canvas, dec, target_dpi, full)
    canvas = draw_header_footer(canvas, dec, page_no, total, target_dpi, today, full)
    canvas = draw_page_number(canvas, dec, page_no, total, target_dpi, full)
    return canvas.crop((ox, oy, ox + img.size[0], oy + img.size[1]))


# ------------------------------------------------------------------ 自检
def _selftest() -> int:
    fails = 0

    def check(name, got, want):
        nonlocal fails
        ok = got == want
        if not ok:
            fails += 1
        print("  %-50s %s" % (name, "ok" if ok else "FAIL  得到 %r 期望 %r" % (got, want)))

    def raises(name, fn, needle=""):
        nonlocal fails
        try:
            fn()
        except DecorError as exc:
            ok = needle in str(exc)
            if not ok:
                fails += 1
            print("  %-50s %s" % (name, "ok" if ok else "FAIL  信息不含 %r：%s" % (needle, exc)))
        except Exception as exc:                      # noqa: BLE001
            fails += 1
            print("  %-50s FAIL  抛错类型不对：%r" % (name, exc))
        else:
            fails += 1
            print("  %-50s FAIL  没有抛错" % name)

    print("== pg_decor 自检 ==")

    # 颜色
    check("颜色 #ffffff", parse_color("#ffffff"), (255, 255, 255))
    check("颜色 #000", parse_color("#000"), (0, 0, 0))
    check("颜色 #Ab12Cd 大小写无关", parse_color("#Ab12Cd"), (171, 18, 205))
    check("空颜色回退", parse_color("", (7, 8, 9)), (7, 8, 9))
    raises("颜色非法报错", lambda: parse_color("red"), "颜色格式")
    raises("颜色注入尝试被拒", lambda: parse_color("#fff;rm -rf /"), "颜色格式")

    # 解析
    d = Decor.from_dict({"wm_enabled": "on", "wm_size": "48", "wm_opacity": "0.5",
                         "pn_start": "3", "hf_size": "11"})
    check("布尔解析 on", d.wm_enabled, True)
    check("整数解析", d.wm_size, 48)
    check("浮点解析", d.wm_opacity, 0.5)
    check("起始页码解析", d.pn_start, 3)
    check("未知字段被忽略", Decor.from_dict({"nope": 1}).wm_size, 60)
    check("非字典回退默认", Decor.from_dict(None).pn_format, "n-of-total")

    # active
    check("默认无装饰", Decor().active, False)
    check("水印开关但空文字不算激活",
          Decor(wm_enabled=True, wm_text="  ").active, False)
    check("水印有字才算激活", Decor(wm_enabled=True, wm_text="机密").active, True)
    check("页码激活", Decor(pn_enabled=True).active, True)
    check("页眉激活", Decor(hf_header="报告").active, True)
    check("页脚激活", Decor(hf_footer="内部").active, True)
    check("页眉空白不算激活", Decor(hf_header="   ").active, False)

    # 校验
    Decor().validate()
    raises("页码位置白名单", lambda: Decor(pn_position="middle").validate(), "页码位置")
    raises("页脚位置白名单", lambda: Decor(hf_position="top").validate(), "页眉页脚位置")
    raises("页码格式白名单", lambda: Decor(pn_format="roman").validate(), "页码格式")
    raises("水印角度白名单", lambda: Decor(wm_angle=17).validate(), "水印角度")
    raises("透明度上界", lambda: Decor(wm_opacity=1.5).validate(), "透明度")
    raises("透明度下界", lambda: Decor(wm_opacity=-0.1).validate(), "透明度")
    raises("水印字号过小", lambda: Decor(wm_size=2).validate(), "水印字号")
    raises("页码字号过大", lambda: Decor(pn_size=200).validate(), "页码字号")
    raises("页眉字号过大", lambda: Decor(hf_size=200).validate(), "字号需在")
    raises("边距越界", lambda: Decor(hf_margin_mm=99).validate(), "边距")
    raises("起始页码为 0", lambda: Decor(pn_start=0).validate(), "起始页码")
    raises("水印文字过长", lambda: Decor(wm_text="字" * 41).validate(), "水印文字过长")
    raises("页眉文字过长", lambda: Decor(hf_header="x" * 121).validate(), "页眉文字过长")
    raises("水印颜色非法", lambda: Decor(wm_color="red").validate(), "颜色格式")

    # 页码格式
    dd = Decor()
    dd.pn_format = "n"
    check("格式 n", format_page_number(dd, 3, 8), "3")
    dd.pn_format = "n-of-total"
    check("格式 n-of-total", format_page_number(dd, 3, 8), "3 / 8")
    dd.pn_format = "page-n"
    check("格式 page-n", format_page_number(dd, 3, 8), "第 3 页")
    dd.pn_format = "page-n-of-total"
    check("格式 page-n-of-total", format_page_number(dd, 3, 8), "第 3 页 / 共 8 页")

    # 占位符
    check("占位符替换",
          expand_placeholders("第{page}页/共{total}页 {date}", 2, 9, "2026-09-16"),
          "第2页/共9页 2026-09-16")
    check("无占位符原样", expand_placeholders("内部资料", 1, 1), "内部资料")
    check("空串返回空", expand_placeholders("", 1, 1), "")

    # 锚点
    check("页脚居左锚点", _anchor_for("bottom-left")[0], "la")
    check("页脚居中锚点", _anchor_for("bottom-center")[0], "ma")
    check("页脚居右锚点", _anchor_for("bottom-right")[0], "ra")
    check("居中比例", _anchor_for("center")[1], 0.5)

    # 字体
    fi = font_index()
    if fi:
        f = load_font(10, 300)
        check("10pt@300dpi 像素尺寸", round(f.size), round(10 * 300 / 72.0))
        a, b = load_font(10, 300).size, load_font(10, 600).size
        check("字号随 dpi 线性放大", abs(b - 2 * a) <= 1, True)
        check("粗体与常规不同文件", fi[0] != fi[1], True)
        check("ttc 索引是合法整数", isinstance(fi[2], int) and fi[2] >= 0, True)
    else:
        print("  （本机没有可用字体，跳过字体相关检查）")

    # ---- 渲染级检查：真的画出来再量像素，避免「逻辑对但画错位置」
    if fi:
        from PIL import Image

        def dark(im, box, thr=200):
            """
            区域内「暗于阈值」的像素个数。

            不用平均暗度：页码只有 10pt，在 1000x1400 的图里一平均几乎
            冲淡成 255，阈值稍微一动结论就翻。数像素个数稳得多。

            thr 要按内容挑：文字用 200，水印是浅灰的（合成后约 213-230），
            得放宽到 245 才数得到。
            """
            hist = im.crop(box).convert("L").histogram()
            return sum(hist[:thr])

        def mean(im, box):
            hist = im.crop(box).convert("L").histogram()
            total = sum(hist) or 1
            return sum(i * n for i, n in enumerate(hist)) / float(total)

        W, H = 1240, 1754                       # A4 @150dpi 的真实像素尺寸
        blank = Image.new("RGB", (W, H), (255, 255, 255))
        top = (0, 0, W, 90)
        bottom = (0, H - 90, W, H)

        # 页码：居中
        d = Decor(pn_enabled=True, pn_position="bottom-center",
                  pn_format="n-of-total")
        got = apply(blank.copy(), d, 3, 8, 150)
        check("页码居中：底部有内容", dark(got, bottom) > 20, True)
        check("页码居中：顶部保持空白", dark(got, top), 0)
        check("页码居中：左下角空白（确是真居中）",
              dark(got, (0, H - 90, W // 4, H)), 0)

        # 页码：居左
        d = Decor(pn_enabled=True, pn_position="bottom-left", pn_format="n")
        got = apply(blank.copy(), d, 1, 5, 150)
        check("页码居左：左下角有内容", dark(got, (0, H - 90, W // 4, H)) > 20, True)
        check("页码居左：右下角空白", dark(got, (3 * W // 4, H - 90, W, H)), 0)

        # 首页不显示页码
        d = Decor(pn_enabled=True, pn_skip_first=True)
        check("首页跳过页码时原图不变",
              apply(blank.copy(), d, 1, 5, 150).tobytes() == blank.tobytes(), True)
        check("首页跳过但第二页仍有页码",
              apply(blank.copy(), d, 2, 5, 150).tobytes() != blank.tobytes(), True)

        # 页眉 / 页脚
        got = apply(blank.copy(), Decor(hf_header="月度报告 {page}/{total}"),
                    1, 3, 150)
        check("页眉：顶部有内容", dark(got, top) > 20, True)
        check("页眉：底部保持空白", dark(got, bottom), 0)
        got = apply(blank.copy(), Decor(hf_footer="内部资料"), 1, 3, 150)
        check("页脚：底部有内容", dark(got, bottom) > 20, True)

        # 水印：平铺 vs 居中
        d = Decor(wm_enabled=True, wm_text="机密", wm_tile=True, wm_opacity=0.5)
        got = apply(blank.copy(), d, 1, 1, 150)
        quads = [dark(got, (x, y, x + W // 4, y + H // 4), 245)
                 for x in (0, W // 2) for y in (0, H // 2)]
        check("水印平铺：四个象限都铺到了", all(q > 2000 for q in quads), True)
        ratio = dark(got, (0, 0, W, H), 245) / float(W * H)
        check("水印平铺：覆盖率在合理区间", 0.02 < ratio < 0.30, True)

        d = Decor(wm_enabled=True, wm_text="机密", wm_tile=False, wm_opacity=0.6)
        got = apply(blank.copy(), d, 1, 1, 150)
        check("水印居中：左上角干净", dark(got, (0, 0, W // 5, H // 5)), 0)
        check("水印居中：中心有墨",
              dark(got, (2 * W // 5, 2 * H // 5, 3 * W // 5, 3 * H // 5), 245) > 50,
              True)

        # 透明度真的起作用
        light = apply(blank.copy(), Decor(wm_enabled=True, wm_text="机密",
                                          wm_tile=False, wm_opacity=0.1), 1, 1, 150)
        heavy = apply(blank.copy(), Decor(wm_enabled=True, wm_text="机密",
                                          wm_tile=False, wm_opacity=0.9), 1, 1, 150)
        centre = (2 * W // 5, 2 * H // 5, 3 * W // 5, 3 * H // 5)
        check("透明度：高比低更暗", mean(heavy, centre) < mean(light, centre), True)

        # 灰度作业（L 模式）不能被画坏
        check("灰度图模式保持 L",
              apply(Image.new("L", (W, H), 255), Decor(hf_footer="页脚"),
                    1, 1, 150).mode, "L")

        # 分块渲染必须与整页渲染逐像素一致 —— 分割打印的正确性基础
        d = Decor(pn_enabled=True, pn_position="bottom-center", hf_header="报表")
        whole = apply(blank.copy(), d, 1, 2, 150)
        cut = int(W * 0.37)
        left = apply(blank.copy().crop((0, 0, cut, H)), d, 1, 2, 150, (W, H), (0, 0))
        right = apply(blank.copy().crop((cut, 0, W, H)), d, 1, 2, 150, (W, H), (cut, 0))
        stitched = Image.new("RGB", (W, H), (255, 255, 255))
        stitched.paste(left, (0, 0))
        stitched.paste(right, (cut, 0))
        check("分块渲染与整页渲染一致", stitched.tobytes() == whole.tobytes(), True)

        # 无装饰时一个像素都不许动
        check("无装饰时原图不变",
              apply(blank.copy(), Decor(), 1, 1, 150).tobytes() == blank.tobytes(),
              True)

    print("== 自检结束：%s ==" % ("全部通过" if not fails else "%d 项失败" % fails))
    return fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if _selftest() else 0)
