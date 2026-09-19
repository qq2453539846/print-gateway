# -*- coding: utf-8 -*-
"""
管理端支撑：凭据存储、acme.sh 封装、DDNS 同步、状态采集、`/admin` 页面。

安全模型
--------
* **凭据只落盘不回显**：`/etc/print-gateway/secrets.json` chmod 600，页面只显示掩码。
* **admin 默认只许内网**：公网来源（经 DNAT 进来的手机）访问 `/admin` 直接 404 ——
  它连「这里有个管理页」都不该知道。内网判据见 `is_lan_addr()`。
* **admin 口令与打印口令分开**：`--admin-token` 留空 = 整个 admin 功能关闭，
  而不是「无口令可进」。
* 配置与运行状态**分开存**：凭据文件只在用户改配置时写，DDNS 每分钟的状态
  写 `/var/lib/print-gateway/ddns.json` —— 避免高频重写含密钥的文件。
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import subprocess
import time

import pg_dns

SECRETS_DIR = "/etc/print-gateway"
SECRETS_PATH = os.path.join(SECRETS_DIR, "secrets.json")
TLS_DIR = os.path.join(SECRETS_DIR, "tls")
STATE_DIR = "/var/lib/print-gateway"
DDNS_STATE = os.path.join(STATE_DIR, "ddns.json")

ACME_HOME = "/root/.acme.sh"
ACME_CANDIDATES = (
    "/root/.acme.sh/acme.sh",
    "/usr/local/bin/acme.sh",
    "/opt/acme.sh/acme.sh",
)

# 证书安装到固定路径，网关只认这两个文件（acme.sh 的续期会自动覆盖它们）
CERT_CRT = "fullchain.crt"
CERT_KEY = "privkey.key"

# 续期成功后的动作：网关启动时读证书，所以续期完必须重启才生效
RELOAD_CMD = "systemctl restart print-gateway"

ACME_RELOAD_MIN_DAYS = 30          # 剩余天数低于这个值建议续期


class AdminError(Exception):
    """配置或动作失败。"""


# ---------------------------------------------------------------- 凭据存储

def default_secrets() -> dict:
    return {
        "provider": "dnspod",          # dnspod | tencent
        "record": {"root": "", "sub": ""},
        "ttl": pg_dns.DEFAULT_TTL,
        "dnspod": {"id": "", "key": ""},
        "tencent": {"secret_id": "", "secret_key": ""},
    }


def load_secrets() -> dict:
    """读配置；文件不存在或损坏时返回默认值（不抛异常，便于首启）。"""
    data = {}
    try:
        with open(SECRETS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    base = default_secrets()
    for key, val in base.items():
        if key not in data:
            data[key] = val
        elif isinstance(val, dict) and isinstance(data.get(key), dict):
            for sub_key, sub_val in val.items():
                data[key].setdefault(sub_key, sub_val)
    return data


def save_secrets(data: dict) -> None:
    """原子写 + 600 权限。目录若不存在则创建（700）。"""
    os.makedirs(SECRETS_DIR, mode=0o700, exist_ok=True)
    tmp = SECRETS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SECRETS_PATH)


def mask(value: str, keep: int = 4) -> str:
    """掩码：保留末 `keep` 位。太短的值一律全星号，避免「掩码反而泄露」。"""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * min(12, max(4, len(value) - keep)) + value[-keep:]


# 掩码串的特征：连续 4 个以上星号**开头**。
# 真实凭据（DNSPod Token / 腾讯云密钥）都是字母数字，不会长成这样。
_MASK_RE = re.compile(r"^\*{4,}")


def looks_masked(value: str) -> bool:
    return bool(_MASK_RE.match(value or ""))


def secrets_view(data: dict) -> dict:
    """给页面的**掩码视图** —— 任何情况下都别把原文发出去。"""
    dp = data.get("dnspod") or {}
    tc = data.get("tencent") or {}
    rec = data.get("record") or {}
    return {
        "provider": data.get("provider") or "dnspod",
        "ttl": int(data.get("ttl") or pg_dns.DEFAULT_TTL),
        "record": {"root": rec.get("root") or "", "sub": rec.get("sub") or ""},
        "dnspod": {"id": mask(dp.get("id", "")), "key": mask(dp.get("key", "")),
                   "has_id": bool(dp.get("id")), "has_key": bool(dp.get("key"))},
        "tencent": {"secret_id": mask(tc.get("secret_id", "")),
                    "secret_key": mask(tc.get("secret_key", "")),
                    "has_id": bool(tc.get("secret_id")),
                    "has_key": bool(tc.get("secret_key"))},
    }


def apply_changes(data: dict, changes: dict) -> dict:
    """
    把页面的改动合并进配置。

    **掩码回显的保护**：表单里字段留空 = 「不改这一项」。因为页面把敏感项渲染成
    掩码，用户直接保存时会把掩码串发回来 —— 这里一旦收到「全是星号」或空串，
    就保留原值，绝不把掩码写进配置。
    """
    if not isinstance(changes, dict):
        raise AdminError("改动内容格式不对")

    provider = changes.get("provider")
    if provider in ("dnspod", "tencent"):
        data["provider"] = provider
    if changes.get("ttl"):
        try:
            ttl = int(changes["ttl"])
        except (TypeError, ValueError):
            raise AdminError("TTL 必须是数字")
        if ttl < pg_dns.DEFAULT_TTL:
            raise AdminError("DNSPod 免费版 TTL 最小 %d 秒" % pg_dns.DEFAULT_TTL)
        data["ttl"] = ttl

    rec = changes.get("record") or {}
    if rec:
        root = (rec.get("root") or "").strip().lower().rstrip(".")
        sub = (rec.get("sub") or "").strip().lower().rstrip(".")
        if root:
            if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", root):
                raise AdminError("根域名格式不对：%s" % root)
            data["record"]["root"] = root
        if sub:
            if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", sub):
                raise AdminError("子域名格式不对（只能是字母数字与连字符）：%s" % sub)
            data["record"]["sub"] = sub

    for kind, fields in (("dnspod", ("id", "key")),
                         ("tencent", ("secret_id", "secret_key"))):
        patch = changes.get(kind) or {}
        for field in fields:
            raw = patch.get(field)
            if raw is None:
                continue
            raw = str(raw).strip()
            if not raw:
                continue                     # 空 = 不改
            # 掩码 = 不改。两道判据都要有：
            #   * 精确等于「当前值算出来的掩码」—— 最准，但依赖掩码算法一致
            #   * 形态上像掩码（连续星号开头）—— 兜底
            # 只用后者会把「用户真想设一个以 * 开头的值」误判（实际不会发生），
            # 只用前者则一旦掩码参数改了就会把掩码写进配置。
            if looks_masked(raw) or raw == mask(data[kind].get(field, "")):
                continue
            data[kind][field] = raw
    return data


def clear_credentials(data: dict) -> dict:
    """清空所有凭据（用户换账号时用）。"""
    data["dnspod"] = {"id": "", "key": ""}
    data["tencent"] = {"secret_id": "", "secret_key": ""}
    return data


def has_credentials(data: dict) -> bool:
    kind = data.get("provider") or "dnspod"
    cred = data.get(kind) or {}
    if kind == "dnspod":
        return bool(cred.get("id") and cred.get("key"))
    return bool(cred.get("secret_id") and cred.get("secret_key"))


def full_domain(data: dict) -> str:
    rec = data.get("record") or {}
    root, sub = (rec.get("root") or "").strip(), (rec.get("sub") or "").strip()
    return ("%s.%s" % (sub, root)) if (root and sub) else ""


# ---------------------------------------------------------------- 运行状态

def load_state() -> dict:
    try:
        with open(DDNS_STATE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def save_state(patch: dict) -> dict:
    """
    合并写入运行状态。**值为 None 表示删除该键** —— 用来在故障恢复后把
    `last_error` 清掉，否则页面会一直显示一条早已过期的报错，让人误判成当前故障。
    """
    state = load_state()
    for key, value in patch.items():
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    tmp = DDNS_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, DDNS_STATE)
    return state


# ---------------------------------------------------------------- 内网判据

def is_lan_addr(addr: str) -> bool:
    """
    admin 的准入判据：来源是不是内网地址。

    DNAT 不改源 IP，所以从公网进来的手机在服务端看到的是手机的公网地址 →
    判为非内网 → admin 不暴露。内网直连（含经爱快 hairpin 回来）看到 192.168.x.x
    或 IPv6 ULA → 放行。

    解析失败一律按**非内网**处理（fail-closed）。
    """
    if not addr:
        return False
    host = addr.strip()
    if host.startswith("[") and "]" in host:            # [::1]:1234
        host = host[1:host.index("]")]
    elif host.count(":") == 1:                          # 1.2.3.4:56
        host = host.split(":")[0]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


# ---------------------------------------------------------------- acme.sh

def acme_bin() -> str | None:
    for path in ACME_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _acme_env(data: dict) -> tuple[dict, str]:
    """acme.sh 的 DNS 插件凭据走环境变量；同时返回插件名。"""
    env = os.environ.copy()
    env["HOME"] = "/root"
    env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    kind = data.get("provider") or "dnspod"
    if kind == "dnspod":
        env["DP_Id"] = (data.get("dnspod") or {}).get("id", "")
        env["DP_Key"] = (data.get("dnspod") or {}).get("key", "")
        return env, "dns_dp"
    env["Tencent_SecretId"] = (data.get("tencent") or {}).get("secret_id", "")
    env["Tencent_SecretKey"] = (data.get("tencent") or {}).get("secret_key", "")
    return env, "dns_tencent"


def _run(cmd: list, env: dict, timeout: int = 300) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, env=env, timeout=timeout, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.TimeoutExpired:
        return 124, "命令超时（%ds）：%s" % (timeout, " ".join(cmd[:3]))
    except OSError as exc:
        return 127, "无法执行：%s" % exc
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def cert_paths() -> dict:
    return {"crt": os.path.join(TLS_DIR, CERT_CRT),
            "key": os.path.join(TLS_DIR, CERT_KEY)}


def _openssl_cert_info(path: str) -> dict:
    """用 openssl 读证书到期时间与主题（不依赖 acme.sh 的内部状态文件）。"""
    info = {"not_after": None, "not_before": None, "subject": "", "days_left": None}
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-in", path, "-noout", "-enddate", "-startdate", "-subject"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return info
    if proc.returncode != 0:
        return info
    text = proc.stdout.decode("utf-8", "replace")
    end = re.search(r"notAfter=(.+)", text)
    start = re.search(r"notBefore=(.+)", text)
    subject = re.search(r"subject=(.+)", text)
    if subject:
        info["subject"] = subject.group(1).strip()
    if end:
        info["not_after"] = end.group(1).strip()
        parsed = _parse_openssl_time(info["not_after"])
        if parsed:
            info["days_left"] = int((parsed - time.time()) // 86400)
    if start:
        info["not_before"] = start.group(1).strip()
    return info


def _parse_openssl_time(text: str) -> float | None:
    """
    openssl 的时间形如 `Sep 18 06:30:00 2026 GMT`（**永远是 GMT**）。

    必须用 `calendar.timegm` 而不是 `time.mktime`：mktime 会把字段当成**本地时间**，
    在 UTC+8 的机器上会整整差 8 小时 —— 证书「剩余天数」就会算错，
    而剩余天数正是「要不要续期」的判据。
    """
    if not text:
        return None
    cleaned = text.strip()
    for suffix in (" GMT", " UTC"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    for fmt in ("%b %d %H:%M:%S %Y",):
        try:
            return float(calendar.timegm(time.strptime(cleaned.strip(), fmt)))
        except ValueError:
            return None
    return None


def cert_status() -> dict:
    """证书现状：文件在不在、到期日、剩余天数、与配置域名是否匹配。"""
    paths = cert_paths()
    out = {"crt": paths["crt"], "key": paths["key"],
           "exists": False, "days_left": None, "not_after": None,
           "subject": "", "needs_renew": False}
    if not (os.path.isfile(paths["crt"]) and os.path.isfile(paths["key"])):
        return out
    out["exists"] = True
    out.update({k: v for k, v in _openssl_cert_info(paths["crt"]).items()})
    if out["days_left"] is not None:
        out["needs_renew"] = out["days_left"] < ACME_RELOAD_MIN_DAYS
    return out


def issue_cert(data: dict, force: bool = False) -> dict:
    """
    签发（或续期）证书并安装到固定路径。

    走 DNS-01 —— 家宽 80 端口被封，HTTP-01 不可行（见规划文档）。
    acme.sh 会自动注册账号、按需续期、把凭据存进 account.conf（我们随后 chmod 600）。
    """
    binary = acme_bin()
    if not binary:
        raise AdminError("未安装 acme.sh（设备上执行：curl -sL https://get.acme.sh | sh -s -- --nocron）")
    domain = full_domain(data)
    if not domain:
        raise AdminError("请先填写根域名与子域名")
    if not has_credentials(data):
        raise AdminError("请先录入 DNS 凭据")

    env, plugin = _acme_env(data)
    paths = cert_paths()
    os.makedirs(TLS_DIR, mode=0o700, exist_ok=True)

    steps = []

    issue_cmd = [binary, "--issue", "--dns", plugin, "-d", domain,
                 "--keylength", "ec-256", "--server", "letsencrypt"]
    if force:
        issue_cmd.append("--force")
    code, log = _run(issue_cmd, env, timeout=420)
    steps.append({"step": "issue", "code": code, "log": log[-4000:]})
    if code != 0:
        return {"ok": False, "steps": steps,
                "message": "签发失败（acme.sh 返回 %d）" % code}

    install_cmd = [binary, "--install-cert", "-d", domain, "--ecc",
                   "--key-file", paths["key"],
                   "--fullchain-file", paths["crt"],
                   "--reloadcmd", RELOAD_CMD]
    code2, log2 = _run(install_cmd, env, timeout=120)
    steps.append({"step": "install", "code": code2, "log": log2[-2000:]})
    if code2 != 0:
        return {"ok": False, "steps": steps,
                "message": "证书已签发但安装失败（返回 %d）" % code2}

    # account.conf 里有明文 DNS 凭据，收权限
    for conf in (os.path.join(ACME_HOME, "account.conf"),):
        if os.path.isfile(conf):
            try:
                os.chmod(conf, 0o600)
            except OSError:
                pass
    for path in (paths["crt"], paths["key"]):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    return {"ok": True, "steps": steps, "domain": domain,
            "message": "%s 证书就绪，网关重启后 8443 生效" % domain,
            "cert": cert_status()}


def auto_renew() -> dict:
    """
    由 systemd timer 调用：证书剩余天数不足就续期。

    比 acme.sh 自带的 cron 好在**判据可见**（同一份 cert_status 逻辑），
    且续期动作写进 ddns 状态文件，admin 页面能看到历史。
    """
    status = cert_status()
    if not status["exists"]:
        return {"ok": False, "skipped": True, "message": "还没有证书"}
    if not status["needs_renew"]:
        return {"ok": True, "skipped": True,
                "message": "剩余 %s 天，暂不续期" % status["days_left"]}
    data = load_secrets()
    result = issue_cert(data, force=False)
    save_state({"last_renew": {"at": int(time.time()), "ok": result["ok"],
                               "message": result["message"]}})
    return result


# ---------------------------------------------------------------- DDNS

def sync_ddns(data: dict | None = None, ip: str | None = None,
              force: bool = False, timeout: float = 15.0) -> dict:
    """
    把 `sub`.`root` 的 A 记录对齐到当前公网 IP。

    只碰这一条记录；**先查后写**，IP 没变就不动 DNS（省 API 配额、避免无谓的
    记录变更）。返回结构里带 `changed` 供调用方判断。
    """
    data = load_secrets() if data is None else data
    root = (data.get("record") or {}).get("root", "")
    sub = (data.get("record") or {}).get("sub", "")
    if not (root and sub):
        raise AdminError("请先在 /admin 里填写根域名与子域名")
    if not has_credentials(data):
        raise AdminError("请先录入 DNS 凭据")

    fqdn = "%s.%s" % (sub, root)
    ip = ip or pg_dns.detect_wan_ip()
    dns = pg_dns.make_dns(data, timeout=timeout)
    before = dns.get_ip(root, sub)
    action = "skip"
    if before != ip or force:
        action = dns.set_ip(root, sub, ip)

    now = int(time.time())
    result = {"ok": True, "domain": fqdn, "ip": ip, "before": before,
              "action": action, "changed": action in ("created", "modified"),
              "at": now}
    # 这一轮跑通了 → 把历史上那条报错清掉，否则页面会一直挂着早已恢复的故障
    save_state({"last_check": now, "ip": ip, "domain": fqdn, "action": action,
                "before": before, "last_error": None, "last_error_at": None})
    if result["changed"]:
        save_state({"last_change": now})
    return result


def verify_credentials(data: dict) -> dict:
    """只读自检（admin 的「检测凭据」按钮）。委托给 pg_dns，不带写操作。"""
    root = (data.get("record") or {}).get("root", "")
    sub = (data.get("record") or {}).get("sub", "")
    if not (root and sub):
        return {"ok": False, "message": "请先填写根域名与子域名"}
    if not has_credentials(data):
        return {"ok": False, "message": "请先录入 DNS 凭据"}
    return pg_dns.verify_credentials(data, root, sub)


# ---------------------------------------------------------------- TLS 实况

# 服务单元里写着 `--tls-port`；drop-in 优先（它才是实际生效的那份）
GATEWAY_UNIT_PATHS = (
    "/etc/systemd/system/print-gateway.service.d/override.conf",
    "/etc/systemd/system/print-gateway.service",
)


def parse_tls_port(text: str) -> int:
    """从 systemd 单元文本里抽 `--tls-port`；取不到或越界一律返回 0。"""
    m = re.search(r"--tls-port[=\s]+(\d{1,5})", text or "")
    if not m:
        return 0
    port = int(m.group(1))
    return port if 0 < port < 65536 else 0


def _port_listening(port: int, host: str = "127.0.0.1",
                    timeout: float = 0.4) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def detect_tls_runtime() -> dict:
    """
    探测 HTTPS 是否**真在跑**。

    为什么不直接读配置：配置写着 8443 不等于真在监听 —— 证书读不出来、端口被占、
    进程刚崩，都会让「配置」和「实况」分家。而页面要回答的是后者，所以先取配置里
    的端口，再实连一次，连得上才算启用。回到环境探测不到（例如在本机 Windows 上
    跑单测）时安静降级为未启用。
    """
    port = 0
    for path in GATEWAY_UNIT_PATHS:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                port = parse_tls_port(fh.read())
        except OSError:
            continue
        if port:
            break
    return {"enabled": _port_listening(port), "port": port}


# ---------------------------------------------------------------- 状态汇总

def collect_status(data: dict, tls_enabled: bool | None = None,
                   tls_port: int = 0) -> dict:
    """一次拿齐页面要显示的东西。所有子项都容错 —— 任何一项失败都不该让页面白屏。"""
    state = load_state()
    fqdn = full_domain(data)
    view = secrets_view(data)

    current = None
    if fqdn and has_credentials(data):
        try:
            current = pg_dns.make_dns(data, timeout=10).get_ip(
                (data.get("record") or {}).get("root", ""),
                (data.get("record") or {}).get("sub", ""))
        except pg_dns.DnsError:
            current = None

    return {
        "domain": fqdn,
        "record": {"root": (data.get("record") or {}).get("root") or "",
                   "sub": (data.get("record") or {}).get("sub") or ""},
        "view": view,
        "provider": view["provider"],
        "provider_label": pg_dns.PROVIDERS[view["provider"]].label
                          if view["provider"] in pg_dns.PROVIDERS else view["provider"],
        "has_credentials": has_credentials(data),
        "ttl": int(data.get("ttl") or pg_dns.DEFAULT_TTL),
        "current_ip": current,
        "state": state,
        "cert": cert_status(),
        "acme_installed": bool(acme_bin()),
        "tls": ({"enabled": bool(tls_enabled), "port": int(tls_port or 0)}
                if tls_enabled is not None else detect_tls_runtime()),
        "now": int(time.time()),
    }


# ---------------------------------------------------------------- 管理页面

ADMIN_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>打印网关 · 管理</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--line:#e3e5e8;--text:#1f2329;--muted:#8a9099;
--accent:#2b6de5;--accent-soft:#eef3fe;--warn:#b8730b;--err:#d2453c;--ok:#1d7a3d}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line);padding:12px 16px;
display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:20}
header h1{font-size:16px;font-weight:600;margin:0;flex:1}
header .badge{font-size:12px;color:var(--muted)}
.wrap{max-width:860px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px;margin-bottom:12px}
.card h2{font-size:14px;font-weight:600;margin:0 0 10px;
display:flex;align-items:center;gap:8px}
.card h2 .sub{font-weight:400;color:var(--muted);font-size:12px}
.row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.row:last-child{margin-bottom:0}
.row>label{flex:0 0 96px;color:var(--muted);font-size:13px}
select,input[type=text],input[type=password],input[type=number]{flex:1;min-width:0;
padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:#fff;
font-size:14px;color:var(--text);font-family:inherit}
button{font:inherit;border-radius:8px;border:1px solid var(--line);background:#fff;
padding:8px 14px;cursor:pointer;color:var(--text)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button:disabled{opacity:.5;cursor:not-allowed}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.kv{font-size:13px;color:var(--muted);line-height:2}
.kv b{color:var(--text);font-weight:600}
.kv code{background:#f0f1f3;padding:1px 6px;border-radius:4px;font-size:12px}
.msg{border-radius:8px;padding:9px 12px;font-size:13px;margin-bottom:12px;display:none}
.msg.show{display:block}
.msg.ok{background:#e9f7ee;color:var(--ok)}
.msg.err{background:#fdeceb;color:var(--err)}
.tag{display:inline-block;padding:1px 8px;border-radius:5px;font-size:12px;
background:var(--accent-soft);color:var(--accent)}
.tag.warn{background:#fdf3e2;color:var(--warn)}
.tag.err{background:#fdeceb;color:var(--err)}
.tag.ok{background:#e9f7ee;color:var(--ok)}
pre{background:#1f2329;color:#d8dade;padding:10px;border-radius:7px;font-size:12px;
overflow:auto;max-height:240px;margin:10px 0 0;white-space:pre-wrap;
word-break:break-all;display:none}
pre.show{display:block}
.hint{font-size:12px;color:var(--muted);margin-top:8px;line-height:1.8}
.qrgrid{display:flex;gap:10px;flex-wrap:wrap}
.qrcell{flex:1 1 130px;min-width:130px;border:1px solid var(--line);border-radius:9px;
padding:10px;text-align:center;background:#fff;cursor:pointer}
.qrcell.on{border-color:var(--accent);background:var(--accent-soft)}
.qrcell.off{opacity:.5;cursor:not-allowed}
.qrcell img{width:100%;max-width:132px;height:auto;display:block;margin:6px auto;
cursor:zoom-in}
/* 点开放大。屏幕上的 132px 预览对手机摄像头太密了：公网码 64 字符 = QR 版本 5
   （37 模块）+ 静区，折算下来只有 3.2px/模块；实测再小到 96px 就彻底解不出来。
   放大到 320px 约 7.8px/模块，斜着拍、隔半米拍都还有余量。 */
#qrZoom{position:fixed;inset:0;background:rgba(0,0,0,.75);display:none;z-index:99;
align-items:center;justify-content:center;flex-direction:column;gap:14px;padding:20px}
#qrZoom.show{display:flex}
#qrZoom .zcard{background:#fff;border-radius:14px;padding:16px;text-align:center;
max-width:min(92vw,420px)}
#qrZoom .zcard img{width:min(78vw,320px);height:auto;display:block;margin:0 auto}
#qrZoom .zurl{color:var(--muted);font-size:11px;word-break:break-all;
margin-top:8px;max-width:min(78vw,320px)}
#qrZoom .ztip{color:#f2f3f5;font-size:13px;line-height:1.7;text-align:center;
max-width:min(92vw,420px)}
.qrcell b{display:block;font-size:13px}
.qrcell span{display:block;font-size:11px;color:var(--muted);word-break:break-all;
line-height:1.5;margin-top:2px}
.qrcell .blank{height:132px;display:flex;align-items:center;justify-content:center;
color:var(--muted);font-size:12px}
.warnbox{background:#fdf6e7;border:1px solid #f0dcb0;color:var(--warn);
border-radius:8px;padding:9px 11px;font-size:12.5px;line-height:1.7;margin-top:10px}
.hide{display:none}
@media(max-width:520px){.row{flex-wrap:wrap}.row>label{flex:0 0 100%;margin-bottom:2px}}
</style>
</head>
<body>
<header>
  <h1>打印网关 · 管理</h1>
  <span class="badge">__VERSION__</span>
</header>
<div class="wrap">
  <div class="msg" id="msg"></div>

  <div class="card">
    <h2>公开访问<span class="sub">只读</span></h2>
    <div class="kv" id="overview">读取中…</div>
  </div>

  <div class="card">
    <h2>二维码贴纸<span class="sub">印出来贴在打印机旁，扫码即用</span></h2>
    <div id="stickerBox"><div class="kv">读取中…</div></div>
    <div class="row" style="margin-top:12px">
      <label>每页版式</label>
      <select id="stLayout">
        <option value="1">整页 1 张（码最大）</option>
        <option value="2">A5 两张（剪开分贴）</option>
        <option value="4">A6 四张（名片大小）</option>
      </select>
    </div>
    <div id="stWarn"></div>
    <div class="btns">
      <button class="primary" id="bSticker">生成贴纸并预览</button>
      <span style="color:var(--muted);font-size:12px;align-self:center">生成后进打印面板，可看预览再出纸</span>
    </div>
  </div>

  <!-- 放大看 / 放大扫。图仍走那个只读接口 /admin/qr.svg，内容由服务端按 kind 现算，
       前端只是把它渲染大一点，不参与决定码里写什么 -->
  <div id="qrZoom">
    <div class="zcard">
      <img id="qrZoomImg" alt="连接码">
      <div class="zurl" id="qrZoomUrl"></div>
    </div>
    <div class="ztip">把手机对准这个放大的二维码扫描。<br>点任意处或按 Esc 关闭</div>
  </div>

  <div class="card">
    <h2>域名解析<span class="sub">DDNS 每 60 秒自动对齐</span></h2>
    <div class="row"><label>根域名</label>
      <input type="text" id="root" placeholder="example.com" autocomplete="off"></div>
    <div class="row"><label>子域名</label>
      <input type="text" id="sub" placeholder="print" autocomplete="off"></div>
    <div class="row"><label>TTL</label>
      <input type="number" id="ttl" min="600" step="60" placeholder="600">
    </div>
    <div class="btns">
      <button class="primary" id="bSaveCfg">保存配置</button>
      <button id="bVerify">检测凭据</button>
      <button id="bSync">立即同步 DNS</button>
    </div>
    <div class="kv" id="dnsState" style="margin-top:12px"></div>
  </div>

  <div class="card">
    <h2>DNS 凭据<span class="sub">只存本机，页面只回显掩码</span></h2>
    <div class="row"><label>凭据类型</label>
      <select id="provider">
        <option value="dnspod">DNSPod Token（ID + Key）</option>
        <option value="tencent">腾讯云 API 密钥</option>
      </select>
    </div>
    <div id="boxDnspod">
      <div class="row"><label>Token ID</label>
        <input type="password" id="dpId" autocomplete="new-password"></div>
      <div class="row"><label>Token Key</label>
        <input type="password" id="dpKey" autocomplete="new-password"></div>
    </div>
    <div id="boxTencent" class="hide">
      <div class="row"><label>SecretId</label>
        <input type="password" id="tId" autocomplete="new-password"></div>
      <div class="row"><label>SecretKey</label>
        <input type="password" id="tKey" autocomplete="new-password"></div>
    </div>
    <div class="btns">
      <button class="primary" id="bSaveCred">保存凭据</button>
      <button id="bClearCred">清空凭据</button>
    </div>
    <div class="hint">输入框留空 = 不修改已保存的值。建议用**子账号 / 最小权限令牌**，
只授权目标域名的 DNS 修改权 —— 令牌泄露时损失可控。</div>
  </div>

  <div class="card">
    <h2>HTTPS 证书<span class="sub">Let's Encrypt · DNS-01 · ECDSA</span></h2>
    <div class="kv" id="certState">读取中…</div>
    <div class="btns">
      <button class="primary" id="bIssue">签发 / 续期</button>
      <button id="bForce">强制重签</button>
    </div>
    <div class="hint">签发走 DNS-01（往 <code>_acme-challenge</code> 写 TXT 验证所有权），
<b>不需要任何入站端口</b> —— 家宽 80/443 被封也不影响。证书装到固定路径后
网关会自动重启以载入新证书。</div>
    <pre id="certLog"></pre>
  </div>
</div>

<script>
var $ = function(s){ return document.querySelector(s); };
var busy = false;

function msg(text, kind){
  var el = $('#msg');
  el.className = 'msg show ' + (kind || 'ok');
  el.textContent = text;
}
function hideMsg(){ $('#msg').className = 'msg'; }
function log(text){
  var el = $('#certLog');
  el.className = 'pre show';
  el.textContent = text;
}
function when(ts){
  if(!ts) return '—';
  var d = new Date(ts * 1000);
  return d.toLocaleString();
}

function req(path, body){
  return fetch('/admin/api/' + path, {
    method: body ? 'POST' : 'GET',
    headers: body ? {'Content-Type': 'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: 'same-origin',
    cache: 'no-store'
  }).then(function(r){
    return r.json().catch(function(){ return {error: '响应不是合法 JSON'}; })
      .then(function(j){
        if(!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
        return j;
      });
  });
}

function withBusy(btn, text, fn){
  if(busy) return;
  busy = true;
  var old = btn.textContent;
  btn.disabled = true;
  btn.textContent = text;
  hideMsg();
  fn().catch(function(e){ msg(e.message, 'err'); })
    .then(function(){ busy = false; btn.disabled = false; btn.textContent = old; });
}

function tag(ok, yes, no){
  return '<span class="tag ' + (ok ? 'ok' : 'warn') + '">' + (ok ? yes : no) + '</span>';
}

// 定义在顶层：按钮是 innerHTML 重建出来的，内联 onclick 只能找到全局函数
function copyUrl(){
  var el = $('#pubUrl');
  if(!el) return;
  el.select();
  el.setSelectionRange(0, 999);
  var done = function(){ msg('链接已复制到剪贴板'); };
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(el.value).then(done, function(){
      document.execCommand('copy'); done();
    });
  } else {
    document.execCommand('copy'); done();
  }
}

function render(st){
  var tls = st.tls || {};
  var cert = st.cert || {};
  var s = st.state || {};

  $('#overview').innerHTML =
    '域名 <b>' + (st.domain || '未配置') + '</b>' +
    (tls.enabled ? ' · HTTPS <b>:' + tls.port + '</b>' + tag(true, '已启用', '') 
                 : ' · HTTPS <b>未启用</b>' + tag(false, '', '未启用')) +
    '<br>凭据 <b>' + (st.provider_label || '') + '</b>' +
    (st.has_credentials ? tag(true, '已录入', '') : tag(false, '', '未录入')) +
    '<br>当前解析 <code>' + (st.current_ip || '无记录') + '</code>' +
    '<br>acme.sh ' + (st.acme_installed ? tag(true, '已安装', '') : tag(false, '', '未安装')) +
    (st.public_url
      ? '<div style="margin-top:10px"><b>公网访问链接</b>'
        + '<span style="color:var(--muted)">（含口令，可直接做成二维码）</span>'
        + '<div style="display:flex;gap:8px;margin-top:6px">'
        + '<input type="text" id="pubUrl" readonly value="' + st.public_url + '">'
        + '<button onclick="copyUrl()">复制</button></div></div>'
      : '');


  $('#dnsState').innerHTML =
    '上次检查 <b>' + when(s.last_check) + '</b>' +
    (s.last_check ? '（' + (s.action === 'skip' ? 'IP 未变，跳过'
        : s.action === 'created' ? '新建记录'
        : s.action === 'modified' ? '更新记录' : s.action) + '）' : '') +
    '<br>上次变更 <b>' + when(s.last_change) + '</b>' +
    (s.ip ? '<br>同步目标 IP <code>' + s.ip + '</code>' : '');

  var days = cert.days_left;
  $('#certState').innerHTML = cert.exists
    ? '状态 ' + (cert.needs_renew ? tag(false, '', '建议续期') : tag(true, '有效', '')) +
      '<br>到期 <b>' + (cert.not_after || '—') + '</b>' +
      (days === null ? '' : '<br>剩余 <b>' + days + '</b> 天') +
      '<br>主题 <code>' + (cert.subject || '—') + '</code>'
    : '状态 ' + tag(false, '', '未签发') +
      '<br><span style="color:var(--muted)">填好域名与凭据后点「签发 / 续期」</span>';

  // 表单填值：敏感项只在「当前为空」时填掩码提示，避免用户误存掩码
  var rec = st.record || {};
  $('#root').value = rec.root || '';
  $('#sub').value = rec.sub || '';
  $('#ttl').value = st.ttl || 600;
  $('#provider').value = st.provider || 'dnspod';
  toggleProvider();

  var v = st.view || {};
  var dp = v.dnspod || {}, tc = v.tencent || {};
  setHint('#dpId', dp.has_id ? dp.id : '', 'Token ID');
  setHint('#dpKey', dp.has_key ? dp.key : '', 'Token Key');
  setHint('#tId', tc.has_id ? tc.secret_id : '', 'SecretId');
  setHint('#tKey', tc.has_key ? tc.secret_key : '', 'SecretKey');

  renderSticker(st);
}

var QR_KINDS = ['lan', 'wan', 'app'];

// 贴纸预览。图不走 JSON，直接让 <img> 去取 /admin/qr.svg?kind=xxx ——
// 内容由服务端按 kind 现算，前端不传 URL（否则这就是个任意二维码接口了）。
function renderSticker(st){
  var info = st.sticker || {};
  var html = '<div class="qrgrid">';
  QR_KINDS.forEach(function(k){
    var it = info[k] || {};
    var ok = !!it.available;
    html += '<label class="qrcell ' + (ok ? 'on' : 'off') + '">'
      + '<input type="checkbox" class="qrk" value="' + k + '"'
      + (ok ? ' checked' : ' disabled') + '>'
      + (ok ? '<img src="/admin/qr.svg?kind=' + k + '" alt="' + k + '">'
            : '<div class="blank">当前不可用</div>')
      + '<b>' + (it.label || k) + '</b>'
      + '<span>' + (ok ? it.sub : (it.reason || '不可用')) + '</span>'
      + '</label>';
  });
  html += '</div>';
  $('#stickerBox').innerHTML = html;

  Array.prototype.forEach.call(document.querySelectorAll('.qrk'), function(cb){
    cb.addEventListener('change', function(){
      this.closest('.qrcell').classList.toggle('on', this.checked);
    });
  });

  // 公网码里带着口令 —— 这条必须说清楚，不然贴出去等于开放打印权限
  var wan = info.wan || {};
  $('#stWarn').innerHTML = wan.available
    ? '<div class="warnbox"><b>注意：公网码里带着访问口令。</b>'
      + '贴纸一旦贴到别人也能看到的地方，等于把口令公开 —— '
      + '外网任何人扫一下就能打印。只在自己能掌握范围的机器旁用它。</div>'
    : '';
}

function setHint(sel, masked, label){
  var el = $(sel);
  if(!el.value) el.placeholder = masked ? ('已保存：' + masked + '（留空不改）') : label;
}

function toggleProvider(){
  var p = $('#provider').value;
  $('#boxDnspod').className = p === 'dnspod' ? '' : 'hide';
  $('#boxTencent').className = p === 'tencent' ? '' : 'hide';
}

function refresh(){
  return req('status').then(render);
}

function collect(){
  return {
    provider: $('#provider').value,
    ttl: parseInt($('#ttl').value, 10) || 600,
    record: {root: $('#root').value, sub: $('#sub').value},
    dnspod: {id: $('#dpId').value, key: $('#dpKey').value},
    tencent: {secret_id: $('#tId').value, secret_key: $('#tKey').value}
  };
}

$('#provider').addEventListener('change', toggleProvider);

$('#bSaveCfg').addEventListener('click', function(){
  withBusy(this, '保存中…', function(){
    return req('config', collect()).then(function(r){
      msg(r.message || '配置已保存');
      return refresh();
    });
  });
});

$('#bSaveCred').addEventListener('click', function(){
  withBusy(this, '保存中…', function(){
    return req('config', collect()).then(function(r){
      msg(r.message || '凭据已保存');
      ['#dpId','#dpKey','#tId','#tKey'].forEach(function(s){ $(s).value = ''; });
      return refresh();
    });
  });
});

$('#bClearCred').addEventListener('click', function(){
  if(!confirm('清空已保存的 DNS 凭据？下次签发/DDNS 需要重新录入。')) return;
  withBusy(this, '清空中…', function(){
    return req('config', {clear_credentials: true}).then(function(r){
      msg(r.message || '凭据已清空');
      return refresh();
    });
  });
});

$('#bVerify').addEventListener('click', function(){
  withBusy(this, '检测中…', function(){
    return req('verify').then(function(r){
      msg(r.message, r.ok ? 'ok' : 'err');
    });
  });
});

$('#bSync').addEventListener('click', function(){
  withBusy(this, '同步中…', function(){
    return req('ddns').then(function(r){
      msg(r.message, r.ok ? 'ok' : 'err');
      return refresh();
    });
  });
});

function issue(force){
  return req('cert', {force: force}).then(function(r){
    var lines = (r.steps || []).map(function(s){
      return '=== ' + s.step + ' (exit ' + s.code + ') ===\n' + (s.log || '');
    }).join('\n');
    log(lines || r.message);
    msg(r.message, r.ok ? 'ok' : 'err');
    return refresh();
  });
}

$('#bIssue').addEventListener('click', function(){
  withBusy(this, '签发中…（约 1 分钟）', function(){ return issue(false); });
});
$('#bForce').addEventListener('click', function(){
  if(!confirm('强制重签：会向 Let\'s Encrypt 重新申请一次证书。继续？')) return;
  withBusy(this, '重签中…', function(){ return issue(true); });
});

$('#bSticker').addEventListener('click', function(){
  var kinds = [];
  Array.prototype.forEach.call(document.querySelectorAll('.qrk'), function(cb){
    if(cb.checked) kinds.push(cb.value);
  });
  if(!kinds.length){ msg('至少选一个二维码', 'err'); return; }
  var layout = parseInt($('#stLayout').value, 10) || 1;
  withBusy(this, '生成中…', function(){
    // 生成的是一个**作业**，不是直接出纸 —— 跳到打印面板让用户先看预览、
    // 选打印机和纸盒。份数、纸型这些面板里本来就有。
    return req('sticker', {kinds: kinds, layout: layout}).then(function(r){
      msg('已生成，正在打开打印面板…');
      location.href = '/?job=' + encodeURIComponent(r.id);
    });
  });
});

// 点小图放大 —— 放大了才好扫。
// 这里必须 preventDefault：图片在 <label> 里，不挡掉默认行为的话，点一下会
// 顺手把这张码勾选/取消掉，而用户的本意只是「放大看看能不能扫」。
(function(){
  var zi = $('#qrZoomImg'), zu = $('#qrZoomUrl'), box = $('#qrZoom');
  document.addEventListener('click', function(e){
    var t = e.target;
    if(!t || t.tagName !== 'IMG' || !t.closest('.qrcell')) return;
    e.preventDefault();
    zi.src = t.getAttribute('src');
    var sub = t.parentNode.querySelector('span');
    zu.textContent = sub ? sub.textContent : '';
    box.classList.add('show');
  }, true);
  function close(){ box.classList.remove('show'); zi.removeAttribute('src'); }
  box.addEventListener('click', function(e){ e.preventDefault(); close(); });
  document.addEventListener('keydown', function(e){
    if(e.key === 'Escape' || e.keyCode === 27) close();
  });
})();

refresh().catch(function(e){ msg('加载状态失败：' + e.message, 'err'); });
</script>
</body>
</html>
"""
