"""
打印引擎 —— 把一份文件按打印设置处理成「最终 PDF」。

四段流水线
----------
    输入( PDF / 图片 )
      │
      ├─ 1. 页选取      pdfseparate + pdfunite        矢量，精确控制顺序
      ├─ 2. 归一化      ghostscript                   矢量，纸张/缩放/灰度/注释/镜像
      ├─ 3. 拼版        pdftoppm + reportlab          栅格，仅需要时触发
      └─ 4. 输出        预览(pdftoppm) / 打印(lp)

为什么不用 CUPS 的 pdftopdf 做拼版
----------------------------------
实测 cups-filters 1.28.17：booklet 会把内容整体转 90°，而 `booklet-signature`
直接 SIGABRT 崩溃 —— 无法支持装订位置与子集。自建拼版换来确定性与所见即所得。
页选取同理：自己用 poppler 拆分合并，顺序完全由代码决定，预览与打印必然一致。

为什么 1 版时保持矢量
---------------------
不需要重排时不光栅化，文字仍是矢量、直接透传，画质与速度都是最优。
只有真正涉及重排（多版、小册子、页边距、版边框）时才走栅格拼版。
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, asdict

import pg_decor
import pg_layout
from pg_layout import PAPERS, MM

# ------------------------------------------------------------------ 常量
GS = "gs"
PDFINFO = "pdfinfo"
PDFSEPARATE = "pdfseparate"
PDFUNITE = "pdfunite"
PDFTOPPM = "pdftoppm"
LP = "lp"
LPSTAT = "lpstat"

# 兜底 PPD：仅在本机队列没有自己的 PPD 时使用（例如 raw 队列）。
# 内容为最小可用 PPD，已通过 cupstestppd 校验。
FALLBACK_PPD = "/usr/local/share/print-gateway/gw.ppd"

DPI_CHOICES = (150, 200, 300, 600)
MAX_COPIES = 99
MAX_MARGIN_MM = 50.0
DEFAULT_DPI = 300
PREVIEW_DPI = 72
PREVIEW_MAX_PAGES = 40
RENDER_WORKERS = 3                     # 板子只有 988MB 内存，栅格化不宜过度并发


# ------------------------------------------------------------------ 规格
@dataclass
class PrintSpec:
    """一份打印作业的全部设置。字段与界面分区一一对应。"""

    # 基础设置
    printer: str = ""
    copies: int = 1
    collate: bool = True
    grayscale: bool = False
    content: str = "document"          # document | annotations

    # 页面范围
    page_range: str = ""
    page_set: str = "all"              # all | odd | even
    reverse: bool = False

    # 打印方式
    per_sheet: int = 1
    layout_order: str = "lrtb"
    booklet: bool = False
    booklet_binding: str = "left"      # left | right
    booklet_subset: str = "both"       # both | front | back
    border: bool = False
    mirror: bool = False
    duplex: str = "one-sided"          # one-sided | two-sided-long-edge | two-sided-short-edge

    # 页面设置
    paper: str = "A4"
    orientation: str = "auto"          # auto | portrait | landscape
    scale_mode: str = "fit"            # none | fit | stretch
    margins: dict = field(default_factory=lambda: {"top": 0.0, "right": 0.0,
                                                   "bottom": 0.0, "left": 0.0})

    # 内容设置
    auto_center: bool = True
    auto_rotate: bool = False

    # 裁剪：四边各切掉多少毫米
    crop_mm: dict = field(default_factory=lambda: {"top": 0.0, "right": 0.0,
                                                   "bottom": 0.0, "left": 0.0})

    # 分割（海报打印）：一页放大到 rows x cols 张纸。1 表示不分割。
    split_rows: int = 1
    split_cols: int = 1

    # 页面装饰：水印 / 页码 / 页眉页脚（见 pg_decor.Decor）
    decor: dict = field(default_factory=dict)

    # 出图
    dpi: int = DEFAULT_DPI

    # ---------------------------------------------------------- 构造与校验
    @classmethod
    def from_form(cls, data: dict) -> "PrintSpec":
        """从表单字典构造。未知字段忽略，非法值在 validate() 里报错。"""
        spec = cls()
        if not isinstance(data, dict):
            return spec
        for key in cls.__dataclass_fields__:
            if key not in data:
                continue
            val = data[key]
            cur = getattr(spec, key)
            if key == "margins":
                if isinstance(val, dict):
                    merged = dict(spec.margins)
                    for k in ("top", "right", "bottom", "left"):
                        if k in val:
                            merged[k] = _to_float(val[k], 0.0)
                    spec.margins = merged
            elif key in ("crop_mm", "decor"):
                # 字典字段只做浅合并；具体清洗交给 validate() 与下游模块，
                # 免得同一套规则在这里再写一遍、日后两边走岔。
                if isinstance(val, dict):
                    merged = dict(cur or {})
                    merged.update(val)
                    setattr(spec, key, merged)
            elif isinstance(cur, bool):
                setattr(spec, key, _to_bool(val))
            elif isinstance(cur, int):
                setattr(spec, key, _to_int(val, cur))
            elif isinstance(cur, float):
                setattr(spec, key, _to_float(val, cur))
            else:
                setattr(spec, key, "" if val is None else str(val).strip())
        return spec

    def validate(self) -> None:
        """严格校验。所有枚举都走白名单 —— 这是喂给命令行的第一道闸门。"""
        if self.paper not in PAPERS:
            raise ValueError("纸张只支持 %s" % "/".join(PAPERS))
        if self.per_sheet not in pg_layout.NUP_GRID:
            raise ValueError("每版页数只支持 %s"
                             % "/".join(str(k) for k in sorted(pg_layout.NUP_GRID)))
        if self.layout_order not in pg_layout.ORDER_NAMES:
            raise ValueError("排列顺序只支持 %s" % "/".join(pg_layout.ORDER_NAMES))
        if self.page_set not in ("all", "odd", "even"):
            raise ValueError("奇偶页只支持 all/odd/even")
        if self.orientation not in ("auto", "portrait", "landscape"):
            raise ValueError("方向只支持 auto/portrait/landscape")
        if self.scale_mode not in ("none", "fit", "stretch"):
            raise ValueError("缩放只支持 none/fit/stretch")
        if self.content not in ("document", "annotations"):
            raise ValueError("打印内容只支持 document/annotations")
        if self.duplex not in ("one-sided", "two-sided-long-edge", "two-sided-short-edge"):
            raise ValueError("双面设置非法")
        if self.booklet_binding not in ("left", "right"):
            raise ValueError("装订位置只支持 left/right")
        if self.booklet_subset not in ("both", "front", "back"):
            raise ValueError("小册子子集只支持 both/front/back")
        if self.dpi not in DPI_CHOICES:
            raise ValueError("分辨率只支持 %s" % "/".join(str(d) for d in DPI_CHOICES))
        if not (1 <= self.copies <= MAX_COPIES):
            raise ValueError("份数需在 1-%d 之间" % MAX_COPIES)
        for k in ("top", "right", "bottom", "left"):
            m = float(self.margins.get(k, 0) or 0)
            if m < 0 or m > MAX_MARGIN_MM:
                raise ValueError("页边距需在 0-%g 毫米之间" % MAX_MARGIN_MM)
        # 只放行数字、逗号、连字符、空格与制表符。刻意用显式字符类而不用 \s ——
        # \s 会把换行也算作空白，让带换行的输入穿过这道闸门。
        if self.page_range and not re.fullmatch(r"[0-9,\- \t]+", self.page_range):
            raise ValueError("页码范围只能包含数字、逗号、连字符和空格")
        if self.booklet and self.per_sheet != 1:
            # 小册子固定 2 版，忽略用户的每版页数而不是报错
            self.per_sheet = 1

        # 裁剪量：越界夹住、非数字归零，然后写回规范化结果，
        # 这样 cache_key 对「10」与「10.0」是一致的
        self.crop_mm = pg_layout.parse_crop({"crop_mm": self.crop_mm})

        srows, scols = pg_layout.split_grid({"split_rows": self.split_rows,
                                             "split_cols": self.split_cols})
        self.split_rows, self.split_cols = srows, scols

        # 装饰：规范化 + 白名单校验。颜色/位置最终会进绘图调用，必须先卡住。
        dec = pg_decor.Decor.from_dict(self.decor)
        dec.validate()
        if dec.active and pg_decor.font_index() is None:
            raise ValueError("系统缺少中文字体，无法绘制水印/页码/页眉页脚")
        self.decor = dec.to_dict()

    def cache_key(self) -> str:
        d = asdict(self)
        d.pop("printer", None)             # 打印机不影响版面
        d.pop("copies", None)
        return hashlib.sha256(json.dumps(d, sort_keys=True,
                                         ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

    def layout_dict(self) -> dict:
        return {
            "paper": self.paper,
            "orientation": self.orientation,
            "per_sheet": self.per_sheet,
            "layout_order": self.layout_order,
            "booklet": self.booklet,
            "booklet_binding": self.booklet_binding,
            "booklet_subset": self.booklet_subset,
            "border": self.border,
            "mirror": self.mirror,
            "margins": self.margins,
            "auto_center": self.auto_center,
            "auto_rotate": self.auto_rotate,
            "crop_mm": self.crop_mm,
            "split_rows": self.split_rows,
            "split_cols": self.split_cols,
            "stretch": self.scale_mode == "stretch",
        }


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "on", "yes", "是", "y")


def _to_int(v, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _to_float(v, default: float) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ 子进程
class EngineError(RuntimeError):
    pass


def run(cmd: list[str], timeout: int = 300, cwd: str | None = None) -> tuple[int, str]:
    """执行外部命令，返回 (退出码, 合并输出)。不使用 shell，避免注入。"""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, cwd=cwd)
    except FileNotFoundError as exc:
        raise EngineError("缺少程序：%s" % cmd[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise EngineError("命令超时（%d 秒）：%s" % (timeout, " ".join(cmd[:3]))) from exc
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace")


def which(name: str) -> bool:
    return shutil.which(name) is not None


# ------------------------------------------------------------------ PDF 信息
_PAGE_COUNT_RE = re.compile(r"^Pages:\s+(\d+)", re.M)
# poppler 有两种输出：所有页同尺寸时只有一行 `Page size:`，
# 尺寸不一致时才逐页输出 `Page N size:`。两种都要认。
_PAGE_SIZE_RE = re.compile(
    r"^Page(?:\s+(\d+))?\s+size:\s+([\d.]+)\s+x\s+([\d.]+)", re.M)


def pdf_info(path: str) -> tuple[int, list[tuple[float, float]]]:
    """返回 (页数, 每页尺寸)。pdfinfo -box 逐页输出 size 行。"""
    rc, out = run([PDFINFO, "-box", path], timeout=120)
    if rc != 0:
        raise EngineError("无法解析 PDF：%s" % out.strip()[:200])
    m = _PAGE_COUNT_RE.search(out)
    count = int(m.group(1)) if m else 0

    uniform = None                                # 形如 "Page size: W x H"
    per_page: dict[int, tuple[float, float]] = {}  # 形如 "Page N size: W x H"
    for pm in _PAGE_SIZE_RE.finditer(out):
        size = (float(pm.group(2)), float(pm.group(3)))
        if pm.group(1) is None:
            uniform = size
        else:
            per_page[int(pm.group(1))] = size

    if not uniform and not per_page:
        raise EngineError("PDF 中找不到页面尺寸")

    if uniform is not None:                       # 同尺寸：一行铺满全篇
        sizes = [uniform] * count
        for idx, size in per_page.items():        # 个别页例外则以逐页声明为准
            if 1 <= idx <= count:
                sizes[idx - 1] = size
    else:                                         # 逐页声明：缺失页用首见尺寸补齐
        first = per_page[sorted(per_page)[0]]
        sizes = [per_page.get(i + 1, first) for i in range(count)]
    return count, sizes[:count]


# ------------------------------------------------------------------ 1. 页选取
def select_pages(src: str, pages: list[int], out: str, workdir: str) -> None:
    """
    按给定页号顺序抽出页面并合并 —— 页范围、奇偶页、反向都在这里一次解决。
    用 poppler 的 pdfseparate/pdfunite，顺序由代码决定，不依赖任何外部工具的默认行为。
    """
    parts_dir = os.path.join(workdir, "parts")
    os.makedirs(parts_dir, exist_ok=True)
    made: list[str] = []
    for i, p in enumerate(pages):
        target = os.path.join(parts_dir, "p%05d.pdf" % i)
        rc, out_txt = run([PDFSEPARATE, "-f", str(p), "-l", str(p), src, target],
                          timeout=120)
        if rc != 0 or not os.path.exists(target):
            raise EngineError("拆页失败（第 %d 页）：%s" % (p, out_txt.strip()[:160]))
        made.append(target)
    if len(made) == 1:
        shutil.copyfile(made[0], out)
        return
    rc, out_txt = run([PDFUNITE] + made + [out], timeout=300)
    if rc != 0 or not os.path.exists(out):
        raise EngineError("合并页面失败：%s" % out_txt.strip()[:160])


def merge_pdfs(parts: list[str], out: str) -> str:
    """
    按顺序合并多个 PDF —— 批量打印用（一次选了多个文件）。

    只有一个文件时直接复制，绕开 pdfunite 的无谓重写（重写会丢掉一些
    阅读器专有的元数据，也没必要多花一次 IO）。
    """
    if not parts:
        raise EngineError("没有可合并的文件")
    for p in parts:
        if not os.path.exists(p):
            raise EngineError("待合并的文件不存在：%s" % os.path.basename(p))
    if len(parts) == 1:
        if os.path.realpath(parts[0]) != os.path.realpath(out):
            shutil.copyfile(parts[0], out)
        return out
    rc, txt = run([PDFUNITE] + list(parts) + [out], timeout=600)
    if rc != 0 or not os.path.exists(out):
        raise EngineError("合并文件失败：%s" % txt.strip()[:200])
    return out


# ------------------------------------------------------------------ 2. 归一化
def gs_normalize(src: str, out: str, *, target: tuple[float, float] | None,
                 fit: bool, grayscale: bool, annotations: bool,
                 timeout: int = 300) -> str:
    """
    ghostscript 归一化。全程矢量，不损失文字锐度。

    target  目标纸张 (宽, 高)，单位点；None 表示不改尺寸
    fit     是否等比缩放到目标纸张
    """
    cmd = [GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER",
           "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.7"]
    if target and fit:
        w, h = target
        cmd += ["-dFIXEDMEDIA", "-dPDFFitPage",
                "-dDEVICEWIDTHPOINTS=%g" % w, "-dDEVICEHEIGHTPOINTS=%g" % h]
    elif target:
        w, h = target
        cmd += ["-dFIXEDMEDIA", "-dDEVICEWIDTHPOINTS=%g" % w, "-dDEVICEHEIGHTPOINTS=%g" % h]
    if grayscale:
        cmd += ["-sColorConversionStrategy=Gray", "-dProcessColorModel=/DeviceGray"]
    if not annotations:
        cmd += ["-dShowAnnots=false", "-dShowAcroForm=false"]
    # 注意：镜像（反片）不在这里做。ghostscript 的 /MirrorPrint、/Install、
    # /BeginPage 三条钩子在 pdfwrite 上对多页文档实测全部失效，改由
    # pg_layout.compose 在合成阶段翻转。
    cmd += ["-sOutputFile=" + out, src]
    rc, txt = run(cmd, timeout=timeout)
    if rc != 0 or not os.path.exists(out):
        raise EngineError("归一化失败：%s" % txt.strip()[-240:])
    return out


# ------------------------------------------------------------------ 3. 拼版
def render_source_pages(src: str, jobs: list[dict], workdir: str,
                        grayscale: bool, decor=None, target_dpi: int = DEFAULT_DPI,
                        total: int = 0, today: str = "") -> dict[str, str]:
    """
    按渲染任务逐块栅格化，并在渲染后立刻叠加页面装饰。

    为什么逐块渲染：分割打印时若先渲染整页大图再切，那张图是 (行 x 列) 倍
    像素 —— A4@300dpi 的 3x3 就是 7441x10524，约 235MB，这台 988MB 的板子
    撑不住。逐块时每块的像素量恒等于「一张纸在目标 dpi 下的像素」，与分割
    数无关。pdftoppm 的 -x/-y/-W/-H 正好可以按像素取块。

    装饰在这里做（而不是矢量层）的原因见 pg_decor 模块头。并行度仍受内存
    限制，默认 3 个 worker。
    """
    from concurrent.futures import ThreadPoolExecutor

    out: dict[str, str] = {}
    errors: list[str] = []
    need_decor = decor is not None and decor.active

    def one(job: dict) -> tuple[str | None, str | None]:
        key = job["key"]
        safe = re.sub(r"[^0-9A-Za-z@,]", "_", key)
        prefix = os.path.join(workdir, "src_%s" % safe)
        x, y, w, h = job["region"]
        cmd = [PDFTOPPM, "-f", str(job["page"]), "-l", str(job["page"]),
               "-r", str(job["dpi"]), "-png",
               "-x", str(x), "-y", str(y), "-W", str(w), "-H", str(h)]
        if grayscale:
            cmd.append("-gray")
        cmd += [src, prefix]
        rc, txt = run(cmd, timeout=900)
        if rc != 0:
            errors.append("第 %d 页渲染失败：%s" % (job["page"], txt.strip()[:120]))
            return None, None
        found = sorted(glob.glob(prefix + "*.png"))
        if not found:
            errors.append("第 %d 页未生成图片" % job["page"])
            return None, None

        path = found[0]
        if need_decor:
            try:
                from PIL import Image
                img = Image.open(path)
                img.load()
                img = pg_decor.apply(img, decor, int(job.get("label", job["page"])),
                                     total or 1, target_dpi,
                                     tuple(job["full"]), tuple(job["offset"]), today)
                img.save(path)
            except Exception as exc:                     # noqa: BLE001
                errors.append("第 %d 页装饰失败：%s" % (job["page"], exc))
                return None, None
        return key, path

    with ThreadPoolExecutor(max_workers=RENDER_WORKERS) as pool:
        for key, path in pool.map(one, jobs):
            if key and path:
                out[key] = path
    if errors:
        raise EngineError("；".join(errors[:3]))
    return out


def impose(src: str, out: str, plan: dict, dpi: int, grayscale: bool, workdir: str,
           decor=None, labels: dict | None = None,
           total_pages: int = 0, today: str = "") -> dict:
    """栅格拼版：按方案逐块渲染源页 -> 合成到纸面。"""
    plan = dict(plan)
    plan["pages_used"] = plan.get("pages_used") or len(plan["_page_sizes"])
    jobs = pg_layout.render_jobs(plan, dpi)
    if not jobs:
        raise EngineError("没有需要渲染的页面")

    labels = labels or {}
    for job in jobs:
        job["label"] = labels.get(job["page"], job["page"])

    images = render_source_pages(src, jobs, workdir, grayscale, decor,
                                 target_dpi=dpi, total=total_pages or len(jobs),
                                 today=today)
    pg_layout.compose(plan, images, out)
    return {"render_dpi": [j["dpi"] for j in jobs],
            "images": len(images), "tiles": len(jobs)}


# ------------------------------------------------------------------ 4. 预览与打印
def make_preview(pdf: str, workdir: str, dpi: int = PREVIEW_DPI,
                 max_pages: int = PREVIEW_MAX_PAGES) -> list[str]:
    """把最终 PDF 渲染成预览图 —— 预览渲染的就是即将送印的同一个文件。"""
    count, _ = pdf_info(pdf)
    limit = min(count, max_pages)
    prefix = os.path.join(workdir, "preview")
    cmd = [PDFTOPPM, "-f", "1", "-l", str(limit), "-r", str(dpi), "-png", pdf, prefix]
    rc, txt = run(cmd, timeout=300)
    if rc != 0:
        raise EngineError("预览渲染失败：%s" % txt.strip()[:200])
    files = sorted(glob.glob(prefix + "*.png"),
                   key=lambda p: int(re.search(r"(\d+)\.png$", p).group(1)))
    return files


def send_to_printer(pdf: str, spec: PrintSpec, title: str) -> str:
    """
    送队列。排版已在网关侧完成，这里只传驱动级选项（份数/逐份/双面/纸张）。

    注意：不传 number-up / booklet —— 版面已经拼好，再传会二次拼版。
    """
    if not spec.printer:
        raise EngineError("未选择打印机")
    cmd = [LP, "-d", spec.printer, "-n", str(spec.copies),
           "-t", title or "print-gateway",
           "-o", "collate=" + ("true" if spec.collate else "false"),
           "-o", "media=" + spec.paper,
           "-o", "sides=" + spec.duplex]
    if spec.grayscale:
        cmd += ["-o", "ColorModel=Gray"]
    cmd += [pdf]
    rc, txt = run(cmd, timeout=120)
    if rc != 0:
        raise EngineError("提交打印失败：%s" % txt.strip()[:200])
    m = re.search(r"request id is (\S+)", txt)
    return m.group(1) if m else txt.strip().splitlines()[0] if txt.strip() else "unknown"


def list_printers() -> list[dict]:
    """列出可用队列及其状态。"""
    if not which(LP):
        return []
    rc, out = run(["lpstat", "-p", "-d"], timeout=30)
    printers = []
    for m in re.finditer(r"^printer (\S+) is (\S+)(?:\.\s*(.*))?$", out, re.M):
        printers.append({"name": m.group(1), "state": m.group(2),
                         "detail": (m.group(3) or "").strip()})
    return printers


# `lpstat -d` 有默认队列时输出 `system default destination: NAME`；
# 没设默认时输出 `no system default destination`，正则不匹配，返回空串。
_DEFAULT_DEST_RE = re.compile(r"^system default destination:\s*(\S+)\s*$", re.M)


def cups_default_printer() -> str:
    """
    返回 CUPS 的**系统默认队列**（即 `lpstat -d` / CUPS 网页里设的默认打印机）。

    取不到时返回空串（未安装 / 未设默认 / 命令失败），调用方自行回退。
    """
    if not which(LPSTAT):
        return ""
    try:
        rc, out = run([LPSTAT, "-d"], timeout=15)
    except EngineError:
        return ""
    if rc != 0:
        return ""
    m = _DEFAULT_DEST_RE.search(out)
    return m.group(1) if m else ""


# ------------------------------------------------------------------ 编排
def _today() -> str:
    import time
    return time.strftime("%Y-%m-%d")


def build_final(spec: PrintSpec, src_pdf: str, workdir: str) -> dict:
    """
    把源 PDF 按设置处理成最终 PDF。

    返回 {"pdf","plan","mode","pages","sheet","notes"}
    mode 为 "vector"（矢量直出）/ "raster"（栅格拼版）/ "tile"（分割打印）。
    """
    os.makedirs(workdir, exist_ok=True)
    notes: list[str] = []

    dec = pg_decor.Decor.from_dict(spec.decor)
    today = _today()

    count, sizes = pdf_info(src_pdf)
    if count == 0:
        raise EngineError("文档没有可打印的页面")

    # ---- 页选取：范围 -> 奇偶 -> 反向
    wanted = pg_layout.parse_page_range(spec.page_range, count)
    wanted = pg_layout.apply_page_set(wanted, spec.page_set)
    if spec.reverse:
        wanted = list(reversed(wanted))
    if not wanted:
        raise EngineError("按当前页码范围没有选中任何页面")

    cur = src_pdf
    if wanted != list(range(1, count + 1)):
        sel = os.path.join(workdir, "selected.pdf")
        select_pages(src_pdf, wanted, sel, workdir)
        cur = sel
        notes.append("已选取 %d 页（共 %d 页）" % (len(wanted), count))
    count, sizes = pdf_info(cur)

    # 页码按「打印顺序」编号，而不是原始页号 —— 反向打印时第 1 张纸就该显示
    # 第 1 页。同一页被重复选中时以首次出现的序号为准。
    labels: dict[int, int] = {}
    for i, p in enumerate(wanted):
        labels.setdefault(p, i + 1)
    total = len(wanted)

    # ---- 先探一次版面。这一步只需要「是否要拼版」与纸面尺寸，二者都只依赖
    #      设置而不依赖源页尺寸，所以可以放在归一化之前。
    probe = pg_layout.build_plan(spec.layout_dict(), sizes, count)
    need_raster = bool(probe["need_raster"] or dec.active)
    if need_raster:
        # 拼版时源页先归一到「未转方向的纸张」，纸面方向交给拼版决定。
        # 拉伸铺满**不做归一化**：内容要保持完整（归一化会等比缩放甚至改变
        # 媒体框），拉满版心的活儿交给拼版层。
        target = PAPERS[spec.paper] if spec.scale_mode == "fit" else None
    else:
        target = ((probe["sheet_w"], probe["sheet_h"])
                  if spec.scale_mode == "fit" else None)
    sheet_note = "纸面 %g×%g 点" % (probe["sheet_w"], probe["sheet_h"])

    # ---- 归一化
    norm = os.path.join(workdir, "normalized.pdf")
    gs_normalize(cur, norm, target=target, fit=(spec.scale_mode == "fit"),
                 grayscale=spec.grayscale,
                 annotations=(spec.content == "annotations"))

    # ---- 归一化会改变页面尺寸（例如 Letter 缩到 A4）。切割位置、缩放比例都必须
    #      基于归一化后的**真实**尺寸来算，否则裁剪/分割会整体偏移。
    n_count, n_sizes = pdf_info(norm)
    if n_count != count:
        raise EngineError("归一化后页数发生变化（%d -> %d）" % (count, n_count))
    count, sizes = n_count, n_sizes

    plan = pg_layout.build_plan(spec.layout_dict(), sizes, count)
    plan["need_raster"] = need_raster

    # ---- 拼版（需要时才栅格化）
    final = os.path.join(workdir, "final.pdf")
    info = {}
    if need_raster:
        info = impose(norm, final, plan, spec.dpi, spec.grayscale, workdir,
                      decor=dec, labels=labels, total_pages=total, today=today)
        srows, scols = plan["split"]
        if srows * scols > 1:
            mode = "tile"
            notes.append("分割打印：每页放大到 %d×%d 张纸，共 %d 张"
                         % (srows, scols, plan["pages_used"] * srows * scols))
        else:
            mode = "raster"
            notes.append("拼版处理：%d 版/张，单页渲染分辨率约 %s dpi"
                         % (spec.per_sheet if not spec.booklet else 2,
                            ",".join(str(d) for d in sorted(set(info["render_dpi"]))[:3])))
        if spec.scale_mode == "stretch":
            notes.append("缩放方式：拉伸铺满（按版心比例非等比拉伸，可消除白边）")
        if pg_layout.has_crop(plan["crop"]):
            c = plan["crop"]
            notes.append("已裁剪：上 %g / 右 %g / 下 %g / 左 %g 毫米"
                         % (c["top"], c["right"], c["bottom"], c["left"]))
        if dec.active:
            bits = []
            if dec.wm_enabled and dec.wm_text.strip():
                bits.append("水印「%s」" % dec.wm_text.strip())
            if dec.pn_enabled:
                bits.append("页码（%s）" % dec.pn_position)
            if dec.hf_header.strip():
                bits.append("页眉")
            if dec.hf_footer.strip():
                bits.append("页脚")
            notes.append("已叠加 %s" % "、".join(bits))
        if spec.mirror:
            notes.append("已按竖直中线镜像（反片）")
    else:
        shutil.copyfile(norm, final)
        mode = "vector"
        notes.append("矢量直出：文字保持矢量，未经栅格化")

    out_count, _ = pdf_info(final)
    return {
        "pdf": final,
        "plan": plan,
        "mode": mode,
        "pages": out_count,
        "source_pages": total,
        "sheet": (round(plan["sheet_w"], 2), round(plan["sheet_h"], 2)),
        "notes": notes + [sheet_note],
        "info": info,
    }


# ------------------------------------------------------------------ 命令行自测
def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="打印引擎（可脱离 Web 单独调用）")
    ap.add_argument("--in", dest="src", required=True, help="源 PDF")
    ap.add_argument("--out", dest="out", required=True, help="最终 PDF")
    ap.add_argument("--spec", default="{}", help="设置 JSON")
    ap.add_argument("--preview-dir", default="", help="如果给出则同时出预览图")
    ap.add_argument("--print", dest="do_print", action="store_true", help="送打印队列")
    args = ap.parse_args(argv)

    spec = PrintSpec.from_form(json.loads(args.spec))
    spec.validate()
    workdir = tempfile.mkdtemp(prefix="gw-engine-")
    try:
        res = build_final(spec, args.src, workdir)
        shutil.copyfile(res["pdf"], args.out)
        print("模式: %s" % res["mode"])
        print("输出页数: %d（源 %d 页）" % (res["pages"], res["source_pages"]))
        print("纸面尺寸: %g x %g 点" % res["sheet"])
        for n in res["notes"]:
            print("  - %s" % n)
        if args.preview_dir:
            files = make_preview(res["pdf"], args.preview_dir)
            print("预览图: %d 张 -> %s" % (len(files), args.preview_dir))
        if args.do_print:
            print("已提交: %s" % send_to_printer(res["pdf"], spec, "engine-selftest"))
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
