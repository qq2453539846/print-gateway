"""
扫码打印网关 —— 网页打印面板 + 服务端排版引擎。

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
    GET  /admin           管理页（域名 / 凭据 / 证书；**仅限内网**）

TLS 与公网
----------
`--tls-port` 指定时才开 TLS 监听（证书缺失则不监听、只告警）；明文端口与 TLS 端口
可以并存 —— 内网继续走 8080 明文扫码即用，公网走 8443 TLS。
**启用 TLS 端口时强制要求 `--token`**：宁可拒绝启动，也不允许出现「公网免密打印」。

`--public-url` 给「TLS 在**别处**终结」的部署用（第三方内网穿透 / 反向代理，如 DDNSTO）：
此时本机不开 TLS，但公网链接仍然存在，二维码贴纸上的「公网码」要指向隧道域名。
**它同样强制要求 `--token`** —— 判据是「这条链接公网可达吗」，而不是
「TLS 在谁的机器上终结」。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pg_admin
import pg_engine
import pg_layout
import pg_sticker
from pg_engine import EngineError, PrintSpec

LOG = logging.getLogger("print-gateway")

MAX_UPLOAD_MB = 50
# 一次能选几个文件。批量打印的合理上限，同时也是请求体总量的倍数。
MAX_FILES = 10
# 界面版本号（显示在页面右上角）。改动前端时一并递增 ——
# 用户报「怎么改了没生效」时，第一件事就是看他看到的是哪个版本。
VERSION = "v3.12.0 0920"
# 二维码贴纸的版式名（一页印几张）
_LAYOUT_LABEL = {1: "整页 1 张", 2: "A5 两张", 4: "A6 四张"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
PDF_EXTS = {".pdf"}
JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
HASH_RE = re.compile(r"^[0-9a-f]{16}$")

# 单条连接的 TLS 握手必须限时。公网端口常年被扫描器敲门，而扫描器最典型的动作
# 就是「TCP 连上就不说话」——没有超时的话它会一直占着处理它的那个线程不放。
TLS_HANDSHAKE_TIMEOUT = 8.0
# 监听队列长度。**必须是类属性**：TCPServer.__init__ 里已经 listen() 过了，
# 构造之后再改实例属性对已 listen 的套接字毫无作用（实测 Send-Q 恒为默认的 5）。
SERVER_BACKLOG = 128

# 预览结果缓存：避免每次动一个滑块就把整条流水线重跑一遍
_CACHE: dict[tuple[str, str], dict] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_LIMIT = 24

# 预览档与出纸档分开缓存/落盘。键上挂个后缀而不是新开一张表 ——
# 后缀只存在于服务端内部，URL 里的 k 仍是 spec.cache_key()（必须是纯 16 位十六进制，
# 见 HASH_RE），这样前端和 /img 的一整套约定都不用动。
_PV = "~pv"


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


# ------------------------------------------------------------------ 小册子示意
def booklet_summary(pages: int) -> dict:
    """
    面板上那枚「小册子缩略示意」需要的最小数据。

    只做配对，不做任何渲染 —— 走的是与实印同一个 pg_layout.booklet_sides()，
    所以图上的页号不可能和真印出来的结果打架（这是它敢自称「示意」的前提）。

    配对一律按**左装订**算：右装订只是把每面的左右两页对调，前端照着翻一下
    即可，省得两端各写一份配对公式（这种重复迟早会跑偏）。
    """
    if pages <= 0:
        return {"padded": 0, "sheets": 0, "blank": 0, "faces": []}
    padded = ((pages + 3) // 4) * 4
    faces = pg_layout.booklet_sides(pages, "left", "both")[:2]
    return {
        "padded": padded,
        "sheets": padded // 4,
        "blank": padded - pages,
        "faces": [{"sheet": f["sheet"], "kind": f["kind"],
                   "left": f["left"], "right": f["right"]} for f in faces],
    }


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
.bk{display:flex;gap:8px;flex:1}
.bkbtn{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;
padding:6px 2px 5px;border:1px solid var(--line);border-radius:8px;background:#fff;
cursor:pointer;user-select:none;transition:.15s}
.bkbtn svg{display:block;width:100%;max-width:52px;height:auto}
.bkbtn span{font-size:12px;color:var(--muted);line-height:1.2}
.bkbtn.on{border-color:var(--accent);background:var(--accent-soft)}
.bkbtn.on span{color:var(--accent)}
.bkbtn:active{transform:scale(.97)}
.bkthumb{display:flex;align-items:center;gap:12px;flex:1;flex-wrap:wrap}
.bkthumb svg{display:block;border:1px solid var(--line);border-radius:6px;background:#fff}
.bksum{font-size:12px;color:var(--muted);line-height:1.7}
.bksum b{color:var(--text);font-weight:600}
@media(max-width:820px){.left{flex:1 1 100%}.right{flex:1 1 100%}
.wrap{padding:12px}.pv{max-height:46vh}}
@media(max-width:400px){.row>label{flex:0 0 64px;font-size:12px}
.bk{gap:5px}.bkbtn{padding:5px 1px 4px}.bkbtn span{font-size:11px}}
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
          <!-- 参数仍是 bookletBinding / bookletSubset 两个值，只是换了呈现方式：
               图标按钮比下拉更容易看懂「哪边装订」「哪些面会印」。 -->
          <input type="hidden" id="bookletBinding" value="left">
          <input type="hidden" id="bookletSubset" value="both">
          <div class="row" id="bkRow" style="display:none"><label>装订方向</label>
            <div class="bk">
              <div class="bkbtn" data-for="bookletBinding" data-val="left">
                <svg width="52" height="34" viewBox="0 0 52 34" aria-hidden="true">
                  <rect x="2" y="2" width="48" height="30" rx="2.5"
                        fill="var(--accent-soft)" stroke="var(--accent)"/>
                  <line x1="26" y1="5" x2="26" y2="29" stroke="#a9b0b8" stroke-dasharray="3 2"/>
                  <rect x="2" y="2" width="3.5" height="30" rx="1.5" fill="var(--err)"/>
                </svg>
                <span>左侧装订</span>
              </div>
              <div class="bkbtn" data-for="bookletBinding" data-val="right">
                <svg width="52" height="34" viewBox="0 0 52 34" aria-hidden="true">
                  <rect x="2" y="2" width="48" height="30" rx="2.5"
                        fill="var(--accent-soft)" stroke="var(--accent)"/>
                  <line x1="26" y1="5" x2="26" y2="29" stroke="#a9b0b8" stroke-dasharray="3 2"/>
                  <rect x="46.5" y="2" width="3.5" height="30" rx="1.5" fill="var(--err)"/>
                </svg>
                <span>右侧装订</span>
              </div>
            </div>
          </div>
          <div class="row" id="bkRow2" style="display:none"><label>打印面</label>
            <div class="bk">
              <div class="bkbtn" data-for="bookletSubset" data-val="both">
                <svg width="52" height="34" viewBox="0 0 52 34" aria-hidden="true">
                  <rect x="10" y="2" width="40" height="26" rx="2.5"
                        fill="var(--accent-soft)" stroke="var(--accent)"/>
                  <rect x="2" y="7" width="40" height="26" rx="2.5"
                        fill="var(--accent-soft)" stroke="var(--accent)"/>
                </svg>
                <span>双面</span>
              </div>
              <div class="bkbtn" data-for="bookletSubset" data-val="front">
                <svg width="52" height="34" viewBox="0 0 52 34" aria-hidden="true">
                  <rect x="10" y="2" width="40" height="26" rx="2.5"
                        fill="none" stroke="#c3c8ce" stroke-dasharray="3 2"/>
                  <rect x="2" y="7" width="40" height="26" rx="2.5"
                        fill="var(--accent-soft)" stroke="var(--accent)"/>
                </svg>
                <span>仅正面</span>
              </div>
              <div class="bkbtn" data-for="bookletSubset" data-val="back">
                <svg width="52" height="34" viewBox="0 0 52 34" aria-hidden="true">
                  <rect x="2" y="2" width="40" height="26" rx="2.5"
                        fill="none" stroke="#c3c8ce" stroke-dasharray="3 2"/>
                  <rect x="10" y="7" width="40" height="26" rx="2.5"
                        fill="var(--accent-soft)" stroke="var(--accent)"/>
                </svg>
                <span>仅背面</span>
              </div>
            </div>
          </div>
          <div class="row" id="bkThumbRow" style="display:none"><label>示意</label>
            <div class="bkthumb">
              <div id="bkSvg"></div>
              <div class="bksum" id="bkSum"></div>
            </div>
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
  bkPaint();                       // 上传接口已带回页数与配对，无需等预览
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
  if (j.booklet) JOB.booklet = j.booklet;      // 预览是权威（含页范围后的真实页数）
  $('#pvsub').textContent = j.pages ? ('共 ' + j.pages + ' 张纸') : '';
  var pv = $('#pv');
  if (!j.images || !j.images.length){
    pv.innerHTML = '<div class="empty">没有可预览的页面</div>';
  } else {
    pv.innerHTML = '';
    j.images.forEach(function(src, i){
      var img = document.createElement('img');
      // 预览图 URL 是 /img?…，它同样在鉴权之后 —— 必须走 api() 补口令，
      // 否则公网端口整片裂图（服务端虽然已经拼了一次，这里是第二道保险，
      // 重复带同一个 t= 无害）。别改回 img.src = src。
      img.src = api(src); img.alt = '第 ' + (i + 1) + ' 张'; img.loading = 'lazy';
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
  bkPaint();
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

// ---- 小册子图标示意 ---------------------------------------------------
/**
 * 页号来自服务端的 booklet 摘要（与实印是同一个 booklet_sides()），
 * 这里只做两件纯显示的事：右装订把左右对调、按正/背面挑一张。
 * 配对公式一概不在前端重算 —— 那份重复迟早会和后端跑偏。
 */
function bkCell(cx, v){
  if (v === null || v === undefined)
    return '<text x="' + cx + '" y="58" text-anchor="middle" font-size="13"'
      + ' fill="#8a9099">空白</text>';
  return '<text x="' + cx + '" y="58" text-anchor="middle" font-size="24"'
    + ' fill="#1f2329">' + v + '</text>';
}

/**
 * 小册子的双面档位由几何决定，界面直接锁死它 —— 让用户能选却又不生效，
 * 比没有这个控件更糟（见 pitfalls 里同类教训）。规则与
 * pg_engine.PrintSpec.validate() 的归一化必须一致，改一处要同时改两处。
 * 取消勾选后还原用户原来的选择，别把「短边翻页」留给下一次普通打印。
 */
var bkDupSaved = '';
function bkDuplex(){
  var on = $('#booklet').checked;
  var ori = $('#orientation');
  if (ori) ori.disabled = on;          // 方向对小册子无效：横向是几何前提
  var sel = $('#duplex');
  if (!sel) return;
  if (on){
    if (!bkDupSaved) bkDupSaved = sel.value;
    var both = ($('#bookletSubset').value === 'both');
    sel.value = both ? 'two-sided-short-edge' : 'one-sided';
    sel.disabled = true;
    sel.title = both
      ? '小册子横向对折：折线是竖直的，翻面轴必须与它平行，只能短边翻页'
      : '只印单面（手动双面）：两次各印一遍，正反面不会互相占位';
  } else if (bkDupSaved){
    sel.value = bkDupSaved;
    sel.disabled = false;
    sel.title = '';
    bkDupSaved = '';
  }
}

function bkPaint(){
  bkDuplex();
  var on = $('#booklet').checked;
  $('#bkRow').style.display = on ? 'flex' : 'none';
  $('#bkRow2').style.display = on ? 'flex' : 'none';
  $('#bkThumbRow').style.display = on ? 'flex' : 'none';

  var binding = $('#bookletBinding').value, subset = $('#bookletSubset').value;
  [].forEach.call(document.querySelectorAll('.bkbtn'), function(b){
    b.classList.toggle('on',
      $('#' + b.getAttribute('data-for')).value === b.getAttribute('data-val'));
  });
  if (!on) return;

  var bk = (JOB && JOB.booklet) || null;
  var box = $('#bkSvg'), sum = $('#bkSum');
  if (!bk || !bk.faces || !bk.faces.length){
    box.innerHTML = ''; sum.textContent = '等待页数…'; return;
  }

  var want = (subset === 'back') ? 'back' : 'front', face = null;
  bk.faces.forEach(function(f){ if (!face && f.kind === want) face = f; });
  if (!face) face = bk.faces[0];

  var l = face.left, r = face.right;
  if (binding === 'right'){ var t = l; l = r; r = t; }

  var bar = (binding === 'right') ? 146 : 3;
  box.innerHTML =
    '<svg width="150" height="104" viewBox="0 0 150 104">'
    + '<rect x="9" y="9" width="61" height="86" rx="4" fill="var(--accent-soft)"'
    + ' stroke="var(--accent)" stroke-width="0.5"/>'
    + '<rect x="80" y="9" width="61" height="86" rx="4" fill="var(--accent-soft)"'
    + ' stroke="var(--accent)" stroke-width="0.5"/>'
    + '<line x1="75" y1="9" x2="75" y2="95" stroke="#a9b0b8" stroke-dasharray="4 3"/>'
    + '<rect x="' + bar + '" y="9" width="3" height="86" rx="1.5" fill="var(--err)"/>'
    + bkCell(39.5, l) + bkCell(110.5, r)
    + '</svg>';

  var s = '<b>第 ' + face.sheet + ' 张 · '
    + (face.kind === 'back' ? '背面' : '正面') + '</b><br>'
    + '共 ' + bk.sheets + ' 张纸 · ' + bk.padded + ' 页';
  s += '<br>横向对折 · ' + (subset === 'both'
    ? '双面·短边翻页'
    : '单面（' + (subset === 'front' ? '仅正面' : '仅背面') + '）');
  if (bk.blank > 0) s += '<br>末尾补 ' + bk.blank + ' 页空白';
  sum.innerHTML = s;
}

function bkPick(group, val){
  var sel = $('#' + group);
  if (sel.value !== val){
    sel.value = val;
    sel.dispatchEvent(new Event('change'));       // 复用已有的 change 监听
  } else {
    bkPaint();
  }
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
  bkPaint();                       // 小册子那几行 + 缩略示意一起刷新
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
  // 小册子那两组图标按钮：写回隐藏输入框，再走与下拉完全相同的 change 通路
  [].forEach.call(document.querySelectorAll('.bkbtn'), function(b){
    b.addEventListener('click', function(){
      bkPick(b.getAttribute('data-for'), b.getAttribute('data-val'));
    });
  });
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
    # 让明文端口也校验口令（默认否：内网扫码即用是既定体验）
    token_always: bool = False
    host_display: str = ""
    # 可下载的安卓 App 包（--apk 指定；不存在则 /app 返回 404、healthz 报 app:false）
    apk_path: str = ""
    apk_name: str = "print-selfservice.apk"
    # 管理页口令。**留空 = 整个 admin 功能关闭**，而不是「无口令可进」——
    # 一个默认敞开的管理页比没有管理页危险得多。
    admin_token: str = ""
    # 供 /admin 页面显示 TLS 现状
    tls_enabled: bool = False
    tls_port: int = 0
    # 外部可达的完整基地址（--public-url）。给「TLS 在别处终结」的部署用
    # （第三方内网穿透 / 反向代理）—— 这类部署下 tls_enabled 恒为 False，
    # 公网链接只能由这里给出，否则公网码永远是空的。
    public_url_override: str = ""
    # 明文端口。贴纸上的「内网码」要靠它拼出 http://IP:PORT/，
    # 所以启动时把 --port 存下来 —— 光有 host_display 只有 IP 没有端口。
    http_port: int = 0

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
        """
        口令口径：**公网可达的入口强制校验，内网明文端口保持免密**。

        之所以不在明文端口上也收口令，是因为「扫码即用」正是内网那条路的全部意义 ——
        二维码里是固定 URL，带不了口令。内网免密的安全前提是
        **8080 不做 DNAT**；一旦哪天把 8080 也映射到公网，这里就成了公网免密，
        所以启动日志会把这件事说出来。要在两个端口上都校验，加 `--token-always`。

        「公网可达」有三种形态，三者都必须校验口令：

          1. 本机开了 TLS 监听（``is_tls``）；
          2. 显式要求（``--token-always``）；
          3. 声明了 ``--public-url`` —— 内网穿透 / 反向代理那条路。

        第 3 条**曾经漏掉过**：启动时已经强制要求「配了 `--public-url` 就得配
        `--token`」，但校验这里没把 public-url 算作公网，于是「明文 + public-url」
        的实例会变成**公网免密**。配了公网地址就等于公网可达，没有例外。
        """
        if not self.token:
            return True
        server = getattr(self, "server", None)
        on_public = (bool(getattr(server, "is_tls", False))
                     or self.token_always
                     or bool(self.public_url_override))
        if not on_public:
            return True
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        given = self.headers.get("X-Token") or (q.get("t") or [""])[0]
        return secrets.compare_digest(given, self.token)

    # ---------------------------------------------------------- 管理页鉴权
    def _admin_cookie(self) -> str:
        """
        Cookie 里放**派生值**而不是口令本身：浏览器同步、历史记录、代理日志
        都不会因此泄露口令；服务端拿同样的派生值比对即可。
        """
        if not self.admin_token:
            return ""
        return hashlib.sha256(("pg-admin:" + self.admin_token).encode("utf-8")).hexdigest()[:32]

    def _cookies(self) -> dict:
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                key, val = part.split("=", 1)
                out[key.strip()] = val.strip()
        return out

    def _admin_authed(self) -> bool:
        if not self.admin_token:
            return False
        given = self.headers.get("X-Admin-Token") or ""
        if given and secrets.compare_digest(given, self.admin_token):
            return True
        got = self._cookies().get("pg_admin", "")
        return bool(got) and secrets.compare_digest(got, self._admin_cookie())

    def _admin_denied(self):
        body = ("<!DOCTYPE html><meta charset=utf-8>"
                "<title>需要口令</title>"
                "<body style=\"font:15px/1.7 -apple-system,'PingFang SC',sans-serif;"
                "max-width:520px;margin:80px auto;padding:0 20px;color:#1f2329\">"
                "<h2 style=\"font-size:17px\">需要管理口令</h2>"
                "<p style=\"color:#8a9099\">请在地址后附上口令访问一次，"
                "例如 <code>/admin?t=你的口令</code>，之后会自动记住。</p>"
                "</body>").encode("utf-8")
        return self._send(401, body, "text/html; charset=utf-8",
                          {"Cache-Control": "no-store"})

    # 管理页的动作白名单：只认这几个，避免把 Handler 的私有方法暴露成路由
    _ADMIN_GETS = ("/admin/api/status", "/admin/qr.svg")
    _ADMIN_POSTS = ("/admin/api/config", "/admin/api/verify",
                    "/admin/api/ddns", "/admin/api/cert",
                    "/admin/api/sticker")

    def _admin_dispatch(self, path: str, is_post: bool):
        """
        管理页的唯一入口。三道关，顺序不能换：

          1. **只许内网** —— 公网来源一律 404（连「这里有管理页」都不该被知道）
          2. **必须配了口令** —— 没配就是功能关闭，不是无保护
          3. **口令校验** —— 支持 `?t=` 引导种 Cookie，或 `X-Admin-Token` 头
        """
        if not pg_admin.is_lan_addr(self.client_address[0]):
            return self._err("未找到", 404)
        if not self.admin_token:
            return self._err("未启用管理页（启动时加 --admin-token）", 404)

        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        given = (query.get("t") or [""])[0]
        # 引导：带对口令访问一次 → 种 Cookie → 跳回干净 URL。
        # 跳转是为了让口令不留在地址栏与浏览器历史里。
        if given and secrets.compare_digest(given, self.admin_token):
            return self._send(302, b"", "text/plain", {
                "Set-Cookie": ("pg_admin=%s; Path=/admin; HttpOnly; "
                               "SameSite=Strict; Max-Age=2592000" % self._admin_cookie()),
                "Location": "/admin",
            })
        if not self._admin_authed():
            return self._admin_denied()

        allowed = self._ADMIN_POSTS if is_post else self._ADMIN_GETS
        if path not in allowed and path != "/admin":
            return self._err("未找到", 404)
        if path == "/admin":
            if is_post:
                return self._err("未找到", 404)
            body = pg_admin.ADMIN_PAGE.replace("__VERSION__", VERSION).encode("utf-8")
            return self._send(200, body, "text/html; charset=utf-8",
                              {"Cache-Control": "no-store, no-cache, must-revalidate"})

        try:
            return self._admin_action(path, is_post)
        except EngineError as exc:
            return self._err(exc)
        except pg_admin.AdminError as exc:
            return self._err(exc)
        except Exception as exc:                                  # noqa: BLE001
            LOG.exception("管理页 %s 失败", path)
            return self._err("服务器内部错误：%s" % exc, 500)

    def _public_url(self, data: dict) -> str:
        """
        公网访问链接（含口令）。

        两个来源，`--public-url` 优先：

        1. **显式给出**（`--public-url`）—— 给 TLS 在**别处**终结的部署用：
           第三方内网穿透（DDNSTO 等）或反向代理。这种部署下网关自己不开 TLS，
           所以**不能**再拿 `tls_enabled` 当判据，否则公网码永远为空、
           管理页那一项永远是灰的。
        2. **按域名 + 本机 TLS 端口自动拼** —— 只有 TLS 真起来了才生成，
           否则给出一个打不开的链接，还不如不给。

        这个值只发给**已通过 admin 鉴权**的会话：用户本来就有权知道自己的口令，
        但要他手工拼一条 40 字符的 URL 未免太不讲道理。
        """
        override = (self.public_url_override or "").strip().rstrip("/")
        if override:
            tail = "?t=%s" % self.token if self.token else ""
            return "%s/%s" % (override, tail)

        domain = pg_admin.full_domain(data)
        if not (domain and self.tls_enabled and self.tls_port):
            return ""
        port = "" if self.tls_port == 443 else ":%d" % self.tls_port
        tail = "?t=%s" % self.token if self.token else ""
        return "https://%s%s/%s" % (domain, port, tail)

    def _sticker_avail(self) -> dict:
        """
        每种码当前能不能印 —— 值是一个 `(Code 或 None, 不可用的原因)`。

        「不可用」在这里是**正常状态**而不是错误：没配域名就没有公网码，
        没放 APK 就没有下载码。管理页把这几项灰掉并说明原因，总比印一张
        扫出来是 404 的贴纸强。
        """
        host = pg_sticker.lan_ip() or self.host_display
        port = self.http_port or 80
        suffix = "" if port == 80 else ":%d" % port
        base = "http://%s%s" % (host, suffix)
        data = pg_admin.load_secrets()
        wan = self._public_url(data)
        has_apk = bool(self.apk_path) and os.path.exists(self.apk_path)
        no_host = "探测不到本机局域网地址"

        return {
            "lan": ((pg_sticker.Code("lan", "同一 Wi-Fi",
                                     "%s%s" % (host, suffix), base + "/"), "")
                    if host else (None, no_host)),
            "wan": ((pg_sticker.Code("wan", "任意网络",
                                     pg_admin.full_domain(data) or "公网", wan), "")
                    if wan else (None, "未启用 HTTPS 或未配域名")),
            "app": ((pg_sticker.Code("app", "装 App", "扫码安装", base + "/app"), "")
                    if (host and has_apk)
                    else (None, no_host if not host
                          else "未找到可下载的 APK（启动参数 --apk）")),
        }

    def _sticker_codes(self, kinds) -> tuple[list, list]:
        """按 kind 列表取可用的码。返回 (Code 列表, 被跳过的 kind 列表)。"""
        avail = self._sticker_avail()
        codes, skipped = [], []
        for kind in (kinds or list(pg_sticker.CODE_KINDS)):
            code, _why = avail.get(kind, (None, "未知类型"))
            (codes if code else skipped).append(code or kind)
        return codes, skipped

    def _admin_qr_svg(self):
        """
        管理页里的单码预览。

        内容一律**由服务端按 kind 现算**，不接受前端传 URL —— 否则这就是
        一个开在管理页后面的任意二维码生成接口了，没这个必要。
        """
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        codes, _ = self._sticker_codes([(q.get("kind") or [""])[0]])
        if not codes:
            return self._err("这个二维码当前不可用", 404)
        try:
            svg = pg_sticker.svg_for(codes[0].url)
        except pg_sticker.StickerError as exc:
            return self._err(str(exc))
        return self._send(200, svg.encode("utf-8"),
                          "image/svg+xml; charset=utf-8",
                          {"Cache-Control": "no-store"})

    def _admin_sticker(self, payload: dict):
        """
        生成贴纸 PDF 并**注册成一个作业**，由前端带着 job id 跳进打印面板。

        不直接送 CUPS 是刻意的：先落到面板上，用户能看到预览、能选打印机
        和纸盒、能改份数 —— 这些能力面板里本来就有，没理由再实现一遍，
        更没理由绕过它们把纸直接推出去。
        """
        # 注意别写成 `int(payload.get("layout") or 1)` —— 0 是 falsy，
        # 会被悄悄换成 1，等于把非法参数当成没传。少传用默认，传错要报错。
        raw = payload.get("layout")
        if raw in (None, ""):
            layout = 1
        else:
            try:
                layout = int(raw)
            except (TypeError, ValueError):
                return self._err("版式参数不对")
        if layout not in pg_sticker.LAYOUTS:
            return self._err("不支持的版式：%s" % layout)

        codes, skipped = self._sticker_codes(payload.get("kinds"))
        if not codes:
            reasons = self._sticker_avail()
            why = "；".join(sorted({reasons[k][1] for k in pg_sticker.CODE_KINDS}))
            return self._err("没有可用的二维码 —— %s" % why)

        job = self.store.create()
        target = os.path.join(job.dir, "source.pdf")
        try:
            pg_sticker.build_pdf(codes, target, layout=layout)
            pages, sizes = pg_engine.pdf_info(target)
        except (pg_sticker.StickerError, EngineError, OSError) as exc:
            shutil.rmtree(job.dir, ignore_errors=True)
            return self._err("贴纸生成失败：%s" % exc)

        job.filename = "二维码贴纸（%s）.pdf" % _LAYOUT_LABEL.get(layout, layout)
        job.source_pdf = target
        job.pages = pages
        job.files = 1
        job.sizes = sizes
        LOG.info("生成二维码贴纸 %s：%s，%d 联 -> 作业 %s",
                 job.filename, "/".join(c.kind for c in codes), layout, job.id)
        return self._json({"id": job.id, "filename": job.filename,
                           "pages": pages, "files": 1,
                           "codes": [c.kind for c in codes],
                           "skipped": skipped,
                           "booklet": booklet_summary(pages)})

    def _admin_action(self, path: str, is_post: bool):
        if path == "/admin/api/status":
            data = pg_admin.load_secrets()
            status = pg_admin.collect_status(data, self.tls_enabled, self.tls_port)
            status["public_url"] = self._public_url(data)
            avail = self._sticker_avail()
            status["sticker"] = {
                kind: {"available": bool(code), "reason": why,
                       "label": code.label if code else "",
                       "sub": code.sub if code else "",
                       "url": code.url if code else ""}
                for kind, (code, why) in avail.items()
            }
            return self._json(status)

        if path == "/admin/qr.svg":
            return self._admin_qr_svg()

        payload = self._json_body()
        if path == "/admin/api/sticker":
            return self._admin_sticker(payload)
        if path == "/admin/api/config":
            data = pg_admin.load_secrets()
            if payload.get("clear_credentials"):
                pg_admin.clear_credentials(data)
                pg_admin.save_secrets(data)
                return self._json({"ok": True, "message": "凭据已清空"})
            pg_admin.apply_changes(data, payload)
            pg_admin.save_secrets(data)
            return self._json({"ok": True,
                               "message": "已保存（域名 %s）"
                                          % (pg_admin.full_domain(data) or "未配置")})

        if path == "/admin/api/verify":
            return self._json(pg_admin.verify_credentials(pg_admin.load_secrets()))

        if path == "/admin/api/ddns":
            result = pg_admin.sync_ddns(force=bool(payload.get("force")))
            if result["changed"]:
                note = "已更新：%s → %s（%s）" % (result["domain"], result["ip"],
                                              result["action"])
            else:
                note = "%s 已是 %s，无需变更" % (result["domain"], result["ip"])
            result["message"] = note
            return self._json(result)

        if path == "/admin/api/cert":
            data = pg_admin.load_secrets()
            result = pg_admin.issue_cert(data, force=bool(payload.get("force")))
            pg_admin.save_state({"last_issue": {"at": int(time.time()),
                                                "ok": result["ok"],
                                                "message": result["message"]}})
            return self._json(result)

        return self._err("未找到", 404)

    # -------------------------------------------------------------- 路由
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        # 管理页走**独立口令**，所以必须在 _authed() 之前分流
        if path == "/admin" or path.startswith("/admin/"):
            return self._admin_dispatch(path, False)
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
                                   "version": VERSION,
                                   "printers": len(pg_engine.list_printers()),
                                   "app": bool(self.apk_path and
                                               os.path.exists(self.apk_path)),
                                   # 公网部署后排查第一步就是看这两项：
                                   # 8443 到底起没起、管理页开没开
                                   "tls": {"enabled": self.tls_enabled,
                                           "port": self.tls_port},
                                   "auth": bool(self.token),
                                   "admin": bool(self.admin_token)})
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
        if path == "/admin" or path.startswith("/admin/"):
            return self._admin_dispatch(path, True)
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
                           "files": len(items), "size": total_bytes,
                           "booklet": booklet_summary(pages)})

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
        """
        构建一次作业，返回可直接用于响应/出纸的条目。

        预览与出纸**分开构建**，因为两者要的分辨率根本不同：

        - 预览只要看清版面，最终图就是 72dpi 的 → 按 PREVIEW_BUILD_DPI 构建，
          实测 10 页小册子 20.1s → 6.3s（版面完全一致，见 build_final 的说明）；
        - 出纸必须按 spec.dpi 构建，拿预览档去印会糊。

        所以两条路径各有各的缓存槽与工作目录（`build-<key>` / `build-<key>~pv`），
        互不覆盖。代价是「先预览再打印」时那次出纸档构建躲不掉 —— 但那本来就是
        必须付的成本，只是从预览时刻挪到了打印时刻。
        """
        key = spec.cache_key()
        slot = key + _PV if with_preview else key
        cached = _CACHE.get((job.id, slot))
        if cached and os.path.exists(cached["pdf"]) and \
                (not with_preview or cached.get("images")):
            return cached

        work = self.store.workdir_for(job, slot)
        res = pg_engine.build_final(spec, job.source_pdf, work,
                                    preview=with_preview)
        entry = {"pdf": res["pdf"], "mode": res["mode"], "pages": res["pages"],
                 "source_pages": res["source_pages"], "sheet": res["sheet"],
                 "notes": res["notes"], "images": []}
        if with_preview:
            entry["images"] = pg_engine.make_preview(res["pdf"], work)
        with _CACHE_LOCK:
            _CACHE[(job.id, slot)] = entry
            while len(_CACHE) > _CACHE_LIMIT:
                _CACHE.pop(next(iter(_CACHE)))
        return entry

    def _preview_urls(self, job: Job, key: str, entry: dict) -> list[str]:
        """
        预览图的 URL 列表。

        **口令直接拼进去** —— `/img` 在 `_authed()` 之后，公网端口不带口令就是
        401，前端拿到一串裂图。以前这里只拼 job/k/n，前端那边 `<img src>` 又
        忘了走 `api()`，于是「内网好好的、外网预览全裂」。

        这里与前端 `api()` 是**双保险**：前端仍会用 api() 再补一次 t=，
        重复带同一个值对 `_authed()`（取 parse_qs 的第一个值）完全无害；
        直接消费这份 JSON 的客户端（含安卓 App 的 WebView）则不必自己拼。
        """
        urls = []
        tail = "&t=%s" % urllib.parse.quote(self.token, safe="") if self.token else ""
        for path in entry["images"]:
            m = re.search(r"(\d+)\.png$", path)
            if m:
                urls.append("/img?job=%s&k=%s&n=%s%s"
                            % (job.id, key, m.group(1), tail))
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
            "booklet": booklet_summary(entry["source_pages"]),
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
        # 预览图既可能在预览档槽位（常见），也可能在出纸档槽位（旧版行为）；
        # 缓存被挤掉时退回磁盘上的两个构建目录找 —— URL 里的 k 保持不变。
        for slot in (key + _PV, key):
            entry = _CACHE.get((job.id, slot))
            for p in (entry or {}).get("images", []):
                if re.search(r"[-/]0*%d\.png$" % n, p):
                    path = p
                    break
            if path:
                break
        if not path:
            for slot in (key + _PV, key):
                cand = os.path.join(job.dir, "build-" + slot, "preview-%d.png" % n)
                if os.path.exists(cand):
                    path = cand
                    break
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


class QueueHTTPServer(ThreadingHTTPServer):
    """
    带足够长监听队列的 HTTP 服务。

    `request_queue_size` **必须是类属性**：`TCPServer.__init__` 内部走的是
    `server_bind()` → `self.listen(self.request_queue_size)`，构造完成之后再赋值
    就晚了 —— 实测那样写 Send-Q 一直是标准库默认的 5，多台手机同时上传会被拒。
    """

    request_queue_size = SERVER_BACKLOG
    daemon_threads = True

    def handle_error(self, request, client_address):
        # 公网/手机侧连接被随手掐断是常态，别刷栈
        exc = sys.exc_info()[1]
        if isinstance(exc, (ssl.SSLError, ConnectionResetError, BrokenPipeError)):
            LOG.debug("连接异常（%s）：%s", client_address[0], exc)
            return
        super().handle_error(request, client_address)


class DualStackServer(QueueHTTPServer):
    """
    双栈监听：同一端口同时接受 IPv4 与 IPv6。

    为什么要显式设 `IPV6_V6ONLY=0`：Linux 的默认值虽是 0，但发行版/内核参数
    （`net.ipv6.bindv6only`）会改它。一旦是 1，绑 `::` 就**只听得懂 IPv6**，
    而用户的 DNAT 是 IPv4 —— 表现为「映射配好了、外面死活连不上」，
    排查起来极其费时。显式关掉，两头都收。
    """

    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError) as exc:
            LOG.warning("无法关闭 IPV6_V6ONLY（%s）—— IPv4 可能收不到", exc)
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        # 跳过标准库的 getfqdn：对 "::" 会触发一次 DNS 查询，白等几秒
        self.server_name = host or "::"
        self.server_port = port


class TLSHandshakeMixin:
    """
    把 TLS 握手放到**每连接线程**里做，绝不在 accept 循环里做。

    为什么非要这样：标准库 `SSLSocket.accept()` 的实现是「accept + 同步握手」，
    而 `do_handshake_on_connect` 默认 True、又没有超时。于是只要把 TLS 套在
    **监听套接字**上，一条「TCP 连上但不发 ClientHello」的连接（端口扫描器的
    标准动作）就会把 `serve_forever()` 的 accept 循环**永久卡死**：
    之后所有连接只能堆在 backlog 里，直到进程重启。现场特征很好认 ——
    `ss -lnt` 的 Recv-Q 顶到 backlog 不归零，`ss -ntp` 里挂着一堆
    CLOSE-WAIT 连接，Recv-Q 里躺着几百字节没被读走的 ClientHello。

    2026-09-19 就是这样被 66.132.x.x / 115.231.x.x 的扫描器打死的：
    DNAT、DNS、DDNS、证书全部正常，唯独 8443 连本机自己都连不上。
    公网端口天天被扫，这不是「可能发生」而是「必然发生」。

    改成下面这样之后：accept 循环永远只做 `accept()`，握手落在
    ThreadingMixIn 已经起好的每连接线程里，并且有 TLS_HANDSHAKE_TIMEOUT 兜底 ——
    扫描器再多也卡不住入口。
    """

    tls_context = None

    def get_request(self):
        # 只 accept，不握手；握手交给 process_request_thread
        sock, addr = self.socket.accept()
        return sock, addr

    def process_request_thread(self, request, client_address):
        # 这一段已经跑在每连接线程里（不是 accept 循环），卡住也不影响别人
        try:
            request.settimeout(TLS_HANDSHAKE_TIMEOUT)
            tls = self.tls_context.wrap_socket(request, server_side=True)
            tls.settimeout(None)           # 交回阻塞模式给后续读写
        except (ssl.SSLError, OSError, ValueError) as exc:
            # 扫描器握手失败是常态，静默丢弃即可。
            # wrap_socket 失败时标准库已自行 close 过 fd（_create 里先
            # sock.detach() 再握手），所以这里不会再关第二次、不会误关别人的连接。
            LOG.debug("TLS 握手失败（%s）：%s", client_address[0], exc)
            self.shutdown_request(request)
            return
        super().process_request_thread(tls, client_address)


def make_tls_context(cert: str, key: str) -> ssl.SSLContext:
    """
    建 server 侧 TLS 上下文。

    只支持 TLS 1.2+（微信 WebView 与所有现代浏览器都没问题，老 Android 4.x 不行）。
    这里**不碰监听套接字** —— 握手由 TLSHandshakeMixin 在每连接线程里做。
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        # 只宣告 http/1.1：本网关不会说 HTTP/2，宣告了反而让部分客户端
        # 协商到我们答不上来的协议
        ctx.set_alpn_protocols(["http/1.1"])
    except NotImplementedError:
        pass
    return ctx


