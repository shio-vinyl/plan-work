"""按 plan-work 目录规范归档政府网页原件、带 YAML 核验头的清洗正文、附件与索引。"""
from __future__ import annotations

import datetime
import json
import os
import re
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

from .http_client import GovHttpClient
from .registry import classify_source


def _safe_filename(s: str, max_len: int = 48) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (s or "").strip())
    s = re.sub(r"_+", "_", s).strip("._ ")
    return s[:max_len] or "untitled"


def _date_prefix(pub_date: str, draft_date: str) -> str:
    d = pub_date or draft_date or ""
    digits = re.sub(r"\D", "", d)
    if len(digits) >= 8:
        return digits[:8]
    return datetime.date.today().strftime("%Y%m%d")


def _attachment_filename(att: Dict[str, str], date_prefix: str, idx: int) -> str:
    label = (att.get("label") or "").strip()
    ext = (att.get("ext") or "").lower()
    if not ext:
        ext = os.path.splitext(urlparse(att["url"]).path.lower())[1]
    base = unquote(os.path.basename(urlparse(att["url"]).path))
    stem = label if (label and len(label) >= 3) else os.path.splitext(base)[0]
    if ext and stem.lower().endswith(ext):
        stem = stem[: -len(ext)]
    stem_clean = _safe_filename(stem, max_len=64)
    if not stem_clean or stem_clean == "untitled":
        stem_clean = f"attachment_{idx}"
    return f"{date_prefix}_{stem_clean}{ext}"


def format_txt_with_header(parsed: Dict[str, Any], source_info: Dict[str, Any], saved_atts: List[Dict[str, Any]]) -> str:
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    val = parsed.get("validity") or {}
    header_lines = [
        "---",
        f"title: {parsed.get('title', '')}",
        f"url: {parsed.get('url', '')}",
        f"fetched_at: {now_str}",
        f"source_tier: {source_info.get('source_tier', '')} ({source_info.get('tier_label', '')})",
        f"doc_no: {parsed.get('doc_no', '')}",
        f"agency: {parsed.get('agency', '')}",
        f"site_name: {parsed.get('site_name', '')}",
        f"site_id_code: {parsed.get('site_id_code', '')}",
        f"column_name: {parsed.get('column_name', '')}",
        f"draft_date: {parsed.get('draft_date', '')}",
        f"pub_date: {parsed.get('pub_date', '')}",
        f"validity_status: {val.get('explicit_status', '')}",
        f"validity_clauses: {'; '.join(val.get('clauses') or [])}",
        f"warnings: {'; '.join(parsed.get('warnings') or [])}",
        f"attachments_count: {len(saved_atts)}",
        "---",
        "",
    ]
    if saved_atts:
        header_lines.append("【已登记附件】")
        for a in saved_atts:
            st = a.get("download_status", "listed")
            loc = a.get("local_path", "")
            header_lines.append(f"- [{st}] {a.get('label', '')} | {a.get('url', '')}" + (f" -> {loc}" if loc else ""))
        header_lines.append("")
    header_lines.append(parsed.get("text", ""))
    return "\n".join(header_lines)


def archive_parsed_page(
    client: GovHttpClient,
    raw_html_bytes: bytes,
    parsed: Dict[str, Any],
    archive_dir: str,
    *,
    download_attachments: bool = True,
    download_images: bool = False,
) -> Dict[str, Any]:
    os.makedirs(archive_dir, exist_ok=True)
    att_dir = os.path.join(archive_dir, "附件")

    date_pfx = _date_prefix(parsed.get("pub_date", ""), parsed.get("draft_date", ""))
    agency_part = _safe_filename(parsed.get("agency") or parsed.get("site_name") or "官网", max_len=16)
    title_part = _safe_filename(parsed.get("doc_no") or parsed.get("title") or "公文", max_len=40)
    if parsed.get("doc_no") and parsed.get("title"):
        title_part = _safe_filename(f"{parsed['doc_no']}_{parsed['title']}", max_len=52)

    base_name = f"{date_pfx}_{agency_part}_{title_part}"
    html_path = os.path.join(archive_dir, base_name + ".html")
    txt_path = os.path.join(archive_dir, base_name + ".txt")

    with open(html_path, "wb") as f:
        f.write(raw_html_bytes)

    source_info = classify_source(parsed.get("url", ""))
    saved_atts: List[Dict[str, Any]] = []

    for idx, att in enumerate(parsed.get("attachments") or [], 1):
        rec = dict(att)
        should_dl = download_attachments and (att.get("kind") == "document" or download_images)
        if should_dl:
            os.makedirs(att_dir, exist_ok=True)
            fn = _attachment_filename(att, date_pfx, idx)
            dest = os.path.join(att_dir, fn)
            ok, flag, info = client.download_file(att["url"], dest, referer=parsed.get("url"))
            rec["download_status"] = "ok" if ok else flag
            if ok:
                rec["local_path"] = os.path.relpath(dest, archive_dir)
                rec["size"] = info.get("size", 0)
            else:
                rec["error"] = info.get("error", "")
        else:
            rec["download_status"] = "skipped_image" if att.get("kind") == "image" else "listed_only"
        saved_atts.append(rec)

    txt_content = format_txt_with_header(parsed, source_info, saved_atts)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(txt_content)

    manifest_entry = {
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "url": parsed.get("url", ""),
        "title": parsed.get("title", ""),
        "doc_no": parsed.get("doc_no", ""),
        "agency": parsed.get("agency", ""),
        "pub_date": parsed.get("pub_date", ""),
        "draft_date": parsed.get("draft_date", ""),
        "source_tier": source_info.get("source_tier", ""),
        "validity": parsed.get("validity", {}),
        "warnings": parsed.get("warnings", []),
        "html_file": os.path.basename(html_path),
        "txt_file": os.path.basename(txt_path),
        "attachments": saved_atts,
    }
    manifest_path = os.path.join(archive_dir, "manifest.jsonl")
    with open(manifest_path, "a", encoding="utf-8") as mf:
        mf.write(json.dumps(manifest_entry, ensure_ascii=False) + "\n")

    return {
        "archive_dir": archive_dir,
        "html_path": html_path,
        "txt_path": txt_path,
        "manifest_path": manifest_path,
        "attachments": saved_atts,
    }
