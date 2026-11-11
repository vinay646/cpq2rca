"""Tests for the coverage additions: search filters, quote terms, translations,
custom/twin fields, automation (flows, Apex triggers, validation rules) and
active vs. total transaction counts."""

import json
import shutil

import pytest
from openpyxl import load_workbook

from cpq2rca.cli import SAMPLE
from cpq2rca.connectors.mock import FolderConnector
from cpq2rca.connectors.salesforce import SalesforceConnector
from cpq2rca.extract import extract
from cpq2rca.pipeline import analyse, build_documents


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    d = tmp_path_factory.mktemp("cov")
    m = extract(FolderConnector(SAMPLE), d)
    return d, m, analyse(d)


def items_of(r, typ):
    return {i["name"]: i for i in r["items"] if i["type"].startswith(typ)}


def titles(r):
    return [f["title"] for f in r["findings"]]


# ------------------------------------------------------------ new objects
def test_search_filters(run):
    _, _, r = run
    sf = items_of(r, "Search Filter")
    assert "Hidden filter" in sf["Active products only"]["summary"]
    partner = sf["Partner-eligible products"]
    assert "Apply Partner Discount" in partner["summary"]
    assert partner["dependencies"][0]["name"] == "Apply Partner Discount"
    assert "Partner_Tier__c" in partner["custom_fields"]


def test_quote_terms(run):
    _, _, r = run
    qt = items_of(r, "Quote Term")
    assert "on every quote" in qt["QT-0001"]["summary"] and "Locked" in qt["QT-0001"]["summary"]
    assert qt["QT-0002"]["conditions"] == ["summary variable 'Hardware Units' is greater than 0"]
    assert qt["QT-0002"]["approach"] == "Adapt"
    assert qt["QT-0003"]["approach"] == "Retire"  # Draft status
    assert any("Hardware ships" in d for d in qt["QT-0002"]["details"])


def test_localizations_grouped_by_language(run):
    _, _, r = run
    loc = items_of(r, "Localization")
    assert set(loc) == {"Translations: de", "Translations: fr"}
    assert "3 products" in loc["Translations: de"]["summary"]
    assert len(loc["Translations: de"]["source"]["children"]) == 3


# ----------------------------------------------------- active vs. total
def test_transaction_segments(run):
    _, m, r = run
    assert m["transaction_segments"]["SBQQ__Subscription__c"] == {"active": 3870}
    assert m["transaction_segments"]["SBQQ__Quote__c"] == {"last_12_months": 1380}
    f = next(f for f in r["findings"] if f["area"] == "Transactions")
    assert f["title"] == "5,490 active subscriptions/assets in flight (of 9,150 in total)"
    assert "Contract: 455 active of 812" in f["refs"]


