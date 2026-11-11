"""Tests for tester mode: "Connect Salesforce org" (OAuth web server flow + PKCE)
and per-tester run isolation."""

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from cpq2rca import cli
from cpq2rca.web import app as webapp
from cpq2rca.web import oauth

CFG = oauth.OAuthConfig("CID", "SECRET", "https://cpq.example.com/oauth/callback", ())

STARTED = []  # job functions handed to threading.Thread

USERS = {  # access token -> (org id, user id, username)
    "at-ana": ("00D5g000000AAAA", "0055g000000ANA1", "ana@acme.com"),
    "at-bo": ("00D5g000000BBBB", "0055g000000BOB1", "bo@globex.com"),
}


class FakeSalesforce:
    """Stands in for login.salesforce.com: token, identity and revoke endpoints."""

    def __init__(self):
        self.posts, self.next_user, self.revoked = [], "at-ana", []

    def post(self, url, data=None, timeout=None):
        self.posts.append((url, dict(data or {})))
        if url.endswith("/revoke"):
            self.revoked.append(data["token"])
            return SimpleNamespace(status_code=200, json=lambda: {})
        if data["grant_type"] == "refresh_token":
            return SimpleNamespace(status_code=200, json=lambda: {"access_token": "at-refreshed"})
        if data.get("code") == "bad":
            return SimpleNamespace(status_code=400, json=lambda: {"error": "invalid_grant",
                                                                    "error_description": "expired authorization code"})
        tok = self.next_user
        org, user, _ = USERS[tok]
        host = urlsplit(url).hostname
        return SimpleNamespace(status_code=200, json=lambda: {
            "access_token": tok, "refresh_token": f"rt-{tok}",
            "instance_url": "https://acme.my.salesforce.com",
            "id": f"https://{host}/id/{org}AAA/{user}AAA"})

    def get(self, url, headers=None, timeout=None):
        tok = headers["Authorization"].split()[1]
        _, _, username = USERS[tok]
        return SimpleNamespace(status_code=200, json=lambda: {"username": username, "email": username,
                                                               "display_name": username.split("@")[0].title()})


@pytest.fixture
def sf(monkeypatch):
    fake = FakeSalesforce()
    monkeypatch.setattr(oauth.requests, "post", fake.post)
    monkeypatch.setattr(oauth.requests, "get", fake.get)
    return fake


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "JOBS", {})
    for v in ("CPQ2RCA_WEB_PASSWORD", "SF_OAUTH_CLIENT_ID", "CPQ2RCA_TESTER_AI"):
        monkeypatch.delenv(v, raising=False)
    # runs start in a background thread; record instead of running the pipeline
    STARTED.clear()
    monkeypatch.setattr(webapp.threading, "Thread",
                        lambda target, daemon: SimpleNamespace(start=lambda: STARTED.append(target)))
    return tmp_path


def make_app(runs, cfg=CFG, password="adminpw"):
    return webapp.create_app(runs, password=password, oauth_config=cfg)


def login(client, sf, user="at-ana", env="sandbox"):
    sf.next_user = user
    r = client.get(f"/connect?env={env}")
    assert r.status_code == 302
    q = parse_qs(urlsplit(r.headers["Location"]).query)
    return client.get(f"/oauth/callback?code=good&state={q['state'][0]}")


# ------------------------------------------------------------- login flow
def test_not_connected_sees_connect_page_only(runs):
    c = make_app(runs).test_client()
    page = c.get("/").get_data(as_text=True)
    assert "Connect your Salesforce org" in page and "Connect sandbox" in page
    assert "New run" not in page
    assert c.post("/runs", data={"source": "sample"}).status_code == 302  # bounced to home
    assert not STARTED


def test_connect_redirects_to_salesforce_with_pkce(runs, sf):
    c = make_app(runs).test_client()
    r = c.get("/connect?env=sandbox")
    url = urlsplit(r.headers["Location"])
    q = parse_qs(url.query)
    assert url.netloc == "test.salesforce.com" and url.path == "/services/oauth2/authorize"
    assert q["client_id"] == ["CID"] and q["redirect_uri"] == ["https://cpq.example.com/oauth/callback"]
    assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["api refresh_token"]
    with c.session_transaction() as s:
        verifier = s["oauth"]["verifier"]
        assert s["oauth"]["state"] == q["state"][0]
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == [expected]
    assert "verifier" not in r.headers["Location"] and verifier not in r.headers["Location"]


def test_full_login_and_token_exchange(runs, sf):
    c = make_app(runs).test_client()
    r = login(c, sf)
    assert r.status_code == 302
    url, data = sf.posts[-1]
    assert url == "https://test.salesforce.com/services/oauth2/token"
    assert data["grant_type"] == "authorization_code" and data["client_secret"] == "SECRET"
    assert data["code_verifier"] and data["redirect_uri"] == "https://cpq.example.com/oauth/callback"
    page = c.get("/").get_data(as_text=True)
    assert "Connected:" in page and "ana@acme.com" in page
    assert "My connected org (ana@acme.com)" in page
    with c.session_transaction() as s:  # tokens never go to the browser
        assert "at-ana" not in json.dumps(dict(s)) and "rt-at-ana" not in json.dumps(dict(s))


