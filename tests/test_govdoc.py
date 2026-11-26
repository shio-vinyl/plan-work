"""govdoc 离线单元测试（仅使用虚构/通用示例数据，不依赖外网）。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from _govdoc.archiver import archive_parsed_page
from _govdoc.cli import _load_search_env
from _govdoc.http_client import CircuitOpenError, GovHttpClient, decode_bytes, validate_binary_bytes
from _govdoc.official_dbs import _split_dbba_query_area
from _govdoc.parser import extract_ofd_text, parse_gov_html
from _govdoc.providers import (
    SearchHit,
    _baidu_match_sites,
    _enrich_hit,
    _rank_key,
    build_provider_payload,
    deduplicate_hits,
    parse_baidu_response,
    parse_bocha_response,
    parse_gd_jsonp,
    parse_iqs_response,
)
from _govdoc.registry import build_route_plan, classify_source, normalize_policy_query


class TestCredentials(unittest.TestCase):
    def test_load_search_env_reads_config_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".config" / "govdoc" / "env"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(
                "# 示例凭证文件\nBAIDU_QIANFAN_API_KEY=demo-baidu-token\nBOCHA_API_KEY=demo-bocha-token\n",
                encoding="utf-8",
            )
            with patch.object(Path, "home", return_value=Path(tmp)), patch.dict(os.environ, {}, clear=True):
                _load_search_env()
                self.assertEqual(os.environ.get("BAIDU_QIANFAN_API_KEY"), "demo-baidu-token")
                self.assertEqual(os.environ.get("BOCHA_API_KEY"), "demo-bocha-token")

    def test_load_search_env_respects_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".config" / "govdoc" / "env"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(
                "BAIDU_QIANFAN_API_KEY=from-file\nALIYUN_IQS_API_KEY=iqs-from-file\n",
                encoding="utf-8",
            )
            with patch.object(Path, "home", return_value=Path(tmp)), patch.dict(
                os.environ, {"BAIDU_QIANFAN_API_KEY": "from-env"}, clear=True
            ):
                _load_search_env()
                self.assertEqual(os.environ.get("BAIDU_QIANFAN_API_KEY"), "from-env")
                self.assertEqual(os.environ.get("ALIYUN_IQS_API_KEY"), "iqs-from-file")

    def test_load_search_env_respects_baidu_alias_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".config" / "govdoc" / "env"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text("BAIDU_QIANFAN_API_KEY=from-file\n", encoding="utf-8")
            with patch.object(Path, "home", return_value=Path(tmp)), patch.dict(
                os.environ, {"QIANFAN_API_KEY": "alias-from-env"}, clear=True
            ):
                _load_search_env()
                self.assertNotIn("BAIDU_QIANFAN_API_KEY", os.environ)
                self.assertEqual(os.environ.get("QIANFAN_API_KEY"), "alias-from-env")


class TestRegistryAndRouting(unittest.TestCase):
    def test_route_exact_region(self) -> None:
        plan = build_route_plan("土地功能混合", track="spatial-plan", region="北京")
        self.assertEqual(plan.route_mode, "exact_region")
        self.assertEqual(plan.matched_region, "北京")
        self.assertIn("ghzrzyw.beijing.gov.cn", plan.site_domains)
        self.assertIn("www.beijing.gov.cn", plan.site_domains)

    def test_route_national_fallback_for_uncataloged_county(self) -> None:
        plan = build_route_plan("通则式规划管理规定", track="spatial-plan", region="四川示例县")
        self.assertEqual(plan.route_mode, "national_fallback")
        self.assertIsNone(plan.matched_region)
        self.assertEqual(plan.site_domains, ["gov.cn"])
        self.assertIn("四川示例县", plan.effective_query)

    def test_route_multi_region_benchmark(self) -> None:
        plan = build_route_plan("存量建筑用途转换", track="benchmark", region="北京,上海,广州")
        self.assertEqual(plan.route_mode, "multi_region")
        self.assertIn("ghzrzyw.beijing.gov.cn", plan.site_domains)
        self.assertIn("ghzyj.sh.gov.cn", plan.site_domains)
        self.assertIn("ghzyj.gz.gov.cn", plan.site_domains)

    def test_normalize_policy_query_keeps_doc_no_cohesive(self) -> None:
        q = normalize_policy_query("《关于试点工作的通知》 甲府规[2025]12号")
        self.assertNotIn("《", q)
        self.assertIn("甲府规〔2025〕12号", q)

    def test_classify_source_five_tiers(self) -> None:
        self.assertEqual(classify_source("https://www.gov.cn/zhengce/content/2025-01/content_1.htm")["source_tier"], "T1_central")
        self.assertEqual(classify_source("https://www.mnr.gov.cn/dt/ywbb/202501/t1.html")["source_tier"], "T1_central")
        t2 = classify_source("https://ghzrzyw.beijing.gov.cn/zhengwuxinxi/zcwj/t1.html")
        self.assertEqual(t2["source_tier"], "T2_local_gov")
        self.assertTrue(t2["is_policy_channel"])
        self.assertEqual(classify_source("https://ggzy.example.org.cn/deal/1.html")["source_tier"], "T3_public_platform")
        self.assertEqual(classify_source("https://www.news.cn/20250101/abc.html")["source_tier"], "T4_state_media")
        t5 = classify_source("https://example-blog.com/article/123")
        self.assertEqual(t5["source_tier"], "T5_media_repost")
        self.assertTrue(t5["needs_primary_verify"])


class TestHttpSafety(unittest.TestCase):
    def test_decode_bytes_gb18030(self) -> None:
        raw = '<meta charset="gb2312"><title>规划公示通知</title>'.encode("gb18030")
        txt, enc, warn = decode_bytes(raw)
        self.assertIn("规划公示通知", txt)
        self.assertEqual(enc, "gb18030")
        self.assertIsNone(warn)

    def test_validate_binary_bytes_rejects_fake_pdf_html(self) -> None:
        ok, flag = validate_binary_bytes(b"<!DOCTYPE html><html><title>404</title></html>", "plan.pdf", "text/html")
        self.assertFalse(ok)
        self.assertEqual(flag, "FAKE_BINARY_HTML")

        ok_real, flag_real = validate_binary_bytes(b"%PDF-1.7\n1 0 obj\n", "plan.pdf", "application/pdf")
        self.assertTrue(ok_real)
        self.assertEqual(flag_real, "OK")

    def test_circuit_breaker_on_412(self) -> None:
        client = GovHttpClient(default_delay=0.0)
        flag, _ = client._classify_response("blocked.gov.cn", 412, b"<html>js challenge</html>", "")
        self.assertEqual(flag, "WAF_BLOCKED")
        with self.assertRaises(CircuitOpenError):
            client.request("https://blocked.gov.cn/xxgk/1.html")


class TestProviders(unittest.TestCase):
    def test_gd_provider_blocks_more_than_10_sites(self) -> None:
        plan = build_route_plan("城市更新", track="housing-renewal", region="广州")
        with self.assertRaises(ValueError):
            build_provider_payload("gd", plan, gd_sites=[str(200001 + i) for i in range(11)])

    def test_baidu_match_sites_skips_bare_gov_cn_and_expands_www_root(self) -> None:
        sites_fb, local_fb = _baidu_match_sites(["gov.cn"])
        self.assertEqual(sites_fb, [])
        self.assertTrue(local_fb)

        sites_city, local_city = _baidu_match_sites(["ghzyj.gz.gov.cn", "www.gz.gov.cn"])
        self.assertFalse(local_city)
        self.assertIn("ghzyj.gz.gov.cn", sites_city)
        self.assertIn("www.gz.gov.cn", sites_city)
        self.assertIn("gz.gov.cn", sites_city)

    def test_baidu_parser_separates_doc_no_and_cited_doc_nos(self) -> None:
        raw = {
            "references": [
                {
                    "title": "关于印发明确用途转换办法的通知（甲规自规〔2025〕2号）",
                    "url": "https://ghzyj.example.gov.cn/zcwj/202501/t1.html",
                    "date": "2025-01-15 10:00:00",
                    "website": "示例市规资局",
                    "snippet": "依据国办发〔2021〕22号文件精神制定本办法。",
                    "rerank_score": 0.91,
                },
                {
                    "title": "某宗地国有建设用地使用权挂牌出让公告",
                    "url": "https://ghzyj.example.gov.cn/gg/202502/t2.html",
                    "date": "2025-02-01",
                    "snippet": "根据甲府规〔2024〕8号与乙规自发〔2025〕3号有关要求，现予挂牌。",
                },
                {
                    "title": "被过滤的商业转载",
                    "url": "https://baijiahao.baidu.com/s?id=123",
                    "date": "2025-02-02",
                    "snippet": "转载内容",
                },
            ]
        }
        hits = parse_baidu_response(raw, exclude_domains=["baijiahao.baidu.com"])
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].doc_no, "甲规自规〔2025〕2号")
        self.assertEqual(hits[0].cited_doc_nos, ["国办发〔2021〕22号"])
        self.assertEqual(hits[1].doc_no, "")
        self.assertEqual(hits[1].cited_doc_nos, ["甲府规〔2024〕8号", "乙规自发〔2025〕3号"])

    def test_bocha_iqs_gd_parsers(self) -> None:
        bocha_hits = parse_bocha_response(
            {
                "data": {
                    "webPages": {
                        "value": [
                            {
                                "name": "示例市住房发展规划（2026—2030年）",
                                "url": "https://zjj.example.gov.cn/zfxxgk/1.html",
                                "datePublished": "2026-03-01T00:00:00Z",
                                "siteName": "示例市住建局",
                                "summary": "明确保障性租赁住房筹集目标。",
                            }
                        ]
                    }
                }
            }
        )
        self.assertEqual(len(bocha_hits), 1)
        self.assertEqual(bocha_hits[0].source_tier, "T2_local_gov")

        iqs_hits = parse_iqs_response(
            {
                "pageItems": [
                    {
                        "title": "自然资源部关于深化规划改革的意见",
                        "link": "https://www.mnr.gov.cn/dt/ywbb/2025/t1.html",
                        "publishedTime": "2025-06-10",
                        "hostname": "www.mnr.gov.cn",
                        "summary": "深化详细规划改革。",
                        "rerankScore": 0.85,
                    }
                ]
            }
        )
        self.assertEqual(len(iqs_hits), 1)
        self.assertEqual(iqs_hits[0].source_tier, "T1_central")

        gd_hits, total = parse_gd_jsonp(
            'cb({"count":1,"results":[{"title":"<em>城市更新</em>实施细则","post_url":"https://www.gd.gov.cn//zwgk/1.html","pub_time":"2025-04-01","site_name":"省政府门户"}]})'
        )
        self.assertEqual(total, 1)
        self.assertEqual(gd_hits[0].title, "城市更新实施细则")
        self.assertEqual(gd_hits[0].url, "https://www.gd.gov.cn/zwgk/1.html")

    def test_deduplicate_hits_folds_mpost_and_same_title(self) -> None:
        h1 = _enrich_hit(
            SearchHit(
                title="示例区某片区城市更新单元规划草案公示",
                url="http://www.demo-city.gov.cn/mobile/content/mpost_1001.html",
                date="2025-05-01",
            )
        )
        h2 = _enrich_hit(
            SearchHit(
                title="示例区某片区城市更新单元规划草案公示",
                url="https://www.demo-city.gov.cn/zwgk/content/post_1001.html",
                date="2025-05-01",
            )
        )
        h3 = _enrich_hit(
            SearchHit(
                title="示例区某片区城市更新单元规划草案公示",
                url="https://sub.demo-city.gov.cn/tzgg/202505/t20250501_99.html",
                date="2025-05-01",
            )
        )
        deduped = deduplicate_hits([h1, h2, h3])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].url, h2.url)
        self.assertEqual(len(deduped[0].alternate_urls), 2)

    def test_index_page_detection_and_track_rerank(self) -> None:
        h_index = _enrich_hit(
            SearchHit(
                title="意见征集栏目",
                url="https://www.demo-city.gov.cn/hdjl/yjzj/index.html",
                date="2026-11-16",
            )
        )
        self.assertTrue(h_index.is_index_page)

        h_auction = _enrich_hit(
            SearchHit(
                title="示例区2025-01号宗地国有建设用地使用权挂牌出让公告",
                url="https://ghzyj.demo-city.gov.cn/gg/202503/t1.html",
                date="2025-03-01",
                snippet="依据甲府规〔2025〕2号新型产业用地规定",
            )
        )
        h_policy = _enrich_hit(
            SearchHit(
                title="示例区关于印发新型产业用地管理办法的通知（甲府规〔2025〕2号）",
                url="https://www.demo-city.gov.cn/zcwj/202501/t2.html",
                date="2025-01-10",
                snippet="明确新型产业用地混合比例与准入条件",
            )
        )
        ranked = sorted(
            [h_auction, h_policy],
            key=lambda x: _rank_key(x, "spatial-plan", ["甲府规〔2025〕2号", "新型产业用地"]),
        )
        self.assertEqual(ranked[0].url, h_policy.url)


class TestOfficialDbs(unittest.TestCase):
    def test_split_dbba_query_area(self) -> None:
        q1, a1 = _split_dbba_query_area("浙江 城市更新 导则", "")
        self.assertEqual(a1, "浙江")
        self.assertEqual(q1, "城市更新")

        q2, a2 = _split_dbba_query_area("国土空间规划 指标", "广东")
        self.assertEqual(a2, "广东")
        self.assertEqual(q2, "国土空间规划")


class TestParserAndArchiver(unittest.TestCase):
    def test_parse_gov_html_metadata_attachments_and_warnings(self) -> None:
        sample_html = """<!DOCTYPE html>
