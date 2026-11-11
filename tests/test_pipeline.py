import json
from pathlib import Path

import pytest

from cpq2rca.catalog import load_order
from cpq2rca.connectors.mock import FolderConnector
from cpq2rca.explain import _formula_example, _satisfying, analyse_qcp
from cpq2rca.extract import extract
from cpq2rca.pipeline import analyse, build_documents

SAMPLE = Path(__file__).parents[1] / "cpq2rca" / "sample_data" / "northwind"


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    if not SAMPLE.exists():
        import runpy
        runpy.run_path(str(SAMPLE.parent / "generate.py"), run_name="__main__")
    d = tmp_path_factory.mktemp("run")
    extract(FolderConnector(SAMPLE), d)
    return d, analyse(d)


def test_load_order_parents_first():
    order = load_order()
    for parent, child in [("Product2", "SBQQ__ProductOption__c"), ("SBQQ__ProductRule__c", "SBQQ__ErrorCondition__c"),
                          ("SBQQ__PriceRule__c", "SBQQ__PriceAction__c"), ("SBQQ__DiscountSchedule__c", "SBQQ__DiscountTier__c"),
                          ("SBQQ__SummaryVariable__c", "SBQQ__PriceCondition__c")]:
        assert order.index(parent) < order.index(child)


def test_extract_preserves_ids_and_counts(run):
    d, _ = run
    m = json.loads((d / "extract" / "manifest.json").read_text())
    src = json.loads((SAMPLE / "SBQQ__ProductRule__c.json").read_text())
    out = json.loads((d / "extract" / "SBQQ__ProductRule__c.json").read_text())
    assert [r["Id"] for r in src] == [r["Id"] for r in out]
    assert m["objects"]["SBQQ__ProductRule__c"]["count"] == len(src)
    assert m["transactions"]["SBQQ__Subscription__c"] == 6940


def test_every_rule_explained(run):
    _, r = run
    types = [i["type"] for i in r["items"]]
    assert sum(t.startswith("Product Rule") for t in types) == 9
    assert sum(t.startswith("Price Rule") for t in types) == 6
    for it in r["items"]:
        assert it["summary"] and it["approach"] in ("Direct", "Adapt", "Redesign", "Retire")


def test_explanations_content(run):
    _, r = run
    by = {i["name"]: i for i in r["items"]}
    assert "cannot be selected together" in by["BND-ENT support exclusive"]["summary"]
    assert "7,500" in by["Implementation fee scales with users"]["example"]
    assert by["Northwind QCP"]["approach"] == "Redesign"
    assert by["Block Legacy Reporting"]["approach"] == "Retire"
    assert by["Total Services Amount"]["approach"] == "Retire"
    slab = by["User Slab Pricing"]["example"]
    assert "50 at 0%" in slab and "150 at 8%" in slab


def test_findings(run):
    _, r = run
    titles = " ".join(f["title"] for f in r["findings"])
    assert "share evaluation order" in titles
    assert "inactive products" in titles
    assert "no conditions" in titles


def test_documents(run):
    d, r = run
    files = build_documents(d, r)
    assert all(f.exists() and f.stat().st_size > 5000 for f in files)


def test_helpers():
    assert _satisfying("1 AND (2 OR 3)", "Custom", 3) == [1, 2]
    assert _formula_example("MAX(5000, X__c * 25)", 300).startswith("7,500")
    assert _formula_example("import os", 1) == ""
    a = analyse_qcp("export function onInit(lines){ line.record['A__c'] = 1; }")
    assert a["hooks"] == ["onInit"] and a["writes"] == ["A__c"]


def test_web(run):
    from cpq2rca.web.app import create_app
    d, r = run
    app = create_app(d.parent)
    c = app.test_client()
    assert c.get("/").status_code == 200
    assert c.get(f"/run/{d.name}").status_code == 200
    assert c.get(f"/run/{d.name}/items?approach=Redesign").status_code == 200
    item = r["items"][0]["id"]
    assert c.get(f"/run/{d.name}/item/{item}").status_code == 200
    assert c.get(f"/run/{d.name}/findings").status_code == 200
    assert c.get(f"/run/{d.name}/download/extract.zip").status_code == 200
    assert c.get("/run/../etc").status_code == 404
