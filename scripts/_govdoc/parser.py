"""中文政务网页解析器、元数据与效力抽取、全附件发现与本地多格式公文抽文本。"""
from __future__ import annotations

import html as html_lib
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

DOC_ATTACH_EXTS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".et", ".wps",
    ".ofd", ".zip", ".rar", ".7z", ".ppt", ".pptx", ".csv",
)
IMG_ATTACH_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff")
ALL_ATTACH_RE = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|et|wps|ofd|zip|rar|7z|csv|jpe?g|png|tiff?)(\?|$|#)", re.I
)
PLANNING_IMG_HINT_RE = re.compile(
    r"(附件|图则|指标|控制图|范围图|示意图|规划图|展板|红线|区位图|总平|公示图|一览表|长图|图解)", re.I
)
NOISE_IMG_RE = re.compile(
    r"(icon|logo|banner|qrcode|ewm|wx|wechat|weibo|ghs\.png|jiucuo|red\.png|barrier|wza|close|arrow|btn|bg_|foot|head|share|print|pubimages|zxtop|top_|党政机关|公网安备|找错|可信网站|无障碍)",
    re.I,
)
HTML_PAGE_EXTS = (".html", ".htm", ".shtml", ".jsp", ".asp", ".aspx", ".php")
CHROME_LINE_RE = re.compile(
    r"^(首页|无障碍|无障碍浏览|无障碍声明|长者助手|关怀版|视力保护色|人工智能朗读|智能机器人|"
    r"【?\s*字体[:：].*?】?|字号[:：].*|分享到[:：]?.*|打印本页|关闭窗口|收藏本站|繁\s*\|\s*EN|"
    r"扫一扫在手机打开当前页|版权保护|网站地图|联系我们|纠错|政府网站找错)$"
)


