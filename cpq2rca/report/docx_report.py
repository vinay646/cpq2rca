"""Business-readable CPQ Configuration Document (.docx)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT  # noqa: F401
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from ..catalog import BY_NAME, CATEGORY_ORDER
from .branding import BLUE, COMPANY, GREY, LIGHT, NAVY, logo_path

APPROACH_HELP = {
    "Direct": "maps to an RCA equivalent with little change",
    "Adapt": "has an RCA equivalent but needs restructuring",
    "Redesign": "no direct equivalent; needs a new RCA design",
    "Retire": "inactive or unused; confirm and leave out of scope",
}
SEV_COLOR = {"High": "C0392B", "Medium": "D68910", "Low": "5B6573"}


def _rgb(h):
    return RGBColor.from_string(h)


def _shade(cell, hex_color):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def _cell_margins(table, top=60, bottom=60, left=100, right=100):
    tblPr = table._tbl.tblPr
    mar = OxmlElement("w:tblCellMar")
    for k, v in (("top", top), ("bottom", bottom), ("left", left), ("right", right)):
        e = OxmlElement(f"w:{k}")
        e.set(qn("w:w"), str(v))
        e.set(qn("w:type"), "dxa")
        mar.append(e)
    tblPr.append(mar)


def _table(doc, headers, rows, widths=None, font=9):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    _cell_margins(t)
    for i, h in enumerate(headers):
        c = t.rows[0].cells[i]
        c.text = ""
        run = c.paragraphs[0].add_run(h)
        run.bold = True
        run.font.size = Pt(font)
        run.font.color.rgb = _rgb("FFFFFF")
        _shade(c, NAVY)
    for ri, row in enumerate(rows):
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = ""
            p = cells[i].paragraphs[0]
            if isinstance(v, tuple):  # (text, color, bold)
                run = p.add_run(str(v[0]))
                run.font.color.rgb = _rgb(v[1])
                run.bold = v[2] if len(v) > 2 else False
            else:
                run = p.add_run("" if v is None else str(v))
            run.font.size = Pt(font)
            if ri % 2:
                _shade(cells[i], "F5F8FB")
    if widths:
        t.autofit = False
        grid = t._tbl.tblGrid
        for i, gc in enumerate(grid.findall(qn("w:gridCol"))):
            if i < len(widths):
                gc.set(qn("w:w"), str(int(widths[i] * 567)))
        for row in t.rows:
            for i, w in enumerate(widths):
                row.cells[i].width = Cm(w)
    doc.add_paragraph()
    return t


def _field_page(paragraph):
    run = paragraph.add_run()
    for tag, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if tag:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), tag)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)
    run.font.size = Pt(8)


def _styles(doc):
    st = doc.styles
    st["Normal"].font.name = "Calibri"
    st["Normal"].font.size = Pt(10)
    st["Normal"].paragraph_format.space_after = Pt(4)
    for name, size, color in (("Title", 26, NAVY), ("Heading 1", 16, NAVY), ("Heading 2", 13, NAVY),
                              ("Heading 3", 11, BLUE)):
        s = st[name]
        s.font.name = "Calibri"
        s.font.size = Pt(size)
        s.font.color.rgb = _rgb(color)
        s.font.bold = True
        s.element.rPr.rFonts.set(qn("w:asciiTheme"), "")  # let Calibri win over theme
    st["Heading 3"].paragraph_format.space_before = Pt(10)


def _label_para(doc, label, text, italic=False):
    p = doc.add_paragraph()
    r = p.add_run(f"{label}: ")
    r.bold = True
    r.font.color.rgb = _rgb(NAVY)
    r2 = p.add_run(text)
    r2.italic = italic
    p.paragraph_format.space_after = Pt(2)
    return p


def _bullets(doc, lines, size=9.5):
    for line in lines:
        nested = line.startswith("    - ")
        p = doc.add_paragraph(style="List Bullet 2" if nested else "List Bullet")
        r = p.add_run(line.strip()[2:] if nested else line.strip())
        r.font.size = Pt(size)
        p.paragraph_format.space_after = Pt(0)


def _example_box(doc, text):
    t = doc.add_table(rows=1, cols=1)
    _cell_margins(t, 80, 80, 140, 140)
    c = t.rows[0].cells[0]
    _shade(c, LIGHT)
    c.text = ""
    r = c.paragraphs[0].add_run(text)
    r.italic = True
    r.font.size = Pt(9.5)
    r.font.color.rgb = _rgb("1F2D3D")


def _item_block(doc, it):
    doc.add_heading(it["name"], level=3)
    meta = doc.add_paragraph()
    status = "Active" if it["active"] else "Inactive"
    for i, (txt, color) in enumerate(((it["type"], GREY), (status, GREY),
                                      (f"Approach: {it['approach']}", BLUE),
                                      (f"Suggested RCA target: {it['rca_target']}", GREY))):
        if i:
            meta.add_run("  |  ").font.color.rgb = _rgb("B0B8C1")
        r = meta.add_run(txt)
        r.font.size = Pt(8.5)
        r.font.color.rgb = _rgb(color)
    _label_para(doc, "Purpose", it["summary"])
    if it["when"]:
        _label_para(doc, "When it runs", it["when"])
    if it["applies_to"]:
        _label_para(doc, "Applies to", ", ".join(it["applies_to"]))
    if it["conditions"]:
        _label_para(doc, "Conditions", it["logic"] or "")
        _bullets(doc, it["conditions"])
    if it["actions"]:
        _label_para(doc, "What it does", "")
        _bullets(doc, it["actions"])
    if it["details"]:
        _label_para(doc, "Details", "")
        _bullets(doc, it["details"][:40])
        if len(it["details"]) > 40:
            _bullets(doc, [f"... {len(it['details']) - 40} more (see Excel inventory)"])
    if it["example"]:
        _example_box(doc, it["example"])
    deps = [f"{d['name']} ({d['relation']})" for d in it["dependencies"]]
    if deps:
        _label_para(doc, "Depends on", "; ".join(deps[:15]) + (" ..." if len(deps) > 15 else ""))
    if it["notes"]:
        _label_para(doc, "Migration notes", "")
        _bullets(doc, it["notes"])
    src = doc.add_paragraph()
    r = src.add_run(f"Source: {it['source']['object']} {it['source']['id']}"
                    + (f" (+{len(it['source']['children'])} child records)" if it["source"]["children"] else ""))
    r.font.size = Pt(7.5)
    r.font.color.rgb = _rgb("8A95A3")


def write_configuration_document(result: dict, path: str | Path, logo=None) -> Path:
    path = Path(path)
    s, items = result["summary"], result["items"]
    logo = logo_path(logo, warn=True)
    org = s.get("org", {})
    doc = Document()
    _styles(doc)
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(2)
    sec.top_margin, sec.bottom_margin = Cm(2.2), Cm(2)
    sec.different_first_page_header_footer = True

    # header / footer (pages 2+)
    hp = sec.header.paragraphs[0]
    if logo:
        hp.add_run().add_picture(str(logo), height=Cm(0.75))
    hr = hp.add_run("\tCPQ Configuration Document")
    hr.font.size = Pt(8)
    hr.font.color.rgb = _rgb(GREY)
    fp = sec.footer.paragraphs[0]
    fr = fp.add_run(f"{COMPANY}  |  CPQ to RCA Migration  |  {org.get('org_name', '')}  |  Page ")
    fr.font.size = Pt(8)
    fr.font.color.rgb = _rgb(GREY)
    _field_page(fp)

    # ------------------------------------------------------------ cover page
    if logo:
        p = doc.add_paragraph()
        p.add_run().add_picture(str(logo), width=Cm(7))
    for _ in range(5):
        doc.add_paragraph()
    doc.add_paragraph("CPQ Configuration Document", style="Title")
    p = doc.add_paragraph()
    r = p.add_run("Phase 1 - Extract and explain  |  Salesforce CPQ to Revenue Cloud Advanced")
    r.font.size = Pt(12)
    r.font.color.rgb = _rgb(BLUE)
    doc.add_paragraph()
    extracted = s.get("extracted_at") or ""
    try:
        extracted = datetime.fromisoformat(extracted).strftime("%d %B %Y, %H:%M UTC")
    except ValueError:
        pass
    for k, v in (("Organisation", org.get("org_name", "-")), ("Org ID", org.get("org_id", "-")),
                 ("Environment", "Sandbox" if org.get("is_sandbox") else "Production" if org.get("is_sandbox") is False else "-"),
                 ("CPQ package", ", ".join(f"{p['name']} {p['version']}" for p in org.get("packages", [])) or "-"),
                 ("Currencies", ", ".join(s.get("currencies") or []) or "Single currency"),
                 ("Extracted", extracted), ("Prepared by", COMPANY)):
        _label_para(doc, k, str(v))
    doc.add_page_break()

    # ------------------------------------------------------ executive summary
    doc.add_heading("1. Executive summary", level=1)
    doc.add_paragraph(
        f"This document explains the Salesforce CPQ configuration extracted from {org.get('org_name', 'the source org')}. "
        f"It covers {s['items']} configuration items built from {s['total_records']:,} CPQ records, and shows "
        "what each one does, when it runs, a business example, and a suggested migration approach. Use it with the "
        "Excel inventory to agree Phase 2 mappings. RCA targets are suggestions to confirm during mapping.")
    _table(doc, ["Measure", "Value"], [
        ("Overall migration complexity", (s["complexity_rating"], {"High": "C0392B", "Medium": "D68910", "Low": "1E8449"}[s["complexity_rating"]], True)),
        ("Configuration items explained", s["items"]),
        ("Products (active / total)", f"{s['active_products']} / {s['products']}"),
        ("CPQ configuration records extracted", f"{s['total_records']:,}"),
        ("Indicative configuration effort", f"{s['effort_days']} person-days ({s['effort_hours']} h)"),
        ("Findings (high / medium / low)", f"{s['findings'].get('High', 0)} / {s['findings'].get('Medium', 0)} / {s['findings'].get('Low', 0)}"),
    ], widths=[8, 9])

    doc.add_heading("Migration approach by area", level=2)
    rows = [(cat, d["items"], d["Direct"], d["Adapt"], d["Redesign"], d["Retire"], f"{d['hours']:.0f}")
            for cat, d in s["by_category"].items()]
    ba = s["by_approach"]
    rows.append((("Total", NAVY, True), s["items"], ba["Direct"], ba["Adapt"], ba["Redesign"], ba["Retire"],
                 f"{s['effort_hours']:.0f}"))
    _table(doc, ["Area", "Items", "Direct", "Adapt", "Redesign", "Retire", "Effort (h)"], rows,
           widths=[4.5, 1.8, 1.8, 1.8, 2, 1.8, 2.3])
    for k, v in APPROACH_HELP.items():
        p = doc.add_paragraph()
        r = p.add_run(f"{k}: ")
        r.bold = True
        r.font.size = Pt(8.5)
        p.add_run(v).font.size = Pt(8.5)
        p.paragraph_format.space_after = Pt(0)
    p = doc.add_paragraph()
    r = p.add_run("Effort covers configuration rebuild only and excludes data migration, testing, "
                  "integration and project management. Calibrate the rates after the first migration.")
    r.italic = True
    r.font.size = Pt(8.5)

    tx = {k: v for k, v in (s.get("transactions") or {}).items() if v}
    if tx:
        doc.add_heading("Transactional data in scope for cut-over planning", level=2)
        segs = s.get("transaction_segments") or {}

        def _subset(k):
            parts = [f"{v:,} {lbl.replace('_', ' ')}" for lbl, v in (segs.get(k) or {}).items() if v is not None]
            return ", ".join(parts) or "-"

        _table(doc, ["Object", "Records", "Of which", "Suggested RCA target"],
               [(BY_NAME[k].label, f"{v:,}", _subset(k), BY_NAME[k].rca_target) for k, v in tx.items()],
               widths=[3.5, 2.2, 3.8, 7.5])

    # --------------------------------------------------------------- findings
    doc.add_heading("2. Key findings", level=1)
    if result["findings"]:
        _table(doc, ["Severity", "Area", "Finding", "Recommended action"],
               [((f["severity"], SEV_COLOR[f["severity"]], True), f["area"],
                 f["title"] + (f"\n{f['detail']}" if f["detail"] else ""), f["action"])
                for f in result["findings"]], widths=[1.8, 2.8, 7.6, 4.8], font=8.5)
    else:
        doc.add_paragraph("No issues detected.")

    # ----------------------------------------------------- config by category
    n = 3
    for cat in CATEGORY_ORDER:
        cat_items = [it for it in items if it["category"] == cat]
        if not cat_items:
            continue
        doc.add_page_break()
        doc.add_heading(f"{n}. {cat}", level=1)
        n += 1
        _table(doc, ["Item", "Type", "Status", "Approach"],
               [(it["name"], it["type"], "Active" if it["active"] else "Inactive", it["approach"])
                for it in cat_items], widths=[7, 5, 2, 3], font=8.5)
        for it in cat_items:
            _item_block(doc, it)

    # -------------------------------------------------------------- appendices
    doc.add_page_break()
    doc.add_heading("Appendix A. Custom fields referenced by CPQ logic", level=1)
    doc.add_paragraph("These fields must exist in RCA (on Quote / Quote Line Item or via the context definition) "
                      "for migrated rules to work.")
    if result["custom_fields"]:
        _table(doc, ["Field", "Used by"], [(f["field"], ", ".join(f["used_by"])) for f in result["custom_fields"]],
               widths=[5.5, 11.5], font=8.5)
    schema = result.get("schema") or {}
    if schema.get("objects"):
        doc.add_heading("Appendix B. Custom fields on quote, product and order objects", level=1)
        doc.add_paragraph("Custom fields found on each object. Fields on CPQ quote objects must be recreated on the "
                          "RCA object shown. Twin fields are copied automatically by CPQ; RCA has no such sync.")
        _table(doc, ["Object", "RCA object", "Custom fields", "Used by CPQ logic", "Twin fields"],
               [(BY_NAME[o].label if o in BY_NAME else o, fs[0]["rca_object"] if fs else o, len(fs),
                 sum(1 for f in fs if f["used_by"]), sum(1 for f in fs if f["twins"]))
                for o, fs in schema["objects"].items()], widths=[4, 4, 3, 3, 3], font=8.5)
        if schema.get("twins"):
            doc.add_heading("Twin fields", level=2)
            _table(doc, ["Field", "Copied from", "Copied to", "Types"],
                   [(t["field"], t["from"], t["to"],
                     t["from_type"] if t["type_match"] else f"{t['from_type']} -> {t['to_type']} (mismatch)")
                    for t in schema["twins"]], widths=[4.5, 4, 4.5, 4], font=8.5)
    doc.add_heading(f"Appendix {'C' if schema.get('objects') else 'B'}. Extracted record inventory", level=1)
    _table(doc, ["CPQ object", "API name", "Records", "Suggested RCA target"],
           [(BY_NAME[o].label if o in BY_NAME else o, o, c, BY_NAME[o].rca_target if o in BY_NAME else "")
            for o, c in s["record_counts"].items()], widths=[3.8, 5, 1.7, 6.5], font=8)
    doc.save(path)
    return path
