"""可插拔的全国政务与规划检索提供方。

支持：
- baidu : 百度千帆 Web Search API（环境变量 BAIDU_QIANFAN_API_KEY 或 QIANFAN_API_KEY）
- bocha : 博查 AI Web Search API（环境变量 BOCHA_API_KEY）
- iqs   : 阿里云 IQS UnifiedSearch API（环境变量 ALIYUN_IQS_API_KEY）
- gd    : 广东政府网站群统一搜索（零 Key，仅限显式已验证站点 ID，带限速与 403 熔断）
- multi : 并行调用已配置的全部商业搜索 API 并合并去重
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .http_client import GovHttpClient
from .registry import RoutePlan, classify_source

DOC_NO_RE = re.compile(
    r"[\u4e00-\u9fa5]{1,12}[〔\[【\(](?:19|20)\d{2}[〕\]】\)]\s*\d{1,5}\s*号"
)
DOC_NO_PREFIX_TRIM_RE = re.compile(
    r"^(?:依据|根据|按照|按|落实|印发|执行|参照|详见|见|附|关于|的|为|对|经|以|由|及|与|和)+"
)
INDEX_PATH_RE = re.compile(
    r"(?:/|/(?:m_)?index\.s?html?|/list(?:_\d+)?\.s?html?|/default\.s?html?)$", re.I
)
INDEX_TITLE_RE = re.compile(
    r"^(?:>\s*)?(?:[\u4e00-\u9fa5]{2,12}(?:政策文件库|信息公开|通知公告|意见征集|政府公报|门户网站欢迎您|首页))$",
    re.I,
)

TRACK_BOOST_RE: Dict[str, re.Pattern] = {
    "upper-policy": re.compile(r"(规划纲要|十五五|十四五|工作报告|政府公报|指导意见|实施意见|若干措施|办法|条例|规定|批复|通知)"),
    "spatial-plan": re.compile(r"(国土空间|详细规划|控规|法定图则|混合|用途转换|功能转换|新型产业用地|通则式|管理办法|实施办法|指导意见|若干措施|清单|批复)"),
    "housing-renewal": re.compile(r"(城市更新单元|更新单元规划|更新单元计划|保障性住房|保障性租赁住房|配租|配售|住房发展|好房子|物业|计划公告|规划草案|意愿公示)"),
    "land-supply": re.compile(r"(供地计划|土地供应|挂牌出让|土地出让|规划设计条件|基准地价|土地储备|宗地)"),
    "stats-census": re.compile(r"(统计公报|统计年鉴|经济运行|人口普查|经济普查|法治政府|国民经济)"),
    "standards": re.compile(r"(标准|规范|导则|准则|规程|GB|TD/T|CJJ|JGJ|DB\d)"),
    "benchmark": re.compile(r"(管理办法|实施办法|指导意见|若干措施|操作指引|正面清单|负面清单|试行|政策解读)"),
}
TRACK_DEMOTE_RE: Dict[str, re.Pattern] = {
    "upper-policy": re.compile(r"(挂牌出让|中标公告|采购公告|采购项目|任免职务|行政处罚)"),
    "spatial-plan": re.compile(r"(挂牌出让公告|使用权挂牌|中标公告|采购公告|采购项目|任免职务|农用地转用|行政处罚)"),
    "housing-renewal": re.compile(r"(任免职务|采购公告|中标公告|工作报告|工作总结|建议的答复|提案答复)"),
    "land-supply": re.compile(r"(任免职务|采购公告|党工委工作报告)"),
    "stats-census": re.compile(r"(挂牌出让|中标公告|采购公告|任免职务)"),
    "standards": re.compile(r"(挂牌出让|中标公告|采购公告|任免职务)"),
    "benchmark": re.compile(r"(挂牌出让|中标公告|采购公告|任免职务|采购项目)"),
}


@dataclass
class SearchHit:
    title: str
    url: str
    date: str = ""
    site_name: str = ""
    snippet: str = ""
    doc_no: str = ""
    cited_doc_nos: List[str] = field(default_factory=list)
    agency: str = ""
    category: str = ""
    provider: str = ""
    source_tier: str = ""
    tier_rank: int = 5
    tier_label: str = ""
    is_policy_channel: bool = False
    is_index_page: bool = False
    needs_primary_verify: bool = True
    score: Optional[float] = None
    alternate_urls: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for opt_key in ("extra", "cited_doc_nos", "alternate_urls"):
            if not d.get(opt_key):
                d.pop(opt_key, None)
        return d


def _clean_html_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = html_lib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_all_doc_nos(text: str) -> List[str]:
    seen: List[str] = []
    for m in DOC_NO_RE.finditer(text or ""):
        val = re.sub(r"\s+", "", m.group(0))
        val = DOC_NO_PREFIX_TRIM_RE.sub("", val)
        if val and val not in seen:
            seen.append(val)
    return seen


def _detect_index_page(url: str, title: str) -> bool:
    path = (urlparse(url).path or "/").strip()
    if INDEX_PATH_RE.search(path):
        return True
    if INDEX_TITLE_RE.match((title or "").strip()):
        return True
    return False


def _enrich_hit(hit: SearchHit) -> SearchHit:
    info = classify_source(hit.url)
    hit.source_tier = info["source_tier"]
    hit.tier_rank = info["tier_rank"]
    hit.tier_label = info["tier_label"]
    hit.is_policy_channel = info["is_policy_channel"]
    hit.needs_primary_verify = info["needs_primary_verify"]
    hit.is_index_page = _detect_index_page(hit.url, hit.title)

    # 仅将标题或官方库结构化字段里的文号记作本条文号；摘要中提及的文号单列为 cited_doc_nos
    if not hit.doc_no:
        title_nos = _extract_all_doc_nos(hit.title)
        if title_nos:
            hit.doc_no = title_nos[0]
    snippet_nos = [n for n in _extract_all_doc_nos(hit.snippet) if n != hit.doc_no]
    if snippet_nos:
        hit.cited_doc_nos = snippet_nos[:4]
    return hit


def _host_matches_any(url: str, domains: List[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    for d in domains:
        d_l = d.lower().lstrip(".")
        if host == d_l or host.endswith("." + d_l):
            return True
    return False


def _baidu_match_sites(domains: List[str]) -> Tuple[List[str], bool]:
    """为百度千帆 search_filter.match.site 整理域名表。

    - 跳过裸顶级后缀 "gov.cn"（百度传入裸后缀会返回 0 条），改走本地 .gov.cn 过滤；
    - 保留显式局站域名，同时为 www.<city>.gov.cn 补齐无 www. 的根域以覆盖同市子站。
    """
    if not domains:
        return [], False
    real_sites: List[str] = []
    has_bare_gov = False
    for d in domains:
        d_c = d.strip().lower().lstrip(".")
        if not d_c:
            continue
        if d_c == "gov.cn":
            has_bare_gov = True
            continue
        if d_c not in real_sites:
            real_sites.append(d_c)
        if d_c.startswith("www.") and d_c.endswith(".gov.cn") and d_c.count(".") >= 3:
            root = d_c[4:]
            if root not in real_sites:
                real_sites.append(root)

    if has_bare_gov and not real_sites:
        return [], True
    return real_sites[:20], has_bare_gov


def _map_since_baidu(since: str) -> Optional[str]:
    m = {"day": "week", "week": "week", "month": "month", "halfyear": "semiyear", "year": "year"}
    return m.get((since or "").lower())


def _map_since_bocha(since: str) -> str:
    m = {
        "day": "oneDay",
        "week": "oneWeek",
        "month": "oneMonth",
        "halfyear": "oneYear",
        "year": "oneYear",
    }
    return m.get((since or "").lower(), "noLimit")


def detect_available_providers() -> Dict[str, bool]:
    return {
        "baidu": bool(os.environ.get("BAIDU_QIANFAN_API_KEY") or os.environ.get("QIANFAN_API_KEY")),
        "bocha": bool(os.environ.get("BOCHA_API_KEY")),
        "iqs": bool(os.environ.get("ALIYUN_IQS_API_KEY")),
        "gd": True,
    }


def build_provider_payload(
    provider: str,
    plan: RoutePlan,
    *,
    count: int = 15,
    since: str = "",
    gd_sites: Optional[List[str]] = None,
    gd_pos: str = "all",
    page: int = 1,
) -> Dict[str, Any]:
    fetch_k = min(max(count * 3, 20), 50)

    if provider == "baidu":
        baidu_sites, local_gov_filter = _baidu_match_sites(plan.site_domains)
        q_text = plan.effective_query
        if local_gov_filter and not re.search(r"(政府|规划|自然资源|住建|政策|规定|办法|公报)", q_text):
            q_text = f"{q_text} 人民政府"
        body: Dict[str, Any] = {
            "messages": [{"role": "user", "content": q_text}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": fetch_k}],
        }
        if baidu_sites:
            body["search_filter"] = {"match": {"site": baidu_sites}}
        rec = _map_since_baidu(since)
        if rec:
            body["search_recency_filter"] = rec
        return {
            "provider": "baidu",
            "method": "POST",
            "url": "https://qianfan.baidubce.com/v2/ai_search/web_search",
            "body": body,
            "local_gov_filter": local_gov_filter,
        }

    if provider == "bocha":
        body = {
            "query": plan.effective_query,
            "freshness": _map_since_bocha(since),
            "summary": True,
            "count": fetch_k,
        }
        if plan.site_domains:
            body["include"] = "|".join(plan.site_domains[:100])
        if plan.exclude_domains:
            body["exclude"] = "|".join(plan.exclude_domains[:50])
        return {
            "provider": "bocha",
            "method": "POST",
            "url": "https://api.bochaai.com/v1/web-search",
            "body": body,
            "local_gov_filter": False,
        }

    if provider == "iqs":
        body = {
            "query": plan.effective_query,
            "engineType": "Generic",
            "contents": {"mainText": False, "markdownText": False, "summary": True},
        }
        if plan.site_domains:
            body["includeSites"] = ",".join(plan.site_domains[:100])
        if plan.exclude_domains:
            body["excludeSites"] = ",".join(plan.exclude_domains[:100])
        return {
            "provider": "iqs",
            "method": "POST",
            "url": "https://cloud-iqs.aliyuncs.com/search/unified",
            "body": body,
            "local_gov_filter": False,
        }

    if provider == "gd":
        sites = gd_sites or plan.gd_site_ids
        if not sites:
            raise ValueError("广东统一搜索需要提供已验证的 --gd-site 站点 ID（严禁枚举猜测）。")
        if len(sites) > 10:
            raise ValueError("为防触发 403 限流，单次广东统一搜索最多允许 10 个已验证站点 ID，禁止扫段。")
        qs = urllib.parse.urlencode(
            {
                "text": plan.normalized_query,
                "callback": "cb",
                "position": gd_pos,
                "page": str(page),
                "pagesize": str(min(max(count, 1), 30)),
                "including_link_doc": "1",
                "including_url_doc": "1",
                "sites": ",".join(sites),
            }
        )
        return {
            "provider": "gd",
            "method": "GET",
            "url": f"https://search.gd.gov.cn/jsonp/sites?{qs}",
            "referer": "https://www.gd.gov.cn/",
            "local_gov_filter": False,
        }

    raise ValueError(f"不支持的搜索提供方: {provider}")


def parse_baidu_response(raw_json: Dict[str, Any], exclude_domains: Optional[List[str]] = None) -> List[SearchHit]:
    refs = raw_json.get("references") or (raw_json.get("data") or {}).get("references") or []
    hits: List[SearchHit] = []
    for item in refs:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        if not url:
            continue
        if exclude_domains and _host_matches_any(url, exclude_domains):
            continue
        date_str = str(item.get("date") or "")[:10]
        snippet = _clean_html_text(item.get("snippet") or item.get("content") or "")[:420]
        h = SearchHit(
            title=_clean_html_text(item.get("title") or item.get("web_anchor") or ""),
            url=url,
            date=date_str,
            site_name=str(item.get("website") or ""),
            snippet=snippet,
            provider="baidu",
            score=item.get("rerank_score"),
            extra={k: item[k] for k in ("authority_score",) if item.get(k) is not None},
        )
        hits.append(_enrich_hit(h))
    return hits


def parse_bocha_response(raw_json: Dict[str, Any], exclude_domains: Optional[List[str]] = None) -> List[SearchHit]:
    data = raw_json.get("data") or raw_json
    pages = (data.get("webPages") or {}).get("value") or []
    hits: List[SearchHit] = []
    for item in pages:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        if not url:
            continue
        if exclude_domains and _host_matches_any(url, exclude_domains):
            continue
        date_raw = str(item.get("datePublished") or item.get("dateLastCrawled") or "")
        date_str = date_raw[:10] if len(date_raw) >= 10 else date_raw
        snippet = _clean_html_text(item.get("summary") or item.get("snippet") or "")[:450]
        h = SearchHit(
            title=_clean_html_text(item.get("name") or ""),
            url=url,
            date=date_str,
            site_name=str(item.get("siteName") or ""),
            snippet=snippet,
            provider="bocha",
        )
        hits.append(_enrich_hit(h))
    return hits


def parse_iqs_response(raw_json: Dict[str, Any], exclude_domains: Optional[List[str]] = None) -> List[SearchHit]:
    items = raw_json.get("pageItems") or (raw_json.get("data") or {}).get("pageItems") or []
    hits: List[SearchHit] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = (item.get("link") or item.get("url") or "").strip()
        if not url:
            continue
        if exclude_domains and _host_matches_any(url, exclude_domains):
            continue
        date_str = str(item.get("publishedTime") or "")[:10]
        snippet = _clean_html_text(item.get("summary") or item.get("snippet") or "")[:450]
        h = SearchHit(
            title=_clean_html_text(item.get("title") or ""),
            url=url,
            date=date_str,
            site_name=str(item.get("hostname") or item.get("siteName") or ""),
            snippet=snippet,
            provider="iqs",
            score=item.get("rerankScore"),
        )
        hits.append(_enrich_hit(h))
    return hits


def parse_gd_jsonp(raw_text: str) -> Tuple[List[SearchHit], Optional[int]]:
    m = re.match(r"^\s*cb\((.*)\)\s*;?\s*$", raw_text, re.S)
    if not m:
        raise ValueError(f"广东统一搜索返回非 JSONP 格式：{raw_text[:200]}")
    d = json.loads(m.group(1))
    results = d.get("results") or []
    hits: List[SearchHit] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        url = (r.get("post_url") or r.get("url") or "").replace(".cn//", ".cn/").strip()
        if not url:
            continue
        h = SearchHit(
            title=_clean_html_text(r.get("title") or ""),
            url=url,
            date=str(r.get("pub_time") or "")[:10],
            site_name=str(r.get("site_name") or ""),
            snippet=_clean_html_text(r.get("content") or r.get("desc") or "")[:300],
            provider="gd",
        )
        hits.append(_enrich_hit(h))
    return hits, d.get("count")


def _root_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("gov", "org", "edu", "com", "net") and parts[-1] == "cn":
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _canonical_url_key(url: str) -> str:
    p = urlparse(url.split("#")[0].rstrip("/"))
    host = (p.hostname or "").lower()
    path = p.path
    m_post = re.search(r"/m?(post_\d+\.html?)$", path, re.I)
    if m_post:
        return f"{_root_domain(url)}::{m_post.group(1).lower()}"
    path = re.sub(r"/mobile/", "/", path, flags=re.I)
    return f"{host}{path}"


def _norm_title_key(title: str) -> str:
    t = re.sub(r"[\s\-_—|·•《》「」【】\[\]()（）:：,，.。!！?？]+", "", (title or "").lower())
    return t[:48]


def _url_quality_rank(h: SearchHit) -> Tuple[int, int, int, int]:
    u = h.url.lower()
    not_mobile = 0 if ("mpost_" in u or "/mobile/" in u) else 1
    is_https = 1 if u.startswith("https://") else 0
    is_policy = 1 if h.is_policy_channel else 0
    not_index = 0 if h.is_index_page else 1
    return (not_index, not_mobile, is_policy, is_https)


def deduplicate_hits(hits: List[SearchHit]) -> List[SearchHit]:
    """折叠同文不同链接（http/https、post_/mpost_、同根域同标题跨栏目转发）。"""
    by_url_key: Dict[str, SearchHit] = {}
    for h in hits:
        uk = _canonical_url_key(h.url)
        if uk not in by_url_key:
            by_url_key[uk] = h
        else:
            prev = by_url_key[uk]
            if _url_quality_rank(h) > _url_quality_rank(prev):
                h.alternate_urls.append(prev.url)
                h.alternate_urls.extend(prev.alternate_urls)
                by_url_key[uk] = h
            elif h.url != prev.url and h.url not in prev.alternate_urls:
                prev.alternate_urls.append(h.url)

    by_title_key: Dict[str, SearchHit] = {}
    ordered: List[SearchHit] = []
    for h in by_url_key.values():
        nt = _norm_title_key(h.title)
        if len(nt) < 8 or h.is_index_page:
            ordered.append(h)
            continue
        tk = f"{_root_domain(h.url)}::{nt}"
        if tk not in by_title_key:
            by_title_key[tk] = h
            ordered.append(h)
        else:
            prev = by_title_key[tk]
            if _url_quality_rank(h) > _url_quality_rank(prev):
                h.alternate_urls.append(prev.url)
                h.alternate_urls.extend(prev.alternate_urls)
                idx = ordered.index(prev)
                ordered[idx] = h
                by_title_key[tk] = h
            elif h.url != prev.url and h.url not in prev.alternate_urls:
                prev.alternate_urls.append(h.url)
    return ordered


def _rank_key(h: SearchHit, track: str, query_tokens: List[str]) -> Tuple[int, float, int, int]:
    rel = 0.0
    title_l = h.title.lower()
    snip_l = h.snippet.lower()

    if h.doc_no and any(h.doc_no in tok or tok in h.doc_no for tok in query_tokens):
        rel += 3.0

    for tok in query_tokens:
        if len(tok) < 2:
            continue
        if tok.lower() in title_l:
            rel += 1.2
        elif tok.lower() in snip_l:
            rel += 0.3

    boost_re = TRACK_BOOST_RE.get(track)
    demote_re = TRACK_DEMOTE_RE.get(track)
    if boost_re and boost_re.search(h.title):
        rel += 1.0
    if demote_re and demote_re.search(h.title):
        rel -= 2.2
    if h.is_policy_channel:
        rel += 0.6
    if h.is_index_page:
        rel -= 0.4
    if h.score is not None:
        rel += float(h.score) * 0.5

    date_int = int(re.sub(r"\D", "", h.date) or "0")
    return (h.tier_rank, -round(rel, 3), 1 if h.is_index_page else 0, -date_int)


def _execute_single_provider(
    client: GovHttpClient,
    chosen: str,
    plan: RoutePlan,
    *,
    count: int,
    since: str,
    gd_sites: Optional[List[str]],
    gd_pos: str,
    page: int,
) -> Tuple[bool, List[SearchHit], Optional[Dict[str, Any]], bool]:
    payload = build_provider_payload(
        chosen, plan, count=count, since=since, gd_sites=gd_sites, gd_pos=gd_pos, page=page
    )
    local_gov_filter = bool(payload.get("local_gov_filter"))

    if chosen == "baidu":
        key = os.environ.get("BAIDU_QIANFAN_API_KEY") or os.environ.get("QIANFAN_API_KEY")
        if not key:
            return False, [], {"ok": False, "error_code": "MISSING_API_KEY", "message": "缺少环境变量 BAIDU_QIANFAN_API_KEY"}, False
        resp = client.request(
            payload["url"],
            method="POST",
            headers={"Authorization": f"Bearer {key}"},
            json_body=json.dumps(payload["body"], ensure_ascii=False).encode("utf-8"),
            timeout=30,
        )
        if not resp.ok:
            return False, [], {"ok": False, "error_code": resp.code_flag, "status": resp.status, "message": resp.error, "raw": resp.text()[:300]}, False
        hits = parse_baidu_response(json.loads(resp.text()), exclude_domains=plan.exclude_domains)
        return True, hits, None, local_gov_filter

    if chosen == "bocha":
        key = os.environ.get("BOCHA_API_KEY")
        if not key:
            return False, [], {"ok": False, "error_code": "MISSING_API_KEY", "message": "缺少环境变量 BOCHA_API_KEY"}, False
        resp = client.request(
            payload["url"],
            method="POST",
            headers={"Authorization": f"Bearer {key}"},
            json_body=json.dumps(payload["body"], ensure_ascii=False).encode("utf-8"),
            timeout=30,
        )
        if not resp.ok:
            return False, [], {"ok": False, "error_code": resp.code_flag, "status": resp.status, "message": resp.error, "raw": resp.text()[:300]}, False
        hits = parse_bocha_response(json.loads(resp.text()), exclude_domains=plan.exclude_domains)
        return True, hits, None, False

    if chosen == "iqs":
        key = os.environ.get("ALIYUN_IQS_API_KEY")
        if not key:
            return False, [], {"ok": False, "error_code": "MISSING_API_KEY", "message": "缺少环境变量 ALIYUN_IQS_API_KEY"}, False
        resp = client.request(
            payload["url"],
            method="POST",
            headers={"Authorization": f"Bearer {key}"},
            json_body=json.dumps(payload["body"], ensure_ascii=False).encode("utf-8"),
            timeout=30,
        )
        if not resp.ok:
            return False, [], {"ok": False, "error_code": resp.code_flag, "status": resp.status, "message": resp.error, "raw": resp.text()[:300]}, False
        hits = parse_iqs_response(json.loads(resp.text()), exclude_domains=plan.exclude_domains)
        return True, hits, None, False

    if chosen == "gd":
        resp = client.request(
            payload["url"],
            method="GET",
            referer=payload.get("referer"),
            timeout=20,
            min_delay=1.3,
        )
        if not resp.ok:
            return False, [], {
                "ok": False,
                "error_code": resp.code_flag,
                "status": resp.status,
                "message": f"广东统一搜索失败（{resp.error}）。若返回 403，请停搜并改用全国搜索 API 或栏目翻页。",
            }, False
        hits, _ = parse_gd_jsonp(resp.text())
        return True, hits, None, False

    return False, [], {"ok": False, "error_code": "UNKNOWN_PROVIDER", "message": f"未知 provider: {chosen}"}, False


def run_search(
    client: GovHttpClient,
    plan: RoutePlan,
    *,
    provider: str = "auto",
    count: int = 15,
    since: str = "",
    gd_sites: Optional[List[str]] = None,
    gd_pos: str = "all",
    page: int = 1,
    dry_run: bool = False,
) -> Dict[str, Any]:
    avail = detect_available_providers()
    chosen = provider

    if chosen == "multi":
        active_list = [p for p in ("baidu", "bocha", "iqs") if avail.get(p)]
        if not active_list:
            chosen = "auto"
        elif dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "available_providers": avail,
                "route": asdict(plan),
                "request": build_provider_payload(active_list[0], plan, count=count, since=since),
                "multi_providers": active_list,
                "suggested_host_queries": _build_fallback_queries(plan),
            }
        else:
            merged: List[SearchHit] = []
            warnings: List[str] = []
            any_local_gov = False
            for p in active_list:
                ok, p_hits, err, lgf = _execute_single_provider(
                    client, p, plan, count=count, since=since, gd_sites=gd_sites, gd_pos=gd_pos, page=page
                )
                if ok:
                    merged.extend(p_hits)
                    any_local_gov = any_local_gov or lgf
                elif err:
                    warnings.append(f"{p} 失败: {err.get('message', '')}")
            return _finalize_hits(merged, "+".join(active_list), plan, count, any_local_gov, warnings)

    if chosen == "auto":
        if gd_sites:
            chosen = "gd"
        elif avail["baidu"]:
            chosen = "baidu"
        elif avail["bocha"]:
            chosen = "bocha"
        elif avail["iqs"]:
            chosen = "iqs"
        elif plan.gd_site_ids and not dry_run:
            chosen = "gd"
        elif dry_run:
            chosen = "baidu"
        else:
            return {
                "ok": False,
                "error_code": "NO_SEARCH_API_KEY",
                "message": (
                    "未检测到全国搜索 API Key（BAIDU_QIANFAN_API_KEY / BOCHA_API_KEY / ALIYUN_IQS_API_KEY）。"
                    "可在环境变量或 ~/.config/govdoc/env 中配置；"
                    "若查国家政策、行标/地标或已知栏目，可直接使用零 Key 子命令：central、std、list。"
                ),
                "route": asdict(plan),
                "suggested_host_queries": _build_fallback_queries(plan),
            }

    if dry_run:
        payload = build_provider_payload(
            chosen, plan, count=count, since=since, gd_sites=gd_sites, gd_pos=gd_pos, page=page
        )
        return {
            "ok": True,
            "dry_run": True,
            "available_providers": avail,
            "route": asdict(plan),
            "request": payload,
            "suggested_host_queries": _build_fallback_queries(plan),
        }

    ok, hits, err, local_gov_filter = _execute_single_provider(
        client, chosen, plan, count=count, since=since, gd_sites=gd_sites, gd_pos=gd_pos, page=page
    )
    if not ok:
        return err or {"ok": False, "error_code": "SEARCH_FAILED", "message": "搜索失败"}
    return _finalize_hits(hits, chosen, plan, count, local_gov_filter, [])


def _finalize_hits(
    hits: List[SearchHit],
    provider_label: str,
    plan: RoutePlan,
    count: int,
    local_gov_filter: bool,
    warnings: List[str],
) -> Dict[str, Any]:
    raw_count = len(hits)
    if local_gov_filter:
        gov_only = [h for h in hits if h.tier_rank <= 3]
        if gov_only:
            hits = gov_only
        elif hits:
            warnings.append(
                "全国泛化模式下前批结果未命中 .gov.cn 官网，已保留非官站线索（均标为 T4/T5 待核原文）；"
                "建议补充准确县市全称或配合 list 子命令 / 浏览器核查。"
            )

    deduped = deduplicate_hits(hits)
    collapsed = len(hits) - len(deduped)
    query_tokens = [t for t in re.split(r"[\s,，、]+", plan.effective_query) if t]
    deduped.sort(key=lambda h: _rank_key(h, plan.track, query_tokens))

    final_hits = [h.to_dict() for h in deduped[:count]]
    out: Dict[str, Any] = {
        "ok": True,
        "provider": provider_label,
        "raw_count": raw_count,
        "collapsed_duplicates": collapsed,
        "count": len(final_hits),
        "route": asdict(plan),
        "hits": final_hits,
    }
    if warnings:
        out["warnings"] = warnings
    return out


def _build_fallback_queries(plan: RoutePlan) -> List[str]:
    out = []
    if plan.site_domains:
        for d in plan.site_domains[:3]:
            out.append(f"site:{d} {plan.effective_query}")
    out.append(f"site:gov.cn {plan.effective_query}")
    return out