class _GovHtmlCleaner(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "template"}
    BLOCK_TAGS = {
        "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "table", "blockquote", "dt", "dd", "hr",
    }

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._chunks: List[str] = []
        self._in_a = False
        self._cur_a_href = ""
        self._cur_a_title = ""
        self._cur_a_buf: List[str] = []
        self.links: List[Tuple[str, str]] = []
        self.images: List[Tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        ad = dict(attrs)
        if t == "title":
            self._in_title = True
            return
        if t in self.BLOCK_TAGS:
            self._chunks.append("\n")
        elif t in ("td", "th"):
            self._chunks.append(" | ")
        elif t == "a":
            href = (ad.get("href") or "").strip()
            if href and not href.startswith(("javascript:", "#", "mailto:")):
                self._in_a = True
                self._cur_a_href = urljoin(self.base_url, href)
                self._cur_a_title = (ad.get("title") or "").strip()
                self._cur_a_buf = []
        elif t == "img":
            src = (ad.get("src") or ad.get("data-src") or ad.get("data-original") or "").strip()
            if src and not src.startswith("data:"):
                abs_src = urljoin(self.base_url, src)
                alt = (ad.get("alt") or ad.get("title") or "").strip()
                self.images.append((abs_src, alt))

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self.SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if t == "title":
            self._in_title = False
            return
        if t == "a" and self._in_a:
            txt = re.sub(r"\s+", " ", "".join(self._cur_a_buf)).strip()
            label = self._cur_a_title if len(self._cur_a_title) > len(txt) else txt
            self.links.append((self._cur_a_href, label))
            self._in_a = False
            self._cur_a_href = ""
            self._cur_a_title = ""
            self._cur_a_buf = []
        if t in self.BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title += data
            return
        if self._in_a:
            self._cur_a_buf.append(data)
        self._chunks.append(data)

    def cleaned_text(self) -> str:
        raw = html_lib.unescape("".join(self._chunks))
        lines = []
        for ln in raw.splitlines():
            ln = re.sub(r"[ \t\u3000\xa0]+", " ", ln).strip()
            ln = re.sub(r"^(\|\s*)+", "", ln).strip()
            if not ln:
                continue
            if CHROME_LINE_RE.match(ln):
                continue
            lines.append(ln)
        return "\n".join(lines)


def _extract_gov_metas(html: str) -> Dict[str, str]:
    metas: Dict[str, str] = {}
    head = html[:20000]
    for k, v in re.findall(r'<meta\s+name=["\']([^"\']+)["\']\s+content=["\']([^"\']*)["\']', head, re.I):
        metas[k] = html_lib.unescape(v.strip())
    for v, k in re.findall(r'<meta\s+content=["\']([^"\']*)["\']\s+name=["\']([^"\']+)["\']', head, re.I):
        metas.setdefault(k, html_lib.unescape(v.strip()))
    return metas


def _extract_date_from_text(pat: str, text: str) -> str:
    m = re.search(pat, text)
    if not m:
        return ""
    s = m.group(1)
    m2 = re.search(r"((?:19|20)\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", s)
    if m2:
        return f"{int(m2.group(1)):04d}-{int(m2.group(2)):02d}-{int(m2.group(3)):02d}"
    return s.strip()


def _detect_validity(text: str) -> Dict[str, Any]:
    status = ""
    details: List[str] = []

    m_explicit = re.search(r"(?:有效性|文件状态|效力状态|状态)\s*[:：]\s*(现行有效|有效|已失效|失效|已废止|废止|已修改)", text)
    if m_explicit:
        status = m_explicit.group(1)
    elif re.search(r"本(?:通知|办法|意见|规定|措施)已(?:失效|废止)", text):
        status = "已失效/废止"

    for pat in [
        r"(?:有效期|试行期)(?:为|至)?[^\n。；;]{2,40}",
        r"自\s*(?:19|20)\d{2}\s*年\d{1,2}月\d{1,2}日(?:起)?(?:施行|实施|执行)[^\n。]{0,40}",
        r"同时废止[^\n。]{0,60}",
    ]:
        for m in re.finditer(pat, text):
            s = m.group(0).strip()
            if s not in details:
                details.append(s)
            if len(details) >= 4:
                break

    return {"explicit_status": status, "clauses": details}


def parse_gov_html(url: str, html: str, *, include_content_images: bool = False) -> Dict[str, Any]:
    metas = _extract_gov_metas(html)
    cleaner = _GovHtmlCleaner(url)
    cleaner.feed(html)
    text = cleaner.cleaned_text()

    title = metas.get("ArticleTitle") or re.sub(r"\s+", " ", cleaner.title).strip()
    if not metas.get("ArticleTitle") and title:
        title = re.split(
            r"[-_—|](?:中国政府网|自然资源部|住房和城乡建设部|[\u4e00-\u9fa5]{2,8}(?:人民政府门户网站|政府在线|政务网))",
            title,
        )[0].strip()

    m_doc = re.search(r"(?:发文字号|文\s*号)\s*[:：]\s*[\r\n]*([^\n]{3,40}?号)", text)
    if not m_doc:
        m_doc = re.search(
            r"[\u4e00-\u9fa5]{1,12}[〔\[【\(](?:19|20)\d{2}[〕\]】\)]\s*\d{1,5}\s*号",
            title + "\n" + text[:3000],
        )
    doc_no = m_doc.group(1).strip() if (m_doc and m_doc.lastindex) else (m_doc.group(0).strip() if m_doc else "")

    pub_date = (
        _extract_date_from_text(r"((?:19|20)\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2})", metas.get("PubDate", ""))
        or _extract_date_from_text(r"(?:发布日期|发布时间|信息提供日期|公开日期)\s*[:：]\s*[\r\n]*([^\n]{8,25})", text[:2500])
        or _extract_date_from_text(r"/((?:19|20)\d{2}[-/]?\d{2}[-/]?\d{2})/", url)
    )
    draft_date = _extract_date_from_text(
        r"(?:成文日期|印发日期|落款日期)\s*[:：]\s*[\r\n]*([^\n]{8,25})", text[:2500]
    )
    if not draft_date:
        tail_dates = re.findall(r"(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", text[-1500:])
        if tail_dates:
            draft_date = _extract_date_from_text(r"(.*)", tail_dates[-1])

    agency = metas.get("ContentSource") or metas.get("SiteName") or ""
    if not agency:
        m_ag = re.search(r"(?:信息来源|发文机关|发布机构|制发单位|来源)\s*[:：]\s*[\r\n]*([^\n|]{2,30})", text[:2500])
        if m_ag:
            agency = m_ag.group(1).strip()

    validity = _detect_validity(text)

    attachments: List[Dict[str, str]] = []
    seen_att = set()
    for link_url, label in cleaner.links:
        ext = os.path.splitext(urlparse(link_url).path.lower())[1]
        is_direct_ext = bool(ALL_ATTACH_RE.search(link_url))
        is_dl_endpoint = (
            not is_direct_ext
            and ext not in HTML_PAGE_EXTS
            and bool(re.search(r"(downfile|/attachment/|download\?)", link_url, re.I))
        )
        if is_direct_ext or is_dl_endpoint:
            if link_url in seen_att:
                continue
            is_img = ext in IMG_ATTACH_EXTS
            if is_img and (NOISE_IMG_RE.search(link_url) or NOISE_IMG_RE.search(label)) and not PLANNING_IMG_HINT_RE.search(label):
                continue
            seen_att.add(link_url)
            attachments.append(
                {
                    "url": link_url,
                    "label": label or unquote(os.path.basename(urlparse(link_url).path)),
                    "kind": "image" if is_img else "document",
                    "ext": ext or "",
                }
            )

    auto_imgs = include_content_images or (len(text) < 500)
    for img_url, alt in cleaner.images:
        if img_url in seen_att:
            continue
        if NOISE_IMG_RE.search(img_url) or NOISE_IMG_RE.search(alt):
            continue
        is_planning_hint = bool(
            PLANNING_IMG_HINT_RE.search(alt)
            or PLANNING_IMG_HINT_RE.search(unquote(img_url))
            or "/attachment/" in img_url.lower()
            or "/img/" in img_url.lower()
            or "/imagesweb/" in img_url.lower()
            or "/upload/" in img_url.lower()
        )
        if auto_imgs or is_planning_hint:
            ext = os.path.splitext(urlparse(img_url).path.lower())[1] or ".jpg"
            if ext not in IMG_ATTACH_EXTS:
                continue
            seen_att.add(img_url)
            attachments.append(
                {
                    "url": img_url,
                    "label": alt or unquote(os.path.basename(urlparse(img_url).path)),
                    "kind": "image",
                    "ext": ext,
                }
            )

    warnings: List[str] = []
    if re.search(r"本文有删减|公开文本.*删减|略去涉密|涉密内容.*略", text):
        warnings.append("REDACTED_PUBLIC_TEXT（公开文本注明有删减，未搜到某词不代表未删减原文未提）")

    is_hdjlpt = "/hdjlpt/yjzj/" in url.lower()
    spa_markers = (
        is_hdjlpt
        or (len(text) < 350 and re.search(r"暂无附件|正在加载|请开启\s*JavaScript|<div\s+id=[\"'](app|root)[\"']", html, re.I))
    )
    if spa_markers and len(text) < 600:
        warnings.append("NEEDS_BROWSER_RENDER（疑似前端 SPA 异步渲染页，静态抓取可能丢失正文或附件，建议浏览器打开）")

    if re.search(r"试行|暂行|征求意见稿|草案", title):
        warnings.append("DRAFT_OR_TRIAL（标题含草案/征求意见稿/试行/暂行，引用前请核查是否已有正式版或到期失效）")

    if not validity["explicit_status"] and not validity["clauses"]:
        warnings.append("VALIDITY_UNSPECIFIED（页面未标注效力状态，建议反查是否已被新文取代或废止）")

    return {
        "url": url,
        "title": title,
        "doc_no": doc_no,
        "agency": agency,
        "site_name": metas.get("SiteName", ""),
        "site_id_code": metas.get("SiteIDCode", ""),
        "column_name": metas.get("ColumnName", ""),
        "pub_date": pub_date,
        "draft_date": draft_date,
        "validity": validity,
        "char_count": len(text),
        "attachments": attachments,
        "warnings": warnings,
        "text": text,
    }


def extract_ofd_text(path: str) -> Dict[str, Any]:
    """纯 Python 提取 OFD 公文版式文件全文。"""
    pages_text: List[str] = []
    with zipfile.ZipFile(path, "r") as z:
        content_xmls = sorted(
            n for n in z.namelist() if re.search(r"Pages/Page_\d+/Content\.xml$", n, re.I)
        )
        if not content_xmls:
            content_xmls = sorted(n for n in z.namelist() if n.endswith("Content.xml"))
        for name in content_xmls:
            raw = z.read(name).decode("utf-8", errors="replace")
            try:
                root = ET.fromstring(raw)
                parts = []
                for elem in root.iter():
                    if elem.tag.endswith("TextCode") and elem.text:
                        parts.append(elem.text)
                pages_text.append("".join(parts))
            except ET.ParseError:
                parts = re.findall(r"<[^>]*TextCode[^>]*>(.*?)</[^>]*TextCode>", raw)
                pages_text.append(html_lib.unescape("".join(parts)))
    full = "\n\n".join(f"--- 第 {i+1} 页 ---\n{t}" for i, t in enumerate(pages_text))
    return {
        "ok": True,
        "format": "ofd",
        "pages": len(pages_text),
        "char_count": len(full),
        "text": full,
    }


def extract_docx_text(path: str) -> Dict[str, Any]:
    lines: List[str] = []
    with zipfile.ZipFile(path, "r") as z:
        if "word/document.xml" not in z.namelist():
            return {"ok": False, "format": "docx", "error": "缺少 word/document.xml"}
        root = ET.fromstring(z.read("word/document.xml"))
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        for p in root.iter(ns + "p"):
            txt = "".join(t.text or "" for t in p.iter(ns + "t")).strip()
            if txt:
                lines.append(txt)
    full = "\n".join(lines)
    return {"ok": True, "format": "docx", "char_count": len(full), "text": full}


def extract_pdf_text(path: str, *, ocr: bool = False) -> Dict[str, Any]:
    if not shutil.which("pdftotext"):
        return {"ok": False, "format": "pdf", "error": "系统未安装 pdftotext（poppler）"}

    pages_total = 0
    if shutil.which("pdfinfo"):
        r_info = subprocess.run(["pdfinfo", path], capture_output=True, text=True)
        m = re.search(r"Pages:\s*(\d+)", r_info.stdout)
        if m:
            pages_total = int(m.group(1))

    r_txt = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True)
    if r_txt.returncode != 0:
        return {"ok": False, "format": "pdf", "error": r_txt.stderr.strip() or "pdftotext 解析失败"}

    raw_text = r_txt.stdout
    page_chunks = raw_text.split("\f")
    if page_chunks and not page_chunks[-1].strip():
        page_chunks = page_chunks[:-1]
    if not pages_total:
        pages_total = len(page_chunks)

    scanned_pages = [
        i + 1 for i, chunk in enumerate(page_chunks) if len(re.sub(r"\s+", "", chunk)) < 30
    ]
    warnings: List[str] = []
    if scanned_pages:
        warnings.append(
            f"SCANNED_PDF_PAGES（共 {pages_total} 页中有 {len(scanned_pages)} 页几乎无文字层："
            f"{scanned_pages[:15]}{'...' if len(scanned_pages) > 15 else ''}，检索结果不含这些扫描页）"
        )
    if re.search(r"本文有删减|略去涉密", raw_text):
        warnings.append("REDACTED_PUBLIC_TEXT（PDF 文本注明有删减）")

    return {
        "ok": True,
        "format": "pdf",
        "pages": pages_total,
        "scanned_pages": scanned_pages,
        "char_count": len(raw_text),
        "warnings": warnings,
        "text": raw_text,
    }


