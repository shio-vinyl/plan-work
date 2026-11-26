"""零 API Key 国家级官方库与政府网站栏目直查通道。

1. 中国政府网政策文件库（sousuo.www.gov.cn/search-gov/data）：国务院文件、部委文件、公报、解读。
2. 全国行业标准与地方标准备案库（hbba.sacinfo.org.cn / dbba.sacinfo.org.cn）：TD/T、CJJ/JGJ、DB*。
3. 政府栏目翻页器：支持全国静态分页栏目与广东 gkmlpt 政府信息公开目录 JSON API。
4. 站点单次探测：读取首页 <meta name="SiteIDCode">、名称与搜索线索（不扫段）。
"""
from __future__ import annotations

import datetime
import html as html_lib
import json
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from .http_client import GovHttpClient
from .providers import SearchHit, _clean_html_text, _enrich_hit

PROVINCE_NAMES = [
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
    "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
    "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州",
    "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
]


def _ms_to_date(val: Any) -> str:
    if val is None or val == "":
        return ""
    if isinstance(val, (int, float)) or (isinstance(val, str) and val.isdigit()):
        iv = int(val)
        if iv > 10_000_000_000:
            iv //= 1000
        try:
            return datetime.datetime.fromtimestamp(iv, tz=datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
        except Exception:
            return str(val)
    return str(val)[:10]


def search_central_gov(
    client: GovHttpClient,
    query: str,
    *,
    types: Optional[List[str]] = None,
    search_field: str = "title",
    year: str = "",
    sort: str = "pubtime",
    page: int = 1,
    size: int = 10,
) -> Dict[str, Any]:
    wanted = set(types or ["gongwen", "bumenfile", "gongbao", "otherfile"])
    if wanted == {"gongwen"}:
        t_param = "zhengcelibrary_gw"
    elif wanted == {"bumenfile"}:
        t_param = "zhengcelibrary_bm"
    elif wanted <= {"gongwen", "bumenfile"}:
        t_param = "zhengcelibrary_gw_bm"
    else:
        t_param = "zhengcelibrary"

    params = {
        "t": t_param,
        "q": query.strip(),
        "timetype": "timeqb",
        "sort": sort,
        "sortType": "1",
        "searchfield": "title" if search_field == "title" else "",
        "pubtimeyear": year,
        "p": str(page),
        "n": str(min(max(size, 1), 30)),
    }
    url = "https://sousuo.www.gov.cn/search-gov/data?" + urllib.parse.urlencode(params)
    resp = client.request(url, referer="https://sousuo.www.gov.cn/zcwjk/policyDocumentLibrary", timeout=20)
    if not resp.ok:
        return {"ok": False, "error_code": resp.code_flag, "status": resp.status, "message": resp.error}

    try:
        j = json.loads(resp.text())
    except Exception as e:
        return {"ok": False, "error_code": "PARSE_ERROR", "message": f"解析中国政府网 JSON 失败: {e}"}

    svo = j.get("searchVO") or {}
    cat_map = svo.get("catMap") or {}
    hits: List[Dict[str, Any]] = []
    bucket_counts: Dict[str, int] = {}

    for cat_key, cat_val in cat_map.items():
        if not isinstance(cat_val, dict):
            continue
        cnt = int(cat_val.get("totalCount") or 0)
        bucket_counts[cat_key] = cnt
        if wanted and cat_key not in wanted:
            continue
        for item in cat_val.get("listVO") or []:
            u = (item.get("url") or "").strip()
            if not u:
                continue
            pub_str = str(item.get("pubtimeStr") or "").replace(".", "-")
            h = SearchHit(
                title=_clean_html_text(item.get("title") or ""),
                url=u,
                date=pub_str[:10],
                site_name="中国政府网",
                snippet=_clean_html_text(item.get("summary") or item.get("puborg") or "")[:300],
                doc_no=_clean_html_text(item.get("pcode") or ""),
                agency=_clean_html_text(item.get("puborg") or ""),
                category=cat_key,
                provider="central_gov",
            )
            hits.append(_enrich_hit(h).to_dict())

    return {
        "ok": True,
        "provider": "central_gov",
        "query": query,
        "t": t_param,
        "bucket_counts": bucket_counts,
        "count": len(hits),
        "hits": hits,
        "note": "中国政府网政策库不单设效力状态栏，引用前请结合发文日期与后续替代文件核验现行状态。",
    }


def _split_dbba_query_area(query: str, area: str) -> Tuple[str, str]:
    q = query.strip()
    a = area.strip()
    if not a:
        for p in PROVINCE_NAMES:
            if q.startswith(p) and len(q) > len(p):
                a = p
                q = q[len(p):].strip()
                break
    tokens = [t for t in re.split(r"\s+", q) if t]
    if len(tokens) > 1:
        q = max(tokens, key=len)
    return q, a


def search_standards(
    client: GovHttpClient,
    query: str,
    *,
    kinds: Optional[List[str]] = None,
    area: str = "",
    status: str = "",
    page: int = 1,
    size: int = 10,
) -> Dict[str, Any]:
    wanted = kinds or ["hb", "db"]
    records: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    if "hb" in wanted:
        form = urllib.parse.urlencode(
            {
                "current": str(page),
                "size": str(min(max(size, 1), 30)),
                "key": query.strip(),
                "ministry": "",
                "industry": "",
                "status": status,
                "pubdate": "",
                "date": "",
                "sort": "",
            }
        )
        resp = client.request(
            "https://hbba.sacinfo.org.cn/stdQueryList",
            method="POST",
            referer="https://hbba.sacinfo.org.cn/",
            data=form,
            timeout=20,
        )
        if resp.ok:
            try:
                j = json.loads(resp.text())
                for r in j.get("records") or []:
                    pk = r.get("pk") or ""
                    records.append(
                        {
                            "kind": "行业标准(HB)",
                            "code": r.get("code") or "",
                            "title": r.get("chName") or "",
                            "status": r.get("status") or "",
                            "agency": r.get("chargeDept") or "",
                            "industry_or_area": r.get("industry") or "",
                            "issue_date": _ms_to_date(r.get("issueDate")),
                            "effective_date": _ms_to_date(r.get("actDate")),
                            "url": f"https://hbba.sacinfo.org.cn/stdDetail/{pk}" if pk else "https://hbba.sacinfo.org.cn/",
                            "source_tier": "T1_central",
                        }
                    )
            except Exception as e:
                errors.append({"source": "hbba", "error": str(e)})
        else:
            errors.append({"source": "hbba", "code": resp.code_flag, "error": resp.error})

    if "db" in wanted:
        db_q, db_area = _split_dbba_query_area(query, area)
        req_size = 30 if db_area else min(max(size, 1), 30)
        form = urllib.parse.urlencode(
            {
                "current": str(page),
                "size": str(req_size),
                "key": db_q,
                "status": status,
                "area": db_area,
                "pubdate": "",
                "date": "",
                "sort": "",
            }
        )
        resp = client.request(
            "https://dbba.sacinfo.org.cn/stdQueryList",
            method="POST",
            referer="https://dbba.sacinfo.org.cn/",
            data=form,
            timeout=20,
        )
        if resp.ok:
            try:
                j = json.loads(resp.text())
                added_db = 0
                for r in j.get("records") or []:
                    ind_area = str(r.get("industry") or "")
                    dept = str(r.get("chargeDept") or "")
                    if db_area and (db_area not in ind_area and db_area not in dept):
                        continue
                    pk = r.get("pk") or ""
                    records.append(
                        {
                            "kind": "地方标准(DB)",
                            "code": r.get("code") or "",
                            "title": r.get("chName") or "",
                            "status": r.get("status") or "",
                            "agency": dept,
                            "industry_or_area": ind_area or db_area,
                            "issue_date": _ms_to_date(r.get("issueDate")),
                            "effective_date": _ms_to_date(r.get("actDate")),
                            "url": f"https://dbba.sacinfo.org.cn/stdDetail/{pk}" if pk else "https://dbba.sacinfo.org.cn/",
                            "source_tier": "T1_central",
                        }
                    )
                    added_db += 1
                    if added_db >= size:
                        break
            except Exception as e:
                errors.append({"source": "dbba", "error": str(e)})
        else:
            errors.append({"source": "dbba", "code": resp.code_flag, "error": resp.error})

    return {
        "ok": bool(records) or not errors,
        "provider": "sacinfo_std",
        "query": query,
        "area": area,
        "count": len(records),
        "records": records,
        "errors": errors,
        "note": "本命令直连全国行标库(TD/T、CJJ/JGJ等)与地标库(DB*)；查国标(GB/GB/T)请用 search --track standards。",
    }


class _LinkDateExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[Tuple[str, str, str]] = []
        self._in_a = False
        self._href = ""
        self._a_title = ""
        self._buf: List[str] = []
        self._context_buf: List[str] = []
        self._in_ignored = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in ("script", "style", "nav", "footer"):
            self._in_ignored = True
            return
        if self._in_ignored:
            return
        if t in ("li", "tr"):
            self._context_buf = []
        if t == "a":
            ad = dict(attrs)
            href = (ad.get("href") or "").strip()
            if href and not href.startswith(("javascript:", "#", "mailto:")):
                self._in_a = True
                self._href = href
                self._a_title = (ad.get("title") or "").strip()
                self._buf = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style", "nav", "footer"):
            self._in_ignored = False
            return
        if t == "a" and self._in_a:
            txt = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            best_title = self._a_title if len(self._a_title) > len(txt) else txt
            ctx = "".join(self._context_buf)
            self.links.append((self._href, best_title, ctx))
            self._in_a = False
            self._href = ""
            self._a_title = ""
            self._buf = []
        elif t in ("li", "tr") and self.links:
            ctx = "".join(self._context_buf)
            m = re.search(r"((?:19|20)\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2})", ctx)
            if m:
                href, title, old_ctx = self.links[-1]
                if not re.search(r"(?:19|20)\d{2}", old_ctx):
                    self.links[-1] = (href, title, m.group(1))
            self._context_buf = []

    def handle_data(self, data: str) -> None:
        if self._in_ignored:
            return
        if self._in_a:
            self._buf.append(data)
        self._context_buf.append(data)


def _norm_date(s: str) -> str:
    m = re.search(r"((?:19|20)\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", s or "")
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m2 = re.search(r"/t((?:19|20)\d{2})(\d{2})(\d{2})_", s or "")
    if m2:
        return f"{int(m2.group(1)):04d}-{int(m2.group(2)):02d}-{int(m2.group(3)):02d}"
    return ""


def _list_gd_gkmlpt(
    client: GovHttpClient,
    url: str,
    *,
    pages: int = 3,
    match_re: Optional[re.Pattern] = None,
    cat_id: str = "0",
) -> Dict[str, Any]:
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    m_cat = re.search(r"/gkmlpt/(?:index|all)/(\d+)", url)
    if m_cat:
        cat_id = m_cat.group(1)

    items: List[Dict[str, Any]] = []
    seen = set()
    fetched_pages = 0

    for p in range(1, max(1, pages) + 1):
        api_url = f"{base}/gkmlpt/api/all/{cat_id}?page={p}"
        resp = client.request(api_url, referer=f"{base}/gkmlpt/index", timeout=20, min_delay=0.6)
        if not resp.ok:
            if p == 1:
                return {"ok": False, "error_code": resp.code_flag, "status": resp.status, "message": resp.error}
            break
        try:
            j = json.loads(resp.text())
        except Exception:
            break
        arts = j.get("articles") or []
        if not arts:
            break
        fetched_pages += 1
        for a in arts:
            u = (a.get("url") or "").strip()
            if u and not u.startswith("http"):
                u = urljoin(base, u)
            if not u or u in seen:
                continue
            title = _clean_html_text(a.get("title") or "")
            doc_no = _clean_html_text(a.get("document_number") or "")
            if match_re and not (match_re.search(title) or match_re.search(doc_no)):
                continue
            seen.add(u)
            dt = _ms_to_date(a.get("created_at") or a.get("display_publish_time") or a.get("date"))
            h = SearchHit(
                title=title,
                url=u,
                date=dt,
                doc_no=doc_no,
                agency=_clean_html_text(a.get("publisher") or ""),
                category=_clean_html_text(a.get("classify_main_name") or ""),
                provider="gd_gkmlpt",
                extra={"identifier": a.get("identifier", ""), "invalid": a.get("invalid")},
            )
            items.append(_enrich_hit(h).to_dict())

    return {
        "ok": True,
        "mode": "gd_gkmlpt_json",
        "source_url": url,
        "pages_fetched": fetched_pages,
        "count": len(items),
        "hits": items,
    }


def _next_static_page_url(base_url: str, page_num: int, html: str) -> Optional[str]:
    if page_num == 1:
        return base_url
    p_idx0 = page_num - 1
    candidates = [
        rf'href=["\']([^"\']*index_{p_idx0}\.s?html?)["\']',
        rf'href=["\']([^"\']*list_{page_num}\.s?html?)["\']',
        rf'href=["\']([^"\']*[?&]page={page_num}[^"\']*)["\']',
    ]
    for pat in candidates:
        m = re.search(pat, html, re.I)
        if m:
            return urljoin(base_url, html_lib.unescape(m.group(1)))

    ext = ".htm" if re.search(r"\.htm(\?|$)", base_url, re.I) and not re.search(r"\.html", base_url, re.I) else ".html"
    if re.search(r"createPageHTML|index_1\.htm", html, re.I):
        root = base_url.rsplit("/", 1)[0] + "/" if not base_url.endswith("/") else base_url
        return f"{root}index_{p_idx0}{ext}"

    if base_url.endswith("/"):
        return f"{base_url}index_{p_idx0}.html"
    m_file = re.match(r"^(.*)/(index|list)(\.s?html?)$", base_url, re.I)
    if m_file:
        stem = m_file.group(2)
        suf = m_file.group(3)
        num = p_idx0 if stem.lower() == "index" else page_num
        return f"{m_file.group(1)}/{stem}_{num}{suf}"
    return None


def list_column(
    client: GovHttpClient,
    url: str,
    *,
    pages: int = 3,
    match: str = "",
) -> Dict[str, Any]:
    match_re = re.compile(match, re.I) if match else None

    if "/gkmlpt" in url:
        return _list_gd_gkmlpt(client, url, pages=pages, match_re=match_re)

    items: List[Dict[str, Any]] = []
    seen = set()
    fetched_pages = 0
    first_html = ""
    warnings: List[str] = []

    for p in range(1, max(1, pages) + 1):
        target = url if p == 1 else _next_static_page_url(url, p, first_html)
        if not target:
            break
        resp = client.request(target, referer=url, timeout=20, min_delay=0.6)
        if not resp.ok:
            if p == 1:
                return {"ok": False, "error_code": resp.code_flag, "status": resp.status, "message": resp.error}
            warnings.append(f"第 {p} 页停止（{resp.code_flag}: {target}）")
            break
        html = resp.text()
        if p == 1:
            first_html = html
        fetched_pages += 1

        ext = _LinkDateExtractor()
        ext.feed(html)
        page_added = 0
        base_host = (urlparse(resp.effective_url).hostname or "").lower()
        for href, title, ctx in ext.links:
            if len(title) < 8:
                continue
            abs_u = urljoin(resp.effective_url, href)
            if abs_u in seen:
                continue
            if re.search(r"^(首页|上一页|下一页|尾页|政务公开|网站地图|联系我们|关于我们|版权声明|中央政府门户网站|English)$", title, re.I):
                continue
            link_host = (urlparse(abs_u).hostname or "").lower()
            if link_host and base_host and link_host != base_host and not link_host.endswith("." + base_host):
                continue
            if match_re and not match_re.search(title):
                continue
            seen.add(abs_u)
            dt = _norm_date(abs_u) or _norm_date(ctx)
            h = SearchHit(
                title=title,
                url=abs_u,
                date=dt,
                provider="column_list",
            )
            items.append(_enrich_hit(h).to_dict())
            page_added += 1
        if page_added == 0 and not match_re and p > 1:
            break

    return {
        "ok": True,
        "mode": "static_column",
        "source_url": url,
        "pages_fetched": fetched_pages,
        "count": len(items),
        "warnings": warnings,
        "hits": items,
    }


def probe_site(client: GovHttpClient, url: str) -> Dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    resp = client.request(url, timeout=15)
    if not resp.ok:
        return {"ok": False, "url": url, "error_code": resp.code_flag, "status": resp.status, "message": resp.error}
    html = resp.text()
    metas: Dict[str, str] = {}
    for k, v in re.findall(r'<meta\s+name=["\']([^"\']+)["\']\s+content=["\']([^"\']*)["\']', html, re.I):
        metas[k] = v.strip()
    for v, k in re.findall(r'<meta\s+content=["\']([^"\']*)["\']\s+name=["\']([^"\']+)["\']', html, re.I):
        metas.setdefault(k, v.strip())

    forms = re.findall(r'<form[^>]+action=["\']([^"\']+)["\']', html, re.I)
    gd_ids = sorted(
        set(
            re.findall(r"search\.gd\.gov\.cn/search/local_msg/(\d+)", html)
            + re.findall(r"(?:site_id|sites)\s*[:=]\s*['\"]?(\d{4,8})['\"]?", html)
        )
    )
    has_gkmlpt = "/gkmlpt" in html
    search_urls = sorted(
        set(
            re.findall(
                r'(?:https?:)?//[a-z0-9.\-]+/[^\s"\'<>]*(?:search|/so/s|jrobot|was5|jsearch|gkmlpt)[^\s"\'<>]*',
                html,
                re.I,
            )
        )
    )[:8]

    return {
        "ok": True,
        "url": resp.effective_url,
        "status": resp.status,
        "encoding": resp.encoding,
        "site_name": metas.get("SiteName", ""),
        "site_domain": metas.get("SiteDomain", urlparse(resp.effective_url).hostname or ""),
        "site_id_code": metas.get("SiteIDCode", ""),
        "has_gd_gkmlpt": has_gkmlpt,
        "gd_site_ids_found": gd_ids,
        "search_forms": forms[:5],
        "search_url_hints": search_urls,
    }
