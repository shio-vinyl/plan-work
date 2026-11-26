"""规划特化信源注册表：7 条业务轨道、地区→官站域名路由、5 级信源分级与公文词规整。"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

DEFAULT_REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "planning_sources.json"
)
USER_OVERRIDE_PATH = os.path.expanduser("~/.config/govdoc/sites.json")

CENTRAL_HOSTS_EXACT = {
    "www.gov.cn", "sousuo.www.gov.cn", "www.mnr.gov.cn", "gk.mnr.gov.cn",
    "www.mohurd.gov.cn", "www.ndrc.gov.cn", "www.stats.gov.cn", "data.stats.gov.cn",
    "www.mee.gov.cn", "www.moa.gov.cn", "www.mwr.gov.cn", "flk.npc.gov.cn",
    "www.npc.gov.cn", "xzfg.moj.gov.cn", "www.moj.gov.cn", "www.court.gov.cn",
    "hbba.sacinfo.org.cn", "dbba.sacinfo.org.cn", "std.samr.gov.cn",
    "openstd.samr.gov.cn", "www.mof.gov.cn", "www.mot.gov.cn", "www.miit.gov.cn",
}
CENTRAL_SUFFIXES = (
    ".mnr.gov.cn", ".mohurd.gov.cn", ".ndrc.gov.cn", ".stats.gov.cn",
    ".npc.gov.cn", ".moj.gov.cn", ".mee.gov.cn", ".samr.gov.cn", ".sacinfo.org.cn",
    ".mof.gov.cn", ".mot.gov.cn", ".court.gov.cn",
)
PUBLIC_PLATFORM_HINTS = (
    "ggzy", "landchina.com", "shtdsc.com", "gdzwfw.gov.cn",
    "zjzwfw.gov.cn", "zwfw.",
)
STATE_MEDIA_HOSTS = (
    "news.cn", "xinhuanet.com", "people.com.cn", "chinajsb.cn",
    "ce.cn", "gmw.cn", "cnr.cn", "cctv.com", "qstheory.cn", "12371.cn",
)
GAZETTE_OR_POLICY_PATH_RE = re.compile(
    r"/(zfgb|gongbao|gwk|zcwj|gfxwj|gkmlpt|zhengce|zcfg|xxgk|zwgk)/", re.I
)


@dataclass
class RoutePlan:
    track: str
    track_label: str
    region_input: str
    matched_region: Optional[str]
    route_mode: str  # exact_region | national_fallback | central_only | multi_region
    site_domains: List[str]
    exclude_domains: List[str]
    normalized_query: str
    effective_query: str
    gd_site_ids: List[str] = field(default_factory=list)
    prefer_direct: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def load_registry() -> Dict[str, Any]:
    with open(DEFAULT_REGISTRY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if os.path.exists(USER_OVERRIDE_PATH):
        try:
            with open(USER_OVERRIDE_PATH, "r", encoding="utf-8") as uf:
                override = json.load(uf)
            for k, v in (override.get("regions") or {}).items():
                data.setdefault("regions", {})[k] = v
        except Exception:
            pass
    return data


def save_user_region(
    name: str,
    domains: Dict[str, List[str]],
    *,
    aliases: Optional[List[str]] = None,
    parent: Optional[str] = None,
    level: str = "city",
    gd_site_ids: Optional[List[str]] = None,
    notes: str = "",
) -> str:
    os.makedirs(os.path.dirname(USER_OVERRIDE_PATH), exist_ok=True)
    cur: Dict[str, Any] = {"regions": {}}
    if os.path.exists(USER_OVERRIDE_PATH):
        try:
            with open(USER_OVERRIDE_PATH, "r", encoding="utf-8") as f:
                cur = json.load(f)
        except Exception:
            cur = {"regions": {}}
    entry: Dict[str, Any] = {"level": level, "domains": domains}
    if aliases:
        entry["aliases"] = aliases
    if parent:
        entry["parent"] = parent
    if gd_site_ids:
        entry["gd_site_ids"] = gd_site_ids
    if notes:
        entry["notes"] = notes
    cur.setdefault("regions", {})[name] = entry
    with open(USER_OVERRIDE_PATH, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    return USER_OVERRIDE_PATH


def normalize_policy_query(raw: str) -> str:
    """去掉书名号，统一文号年份括号为标准〔YYYY〕并保持文号完整。"""
    q = raw.strip()
    q = q.replace("《", " ").replace("》", " ")
    q = re.sub(r"([\u4e00-\u9fa5]{1,12})\s*[\[【\(〔]((?:19|20)\d{2})[\]】\)〕]\s*(\d{1,5})\s*号", r"\1〔\2〕\3号", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def classify_source(url: str) -> Dict[str, Any]:
    """按五级规划信源分级标准给 URL 打标。"""
    host = (urlparse(url).hostname or "").lower()
    path = (urlparse(url).path or "").lower()
    is_gazette = bool(GAZETTE_OR_POLICY_PATH_RE.search(path))

    if host in CENTRAL_HOSTS_EXACT or host.endswith(CENTRAL_SUFFIXES):
        return {
            "source_tier": "T1_central",
            "tier_rank": 1,
            "tier_label": "国家级官方源",
            "is_policy_channel": is_gazette or "zhengce" in path,
            "needs_primary_verify": False,
        }

    if host.endswith(".gov.cn") or host == "gov.cn":
        return {
            "source_tier": "T2_local_gov",
            "tier_rank": 2,
            "tier_label": "地方政府/部门官网",
            "is_policy_channel": is_gazette,
            "needs_primary_verify": False,
        }

    if any(h in host for h in PUBLIC_PLATFORM_HINTS):
        return {
            "source_tier": "T3_public_platform",
            "tier_rank": 3,
            "tier_label": "公共资源/政务服务平台",
            "is_policy_channel": is_gazette,
            "needs_primary_verify": False,
        }

    if any(host == m or host.endswith("." + m) for m in STATE_MEDIA_HOSTS):
        return {
            "source_tier": "T4_state_media",
            "tier_rank": 4,
            "tier_label": "官媒/行业官方媒体",
            "is_policy_channel": False,
            "needs_primary_verify": True,
        }

    return {
        "source_tier": "T5_media_repost",
        "tier_rank": 5,
        "tier_label": "商业媒体/转载（需核官网原文）",
        "is_policy_channel": False,
        "needs_primary_verify": True,
    }


def _find_region(reg: Dict[str, Any], name: str) -> Optional[str]:
    if not name:
        return None
    name_c = name.strip()
    regions = reg.get("regions") or {}
    if name_c in regions:
        return name_c
    for rname, cfg in regions.items():
        if name_c in (cfg.get("aliases") or []):
            return rname
    stripped = re.sub(r"(特别合作区|新区|自治州|自治区|市|区|省|县)$", "", name_c)
    if stripped and stripped in regions:
        return stripped
    for rname, cfg in regions.items():
        if stripped and stripped in (cfg.get("aliases") or []):
            return rname
    # 兼容“省+市”连写，同时避免把未入库县域误折叠到上级省门户
    norm_no_suffix = re.sub(r"(省|市|区|县)", "", name_c)
    if norm_no_suffix in regions:
        return norm_no_suffix
    for rname, cfg in regions.items():
        if norm_no_suffix in (cfg.get("aliases") or []):
            return rname
    return None


def _collect_region_domains(
    reg: Dict[str, Any], region_key: str, dept_keys: List[str], include_parent: bool = True
) -> Tuple[List[str], List[str], List[str]]:
    regions = reg.get("regions") or {}
    cfg = regions[region_key]
    doms: List[str] = []
    gd_ids: List[str] = []
    notes: List[str] = []

    def add_cfg(c: Dict[str, Any]) -> None:
        dmap = c.get("domains") or {}
        for dk in dept_keys:
            for d in dmap.get(dk, []):
                if d not in doms:
                    doms.append(d)
        for d in dmap.get("gov", []):
            if d not in doms:
                doms.append(d)
        for gid in c.get("gd_site_ids", []):
            if gid not in gd_ids:
                gd_ids.append(gid)
        if c.get("notes"):
            notes.append(c["notes"])

    add_cfg(cfg)

    if include_parent and cfg.get("parent") and cfg["parent"] in regions:
        p_cfg = regions[cfg["parent"]]
        if cfg.get("level") == "district" or not doms:
            add_cfg(p_cfg)

    return doms, gd_ids, notes


def build_route_plan(
    query: str,
    *,
    track: str = "upper-policy",
    region: str = "",
    scope: str = "gov",
    extra_sites: Optional[List[str]] = None,
) -> RoutePlan:
    reg = load_registry()
    tracks = reg.get("tracks") or {}
    if track not in tracks:
        raise ValueError(f"未知规划轨道 '{track}'，可选：{', '.join(tracks.keys())}")
    tcfg = tracks[track]
    dept_keys = tcfg.get("dept_keys") or ["gov"]
    central_keys = tcfg.get("central_keys") or ["gov"]

    norm_q = normalize_policy_query(query)
    exclude_domains = reg.get("exclude_noise_domains", []) if scope == "gov" else []

    site_domains: List[str] = []
    gd_ids: List[str] = []
    notes: List[str] = []
    matched_region: Optional[str] = None
    route_mode = "central_only"
    effective_q = norm_q

    if extra_sites:
        site_domains.extend(s.strip() for s in extra_sites if s.strip())

    region_parts = [p.strip() for p in re.split(r"[,，、]+", region) if p.strip()] if region else []

    if not region_parts:
        if scope == "gov":
            for ck in central_keys:
                for d in (reg.get("central") or {}).get(ck, []):
                    if d not in site_domains:
                        site_domains.append(d)
            if "gov.cn" not in site_domains and track in ("upper-policy", "benchmark"):
                site_domains.append("gov.cn")
        route_mode = "central_only"
    elif len(region_parts) > 1:
        route_mode = "multi_region"
        matched_list = []
        for rp in region_parts:
            mk = _find_region(reg, rp)
            if mk:
                matched_list.append(mk)
                d_list, g_list, n_list = _collect_region_domains(reg, mk, dept_keys)
                for d in d_list:
                    if d not in site_domains:
                        site_domains.append(d)
                for g in g_list:
                    if g not in gd_ids:
                        gd_ids.append(g)
                notes.extend(n_list)
            else:
                if "gov.cn" not in site_domains:
                    site_domains.append("gov.cn")
        matched_region = ",".join(matched_list) if matched_list else None
    else:
        rp = region_parts[0]
        mk = _find_region(reg, rp)
        if mk:
            matched_region = mk
            route_mode = "exact_region"
            d_list, g_list, n_list = _collect_region_domains(reg, mk, dept_keys)
            for d in d_list:
                if d not in site_domains:
                    site_domains.append(d)
            gd_ids.extend(g_list)
            notes.extend(n_list)
            cfg = (reg.get("regions") or {}).get(mk, {})
            if cfg.get("level") == "district" and rp not in effective_q:
                parent_name = cfg.get("parent") or ""
                short_rp = rp[len(parent_name):] if (parent_name and rp.startswith(parent_name)) else rp
                if short_rp and short_rp not in effective_q:
                    effective_q = f"{short_rp} {effective_q}"
        else:
            route_mode = "national_fallback"
            if scope == "gov" and "gov.cn" not in site_domains:
                site_domains.append("gov.cn")
            if rp not in effective_q:
                effective_q = f"{rp} {effective_q}"
            notes.append(
                f"地区“{rp}”未在预置站点表中，已自动切至全国 gov.cn 泛化路由；"
                "命中官站后可用 `sites add` 沉淀到 ~/.config/govdoc/sites.json。"
            )

    if scope == "all":
        site_domains = [s.strip() for s in (extra_sites or []) if s.strip()]

    return RoutePlan(
        track=track,
        track_label=tcfg.get("label", track),
        region_input=region,
        matched_region=matched_region,
        route_mode=route_mode,
        site_domains=site_domains,
        exclude_domains=exclude_domains,
        normalized_query=norm_q,
        effective_query=effective_q,
        gd_site_ids=gd_ids,
        prefer_direct=tcfg.get("prefer_direct", []),
        notes=notes,
    )
