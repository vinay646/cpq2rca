"""Tests for per-object extract isolation, Salesforce API error handling,
multi-currency detection and the bundled logo."""

import json
import logging
import shutil
from pathlib import Path

import pytest
import requests

from cpq2rca.cli import SAMPLE, make_connector
from cpq2rca.connectors import salesforce
from cpq2rca.connectors.base import ApiLimitExceeded, ObjectNotFound
from cpq2rca.connectors.mock import FolderConnector
from cpq2rca.connectors.salesforce import SalesforceConnector
from cpq2rca.extract import extract
from cpq2rca.pipeline import analyse
from cpq2rca.report.branding import DEFAULT_LOGO, logo_path


# ------------------------------------------------------------------ helpers
class FakeResponse:
    def __init__(self, status=200, body=None, text="", headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text or json.dumps(self._body)
        self.headers = headers or {}

    def json(self):
        return self._body


def connector_with(responses, refresher=None):
    """SalesforceConnector whose session.get returns/raises the given items in order."""
    conn = SalesforceConnector("https://example.my.salesforce.com", "tok-1", token_refresher=refresher)
    seq = list(responses)
    seen = []

    def fake_get(url, params=None, timeout=None):
        seen.append(conn.s.headers["Authorization"])
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    conn.s.get = fake_get
    return conn, seen


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(salesforce.time, "sleep", lambda s: None)


# ------------------------------------------------------- 2. API error handling
def test_retries_on_429_then_succeeds():
    conn, _ = connector_with([FakeResponse(429, text="busy"), FakeResponse(200, {"ok": 1})])
    assert conn._get("u") == {"ok": 1}


def test_retries_on_connection_error():
    conn, _ = connector_with([requests.ConnectionError("reset"), requests.Timeout("slow"),
                              FakeResponse(200, {"ok": 1})])
    assert conn._get("u") == {"ok": 1}


def test_gives_up_after_max_attempts():
    conn, _ = connector_with([requests.ConnectionError("down")] * salesforce.MAX_ATTEMPTS)
    with pytest.raises(RuntimeError, match="unreachable"):
        conn._get("u")


def test_request_limit_exceeded_is_fatal_not_retried():
    body = '[{"message":"TotalRequests Limit exceeded.","errorCode":"REQUEST_LIMIT_EXCEEDED"}]'
    conn, seen = connector_with([FakeResponse(403, text=body)])
    with pytest.raises(ApiLimitExceeded):
        conn._get("u")
    assert len(seen) == 1


def test_other_403_is_plain_error():
    conn, _ = connector_with([FakeResponse(403, text='[{"errorCode":"INSUFFICIENT_ACCESS"}]')])
    with pytest.raises(RuntimeError) as e:
        conn._get("u")
    assert not isinstance(e.value, ApiLimitExceeded)


def test_401_refreshes_token_once():
    conn, seen = connector_with([FakeResponse(401, text="expired"), FakeResponse(200, {"ok": 1})],
                                refresher=lambda: "tok-2")
    assert conn._get("u") == {"ok": 1}
    assert seen == ["Bearer tok-1", "Bearer tok-2"]


def test_401_without_refresher_or_twice_fails():
    conn, _ = connector_with([FakeResponse(401, text="expired")])
    with pytest.raises(RuntimeError, match="401"):
        conn._get("u")
    conn, _ = connector_with([FakeResponse(401), FakeResponse(401)], refresher=lambda: "tok-2")
    with pytest.raises(RuntimeError, match="401"):
        conn._get("u")


def test_404_is_object_not_found():
    conn, _ = connector_with([FakeResponse(404)])
    with pytest.raises(ObjectNotFound):
        conn._get("u")


# ------------------------------------------------ 1. per-object isolation
class FlakyConnector(FolderConnector):
    def __init__(self, folder, fail=(), fail_count=(), limit_on=None):
        super().__init__(folder)
        self.fail, self.fail_count, self.limit_on = set(fail), set(fail_count), limit_on

    def query_all(self, sobject, fields, where=""):
        if sobject == self.limit_on:
            raise ApiLimitExceeded("limit")
        if sobject in self.fail:
            raise RuntimeError("Salesforce API error 400: INVALID_FIELD")
        return super().query_all(sobject, fields, where)

    def count(self, sobject):
        if sobject in self.fail_count:
            raise RuntimeError("Salesforce API error 500")
        return super().count(sobject)


def test_one_failed_object_does_not_stop_extract(tmp_path):
    conn = FlakyConnector(SAMPLE, fail={"SBQQ__PriceRule__c"}, fail_count={"SBQQ__Quote__c"})
    m = extract(conn, tmp_path)
    assert m["objects"]["SBQQ__PriceRule__c"]["status"] == "error"
    assert "INVALID_FIELD" in m["objects"]["SBQQ__PriceRule__c"]["error"]
    assert m["objects"]["SBQQ__ProductRule__c"]["status"] == "extracted"
    assert m["objects"]["SBQQ__PriceAction__c"]["status"] == "extracted"
    assert m["transactions"]["SBQQ__Quote__c"] is None
    assert m["transactions"]["SBQQ__Subscription__c"] == 6940

    r = analyse(tmp_path)
    titles = [f["title"] for f in r["findings"]]
    assert "SBQQ__PriceRule__c could not be extracted" in titles
    assert "SBQQ__Quote__c could not be counted" in titles
    assert not any(t.startswith("Price Rule") for t in (i["type"] for i in r["items"]))


def test_api_limit_stops_extract(tmp_path):
    with pytest.raises(ApiLimitExceeded):
        extract(FlakyConnector(SAMPLE, limit_on="SBQQ__ProductRule__c"), tmp_path)


# ------------------------------------------------------ 4. multi-currency
def test_single_currency_org_has_no_currency_noise(tmp_path):
    m = extract(FolderConnector(SAMPLE), tmp_path)
    assert all("CurrencyIsoCode" not in o.get("missing_fields", []) for o in m["objects"].values())
    r = analyse(tmp_path)
    assert not any("currenc" in f["title"].lower() for f in r["findings"])
    assert r["summary"]["currencies"] == []


def test_multi_currency_detected(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(SAMPLE, src)
    pbe = json.loads((src / "PricebookEntry.json").read_text())
    for r in pbe:
        r["CurrencyIsoCode"] = "USD"
    eur = [dict(r, Id=r["Id"][:-3] + "EUR", CurrencyIsoCode="EUR") for r in pbe[:3]]
    (src / "PricebookEntry.json").write_text(json.dumps(pbe + eur))
    org = json.loads((src / "_org.json").read_text())
    org["currencies"] = [{"iso_code": "USD", "corporate": True}, {"iso_code": "EUR"}, {"iso_code": "GBP"}]
    (src / "_org.json").write_text(json.dumps(org))

    run = tmp_path / "run"
    m = extract(FolderConnector(src), run)
    assert "CurrencyIsoCode" in m["objects"]["PricebookEntry"]["fields"]
    r = analyse(run)
    assert r["summary"]["currencies"] == ["EUR", "GBP", "USD"]
    titles = [f["title"] for f in r["findings"]]
    assert "Multi-currency org: 3 currencies (EUR, GBP, USD)" in titles
    assert "3 product price(s) defined in several currencies" in titles


# ------------------------------------------------------------- 5. logo
def test_logo_is_bundled_in_package():
    pkg = Path(__file__).parents[1] / "cpq2rca"
    assert DEFAULT_LOGO.parent == pkg / "report"
    assert logo_path() == DEFAULT_LOGO and DEFAULT_LOGO.exists()


def test_missing_logo_warns(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert logo_path(tmp_path / "nope.png", warn=True) is None
    assert "WITHOUT the company logo" in caplog.text


def test_sample_source_is_bundled():
    assert SAMPLE.is_dir() and SAMPLE.parent.parent.name == "cpq2rca"
    assert isinstance(make_connector("sample"), FolderConnector)
