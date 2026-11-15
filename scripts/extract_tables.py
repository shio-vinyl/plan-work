"""把材料里的表格提成数据表：一张表一个 xlsx，并登记到数据表目录。

用法：
  python3 -I extract_tables.py 文件或目录 [...] [--out 数据表] [--date YYYYMMDD]
                               [--alias 原文件名=简称 ...] [--list] [--force]

支持：
- pptx：每页的原生表格；名称取该页标题。
- docx：每个表格；名称取表格前面的表题或段落。
- pdf：pdfplumber 自动识别的表格（需对照原文核对）；名称取表格上方一行。
- xlsx/xls/et：每张工作表一个文件（xls、et 先用 soffice 转换）。
- GIS（gpkg、shp、geojson、gdb 目录、kml）：每个图层的属性表，不带几何。
- 图片（png、jpg）：只输出 OCR 文字（tesseract chi_sim）到 .ocr.txt 供核对；表格要看图转录。

文件名：名称_来源_提取日期.xlsx。来源是来源文件的简称加位置（p28、表3、图层名）。
目录：--out 下的 00_数据表目录.xlsx，每张表一行；已登记的文件名不覆盖，除非 --force。
--list 只列出会提取哪些表、叫什么名字，不写文件。

缺 pdfplumber、pyogrio、openpyxl 时，如果装了 uv，会自动用 uv 临时环境重新运行。
"""
import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

