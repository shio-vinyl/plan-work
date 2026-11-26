"""基于 curl 的限速与熔断 HTTP 客户端（零外部依赖，兼容 python3 -I）。

要点：
- 始终核验 HTTP 状态码、Content-Type 与最终跳转地址。
- 按主机限速，并在遇到 403/412/521/429 时自动熔断停手，不尝试绕过 WAF。
- 下载二进制附件时校验文件头魔数，拒存伪装成 PDF/Office 的 HTML 错误页。
- 自动探测中文编码（UTF-8 / GB18030），避免静默吞字。
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
META_SENTINEL = b"\n__GOVDOC_META__\t"

STRICT_HOST_DELAYS = {
    "search.gd.gov.cn": 1.3,
    "sousuo.www.gov.cn": 0.6,
    "hbba.sacinfo.org.cn": 0.6,
    "dbba.sacinfo.org.cn": 0.6,
}

MAGIC_RULES: Dict[str, List[bytes]] = {
    ".pdf": [b"%PDF-"],
    ".zip": [b"PK\x03\x04", b"PK\x05\x06"],
    ".docx": [b"PK\x03\x04", b"PK\x05\x06"],
    ".xlsx": [b"PK\x03\x04", b"PK\x05\x06"],
    ".pptx": [b"PK\x03\x04", b"PK\x05\x06"],
    ".ofd": [b"PK\x03\x04", b"PK\x05\x06"],
    ".rar": [b"Rar!"],
    ".7z": [b"7z\xbc\xaf\x27\x1c"],
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".gif": [b"GIF87a", b"GIF89a"],
    ".tif": [b"II*\x00", b"MM\x00*"],
    ".tiff": [b"II*\x00", b"MM\x00*"],
}
LEGACY_OFFICE_EXTS = {".doc", ".xls", ".wps", ".et"}
LEGACY_OFFICE_MAGICS = [b"\xd0\xcf\x11\xe0", b"PK\x03\x04", b"{\\rtf", b"<?xml"]


class CircuitOpenError(RuntimeError):
    """主机已触发 403/412/521/429 熔断保护。"""


@dataclass
class HttpResponse:
    url: str
    effective_url: str
    status: int
    content_type: str
    body: bytes
    error: str = ""
    code_flag: str = "OK"
    encoding: str = "utf-8"
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.code_flag == "OK"

    def text(self) -> str:
        decoded, enc, warn = decode_bytes(self.body, self.content_type)
        self.encoding = enc
        if warn and warn not in self.warnings:
            self.warnings.append(warn)
        return decoded


def decode_bytes(raw: bytes, content_type: str = "") -> Tuple[str, str, Optional[str]]:
    """按 BOM / Content-Type / HTML meta / UTF-8 / GB18030 顺序探测编码。"""
    if not raw:
        return "", "utf-8", None

    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace"), "utf-8-sig", None

    candidates: List[str] = []
    m_ct = re.search(r"charset\s*=\s*['\"]?([a-zA-Z0-9_\-]+)", content_type or "", re.I)
    if m_ct:
        candidates.append(m_ct.group(1).lower())

    head = raw[:4096]
    m_meta = re.search(rb"<meta[^>]+charset\s*=\s*['\"]?([a-zA-Z0-9_\-]+)", head, re.I)
    if m_meta:
        candidates.append(m_meta.group(1).decode("ascii", errors="ignore").lower())

    norm_candidates: List[str] = []
    for c in candidates + ["utf-8", "gb18030"]:
        if c in ("gbk", "gb2312", "gb_2312", "gb2312-80"):
            c = "gb18030"
        elif c in ("utf8",):
            c = "utf-8"
        if c not in norm_candidates:
            norm_candidates.append(c)

    for enc in norm_candidates:
        try:
            return raw.decode(enc), enc, None
        except (UnicodeDecodeError, LookupError):
            continue

    return raw.decode("utf-8", errors="replace"), "utf-8", "ENCODING_FALLBACK_REPLACE"


def validate_binary_bytes(raw: bytes, filename: str, content_type: str = "") -> Tuple[bool, str]:
    """根据扩展名校验文件头魔数，拒绝伪装成附件的 HTML 报错页。"""
    if not raw:
        return False, "EMPTY_FILE"

    ext = os.path.splitext(filename.lower())[1]
    head = raw[:2048].lstrip()
    head_l = head.lower()
    ct_l = (content_type or "").lower()

    looks_html = (
        head_l.startswith(b"<!doctype html")
        or head_l.startswith(b"<html")
        or b"<title>" in head_l[:512]
        or "text/html" in ct_l
    )

    if ext in MAGIC_RULES:
        for sig in MAGIC_RULES[ext]:
            if head.startswith(sig):
                return True, "OK"
        if looks_html:
            return False, "FAKE_BINARY_HTML"
        return False, f"MAGIC_MISMATCH({ext})"

    if ext in LEGACY_OFFICE_EXTS:
        for sig in LEGACY_OFFICE_MAGICS:
            if head.startswith(sig):
                return True, "OK"
        if looks_html:
            if b"schemas-microsoft-com:office" in raw[:4096].lower() and len(raw) > 1500:
                return True, "HTML_DISGUISED_DOC"
            return False, "FAKE_BINARY_HTML"
        return True, "OK"

    return True, "OK"


class GovHttpClient:
    """带限速与熔断保护的 curl 封装。"""

    def __init__(self, default_delay: float = 0.4, user_agent: str = DEFAULT_UA):
        self.default_delay = default_delay
        self.user_agent = user_agent
        self._last_req: Dict[str, float] = {}
        self._host_blocks: Dict[str, int] = {}
        self._tripped_hosts: Dict[str, str] = {}

    def _host(self, url: str) -> str:
        return (urlparse(url).hostname or "").lower()

    def _wait_slot(self, host: str, min_delay: Optional[float] = None) -> None:
        delay = max(
            self.default_delay,
            STRICT_HOST_DELAYS.get(host, 0.0),
            min_delay or 0.0,
        )
        if delay <= 0:
            return
        last = self._last_req.get(host)
        if last is not None:
            remain = delay - (time.monotonic() - last)
            if remain > 0:
                time.sleep(remain)
        self._last_req[host] = time.monotonic()

    def _check_circuit(self, host: str) -> None:
        if host in self._tripped_hosts:
            raise CircuitOpenError(
                f"主机 {host} 已触发防拦截熔断（原因：{self._tripped_hosts[host]}），"
                "为避免触发封禁已自动停手。请改走上级政府/公报/全国搜索 API 或交互浏览器。"
            )

    def _classify_response(self, host: str, status: int, body: bytes, stderr: str) -> Tuple[str, str]:
        if status == 0:
            s_l = stderr.lower()
            if "ssl" in s_l or "certificate" in s_l or "(60)" in s_l or "(35)" in s_l:
                return "SSL_ERROR", stderr.strip() or "SSL/TLS 证书校验失败"
            if "(28)" in s_l or "timed out" in s_l:
                return "NETWORK_ERROR", stderr.strip() or "请求超时"
            if "(52)" in s_l or "empty reply" in s_l:
                return "EMPTY_BODY", stderr.strip() or "服务器返回空响应"
            return "NETWORK_ERROR", stderr.strip() or "网络连接失败"

        if status in (412, 521, 514):
            self._tripped_hosts[host] = f"HTTP {status} (WAF/JS质询)"
            return "WAF_BLOCKED", f"HTTP {status}（疑似 WAF JS 质询，需浏览器访问或转查上级/公报）"

        if status == 429:
            self._tripped_hosts[host] = "HTTP 429 (频率限制)"
            return "RATE_LIMITED", "HTTP 429（请求频率受限，已熔断停手）"

        if status == 403:
            self._host_blocks[host] = self._host_blocks.get(host, 0) + 1
            if host == "search.gd.gov.cn" or self._host_blocks[host] >= 2:
                self._tripped_hosts[host] = "HTTP 403 Forbidden"
            return "WAF_BLOCKED", "HTTP 403 Forbidden（访问被拒绝或触发限流）"

        if status == 404:
            return "HTTP_404", "HTTP 404（页面不存在或已下架/改版）"

        if status >= 500:
            return "NETWORK_ERROR", f"HTTP {status}（目标站点服务端错误）"

        if not body:
            return "EMPTY_BODY", f"HTTP {status} 但响应体为空"

        self._host_blocks[host] = 0
        return "OK", ""

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        referer: Optional[str] = None,
        data: Optional[str] = None,
        json_body: Optional[bytes] = None,
        timeout: int = 25,
        min_delay: Optional[float] = None,
        follow_redirects: bool = True,
    ) -> HttpResponse:
        host = self._host(url)
        self._check_circuit(host)
        self._wait_slot(host, min_delay=min_delay)

        cmd = [
            "curl", "-sS", "-g",
            "--max-time", str(timeout),
            "-A", self.user_agent,
            "-w", "\n__GOVDOC_META__\t%{http_code}\t%{content_type}\t%{size_download}\t%{url_effective}",
        ]
        if follow_redirects:
            cmd.append("-L")
        if method.upper() != "GET":
            cmd += ["-X", method.upper()]
        if referer:
            cmd += ["-e", referer]
        if headers:
            for k, v in headers.items():
                cmd += ["-H", f"{k}: {v}"]

        stdin_bytes = None
        if json_body is not None:
            cmd += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
            stdin_bytes = json_body
        elif data is not None:
            cmd += ["--data-raw", data]

        cmd.append(url)
        proc = subprocess.run(cmd, input=stdin_bytes, capture_output=True)
        raw = proc.stdout
        stderr = proc.stderr.decode("utf-8", errors="replace")

        status = 0
        content_type = ""
        eff_url = url
        body = raw

        if META_SENTINEL in raw:
            body, meta_raw = raw.rsplit(META_SENTINEL, 1)
            parts = meta_raw.decode("utf-8", errors="replace").strip().split("\t")
            if len(parts) >= 1 and parts[0].isdigit():
                status = int(parts[0])
            if len(parts) >= 2:
                content_type = parts[1]
            if len(parts) >= 4 and parts[3]:
                eff_url = parts[3]

        flag, err_msg = self._classify_response(host, status, body, stderr)
        return HttpResponse(
            url=url,
            effective_url=eff_url,
            status=status,
            content_type=content_type,
            body=body,
            error=err_msg,
            code_flag=flag,
        )

    def download_file(
        self,
        url: str,
        dest_path: str,
        *,
        referer: Optional[str] = None,
        timeout: int = 180,
        resume_retries: int = 1,
    ) -> Tuple[bool, str, Dict[str, object]]:
        """下载附件并校验文件头魔数，支持断点续传重试一次。"""
        host = self._host(url)
        self._check_circuit(host)
        self._wait_slot(host)

        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        tmp_path = dest_path + ".part"

        status = 0
        content_type = ""
        eff_url = url
        stderr = ""

        for attempt in range(resume_retries + 1):
            cmd = [
                "curl", "-sS", "-g", "-L",
                "--max-time", str(timeout),
                "-A", self.user_agent,
                "-o", tmp_path,
                "-w", "%{http_code}\t%{content_type}\t%{size_download}\t%{url_effective}",
            ]
            if attempt > 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                cmd += ["-C", "-"]
            if referer:
                cmd += ["-e", referer]
            cmd.append(url)

            proc = subprocess.run(cmd, capture_output=True)
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            meta = proc.stdout.decode("utf-8", errors="replace").strip().split("\t")
            status = int(meta[0]) if meta and meta[0].isdigit() else 0
            content_type = meta[1] if len(meta) >= 2 else ""
            eff_url = meta[3] if len(meta) >= 4 and meta[3] else url

            if proc.returncode == 0 and 200 <= status < 300:
                break
            if proc.returncode not in (18, 28, 56):
                break
            time.sleep(1.0)

        size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
        sample = b""
        if size > 0:
            with open(tmp_path, "rb") as f:
                sample = f.read(4096)

        flag, err_msg = self._classify_response(host, status, sample, stderr)
        info: Dict[str, object] = {
            "url": url,
            "effective_url": eff_url,
            "status": status,
            "content_type": content_type,
            "size": size,
            "path": dest_path,
        }
        if flag != "OK":
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            info["error"] = err_msg
            return False, flag, info

        valid, vcode = validate_binary_bytes(sample, dest_path, content_type)
        if not valid:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            info["error"] = f"下载内容校验失败：{vcode}（实际 Content-Type={content_type}，非合法目标文件）"
            return False, vcode, info

        os.replace(tmp_path, dest_path)
        if vcode != "OK":
            info["warning"] = vcode
        return True, "OK", info
