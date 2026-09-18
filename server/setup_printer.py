#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
USB 打印机自动识别与队列配置。

插上打印机后运行，脚本会：
  1. 扫描 USB 总线，识别打印机类设备（bInterfaceClass = 07 Printer）
  2. 读取 IEEE-1284 设备 ID，解析厂商/型号/命令集
  3. 在 CUPS 支持的驱动里匹配最佳 driver
  4. 建/更新打印队列（可指定 --name，默认用型号名）
  5. 打印测试页验证

用法（在设备上跑）:
  python3 setup_printer.py                 # 只识别，不建队列
  python3 setup_printer.py --apply         # 识别并建队列
  python3 setup_printer.py --apply --name MY_PRINTER
  python3 setup_printer.py --test          # 对已有队列打测试页
  python3 setup_printer.py --list-drivers HP
"""

import argparse
import os
import re
import subprocess
import sys
import time


def run(cmd, timeout=60, input_data=None):
    try:
        p = subprocess.run(cmd, input=input_data, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "", "超时: %s" % " ".join(cmd)
    except FileNotFoundError:
        return 127, "", "命令不存在: %s" % cmd[0]
    except Exception as exc:                                   # noqa: BLE001
        return 1, "", str(exc)


def find_usb_printers():
    """扫描 /sys/bus/usb/devices，找出打印机类接口设备。"""
    found = []
    base = "/sys/bus/usb/devices"
    if not os.path.isdir(base):
        return found

    for dev in os.listdir(base):
        devpath = os.path.join(base, dev)
        # 只看有 idVendor/idProduct 的实际设备目录
        if not os.path.exists(os.path.join(devpath, "idVendor")):
            continue

        def rd(name, default=""):
            try:
                with open(os.path.join(devpath, name)) as fh:
                    return fh.read().strip()
            except OSError:
                return default

        vid = rd("idVendor")
        pid = rd("idProduct")
        manufacturer = rd("manufacturer")
        product = rd("product")
        serial = rd("serial")

        # 检查是否有 Printer 类接口 (07)
        is_printer = False
        for sub in os.listdir(devpath):
            if not sub.startswith(dev + ":"):
                continue
            iface = os.path.join(devpath, sub)
            cls = ""
            try:
                with open(os.path.join(iface, "bInterfaceClass")) as fh:
                    cls = fh.read().strip()
            except OSError:
                pass
            if cls in ("07", "ff"):                            # 07=Printer, ff=Vendor(部分打印机)
                is_printer = True

        if not is_printer:
            # 兜底：用 lsusb 文本判断
            rc, out, _ = run(["lsusb", "-d", "%s:%s" % (vid, pid)])
            if rc == 0 and ("Printer" in out or "print" in out.lower()):
                is_printer = True

        if is_printer:
            found.append({"vid": vid, "pid": pid, "manufacturer": manufacturer,
                          "product": product, "serial": serial,
                          "path": devpath, "bus": rd("busnum"), "devnum": rd("devnum")})
    return found


def find_usb_uri(vid, pid, serial=""):
    """构造 CUPS 可用的 USB URI。"""
    rc, out, _ = run(["lpinfo", "-v"])
    if rc != 0:
        return ""
    vidpid = "%s:%s" % (vid.lower(), pid.lower())
    fallback = ""
    for line in out.splitlines():
        if "usb://" not in line:
            continue
        uri = line.strip().split(" ", 1)[-1].strip()
        if vidpid in uri.lower():
            if serial and ("serial=" not in uri):
                return uri
            return uri
        if not fallback:
            fallback = uri
    return fallback


def read_ieee1284(vid, pid):
    """从 lpinfo 或 sysfs 读 IEEE-1284 设备 ID 字符串。"""
    rc, out, _ = run(["lpinfo", "-l"])
    if rc == 0:
        for line in out.splitlines():
            if "%s" % pid.lower() in line.lower() or "%s" % vid.lower() in line.lower():
                return line.strip()
    return ""


def parse_ieee1284(devid):
    """解析 MFG / MDL / CMD / DES 字段。"""
    info = {"MFG": "", "MDL": "", "CMD": "", "DES": "", "CLS": ""}
    if not devid:
        return info
    for key in ("MANUFACTURER", "MFG"):
        m = re.search(r"%s:([^;]*)" % key, devid, re.I)
        if m:
            info["MFG"] = m.group(1).strip()
            break
    for key in ("MODEL", "MDL"):
        m = re.search(r"%s:([^;]*)" % key, devid, re.I)
        if m:
            info["MDL"] = m.group(1).strip()
            break
    for key in ("COMMAND SET", "CMD"):
        m = re.search(r"%s:([^;]*)" % key, devid, re.I)
        if m:
            info["CMD"] = m.group(1).strip()
            break
    m = re.search(r"DES:([^;]*)", devid, re.I)
    if m:
        info["DES"] = m.group(1).strip()
    m = re.search(r"CLS:([^;]*)", devid, re.I)
    if m:
        info["CLS"] = m.group(1).strip()
    return info


def match_drivers(info, extra_kw=None):
    """在 CUPS 驱动库里按厂商/型号匹配。返回 [(ppd, score, desc)] 按分数降序。"""
    rc, out, _ = run(["lpinfo", "-m"], timeout=90)
    if rc != 0:
        return []

    mfg = (info.get("MFG") or "").lower()
    mdl = (info.get("MDL") or "").lower()
    cmd = (info.get("CMD") or "").lower()
    kw = (extra_kw or "").lower()

    # 厂商名归一化
    alias = {
        "hewlett-packard": ["hp", "hewlett"],
        "hp": ["hp", "hewlett"],
        "epson": ["epson"],
        "canon": ["canon"],
        "brother": ["brother"],
        "lexmark": ["lexmark"],
        "samsung": ["samsung"],
        "ricoh": ["ricoh"],
        "Kyocera": ["Kyocera"],
        "xerox": ["xerox"],
        "dell": ["dell"],
        "lenovo": ["lenovo"],
        "pantum": ["pantum"],
        "founder": ["founder"],
        "hprt": ["hprt"],
        "zebra": ["zebra"],
        "toshiba": ["toshiba"],
    }
    keys = []
    for k, v in alias.items():
        if k in mfg:
            keys.extend(v)
    if not keys and mfg:
        keys.append(mfg.split()[0])

    scored = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("drv://") and not line.startswith("everywhere"):
            continue
        low = line.lower()
        score = 0
        for k in keys:
            if k and k in low:
                score += 30
        if mdl:
            # 型号词逐词匹配（跳过太短的词）
            for word in re.findall(r"[a-z0-9]{2,}", mdl):
                if len(word) >= 3 and word in low:
                    score += 12
        if "postscript" in cmd and "postscript" in low:
            score += 8
        if "pcl" in cmd and ("pcl" in low or "lj" in low):
            score += 6
        if "escp" in cmd and ("escp" in low or "epson" in low):
            score += 11
        if "everywhere" in low:
            score += 4                                  # 通用 IPP Everywhere，兼容面广
        if kw and kw in low:
            score += 25
        if score > 0:
            scored.append((line, score))

    scored.sort(key=lambda x: -x[1])
    return scored


def list_printers():
    printers = []
    default_name = ""
    rc, out, _ = run(["lpstat", "-d"])
    if rc == 0 and ":" in out:
        default_name = out.split(":", 1)[1].strip()
    rc, out, _ = run(["lpstat", "-p"])
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("printer "):
                name = line.split()[1]
                state = "unknown"
                if "is idle" in line:
                    state = "idle"
                elif "now printing" in line:
                    state = "printing"
                elif "disabled" in line:
                    state = "disabled"
                printers.append({"name": name, "state": state,
                                 "is_default": name == default_name})
    return printers, default_name


def create_queue(name, uri, ppd, location="", info=""):
    cmd = ["lpadmin", "-p", name, "-v", uri, "-m", ppd, "-E"]
    if location:
        cmd += ["-L", location]
    if info:
        cmd += ["-D", info]
    rc, out, err = run(cmd)
    return rc == 0, (err or out).strip()


def set_default(name):
    rc, _, err = run(["lpadmin", "-d", name])
    return rc == 0, err.strip()


def make_test_pdf(path):
    """用 gs 生成一页带设备信息与中文字样的测试页。"""
    txt = """测试页 Test Page