def ensure_deps(files):
    """按要处理的文件类型检查依赖，缺了就用 uv 临时环境重新运行。"""
    import importlib.util as u
    exts = {os.path.splitext(f.rstrip("/"))[1].lower() for f in files}
    need = ["openpyxl"]
    if ".pdf" in exts:
        need.append("pdfplumber")
    if exts & GIS_EXT:
        need.append("pyogrio")
    missing = [m for m in need if not u.find_spec(m)]
    if not missing or os.environ.get("PLAN_WORK_REEXEC"):
        return
    if not shutil.which("uv"):
        print("缺少依赖：", ", ".join(missing), "；装 uv 后脚本会自动处理，或自行 pip install。")
        sys.exit(1)
    args = ["uv", "run", "-q", "--no-project"]
    for m in need:
        args += ["--with", m]
    args += ["python", os.path.abspath(__file__), *sys.argv[1:]]
    os.environ["PLAN_WORK_REEXEC"] = "1"
    os.execvp("uv", args)


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}
GIS_EXT = {".gpkg", ".shp", ".geojson", ".json", ".kml", ".gdb"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
CODE_COL = re.compile(r"编码|代码|编号|宗地号|地块号|图斑号|^id$|^ID$|code", re.I)
NUM = re.compile(r"^-?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")


# ---------- 通用 ----------

def clean_name(s, limit=24):
    s = re.sub(r"\s+", "", s or "")
    s = re.sub(r"^表\s*\d+[-—.．]?\d*[：:．.、]?", "", s)          # 表题编号“表1：”
    s = re.sub(r"[（(]?单位[：:].*$", "", s)                      # “（单位：万㎡）”
    s = re.sub(r'[\\/:*?"<>|（）()【】\[\]“”"‘’《》]', "", s)
    s = re.sub(r"^[一二三四五六七八九十\d]+[、.．_]", "", s)
    return s[:limit]


def title_like(texts):
    """从候选文字里挑一个像标题的：短、不是纯数字。"""
    for t in texts:
        t = t.strip()
        if 3 <= len(t) <= 40 and not re.fullmatch(r"[\d\s.%/-]+|(附件|附表|表)\s*\d+", t):
            return t
    return ""


def short_source(path, aliases):
    base = os.path.basename(path.rstrip("/"))
    if base in aliases:
        return aliases[base]
    stem = os.path.splitext(base)[0]
    stem = re.sub(r"[（(]\d+[)）]$", "", stem)
    stem = re.sub(r"[_\-\s]*(v\d+|\d{8}|\d{4}|\d{6})$", "", stem, flags=re.I)
    stem = re.sub(r"^[\d]+[_\-.、]", "", stem)
    return clean_name(stem, 16)


def tidy(rows):
    """去掉空行和末尾的空列，补齐列数。"""
    rows = [["" if c is None else str(c).replace("\n", " ").strip() for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max((max((i for i, c in enumerate(r) if c), default=-1) + 1) for r in rows)
    return [(r + [""] * width)[:width] for r in rows]


def to_value(v, is_code):
    if is_code or not isinstance(v, str):
        return v
    t = v.strip()
    if NUM.match(t) and not (len(t) > 1 and t[0] == "0" and t[1] != "."):
        n = float(t.replace(",", ""))
        return int(n) if n.is_integer() and "." not in t else n
    return v


def write_xlsx(path, rows, keep_types=False):
    """keep_types：GIS 属性表保留字段原类型，不把文本转成数字。"""
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "数据"
    header = [str(h) for h in rows[0]]
    code = [bool(CODE_COL.search(h)) for h in header]
    for i, r in enumerate(rows):
        if i == 0 or keep_types:
            vals = [v.item() if hasattr(v, "item") else v for v in r]
        else:
            vals = [to_value(v, code[j] if j < len(code) else False) for j, v in enumerate(r)]
        ws.append(vals)
    for c in ws[1]:
        c.font = Font(bold=True)
    for j, h in enumerate(header, 1):
        width = max([len(str(h))] + [len(str(r[j - 1])) for r in rows[1:50] if j - 1 < len(r)])
        ws.column_dimensions[openpyxl.utils.get_column_letter(j)].width = min(max(width * 1.6, 6), 40)
        if code[j - 1]:
            for cell in ws.iter_cols(min_col=j, max_col=j, min_row=2):
                for c in cell:
                    c.number_format = "@"
    ws.freeze_panes = "A2"
    wb.save(path)


# ---------- PPT ----------

def pptx_tables(path):
    z = zipfile.ZipFile(path)
    pres = ET.fromstring(z.read("ppt/presentation.xml"))
    rels = ET.fromstring(z.read("ppt/_rels/presentation.xml.rels"))
    rid = {r.get("Id"): r.get("Target") for r in rels}
    for n, s in enumerate(pres.find("p:sldIdLst", NS), 1):
        name = "ppt/" + rid[s.get(f"{{{NS['r']}}}id")].lstrip("/").replace("ppt/", "")
        root = ET.fromstring(z.read(name))
        title, cands = "", []
        for sp in root.iter(f"{{{NS['p']}}}sp"):
            text = "".join(t.text or "" for t in sp.iter(f"{{{NS['a']}}}t")).strip()
            if not text:
                continue
            ph = sp.find("p:nvSpPr/p:nvPr/p:ph", NS)
            if ph is not None and ph.get("type") in ("title", "ctrTitle"):
                title = text
                break
            off = sp.find("p:spPr/a:xfrm/a:off", NS)
            if off is not None:
                cands.append((int(off.get("y")), text))
        if not title:   # 没有标题占位符时，取页面最上方像标题的文字
            title = title_like(t for _, t in sorted(cands))
        tables = list(root.iter(f"{{{NS['a']}}}tbl"))
        for k, tbl in enumerate(tables, 1):
            rows, merged = [], False
            for tr in tbl.findall("a:tr", NS):
                row = []
                for tc in tr.findall("a:tc", NS):
                    if tc.get("hMerge") or tc.get("vMerge"):
                        merged = True
                        row.append("")
                        continue
                    if tc.get("gridSpan") or tc.get("rowSpan"):
                        merged = True
                    paras = ["".join(t.text or "" for t in p.iter(f"{{{NS['a']}}}t")) for p in tc.iter(f"{{{NS['a']}}}p")]
                    row.append(" ".join(x for x in paras if x))
                rows.append(row)
            pos = f"p{n}" + (f"-{k}" if len(tables) > 1 else "")
            note = "有合并单元格，表头可能要整理" if merged else ""
            if root.get("show") == "0":
                note = (note + "；隐藏页").strip("；")
            yield dict(name=title or f"第{n}页表格", pos=pos, rows=rows, how="PPT表格", note=note)


# ---------- Word ----------

def docx_tables(path):
    body = ET.fromstring(zipfile.ZipFile(path).read("word/document.xml")).find("w:body", NS)
    w = NS["w"]
    recent, n = [], 0
    for el in list(body):
        if el.tag == f"{{{w}}}p":
            t = "".join(x.text or "" for x in el.iter(f"{{{w}}}t")).strip()
            if t:
                recent = (recent + [t])[-3:]
        elif el.tag == f"{{{w}}}tbl":
            n += 1
            rows, merged = [], False
            for tr in el.findall("w:tr", NS):
                row = []
                for tc in tr.findall("w:tc", NS):
                    pr = tc.find("w:tcPr", NS)
                    span, vm = 1, None
                    if pr is not None:
                        gs = pr.find("w:gridSpan", NS)
                        span = int(gs.get(f"{{{w}}}val")) if gs is not None else 1
                        vm = pr.find("w:vMerge", NS)
                    text = " ".join("".join(x.text or "" for x in p.iter(f"{{{w}}}t")) for p in tc.findall("w:p", NS)).strip()
                    if vm is not None and vm.get(f"{{{w}}}val") != "restart":
                        text, merged = "", True
                    if span > 1:
                        merged = True
                    row += [text] + [""] * (span - 1)
                rows.append(row)
            cap = next((t for t in reversed(recent) if re.match(r"表\s*\d", t)), "") \
                or title_like(t for t in reversed(recent) if len(t) <= 30)
            yield dict(name=cap or f"表{n}", pos=f"表{n}", rows=rows, how="Word表格",
                       note="有合并单元格，表头可能要整理" if merged else "")
            recent = []


# ---------- PDF ----------

def pdf_tables(path):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        for n, page in enumerate(pdf.pages, 1):
            found = page.find_tables()
            for k, t in enumerate(found, 1):
                top = t.bbox[1]
                above = page.crop((0, max(0, top - 50), page.width, top)).extract_text() or "" if top > 2 else ""
                lines = [x for x in above.split("\n") if x.strip()]
                yield dict(name=lines[-1] if lines else f"第{n}页表格", pos=f"p{n}" + (f"-{k}" if len(found) > 1 else ""),
                           rows=t.extract(), how="PDF自动识别", note="自动识别，要对照原文核对")
            if not page.extract_text():
                yield dict(skip=f"p{n} 没有文字层（扫描页），表格要看图转录")


# ---------- Excel ----------

def xlsx_tables(path, tmp):
    import openpyxl
    src = path
    if path.lower().endswith((".xls", ".et")):
        if not shutil.which("soffice"):
            yield dict(skip="需要 soffice 转换 xls/et")
            return
        subprocess.run(["soffice", "--headless", "--convert-to", "xlsx", "--outdir", tmp, path], capture_output=True)
        src = os.path.join(tmp, os.path.splitext(os.path.basename(path))[0] + ".xlsx")
    wb = openpyxl.load_workbook(src, data_only=True)
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        note = "有合并单元格，表头可能要整理" if ws.merged_cells.ranges else ""
        name = ws.title
        if re.fullmatch(r"(Sheet|工作表|表)\d*", name, re.I):   # 表名没意义时，取表内开头的标题行
            heads = [str(next(c for c in r if c is not None)) for r in rows[:4]
                     if sum(c is not None for c in r) == 1]
            name = title_like(heads) or name
        yield dict(name=name, pos="", rows=rows, how="Excel分表", note=note)


# ---------- GIS ----------

def gis_tables(path):
    import pyogrio
    from pyogrio.raw import read as raw_read
    for layer, geom in pyogrio.list_layers(path):
        meta, _, _, cols = raw_read(path, layer=layer, read_geometry=False)
        fields = list(meta["fields"])
        n = len(cols[0]) if len(cols) else 0
        data = [[None if (v is None or v != v) else v for v in (c[i] for c in cols)] for i in range(n)]
        crs = meta.get("crs") or "无坐标系"
        yield dict(name=re.sub(r"[_\-]?\d{8}$", "", layer), pos="", loc=layer, rows=[fields] + data, how="GIS属性表",
                   note=f"{geom or '无几何'}，{crs}，不含几何", raw=True)


# ---------- 图片 ----------

def image_ocr(path, out):
    if not shutil.which("tesseract"):
        return "没有 tesseract"
    txt = os.path.join(out, os.path.basename(path) + ".ocr")
    subprocess.run(["tesseract", path, txt, "-l", "chi_sim+eng", "--psm", "6"], capture_output=True)
    return f"OCR 文字已输出到 {txt}.txt；表格要看图转录，提取方式写“图片转录”"


# ---------- 主流程 ----------

def collect(paths):
    files = []
    for p in paths:
        p = p.rstrip("/")
        if os.path.isdir(p) and not p.lower().endswith(".gdb"):
            for root, dirs, names in os.walk(p):
                keep = []
                for d in dirs:
                    if d.lower().endswith(".gdb"):
                        files.append(os.path.join(root, d))
                    elif not d.startswith("."):
                        keep.append(d)
                dirs[:] = keep
                files += [os.path.join(root, n) for n in sorted(names) if not n.startswith((".", "~$"))]
        else:
            files.append(p)
    return files


def read_catalog(path):
    import openpyxl
    if not os.path.exists(path):
        return None, set()
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    return wb, {r[0] for r in ws.iter_rows(min_row=2, values_only=True) if r and r[0]}


HEAD = ["文件名", "名称", "来源文件", "位置", "提取日期", "提取方式", "行数", "列数", "需要注意", "核对"]


def main():
    ap = argparse.ArgumentParser(description="把材料里的表格提成数据表")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out", default="数据表")
    ap.add_argument("--date", default=datetime.date.today().strftime("%Y%m%d"))
    ap.add_argument("--alias", action="append", default=[], help="原文件名=简称，可多次")
    ap.add_argument("--name", action="append", default=[],
                    help="来源加位置=名称，改掉自动取的名称，如 汇报稿p12=在建项目清单；可多次")
    ap.add_argument("--list", action="store_true", help="只列出，不写文件")
    ap.add_argument("--force", action="store_true", help="覆盖已登记的同名文件")
    a = ap.parse_args()
    aliases = dict(x.split("=", 1) for x in a.alias)
    names = dict(x.split("=", 1) for x in a.name)
    files = collect(a.paths)
    seen = set()
    ensure_deps(files)

    os.makedirs(a.out, exist_ok=True)
    cat_path = os.path.join(a.out, "00_数据表目录.xlsx")
    wb, known = (None, set()) if a.list else read_catalog(cat_path)
    new_rows, report = [], []

    with tempfile.TemporaryDirectory() as tmp:
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            try:
                if ext == ".pptx":
                    items = pptx_tables(f)
                elif ext == ".docx":
                    items = docx_tables(f)
                elif ext == ".pdf":
                    items = pdf_tables(f)
                elif ext in (".xlsx", ".xls", ".et"):
                    items = xlsx_tables(f, tmp)
                elif ext in GIS_EXT:
                    items = gis_tables(f)
                elif ext in IMG_EXT:
                    report.append((f, "", image_ocr(f, a.out) if not a.list else "图片：要看图转录"))
                    continue
                else:
                    continue
                for it in items:
                    if "skip" in it:
                        report.append((f, "", it["skip"]))
                        continue
                    rows = it["rows"] if it.get("raw") else tidy(it["rows"])
                    min_rows = 3 if it["how"] == "PDF自动识别" else 2   # PDF 识别常带出一两行的碎片
                    if len(rows) < min_rows or len(rows[0]) < 2:
                        continue
                    src = short_source(f, aliases) + it["pos"]
                    name = clean_name(names.get(src, it["name"])) or "表"
                    fname = f"{name}_{src}_{a.date}.xlsx"
                    k = 2
                    while fname in seen:
                        fname = f"{name}{k}_{src}_{a.date}.xlsx"
                        k += 1
                    seen.add(fname)
                    it["name"] = name
                    if fname in known and not a.force:
                        report.append((f, fname, "目录里已有，跳过（要覆盖加 --force）"))
                        continue
                    if not a.list:
                        write_xlsx(os.path.join(a.out, fname), rows, keep_types=it.get("raw", False))
                        new_rows.append([fname, clean_name(it["name"], 60), os.path.basename(f.rstrip("/")),
                                         it.get("loc") or it["pos"] or it["name"], a.date, it["how"], len(rows) - 1, len(rows[0]),
                                         it["note"], ""])
                        known.add(fname)
                    report.append((f, fname, f"{len(rows) - 1} 行 × {len(rows[0])} 列；{it['how']}；{it['note']}".rstrip("；")))
            except Exception as e:  # 单个文件失败不影响其他文件
                report.append((f, "", f"读取失败：{e}"))

    if new_rows:
        import openpyxl
        from openpyxl.styles import Font
        if wb is None:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "目录"
            ws.append(HEAD)
            for c in ws[1]:
                c.font = Font(bold=True)
            for col, w in zip("ABCDEFGHIJ", [44, 24, 40, 10, 10, 12, 6, 6, 30, 8]):
                ws.column_dimensions[col].width = w
            ws.freeze_panes = "A2"
        ws = wb.active
        for r in new_rows:
            ws.append(r)
        wb.save(cat_path)

    print(("只列出，未写文件" if a.list else f"输出目录：{a.out}，目录：{cat_path}，新增 {len(new_rows)} 张"))
    print("| 来源 | 数据表 | 说明 |\n|---|---|---|")
    for f, n, s in report:
        print(f"| {os.path.basename(f)} | {n} | {s} |")


if __name__ == "__main__":
    main()