def test_segments_missing_falls_back_to_total(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(SAMPLE, src)
    counts = json.loads((src / "_counts.json").read_text())
    (src / "_counts.json").write_text(json.dumps({k: v for k, v in counts.items() if "." not in k}))
    (src / "_automation.json").unlink()
    run = tmp_path / "run"
    m = extract(FolderConnector(src), run)
    assert m["transaction_segments"]["SBQQ__Subscription__c"] == {"active": None}
    assert m["automation"] == {"status": "not_available"}
    r = analyse(run)
    assert "9,150 subscriptions/assets in flight" in titles(r)
    assert "Flows, Apex triggers and validation rules were not inventoried" in titles(r)
    assert not any(i["category"] == "Automation" for i in r["items"])


# ------------------------------------------------------ custom / twin fields
def test_schema_and_twins(run):
    d, m, r = run
    assert m["schema"]["objects"]["SBQQ__Quote__c"] == 4
    assert m["schema"]["objects"]["Order"] == 1
    twins = {(t["field"], t["from"], t["to"]) for t in r["schema"]["twins"]}
    assert ("Deployment_Region__c", "Product2", "SBQQ__QuoteLine__c") in twins
    assert ("Deployment_Region__c", "SBQQ__QuoteLine__c", "OpportunityLineItem") in twins
    assert ("Strategic_Tier__c", "SBQQ__Quote__c", "Opportunity") in twins
    assert ("Requires_Shipping__c", "SBQQ__QuoteLine__c", "OrderItem") in twins
    quote = {f["name"]: f for f in r["schema"]["objects"]["SBQQ__Quote__c"]}
    assert quote["Strategic_Tier__c"]["rca_object"] == "Quote"
    assert quote["Strategic_Tier__c"]["used_by"]  # referenced by CPQ logic (QCP / rules)
    assert quote["Margin_Percent__c"]["formula"]
    t = titles(r)
    assert "9 twin field pair(s) kept in sync by CPQ" in t
    assert "1 twin field pair(s) with different field types" in t
    assert "9 custom field(s) on CPQ quote objects to recreate in RCA" in t


# ------------------------------------------------------------- automation
def test_automation_items(run):
    _, m, r = run
    assert m["automation"]["counts"] == {"apex_triggers": 2, "validation_rules": 3, "flows": 2}
    auto = {i["name"]: i for i in r["items"] if i["category"] == "Automation"}
    assert auto["QuoteLineTrigger"]["approach"] == "Redesign"
    assert any("QuoteLineItem" in n for n in auto["QuoteLineTrigger"]["notes"])
    assert any("Old API version" in n for n in auto["QuoteLineTrigger"]["notes"])
    assert auto["OpportunityTrigger"]["approach"] == "Adapt"
    assert auto["Region_Required_For_Hardware"]["approach"] == "Adapt"
    assert auto["Close_Date_Not_In_Past"]["summary"].startswith("Stops an Opportunity")
    assert auto["Old_Discount_Cap"]["approach"] == "Retire"
    assert auto["Order Activation Process"]["approach"] == "Redesign"  # Process Builder
    assert any("Process Builder is retired" in n for n in auto["Order Activation Process"]["notes"])
    f = next(f for f in r["findings"] if f["title"].endswith("on CPQ objects must be rebuilt"))
    assert f["severity"] == "High" and len(f["refs"]) == 3


# ---------------------------------------------------- Salesforce automation
class FakeOrg(SalesforceConnector):
    """Answers describe and query calls for the automation inventory."""

    def __init__(self, fail=()):
        super().__init__("https://acme.my.salesforce.com", "tok")
        self.fail = set(fail)
        self.queries = []

    def _get(self, url, params=None):
        if url.endswith("/describe"):
            obj = url.split("/sobjects/")[1].split("/")[0]
            fields = {
                "ApexTrigger": ["Id", "Name", "TableEnumOrId", "Status", "NamespacePrefix",
                                "LengthWithoutComments", "ApiVersion", "UsageBeforeUpdate"],
                "ValidationRule": ["Id", "ValidationName", "Active", "ErrorMessage", "EntityDefinitionId",
                                   "NamespacePrefix"],
                "FlowDefinitionView": ["DurableId", "ApiName", "Label", "ProcessType", "TriggerType",
                                       "TriggerObjectOrEventId", "TriggerObjectOrEventLabel", "IsActive",
                                       "NamespacePrefix"],
            }[obj]
            return {"fields": [{"name": f} for f in fields]}
        q = params["q"]
        self.queries.append((url, q))
        table = q.split(" FROM ")[1].split()[0]
        if table in self.fail:
            raise RuntimeError("Salesforce API error 400: sObject type not supported")
        recs = {
            "EntityDefinition": [
                {"DurableId": "01I5g000000AbCdEAF", "QualifiedApiName": "SBQQ__Quote__c", "Label": "Quote"},
                {"DurableId": "01I5g000000XyZwEAF", "QualifiedApiName": "SBQQ__QuoteLine__c", "Label": "Quote Line"},
                {"DurableId": "Opportunity", "QualifiedApiName": "Opportunity", "Label": "Opportunity"},
            ],
            "ApexTrigger": [
                # custom object referenced by its 15-character id
                {"Id": "01q1", "Name": "QuoteTrigger", "TableEnumOrId": "01I5g000000AbCd", "Status": "Active",
                 "NamespacePrefix": None, "LengthWithoutComments": 900, "ApiVersion": 58.0, "UsageBeforeUpdate": True},
                {"Id": "01q2", "Name": "PkgTrigger", "TableEnumOrId": "01I5g000000AbCdEAF", "Status": "Active",
                 "NamespacePrefix": "SBQQ", "LengthWithoutComments": 1, "ApiVersion": 58.0},
                {"Id": "01q3", "Name": "CaseTrigger", "TableEnumOrId": "Case", "Status": "Active",
                 "NamespacePrefix": None, "LengthWithoutComments": 1, "ApiVersion": 58.0},
            ],
            "ValidationRule": [
                {"Id": "03d1", "ValidationName": "Need_Region", "Active": True, "ErrorMessage": "Region!",
                 "EntityDefinitionId": "01I5g000000XyZwEAF", "NamespacePrefix": None},
                {"Id": "03d2", "ValidationName": "Opp_Rule", "Active": False, "ErrorMessage": "x",
                 "EntityDefinitionId": "Opportunity", "NamespacePrefix": None},
                {"Id": "03d3", "ValidationName": "Account_Rule", "Active": True, "ErrorMessage": "x",
                 "EntityDefinitionId": "Account", "NamespacePrefix": None},
            ],
            "FlowDefinitionView": [
                {"DurableId": "300a", "ApiName": "Quote_Flow", "Label": "Quote Flow", "ProcessType": "AutoLaunchedFlow",
                 "TriggerType": "RecordAfterSave", "TriggerObjectOrEventId": None,
                 "TriggerObjectOrEventLabel": "Quote", "IsActive": True, "NamespacePrefix": None},
                {"DurableId": "300b", "ApiName": "Screen", "Label": "Screen", "ProcessType": "Flow",
                 "TriggerType": None, "TriggerObjectOrEventId": None, "TriggerObjectOrEventLabel": None,
                 "IsActive": True, "NamespacePrefix": None},
            ],
        }[table]
        return {"records": [dict(r, attributes={}) for r in recs], "done": True, "totalSize": len(recs)}


def test_salesforce_automation_inventory():
    org = FakeOrg()
    a = org.automation(["SBQQ__Quote__c", "SBQQ__QuoteLine__c", "Opportunity"])
    assert a["errors"] == []
    assert [(t["name"], t["object"], t["events"]) for t in a["apex_triggers"]] == \
        [("QuoteTrigger", "SBQQ__Quote__c", ["BeforeUpdate"])]
    assert [(v["name"], v["object"], v["active"]) for v in a["validation_rules"]] == \
        [("Need_Region", "SBQQ__QuoteLine__c", True), ("Opp_Rule", "Opportunity", False)]
    assert [(f["api_name"], f["object"]) for f in a["flows"]] == [("Quote_Flow", "SBQQ__Quote__c")]
    tooling = [q for u, q in org.queries if "/tooling/" in u]
    assert any("FROM ApexTrigger" in q for q in tooling) and any("FROM ValidationRule" in q for q in tooling)
    assert not any("FROM FlowDefinitionView" in q for q in tooling)


def test_salesforce_automation_partial_failure():
    a = FakeOrg(fail={"ValidationRule"}).automation(["SBQQ__Quote__c", "SBQQ__QuoteLine__c", "Opportunity"])
    assert len(a["apex_triggers"]) == 1 and len(a["flows"]) == 1
    assert a["validation_rules"] == []
    assert len(a["errors"]) == 1 and a["errors"][0].startswith("Validation rules:")


def test_partial_automation_reported(tmp_path, monkeypatch):
    conn = FolderConnector(SAMPLE)
    orig = conn.automation

    def partial(objects):
        data = orig(objects)
        data["errors"] = ["Flows: RuntimeError: no access"]
        return data

    monkeypatch.setattr(conn, "automation", partial)
    m = extract(conn, tmp_path)
    assert m["automation"]["status"] == "partial"
    assert "Automation inventory is incomplete" in titles(analyse(tmp_path))


def test_salesforce_count_with_where():
    org = SalesforceConnector("https://acme.my.salesforce.com", "tok")
    seen = []
    org._get = lambda url, params=None: seen.append(params["q"]) or {"totalSize": 7}
    assert org.count("Contract", "Status = 'Activated'") == 7
    assert seen == ["SELECT COUNT() FROM Contract WHERE Status = 'Activated'"]


# ------------------------------------------------------ reports and web UI
def test_reports_include_new_sections(run):
    d, _, r = run
    docx, xlsx = build_documents(d, r)
    wb = load_workbook(xlsx)
    assert {"Object Fields", "Twin Fields"} <= set(wb.sheetnames)
    counts = [row[0] for row in wb["Record Counts"].iter_rows(values_only=True)]
    assert "Subscription (active)" in counts
    from docx import Document
    text = "\n".join(p.text for p in Document(docx).paragraphs)
    assert "Appendix B. Custom fields on quote, product and order objects" in text
    assert "Automation" in text


def test_web_shows_new_data(run):
    from cpq2rca.web.app import create_app
    d, _, r = run
    c = create_app(d.parent).test_client()
    page = c.get(f"/run/{d.name}/fields").get_data(as_text=True)
    assert "Twin with" in page and "Deployment_Region__c" in page
    trig = next(i for i in r["items"] if i["name"] == "QuoteLineTrigger")
    assert c.get(f"/run/{d.name}/item/{trig['id']}").status_code == 200
    assert "Automation" in c.get(f"/run/{d.name}/items?category=Automation").get_data(as_text=True)