def extract_archive(path: str, out_dir: str) -> Dict[str, Any]:
    ext = os.path.splitext(path.lower())[1]
    os.makedirs(out_dir, exist_ok=True)
    if ext == ".zip":
        extracted: List[str] = []
        with zipfile.ZipFile(path, "r") as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                fname = info.filename
                if not (info.flag_bits & 0x800):
                    try:
                        fname = fname.encode("cp437").decode("gb18030")
                    except Exception:
                        pass
                safe_rel = os.path.normpath(fname).lstrip("/\\")
                if safe_rel.startswith(".."):
                    continue
                target = os.path.join(out_dir, safe_rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted.append(target)
        return {"ok": True, "format": "zip", "files": extracted}

    if ext in (".rar", ".7z"):
        if not shutil.which("7z"):
            return {"ok": False, "format": ext.lstrip("."), "error": "系统未安装 7z，无法解压 rar/7z"}
        r = subprocess.run(["7z", "x", "-y", f"-o{out_dir}", path], capture_output=True, text=True)
        if r.returncode != 0:
            return {"ok": False, "format": ext.lstrip("."), "error": r.stderr.strip() or r.stdout[-300:]}
        files = []
        for root, _, fns in os.walk(out_dir):
            for fn in fns:
                files.append(os.path.join(root, fn))
        return {"ok": True, "format": ext.lstrip("."), "files": sorted(files)}

    return {"ok": False, "format": ext, "error": f"不支持的压缩格式: {ext}"}


def extract_local_file(path: str, *, out_dir: Optional[str] = None) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"ok": False, "error": f"文件不存在: {path}"}
    ext = os.path.splitext(path.lower())[1]

    if ext == ".ofd":
        return extract_ofd_text(path)
    if ext == ".pdf":
        return extract_pdf_text(path)
    if ext == ".docx":
        return extract_docx_text(path)
    if ext in (".zip", ".rar", ".7z"):
        target = out_dir or (os.path.splitext(path)[0] + "_extracted")
        return extract_archive(path, target)
    if ext in (".doc", ".wps", ".et", ".xls", ".xlsx"):
        if not shutil.which("soffice"):
            return {"ok": False, "format": ext.lstrip("."), "error": "需要 LibreOffice (soffice) 转换 .doc/.wps/.et"}
        with tempfile.TemporaryDirectory(prefix="govdoc_conv_") as td:
            target_fmt = "csv" if ext in (".et", ".xls", ".xlsx") else "txt:Text (encoded):UTF8"
            r = subprocess.run(
                ["soffice", "--headless", "--convert-to", target_fmt, "--outdir", td, path],
                capture_output=True,
                text=True,
                timeout=90,
            )
            if r.returncode != 0:
                return {"ok": False, "format": ext.lstrip("."), "error": r.stderr.strip() or "soffice 转换失败"}
            conv_files = [os.path.join(td, f) for f in os.listdir(td) if os.path.isfile(os.path.join(td, f))]
            if not conv_files:
                return {"ok": False, "format": ext.lstrip("."), "error": "soffice 未产出转换文件"}
            txt = open(conv_files[0], "r", encoding="utf-8", errors="replace").read()
            return {"ok": True, "format": ext.lstrip("."), "char_count": len(txt), "text": txt}
    if ext in (".txt", ".md", ".html", ".htm"):
        raw = open(path, "rb").read()
        from .http_client import decode_bytes
        txt, enc, _ = decode_bytes(raw)
        return {"ok": True, "format": ext.lstrip("."), "encoding": enc, "char_count": len(txt), "text": txt}

    return {"ok": False, "format": ext, "error": f"暂不支持自动抽文本的格式: {ext}"}
