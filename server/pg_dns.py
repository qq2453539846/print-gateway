# -*- coding: utf-8 -*-
"""
DNS 记录操作：公网 IP 探测 + DNSPod 客户端（旧版 Token / 腾讯云 TC3 签名）。

设计约束
--------
* **只做「查一条」和「写一条」**，调用方只能传 (root, sub)。本模块刻意**不提供**
  「列出全部记录」之类的能力 —— 根域 `example.com` 的 A 记录有别的程序在维护
  （2026-09-18 实测：SOA serial 与 WAN2 拨号时间只差 2 秒，而爱快自己的 DDNS
  日志停在 2022-08-09），误伤它会让用户的根域解析断掉。
* **纯标准库**。腾讯云 TC3-HMAC-SHA256 用 hmac/hashlib 手写，不引入 SDK。
* 免费版 DNSPod 的 TTL 下限是 **600 秒**（不是 60），写死更小的值会被 API 拒绝；
  所以 `DEFAULT_TTL = 600`，可用 `--ttl` 覆盖（专业版才能更小）。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# DNSPod 免费套餐的 TTL 下限；更小的值 API 不接受
DEFAULT_TTL = 600

# 线路名（中文）。免费版只有「默认」，用别的会报错
RECORD_LINE = "默认"

_UA = "print-gateway-ddns/1.0 (+https://github.com/qq2453539846/print-gateway)"
_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")

# 取公网 IP 的源。按「国内可达性」排序，逐个兜底；都不行才报错。
# 全部是 GET、无鉴权、返回体很小（限读 512 字节）。
WAN_SOURCES = (
    "https://myip.ipip.net",
    "http://ip.3322.net",
    "https://4.ipw.cn",
    "https://api.ipify.org",
)


class DnsError(Exception):
    """DNS 操作失败（网络、鉴权、API 报错都归到这里）。"""


# ---------------------------------------------------------------- 公网 IP

def is_public_ipv4(text: str) -> bool:
    """判断是否为**公网** IPv4（排除私网/回环/保留/CGNAT 段）。"""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if ip.version != 4:
        return False
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return False
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    # 100.64.0.0/10 运营商级 NAT：ipaddress 归为 is_private，这里再兜一层，
    # 因为「拿到 CGNAT 地址」是最容易被误当成公网 IP 的坑
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return False
    return True


def detect_wan_ip(timeout: float = 8.0) -> str:
    """
    探测本机出口公网 IPv4。

    多源依次兜底 —— 单个服务抽风不该让 DDNS 停摆。任一源返回了**公网** IPv4
    就返回；全部失败抛 DnsError（错误信息里带上每个源的原因，便于排查）。
    """
    errors = []
    for url in WAN_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as fh:
                raw = fh.read(512).decode("utf-8", "replace").strip()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors.append("%s → %s" % (url, exc))
            continue
        match = _IPV4_RE.search(raw)
        if match and is_public_ipv4(match.group(1)):
            return match.group(1)
        errors.append("%s → 未取到公网 IPv4（%r）" % (url, raw[:48]))
    raise DnsError("无法获取公网 IPv4：\n" + "\n".join(errors))


# ---------------------------------------------------------------- 传输

def _post(url: str, data: bytes, headers: dict, timeout: float) -> dict:
    """POST 并解析 JSON。把 HTTP 错误体也带回错误信息里（API 的报错都在体里）。"""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            body = fh.read(65536)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(1024).decode("utf-8", "replace")
        except Exception:                                        # noqa: BLE001
            pass
        raise DnsError("HTTP %s：%s" % (exc.code, detail[:300] or exc.reason)) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DnsError("网络错误：%s" % exc) from exc
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise DnsError("响应不是合法 JSON：%s" % body[:200]) from exc


# ---------------------------------------------------------------- DNSPod 旧版

class DnsPodToken:
    """
    旧版 DNSPod Token（dnspod.cn 控制台「API 密钥」里的 ID + Key）。

    走 `https://dnsapi.cn`，表单 POST，鉴权靠 `login_token=ID,KEY`。
    acme.sh 的 `dns_dp` 插件用的就是这套。
    """

    name = "dnspod"
    label = "DNSPod Token"
    endpoint = "https://dnsapi.cn/"

    def __init__(self, id_: str, key: str, ttl: int = DEFAULT_TTL, timeout: float = 15.0):
        if not id_ or not key:
            raise DnsError("DNSPod Token 的 ID / Key 不能为空")
        self.id = id_.strip()
        self.key = key.strip()
        self.ttl = int(ttl)
        self.timeout = timeout

    def _call(self, action: str, **fields) -> dict:
        payload = {
            "login_token": "%s,%s" % (self.id, self.key),
            "format": "json",
            "lang": "cn",
            "error_on_empty": "no",
        }
        payload.update({k: v for k, v in fields.items() if v is not None})
        body = urllib.parse.urlencode(payload).encode("utf-8")
        resp = _post(self.endpoint + action, body, {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _UA,
        }, self.timeout)
        status = resp.get("status") or {}
        code = str(status.get("code", ""))
        if code != "1":
            raise DnsError("DNSPod %s 失败：[%s] %s"
                           % (action, code, status.get("message", "未知错误")))
        return resp

    def get_ip(self, root: str, sub: str) -> str | None:
        """查子域名的 A 记录值；不存在返回 None。"""
        resp = self._call("Record.List", domain=root, sub_domain=sub,
                          record_type="A")
        for rec in (resp.get("records") or []):
            if rec.get("type") == "A" and rec.get("name") == sub:
                return rec.get("value")
        return None

    def set_ip(self, root: str, sub: str, ip: str) -> str:
        """写入 A 记录：存在就改、不存在就建。返回 'modified' / 'created'。"""
        resp = self._call("Record.List", domain=root, sub_domain=sub,
                          record_type="A")
        target = None
        for rec in (resp.get("records") or []):
            if rec.get("type") == "A" and rec.get("name") == sub:
                target = rec
                break
        if target is None:
            self._call("Record.Create", domain=root, sub_domain=sub,
                       record_type="A", record_line=RECORD_LINE,
                       value=ip, ttl=self.ttl)
            return "created"
        if target.get("value") == ip:
            return "unchanged"
        self._call("Record.Modify", domain=root, domain_id=resp.get("domain", {}).get("id"),
                   record_id=target.get("id"), sub_domain=sub,
                   record_type="A", record_line=RECORD_LINE,
                   value=ip, ttl=self.ttl)
        return "modified"


# ---------------------------------------------------------------- 腾讯云

class TencentDns:
    """
    腾讯云 API 密钥（SecretId + SecretKey），走 `dnspod.tencentcloudapi.com`。

    TC3-HMAC-SHA256 签名手写实现，避免引入 tencentcloud-sdk（armv7 上装不动）。
    acme.sh 的 `dns_tencent` 插件用的就是这套。
    """

    name = "tencent"
    label = "腾讯云 API 密钥"
    host = "dnspod.tencentcloudapi.com"
    service = "dnspod"
    version = "2021-03-23"

    def __init__(self, secret_id: str, secret_key: str,
                 ttl: int = DEFAULT_TTL, timeout: float = 15.0):
        if not secret_id or not secret_key:
            raise DnsError("腾讯云 SecretId / SecretKey 不能为空")
        self.sid = secret_id.strip()
        self.skey = secret_key.strip()
        self.ttl = int(ttl)
        self.timeout = timeout

    @staticmethod
    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _call(self, action: str, payload: dict) -> dict:
        ts = int(time.time())
        date = time.strftime("%Y-%m-%d", time.gmtime(ts))
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        hashed_payload = hashlib.sha256(body.encode("utf-8")).hexdigest()

        # 参与签名的头必须按字典序、小写
        canonical_headers = ("content-type:application/json; charset=utf-8\n"
                             "host:%s\nx-tc-action:%s\n" % (self.host, action.lower()))
        signed_headers = "content-type;host;x-tc-action"
        canonical_request = "\n".join([
            "POST", "/", "",
            canonical_headers,
            signed_headers,
            hashed_payload,
        ])
        scope = "%s/%s/tc3_request" % (date, self.service)
        string_to_sign = "\n".join([
            "TC3-HMAC-SHA256", str(ts), scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        k_date = self._hmac(("TC3" + self.skey).encode("utf-8"), date)
        k_service = self._hmac(k_date, self.service)
        k_signing = self._hmac(k_service, "tc3_request")
        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"),
                             hashlib.sha256).hexdigest()

        headers = {
            "Authorization": ("TC3-HMAC-SHA256 Credential=%s/%s, "
                              "SignedHeaders=%s, Signature=%s"
                              % (self.sid, scope, signed_headers, signature)),
            "Content-Type": "application/json; charset=utf-8",
            "Host": self.host,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(ts),
            "X-TC-Version": self.version,
            "User-Agent": _UA,
        }
        resp = _post("https://" + self.host, body.encode("utf-8"), headers,
                     self.timeout)
        rsp = resp.get("Response") or {}
        if "Error" in rsp:
            err = rsp["Error"]
            raise DnsError("腾讯云 %s 失败：[%s] %s"
                           % (action, err.get("Code"), err.get("Message")))
        return rsp

    def _find(self, root: str, sub: str) -> dict | None:
        rsp = self._call("DescribeRecordList", {
            "Domain": root, "Subdomain": sub, "RecordType": "A"})
        for rec in (rsp.get("RecordList") or []):
            if rec.get("Type") == "A" and rec.get("Name") == sub:
                return rec
        return None

    def get_ip(self, root: str, sub: str) -> str | None:
        rec = self._find(root, sub)
        return rec.get("Value") if rec else None

    def set_ip(self, root: str, sub: str, ip: str) -> str:
        rec = self._find(root, sub)
        if rec is None:
            self._call("CreateRecord", {
                "Domain": root, "SubDomain": sub, "RecordType": "A",
                "RecordLine": RECORD_LINE, "Value": ip, "TTL": self.ttl})
            return "created"
        if rec.get("Value") == ip:
            return "unchanged"
        self._call("ModifyRecord", {
            "Domain": root, "RecordId": rec.get("RecordId"), "SubDomain": sub,
            "RecordType": "A", "RecordLine": RECORD_LINE, "Value": ip,
            "TTL": self.ttl})
        return "modified"


# ---------------------------------------------------------------- 工厂

PROVIDERS = {
    DnsPodToken.name: DnsPodToken,
    TencentDns.name: TencentDns,
}


def make_dns(secrets: dict, timeout: float = 15.0):
    """
    按 secrets 里的 `provider` 造客户端。

    secrets 结构（见 pg_admin.default_secrets）：
        {"provider": "dnspod"|"tencent", "dnspod": {...}, "tencent": {...}, "ttl": 600}
    """
    kind = (secrets or {}).get("provider") or "dnspod"
    ttl = int((secrets or {}).get("ttl") or DEFAULT_TTL)
    if kind not in PROVIDERS:
        raise DnsError("未知的凭据类型：%s" % kind)
    if kind == "dnspod":
        cred = (secrets.get("dnspod") or {})
        return DnsPodToken(cred.get("id", ""), cred.get("key", ""), ttl, timeout)
    cred = (secrets.get("tencent") or {})
    return TencentDns(cred.get("secret_id", ""), cred.get("secret_key", ""),
                      ttl, timeout)


def verify_credentials(secrets: dict, root: str, sub: str) -> dict:
    """
    只读自检：凭据能不能查到目标子域名。

    返回 {"ok": bool, "current": ip|None, "message": str}。
    **不写任何记录** —— 这是给 admin 页面的「检测凭据」按钮用的，
    用户点一下就写 DNS 是危险的。
    """
    try:
        dns = make_dns(secrets)
        current = dns.get_ip(root, sub)
    except DnsError as exc:
        return {"ok": False, "current": None, "message": str(exc)}
    if current is None:
        return {"ok": True, "current": None,
                "message": "凭据有效；%s.%s 还没有 A 记录，首次同步会新建"
                           % (sub, root)}
    return {"ok": True, "current": current,
            "message": "凭据有效；当前 %s.%s → %s" % (sub, root, current)}
