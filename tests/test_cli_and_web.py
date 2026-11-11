"""Tests for the Salesforce CLI login fallback and web UI security."""

import base64
import json
import subprocess
from types import SimpleNamespace

import pytest

from cpq2rca import cli
from cpq2rca.connectors import salesforce
from cpq2rca.connectors.salesforce import SalesforceConnector
from cpq2rca.web import app as webapp


# ------------------------------------------------- 3. Salesforce CLI login
def fake_sf(monkeypatch, responses):
    """responses: {"subcommand words": (returncode, json_body)}; records calls."""
    calls = []

    def run(cmd, capture_output, text, timeout):
        key = " ".join(w for w in cmd[1:] if not w.startswith("--") and w != "my-org")
        calls.append(key)
        code, body = responses.get(key, (1, {"status": 1, "message": f"{key} is not a sf command"}))
        return SimpleNamespace(returncode=code, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr(salesforce.shutil, "which", lambda name: "/usr/bin/sf")
    monkeypatch.setattr(salesforce.subprocess, "run", run)
    return calls


ORG = (0, {"status": 0, "result": {"instanceUrl": "https://acme.my.salesforce.com"}})


def test_cli_login_uses_show_access_token(monkeypatch):
    fake_sf(monkeypatch, {"org display": ORG,
                          "org auth show-access-token": (0, {"status": 0, "result": {"accessToken": "00D!new"}})})
    conn = SalesforceConnector.from_sf_cli("my-org")
    assert conn.instance_url == "https://acme.my.salesforce.com"
    assert conn.s.headers["Authorization"] == "Bearer 00D!new"


def test_cli_login_falls_back_to_org_display_verbose(monkeypatch):
    # Older CLI: show-access-token does not exist, org display --verbose has the token.
    org = (0, {"status": 0, "result": {"instanceUrl": "https://acme.my.salesforce.com", "accessToken": "00D!old"}})
    calls = fake_sf(monkeypatch, {"org display": org})
    conn = SalesforceConnector.from_sf_cli("my-org")
    assert conn.s.headers["Authorization"] == "Bearer 00D!old"
    assert "org auth show-access-token" in calls


def test_cli_login_skips_redacted_token(monkeypatch):
    org = (0, {"status": 0, "result": {"instanceUrl": "https://x", "accessToken": "[REDACTED]"}})
    fake_sf(monkeypatch, {"org display": org,
                          "org auth show-access-token": (0, {"status": 0, "result": {"accessToken": "[REDACTED]"}})})
    with pytest.raises(RuntimeError, match="no usable access token"):
        SalesforceConnector.from_sf_cli("my-org")


def test_cli_login_unknown_alias_shows_cli_message(monkeypatch):
    fake_sf(monkeypatch, {"org display": (1, {"status": 1, "message": "No authorization information found for my-org."})})
    with pytest.raises(RuntimeError) as e:
        SalesforceConnector.from_sf_cli("my-org")
    assert "No authorization information found" in str(e.value)
    assert not isinstance(e.value, subprocess.CalledProcessError)


def test_cli_missing(monkeypatch):
    monkeypatch.setattr(salesforce.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="npm install"):
        SalesforceConnector.from_sf_cli("my-org")


# ------------------------------------------------------- 6. web UI security
@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "JOBS", {})
    monkeypatch.delenv("CPQ2RCA_WEB_PASSWORD", raising=False)
    return tmp_path


