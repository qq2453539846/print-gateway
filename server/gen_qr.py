#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纯标准库二维码生成器（QR Code, Model 2）。
零依赖，不需要 pip，适合嵌入式设备。

支持 byte 模式（UTF-8）、自动选择版本(1-10)、纠错等级 L/M/Q/H。
输出：终端 ASCII / SVG / PNG(需要 PIL) / PBM(无依赖)

用法:
  python3 gen_qr.py "http://192.168.1.100:8080"            # 终端显示
  python3 gen_qr.py "http://..." -o qr.svg               # 输出 SVG
  python3 gen_qr.py "http://..." -o qr.pbm --ec M        # 输出 PBM
"""

import argparse
import os
import sys

# ------------------------------------------------------------ GF(256) 运算
EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def rs_generator(n):
    """生成 RS 生成多项式，返回系数（最高次到常数项）。"""
    poly = [1]
    for i in range(n):
        new = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            new[j] ^= c
            new[j + 1] ^= gf_mul(c, EXP[i])
        poly = new
    return poly


def rs_encode(data, n):
    """计算 n 个纠错码字。"""
    gen = rs_generator(n)
    res = list(data) + [0] * n
    for i in range(len(data)):
        coef = res[i]
        if coef == 0:
            continue
        for j, g in enumerate(gen):
            res[i + j] ^= gf_mul(g, coef)
    return res[len(data):]


# ------------------------------------------------------------ QR 参数表
# 每版本: (版本, 尺寸, 每块EC码字, 组1块数, 组1数据码字, 组2块数, 组2数据码字)
# 按纠错等级分表。数据来自 ISO/IEC 18004 标准。
EC_TABLE = {
    "L": {
        1: (7, 1, 19, 0, 0),        2: (10, 1, 34, 0, 0),
        3: (15, 1, 55, 0, 0),       4: (20, 1, 80, 0, 0),
        5: (26, 1, 108, 0, 0),      6: (18, 2, 68, 0, 0),
        7: (20, 2, 78, 0, 0),       8: (24, 2, 97, 0, 0),
        9: (30, 2, 116, 0, 0),      10: (18, 2, 68, 2, 69),
    },
    "M": {
        1: (10, 1, 16, 0, 0),       2: (16, 1, 28, 0, 0),
        3: (26, 1, 44, 0, 0),       4: (18, 2, 32, 0, 0),
        5: (24, 2, 43, 0, 0),       6: (16, 4, 27, 0, 0),
        7: (18, 4, 31, 0, 0),       8: (22, 2, 38, 2, 39),
        9: (22, 3, 36, 2, 37),      10: (26, 4, 43, 1, 44),
    },
    "Q": {
        1: (13, 1, 13, 0, 0),       2: (22, 1, 22, 0, 0),
        3: (18, 2, 17, 0, 0),       4: (26, 2, 24, 0, 0),
        5: (18, 2, 15, 2, 16),      6: (24, 4, 19, 0, 0),
        7: (18, 2, 14, 4, 15),      8: (22, 4, 18, 2, 19),
        9: (20, 4, 16, 4, 17),      10: (24, 6, 19, 2, 20),
    },
    "H": {
        1: (17, 1, 9, 0, 0),        2: (28, 1, 16, 0, 0),
        3: (22, 2, 13, 0, 0),       4: (16, 4, 9, 0, 0),
        5: (22, 2, 11, 2, 12),      6: (28, 4, 15, 0, 0),
        7: (26, 4, 13, 1, 14),      8: (26, 4, 14, 2, 15),
        9: (24, 4, 12, 4, 13),      10: (28, 6, 15, 2, 16),
    },
}

# 对齐图案中心坐标（版本 1-10）
ALIGN_POS = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
    10: [6, 28, 50],
}


def char_count_bits(version):
    return 16 if version >= 10 else 8


def total_data_codewords(version, ec):
    _, b1, d1, b2, d2 = EC_TABLE[ec][version]
    return b1 * d1 + b2 * d2


def pick_version(data_len, ec):
    for v in range(1, 11):
        bits = 4 + char_count_bits(v) + data_len * 8
        cap = total_data_codewords(v, ec) * 8
        if bits <= cap:
            return v
    raise ValueError("数据过长，当前实现上限为版本 10（纠错 %s）。"
                     "请缩短 URL 或降低纠错等级。" % ec)


# ------------------------------------------------------------ 位流构建
def build_bitstream(data, version, ec):
    bits = []
    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)                                  # byte 模式
    put(len(data), char_count_bits(version))
    for byte in data:
        put(byte, 8)

    cap = total_data_codewords(version, ec) * 8
    # 结束符最多 4 位
    for _ in range(min(4, cap - len(bits))):
        bits.append(0)
    # 补齐到字节边界
    while len(bits) % 8:
        bits.append(0)
    # 填充字节 0xEC / 0x11 交替
    pad = [0xEC, 0x11]
    i = 0
    while len(bits) < cap:
        put(pad[i % 2], 8)
        i += 1

    return [int("".join(str(b) for b in bits[i:i + 8]), 2)
            for i in range(0, len(bits), 8)]


def interleave(codewords, version, ec):
    """按标准把数据块与纠错块交错排列。"""
    _, b1, d1, b2, d2 = EC_TABLE[ec][version]
    ec_per_block = EC_TABLE[ec][version][0]

    blocks, pos = [], 0
    for i in range(b1):
        blocks.append(codewords[pos:pos + d1]); pos += d1
    for i in range(b2):
        blocks.append(codewords[pos:pos + d2]); pos += d2

    ec_blocks = [rs_encode(b, ec_per_block) for b in blocks]

    out = []
    max_d = max(len(b) for b in blocks)
    for i in range(max_d):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ec_per_block):
        for b in ec_blocks:
            out.append(b[i])
    return out


# ------------------------------------------------------------ 矩阵构建
def make_matrix(version, ec, size):
    """构建基础矩阵 + 保留位图（功能图案已就位，数据区留空待填）。"""
    mat = [[None] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def set_fn(r, c, val):
        if 0 <= r < size and 0 <= c < size:
            mat[r][c] = val
            reserved[r][c] = True

    # 定位图案 + 分隔符
    for (r0, c0) in ((0, 0), (0, size - 7), (size - 7, 0)):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if not (0 <= rr < size and 0 <= cc < size):
                    continue
                if 0 <= r <= 6 and 0 <= c <= 6:
                    edge = r in (0, 6) or c in (0, 6)
                    core = 2 <= r <= 4 and 2 <= c <= 4
                    set_fn(rr, cc, edge or core)
                else:
                    set_fn(rr, cc, False)

    # 定时图案
    for i in range(8, size - 8):
        v = (i % 2 == 0)
        set_fn(6, i, v)
        set_fn(i, 6, v)

    # 对齐图案
    for r in ALIGN_POS[version]:
        for c in ALIGN_POS[version]:
            if (r <= 8 and c <= 8) or (r <= 8 and c >= size - 9) or (r >= size - 9 and c <= 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    set_fn(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)

    # 格式信息区域占位
    for i in range(9):
        if not reserved[8][i]:
            set_fn(8, i, False)
        if not reserved[i][8]:
            set_fn(i, 8, False)
    for i in range(8):
        set_fn(8, size - 1 - i, False)
        set_fn(size - 1 - i, 8, False)
    set_fn(size - 8, 8, True)                       # 固定暗模块

    # 版本信息（版本 >= 7）
    if version >= 7:
        vinfo = version_bits(version)
        for i in range(18):
            bit = (vinfo >> i) & 1
            r, c = i // 3, i % 3
            set_fn(size - 11 + c, r, bool(bit))
            set_fn(r, size - 11 + c, bool(bit))

    return mat, reserved


def version_bits(version):
    """版本信息的 18 位 BCH 编码。"""
    v = version << 12
    g = 0x1F25
    for i in range(17, 11, -1):
        if (v >> i) & 1:
            v ^= g << (i - 12)
    return (version << 12) | v


def format_bits_masked(ec, mask):
    """格式信息的 15 位 BCH 编码（含掩码号），最后异或 0x5412。"""
    ec_bits = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}[ec]
    data = (ec_bits << 3) | mask
    v = data << 10
    g = 0x537
    for i in range(14, 9, -1):
        if (v >> i) & 1:
            v ^= g << (i - 10)
    return ((data << 10) | v) ^ 0x5412


MASK_FN = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def apply_mask_full(mat, reserved, mask, size, ec):
    """
    对数据区应用掩码，并按 ISO/IEC 18004 写入两处格式信息。

    格式信息的位序容易搞反，这里严格照标准摆放（bits 的 LSB 为 bit0）：
      竖排(列8)：bit0..bit5 → (0,8)..(5,8)   bit6,7 → (7,8),(8,8)
                 bit8..bit14 → (size-7,8)..(size-1,8)
      横排(行8)：bit0..bit7 → (8,size-1)..(8,size-8)
                 bit8 → (8,7)   bit9..bit14 → (8,5)..(8,0)
    注意 (6,8) 与 (8,6) 是定时图案，必须跳过。
    """
    out = [row[:] for row in mat]
    fn = MASK_FN[mask]
    for r in range(size):
        for c in range(size):
            if not reserved[r][c] and fn(r, c):
                out[r][c] = not out[r][c]

    fmt = format_bits_masked(ec, mask)

    # 竖排：上段 + 下段
    for i in range(15):
        bit = bool((fmt >> i) & 1)
        if i < 6:
            out[i][8] = bit
        elif i < 8:
            out[i + 1][8] = bit
        else:
            out[size - 15 + i][8] = bit

    # 横排：右上段 + 左上段
    for i in range(15):
        bit = bool((fmt >> i) & 1)
        if i < 8:
            out[8][size - i - 1] = bit
        elif i < 9:
            out[8][15 - i] = bit
        else:
            out[8][15 - i - 1] = bit

    out[size - 8][8] = True                       # 固定暗模块
    return out


def penalty(mat, size):
    """标准四项罚分。"""
    score = 0
    for line in list(mat) + [list(col) for col in zip(*mat)]:
        run, prev = 1, line[0]
        for v in line[1:]:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, prev = 1, v
        if run >= 5:
            score += 3 + (run - 5)

    for r in range(size - 1):
        for c in range(size - 1):
            if mat[r][c] == mat[r][c + 1] == mat[r + 1][c] == mat[r + 1][c + 1]:
                score += 3

    pat1 = [True, False, True, True, True, False, True, False, False, False, False]
    pat2 = [False, False, False, False, True, False, True, True, True, False, True]
    for line in list(mat) + [list(col) for col in zip(*mat)]:
        for i in range(size - 10):
            seg = line[i:i + 11]
            if seg == pat1 or seg == pat2:
                score += 40

    dark = sum(1 for row in mat for v in row if v)
    pct = dark * 100 // (size * size)
    score += abs(pct - 50) // 5 * 10
    return score


# ------------------------------------------------------------ 编解码入口
def full_matrix(text, ec="M"):
    """完整流程：文本 -> 布尔矩阵（True=暗）。"""
    data = text.encode("utf-8")
    version = pick_version(len(data), ec)
    size = version * 4 + 17

    cws = build_bitstream(data, version, ec)
    final = interleave(cws, version, ec)

    mat, reserved = make_matrix(version, ec, size)

    bits = []
    for cw in final:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)

    idx, up = 0, True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if up else range(size)
        for r in rows:
            for c in (col, col - 1):
                if not reserved[r][c]:
                    mat[r][c] = bool(bits[idx]) if idx < len(bits) else False
                    idx += 1
        up = not up
        col -= 2

    best, best_p = None, None
    for m in range(8):
        cand = apply_mask_full(mat, reserved, m, size, ec)
        p = penalty(cand, size)
        if best_p is None or p < best_p:
            best, best_p = cand, p
    return best


# ------------------------------------------------------------ 输出格式
def render_terminal(mat, quiet=2):
    size = len(mat)
    out = []
    for _ in range(quiet):
        out.append(" " * (size + quiet * 2 + 2))
    for row in mat:
        line = " " * quiet
        for v in row:
            line += "\u2588\u2588" if v else "  "
        line += " " * quiet
        out.append(line)
    for _ in range(quiet):
        out.append(" " * (size + quiet * 2 + 2))
    return "\n".join(out)


def render_svg(mat, module=8, quiet=4, dark="#000000", light="#ffffff"):
    n = len(mat)
    total = (n + quiet * 2) * module
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" shape-rendering="crispEdges">' % (total, total, total, total),
        '<rect width="%d" height="%d" fill="%s"/>' % (total, total, light),
        '<path fill="%s" d="' % dark,
    ]
    for r, row in enumerate(mat):
        c = 0
        while c < n:
            if row[c]:
                start = c
                while c < n and row[c]:
                    c += 1
                x = (start + quiet) * module
                y = (r + quiet) * module
                w = (c - start) * module
                parts.append("M%d %dh%dv%dh-%dz" % (x, y, w, module, w))
            else:
                c += 1
    parts.append('"/></svg>')
    return "".join(parts)


def render_pbm(mat, scale=4, quiet=4):
    n = len(mat)
    w = (n + quiet * 2) * scale
    h = w
    rows = ["P4", "%d %d" % (w, h)]
    buf = bytearray()
    for _ in range(quiet * scale):
        buf += b"\x00" * ((w + 7) // 8)
    for row in mat:
        line_bits = [0] * (quiet * scale)
        for v in row:
            line_bits += ([0] * scale if v else [1] * scale)   # PBM: 1=黑
        line_bits += [0] * (quiet * scale)
        while len(line_bits) % 8:
            line_bits.append(0)
        for i in range(0, len(line_bits), 8):
            byte = 0
            for j in range(8):
                if line_bits[i + j]:
                    byte |= 1 << (7 - j)
            buf.append(byte)
    for _ in range(quiet * scale):
        buf += b"\x00" * ((w + 7) // 8)
    return ("\n".join(rows) + "\n").encode() + bytes(buf)


def render_png(mat, scale=8, quiet=4):
    try:
        from PIL import Image
    except ImportError:
        return None
    n = len(mat)
    w = (n + quiet * 2) * scale
    img = Image.new("L", (w, w), 255)
    px = img.load()
    for r, row in enumerate(mat):
        for c, v in enumerate(row):
            if v:
                x0 = (c + quiet) * scale
                y0 = (r + quiet) * scale
                for y in range(y0, y0 + scale):
                    for x in range(x0, x0 + scale):
                        px[x, y] = 0
    return img


def main():
    ap = argparse.ArgumentParser(description="纯标准库二维码生成器")
    ap.add_argument("text", nargs="?", help="要编码的内容（URL 等）")
    ap.add_argument("-o", "--output", help="输出文件 (.svg/.pbm/.png)，省略则终端显示")
    ap.add_argument("--ec", default="M", choices=["L", "M", "Q", "H"], help="纠错等级")
    ap.add_argument("--scale", type=int, default=8, help="模块像素放大倍数")
    ap.add_argument("--quiet", type=int, default=4, help="静区模块数")
    args = ap.parse_args()

    if not args.text:
        ap.print_help()
        return 1

    try:
        mat = full_matrix(args.text, args.ec)
    except ValueError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return 1

    if not args.output:
        print(render_terminal(mat))
        print("版本 %d  尺寸 %dx%d  纠错 %s  内容 %d 字节"
              % ((len(mat) - 17) // 4, len(mat), len(mat), args.ec,
                 len(args.text.encode("utf-8"))))
        return 0

    ext = args.output.rsplit(".", 1)[-1].lower()
    outdir = os.path.dirname(os.path.abspath(args.output))
    if outdir and not os.path.isdir(outdir):
        os.makedirs(outdir, exist_ok=True)
    if ext == "svg":
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(render_svg(mat, args.scale, args.quiet))
    elif ext == "pbm":
        with open(args.output, "wb") as fh:
            fh.write(render_pbm(mat, max(1, args.scale // 2), args.quiet))
    elif ext == "png":
        img = render_png(mat, args.scale, args.quiet)
        if img is None:
            print("错误: 输出 PNG 需要 PIL，请用 .svg 或 .pbm", file=sys.stderr)
            return 1
        img.save(args.output)
    else:
        print("错误: 不支持的输出格式 %s（支持 svg/pbm/png）" % ext, file=sys.stderr)
        return 1

    print("已生成 %s（版本 %d，%dx%d，纠错 %s）"
          % (args.output, (len(mat) - 17) // 4, len(mat), len(mat), args.ec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
