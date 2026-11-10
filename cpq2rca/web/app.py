"""Flask web UI: start runs, browse explanations, findings and downloads."""

from __future__ import annotations

import json
import re
import threading
import traceback
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_file, url_for

from .. import __version__, pipeline
from ..catalog import BY_NAME, CATEGORY_ORDER
from ..report.branding import COMPANY, logo_path

JOBS: dict[str, dict] = {}


def create_app(runs_dir="runs") -> Flask:
    runs = Path(runs_dir).resolve()
    runs.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__)
    app.jinja_env.globals.update(company=COMPANY, version=__version__)

    def run_path(name: str) -> Path:
        if not re.fullmatch(r"[\w.-]+", name):
            abort(404)
        p = runs / name
        if not p.is_dir():
            abort(404)
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
        items = []
        for d in sorted(runs.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            a = d / "analysis.json"
            if d.is_dir() and a.exists():
                s = json.loads(a.read_text())["summary"]
                items.append({"name": d.name, "org": s["org"].get("org_name", ""), "when": s.get("extracted_at", ""),
                              "items": s["items"], "rating": s["complexity_rating"], "days": s["effort_days"]})
        return render_template("home.html", runs=items, jobs=JOBS)

    @app.post("/runs")
    def start_run():
        from ..cli import make_connector

        name = re.sub(r"[^\w.-]", "-", request.form.get("name") or "") or datetime.now().strftime("run-%Y%m%d-%H%M%S")
        kind = request.form.get("source", "sample")
        if kind == "sample":
            source = "sample"
        elif kind == "folder":
            source = "folder:" + request.form.get("folder", "")
        elif kind == "sf":
            source = "sf:" + request.form.get("alias", "")
        else:
            source = None
        JOBS[name] = {"status": "running", "log": [], "started": datetime.now().isoformat(timespec="seconds")}

        def work():
            job = JOBS[name]
            try:
                if source:
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

        request_form = dict(request.form)
        threading.Thread(target=work, daemon=True).start()
        return redirect(url_for("job", name=name))

    @app.route("/jobs/<name>")
    def job(name):
        j = JOBS.get(name)
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
        allowed = {f.name: f for f in base.iterdir() if f.suffix in (".docx", ".xlsx", ".json")}
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

    return app
