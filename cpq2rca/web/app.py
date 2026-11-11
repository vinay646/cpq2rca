"""Flask web UI: start runs, browse explanations, findings and downloads.

Security:
  * Password protection (HTTP Basic auth) is switched on when a password is
    given or CPQ2RCA_WEB_PASSWORD is set. `cpq2rca serve` refuses to listen
    on anything but localhost without one, because runs contain client data.
  * POSTs from other sites are rejected (Origin/Referer check) so a page in
    the user's browser cannot start runs behind their back.
  * Run names are unique: starting a run never overwrites an existing one.
  * Job status is saved in each run folder (job.json) so it survives restarts.

Tester mode (SF_OAUTH_CLIENT_ID set, see web/oauth.py):
  * Testers click "Connect Salesforce org" and log in on Salesforce's own page.
    Tokens stay in server memory; the browser only holds a signed session id.
  * Each run records its owner (Salesforce org + user). Testers only see and
    open their own runs; everything else answers 404.
  * Testers can run against their connected org or the sample org only - not
    server folders, server CLI aliases or pasted tokens.
  * CPQ2RCA_WEB_PASSWORD becomes the admin password (/admin), which sees all runs.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, session, url_for

from .. import __version__, pipeline
from ..catalog import BY_NAME, CATEGORY_ORDER
from ..report.branding import COMPANY, logo_path
from . import oauth

log = logging.getLogger(__name__)

JOBS: dict[str, dict] = {}
JOB_FILE = "job.json"
OWNER_FILE = "owner.json"
INTERNAL_FILES = {JOB_FILE, OWNER_FILE}
_LOCK = threading.Lock()
_NAME_RE = re.compile(r"[\w-][\w.-]{0,63}")  # no leading dot: blocks "." / ".." / hidden folders


def clean_run_name(raw: str | None) -> str:
    name = re.sub(r"[^\w.-]", "-", raw or "").lstrip(".-")[:64]
    return name or datetime.now().strftime("run-%Y%m%d-%H%M%S")


def create_app(runs_dir="runs", password: str | None = None,
               oauth_config: "oauth.OAuthConfig | None" = None) -> Flask:
    runs = Path(runs_dir).resolve()
    runs.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__)
    app.jinja_env.globals.update(company=COMPANY, version=__version__)
    password = password or os.environ.get("CPQ2RCA_WEB_PASSWORD") or None
    cfg = oauth_config or oauth.OAuthConfig.from_env()
    tester_mode = cfg is not None
    app.config["PASSWORD_PROTECTED"] = bool(password)
    app.config["TESTER_MODE"] = tester_mode
    app.config["TESTER_AI"] = os.environ.get("CPQ2RCA_TESTER_AI", "").lower() in ("1", "true", "yes")

    secret = os.environ.get("CPQ2RCA_SECRET_KEY")
    if not secret:
        secret = secrets.token_hex(32)
        if tester_mode:
            log.warning("CPQ2RCA_SECRET_KEY is not set: testers must reconnect after every server restart.")
    app.secret_key = secret
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=bool(cfg and (cfg.callback_url or "").startswith("https://")))

    # Connected orgs, server side only: {connection id: tokens + identity}
    connections: dict[str, dict] = {}

    def viewer() -> dict | None:
        cid = session.get("conn")
        return connections.get(cid) if cid else None

    def is_admin() -> bool:
        return not tester_mode or bool(session.get("admin"))

    def owner_of(name: str) -> str | None:
        f = runs / name / OWNER_FILE
        try:
            return json.loads(f.read_text()).get("owner") if f.exists() else None
        except ValueError:
            return None

    def visible(name: str) -> bool:
        if is_admin():
            return True
        v = viewer()
        return bool(v) and owner_of(name) == oauth.owner_key(v)

    @app.context_processor
    def identity():
        return {"tester_mode": tester_mode, "viewer": viewer(), "is_admin": is_admin(),
                "admin_login": tester_mode and bool(password)}

    # ------------------------------------------------------------ security
    @app.before_request
    def require_password():
        if not password or tester_mode:  # in tester mode the password is the admin login
            return None
        auth = request.authorization
        given = (auth.password or "") if auth else ""
        if not hmac.compare_digest(given.encode(), password.encode()):
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="cpq2rca", charset="UTF-8"'})
        return None

    PUBLIC = {"home", "logo", "static", "connect", "oauth_callback", "admin_login"}

    @app.before_request
    def require_identity():
        if not tester_mode or request.endpoint in PUBLIC or is_admin() or viewer():
            return None
        return redirect(url_for("home"))

    @app.before_request
    def same_origin_posts_only():
        if request.method != "POST":
            return None
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if source and urlsplit(source).netloc != request.host:
            abort(403)
        return None

    # ------------------------------------------------------------ jobs
    def save_job(name: str) -> None:
        job = JOBS.get(name)
        d = runs / name
        if job and d.is_dir():
            (d / JOB_FILE).write_text(json.dumps(job, indent=1))

    def read_job(name: str) -> dict | None:
        if name in JOBS:
            return JOBS[name]
        f = runs / name / JOB_FILE
        if _NAME_RE.fullmatch(name) and f.exists():
            try:
                return json.loads(f.read_text())
            except ValueError:
                return None
        return None

    # A job still marked "running" on disk was cut off by a server restart.
    for f in runs.glob(f"*/{JOB_FILE}"):
        try:
            j = json.loads(f.read_text())
        except ValueError:
            continue
        if j.get("status") == "running":
            j["status"] = "interrupted"
            j["error"] = "The server stopped before this run finished. Start it again."
            f.write_text(json.dumps(j, indent=1))
        if j.get("status") != "done":
            JOBS.setdefault(f.parent.name, j)

    def reserve_run(base: str) -> str:
        """Create a new, empty run folder and return its name. Never reuses an existing one."""
        with _LOCK:
            name, i = base, 2
            while True:
                try:
                    (runs / name).mkdir()
                    return name
                except FileExistsError:
                    name = f"{base[:58]}-{i}"
                    i += 1

    def run_path(name: str) -> Path:
        if not _NAME_RE.fullmatch(name):
            abort(404)
        p = runs / name
        if not p.is_dir() or not visible(name):
            abort(404)  # 404, not 403: do not reveal other testers' run names
        return p

    def load(name: str) -> dict:
        p = run_path(name) / "analysis.json"
        if not p.exists():
            abort(404)
        return json.loads(p.read_text())

    @app.route("/logo.png")
    def logo():
        lp = logo_path()
        return send_file(lp) if lp else ("", 404)

    @app.route("/")
    def home():
        items, broken = [], []
        if tester_mode and not is_admin() and not viewer():
            return render_template("home.html", runs=[], jobs={}, broken=[])
        for d in sorted(runs.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            a = d / "analysis.json"
            if d.is_dir() and a.exists() and visible(d.name):
                try:
                    s = json.loads(a.read_text())["summary"]
                    items.append({"name": d.name, "org": s["org"].get("org_name", ""),
                                  "when": s.get("extracted_at", ""), "items": s["items"],
                                  "rating": s["complexity_rating"], "days": s["effort_days"]})
                except (ValueError, KeyError, TypeError, AttributeError) as e:
                    log.warning("Run %s has an unreadable analysis.json: %s", d.name, e)
                    broken.append(d.name)
        jobs = {n: j for n, j in JOBS.items() if (runs / n).is_dir() and visible(n)}
        return render_template("home.html", runs=items, jobs=jobs, broken=broken)

    @app.post("/runs")
    def start_run():
        from ..cli import make_connector

        kind = request.form.get("source", "sample")
        me = viewer()
        if tester_mode and not is_admin():
            # Testers may only use their own connected org or the sample org: the
            # other sources read server folders, server CLI logins or pasted tokens.
            if kind not in ("org", "sample"):
                abort(403)
            if request.form.get("ai") and not app.config["TESTER_AI"]:
                abort(403)
        if kind == "org" and not me:
            flash("Connect a Salesforce org first.")
            return redirect(url_for("home"))
        name = reserve_run(clean_run_name(request.form.get("name")))
        if tester_mode and me:
            (runs / name / OWNER_FILE).write_text(json.dumps(
                {"owner": oauth.owner_key(me), "username": me.get("username"),
                 "org_id": me.get("org_id"), "created": datetime.now().isoformat(timespec="seconds")}, indent=1))
        if kind == "org":
            source = "org"
        elif kind == "sample":
            source = "sample"
        elif kind == "folder":
            source = "folder:" + request.form.get("folder", "")
        elif kind == "sf":
            source = "sf:" + request.form.get("alias", "")
        else:
            source = None
        JOBS[name] = {"status": "running", "log": [], "started": datetime.now().isoformat(timespec="seconds")}
        save_job(name)

        def work():
            job = JOBS[name]
            try:
                if source == "org":
                    from ..connectors.salesforce import SalesforceConnector
                    conn = SalesforceConnector(me["instance_url"], me["access_token"],
                                               token_refresher=lambda: oauth.refresh(cfg, me))
                elif source:
                    conn = make_connector(source)
                else:
                    from ..connectors.salesforce import SalesforceConnector
                    conn = SalesforceConnector(request_form["instance_url"], request_form["access_token"])
                pipeline.run_all(conn, runs / name, ai=bool(request_form.get("ai")),
                                 progress=lambda m: job["log"].append(m))
                job["status"] = "done"
            except Exception as e:  # noqa: BLE001
                job["status"] = "failed"
                job["error"] = f"{e.__class__.__name__}: {e}"
                job["log"].append(traceback.format_exc(limit=3))
            finally:
                job["finished"] = datetime.now().isoformat(timespec="seconds")
                save_job(name)

        request_form = dict(request.form)
        threading.Thread(target=work, daemon=True).start()
        return redirect(url_for("job", name=name))

    @app.route("/jobs/<name>")
    def job(name):
        if not _NAME_RE.fullmatch(name) or not visible(name):
            abort(404)
        j = read_job(name)
        if j and j["status"] == "done":
            return redirect(url_for("dashboard", name=name))
        return render_template("job.html", name=name, job=j)

    @app.route("/run/<name>")
    def dashboard(name):
        r = load(name)
        return render_template("dashboard.html", name=name, r=r, s=r["summary"], BY_NAME=BY_NAME,
                               files=[f.name for f in run_path(name).iterdir() if f.suffix in (".docx", ".xlsx")])

    @app.route("/run/<name>/items")
    def items(name):
        r = load(name)
        cat, appr, q = request.args.get("category"), request.args.get("approach"), (request.args.get("q") or "").lower()
        rows = [it for it in r["items"]
                if (not cat or it["category"] == cat) and (not appr or it["approach"] == appr)
                and (not q or q in json.dumps([it["name"], it["summary"], it["conditions"], it["actions"]]).lower())]
        cats = [c for c in CATEGORY_ORDER if any(it["category"] == c for it in r["items"])]
        return render_template("items.html", name=name, rows=rows, cats=cats, cat=cat, appr=appr, q=q, s=r["summary"])

    @app.route("/run/<name>/item/<path:item_id>")
    def item(name, item_id):
        r = load(name)
        by_id = {it["id"]: it for it in r["items"]}
        it = by_id.get(item_id) or abort(404)
        ext = run_path(name) / "extract"
        raw = []
        ids = [it["source"]["id"]] + it["source"]["children"]
        objs = {it["source"]["object"]} | {o for o in BY_NAME}
        wanted = set(ids)
        for o in objs:
            f = ext / f"{o}.json"
            if wanted and f.exists():
                for rec in json.loads(f.read_text()):
                    if rec["Id"] in wanted:
                        raw.append((o, rec))
                        wanted.discard(rec["Id"])
        return render_template("item.html", name=name, it=it, by_id=by_id, raw=raw, s=r["summary"])

    @app.route("/run/<name>/findings")
    def findings(name):
        r = load(name)
        return render_template("findings.html", name=name, r=r, s=r["summary"])

    @app.route("/run/<name>/fields")
    def fields(name):
        r = load(name)
        return render_template("fields.html", name=name, r=r, s=r["summary"])

    @app.route("/run/<name>/download/<path:fname>")
    def download(name, fname):
        base = run_path(name)
        allowed = {f.name: f for f in base.iterdir()
                   if f.suffix in (".docx", ".xlsx", ".json") and f.name not in INTERNAL_FILES}
        if fname == "extract.zip":
            import io
            import zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for f in (base / "extract").glob("*.json"):
                    z.write(f, f"extract/{f.name}")
            buf.seek(0)
            return send_file(buf, as_attachment=True, download_name=f"{name}-extract.zip")
        if fname not in allowed:
            abort(404)
        return send_file(allowed[fname], as_attachment=True)

    # ------------------------------------------------- Salesforce connect (tester mode)
    def callback_url() -> str:
        return cfg.callback_url or url_for("oauth_callback", _external=True)

    @app.route("/connect")
    def connect():
        if not tester_mode:
            abort(404)
        try:
            host = oauth.login_host(request.args.get("env", "production"), request.args.get("domain"))
        except oauth.OAuthError as e:
            flash(str(e))
            return redirect(url_for("home"))
        verifier, challenge = oauth.pkce_pair()
        state = secrets.token_urlsafe(32)
        session["oauth"] = {"state": state, "verifier": verifier, "host": host}
        return redirect(oauth.authorize_url(cfg, host, callback_url(), state, challenge))

    @app.route("/oauth/callback")
    def oauth_callback():
        if not tester_mode:
            abort(404)
        pending = session.pop("oauth", None)  # single use
        if request.args.get("error"):
            flash(f"Salesforce login was cancelled or refused: "
                  f"{request.args.get('error_description') or request.args['error']}")
            return redirect(url_for("home"))
        if not pending or not hmac.compare_digest(pending["state"], request.args.get("state", "")):
            flash("The login link expired or was not started here. Click Connect Salesforce org again.")
            return redirect(url_for("home"))
        try:
            conn = oauth.exchange_code(cfg, pending["host"], request.args.get("code", ""),
                                       callback_url(), pending["verifier"])
        except oauth.OAuthError as e:
            flash(str(e))
            return redirect(url_for("home"))
        if not oauth.is_allowed(cfg, conn):
            oauth.revoke(conn)
            log.warning("Blocked Salesforce login from %s (org %s): not in CPQ2RCA_ALLOWED_USERS",
                        conn.get("username"), conn.get("org_id"))
            flash("This Salesforce user is not on the list of testers for this tool. Ask the person who "
                  "invited you to add you.")
            return redirect(url_for("home"))
        old = session.get("conn")
        if old:
            connections.pop(old, None)
        cid = secrets.token_urlsafe(32)
        connections[cid] = conn
        session["conn"] = cid
        log.info("Connected Salesforce org %s as %s", conn["org_id"], conn.get("username"))
        return redirect(url_for("home"))

    @app.post("/disconnect")
    def disconnect():
        conn = connections.pop(session.pop("conn", ""), None)
        if conn:
            oauth.revoke(conn)
        flash("Disconnected. Your runs are kept; connect the same org and user again to see them.")
        return redirect(url_for("home"))

    @app.route("/admin", methods=["GET", "POST"])
    def admin_login():
        if not tester_mode or not password:
            abort(404)
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("password", "").encode(), password.encode()):
                session["admin"] = True
                return redirect(url_for("home"))
            flash("Wrong admin password.")
        return render_template("admin.html")

    @app.post("/admin/logout")
    def admin_logout():
        session.pop("admin", None)
        return redirect(url_for("home"))

    return app
