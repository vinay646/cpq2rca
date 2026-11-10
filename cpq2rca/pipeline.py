"""Orchestrates extract -> explain -> assess -> documents for a run folder."""

from __future__ import annotations

import json
from pathlib import Path

from . import assess, explain
from .extract import extract
from .model import Dataset


def analyse(run_dir: str | Path, ai: bool = False, progress=None) -> dict:
    run_dir = Path(run_dir)
    ds = Dataset(run_dir)
    items = explain.explain_all(ds)
    if ai:
        from .ai import enrich
        enrich(items, progress=progress)
    fnds = assess.findings(ds, items)
    result = {
        "summary": assess.summary(ds, items, fnds),
        "items": items,
        "findings": fnds,
        "custom_fields": assess.custom_fields(items),
        "graph": assess.graph(ds, items),
    }
    (run_dir / "analysis.json").write_text(json.dumps(result, indent=1, default=str))
    return result


def build_documents(run_dir: str | Path, result: dict | None = None, logo: str | Path | None = None) -> list[Path]:
    from .report.docx_report import write_configuration_document
    from .report.xlsx_report import write_inventory

    run_dir = Path(run_dir)
    if result is None:
        result = json.loads((run_dir / "analysis.json").read_text())
    return [write_configuration_document(result, run_dir / "CPQ_Configuration_Document.docx", logo),
            write_inventory(result, run_dir / "CPQ_Inventory_and_Assessment.xlsx", logo)]


def run_all(conn, run_dir: str | Path, ai: bool = False, logo=None, progress=None) -> dict:
    extract(conn, run_dir, progress=progress)
    result = analyse(run_dir, ai=ai, progress=progress)
    build_documents(run_dir, result, logo)
    return result
