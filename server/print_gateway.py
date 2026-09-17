"""
扫码打印网关 —— WPS 风格打印面板 + 服务端排版引擎。

职责划分
--------
* 本文件只做 HTTP、界面与作业管理；
* 真正的版面计算在 pg_layout.py，流水线编排在 pg_engine.py。
  换界面不用碰引擎，换引擎也不用碰界面。

接口
----
    GET  /                打印面板
    POST /api/upload      上传文件 -> 返回作业号、页数
    POST /api/preview     按设置生成最终 PDF 并渲染预览
    POST /api/print       按设置生成最终 PDF 并送队列
    GET  /img             取预览图
    GET  /api/printers    打印机列表
    GET  /api/job?id=     按作业号取作业信息（供 App 从「打开方式」直达设置页）
    GET  /healthz         自检
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pg_engine
from pg_engine import EngineError, PrintSpec

LOG = logging.getLogger("print-gateway")

MAX_UPLOAD_MB = 50
# 一次能选几个文件。批量打印的合理上限，同时也是请求体总量的倍数。
MAX_FILES = 10
# 界面版本号（显示在页面右上角）。改动前端时一并递增 ——
# 用户报「怎么改了没生效」时，第一件事就是看他看到的是哪个版本。
VERSION = "v3.1 0917"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
PDF_EXTS = {".pdf"}
JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
HASH_RE = re.compile(r"^[0-9a-f]{16}$")

# 预览结果缓存：避免每次动一个滑块就把整条流水线重跑一遍
_CACHE: dict[tuple[str, str], dict] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_LIMIT = 24


class Job:
    """一个上传作业。源文件落盘，衍生产物按「设置指纹」分目录。"""

    def __init__(self, root: str):
        self.id = uuid.uuid4().hex[:12]
        self.dir = os.path.join(root, self.id)
        os.makedirs(self.dir, exist_ok=True)
        self.source_pdf = ""
        self.filename = ""
        self.pages = 0
        # 上传时的文件个数。被 /?job= 直达的页面要用它复现「x 个文件」的提示，
        # 必须存在作业上 —— upload 接口能现算，直达入口拿不到原始 part 列表。
        self.files = 0
        self.sizes: list[tuple[float, float]] = []
        self.created = time.time()


class JobStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self) -> Job:
        job = Job(self.root)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        if not JOB_ID_RE.match(job_id or ""):
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def workdir_for(self, job: Job, key: str) -> str:
        d = os.path.join(job.dir, "build-" + key)
        os.makedirs(d, exist_ok=True)
        return d

    def purge_old(self, max_age: float = 6 * 3600) -> int:
        now = time.time()
        with self._lock:
            stale = [j for j in self._jobs.values() if now - j.created > max_age]
            for j in stale:
                self._jobs.pop(j.id, None)
                shutil.rmtree(j.dir, ignore_errors=True)
        if stale:
            LOG.info("清理过期作业 %d 个", len(stale))
        return len(stale)


# ------------------------------------------------------------------ 多部分表单
def parse_multipart(body: bytes, content_type: str) -> tuple[dict, list]:
    """
    极简 multipart/form-data 解析。返回 (字段字典, 文件列表)。

    boundary 可能被引号包裹（RFC 2046 允许，Safari 会这么发），
    所以先匹配带引号的形式，再退回不带引号的 —— 早期版本的正则把引号
    一起吃进了 boundary，导致这类请求静默解析失败。
    """
    fields: dict[str, str] = {}
    files: list[dict] = []
    if not content_type:
        return fields, files
    m = re.search(r'boundary="([^"]+)"', content_type) or \
        re.search(r"boundary=([^;]+)", content_type)
    if not m:
        return fields, files
    boundary = m.group(1).strip()
    if not boundary:
        return fields, files

    delim = b"--" + boundary.encode("latin-1")
    for chunk in body.split(delim):
        if not chunk or chunk in (b"--", b"--\r\n", b"\r\n"):
            continue
        chunk = chunk.lstrip(b"\r\n")
        head_end = chunk.find(b"\r\n\r\n")
        if head_end < 0:
            continue
        raw_head = chunk[:head_end].decode("utf-8", "replace")
        data = chunk[head_end + 4:]
        if data.endswith(b"\r\n"):
            data = data[:-2]

        name = ""
        filename = None
        ctype = ""
        for line in raw_head.split("\r\n"):
            low = line.lower()
            if low.startswith("content-disposition:"):
                for part in line.split(";")[1:]:
                    part = part.strip()
                    if part.startswith("name="):
                        name = part[5:].strip().strip('"')
                    elif part.startswith("filename="):
                        filename = part[9:].strip().strip('"')
            elif low.startswith("content-type:"):
                ctype = line.split(":", 1)[1].strip()
        if not name:
            continue
        if filename is not None:
            # 没选文件时浏览器仍会发一个空 part（filename 与内容都为空）。
            # 早先直接收下，结果上传接口回「文件 是空的」——用户明明选了文件。
            if filename and data:
                files.append({"name": name, "filename": filename,
                              "content_type": ctype, "data": data})
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


def image_to_pdf(src: str, dst: str) -> str:
    """图片转 PDF（按 150 DPI 自然尺寸出页）。"""
    try:
        from PIL import Image
    except ImportError as exc:
        raise EngineError("设备缺少 PIL，无法处理图片") from exc
    try:
        img = Image.open(src)
        frames = []
        if getattr(img, "n_frames", 1) > 1:
            for i in range(img.n_frames):
                img.seek(i)
                frames.append(img.convert("RGB"))
        else:
            im = img
            if im.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                conv = im.convert("RGBA")
                bg.paste(conv, mask=conv.split()[-1])
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            frames.append(im)
        frames[0].save(dst, "PDF", save_all=True, append_images=frames[1:],
                       resolution=150.0)
        return dst
    except Exception as exc:                                    # noqa: BLE001
        raise EngineError("图片转换失败：%s" % exc) from exc


# ------------------------------------------------------------------ 页面
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>扫码打印</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--line:#e3e5e8;--text:#1f2329;--muted:#8a9099;
--accent:#2b6de5;--accent-soft:#eef3fe;--warn:#b8730b;--err:#d2453c}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line);padding:12px 16px;
display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:20}
header h1{font-size:16px;font-weight:600;margin:0;flex:1}
header .badge{font-size:12px;color:var(--muted)}
.wrap{display:flex;gap:16px;padding:16px;align-items:flex-start;
max-width:1180px;margin:0 auto;flex-wrap:wrap}
.left{flex:1 1 320px;min-width:280px}
.right{flex:1 1 420px;min-width:300px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px;margin-bottom:12px}
.card h2{font-size:14px;font-weight:600;margin:0 0 10px;color:var(--text)}
.card h2 .sub{font-weight:400;color:var(--muted);font-size:12px;margin-left:6px}
.row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.row:last-child{margin-bottom:0}
.row>label{flex:0 0 84px;color:var(--muted);font-size:13px}
select,input[type=number],input[type=text]{flex:1;min-width:0;padding:7px 9px;
border:1px solid var(--line);border-radius:7px;background:#fff;font-size:14px;
color:var(--text);font-family:inherit}
input[type=number]{max-width:104px}
input[type=color]{flex:0 0 42px;height:32px;padding:2px;border:1px solid var(--line);
border-radius:7px;background:#fff;cursor:pointer}
.chk{display:flex;align-items:center;gap:8px;font-size:14px;flex:1}
.chk input{width:16px;height:16px;margin:0;accent-color:var(--accent)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px}
.drop{border:2px dashed var(--line);border-radius:12px;padding:28px 16px;text-align:center;
color:var(--muted);background:#fff;cursor:pointer;transition:.15s}
.drop.on{border-color:var(--accent);background:var(--accent-soft);color:var(--accent)}
.drop b{color:var(--text);font-weight:600}
.drop .hint{font-size:12px;margin-top:6px}
button{font:inherit;border-radius:8px;border:1px solid var(--line);background:#fff;
padding:9px 16px;cursor:pointer;color:var(--text)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;
font-weight:600;flex:1}
button:disabled{opacity:.5;cursor:not-allowed}
.pv{background:#eceef1;border-radius:8px;padding:10px;max-height:62vh;overflow:auto;
display:flex;flex-direction:column;gap:10px;align-items:center}
.pv img{width:100%;max-width:340px;border:1px solid #d8dade;background:#fff;
border-radius:4px;display:block}
.pv .empty{color:var(--muted);padding:36px 0;text-align:center;font-size:13px}
.pv .spin{color:var(--accent);font-size:13px;padding:36px 0}
.meta{font-size:12px;color:var(--muted);margin-top:8px;line-height:1.8}
.meta b{color:var(--text);font-weight:600}
.tag{display:inline-block;padding:1px 7px;border-radius:5px;font-size:12px;
background:var(--accent-soft);color:var(--accent);margin-right:6px}
.tag.warn{background:#fdf3e2;color:var(--warn)}
.msg{border-radius:8px;padding:9px 12px;font-size:13px;margin-bottom:10px;display:none}
.msg.show{display:block}
.msg.ok{background:#e9f7ee;color:#1d7a3d}
.msg.err{background:#fdeceb;color:var(--err)}
.file{font-size:13px;color:var(--muted);margin-top:10px;display:flex;
justify-content:space-between;gap:10px;align-items:center}
.file .n{color:var(--text);font-weight:600;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
details{border-top:1px solid var(--line);padding-top:10px;margin-top:10px}
details:first-child{border-top:0;padding-top:0;margin-top:0}
summary{cursor:pointer;font-weight:600;font-size:14px;list-style:none;
display:flex;justify-content:space-between;align-items:center}
summary::-webkit-details-marker{display:none}
summary:after{content:"\203A";color:var(--muted);display:inline-block;
transform:rotate(90deg);font-size:18px;line-height:1}
details[open] summary:after{transform:rotate(-90deg)}
details .body{padding-top:10px}
@media(max-width:820px){.left{flex:1 1 100%}.right{flex:1 1 100%}
.wrap{padding:12px}.pv{max-height:46vh}}
</style>
</head>
<body>
<header>
  <h1>扫码打印</h1>
  <span class="badge" id="hdr">等待上传</span>
  <span class="badge" id="ver">__VERSION__</span>
</header>
<div class="wrap">
  <div class="left">
    <div class="card">
      <h2>打印预览<span class="sub" id="pvsub"></span></h2>
      <div class="pv" id="pv"><div class="empty">上传文件后这里会显示打印效果</div></div>
      <div class="meta" id="meta"></div>
    </div>
  </div>
  <div class="right">
    <div class="msg" id="msg"></div>

    <div class="card" id="upcard">
      <h2>选择文件</h2>
      <div class="drop" id="drop">
        <b>点击选择</b> 或把文件拖到这里
        <div class="hint">支持 PDF 与图片（JPG / PNG / TIFF 等）<br>单文件不超过 __MAXMB__ MB，一次最多 __MAXFILES__ 个（按选择顺序合并）</div>
      </div>
      <input type="file" id="file" accept=".pdf,image/*" multiple hidden>
      <div class="file" id="fileInfo" style="display:none">
        <span class="n" id="fname"></span>
        <button id="reset">换一个</button>
      </div>
      <div class="hint" id="appWrap" style="display:none;margin-top:10px">
        <a id="appBtn" href="/app" style="display:inline-block;padding:7px 14px;
          border:1px solid #b6c2cc;border-radius:6px;color:#33414c;font-size:13px">
          下载安卓 App（从系统「打开方式」直接打印）</a>
      </div>
    </div>

    <div id="panel" style="display:none">
      <div class="card">
        <details open><summary>基础设置</summary><div class="body">
          <div class="row"><label>打印机</label><select id="printer" autocomplete="off"></select></div>
          <div class="row"><label>份数</label>
            <input type="number" id="copies" value="1" min="1" max="99">
            <span class="chk"><input type="checkbox" id="collate" checked><label for="collate">逐份打印</label></span>
          </div>
          <div class="row"><label>颜色</label>
            <span class="chk"><input type="checkbox" id="grayscale"><label for="grayscale">灰度打印</label></span>
          </div>
          <div class="row"><label>打印内容</label>
            <select id="content">
              <option value="document">仅文档</option>
              <option value="annotations">文档与注释</option>
            </select>
          </div>
        </div></details>

        <details open><summary>页面范围</summary><div class="body">
          <div class="row"><label>范围</label>
            <select id="rangeMode">
              <option value="all">全部页面</option>
              <option value="custom">指定页码</option>
            </select>
            <input type="text" id="pageRange" placeholder="如 1-3,5" style="display:none">
          </div>
          <div class="row"><label>奇偶页</label>
            <select id="pageSet">
              <option value="all">全部</option>
              <option value="odd">仅奇数页</option>
              <option value="even">仅偶数页</option>
            </select>
          </div>
          <div class="row"><label></label>
            <span class="chk"><input type="checkbox" id="reverse"><label for="reverse">反向打印（从末页到首页）</label></span>
          </div>
        </div></details>

        <details open><summary>打印方式</summary><div class="body">
          <div class="row"><label>每版页数</label>
            <select id="perSheet">
              <option value="1">1 版（普通）</option>
              <option value="2">2 版</option>
              <option value="4">4 版</option>
              <option value="6">6 版</option>
              <option value="9">9 版</option>
              <option value="16">16 版</option>
            </select>
          </div>
          <div class="row"><label>排列顺序</label>
            <select id="layoutOrder">
              <option value="lrtb">从左到右，从上到下</option>
              <option value="btlr">从下到上，从左到右</option>
              <option value="rlbt">从右到左，从上到下</option>
              <option value="tblr">从上到下，从左到右</option>
            </select>
          </div>
          <div class="row"><label>小册子</label>
            <span class="chk"><input type="checkbox" id="booklet"><label for="booklet">对折装订成册</label></span>
          </div>
          <div class="row" id="bkRow" style="display:none"><label>装订</label>
            <select id="bookletBinding">
              <option value="left">左侧装订</option>
              <option value="right">右侧装订</option>
            </select>
            <select id="bookletSubset">
              <option value="both">双面</option>
              <option value="front">仅正面</option>
              <option value="back">仅背面</option>
            </select>
          </div>
          <div class="row"><label>双面</label>
            <select id="duplex">
              <option value="one-sided">单面</option>
              <option value="two-sided-long-edge">双面 · 长边翻页</option>
              <option value="two-sided-short-edge">双面 · 短边翻页</option>
            </select>
          </div>
          <div class="grid2">
            <span class="chk"><input type="checkbox" id="border"><label for="border">版边框</label></span>
            <span class="chk"><input type="checkbox" id="mirror"><label for="mirror">反片 / 镜像</label></span>
          </div>
        </div></details>

        <details open><summary>页面设置</summary><div class="body">
          <div class="row"><label>纸张</label>
            <select id="paper">
              <option>A4</option><option>A3</option><option>A5</option>
              <option>B5</option><option>Letter</option><option>Legal</option>
            </select>
          </div>
          <div class="row"><label>方向</label>
            <select id="orientation">
              <option value="auto">自动</option>
              <option value="portrait">纵向</option>
              <option value="landscape">横向</option>
            </select>
          </div>
          <div class="row"><label>缩放</label>
            <select id="scaleMode">
              <option value="fit">适应纸张</option>
              <option value="stretch">拉伸铺满</option>
              <option value="none">不缩放</option>
            </select>
          </div>
          <div class="row"><label>页边距</label>
            <span class="chk" style="gap:6px;font-size:13px;color:var(--muted)">上</span>
            <input type="number" id="mTop" value="0" min="0" max="50" step="1">
            <span class="chk" style="gap:6px;font-size:13px;color:var(--muted)">下</span>
            <input type="number" id="mBottom" value="0" min="0" max="50" step="1">
          </div>
          <div class="row"><label></label>
            <span class="chk" style="gap:6px;font-size:13px;color:var(--muted)">左</span>
            <input type="number" id="mLeft" value="0" min="0" max="50" step="1">
            <span class="chk" style="gap:6px;font-size:13px;color:var(--muted)">右</span>
            <input type="number" id="mRight" value="0" min="0" max="50" step="1">
            <span style="font-size:12px;color:var(--muted)">毫米</span>
          </div>
        </div></details>

        <details><summary>水印与页码</summary><div class="body">
          <div class="row"><label>水印</label>
            <span class="chk"><input type="checkbox" id="wmEnabled"><label for="wmEnabled">叠加文字水印</label></span>
          </div>
          <div id="wmBox" style="display:none">
            <div class="row"><label>文字</label>
              <input type="text" id="wmText" value="机密" maxlength="40">
            </div>
            <div class="row"><label>字号</label>
              <input type="number" id="wmSize" value="60" min="6" max="300" step="2">
              <select id="wmOpacity">
                <option value="0.15">透明度 15%</option>
                <option value="0.25">透明度 25%</option>
                <option value="0.35" selected>透明度 35%</option>
                <option value="0.5">透明度 50%</option>
                <option value="0.7">透明度 70%</option>
              </select>
            </div>
            <div class="row"><label>角度</label>
              <select id="wmAngle">
                <option value="45" selected>45°（斜向）</option>
                <option value="30">30°</option>
                <option value="60">60°</option>
                <option value="0">水平</option>
                <option value="90">垂直</option>
                <option value="270">反向垂直</option>
              </select>
              <span class="chk"><input type="checkbox" id="wmTile" checked><label for="wmTile">平铺整页</label></span>
            </div>
            <div class="row"><label>颜色</label>
              <input type="color" id="wmColor" value="#b9bec6">
              <span style="font-size:12px;color:var(--muted)">建议用浅灰，别盖住正文</span>
            </div>
          </div>

          <div class="row"><label>页码</label>
            <span class="chk"><input type="checkbox" id="pnEnabled"><label for="pnEnabled">显示页码</label></span>
          </div>
          <div id="pnBox" style="display:none">
            <div class="row"><label>位置</label>
              <select id="pnPosition">
                <option value="bottom-center" selected>页脚 · 居中</option>
                <option value="bottom-left">页脚 · 居左</option>
                <option value="bottom-right">页脚 · 居右</option>
                <option value="top-center">页眉 · 居中</option>
                <option value="top-left">页眉 · 居左</option>
                <option value="top-right">页眉 · 居右</option>
              </select>
            </div>
            <div class="row"><label>格式</label>
              <select id="pnFormat">
                <option value="n-of-total" selected>1 / 8</option>
                <option value="n">1</option>
                <option value="page-n">第 1 页</option>
                <option value="page-n-of-total">第 1 页 / 共 8 页</option>
              </select>
              <input type="number" id="pnSize" value="10" min="6" max="72" step="1">
              <span style="font-size:12px;color:var(--muted)">pt</span>
            </div>
            <div class="row"><label>起始</label>
              <input type="number" id="pnStart" value="1" min="1" max="9999">
              <span class="chk"><input type="checkbox" id="pnSkipFirst"><label for="pnSkipFirst">首页不显示</label></span>
            </div>
          </div>

          <div class="row" style="margin-top:10px"><label>页眉</label>
            <input type="text" id="hfHeader" placeholder="留空则不显示，可用 {page} {total} {date}" maxlength="120">
          </div>
          <div class="row"><label>页脚</label>
            <input type="text" id="hfFooter" placeholder="留空则不显示" maxlength="120">
          </div>
          <div class="row" id="hfRow" style="display:none"><label>对齐</label>
            <select id="hfPosition">
              <option value="center" selected>居中</option>
              <option value="left">居左</option>
              <option value="right">居右</option>
            </select>
            <input type="number" id="hfSize" value="9" min="6" max="72" step="1">
            <span style="font-size:12px;color:var(--muted)">pt</span>
            <input type="number" id="hfMargin" value="10" min="0" max="40" step="1">
            <span style="font-size:12px;color:var(--muted)">距边 mm</span>
          </div>
          <div class="meta">页码按<b>打印顺序</b>编号，反向打印时第 1 张纸显示第 1 页。
            启用任一项后本作业改走栅格路径（等效 300 dpi），以保证预览与实印一致。</div>
        </div></details>

        <details><summary>裁剪与分割</summary><div class="body">
          <div class="row"><label>裁剪</label>
            <span style="font-size:13px;color:var(--muted)">上</span>
            <input type="number" id="cropTop" value="0" min="0" max="50" step="1">
            <span style="font-size:13px;color:var(--muted)">下</span>
            <input type="number" id="cropBottom" value="0" min="0" max="50" step="1">
          </div>
          <div class="row"><label></label>
            <span style="font-size:13px;color:var(--muted)">左</span>
            <input type="number" id="cropLeft" value="0" min="0" max="50" step="1">
            <span style="font-size:13px;color:var(--muted)">右</span>
            <input type="number" id="cropRight" value="0" min="0" max="50" step="1">
            <span style="font-size:12px;color:var(--muted)">毫米</span>
          </div>
          <div class="row"><label>分割打印</label>
            <select id="splitRows">
              <option value="1">1 行</option><option value="2">2 行</option>
              <option value="3">3 行</option><option value="4">4 行</option>
            </select>
            <select id="splitCols">
              <option value="1">1 列</option><option value="2">2 列</option>
              <option value="3">3 列</option><option value="4">4 列</option>
            </select>
          </div>
          <div class="meta" id="splitHint" style="display:none"></div>
          <div class="meta">分割打印把一页放大到多张纸，拼起来就是一张海报。
            每张纸独占一个瓦片，与「每版页数」「小册子」互斥。</div>
        </div></details>

        <details><summary>内容设置与画质</summary><div class="body">
          <div class="grid2">
            <span class="chk"><input type="checkbox" id="autoCenter" checked><label for="autoCenter">自动居中</label></span>
            <span class="chk"><input type="checkbox" id="autoRotate"><label for="autoRotate">自动旋转</label></span>
          </div>
          <div class="row" style="margin-top:10px"><label>分辨率</label>
            <select id="dpi">
              <option value="150">150 dpi（快）</option>
              <option value="200">200 dpi</option>
              <option value="300" selected>300 dpi（推荐）</option>
              <option value="600">600 dpi（最清晰，较慢）</option>
            </select>
          </div>
          <div class="meta">分辨率只在需要拼版时起作用。不拼版时保持矢量直出，与分辨率无关。</div>
        </div></details>
      </div>

      <div class="card" style="display:flex;gap:10px;align-items:center">
        <button class="primary" id="go">开始打印</button>
        <button id="refresh">刷新预览</button>
      </div>
    </div>
  </div>
</div>
<script>
var $ = function(s){ return document.querySelector(s); };
var JOB = null, TIMER = null;

// 地址查询参数：
//   ?t=   访问口令 —— 服务端用 --token 时，页面自己能打开不代表接口能调，
//         页面内所有 API 请求都得把这个口令带上，否则每个请求都 401。
//   ?job= 作业号 —— 外部客户端（安卓 App 从系统「打开方式」进来）已上传完毕，
//         页面据此直接进入设置界面，用户不必再选一次文件。
var QS = new URLSearchParams(location.search);
var TOKEN = QS.get('t') || '';
function api(p){
  if (!TOKEN) return p;
  return p + (p.indexOf('?') < 0 ? '?' : '&') + 't=' + encodeURIComponent(TOKEN);
}

function spec(){
  return {
    printer: $('#printer').value,
    copies: +$('#copies').value || 1,
    collate: $('#collate').checked,
    grayscale: $('#grayscale').checked,
    content: $('#content').value,
    page_range: $('#rangeMode').value === 'custom' ? $('#pageRange').value : '',
    page_set: $('#pageSet').value,
    reverse: $('#reverse').checked,
    per_sheet: +$('#perSheet').value,
    layout_order: $('#layoutOrder').value,
    booklet: $('#booklet').checked,
    booklet_binding: $('#bookletBinding').value,
    booklet_subset: $('#bookletSubset').value,
    border: $('#border').checked,
    mirror: $('#mirror').checked,
    duplex: $('#duplex').value,
    paper: $('#paper').value,
    orientation: $('#orientation').value,
    scale_mode: $('#scaleMode').value,
    margins: {top:+$('#mTop').value||0, bottom:+$('#mBottom').value||0,
              left:+$('#mLeft').value||0, right:+$('#mRight').value||0},
    auto_center: $('#autoCenter').checked,
    auto_rotate: $('#autoRotate').checked,
    crop_mm: {top:+$('#cropTop').value||0, right:+$('#cropRight').value||0,
              bottom:+$('#cropBottom').value||0, left:+$('#cropLeft').value||0},
    split_rows: +$('#splitRows').value,
    split_cols: +$('#splitCols').value,
    decor: {
      wm_enabled: $('#wmEnabled').checked,
      wm_text: $('#wmText').value,
      wm_size: +$('#wmSize').value || 60,
      wm_color: $('#wmColor').value,
      wm_opacity: +$('#wmOpacity').value,
      wm_angle: +$('#wmAngle').value,
      wm_tile: $('#wmTile').checked,
      pn_enabled: $('#pnEnabled').checked,
      pn_position: $('#pnPosition').value,
      pn_format: $('#pnFormat').value,
      pn_size: +$('#pnSize').value || 10,
      pn_start: +$('#pnStart').value || 1,
      pn_skip_first: $('#pnSkipFirst').checked,
      hf_header: $('#hfHeader').value,
      hf_footer: $('#hfFooter').value,
      hf_position: $('#hfPosition').value,
      hf_size: +$('#hfSize').value || 9,
      hf_margin_mm: +$('#hfMargin').value || 0
    },
    dpi: +$('#dpi').value
  };
}

function toast(text, kind){
  var m = $('#msg');
  m.textContent = text;
  m.className = 'msg show ' + (kind || 'ok');
  if (kind !== 'err') setTimeout(function(){ m.classList.remove('show'); }, 4500);
}
function fail(e){ toast(e && e.message ? e.message : String(e), 'err'); }

function post(url, data){
  return fetch(api(url), {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(data)}).then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(j){
        if (!r.ok || j.error) throw new Error(j.error || ('请求失败 ' + r.status));
        return j;
      });
    });
}
function esc(s){
  return String(s).replace(/[&<>"]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];
  });
}

function pickDefault(sel, name){
  // 用 selectedIndex 赋值而不是 option.selected：单选 select 上更直接可靠
  if (!name) return false;
  for (var i = 0; i < sel.options.length; i++){
    if (sel.options[i].value === name){ sel.selectedIndex = i; return true; }
  }
  return false;
}

function loadPrinters(){
  // cache:'no-store' —— 默认队列是实时读的，不能被 HTTP 缓存挡住
  fetch(api('/api/printers'), {cache: 'no-store'}).then(function(r){ return r.json(); }).then(function(j){
    var sel = $('#printer'), i, dq = j.defaultQueue || '';
    sel.innerHTML = '';
    if (!j.printers || !j.printers.length){
      sel.innerHTML = '<option value="">未发现打印队列</option>';
      return;
    }
    for (i = 0; i < j.printers.length; i++){
      var p = j.printers[i], o = document.createElement('option');
      var isDef = (p.name === dq);
      o.value = p.name;
      o.textContent = p.name + (isDef ? '（默认）' : '')
        + (p.state && p.state !== 'idle' ? '（' + p.state + '）' : '');
      sel.appendChild(o);
    }
    if (!pickDefault(sel, dq)) sel.selectedIndex = 0;
    // 浏览器会在刷新时「恢复上次的表单状态」，时机可能晚于上面这次赋值，
    // 把 select 悄悄改回旧值（用户就会看到上次选的队列而非 CUPS 默认）。
    // 下一帧再确认一次，把它掰回来。
    setTimeout(function(){ if (dq) pickDefault(sel, dq); }, 0);
  }).catch(function(){});
}

/**
 * 把一个作业变成当前工作对象并铺开设置界面。
 * 上传接口与 /?job= 直达两条入口共用，避免两处各写一遍而慢慢跑偏。
 */
function afterJob(j){
  JOB = j;
  $('#panel').style.display = '';
  $('#fileInfo').style.display = 'flex';
  $('#fname').textContent = j.filename;
  $('#hdr').textContent = j.pages + ' 页 · '
    + (j.files > 1 ? j.files + ' 个文件 · ' : '') + '已就绪';
  if (j.pages > 1) $('#pageRange').placeholder = '如 1-3,5（共 ' + j.pages + ' 页）';
  schedulePreview(0);
}

function upload(files){
  var fd = new FormData(), i;
  for (i = 0; i < files.length; i++) fd.append('file', files[i]);
  $('#pv').innerHTML = '<div class="spin">正在上传…</div>';
  return fetch(api('/api/upload'), {method:'POST', body: fd}).then(function(r){
    return r.json().then(function(j){
      if (!r.ok || j.error) throw new Error(j.error || '上传失败');
      afterJob(j);
    });
  });
}

function renderPreview(j){
  $('#pvsub').textContent = j.pages ? ('共 ' + j.pages + ' 张纸') : '';
  var pv = $('#pv');
  if (!j.images || !j.images.length){
    pv.innerHTML = '<div class="empty">没有可预览的页面</div>';
  } else {
    pv.innerHTML = '';
    j.images.forEach(function(src, i){
      var img = document.createElement('img');
      img.src = src; img.alt = '第 ' + (i + 1) + ' 张'; img.loading = 'lazy';
      pv.appendChild(img);
    });
  }
  var labels = {vector:'矢量直出', raster:'拼版处理', tile:'分割打印'};
  var m = '<span class="tag' + (j.mode === 'vector' ? '' : ' warn') + '">' +
    (labels[j.mode] || j.mode) + '</span>' +
    '源文件 <b>' + j.source_pages + '</b> 页 · 输出 <b>' + j.pages + '</b> 页　' +
    '纸面 <b>' + Math.round(j.sheet[0]) + ' × ' + Math.round(j.sheet[1]) + '</b> 点';
  if (j.notes && j.notes.length) m += '<br>' + j.notes.map(esc).join('　·　');
  if (j.truncated) m += '<br>仅预览前 ' + j.images.length + ' 张';
  $('#meta').innerHTML = m;
}

function schedulePreview(delay){
  clearTimeout(TIMER);
  TIMER = setTimeout(runPreview, delay === undefined ? 420 : delay);
}
function runPreview(){
  if (!JOB) return;
  $('#pv').innerHTML = '<div class="spin">正在生成预览…</div>';
  post('/api/preview', {job: JOB.id, spec: spec()})
    .then(renderPreview)
    .catch(function(e){
      $('#pv').innerHTML = '<div class="empty">预览失败</div>';
      fail(e);
    });
}

function syncUI(){
  var bk = $('#booklet').checked;
  var rows = +$('#splitRows').value, cols = +$('#splitCols').value;
  var sp = (rows > 1 || cols > 1);

  // 分割打印与拼版互斥：界面直接禁用比让用户设完再被忽略清楚得多
  // （引擎侧也会忽略，双保险）
  $('#booklet').disabled = sp;
  $('#perSheet').disabled = bk || sp;
  $('#layoutOrder').disabled = bk || sp;
  $('#bkRow').style.display = bk ? 'flex' : 'none';

  $('#wmBox').style.display = $('#wmEnabled').checked ? '' : 'none';
  $('#pnBox').style.display = $('#pnEnabled').checked ? '' : 'none';
  $('#hfRow').style.display =
    ($('#hfHeader').value.trim() || $('#hfFooter').value.trim()) ? 'flex' : 'none';

  var n = rows * cols;
  var hint = $('#splitHint');
  if (n > 1){
    hint.style.display = '';
    hint.textContent = '一页放大到 ' + n + ' 张纸（共 '
      + (JOB ? JOB.pages * n : n) + ' 张），拼起来即一张海报。';
  } else {
    hint.style.display = 'none';
  }

  $('#pageRange').style.display = $('#rangeMode').value === 'custom' ? '' : 'none';
}

function doPrint(){
  if (!JOB) return;
  if (!$('#printer').value){ fail(new Error('请先选择打印机')); return; }
  $('#go').disabled = true;
  $('#go').textContent = '正在提交…';
  post('/api/print', {job: JOB.id, spec: spec()}).then(function(j){
    toast('已提交到 ' + j.printer + '，任务号 ' + j.job + '，共 ' + j.pages + ' 页');
  }).catch(fail).then(function(){
    $('#go').disabled = false;
    $('#go').textContent = '开始打印';
  });
}

function bind(){
  var ctrl = ['printer','copies','collate','grayscale','content','rangeMode','pageRange',
    'pageSet','reverse','perSheet','layoutOrder','booklet','bookletBinding',
    'bookletSubset','border','mirror','duplex','paper','orientation','scaleMode',
    'mTop','mBottom','mLeft','mRight','autoCenter','autoRotate','dpi',
    'cropTop','cropBottom','cropLeft','cropRight','splitRows','splitCols',
    'wmEnabled','wmText','wmSize','wmColor','wmOpacity','wmAngle','wmTile',
    'pnEnabled','pnPosition','pnFormat','pnSize','pnStart','pnSkipFirst',
    'hfHeader','hfFooter','hfPosition','hfSize','hfMargin'];
  ctrl.forEach(function(id){
    var el = $('#' + id);
    if (!el) return;
    el.addEventListener('change', function(){ syncUI(); schedulePreview(); });
    if (el.tagName === 'INPUT' && (el.type === 'number' || el.type === 'text'))
      el.addEventListener('input', function(){ syncUI(); schedulePreview(650); });
  });
  $('#refresh').addEventListener('click', runPreview);
  $('#go').addEventListener('click', doPrint);
  $('#reset').addEventListener('click', function(){ location.reload(); });

  var drop = $('#drop'), inp = $('#file');
  drop.addEventListener('click', function(){ inp.click(); });
  inp.addEventListener('change', function(){
    if (inp.files.length) upload(inp.files).catch(fail);
  });
  ['dragenter','dragover'].forEach(function(ev){
    drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.add('on'); });
  });
  ['dragleave','drop'].forEach(function(ev){
    drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.remove('on'); });
  });
  drop.addEventListener('drop', function(e){
    var fs = e.dataTransfer.files;
    if (fs.length) upload(fs).catch(fail);
  });
}

window.addEventListener('pageshow', function(e){
  // 从「往返缓存」恢复的页面不会重跑 JS，默认队列可能已经变了，重拉一次
  if (e.persisted) { loadPrinters(); syncUI(); }
});

// 「下载 App」按钮：服务端配了 --apk 才显示（/healthz 的 app 键）
fetch(api('/healthz'), {cache: 'no-store'}).then(function(r){ return r.json(); })
  .then(function(j){ if (j && j.app) $('#appWrap').style.display = ''; })
  .catch(function(){});

loadPrinters();
bind();
syncUI();

// /?job=<id>：外部客户端（安卓 App 从系统「打开方式」进来）已经上传完了，
// 这里把作业取回来直接铺开设置界面，用户不必再选一次文件。
(function(){
  var jid = QS.get('job');
  if (!jid) return;
  $('#pv').innerHTML = '<div class="spin">正在载入作业…</div>';
  fetch(api('/api/job?id=' + encodeURIComponent(jid)), {cache:'no-store'})
    .then(function(r){ return r.json().then(function(j){
      if (!r.ok || j.error) throw new Error(j.error || '作业不存在');
      afterJob(j);
    }); })
    .catch(function(e){
      $('#pv').innerHTML = '<div class="empty">作业已失效，请重新选择文件</div>';
      fail(e);
    });
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "PrintGateway/2.0"
    protocol_version = "HTTP/1.1"
    store: JobStore = None                       # type: ignore[assignment]
    default_queue: str = ""
    # --printer 显式指定时置真：此时不再跟随 CUPS 系统默认队列
    printer_locked: bool = False
    token: str = ""
    host_display: str = ""
    # 可下载的安卓 App 包（--apk 指定；不存在则 /app 返回 404、healthz 报 app:false）
    apk_path: str = ""
    apk_name: str = "print-selfservice.apk"

    # -------------------------------------------------------------- 工具
    def log_message(self, fmt, *args):
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _send_file(self, path: str, extra: dict | None = None):
        """
        文件流式响应（不整包读进内存）。
        与 _send() 的区别：先写完全部头再分块写 body，Content-Length
        取自 os.stat —— APK 是几 MB 的包，分块比 3MB+ 堆内 buffer 干净。
        """
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.android.package-archive")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % self.apk_name)
        # 别缓存 APK：换了包（比如升到 v2）手机要立刻拿到新的
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _err(self, msg: str, code: int = 400):
        self._json({"error": str(msg)}, code)

    def _body(self, limit: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > limit:
            raise EngineError("内容过大（上限 %d MB）" % (limit // (1024 * 1024)))
        return self.rfile.read(length)

    def _json_body(self) -> dict:
        raw = self._body(4 * 1024 * 1024)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise EngineError("请求内容不是合法 JSON") from exc

    def _authed(self) -> bool:
        if not self.token:
            return True
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        given = self.headers.get("X-Token") or (q.get("t") or [""])[0]
        return secrets.compare_digest(given, self.token)

    # -------------------------------------------------------------- 路由
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                body = (PAGE.replace("__MAXMB__", str(MAX_UPLOAD_MB))
                .replace("__MAXFILES__", str(MAX_FILES))
                .replace("__VERSION__", VERSION).encode("utf-8"))
                # 必须显式 no-store：否则手机浏览器会缓存整页，用户拿到的是
                # 旧版界面（旧 JS 只选「列表第一项」），表现为「改了部署没生效」
                return self._send(200, body, "text/html; charset=utf-8",
                                  {"Cache-Control": "no-store, no-cache, "
                                                    "must-revalidate"})
            if path == "/healthz":
                return self._json({"ok": True, "service": "print-gateway",
                                   "version": "2.0",
                                   "printers": len(pg_engine.list_printers()),
                                   "app": bool(self.apk_path and
                                               os.path.exists(self.apk_path))})
            if path == "/app":
                # App 包下载：放在鉴权检查之前 —— 手机还没装 App、
                # 也没有口令，是扫码进来的用户要下载它
                if not (self.apk_path and os.path.exists(self.apk_path)):
                    return self._err("未配置 App 包（部署时加 --apk）", 404)
                return self._send_file(self.apk_path)
            if not self._authed():
                return self._err("未授权", 401)
            if path == "/api/printers":
                return self._json(self._printers_payload())
            if path == "/api/job":
                return self._api_job()
            if path == "/img":
                return self._serve_image()
            return self._err("未找到", 404)
        except EngineError as exc:
            self._err(exc)
        except Exception as exc:                              # noqa: BLE001
            LOG.exception("GET %s 失败", path)
            self._err("服务器内部错误：%s" % exc, 500)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._authed():
            return self._err("未授权", 401)
        try:
            if path == "/api/upload":
                return self._api_upload()
            if path == "/api/preview":
                return self._api_preview()
            if path == "/api/print":
                return self._api_print()
            return self._err("未找到", 404)
        except EngineError as exc:
            LOG.warning("POST %s: %s", path, exc)
            self._err(exc)
        except Exception as exc:                              # noqa: BLE001
            LOG.exception("POST %s 失败", path)
            self._err("服务器内部错误：%s" % exc, 500)

    # -------------------------------------------------------------- 接口
    def _api_upload(self):
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            return self._err("请以表单方式上传文件")
        _, files = parse_multipart(
            self._body(MAX_UPLOAD_MB * 1024 * 1024 * MAX_FILES + (1 << 20)), ctype)
        if not files:
            return self._err("没有收到文件")
        if len(files) > MAX_FILES:
            return self._err("一次最多上传 %d 个文件" % MAX_FILES)

        # 先全部校验再落盘 —— 中途失败时不会留下半个作业
        items = []
        for f in files:
            name = os.path.basename(f["filename"] or "upload")
            ext = os.path.splitext(name)[1].lower()
            if ext not in PDF_EXTS and ext not in IMAGE_EXTS:
                return self._err("不支持的文件类型：%s（仅支持 PDF 与常见图片）"
                                 % (ext or name))
            if not f["data"]:
                return self._err("文件 %s 是空的" % name)
            if len(f["data"]) > MAX_UPLOAD_MB * 1024 * 1024:
                return self._err("文件 %s 超过 %d MB" % (name, MAX_UPLOAD_MB))
            items.append((name, ext, f["data"]))

        job = self.store.create()
        total_bytes = sum(len(d) for _, _, d in items)
        try:
            parts = []
            for i, (name, ext, data) in enumerate(items):
                raw = os.path.join(job.dir, "part%02d%s" % (i, ext))
                with open(raw, "wb") as fh:
                    fh.write(data)
                one = os.path.join(job.dir, "part%02d.pdf" % i)
                if ext in IMAGE_EXTS:
                    image_to_pdf(raw, one)
                elif os.path.realpath(raw) != os.path.realpath(one):
                    # 上传的就是 PDF 时 raw 与 one 同名，不能自我复制
                    shutil.copyfile(raw, one)
                parts.append(one)

            target = os.path.join(job.dir, "source.pdf")
            pg_engine.merge_pdfs(parts, target)
            pages, sizes = pg_engine.pdf_info(target)
        except (EngineError, OSError) as exc:
            shutil.rmtree(job.dir, ignore_errors=True)
            return self._err("文件无法解析：%s" % exc)

        job.filename = (items[0][0] if len(items) == 1
                        else "%s 等 %d 个文件" % (items[0][0], len(items)))
        job.source_pdf = target
        job.pages = pages
        job.files = len(items)
        job.sizes = sizes
        LOG.info("上传 %s（%d 个文件，共 %d 页，%d 字节）-> 作业 %s",
                 job.filename, len(items), pages, total_bytes, job.id)
        return self._json({"id": job.id, "filename": job.filename, "pages": pages,
                           "files": len(items), "size": total_bytes})

    def _printers_payload(self) -> dict:
        """
        队列列表 + 应默认选中的队列。

        未被 --printer 锁定时**实时**读 CUPS 系统默认队列，因此在 CUPS 里
        改了默认打印机，用户刷新面板即可生效，无需重启本服务。
        优先级：CUPS 系统默认 > 启动时定下的 default_queue > 列表第一项。
        """
        printers = pg_engine.list_printers()
        names = [p["name"] for p in printers]
        if self.printer_locked:
            return {"printers": printers, "defaultQueue": self.default_queue}
        dq = ""
        cups_def = pg_engine.cups_default_printer()
        if cups_def and cups_def in names:
            dq = cups_def
        elif self.default_queue and self.default_queue in names:
            dq = self.default_queue
        elif names:
            dq = names[0]
        return {"printers": printers, "defaultQueue": dq}

    def _load_spec(self, payload: dict) -> PrintSpec:
        spec = PrintSpec.from_form(payload.get("spec") or {})
        if not spec.printer:
            spec.printer = self._printers_payload().get("defaultQueue") or ""
        spec.validate()
        return spec

    def _build(self, job: Job, spec: PrintSpec, with_preview: bool) -> dict:
        key = spec.cache_key()
        cached = _CACHE.get((job.id, key))
        if cached and os.path.exists(cached["pdf"]) and \
                (not with_preview or cached.get("images")):
            return cached

        work = self.store.workdir_for(job, key)
        res = pg_engine.build_final(spec, job.source_pdf, work)
        entry = {"pdf": res["pdf"], "mode": res["mode"], "pages": res["pages"],
                 "source_pages": res["source_pages"], "sheet": res["sheet"],
                 "notes": res["notes"], "images": []}
        if with_preview:
            entry["images"] = pg_engine.make_preview(res["pdf"], work)
        with _CACHE_LOCK:
            _CACHE[(job.id, key)] = entry
            while len(_CACHE) > _CACHE_LIMIT:
                _CACHE.pop(next(iter(_CACHE)))
        return entry

    def _preview_urls(self, job: Job, key: str, entry: dict) -> list[str]:
        urls = []
        for path in entry["images"]:
            m = re.search(r"(\d+)\.png$", path)
            if m:
                urls.append("/img?job=%s&k=%s&n=%s" % (job.id, key, m.group(1)))
        return urls

    def _api_job(self):
        """
        按作业号复现上传接口的返回。

        供「从系统『打开方式』进来」的客户端使用：它自己上传完只拿到 id，
        页面需要用这里补回文件名与页数，才能直接渲染出设置界面。
        """
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        job = self.store.get((q.get("id") or [""])[0])
        if not job:
            return self._err("作业不存在或已过期，请重新上传")
        return self._json({"id": job.id, "filename": job.filename,
                           "pages": job.pages, "files": job.files})

    def _api_preview(self):
        payload = self._json_body()
        job = self.store.get(payload.get("job", ""))
        if not job:
            return self._err("作业不存在或已过期，请重新上传")
        spec = self._load_spec(payload)
        entry = self._build(job, spec, with_preview=True)
        return self._json({
            "mode": entry["mode"], "pages": entry["pages"],
            "source_pages": entry["source_pages"], "sheet": list(entry["sheet"]),
            "notes": entry["notes"],
            "images": self._preview_urls(job, spec.cache_key(), entry),
            "truncated": entry["pages"] > len(entry["images"]),
        })

    def _api_print(self):
        payload = self._json_body()
        job = self.store.get(payload.get("job", ""))
        if not job:
            return self._err("作业不存在或已过期，请重新上传")
        spec = self._load_spec(payload)
        entry = self._build(job, spec, with_preview=False)
        queue_job = pg_engine.send_to_printer(entry["pdf"], spec, job.filename)
        LOG.info("作业 %s 已提交：%s -> %s（%s，%d 页）",
                 job.id, queue_job, spec.printer, entry["mode"], entry["pages"])
        return self._json({"job": queue_job, "printer": spec.printer,
                           "pages": entry["pages"], "mode": entry["mode"]})

    def _serve_image(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        job = self.store.get((q.get("job") or [""])[0])
        key = (q.get("k") or [""])[0]
        if not job or not HASH_RE.match(key):
            return self._err("无效的预览请求", 404)
        try:
            n = int((q.get("n") or ["0"])[0])
        except ValueError:
            return self._err("无效的页码", 404)

        path = None
        entry = _CACHE.get((job.id, key))
        for p in (entry or {}).get("images", []):
            if re.search(r"[-/]0*%d\.png$" % n, p):
                path = p
                break
        if not path:
            cand = os.path.join(job.dir, "build-" + key, "preview-%d.png" % n)
            path = cand if os.path.exists(cand) else None
        if not path or not os.path.exists(path):
            return self._err("预览图不存在", 404)
        # 只允许读取本作业目录下的文件
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(job.dir) + os.sep):
            return self._err("非法路径", 403)
        try:
            with open(real, "rb") as fh:
                data = fh.read()
        except OSError:
            return self._err("预览图读取失败", 500)
        return self._send(200, data, "image/png", {"Cache-Control": "private, max-age=300"})


def main(argv: list[str] | None = None) -> int:
    global MAX_UPLOAD_MB

    ap = argparse.ArgumentParser(description="扫码打印网关")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--printer", default="",
                    help="锁定默认打印队列；留空则跟随 CUPS 系统默认打印机")
    ap.add_argument("--token", default="", help="可选访问口令，留空则免密")
    ap.add_argument("--spool", default="/var/spool/print-gateway")
    ap.add_argument("--title", default="扫码打印")
    ap.add_argument("--default-dpi", type=int, default=pg_engine.DEFAULT_DPI)
    ap.add_argument("--max-mb", type=int, default=MAX_UPLOAD_MB,
                    help="单文件上传上限（MB）")
    ap.add_argument("--host-display", default="",
                    help="界面上展示的访问地址，留空自动探测")
    ap.add_argument("--apk", default="",
                    help="安卓 App 包路径；配置后 GET /app 下载，未配置则 /app 404")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    MAX_UPLOAD_MB = max(1, int(args.max_mb))

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    missing = [b for b in ("gs", "pdftoppm", "pdfinfo", "pdfseparate", "pdfunite")
               if not pg_engine.which(b)]
    if missing:
        LOG.error("缺少必需程序：%s", " ".join(missing))
        return 2
    try:
        import reportlab                                          # noqa: F401
    except ImportError:
        LOG.error("缺少 reportlab（拼版需要）：apt-get install -y python3-reportlab")
        return 2

    Handler.store = JobStore(args.spool)
    Handler.default_queue = args.printer
    # 显式 --printer 视为锁定：不再跟随 CUPS 默认。
    Handler.printer_locked = bool(args.printer)
    Handler.token = args.token
    pg_engine.DEFAULT_DPI = args.default_dpi

    # 安卓 App 包下载端点。缺失只告警不致命 —— 网关本身可继续服务，
    # /app 会返回 404，healthz 的 app 键如实报告。
    Handler.apk_path = args.apk
    if args.apk:
        Handler.apk_name = os.path.basename(args.apk)
        if not os.path.exists(args.apk):
            LOG.warning("--apk 指向的文件不存在：%s（/app 将返回 404）", args.apk)

    printers = pg_engine.list_printers()
    names = [p["name"] for p in printers]
    LOG.info("发现 %d 个打印队列：%s", len(printers),
             "、".join(names) or "无")
    if not Handler.default_queue:
        cups_def = pg_engine.cups_default_printer()
        if cups_def and cups_def in names:
            Handler.default_queue = cups_def
        elif cups_def:
            LOG.warning("CUPS 默认队列 %s 不在可用队列里，忽略", cups_def)
    if not Handler.default_queue and names:
        Handler.default_queue = names[0]
    if Handler.default_queue:
        LOG.info("默认队列：%s%s", Handler.default_queue,
                 "（--printer 锁定）" if Handler.printer_locked else "（跟随 CUPS 默认）")

    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    httpd.request_queue_size = 64          # 默认只有 5，多台手机同时上传会被拒
    httpd.daemon_threads = True
    try:
        host = args.host_display or socket.gethostbyname(socket.gethostname())
    except OSError:
        host = args.host_display or args.bind
    Handler.host_display = host
    LOG.info("%s 已启动：http://%s:%d", args.title, host, args.port)
    LOG.info("上传上限 %d MB，预览 %d dpi，默认打印 %d dpi",
             MAX_UPLOAD_MB, pg_engine.PREVIEW_DPI, pg_engine.DEFAULT_DPI)

    def reaper():
        while True:
            time.sleep(1800)
            try:
                Handler.store.purge_old()
            except Exception:                                     # noqa: BLE001
                LOG.exception("清理作业时出错")

    threading.Thread(target=reaper, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("收到中断，退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
