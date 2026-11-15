"""批量提取项目材料的文字，供盘点和通读。

用法：
  python3 -I extract_text.py 文件或目录 [...] [--out 输出目录]

每个文件输出一个同名 .txt 到 --out（默认 ./_提取文字），最后打印一张汇总表。
- docx/doc/rtf：macOS 用 textutil；docx 不可用时直接解析 XML。docx 有修订痕迹或批注时，
  正文后附“删除的文字”和“批注”两段，汇总表标出处数。
- pptx：逐页输出文字，标出隐藏页、版面外的对象和备注。
- pdf：pdftotext -layout，按页用分页符分开。
- xlsx：每张表的尺寸和前 20 行。xls/et 先用 soffice 转成 xlsx。
- txt/md/csv：原样复制。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}
TEXT_EXT = {".txt", ".md", ".csv"}


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def paras(el):
    out = []
    for p in el.iter(f"{{{NS['a']}}}p"):
        s = "".join(t.text or "" for t in p.iter(f"{{{NS['a']}}}t")).strip()
        if s:
            out.append(s)
    return out


def docx_revisions(path):
    """修订痕迹和批注：返回 (附加文字, 说明)。删掉的文字常是改过的提法和口径。"""
    if not path.lower().endswith(".docx"):
        return "", ""
    z = zipfile.ZipFile(path)
    W = NS["w"]
    root = ET.fromstring(z.read("word/document.xml"))
    dels, n_small = [], 0
    for para in root.iter(f"{{{W}}}p"):
        parts = ["".join(x.text or "" for x in d.iter(f"{{{W}}}delText")) for d in para.iter(f"{{{W}}}del")]
        parts = [t.strip() for t in parts if t.strip()]
        if not parts:
            continue
        if len(re.sub(r"[\W\d_]", "", "".join(parts))) < 3:   # 只删了标点、数字或一两个字
            n_small += 1
            continue
        dels.append(" … ".join(parts))
    n_ins = sum(1 for i in root.iter(f"{{{W}}}ins") if "".join(x.text or "" for x in i.iter(f"{{{W}}}t")).strip())
    comments = []
    if "word/comments.xml" in z.namelist():
        for c in ET.fromstring(z.read("word/comments.xml")).iter(f"{{{W}}}comment"):
            t = "".join(x.text or "" for x in c.iter(f"{{{W}}}t")).strip()
            if t:
                comments.append(f"{c.get(f'{{{W}}}author', '')}：{t}")
    if not (dels or n_ins or comments):
        return "", ""
    out = []
    if dels:
        out += ["", "===== 修订痕迹：删除的文字（按段落，正文是接受修订后的）"] + dels
    if comments:
        out += ["", "===== 批注"] + comments
    info = f"含修订：{len(dels)} 段有实质删除（另 {n_small} 段只删标点或个别字）、插入 {n_ins} 处" + (f"；批注 {len(comments)} 条" if comments else "")
    return "\n".join(out), info


def docx_text(path):
    rev, rinfo = docx_revisions(path) if path.lower().endswith(".docx") else ("", "")
    text, info = docx_body(path)
    return text + rev, "；".join(x for x in (info, rinfo) if x)


def docx_body(path):
    if shutil.which("textutil"):
        r = run(["textutil", "-convert", "txt", "-stdout", path])
        if r.returncode == 0:
            return r.stdout, ""
    if path.lower().endswith(".docx"):
        root = ET.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
        lines = []
        for p in root.iter(f"{{{NS['w']}}}p"):
            s = "".join(t.text or "" for t in p.iter(f"{{{NS['w']}}}t"))  # 不含 delText，即接受修订后
            if s.strip():
                lines.append(s)
        return "\n".join(lines), ""
    return soffice_txt(path), ""


def soffice_txt(path):
    if not shutil.which("soffice"):
        return "（无法读取：没有 textutil 或 soffice）"
    with tempfile.TemporaryDirectory() as d:
        run(["soffice", "--headless", "--convert-to", "txt:Text", "--outdir", d, path])
        for f in os.listdir(d):
            return open(os.path.join(d, f), encoding="utf-8", errors="ignore").read()
    return "（soffice 转换失败）"


def pptx_text(path):
    z = zipfile.ZipFile(path)
    pres = ET.fromstring(z.read("ppt/presentation.xml"))
    sz = pres.find("p:sldSz", NS)
    W, H = int(sz.get("cx")), int(sz.get("cy"))
    rels = ET.fromstring(z.read("ppt/_rels/presentation.xml.rels"))
    rid2target = {r.get("Id"): r.get("Target") for r in rels}
    order = [rid2target[s.get(f"{{{NS['r']}}}id")] for s in pres.find("p:sldIdLst", NS)]

    out, hidden, offc = [], 0, 0
    for i, target in enumerate(order, 1):
        name = "ppt/" + target.lstrip("/").replace("ppt/", "")
        root = ET.fromstring(z.read(name))
        is_hidden = root.get("show") == "0"
        hidden += is_hidden
        head = f"===== 第 {i} 页" + ("（隐藏页）" if is_hidden else "")
        inside, outside = [], []
        tree = root.find("p:cSld/p:spTree", NS)
        for shp in list(tree) if tree is not None else []:
            texts = paras(shp)
            if not texts:
                continue
            xfrm = next((x for x in (shp.find("p:spPr/a:xfrm", NS), shp.find("p:xfrm", NS),
                                     shp.find("p:grpSpPr/a:xfrm", NS)) if x is not None), None)
            off = xfrm.find("a:off", NS) if xfrm is not None else None
            ext = xfrm.find("a:ext", NS) if xfrm is not None else None
            out_of = False
            if off is not None and ext is not None and ext.get("cx") is not None:
                x, y = int(off.get("x")), int(off.get("y"))
                cx, cy = int(ext.get("cx")), int(ext.get("cy"))
                out_of = x + cx <= 0 or y + cy <= 0 or x >= W or y >= H
            (outside if out_of else inside).append(" / ".join(texts))
        out.append(head)
        out.extend(inside)
        if outside:
            offc += len(outside)
            out.append("--- 版面外（不是正文，可能是任务说明或素材）")
            out.extend(outside)
        rel_name = name.replace("slides/", "slides/_rels/") + ".rels"
        if rel_name in z.namelist():
            for r in ET.fromstring(z.read(rel_name)):
                if r.get("Type", "").endswith("/notesSlide"):
                    nname = os.path.normpath(os.path.join(os.path.dirname(name), r.get("Target")))
                    notes = [s for s in paras(ET.fromstring(z.read(nname))) if not re.fullmatch(r"\d+", s)]
                    if notes:
                        out.append("--- 备注")
                        out.extend(notes)
    info = f"{len(order)} 页，隐藏 {hidden} 页，版面外文字对象 {offc} 个"
    return "\n".join(out), info


def pdf_text(path):
    if not shutil.which("pdftotext"):
        return "（没有 pdftotext）", ""
    r = run(["pdftotext", "-layout", path, "-"])
    pages = r.stdout.count("\f") + 1 if r.stdout else 0
    note = "" if r.stdout.strip() else "，没有文字层（扫描件，需要识别）"
    return r.stdout, f"{pages} 页{note}"


def xlsx_text(path):
    try:
        import openpyxl
    except ImportError:
        return "（没有 openpyxl）", ""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        out.append(f"===== 表：{ws.title}（{len(rows)} 行）")
        for r in rows[:20]:
            vals = [str(v).replace("\n", " ") for v in r if v is not None]
            if vals:
                out.append(" | ".join(vals))
    return "\n".join(out), f"{len(wb.worksheets)} 张表"


def convert_to_xlsx(path, tmp):
    if not shutil.which("soffice"):
        return None
    run(["soffice", "--headless", "--convert-to", "xlsx", "--outdir", tmp, path])
    base = os.path.splitext(os.path.basename(path))[0] + ".xlsx"
    p = os.path.join(tmp, base)
    return p if os.path.exists(p) else None


def extract(path, tmp):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".docx", ".doc", ".rtf"):
        return docx_text(path)
    if ext == ".pptx":
        return pptx_text(path)
    if ext == ".pdf":
        return pdf_text(path)
    if ext == ".xlsx":
        return xlsx_text(path)
    if ext in (".xls", ".et"):
        conv = convert_to_xlsx(path, tmp)
        if not conv:
            return "（转换失败：需要 soffice）", ""
        text, info = xlsx_text(conv)
        return text, info + "（已转 xlsx）"
    if ext in TEXT_EXT:
        return open(path, encoding="utf-8", errors="ignore").read(), ""
    return None, ""


def main():
    ap = argparse.ArgumentParser(description="批量提取项目材料的文字")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out", default="_提取文字")
    a = ap.parse_args()

    files = []
    for p in a.paths:
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                files += [os.path.join(root, n) for n in sorted(names) if not n.startswith((".", "~$"))]
        else:
            files.append(p)

    os.makedirs(a.out, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for f in files:
            try:
                text, info = extract(f, tmp)
            except Exception as e:  # 单个文件失败不影响其他文件
                text, info = None, f"读取失败：{e}"
            if text is None:
                rows.append((f, "跳过", info or "不支持的类型"))
                continue
            name = os.path.basename(f) + ".txt"
            with open(os.path.join(a.out, name), "w", encoding="utf-8") as fh:
                fh.write(text)
            rows.append((f, f"{len(text)} 字", info))

    print(f"输出目录：{a.out}")
    print("| 文件 | 文字量 | 说明 |\n|---|---|---|")
    for f, n, info in rows:
        print(f"| {f} | {n} | {info} |")


if __name__ == "__main__":
    sys.exit(main())
