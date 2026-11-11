"""Excel inventory and assessment. The 'Configuration Items' sheet carries empty
Phase 2 review columns so it can seed the mapping workbook."""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from ..catalog import BY_NAME
from .branding import BLUE, COMPANY, LIGHT, NAVY, logo_path

HDR_FILL = PatternFill("solid", fgColor=NAVY)
P2_FILL = PatternFill("solid", fgColor=BLUE)
HDR_FONT = Font(bold=True, color="FFFFFF")
THIN = Border(bottom=Side(style="thin", color="D5DCE4"))
WRAP = Alignment(wrap_text=True, vertical="top")


def _sheet(wb, title, headers, rows, widths, p2_from=None):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i)
        c.fill = P2_FILL if p2_from and i >= p2_from else HDR_FILL
        c.font = HDR_FONT
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for r in rows:
        ws.append(list(r))
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = WRAP
            c.border = THIN
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    ws.row_dimensions[1].height = 30
    return ws


def write_inventory(result: dict, path: str | Path, logo=None) -> Path:
    path = Path(path)
    s, items = result["summary"], result["items"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    lp = logo_path(logo, warn=True)
    if lp:
        img = XLImage(str(lp))
        img.height, img.width = 47, 200
        ws.add_image(img, "B2")
    ws["B6"] = "CPQ Inventory & Readiness Assessment"
    ws["B6"].font = Font(size=18, bold=True, color=NAVY)
    ws["B7"] = f"{s['org'].get('org_name', '')}  |  extracted {s.get('extracted_at', '')}  |  {COMPANY}"
    ws["B7"].font = Font(italic=True, color="5B6573")
    rows = [("Overall complexity", s["complexity_rating"]), ("Configuration items", s["items"]),
            ("CPQ records extracted", s["total_records"]), ("Products (active)", s["active_products"]),
            ("Indicative effort (hours)", s["effort_hours"]), ("Indicative effort (days)", s["effort_days"]),
            ("Direct", s["by_approach"]["Direct"]), ("Adapt", s["by_approach"]["Adapt"]),
            ("Redesign", s["by_approach"]["Redesign"]), ("Retire", s["by_approach"]["Retire"]),
            ("High findings", s["findings"].get("High", 0)), ("Medium findings", s["findings"].get("Medium", 0)),
            ("Low findings", s["findings"].get("Low", 0))]
    for i, (k, v) in enumerate(rows, 9):
        ws.cell(row=i, column=2, value=k).font = Font(bold=True, color=NAVY)
        ws.cell(row=i, column=3, value=v)
        ws.cell(row=i, column=2).fill = PatternFill("solid", fgColor=LIGHT)
    r0 = 9 + len(rows) + 1
    ws.cell(row=r0, column=2, value="Area").font = HDR_FONT
    for j, h in enumerate(["Items", "Direct", "Adapt", "Redesign", "Retire", "Effort (h)"], 3):
        ws.cell(row=r0, column=j, value=h).font = HDR_FONT
    for j in range(2, 9):
        ws.cell(row=r0, column=j).fill = HDR_FILL
    for i, (cat, d) in enumerate(s["by_category"].items(), r0 + 1):
        for j, v in enumerate([cat, d["items"], d["Direct"], d["Adapt"], d["Redesign"], d["Retire"],
                               round(d["hours"], 1)], 2):
            ws.cell(row=i, column=j, value=v)
    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 30
    for c in "CDEFGH":
        ws.column_dimensions[c].width = 13
    ws.sheet_view.showGridLines = False

    item_headers = ["Item ID", "Name", "Category", "Type", "Active", "Purpose", "When it runs", "Logic",
                    "Conditions", "Actions", "Applies to", "Details", "Example", "Depends on", "Used by",
                    "Custom fields", "Suggested RCA target", "Approach", "Complexity score", "Effort (h)",
                    "Migration notes", "Source object",
                    # Phase 2 review columns
                    "Approved RCA target", "Decision", "Owner", "Review status", "Comments"]
    by_id = {it["id"]: it["name"] for it in items}
    rows = [(it["id"], it["name"], it["category"], it["type"], "Yes" if it["active"] else "No", it["summary"],
             it["when"], it["logic"], "\n".join(it["conditions"]), "\n".join(it["actions"]),
             ", ".join(it["applies_to"]), "\n".join(it["details"][:60]), it["example"],
             "\n".join(f"{d['name']} ({d['relation']})" for d in it["dependencies"][:30]),
             ", ".join(by_id.get(u, u) for u in it.get("used_by", [])), ", ".join(it.get("custom_fields", [])),
             it["rca_target"], it["approach"], it["score"], round(it["effort_hours"], 1), "\n".join(it["notes"]),
             it["source"]["object"], "", "", "", "Proposed", "") for it in items]
    wsi = _sheet(wb, "Configuration Items", item_headers, rows,
                 [20, 32, 14, 22, 7, 45, 35, 25, 45, 45, 28, 40, 50, 35, 25, 25, 32, 11, 10, 9, 40, 24, 28, 18, 14, 14, 30],
                 p2_from=23)
    n = len(rows) + 1
    dv = DataValidation(type="list", formula1='"Proposed,In review,Approved,Rejected"', allow_blank=True)
    dv2 = DataValidation(type="list", formula1='"Direct,Adapt,Redesign,Retire"', allow_blank=True)
    wsi.add_data_validation(dv)
    wsi.add_data_validation(dv2)
    dv.add(f"Z2:Z{n}")
    dv2.add(f"X2:X{n}")
    fills = {"Direct": "D5F5E3", "Adapt": "FCF3CF", "Redesign": "FADBD8", "Retire": "E5E8E8"}
    for r in range(2, n + 1):
        a = wsi.cell(row=r, column=18)
        a.fill = PatternFill("solid", fgColor=fills.get(a.value, "FFFFFF"))

    _sheet(wb, "Findings", ["Severity", "Area", "Finding", "Detail", "Recommended action", "References"],
           [(f["severity"], f["area"], f["title"], f["detail"], f["action"], "\n".join(map(str, f["refs"][:30])))
            for f in result["findings"]], [10, 16, 45, 50, 40, 50])
    _sheet(wb, "Custom Fields", ["Field API name", "Label", "Referenced by (count)", "Referenced by", "RCA note"],
           [(f["field"], f["label"], f["count"], ", ".join(f["used_by"]), f["rca_note"]) for f in result["custom_fields"]],
           [32, 26, 12, 60, 50])
    schema = result.get("schema") or {}
    if schema.get("objects"):
        _sheet(wb, "Object Fields", ["Object", "Field API name", "Label", "Type", "Formula", "RCA object",
                                     "Used by CPQ logic", "Twin with"],
               [(o, f["name"], f["label"], f["type"], "Yes" if f["formula"] else "", f["rca_object"],
                 ", ".join(f["used_by"]), ", ".join(f["twins"]))
                for o, fs in schema["objects"].items() for f in fs],
               [24, 32, 26, 12, 8, 24, 40, 40])
        _sheet(wb, "Twin Fields", ["Field API name", "Label", "Copied from", "Copied to", "From type", "To type",
                                   "Types match"],
               [(t["field"], t["label"], t["from"], t["to"], t["from_type"], t["to_type"],
                 "Yes" if t["type_match"] else "No") for t in schema.get("twins", [])],
               [32, 26, 24, 24, 12, 12, 10])
    segs = s.get("transaction_segments") or {}
    _sheet(wb, "Record Counts", ["CPQ object", "API name", "Records", "Suggested RCA target"],
           [(BY_NAME[o].label if o in BY_NAME else o, o, c, BY_NAME[o].rca_target if o in BY_NAME else "")
            for o, c in s["record_counts"].items()]
           + [(BY_NAME[o].label, o, c, BY_NAME[o].rca_target) for o, c in (s.get("transactions") or {}).items()]
           + [(f"{BY_NAME[o].label} ({lbl.replace('_', ' ')})", o, c, BY_NAME[o].rca_target)
              for o, d in segs.items() for lbl, c in d.items() if c is not None],
           [28, 34, 10, 60])
    wb.save(path)
    return path
