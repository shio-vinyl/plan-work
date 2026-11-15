"""从项目材料里找出引用的上位规划、政策文件和标准，列成依据清单。

用法：
  python3 -I extract_refs.py 文件或目录 [...] [--out 依据清单.xlsx] [--min 1]
                             [--phrases [提法.xlsx]]

抓两类东西：
- 书名号里的名称：《××市住房发展“十五五”规划》
- 文号：粤府〔2024〕12号、国办发〔2021〕22号（方括号、圆括号写法也认）
文号紧跟在书名之后（一般在括号里）时，记到这个文件名下。

还会找“编制依据”“规划依据”这类章节，记下每份材料在里面怎么列（含“在编”
“征求意见稿”这类状态），没有书名号的条目也收进来。

输出一张表（单工作表 xlsx），每个文件一行：
名称、文号、类别、层级、列入依据章节、出现次数、出现在哪些材料、首次位置、原文片段、
现行状态、本项目怎么用、核对。后三列留空，由人填。列入依据章节的排在前面。
终端打印两张 Markdown 表：各材料依据章节的对照，和全部引用。

--phrases：另外统计引号里的提法和口号（“去存量、稳增量、优结构”“好房子”），
按材料列出现次数和首次原文，写进另一张表，后面几列（来源、层次、能不能用、
用在哪里）留空由人填。“十四五”这类时期名和书名号里的引号不算。

类别和层级是按名称猜的，要人工改。文字提取复用同目录的 extract_text.py。
"""
import argparse
import datetime
import os
import re
import sys
import tempfile
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_text import extract  # noqa: E402

TITLE = re.compile(r"《([^《》\n]{2,60})》")
DOCNO = re.compile(r"(?:[一-龥]{1,12}[〔\[［(（]|〔)\s*(?:19|20)\d{2}\s*[〕\]］)）]\s*\d{1,4}\s*号")
# “编制依据”“规划依据”这类章节标题，以及下面的列表项
SEC_HEAD = re.compile(r"^\s*(?:[一二三四五六七八九十]+[、.．]|\d+(?:\.\d+)*[、.．\s]|第[一二三四五六七八九十\d]+[章节部分][、\s]*)?"
                      r"\s*(?:编制|规划|政策|主要|法律|工作|研究)?依据\s*[：:]?\s*$")
ITEM = re.compile(r"^\s*(?:[（(][一二三四五六七八九十\d]+[）)]|\d+[、.．)）]|[①-⑳]|[-•·▪]\s*)\s*(.+)$")
STD = re.compile(r"\b(?:GB|GB/T|CJJ|JGJ|SZDB/Z|SJG|DBJ)\s*[\d/.\-T]+[—\-]\s*\d{4}")

# 按名称结尾判断（先去掉结尾的括号，如“（2021—2035年）”“（试行）”“（修订）”）
CATS = [
    ("政策文件", r"(办法|意见|通知|细则|方案|决定|措施|指引|规则|函|批复)$"),
    ("标准规范", r"(标准|规范|导则|指南|规程|技术规定)$"),
    ("法律法规", r"((?<!办)法|条例|规定)$"),
    ("规划", r"(规划|纲要|纲要草案|计划)$"),
]
# 层级按名称开头的地名猜：省名开头算省；“××区（县、旗）”“××管委会”开头算区；“××市”开头算市。
# 直辖市名称落在“市”，要按本项目的层级关系人工改。
PROVINCES = ("河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|广东|海南|"
             "四川|贵州|云南|陕西|甘肃|青海|台湾|内蒙古|广西|西藏|宁夏|新疆")
LEVELS = [
    ("国家", r"^国|中华人民共和国|中共中央|全国|住房和城乡建设部|住房城乡建设部|自然资源部|国家"),
    ("省", rf"^(?:{PROVINCES})"),
    ("区", r"^(?!关于|城市|城乡|城镇|居住|住宅)[一-龥]{2,12}?(?:区(?!域)|县|旗|管委)"),
    ("市", r"^(?!关于|城市|城乡|城镇)[一-龥]{2,6}?市"),
]
# 文号前缀到层级：国家部委的常见前缀；省级政府和办公厅文号用省份简称开头（粤府、苏政发、浙政办发）。
# 市、区文号的前缀各地不同，不猜，交给名称判断。
DOC_LEVEL = [("国家", r"^(国|国办|建|自然资|财)"),
             ("省", r"^[京津沪渝冀晋辽吉黑苏浙皖闽赣鲁豫鄂湘粤琼川黔云陕甘蒙桂藏新](?:府|政)")]