def test_state_mismatch_and_reuse_rejected(runs, sf):
    c = make_app(runs).test_client()
    c.get("/connect?env=production")
    r = c.get("/oauth/callback?code=good&state=forged", follow_redirects=True)
    assert "login link expired" in r.get_data(as_text=True)
    assert not sf.posts  # no token request was made
    # state is single use
    r = c.get("/connect?env=production")
    state = parse_qs(urlsplit(r.headers["Location"]).query)["state"][0]
    c.get(f"/oauth/callback?code=good&state={state}")
    c.post("/disconnect")
    r = c.get(f"/oauth/callback?code=good&state={state}", follow_redirects=True)
    assert "login link expired" in r.get_data(as_text=True)


def test_salesforce_error_shown(runs, sf):
    c = make_app(runs).test_client()
    r = c.get("/connect?env=sandbox")
    state = parse_qs(urlsplit(r.headers["Location"]).query)["state"][0]
    r = c.get(f"/oauth/callback?code=bad&state={state}", follow_redirects=True)
    assert "expired authorization code" in r.get_data(as_text=True)
    r = c.get("/oauth/callback?error=access_denied&error_description=end-user+denied", follow_redirects=True)
    assert "end-user denied" in r.get_data(as_text=True)


@pytest.mark.parametrize("domain,ok", [
    ("acme.my.salesforce.com", True),
    ("https://acme--uat.sandbox.my.salesforce.com/", True),
    ("evil.com", False),
    ("acme.my.salesforce.com.evil.com", False),
    ("login.salesforce.com@evil.com", False),
])
def test_custom_domain_validation(runs, sf, domain, ok):
    c = make_app(runs).test_client()
    r = c.get("/connect", query_string={"env": "custom", "domain": domain})
    host = urlsplit(r.headers["Location"]).netloc
    if ok:
        assert host.endswith(".my.salesforce.com")
    else:
        assert host in ("", "localhost") or "salesforce" not in host
        assert "My Domain" in c.get("/").get_data(as_text=True)


def test_allowlist(runs, sf):
    cfg = oauth.OAuthConfig("CID", None, "https://cpq.example.com/oauth/callback", ("@acme.com",))
    c = make_app(runs, cfg).test_client()
    r = login(c, sf, "at-bo")
    assert "not on the list of testers" in c.get(r.headers["Location"]).get_data(as_text=True)
    assert "rt-at-bo" in sf.revoked
    assert "Connected:" not in c.get("/").get_data(as_text=True)
    login(c, sf, "at-ana")
    assert "Connected:" in c.get("/").get_data(as_text=True)
    assert oauth.is_allowed(oauth.OAuthConfig("x", None, None, ("00d5g000000bbbbxyz",)),
                            {"org_id": "00D5g000000BBBB", "username": "z@z.com"})


def test_disconnect_revokes(runs, sf):
    c = make_app(runs).test_client()
    login(c, sf)
    c.post("/disconnect")
    assert "rt-at-ana" in sf.revoked
    assert "Connect your Salesforce org" in c.get("/").get_data(as_text=True)


# ---------------------------------------------------------- isolation
def start(client, name, source="sample"):
    return client.post("/runs", data={"name": name, "source": source})


_ANALYSIS = {}


def fake_finished_run(runs, name):
    """Give a run folder a real analysis (from the sample org) so its pages render."""
    if not _ANALYSIS:
        import tempfile

        from cpq2rca.cli import SAMPLE
        from cpq2rca.connectors.mock import FolderConnector
        from cpq2rca.extract import extract
        from cpq2rca.pipeline import analyse
        d = Path(tempfile.mkdtemp())
        extract(FolderConnector(SAMPLE), d)
        _ANALYSIS["json"] = json.dumps(analyse(d), default=str)
    (runs / name / "analysis.json").write_text(_ANALYSIS["json"])


def test_testers_only_see_their_own_runs(runs, sf):
    app = make_app(runs)
    ana, bo, anon = app.test_client(), app.test_client(), app.test_client()
    login(ana, sf, "at-ana")
    login(bo, sf, "at-bo")
    assert start(ana, "ana-run").headers["Location"].endswith("/jobs/ana-run")
    start(bo, "bo-run")
    owner = json.loads((runs / "ana-run" / "owner.json").read_text())
    assert owner["owner"] == "00D5g000000AAAA/0055g000000ANA1" and owner["username"] == "ana@acme.com"
    fake_finished_run(runs, "ana-run")
    fake_finished_run(runs, "bo-run")
    (runs / "cli-run").mkdir()  # run made from the command line: no owner
    fake_finished_run(runs, "cli-run")

    home = ana.get("/").get_data(as_text=True)
    assert "ana-run" in home and "bo-run" not in home and "cli-run" not in home
    for path in ("/run/bo-run", "/run/bo-run/items", "/run/bo-run/findings", "/run/bo-run/fields",
                 "/run/bo-run/download/analysis.json", "/run/bo-run/download/extract.zip", "/jobs/bo-run",
                 "/run/cli-run"):
        assert ana.get(path).status_code == 404, path
    assert ana.get("/run/ana-run").status_code == 200
    assert ana.get("/run/ana-run/download/owner.json").status_code == 404  # internal file
    assert bo.get("/run/ana-run").status_code == 404
    assert anon.get("/run/ana-run").status_code == 302  # not connected: back to connect page


