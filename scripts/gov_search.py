"""广东省政府网站群统一搜索（search.gd.gov.cn）命令行版。

境外搜索引擎搜不到区级政府网站时用它，需要本机能直连国内网络。

用法：
  python3 -I gov_search.py 关键词 --site 站点id [--pos title|all] [--page 1] [--size 20]

站点 id 写在各站搜索页源码的 `sites:` 参数里，例如 2（广东省人民政府）、
755001（深圳市人民政府）；多个站点用逗号分隔。其他省份的统一搜索接口不同，照
references/数据来源与检索.md 的方法找到接口后另写。

输出：每行 “日期 | 标题 | 链接”。
"""
import argparse
import html
import json
import re
import subprocess
import sys

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def main():
    p = argparse.ArgumentParser(description="广东省政府网站群统一搜索")
    p.add_argument("text", help="关键词")
    p.add_argument("--site", required=True, help="站点 id，多个用逗号分隔")
    p.add_argument("--pos", default="all", choices=["all", "title"], help="搜全文或只搜标题")
    p.add_argument("--page", default="1")
    p.add_argument("--size", default="20")
    p.add_argument("--referer", default="https://www.gd.gov.cn/", help="接口要求带 Referer，一般用任一省内政府站即可")
    a = p.parse_args()

    cmd = [
        "curl", "-s", "-m", "20", "-G", "-A", UA, "-e", a.referer,
        "https://search.gd.gov.cn/jsonp/sites",
        "--data-urlencode", f"text={a.text}",
        "-d", f"callback=cb&position={a.pos}&page={a.page}&pagesize={a.size}"
              f"&including_link_doc=1&including_url_doc=1&sites={a.site}",
    ]
    raw = subprocess.run(cmd, capture_output=True, text=True).stdout
    m = re.match(r"^\s*cb\((.*)\)\s*;?\s*$", raw, re.S)
    if not m:
        print("搜索失败（网络不通，或接口要求变了）：", raw[:300])
        sys.exit(1)
    d = json.loads(m.group(1))
    results = d.get("results", [])
    print(f"# 第 {a.page} 页，{len(results)} 条")
    for r in results:
        url = (r.get("post_url") or r.get("url") or "").replace(".cn//", ".cn/")
        title = html.unescape(re.sub(r"<[^>]+>", "", r.get("title", "")))
        print(f"{r.get('pub_time', '')} | {title} | {url}")


if __name__ == "__main__":
    main()