def guess(pattern_list, text, default=""):
    for name, pat in pattern_list:
        if re.search(pat, text):
            return name
    return default


def norm(s):
    s = re.sub(r"\s+", "", s)
    s = s.replace("(", "（").replace(")", "）")
    return re.sub(r"[‐‑‒–—―~～\-]+", "—", s)


def stem(name):
    """去掉结尾括号，用来判断类别。"""
    return re.sub(r"(（[^（）]*）)+$", "", name)


def norm_docno(s):
    s = re.sub(r"\s+", "", s)
    return re.sub(r"[\[［(（]", "〔", re.sub(r"[\]］)）]", "〕", s))


def locator(text, pos, ext):
    if ext == ".pptx":
        m = list(re.finditer(r"===== 第 (\d+) 页", text[:pos]))
        return f"第{m[-1].group(1)}页" if m else ""
    if ext == ".pdf":
        return f"p{text.count(chr(12), 0, pos) + 1}"
    return ""


def snippet(text, a, b, pad=30):
    s = text[max(0, a - pad):b + pad]
    return re.sub(r"\s+", " ", s).strip()


def scan(path, text, refs):
    ext = os.path.splitext(path)[1].lower()
    fname = os.path.basename(path)
    taken = set()

    def add(key, name, docno, a, b, kind):
        r = refs.setdefault(key, OrderedDict(
            名称=name, 文号=docno, 类别=kind or "", 层级="", 出现次数=0, 出现在=OrderedDict(),
            依据章节=OrderedDict(), 首次位置="", 原文片段=""))
        if docno and not r["文号"]:
            r["文号"] = docno
        r["出现次数"] += 1
        r["出现在"][fname] = r["出现在"].get(fname, 0) + 1
        if not r["首次位置"]:
            loc = locator(text, a, ext)
            r["首次位置"] = fname + (f" {loc}" if loc else "")
            r["原文片段"] = snippet(text, a, b)

    for m in TITLE.finditer(text):
        name = norm(m.group(1))
        # 书名后 40 字内的文号算这个文件的
        tail = text[m.end():m.end() + 40]
        d = DOCNO.search(tail)
        docno = ""
        if d and not TITLE.search(tail[:d.start()]):
            docno = norm_docno(d.group(0))
            taken.add((m.end() + d.start(), docno))
        add(name, name, docno, m.start(), m.end(), "")

    for m in DOCNO.finditer(text):
        docno = norm_docno(m.group(0))
        if (m.start(), docno) in taken:
            continue
        key = next((k for k, r in refs.items() if r["文号"] == docno), "文号:" + docno)
        add(key, "", docno, m.start(), m.end(), "")

    for m in STD.finditer(text):
        code = re.sub(r"\s+", " ", m.group(0))
        add("标准:" + code, code, "", m.start(), m.end(), "标准规范")

    # 依据章节：记下每份材料在“编制依据”里怎么列的
    for raw, a in section_items(text, ext):
        t = TITLE.search(raw)
        if t:
            key = norm(t.group(1))
        else:
            name = norm(re.sub(r"[（(][^（）()]*[）)]\s*$", "", raw))
            key = next((k for k, r in refs.items() if r["名称"] == name), name)
            if key not in refs:
                add(key, name, "", a, a + len(raw), "")
        if key in refs:
            refs[key]["依据章节"][fname] = norm(raw)


