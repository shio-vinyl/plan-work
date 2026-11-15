"""抓取政府网页正文，列出并下载附件。

本机直连，适合境外抓不到的国内政府网站。

用法：
  python3 -I gov_fetch.py URL [--grep 正则] [--max 8000] [--out 下载目录]

- 网页：打印标题、附件链接和正文；--grep 只打印匹配的行。
- 附件（pdf/doc/docx/xls/xlsx/et/wps/zip）：下载到 --out（默认 ./downloads），打印本地路径。
"""
import argparse
import html
import os
import re
import subprocess
import sys
from urllib.parse import urljoin

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"
ATTACH = r"\.(pdf|docx?|xlsx?|et|wps|zip|rar)(\?|$)"


def download(url, out):
    os.makedirs(out, exist_ok=True)
    name = re.sub(r"[^\w.\-]", "_", url.split("/")[-1].split("?")[0]) or "file"
    path = os.path.join(out, name)
    subprocess.run(["curl", "-s", "-L", "-m", "300", "-A", UA, "-o", path, url])
    size = os.path.getsize(path) if os.path.exists(path) else 0
    print(f"已下载：{path}（{size} 字节）")


def main():
    p = argparse.ArgumentParser(description="抓取政府网页正文和附件")
    p.add_argument("url")
    p.add_argument("--grep", default="", help="只打印匹配这个正则的行")
    p.add_argument("--max", type=int, default=8000, help="正文最多打印多少字")
    p.add_argument("--out", default="downloads", help="附件下载目录")
    a = p.parse_args()

    if re.search(ATTACH, a.url, re.I):
        download(a.url, a.out)
        return

    raw = subprocess.run(["curl", "-s", "-L", "-m", "30", "-A", UA, a.url], capture_output=True).stdout
    if not raw:
        print("抓取失败：没有返回内容")
        sys.exit(1)
    t = raw.decode("utf-8", errors="ignore")
    title = re.search(r"<title>(.*?)</title>", t, re.S)
    links = sorted({urljoin(a.url, h) for h in re.findall(r'href="([^"]+)"', t) if re.search(ATTACH, h, re.I)})

    t = re.sub(r"(?is)<(script|style).*?</\1>", "", t)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</h\d>|</li>|</div>|</td>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t　\xa0]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()

    print("标题：", title.group(1).strip() if title else "")
    if links:
        print("附件：")
        for link in links:
            print("  ", link)
    if a.grep:
        for line in t.split("\n"):
            if re.search(a.grep, line):
                print("…", line[:400])
    else:
        print(t[: a.max])


if __name__ == "__main__":
    main()