--------------------
打印机网关连通性测试
Print Gateway Connectivity Test

设备: CasaOS / Armbian
队列: %s
时间: %s

如果这行字清晰可读，说明链路正常。
""" % (sys.argv[0], time.strftime("%Y-%m-%d %H:%M:%S"))

    # 优先用 texttopdf / enscript，退化到手写最小 PDF
    tmp_txt = "/tmp/printer-test.txt"
    try:
        with open(tmp_txt, "w") as fh:
            fh.write(txt)
    except OSError:
        return None

    for tool, cmd in (
        ("texttopdf", ["texttopdf", tmp_txt, path]),
    ):
        rc, _, _ = run(cmd)
        if rc == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            return path

    # 兜底：手写一个最小但合法的 PDF
    lines = txt.split("\n")[:20]
    content = ["BT", "/F1 12 Tf", "14 TL", "50 780 Td"]
    for ln in lines:
        safe = ln.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        try:
            safe.encode("ascii")
        except UnicodeEncodeError:
            continue                                    # 纯手写 PDF 不含中文字体，跳过非 ASCII
        content.append("(%s) Tj T*" % safe)
    content.append("ET")
    stream = "\n".join(content)

    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        "<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = ["%PDF-1.4"]
    offsets = []
    body = ""
    for i, o in enumerate(objs, 1):
        offsets.append(len("".join(out)) + len(body) + 1)
        body += "%d 0 obj\n%s\nendobj\n" % (i, o)
    xref_pos = len("".join(out)) + len(body)
    xref = ["xref", "0 %d" % (len(objs) + 1), "0000000000 65535 f "]
    for off in offsets:
        xref.append("%010d 00000 n " % off)
    trailer = ["trailer", "<< /Size %d /Root 1 0 R >>" % (len(objs) + 1),
               "startxref", str(xref_pos), "%%EOF"]
    data = "".join(out) + body + "\n".join(xref) + "\n" + "\n".join(trailer) + "\n"
    try:
        with open(path, "w", encoding="latin-1") as fh:
            fh.write(data)
        return path
    except OSError:
        return None


def print_test(printer):
    path = "/tmp/printer-test.pdf"
    if not make_test_pdf(path):
        print("  无法生成测试页")
        return False
    rc, out, err = run(["lp", "-d", printer, "-o", "job-sheets=none", path])
    print("  lp 返回 %d: %s%s" % (rc, out.strip(), err.strip()))
    return rc == 0


def main():
    ap = argparse.ArgumentParser(description="USB 打印机识别与队列配置")
    ap.add_argument("--apply", action="store_true", help="识别后直接建队列")
    ap.add_argument("--name", default="", help="队列名，默认自动生成")
    ap.add_argument("--test", action="store_true", help="对已有队列打测试页")
    ap.add_argument("--printer", default="", help="--test 时指定队列名")
    ap.add_argument("--list-drivers", default="", help="按关键字列出驱动")
    ap.add_argument("--vendor", default="", help="强制指定厂商关键字匹配驱动")
    args = ap.parse_args()

    if args.list_drivers:
        info = {"MFG": args.list_drivers, "MDL": args.list_drivers, "CMD": ""}
        rows = match_drivers(info, args.list_drivers)
        if not rows:
            print("没有匹配到驱动（关键字: %s）" % args.list_drivers)
            return 1
        print("匹配到 %d 个驱动:" % len(rows))
        for ppd, score in rows[:40]:
            print("  [%3d] %s" % (score, ppd))
        return 0

    if args.test:
        target = args.printer
        if not target:
            plist, default_name = list_printers()
            target = default_name or (plist[0]["name"] if plist else "")
        if not target:
            print("没有可用队列。先运行 --apply 建队列。")
            return 1
        print(">> 对 %s 打测试页" % target)
        return 0 if print_test(target) else 1

    # 主流程：识别
    print("=" * 66)
    print("USB 打印机识别")
    print("=" * 66)

    rc, out, _ = run(["lsusb"])
    print("\n[USB 总线]")
    print(out.strip() or "  (无法读取)")

    print("\n[CUPS 后端发现的设备]")
    rc, out, _ = run(["lpinfo", "-v"], timeout=60)
    usb_lines = [l for l in out.splitlines() if "usb://" in l]
    print("\n".join("  " + l.strip() for l in usb_lines) or "  (未发现 USB 打印机)")

    print("\n[内核识别]")
    rc, out, _ = run(["lsmod"])
    usblp = "usblp" in out
    print("  usblp 模块: %s" % ("已加载" if usblp else "未加载"))
    rc, out, _ = run(["dmesg"])
    hits = [l for l in out.splitlines() if re.search(r"usblp|printer", l, re.I)][-6:]
    for l in hits:
        print("  %s" % l.strip())

    devs = find_usb_printers()
    print("\n[打印机类设备]")
    if not devs:
        print("  未检测到打印机类 USB 设备。")
        print()
        print("  排查建议:")
        print("   1. 确认打印机已开机并插好 USB 线")
        print("   2. USB 线要接数据线（有些线只供电不传数据）")
        print("   3. ARM 板 USB 供电弱，建议用带独立供电的 USB Hub")
        print("   4. 插拔后重新运行本脚本")
        print("   5. 执行 lsusb 看总线是否出现新设备")
        return 1

    for d in devs:
        print("  设备: %s %s" % (d["manufacturer"], d["product"]))
        print("    VID:PID  %s:%s" % (d["vid"], d["pid"]))
        print("    序列号   %s" % (d["serial"] or "(无)"))
        print("    总线      bus %s dev %s" % (d["bus"], d["devnum"]))

        uri = find_usb_uri(d["vid"], d["pid"], d["serial"])
        print("    CUPS URI %s" % (uri or "(未找到)"))

        devid = read_ieee1284(d["vid"], d["pid"])
        info = parse_ieee1284(devid)
        if info["MFG"] or info["MDL"]:
            print("    IEEE1284 MFG=%s MDL=%s" % (info["MFG"], info["MDL"]))
            if info["CMD"]:
                print("             命令集=%s" % info["CMD"])
            if info["DES"]:
                print("             描述=%s" % info["DES"])

        if not info["MFG"] and d["manufacturer"]:
            info["MFG"] = d["manufacturer"]
        if not info["MDL"] and d["product"]:
            info["MDL"] = d["product"]

        print("\n[驱动匹配 top 10]")
        rows = match_drivers(info, args.vendor)
        if not rows:
            print("  未匹配到驱动。可试: --list-drivers <厂商名>")
        for ppd, score in rows[:10]:
            print("  [%3d] %s" % (score, ppd))

        if args.apply:
            if not uri:
                print("\n!! 没有 CUPS URI，无法建队列")
                continue
            if not rows:
                print("\n!! 没有匹配到驱动，无法建队列。可用通用驱动:")
                print("   lpadmin -p NAME -v URI -m everywhere -E")
                continue

            name = args.name
            if not name:
                base = re.sub(r"[^A-Za-z0-9]+", "_",
                              "%s_%s" % (info["MFG"], info["MDL"])).strip("_").upper()
                name = (base or "PRINTER")[:40]

            ppd = rows[0][0]
            print("\n>> 建队列 %s" % name)
            print("   URI  %s" % uri)
            print("   PPD  %s" % ppd)
            ok, msg = create_queue(name, uri, ppd,
                                   location="Print Gateway",
                                   info="%s %s" % (info["MFG"], info["MDL"]))
            if not ok:
                print("   !! 失败: %s" % msg)
                print("   尝试 everywhere 通用驱动…")
                ok, msg = create_queue(name, uri, "everywhere",
                                       location="Print Gateway",
                                       info="%s %s" % (info["MFG"], info["MDL"]))
            print("   %s" % ("成功" if ok else "失败: %s" % msg))

            if ok:
                set_default(name)
                print("   已设为默认队列")
                print("\n>> 打测试页")
                print_test(name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
