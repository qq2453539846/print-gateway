#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成端到端测试用的样例文件（纯标准库，无第三方依赖）。
产出: sample.pdf（多页文本）、sample.png（彩色图）、sample_multipage.pdf
"""

import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))


def make_png(path, w, h):
    """生成一张有清晰色块的 PNG（RGB，无依赖）。"""
    rows = []
    for y in range(h):
        row = bytearray(b"\x00")
        for x in range(w):
            if x < w // 3:
                r, g, b = 220, 60, 60
            elif x < 2 * w // 3:
                r, g, b = 60, 160, 90
            else:
                r, g, b = 70, 110, 210
            if (y // 40) % 2 == 0:
                r, g, b = (255 - r) // 2 + 40, (255 - g) // 2 + 40, (255 - b) // 2 + 40
            row += bytes((r, g, b))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    blob = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(blob)
    return len(blob)


def make_pdf(path, pages=1, title="Print Gateway E2E Test"):
    """生成最小但合法的多页 PDF（Helvetica，纯 ASCII）。"""
    lines_per_page = []
    for i in range(pages):
        lines_per_page.append([
            "Print Gateway - End to End Test",
            "",
            "Page %d of %d" % (i + 1, pages),
            "",
            "This document verifies:",
            "  upload -> convert -> CUPS queue -> backend",
            "",
            "If you can read this, the pipeline works.",
            "",
            "----------------------------------------",
            "E2E-TEST-MARKER-%d" % (i + 1),
        ])

    objs = {}
    # 1 catalog, 2 pages, then per-page content+page objects, last font
    n_pages = pages
    first_page_obj = 3
    page_obj_ids = []
    content_obj_ids = []
    oid = first_page_obj
    for _ in range(n_pages):
        content_obj_ids.append(oid)
        page_obj_ids.append(oid + 1)
        oid += 2
    font_id = oid

    objs[1] = "<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join("%d 0 R" % p for p in page_obj_ids)
    objs[2] = "<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n_pages)

    for i in range(n_pages):
        cid, pid = content_obj_ids[i], page_obj_ids[i]
        content = ["BT", "/F1 12 Tf", "16 TL", "56 780 Td"]
        for ln in lines_per_page[i]:
            safe = ln.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            content.append("(%s) Tj T*" % safe)
        content.append("ET")
        stream = "\n".join(content)
        objs[cid] = "<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
        objs[pid] = ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                     "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                     % (font_id, cid))

    objs[font_id] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    objs[0] = "<< /Title (%s) >>" % title

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for i in sorted(objs):
        offsets[i] = len(out)
        out += ("%d 0 obj\n%s\nendobj\n" % (i, objs[i])).encode("latin-1")
    xref_pos = len(out)
    maxid = max(objs)
    out += ("xref\n0 %d\n" % (maxid + 1)).encode()
    out += b"0000000000 65535 f \n"
    for i in range(1, maxid + 1):
        out += ("%010d 00000 n \n" % offsets[i]).encode()
    out += ("trailer\n<< /Size %d /Root 1 0 R /Info 0 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (maxid + 1, xref_pos)).encode()

    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return len(out)


def main():
    pdf1 = os.path.join(HERE, "sample.pdf")
    pdf3 = os.path.join(HERE, "sample_multipage.pdf")
    png1 = os.path.join(HERE, "sample.png")

    n = make_pdf(pdf1, pages=1)
    print("sample.pdf            %d bytes  1 页" % n)
    n = make_pdf(pdf3, pages=3)
    print("sample_multipage.pdf  %d bytes  3 页" % n)
    n = make_png(png1, 240, 180)
    print("sample.png            %d bytes  240x180 RGB" % n)

    # 自检：确认 PDF 结构可被 pdfinfo 之外的解析器接受（本地无 pdfinfo，做基本校验）
    with open(pdf1, "rb") as fh:
        head = fh.read(8)
    assert head.startswith(b"%PDF-1.4"), "PDF 头不正确"
    print("\n本地基本校验通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
