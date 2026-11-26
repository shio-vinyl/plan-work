"""govdoc 统一命令行入口：search / central / std / list / fetch / extract / sites / probe。"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

from .archiver import archive_parsed_page
from .http_client import CircuitOpenError, GovHttpClient
from .official_dbs import list_column, probe_site, search_central_gov, search_standards
from .parser import ALL_ATTACH_RE, extract_local_file, parse_gov_html
from .providers import detect_available_providers, run_search
from .registry import build_route_plan, classify_source, load_registry, save_user_region


def _load_search_env() -> None:
    """优先使用进程环境变量，缺省时从 ~/.config/govdoc/env 读取本机搜索凭证。"""
    provider_vars = (
        ("BAIDU_QIANFAN_API_KEY", "QIANFAN_API_KEY"),
        ("BOCHA_API_KEY",),
        ("ALIYUN_IQS_API_KEY",),
    )
    missing = {name for group in provider_vars if not any(name in os.environ for name in group) for name in group}
    if not missing:
        return

    path = Path.home() / ".config" / "govdoc" / "env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"无法读取搜索凭证文件 {path}") from exc

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if sep and name.strip() in missing and value.strip():
            os.environ.setdefault(name.strip(), value.strip())


def _print_output(data: Dict[str, Any], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    if not data.get("ok", True) and not data.get("dry_run"):
        print(f"[ERROR] {data.get('error_code', 'FAILED')}: {data.get('message', '')}")
        if data.get("suggested_host_queries"):
            print("建议降级检索词：")
            for q in data["suggested_host_queries"]:
                print(f"  - {q}")
        return

    if data.get("dry_run"):
        r = data["route"]
        print("# 规划路由预览（dry-run）")
        print(f"- 轨道：{r['track']}（{r['track_label']}）｜路由模式：{r['route_mode']}｜匹配地区：{r['matched_region'] or '全国/未入库'}")
        print(f"- 规整检索词：{r['effective_query']}")
        print(f"- 限定域名（{len(r['site_domains'])} 个）：{', '.join(r['site_domains']) or '（不限）'}")
        if r.get("gd_site_ids"):
            print(f"- 已知广东站点 ID：{', '.join(r['gd_site_ids'])}")
        if r.get("notes"):
            for n in r["notes"]:
                print(f"- 提示：{n}")
        print(f"- 请求预览：{data['request']['method']} {data['request']['url']}")
        if data["request"].get("body"):
            print(json.dumps(data["request"]["body"], ensure_ascii=False, indent=2))
        return

    if "hits" in data:
        hits = data["hits"]
        if fmt == "tsv":
            print("tier\tdate\tdoc_no\tagency\ttitle\turl")
            for h in hits:
                print(f"{h.get('source_tier','')}\t{h.get('date','')}\t{h.get('doc_no','')}\t{h.get('agency','')}\t{h.get('title','')}\t{h.get('url','')}")
            return
        prov = data.get("provider") or data.get("mode") or ""
        print(f"# 检索结果（来源：{prov}，共 {len(hits)} 条）")
        if data.get("bucket_counts"):
            print(f"- 分库命中：{data['bucket_counts']}")
        if data.get("note"):
            print(f"- 说明：{data['note']}")
        for w in data.get("warnings") or []:
            print(f"- [WARN] {w}")
        print()
        for i, h in enumerate(hits, 1):
            tier = h.get("source_tier", "")
            dt = h.get("date") or "日期未知"
            doc = f" [{h['doc_no']}]" if h.get("doc_no") else ""
            cat = f" ({h['category']})" if h.get("category") else ""
            idx_badge = " 📂栏目入口页(日期为栏目更新日，可用 list 子命令展开)" if h.get("is_index_page") else ""
            verify = " ⚠️转述待核" if h.get("needs_primary_verify") else ""
            print(f"{i}. [{tier}] {dt}{doc}{cat} | {h.get('title','')}{idx_badge}{verify}")
            print(f"   URL: {h.get('url','')}")
            if h.get("agency") or h.get("site_name"):
                print(f"   来源: {h.get('agency') or h.get('site_name')}")
            if h.get("cited_doc_nos"):
                print(f"   摘要引文文号（非本条文号）: {', '.join(h['cited_doc_nos'])}")
            if h.get("alternate_urls"):
                print(f"   已折叠同文重复链接: {len(h['alternate_urls'])} 条")
            if h.get("snippet"):
                print(f"   摘要: {h['snippet'][:220]}")
        return

    if "records" in data:
        recs = data["records"]
        if fmt == "tsv":
            print("kind\tcode\tstatus\tissue_date\teffective_date\tagency\ttitle\turl")
            for r in recs:
                print(f"{r['kind']}\t{r['code']}\t{r['status']}\t{r['issue_date']}\t{r['effective_date']}\t{r['agency']}\t{r['title']}\t{r['url']}")
            return
        print(f"# 标准查询结果（共 {len(recs)} 条）")
        if data.get("note"):
            print(f"- 说明：{data['note']}")
        for e in data.get("errors") or []:
            print(f"- [WARN] {e}")
        print()
        for i, r in enumerate(recs, 1):
            print(f"{i}. [{r['kind']}] {r['code']}｜{r['title']}｜状态：{r['status']}")
            print(f"   主管/地区：{r['agency']} / {r['industry_or_area']}｜发布：{r['issue_date']}｜实施：{r['effective_date']}")
            print(f"   URL: {r['url']}")
        return

    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_search(args: argparse.Namespace) -> int:
    _load_search_env()
    client = GovHttpClient(default_delay=args.delay)
    extra_sites = [s.strip() for s in (args.site or "").split(",") if s.strip()]
    plan = build_route_plan(
        args.query,
        track=args.track,
        region=args.region or "",
        scope=args.scope,
        extra_sites=extra_sites,
    )
    gd_sites = [s.strip() for s in (args.gd_site or "").split(",") if s.strip()] or None
    res = run_search(
        client,
        plan,
        provider=args.provider,
        count=args.count,
        since=args.since or "",
        gd_sites=gd_sites,
        gd_pos=args.gd_pos,
        page=args.page,
        dry_run=args.dry_run,
    )
    _print_output(res, args.format)
    return 0 if res.get("ok") else 1


def cmd_central(args: argparse.Namespace) -> int:
    client = GovHttpClient(default_delay=args.delay)
    types = [t.strip() for t in args.type.split(",") if t.strip()] if args.type else None
    res = search_central_gov(
        client,
        args.query,
        types=types,
        search_field=args.field,
        year=args.year or "",
        sort=args.sort,
        page=args.page,
        size=args.size,
    )
    _print_output(res, args.format)
    return 0 if res.get("ok") else 1


def cmd_std(args: argparse.Namespace) -> int:
    client = GovHttpClient(default_delay=args.delay)
    kinds = [k.strip() for k in args.kind.split(",") if k.strip()] if args.kind else ["hb", "db"]
    res = search_standards(
        client,
        args.query,
        kinds=kinds,
        area=args.area or "",
        status=args.status or "",
        page=args.page,
        size=args.size,
    )
    _print_output(res, args.format)
    return 0 if res.get("ok") else 1


def cmd_list(args: argparse.Namespace) -> int:
    client = GovHttpClient(default_delay=args.delay)
    res = list_column(client, args.url, pages=args.pages, match=args.match or "")
    _print_output(res, args.format)
    return 0 if res.get("ok") else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    client = GovHttpClient(default_delay=args.delay)
    url = args.url.strip()

    if ALL_ATTACH_RE.search(url):
        out_dir = args.archive or args.out or "downloads"
        os.makedirs(out_dir, exist_ok=True)
        raw_name = unquote(os.path.basename(urlparse(url).path)) or "file"
        safe_name = re.sub(r'[\\/:*?"<>|]+', "_", raw_name)
        dest = os.path.join(out_dir, safe_name)
        ok, flag, info = client.download_file(url, dest, referer=args.referer, timeout=args.timeout)
        res: Dict[str, Any] = {"ok": ok, "code_flag": flag, "download": info}
        if ok and args.extract:
            res["extracted"] = extract_local_file(dest)
        if args.format == "json":
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            if ok:
                print(f"已下载并校验通过：{dest}（{info.get('size', 0)} 字节）")
                if info.get("warning"):
                    print(f"[WARN] {info['warning']}")
                if res.get("extracted"):
                    ex = res["extracted"]
                    if ex.get("ok"):
                        for w in ex.get("warnings") or []:
                            print(f"[WARN] {w}")
                        print((ex.get("text") or "")[: args.max])
                    else:
                        print(f"[WARN] 抽文本失败：{ex.get('error')}")
            else:
                print(f"[ERROR] {flag}: {info.get('error', '')}")
        return 0 if ok else 1

    resp = client.request(url, referer=args.referer, timeout=args.timeout)
    if not resp.ok:
        out = {
            "ok": False,
            "error_code": resp.code_flag,
            "status": resp.status,
            "url": resp.effective_url,
            "message": resp.error,
        }
        _print_output(out, args.format)
        return 1

    html_text = resp.text()
    parsed = parse_gov_html(resp.effective_url, html_text, include_content_images=args.with_images)
    parsed["encoding"] = resp.encoding
    if resp.warnings:
        parsed["warnings"].extend(resp.warnings)
    source_info = classify_source(resp.effective_url)
    parsed["source_info"] = source_info

    archive_res = None
    if args.archive:
        archive_res = archive_parsed_page(
            client,
            resp.body,
            parsed,
            args.archive,
            download_attachments=not args.no_attachments,
            download_images=args.with_images,
        )
        parsed["archived"] = archive_res

    if args.format == "json":
        out_j = dict(parsed)
        if not args.full_text:
            out_j["text"] = out_j["text"][: args.max]
        print(json.dumps({"ok": True, **out_j}, ensure_ascii=False, indent=2))
        return 0

    print(f"# {parsed['title']}")
    print(f"- URL: {parsed['url']}")
    print(f"- 信源等级: {source_info['source_tier']}（{source_info['tier_label']}）｜编码: {parsed['encoding']}")
    print(
        f"- 文号: {parsed['doc_no'] or '-'}｜机关: {parsed['agency'] or '-'}｜"
        f"成文: {parsed['draft_date'] or '-'}｜发布: {parsed['pub_date'] or '-'}"
    )
    if parsed["site_id_code"] or parsed["column_name"]:
        print(f"- 站群标识: {parsed['site_name']} ({parsed['site_id_code']})｜栏目: {parsed['column_name']}")
    val = parsed["validity"]
    if val.get("explicit_status") or val.get("clauses"):
        print(f"- 效力信息: {val.get('explicit_status') or '未明标'}｜{' ; '.join(val.get('clauses') or [])}")
    for w in parsed["warnings"]:
        print(f"- [WARN] {w}")
    if parsed["attachments"]:
        print(f"- 附件（{len(parsed['attachments'])} 个）：")
        for a in parsed["attachments"][:30]:
            print(f"    [{a['kind']}] {a['label']} | {a['url']}")
    if archive_res:
        print(f"- 已归档：{archive_res['txt_path']}（原网页：{archive_res['html_path']}，索引：{archive_res['manifest_path']}）")
    print()

    body = parsed["text"]
    if args.grep:
        pat = re.compile(args.grep, re.I)
        for line in body.splitlines():
            if pat.search(line):
                print("…", line[:400])
    else:
        print(body[: args.max])
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    res = extract_local_file(args.path, out_dir=args.out)
    if args.format == "json":
        out_j = dict(res)
        if "text" in out_j and not args.full:
            out_j["text"] = out_j["text"][: args.max]
        print(json.dumps(out_j, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if not res.get("ok"):
        print(f"[ERROR] {res.get('error')}")
        return 1
    if "files" in res:
        print(f"已解压（{res['format']}），共 {len(res['files'])} 个文件：")
        for f in res["files"]:
            print(" ", f)
        return 0
    for w in res.get("warnings") or []:
        print(f"[WARN] {w}")
    txt = res.get("text", "")
    print(txt if args.full else txt[: args.max])
    return 0


def cmd_sites(args: argparse.Namespace) -> int:
    reg = load_registry()
    if args.sites_action == "tracks":
        tracks = reg.get("tracks") or {}
        if args.format == "json":
            print(json.dumps(tracks, ensure_ascii=False, indent=2))
        else:
            for k, v in tracks.items():
                print(f"- {k:16s} | {v['label']}：{v['description']}")
        return 0

    if args.sites_action == "list":
        regions = reg.get("regions") or {}
        q = (args.filter or "").strip()
        matched = {
            k: v for k, v in regions.items()
            if not q or q in k or any(q in a for a in v.get("aliases") or [])
        }
        if args.format == "json":
            print(json.dumps(matched, ensure_ascii=False, indent=2))
        else:
            print(f"# 规划站点注册表（共 {len(matched)} 个地区）")
            for k, v in matched.items():
                doms = [d for dl in (v.get("domains") or {}).values() for d in dl]
                gd = f" gd_ids={v['gd_site_ids']}" if v.get("gd_site_ids") else ""
                print(f"- {k:10s} ({v.get('level','')}) | {', '.join(doms)}{gd}")
        return 0

    if args.sites_action == "add":
        domains: Dict[str, List[str]] = {}
        for dept in ("gov", "pnr", "housing", "fgw", "stats", "ggzy"):
            val = getattr(args, dept, "")
            if val:
                domains[dept] = [x.strip() for x in val.split(",") if x.strip()]
        if not domains:
            print("[ERROR] 至少需要提供一个域名参数（--gov / --pnr / --housing / --fgw / --stats / --ggzy）")
            return 1
        aliases = [x.strip() for x in (args.aliases or "").split(",") if x.strip()] or None
        gd_ids = [x.strip() for x in (args.gd_ids or "").split(",") if x.strip()] or None
        saved = save_user_region(
            args.name,
            domains,
            aliases=aliases,
            parent=args.parent or None,
            level=args.level,
            gd_site_ids=gd_ids,
            notes=args.notes or "",
        )
        print(f"已将地区“{args.name}”写入本机站点表：{saved}")
        return 0

    if args.sites_action == "status":
        _load_search_env()
        avail = detect_available_providers()
        info = {"providers": avail, "regions_count": len(reg.get("regions") or {}), "tracks_count": len(reg.get("tracks") or {})}
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    return 1


def cmd_probe(args: argparse.Namespace) -> int:
    client = GovHttpClient(default_delay=args.delay)
    res = probe_site(client, args.url)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="govdoc",
        description="规划特化全国政务/政策/公文/公告搜索、直查、校验抓取与归档工具",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("search", help="全国规划特化搜索（百度千帆/博查/阿里云IQS/广东统一搜索）")
    ps.add_argument("query", help="检索词（支持文号、政策名、项目名）")
    ps.add_argument(
        "--track",
        default="upper-policy",
        choices=["upper-policy", "spatial-plan", "housing-renewal", "land-supply", "stats-census", "standards", "benchmark"],
        help="规划业务轨道（默认 upper-policy）",
    )
    ps.add_argument("--region", default="", help="地区名，如 北京、上海、广州、某市某区，或多个城市用逗号隔开")
    ps.add_argument("--scope", default="gov", choices=["gov", "all"], help="gov 仅限官方域与去噪；all 放开全网")
    ps.add_argument("--site", default="", help="额外指定的域名（逗号分隔）")
    ps.add_argument("--provider", default="auto", choices=["auto", "baidu", "bocha", "iqs", "gd", "multi"], help="指定搜索提供方（multi 并行合并已配置的全部 API）")
    ps.add_argument("--since", default="", choices=["", "day", "week", "month", "halfyear", "year"], help="时间范围")
    ps.add_argument("--count", type=int, default=15, help="返回条数（默认 15）")
    ps.add_argument("--page", type=int, default=1, help="页码（用于 gd）")
    ps.add_argument("--gd-site", default="", help="广东统一搜索已验证站点 ID（逗号分隔，最多 10 个）")
    ps.add_argument("--gd-pos", default="all", choices=["all", "title"], help="广东搜索位置")
    ps.add_argument("--dry-run", action="store_true", help="仅预览规划轨道路由和请求体，不发网络请求")
    ps.add_argument("--delay", type=float, default=0.5, help="请求间隔秒数")
    ps.add_argument("--format", default="md", choices=["md", "json", "tsv"])
    ps.set_defaults(func=cmd_search)

    pc = sub.add_parser("central", help="零 Key 直查中国政府网政策库（国务院+部委+公报+解读）")
    pc.add_argument("query", help="关键词或文号")
    pc.add_argument("--type", default="gongwen,bumenfile,gongbao,otherfile", help="库类别（逗号分隔）")
    pc.add_argument("--field", default="title", choices=["title", "all"], help="搜标题或全文")
    pc.add_argument("--year", default="", help="按发布年份过滤，如 2025")
    pc.add_argument("--sort", default="pubtime", choices=["pubtime", "score"])
    pc.add_argument("--page", type=int, default=1)
    pc.add_argument("--size", type=int, default=10)
    pc.add_argument("--delay", type=float, default=0.5)
    pc.add_argument("--format", default="md", choices=["md", "json", "tsv"])
    pc.set_defaults(func=cmd_central)

    pstd = sub.add_parser("std", help="零 Key 直查全国行业标准(TD/T、CJJ/JGJ)与地方标准(DB*)及效力状态")
    pstd.add_argument("query", help="标准关键词或标准号前缀")
    pstd.add_argument("--kind", default="hb,db", help="hb（行标）,db（地标）")
    pstd.add_argument("--area", default="", help="地方标准省市名（如 北京、广东、江苏）")
    pstd.add_argument("--status", default="", help="状态筛选（如 现行）")
    pstd.add_argument("--page", type=int, default=1)
    pstd.add_argument("--size", type=int, default=10)
    pstd.add_argument("--delay", type=float, default=0.6)
    pstd.add_argument("--format", default="md", choices=["md", "json", "tsv"])
    pstd.set_defaults(func=cmd_std)

    pl = sub.add_parser("list", help="零 Key 政务栏目翻页器（支持全国静态栏目与广东 gkmlpt 目录 JSON）")
    pl.add_argument("url", help="政府网站栏目页 URL")
    pl.add_argument("--pages", type=int, default=3, help="最多翻页数（默认 3）")
    pl.add_argument("--match", default="", help="按标题/文号过滤的正则")
    pl.add_argument("--delay", type=float, default=0.6)
    pl.add_argument("--format", default="md", choices=["md", "json", "tsv"])
    pl.set_defaults(func=cmd_list)

    pf = sub.add_parser("fetch", help="强校验抓取政府网页/附件、抽取规划元数据并可选一键归档")
    pf.add_argument("url", help="网页或附件 URL")
    pf.add_argument("--archive", default="", help="归档目录（双存原件 HTML + 带核验头 TXT + 附件 + manifest.jsonl）")
    pf.add_argument("--out", default="", help="直下附件时的保存目录")
    pf.add_argument("--with-images", action="store_true", help="一并发现并下载页面规划图/指标图/展板图片附件")
    pf.add_argument("--no-attachments", action="store_true", help="归档时不下载附件，仅登记链接")
    pf.add_argument("--extract", action="store_true", help="直下附件后立即抽取文本")
    pf.add_argument("--grep", default="", help="只打印匹配该正则的正文行")
    pf.add_argument("--max", type=int, default=8000, help="正文打印最大字数")
    pf.add_argument("--full-text", action="store_true", help="JSON 输出时保留全文不截断")
    pf.add_argument("--referer", default="", help="可选 Referer")
    pf.add_argument("--timeout", type=int, default=30)
    pf.add_argument("--delay", type=float, default=0.4)
    pf.add_argument("--format", default="md", choices=["md", "json"])
    pf.set_defaults(func=cmd_fetch)

    pe = sub.add_parser("extract", help="解析本地公文与规划附件（OFD/PDF含扫描页检测/DOCX/DOC/WPS/ET/RAR/7Z/ZIP）")
    pe.add_argument("path", help="本地文件路径")
    pe.add_argument("--out", default="", help="压缩包解压目标目录")
    pe.add_argument("--max", type=int, default=8000)
    pe.add_argument("--full", action="store_true")
    pe.add_argument("--format", default="md", choices=["md", "json"])
    pe.set_defaults(func=cmd_extract)

    psi = sub.add_parser("sites", help="查看或扩展全国规划部门站点注册表与 API Key 状态")
    psi_sub = psi.add_subparsers(dest="sites_action", required=True)
    psi_t = psi_sub.add_parser("tracks", help="列出 7 条规划检索轨道")
    psi_t.add_argument("--format", default="md", choices=["md", "json"])
    psi_l = psi_sub.add_parser("list", help="列出已收录地区和部门域名")
    psi_l.add_argument("--filter", default="", help="按地名过滤")
    psi_l.add_argument("--format", default="md", choices=["md", "json"])
    psi_s = psi_sub.add_parser("status", help="查看已配置的搜索 API Key 与站点表规模")
    psi_a = psi_sub.add_parser("add", help="向 ~/.config/govdoc/sites.json 新增或更新一个地区的官方域名")
    psi_a.add_argument("name", help="地区名，如 某市某区")
    psi_a.add_argument("--gov", default="")
    psi_a.add_argument("--pnr", default="")
    psi_a.add_argument("--housing", default="")
    psi_a.add_argument("--fgw", default="")
    psi_a.add_argument("--stats", default="")
    psi_a.add_argument("--ggzy", default="")
    psi_a.add_argument("--aliases", default="")
    psi_a.add_argument("--parent", default="")
    psi_a.add_argument("--level", default="city")
    psi_a.add_argument("--gd-ids", default="")
    psi_a.add_argument("--notes", default="")
    psi.set_defaults(func=cmd_sites)

    pp = sub.add_parser("probe", help="单次合法探测某政府网站首页的 SiteIDCode、名称与搜索线索（不扫段）")
    pp.add_argument("url", help="官网域名或首页 URL")
    pp.add_argument("--delay", type=float, default=0.5)
    pp.set_defaults(func=cmd_probe)

    return p


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CircuitOpenError as e:
        print(f"[CIRCUIT_OPEN] {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"[INVALID_ARGUMENT] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
