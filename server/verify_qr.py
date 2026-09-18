#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二维码真机可扫性验证 —— 用 OpenCV 的真实 QR 解码器反向验证 gen_qr.py 的输出。
这是唯一能证明"生成的码能被手机扫出来"的方法。

需要: pip install opencv-python-headless numpy
用法: python verify_qr.py
"""

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gen_qr                                                  # noqa: E402

try:
    import cv2
    import numpy as np
except ImportError:
    print("需要 opencv-python-headless: pip install opencv-python-headless numpy")
    sys.exit(2)

DETECTOR = cv2.QRCodeDetector()

# 用 CI 风格的固定用例，覆盖不同长度/纠错等级/字符集
CASES = [
    ("http://192.168.1.100:8080", "M"),
    ("http://192.168.1.100:8080", "L"),
    ("http://192.168.1.100:8080", "Q"),
    ("http://192.168.1.100:8080", "H"),
    ("http://192.168.1.100:8080/?t=abc123XYZ", "M"),
    ("http://192.168.1.100:8080/print", "H"),
    ("A", "H"),
    ("AB", "M"),
    ("HELLO WORLD", "L"),
    ("HELLO WORLD", "Q"),
    ("HELLO WORLD", "H"),
    ("1234567890", "L"),
    ("http://192.168.1.100:8080/?t=x", "Q"),
    ("https://example.com/very/long/path/to/some/page?query=1&more=2", "L"),
    ("中文测试内容", "H"),
    ("扫码打印 http://192.168.1.100:8080", "M"),
    ("http://192.168.1.100:8080/?t=" + "a" * 40, "M"),
    ("http://192.168.1.100:8080/?t=" + "Z" * 80, "L"),
    ("http://192.168.1.100:8080/?t=" + "b" * 150, "L"),
]


def matrix_to_image(mat, scale=10, quiet=4):
    """把布尔矩阵转成 OpenCV 灰度图。"""
    n = len(mat)
    w = (n + quiet * 2) * scale
    img = np.full((w, w), 255, dtype=np.uint8)
    for r, row in enumerate(mat):
        for c, v in enumerate(row):
            if v:
                y0 = (r + quiet) * scale
                x0 = (c + quiet) * scale
                img[y0:y0 + scale, x0:x0 + scale] = 0
    return img


def decode(img):
    """
    在多种成像条件下尝试解码，返回文本或空串。

    只用单尺度原图会误判：OpenCV 的 QRCodeDetector 对「完美无噪的纯二值图」
    有时反而不如带轻微模糊的图好定位（真实摄像头成像天然带模糊）。
    因此这里同时覆盖放大与轻度模糊的情形，更贴近手机实拍。
    """
    variants = []
    for k in (1, 2, 3):
        variants.append(cv2.resize(img, None, fx=k, fy=k,
                                   interpolation=cv2.INTER_NEAREST))
    variants.append(cv2.GaussianBlur(img, (3, 3), 0.8))
    variants.append(cv2.GaussianBlur(
        cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_LINEAR),
        (5, 5), 1.2))

    for v in variants:
        try:
            data, _, _ = DETECTOR.detectAndDecode(v)
        except Exception:                                      # noqa: BLE001
            data = ""
        if data:
            return data
    return ""


def main():
    print("=" * 68)
    print("二维码可扫性验证 (OpenCV %s)" % cv2.__version__)
    print("=" * 68)

    passed = failed = 0
    failures = []

    for text, ec in CASES:
        try:
            mat = gen_qr.full_matrix(text, ec)
        except Exception as exc:                               # noqa: BLE001
            failed += 1
            failures.append((text, ec, "生成异常: %s" % exc))
            print("  FAIL 生成失败  ec=%s  %r  -> %s" % (ec, text[:40], exc))
            continue

        version = (len(mat) - 17) // 4

        decoded = ""
        for scale in (6, 8, 10, 12, 16):
            img = matrix_to_image(mat, scale)
            decoded = decode(img)
            if decoded == text:
                break

        if decoded == text:
            passed += 1
            print("  ok    v%-2d %s  %d 字节  %r" % (version, ec, len(text.encode()), text[:36]))
        else:
            failed += 1
            got = decoded if decoded else "(无法解码)"
            failures.append((text, ec, got))
            print("  FAIL  v%-2d %s  %r" % (version, ec, text[:36]))
            print("        期望: %r" % text)
            print("        实得: %r" % got)

    print()
    print("-" * 68)
    print("通过 %d / %d" % (passed, passed + failed))
    if failures:
        print()
        print("失败明细:")
        for text, ec, got in failures:
            print("  [%s] %r -> %r" % (ec, text, got))
        return 1
    print("全部通过 —— 生成的二维码可被标准解码器正确识别。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
