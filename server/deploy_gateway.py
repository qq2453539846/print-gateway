#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
部署 print_gateway 到嵌入式设备。

- 上传 print_gateway.py 到 /opt/print-gateway/
- 生成 systemd unit 并 enable + start
- 建 spool 目录
- 打印访问地址与二维码

用法（在本地 Windows 上跑）:
  python deploy_gateway.py --host gateway-host --port 8080
"""

import argparse
import os
import shlex
import subprocess
import sys

REMOTE_DIR = "/opt/print-gateway"
SERVICE_NAME = "print-gateway"
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))


def sh(cmd, check=True, timeout=60, capture=True):
    p = subprocess.run(cmd, stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.PIPE if capture else None,
                       timeout=timeout)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    if check and p.returncode != 0:
        raise RuntimeError("命令失败: %s\n%s\n%s" % (" ".join(cmd), out, err))
    return p.returncode, out, err


def remote(host, script, check=True, timeout=120):
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, script]
    return sh(cmd, check=check, timeout=timeout)


def scp_to(host, local, remote_path):
    cmd = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", local,
           "%s:%s" % (host, remote_path)]
    return sh(cmd, timeout=120)


def build_unit(rdir, port, printer, token, title, maxmb, hostip, apk=""):
    """
    生成 systemd unit。

    注意：可选参数为空时必须整项省略。若写成 `--printer ` 再跟续行符，
    systemd 拼接后会变成 `--printer --token ...`，argparse 会报
    "expected one argument" 并让服务陷入重启循环。
    """
    args = [
        "/usr/bin/python3", "%s/print_gateway.py" % rdir,
        "--port", str(port),
        "--bind", "0.0.0.0",
        "--spool", "/var/spool/print-gateway",
        "--title", title,
        "--max-mb", str(maxmb),
        "--host-display", hostip,
    ]
    if printer:
        args += ["--printer", printer]
    if token:
        args += ["--token", token]
    if apk:
        args += ["--apk", apk]
    exec_start = " ".join(shlex.quote(a) for a in args)

    return """[Unit]
Description=Print Gateway - 扫码上传打印服务
After=network-online.target cups.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={rdir}
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=print-gateway

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/var/spool/print-gateway /tmp