def section_items(text, ext):
    """找出依据章节里的条目，返回 [(原文, 位置)]。
    docx/pdf：标题下面连续的列表行；pptx：标题所在页里以《开头的行。"""
    lines, pos = [], 0
    for ln in text.split("\n"):
        lines.append((ln, pos))
        pos += len(ln) + 1
    out = []
    for i, (ln, p) in enumerate(lines):
        if not SEC_HEAD.match(ln):
            continue
        if ext == ".pptx":
            j = i
            while j > 0 and not lines[j][0].startswith("===== 第"):
                j -= 1
            k = i + 1
            while k < len(lines) and not lines[k][0].startswith("===== 第"):
                k += 1
            out += [(l.strip(), q) for l, q in lines[j:k] if l.strip().startswith("《")]
            continue
        for ln2, p2 in lines[i + 1:i + 60]:
            if not ln2.strip():
                continue
            m = ITEM.match(ln2)
            if m:
                out.append((m.group(1).strip(), p2))
            elif ln2.strip().startswith("《"):
                out.append((ln2.strip(), p2))
            else:
                break
    return out


def merge_by_docno(refs):
    """同一个文号的几种写法并成一条，名称取最长的。"""
    by = OrderedDict()
    for r in refs.values():
        k = r["文号"] or id(r)
        if k in by:
            t = by[k]
            if len(r["名称"]) > len(t["名称"]):
                t["名称"] = r["名称"]
            t["出现次数"] += r["出现次数"]
            for f, n in r["出现在"].items():
                t["出现在"][f] = t["出现在"].get(f, 0) + n
            for f, v in r["依据章节"].items():
                t["依据章节"].setdefault(f, v)
        else:
            by[k] = r
    return by.values()


def finish(refs):
    rows = []
    for r in merge_by_docno(refs):
        name = r["名称"]
        if not r["类别"]:
            r["类别"] = guess(CATS, stem(name), "其他") if name else "只有文号"
        r["层级"] = guess(DOC_LEVEL, r["文号"]) if r["文号"] else ""
        if not r["层级"] and name:
            r["层级"] = guess(LEVELS, name)
        r["出现在"] = "；".join(f"{f}（{n}）" for f, n in r["出现在"].items())
        r["列入依据章节"] = "\n".join(f"{f}：{v}" for f, v in r["依据章节"].items())
        rows.append(r)
    order = {"规划": 0, "政策文件": 1, "法律法规": 2, "标准规范": 3, "只有文号": 4, "其他": 5}
    rows.sort(key=lambda r: (not r["依据章节"], order.get(r["类别"], 9), -r["出现次数"]))
    return rows


PHRASE = re.compile(r"“([^“”《》\n]{2,30})”")
SKIP_PHRASE = re.compile(r"^(十[一二三四五六七八九]五|[\d\s.%]+)$")


def scan_phrases(path, text, phrases):
    ext = os.path.splitext(path)[1].lower()
    fname = os.path.basename(path)
    inside_title = [(m.start(), m.end()) for m in TITLE.finditer(text)]
    for m in PHRASE.finditer(text):
        p = re.sub(r"\s+", "", m.group(1))
        if SKIP_PHRASE.match(p) or any(a <= m.start() < b for a, b in inside_title):
            continue
        r = phrases.setdefault(p, OrderedDict(提法=p, 出现次数=0, 出现在=OrderedDict(),
                                              首次位置="", 原文片段=""))
        r["出现次数"] += 1
        r["出现在"][fname] = r["出现在"].get(fname, 0) + 1
        if not r["首次位置"]:
            loc = locator(text, m.start(), ext)
            r["首次位置"] = fname + (f" {loc}" if loc else "")
            r["原文片段"] = snippet(text, m.start(), m.end())


def write_phrases(phrases, out, min_n):
    rows = []
    for r in phrases.values():
        if r["出现次数"] < min_n:
            continue
        r["材料数"] = len(r["出现在"])
        r["出现在"] = "；".join(f"{f}（{n}）" for f, n in r["出现在"].items())
        rows.append(r)
    rows.sort(key=lambda r: (-r["材料数"], -r["出现次数"]))
    head = ["提法", "材料数", "出现次数", "出现在", "首次位置", "原文片段",
            "来源", "层次", "能不能用", "用在哪里"]
    write_xlsx(rows, out, head, [28, 7, 8, 40, 26, 50, 30, 10, 10, 20], "提法")
    return rows