def basic(pw, user="anyone"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_password_required_when_set(runs):
    c = webapp.create_app(runs, password="s3cret").test_client()
    assert c.get("/").status_code == 401
    assert "Basic" in c.get("/").headers["WWW-Authenticate"]
    assert c.get("/", headers=basic("wrong")).status_code == 401
    assert c.get("/", headers=basic("s3cret")).status_code == 200


def test_password_from_env(runs, monkeypatch):
    monkeypatch.setenv("CPQ2RCA_WEB_PASSWORD", "env-pw")
    c = webapp.create_app(runs).test_client()
    assert c.get("/").status_code == 401
    assert c.get("/", headers=basic("env-pw")).status_code == 200


def test_no_password_by_default(runs):
    assert webapp.create_app(runs).test_client().get("/").status_code == 200


def test_cross_site_post_rejected(runs, monkeypatch):
    started = []
    monkeypatch.setattr(webapp.threading, "Thread", lambda target, daemon: SimpleNamespace(start=lambda: started.append(1)))
    c = webapp.create_app(runs).test_client()
    r = c.post("/runs", data={"name": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and not started
    r = c.post("/runs", data={"name": "x"}, headers={"Referer": "https://evil.example/page"})
    assert r.status_code == 403 and not started
    r = c.post("/runs", data={"name": "x"}, headers={"Origin": "http://localhost"})
    assert r.status_code == 302 and started


def test_run_names_never_overwrite(runs, monkeypatch):
    monkeypatch.setattr(webapp.threading, "Thread", lambda target, daemon: SimpleNamespace(start=lambda: None))
    (runs / "acme").mkdir()
    (runs / "acme" / "keep.txt").write_text("existing run")
    c = webapp.create_app(runs).test_client()
    loc1 = c.post("/runs", data={"name": "acme"}).headers["Location"]
    loc2 = c.post("/runs", data={"name": "acme"}).headers["Location"]
    assert loc1.endswith("/jobs/acme-2") and loc2.endswith("/jobs/acme-3")
    assert (runs / "acme" / "keep.txt").read_text() == "existing run"


def test_dot_names_cannot_escape_runs_folder(runs, monkeypatch):
    monkeypatch.setattr(webapp.threading, "Thread", lambda target, daemon: SimpleNamespace(start=lambda: None))
    assert webapp.clean_run_name("..") .startswith("run-")
    assert webapp.clean_run_name("../../etc") == "etc"
    c = webapp.create_app(runs).test_client()
    loc = c.post("/runs", data={"name": ".."}).headers["Location"]
    assert "/jobs/run-" in loc
    assert c.get("/run/..").status_code == 404
    assert c.get("/run/.hidden").status_code == 404


def test_job_status_survives_restart(runs, monkeypatch):
    monkeypatch.setattr(webapp.threading, "Thread", lambda target, daemon: SimpleNamespace(start=lambda: None))
    c = webapp.create_app(runs).test_client()
    c.post("/runs", data={"name": "slow"})
    assert json.loads((runs / "slow" / "job.json").read_text())["status"] == "running"

    webapp.JOBS.clear()  # simulate a server restart
    c = webapp.create_app(runs).test_client()
    page = c.get("/jobs/slow").get_data(as_text=True)
    assert "interrupted" in page
    assert "slow" in c.get("/").get_data(as_text=True)
    # job.json is internal and not offered as a download
    assert c.get("/run/slow/download/job.json").status_code == 404


def test_failed_job_saved(runs):
    c = webapp.create_app(runs).test_client()
    c.post("/runs", data={"name": "bad", "source": "folder", "folder": "/does/not/exist"})
    for t in webapp.threading.enumerate():
        if t is not webapp.threading.current_thread() and t.daemon:
            t.join(timeout=10)
    job = json.loads((runs / "bad" / "job.json").read_text())
    assert job["status"] == "failed" and "FileNotFoundError" in job["error"] and job["finished"]


def test_corrupt_analysis_does_not_break_home(runs):
    (runs / "broken").mkdir()
    (runs / "broken" / "analysis.json").write_text("{not json")
    r = webapp.create_app(runs).test_client().get("/")
    assert r.status_code == 200 and "unreadable" in r.get_data(as_text=True)


# --------------------------------------------------------- serve command
def test_serve_refuses_public_host_without_password(monkeypatch, tmp_path):
    monkeypatch.delenv("CPQ2RCA_WEB_PASSWORD", raising=False)
    with pytest.raises(SystemExit, match="Refusing"):
        cli.serve(str(tmp_path), "0.0.0.0", 8080)


def test_serve_public_host_with_password(monkeypatch, tmp_path):
    monkeypatch.setenv("CPQ2RCA_WEB_PASSWORD", "pw")
    ran = {}
    monkeypatch.setattr("flask.Flask.run", lambda self, host, port: ran.update(host=host, port=port))
    cli.serve(str(tmp_path), "0.0.0.0", 8080)
    assert ran == {"host": "0.0.0.0", "port": 8080}


def test_serve_localhost_without_password(monkeypatch, tmp_path):
    monkeypatch.delenv("CPQ2RCA_WEB_PASSWORD", raising=False)
    monkeypatch.setattr("flask.Flask.run", lambda self, host, port: None)
    cli.serve(str(tmp_path), "127.0.0.1", 8080)