def tls_server_class(dual_stack: bool) -> type:
    """把 TLS 握手混入正确的监听基类（双栈 / 仅 IPv4）。"""
    base = DualStackServer if dual_stack else QueueHTTPServer
    return type("TLSServer", (TLSHandshakeMixin, base), {})


def main(argv: list[str] | None = None) -> int:
    global MAX_UPLOAD_MB

    ap = argparse.ArgumentParser(description="扫码打印网关")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--printer", default="",
                    help="锁定默认打印队列；留空则跟随 CUPS 系统默认打印机")
    ap.add_argument("--token", default="", help="公网端口(TLS)的访问口令")
    ap.add_argument("--token-always", action="store_true",
                    help="明文端口也校验口令（默认内网免密，扫码即用）")
    ap.add_argument("--spool", default="/var/spool/print-gateway")
    ap.add_argument("--title", default="扫码打印")
    ap.add_argument("--default-dpi", type=int, default=pg_engine.DEFAULT_DPI)
    ap.add_argument("--max-mb", type=int, default=MAX_UPLOAD_MB,
                    help="单文件上传上限（MB）")
    ap.add_argument("--host-display", default="",
                    help="界面上展示的访问地址，留空自动探测")
    ap.add_argument("--apk", default="",
                    help="安卓 App 包路径；配置后 GET /app 下载，未配置则 /app 404")
    ap.add_argument("--admin-token", default="",
                    help="管理页口令；留空则整个 /admin 关闭（不会免密开放）")
    ap.add_argument("--tls-port", type=int, default=0,
                    help="TLS 监听端口（如 8443）；0 = 不开。需 --token 与有效证书")
    ap.add_argument("--tls-bind", default="::",
                    help="TLS 监听地址，默认 :: 双栈（IPv4/IPv6 都收）")
    ap.add_argument("--tls-cert", default="",
                    help="证书链路径，默认 %s" % pg_admin.cert_paths()["crt"])
    ap.add_argument("--tls-key", default="",
                    help="私钥路径，默认 %s" % pg_admin.cert_paths()["key"])
    ap.add_argument("--public-url", default="",
                    help="外部可达的完整基地址，如 https://xxx.ddnsto.com。"
                         "给「TLS 在别处终结」的部署用（第三方内网穿透 / 反向代理）："
                         "本机不开 TLS，但公网码要指向隧道域名。配了它必须同时配 --token")
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
    Handler.token_always = bool(args.token_always)
    pg_engine.DEFAULT_DPI = args.default_dpi

    # 安卓 App 包下载端点。缺失只告警不致命 —— 网关本身可继续服务，
    # /app 会返回 404，healthz 的 app 键如实报告。
    Handler.apk_path = args.apk
    if args.apk:
        Handler.apk_name = os.path.basename(args.apk)
        if not os.path.exists(args.apk):
            LOG.warning("--apk 指向的文件不存在：%s（/app 将返回 404）", args.apk)

    Handler.admin_token = args.admin_token
    if not args.admin_token:
        LOG.warning("未设置 --admin-token：/admin 已关闭"
                    "（本网关不提供「无口令的管理页」）")

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

    # 用 QueueHTTPServer 而不是裸的 ThreadingHTTPServer：backlog 得靠类属性才生效
    # （见 SERVER_BACKLOG 的注释），默认的 5 在多台手机同时上传时会被拒。
    httpd = QueueHTTPServer((args.bind, args.port), Handler)
    httpd.daemon_threads = True
    try:
        host = args.host_display or socket.gethostbyname(socket.gethostname())
    except OSError:
        host = args.host_display or args.bind
    Handler.host_display = host
    Handler.http_port = args.port
    LOG.info("%s 已启动：http://%s:%d", args.title, host, args.port)
    LOG.info("上传上限 %d MB，预览 %d dpi，默认打印 %d dpi",
             MAX_UPLOAD_MB, pg_engine.PREVIEW_DPI, pg_engine.DEFAULT_DPI)
    if args.token and not args.token_always:
        # 这句话必须说出来：免密的合法性完全建立在「8080 不做 DNAT」上，
        # 哪天有人把它映射出去，这里就变成了公网免密
        LOG.warning("明文端口 %d 免密（内网扫码即用）—— "
                    "前提是它**不做 DNAT**；映射到公网即等于公网免密",
                    args.port)

    # ------------------------------------------------- 公网链接（隧道/反代）
    # 判据是「这条链接公网可达吗」，而不是「TLS 在谁的机器上终结」。
    # 内网穿透（DDNSTO 等）把 TLS 放在边缘，网关自己跑明文，但公网链接照样成立，
    # 所以这里必须和 --tls-port 那条一样硬：宁可拒绝启动，也不出现公网免密。
    if args.public_url:
        if not args.token:
            LOG.error("指定 --public-url 时必须同时设置 --token"
                      "（公网可达的链接不允许免密）")
            return 2
        if "://" not in args.public_url:
            args.public_url = "https://" + args.public_url
            LOG.warning("--public-url 未写协议，按 https 处理：%s", args.public_url)
        Handler.public_url_override = args.public_url.strip().rstrip("/")
        LOG.info("公网链接来自 --public-url：%s（不再依赖本机 TLS 端口）",
                 Handler.public_url_override)
        if args.tls_port:
            LOG.info("同时开着 --tls-port %d：两条链接都成立，公网码以 "
                     "--public-url 为准", args.tls_port)
        if args.admin_token:
            # 这是隧道部署最容易忽略的一处：/admin 的「仅限内网」判的是源 IP，
            # 前提是「公网进来的源 IP 是公网地址」。穿透客户端就站在局域网里，
            # 于是公网请求的源 IP 是内网地址 → 这道关被**反向**绕过，只剩口令一道。
            LOG.warning("公网实例启用了 /admin：经内网穿透访问时源 IP 是隧道客户端的"
                        "内网地址，「仅限内网」判据会失效，管理页只剩口令一层保护 —— "
                        "公网实例建议不要设 --admin-token")

    # ---------------------------------------------------------- TLS 监听
    # 证书缺失**只告警不退出**：内网明文那条路照常工作，不能因为「还没签证书」
    # 就把用户现有的打印服务弄挂。
    tls_server = None
    if args.tls_port:
        if not args.token:
            # 硬拒绝：公网端口免密等于把打印机敞开给全网
            LOG.error("启用 --tls-port 时必须同时设置 --token"
                      "（公网端口不允许免密）")
            return 2
        cert = args.tls_cert or pg_admin.cert_paths()["crt"]
        key = args.tls_key or pg_admin.cert_paths()["key"]
        if not (os.path.isfile(cert) and os.path.isfile(key)):
            LOG.warning("证书缺失（%s）—— %d 端口未启用；先在 /admin 里签发",
                        cert, args.tls_port)
        else:
            server_cls = tls_server_class(":" in args.tls_bind)
            try:
                tls_server = server_cls((args.tls_bind, args.tls_port), Handler)
                # 上下文必须在 serve_forever() 之前设好 —— 每连接线程要用它做握手
                tls_server.tls_context = make_tls_context(cert, key)
                # 标记：Handler 靠它判断「这是公网端口，必须校验口令」
                tls_server.is_tls = True
            except (OSError, ssl.SSLError) as exc:
                LOG.error("TLS 端口 %d 启动失败：%s", args.tls_port, exc)
                return 2
            Handler.tls_enabled = True
            Handler.tls_port = args.tls_port
            LOG.info("HTTPS 已启用：https://%s:%d（证书 %s）",
                     args.tls_bind, args.tls_port, cert)
            LOG.info("TLS 握手在每连接线程内完成（限时 %.0f 秒）——"
                     "扫描器占不住 accept 循环", TLS_HANDSHAKE_TIMEOUT)
    if args.admin_token:
        LOG.info("管理页：http://%s:%d/admin （仅限内网）", host, args.port)

    def reaper():
        while True:
            time.sleep(1800)
            try:
                Handler.store.purge_old()
            except Exception:                                     # noqa: BLE001
                LOG.exception("清理作业时出错")

    threading.Thread(target=reaper, daemon=True).start()

    # 明文端口跑在主线程（它是「必须活着」的那个），TLS 端口在守护线程里
    extra = [srv for srv in (tls_server,) if srv is not None]
    for srv in extra:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("收到中断，退出")
    finally:
        for srv in [httpd] + extra:
            try:
                srv.server_close()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