def write_xlsx(rows, out, head=None, widths=None, title="依据清单"):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    wb = Workbook()
    ws = wb.active
    ws.title = title
    head = head or ["名称", "文号", "类别", "层级", "列入依据章节", "出现次数", "出现在", "首次位置",
                    "原文片段", "现行状态", "本项目怎么用", "核对"]
    ws.append(head)
    for r in rows:
        ws.append([r.get(h, "") for h in head])
    for c in ws[1]:
        c.font = Font(bold=True)
    widths = widths or [40, 22, 10, 6, 50, 8, 40, 26, 50, 16, 30, 8]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    wb.save(out)


def ensure_openpyxl():
    import importlib.util as u
    import shutil
    if u.find_spec("openpyxl") or os.environ.get("PLAN_WORK_REEXEC"):
        return
    if not shutil.which("uv"):
        print("缺少 openpyxl；装 uv 后脚本会自动处理，或自行 pip install openpyxl。")
        sys.exit(1)
    os.environ["PLAN_WORK_REEXEC"] = "1"
    os.execvp("uv", ["uv", "run", "-q", "--no-project", "--with", "openpyxl",
                     "python", os.path.abspath(__file__), *sys.argv[1:]])


def main():
    ap = argparse.ArgumentParser(description="列出材料引用的上位规划、政策文件和标准")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out", help="输出 xlsx；默认 依据清单_自动提取_日期.xlsx")
    ap.add_argument("--min", type=int, default=1, help="只保留出现次数不少于此数的条目")
    ap.add_argument("--phrases", nargs="?", const="", default=None,
                    help="另外统计引号里的提法；可给输出文件名，默认 提法_自动提取_日期.xlsx")
    a = ap.parse_args()
    ensure_openpyxl()
    out = a.out or f"依据清单_自动提取_{datetime.date.today():%Y%m%d}.xlsx"

    files = []
    for p in a.paths:
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
                files += [os.path.join(root, n) for n in sorted(names) if not n.startswith((".", "~$"))]
        else:
            files.append(p)

    refs = OrderedDict()
    phrases = OrderedDict()
    done = []
    with tempfile.TemporaryDirectory() as tmp:
        for f in files:
            try:
                text, _ = extract(f, tmp)
            except Exception as e:
                print(f"跳过 {f}：{e}", file=sys.stderr)
                continue
            if text:
                scan(f, text, refs)
                if a.phrases is not None:
                    scan_phrases(f, text, phrases)
                done.append(f)

    rows = [r for r in finish(refs) if r["出现次数"] >= a.min]
    write_xlsx(rows, out)
    print(f"读了 {len(done)} 个文件，找到 {len(rows)} 条，写入 {out}\n")
    listed = [r for r in rows if r["依据章节"]]
    if listed:
        files = list(OrderedDict.fromkeys(f for r in listed for f in r["依据章节"]))
        print("依据章节里列了什么（各材料的写法；“—”表示这份材料的依据章节没有列）：\n")
        print("| 名称 | " + " | ".join(files) + " |\n|---|" + "---|" * len(files))
        for r in listed:
            print(f"| {r['名称'] or r['文号']} | " + " | ".join(r["依据章节"].get(f, "—") for f in files) + " |")
        print()
    print("全部引用：\n\n| 类别 | 名称 | 文号 | 次数 | 出现在 |\n|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['类别']} | {r['名称']} | {r['文号']} | {r['出现次数']} | {r['出现在']} |")

    if a.phrases is not None:
        pout = a.phrases or f"提法_自动提取_{datetime.date.today():%Y%m%d}.xlsx"
        prow = write_phrases(phrases, pout, a.min)
        print(f"\n提法 {len(prow)} 条，写入 {pout}。两份以上材料都用的：\n")
        print("| 提法 | 材料数 | 次数 | 出现在 |\n|---|---|---|---|")
        for r in prow:
            if r["材料数"] >= 2:
                print(f"| {r['提法']} | {r['材料数']} | {r['出现次数']} | {r['出现在']} |")


if __name__ == "__main__":
    sys.exit(main())