[Install]
WantedBy=multi-user.target
""".format(rdir=rdir, exec_start=exec_start)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="gateway-host", help="SSH 配置里的 Host 别名")
    ap.add_argument("--hostip", default="", help="页脚显示用的设备 IP")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--printer", default="",
                    help="锁定默认打印队列；留空则跟随 CUPS 系统默认打印机")
    ap.add_argument("--token", default="", help="访问口令，留空免密")
    ap.add_argument("--title", default="扫码打印")
    ap.add_argument("--maxmb", type=int, default=32)
    ap.add_argument("--apk", default="",
                    help="本地安卓 App 包路径；上传到远端并托管在 GET /app")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    # 行缓冲，否则重定向到文件时看不到中间进度
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:                                          # noqa: BLE001
        pass

    if args.uninstall:
        print(">> 停止并卸载服务")
        remote(args.host, "systemctl stop %s 2>/dev/null; systemctl disable %s 2>/dev/null; "
                          "rm -f /etc/systemd/system/%s.service; systemctl daemon-reload; "
                          "rm -rf %s; echo UNINSTALLED" % (
                              SERVICE_NAME, SERVICE_NAME, SERVICE_NAME, REMOTE_DIR),
               check=False)
        print(">> 已卸载（spool 目录保留，如需清理执行 rm -rf /var/spool/print-gateway）")
        return

    if not args.hostip:
        rc, out, _ = remote(args.host,
                            "ip -4 route get 1.1.1.1 2>/dev/null | "
                            "grep -oP 'src \\K[0-9.]+' | head -1", check=False)
        args.hostip = out.strip() or "192.168.1.100"

    print(">> 目标设备: %s  地址: %s:%d" % (args.host, args.hostip, args.port))

    print(">> 创建远端目录")
    remote(args.host, "mkdir -p %s && echo MKDIR_OK" % REMOTE_DIR)

    print(">> 上传 print_gateway.py")
    scp_to(args.host, os.path.join(LOCAL_DIR, "print_gateway.py"),
           REMOTE_DIR + "/print_gateway.py")

    print(">> 上传二维码脚本")
    qr_local = os.path.join(LOCAL_DIR, "gen_qr.py")
    if os.path.exists(qr_local):
        scp_to(args.host, qr_local, REMOTE_DIR + "/gen_qr.py")

    apk_remote = ""
    if args.apk:
        if not os.path.isfile(args.apk):
            print("!! --apk 指定的文件不存在：%s" % args.apk)
            sys.exit(1)
        apk_name = os.path.basename(args.apk)
        apk_remote = "%s/%s" % (REMOTE_DIR, apk_name)
        print(">> 上传 App 包（%s）" % apk_name)
        scp_to(args.host, args.apk, apk_remote)

    print(">> 建 spool 目录")
    remote(args.host, "mkdir -p /var/spool/print-gateway && chmod 700 /var/spool/print-gateway "
                      "&& echo SPOOL_OK")

    print(">> 语法自检")
    rc, out, err = remote(args.host,
                          "cd %s && python3 -m py_compile print_gateway.py && echo COMPILE_OK"
                          % REMOTE_DIR, check=False)
    if "COMPILE_OK" not in out:
        print("!! 远端编译失败：\n%s\n%s" % (out, err))
        sys.exit(1)
    print("   编译通过")

    print(">> 写 systemd unit")
    unit = build_unit(REMOTE_DIR, args.port, args.printer, args.token,
                      args.title, args.maxmb, args.hostip, apk=apk_remote)
    local_unit = os.path.join(LOCAL_DIR, ".print-gateway.service.tmp")
    with open(local_unit, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(unit)
    scp_to(args.host, local_unit, "/tmp/%s.service" % SERVICE_NAME)
    remote(args.host, "mv /tmp/%s.service /etc/systemd/system/%s.service "
                      "&& chmod 644 /etc/systemd/system/%s.service && echo UNIT_OK"
                      % (SERVICE_NAME, SERVICE_NAME, SERVICE_NAME))
    try:
        os.remove(local_unit)
    except OSError:
        pass

    print(">> 重载并启动")
    rc, out, err = remote(args.host,
                          "systemctl daemon-reload && systemctl enable %s >/dev/null 2>&1; "
                          "systemctl reset-failed %s 2>/dev/null; "
                          "systemctl restart %s; sleep 4; systemctl is-active %s"
                          % (SERVICE_NAME, SERVICE_NAME, SERVICE_NAME, SERVICE_NAME),
                          check=False)
    print("   服务状态: %s" % out.strip())

    if out.strip() != "active":
        rc, logs, _ = remote(args.host,
                             "journalctl -u %s -n 30 --no-pager 2>&1" % SERVICE_NAME,
                             check=False)
        print("!! 启动失败，日志：\n%s" % logs)
        sys.exit(1)

    print(">> 自检 HTTP")
    rc, out, err = remote(args.host,
                          "sleep 2; curl -s -o /dev/null -w '%%{http_code}' "
                          "http://127.0.0.1:%d/healthz" % args.port, check=False)
    print("   /healthz -> HTTP %s" % out.strip())

    rc, body, _ = remote(args.host,
                         "curl -s http://127.0.0.1:%d/healthz" % args.port, check=False)
    print("   %s" % body.strip())

    print()
    print("=" * 56)
    print("部署完成")
    print("=" * 56)
    print("手机访问 : http://%s:%d" % (args.hostip, args.port))
    if args.token:
        print("带口令   : http://%s:%d/?t=%s" % (args.hostip, args.port, args.token))
    if apk_remote:
        print("App 下载 : http://%s:%d/app" % (args.hostip, args.port))
    print("CUPS 管理: http://%s:631" % args.hostip)
    print()
    print("查看日志 : ssh %s journalctl -u %s -f" % (args.host, SERVICE_NAME))
    print("重启服务 : ssh %s systemctl restart %s" % (args.host, SERVICE_NAME))
    print("卸载     : python deploy_gateway.py --host %s --uninstall" % args.host)


if __name__ == "__main__":
    main()