def test_runs_follow_identity_across_reconnect(runs, sf):
    app = make_app(runs)
    c = app.test_client()
    login(c, sf, "at-ana")
    start(c, "keep")
    fake_finished_run(runs, "keep")
    c.post("/disconnect")
    login(c, sf, "at-ana")
    assert c.get("/run/keep").status_code == 200


def test_testers_cannot_use_server_sources_or_ai(runs, sf):
    c = make_app(runs).test_client()
    login(c, sf)
    for src, extra in (("folder", {"folder": "/etc"}), ("sf", {"alias": "admin-org"}),
                       ("token", {"instance_url": "https://x.my.salesforce.com", "access_token": "t"})):
        assert c.post("/runs", data={"name": "x", "source": src, **extra}).status_code == 403, src
    assert c.post("/runs", data={"name": "x", "source": "sample", "ai": "1"}).status_code == 403
    page = c.get("/").get_data(as_text=True)
    assert "Export folder on server" not in page and "Salesforce CLI alias" not in page
    assert "Polish explanations" not in page
    assert not STARTED


def test_tester_ai_can_be_enabled(runs, sf, monkeypatch):
    monkeypatch.setenv("CPQ2RCA_TESTER_AI", "true")
    c = make_app(runs).test_client()
    login(c, sf)
    assert "Polish explanations" in c.get("/").get_data(as_text=True)
    assert start(c, "x").status_code == 302


def test_connected_org_run_uses_tokens_and_refresh(runs, sf, monkeypatch):
    c = make_app(runs).test_client()
    login(c, sf)
    used = {}

    def fake_run_all(conn, run_dir, ai, progress):
        used["url"] = conn.instance_url
        used["auth"] = conn.s.headers["Authorization"]
        used["refreshed"] = conn.token_refresher()

    monkeypatch.setattr(webapp.pipeline, "run_all", fake_run_all)
    start(c, "live", source="org")
    STARTED[-1]()  # run the job inline
    assert used == {"url": "https://acme.my.salesforce.com", "auth": "Bearer at-ana", "refreshed": "at-refreshed"}
    url, data = sf.posts[-1]
    assert data["grant_type"] == "refresh_token" and data["refresh_token"] == "rt-at-ana"
    assert json.loads((runs / "live" / "job.json").read_text())["status"] == "done"


# --------------------------------------------------------------- admin
def test_admin_sees_everything(runs, sf):
    app = make_app(runs)
    ana, admin = app.test_client(), app.test_client()
    login(ana, sf)
    start(ana, "ana-run")
    fake_finished_run(runs, "ana-run")
    assert admin.post("/admin", data={"password": "nope"}).status_code == 200
    assert admin.get("/run/ana-run").status_code == 302
    admin.post("/admin", data={"password": "adminpw"})
    assert admin.get("/run/ana-run").status_code == 200
    page = admin.get("/").get_data(as_text=True)
    assert "ana-run" in page and "Export folder on server" in page
    admin.post("/admin/logout")
    assert admin.get("/run/ana-run").status_code == 302


def test_no_admin_page_without_password(runs):
    assert make_app(runs, password=None).test_client().get("/admin").status_code == 404


def test_tester_mode_off_by_default(runs):
    c = webapp.create_app(runs).test_client()
    assert c.get("/connect").status_code == 404
    assert "New run" in c.get("/").get_data(as_text=True)


# --------------------------------------------------------- oauth helpers
def test_exchange_rejects_non_salesforce_urls(monkeypatch):
    monkeypatch.setattr(oauth.requests, "post", lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {
        "access_token": "x", "instance_url": "https://evil.example", "id": "https://login.salesforce.com/id/a/b"}))
    with pytest.raises(oauth.OAuthError):
        oauth.exchange_code(CFG, "login.salesforce.com", "c", "https://cb", "v")


def test_serve_allows_public_host_in_tester_mode(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("CPQ2RCA_WEB_PASSWORD", raising=False)
    monkeypatch.delenv("CPQ2RCA_ALLOWED_USERS", raising=False)
    monkeypatch.setenv("SF_OAUTH_CLIENT_ID", "CID")
    monkeypatch.setattr("flask.Flask.run", lambda self, host, port: None)
    cli.serve(str(tmp_path), "0.0.0.0", 8080)
    err = capsys.readouterr().err
    assert "ANY Salesforce user can connect" in err and "Tester mode" in err