<html>
<head>
  <title>关于印发存量建筑转换用途实施办法（试行）的通知 - 示例市规划和自然资源局</title>
  <meta name="ArticleTitle" content="关于印发存量建筑转换用途实施办法（试行）的通知" />
  <meta name="SiteName" content="示例市规划和自然资源局" />
  <meta name="SiteIDCode" content="4400000001" />
  <meta name="ColumnName" content="规范性文件" />
  <meta name="PubDate" content="2025-08-10 09:00:00" />
  <meta name="ContentSource" content="示例市规划和自然资源局" />
</head>
<body>
  <div class="meta">发文字号：甲规自规〔2025〕6号 成文日期：2025-08-01 有效性：现行有效</div>
  <div class="content">
    <p>各区人民政府、有关单位：现印发实施办法，自2025年9月1日起施行，有效期3年。本文有删减。</p>
    <p><a href="/files/method_2025.pdf">附件1：存量建筑转换用途正面清单.pdf</a></p>
    <p><a href="/en/Chinese.html">附件2：English Version</a></p>
    <p><img src="/upload/plan_control_map.png" alt="指标控制图" /></p>
    <p><img src="/pubimages/red.png" alt="党政机关" /></p>
  </div>
</body>
</html>"""
        parsed = parse_gov_html("https://ghzyj.example.gov.cn/zcwj/202508/t1.html", sample_html)
        self.assertEqual(parsed["doc_no"], "甲规自规〔2025〕6号")
        self.assertEqual(parsed["draft_date"], "2025-08-01")
        self.assertEqual(parsed["pub_date"], "2025-08-10")
        self.assertEqual(parsed["site_id_code"], "4400000001")
        self.assertEqual(parsed["validity"]["explicit_status"], "现行有效")

        att_urls = [a["url"] for a in parsed["attachments"]]
        self.assertIn("https://ghzyj.example.gov.cn/files/method_2025.pdf", att_urls)
        self.assertIn("https://ghzyj.example.gov.cn/upload/plan_control_map.png", att_urls)
        self.assertFalse(any("Chinese.html" in u for u in att_urls))
        self.assertFalse(any("red.png" in u for u in att_urls))

        warn_str = " ".join(parsed["warnings"])
        self.assertIn("REDACTED_PUBLIC_TEXT", warn_str)
        self.assertIn("DRAFT_OR_TRIAL", warn_str)

    def test_parse_gov_html_spa_shell_warning(self) -> None:
        spa_html = '<html><head><title>意见征集</title></head><body><div id="app">暂无附件</div></body></html>'
        parsed = parse_gov_html("https://example.gov.cn/hdjlpt/yjzj/1001", spa_html)
        self.assertTrue(any("NEEDS_BROWSER_RENDER" in w for w in parsed["warnings"]))

    def test_extract_ofd_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ofd_path = os.path.join(tmp, "sample.ofd")
            page_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<ofd:Page xmlns:ofd="http://www.ofdspec.org/2016">'
                '<ofd:Content><ofd:Layer><ofd:TextObject>'
                '<ofd:TextCode>示例公文正文第一条</ofd:TextCode>'
                '</ofd:TextObject></ofd:Layer></ofd:Content></ofd:Page>'
            )
            with zipfile.ZipFile(ofd_path, "w") as z:
                z.writestr("Doc_0/Pages/Page_0/Content.xml", page_xml)
            res = extract_ofd_text(ofd_path)
            self.assertTrue(res["ok"])
            self.assertEqual(res["pages"], 1)
            self.assertIn("示例公文正文第一条", res["text"])

    def test_archive_parsed_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = GovHttpClient(default_delay=0.0)
            parsed = {
                "url": "https://www.example.gov.cn/zcwj/202501/t1.html",
                "title": "示例规划实施办法",
                "doc_no": "甲府规〔2025〕1号",
                "agency": "示例市人民政府",
                "site_name": "示例市政府门户",
                "site_id_code": "1100000001",
                "column_name": "规范性文件",
                "pub_date": "2025-01-20",
                "draft_date": "2025-01-15",
                "validity": {"explicit_status": "现行有效", "clauses": ["有效期5年"]},
                "attachments": [
                    {
                        "url": "https://www.example.gov.cn/files/list.pdf",
                        "label": "附表1.pdf",
                        "kind": "document",
                        "ext": ".pdf",
                    }
                ],
                "warnings": [],
                "text": "第一条 为规范城乡规划管理制定本办法。",
            }

            def fake_download(url: str, dest: str, referer=None):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(b"%PDF-1.7\n")
                return True, "OK", {"size": 9}

            with patch.object(client, "download_file", side_effect=fake_download):
                out = archive_parsed_page(client, b"<html>ok</html>", parsed, tmp)

            self.assertTrue(os.path.exists(out["html_path"]))
            self.assertTrue(os.path.exists(out["txt_path"]))
            self.assertTrue(os.path.exists(out["manifest_path"]))
            txt_content = Path(out["txt_path"]).read_text(encoding="utf-8")
            self.assertIn("doc_no: 甲府规〔2025〕1号", txt_content)
            self.assertIn("T2_local_gov", txt_content)
            manifest_lines = Path(out["manifest_path"]).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(manifest_lines), 1)
            rec = json.loads(manifest_lines[0])
            self.assertEqual(rec["doc_no"], "甲府规〔2025〕1号")


if __name__ == "__main__":
    unittest.main()
