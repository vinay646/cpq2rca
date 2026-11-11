"""Command line interface.

Examples
  cpq2rca run   --source sample                 --out runs/demo
  cpq2rca run   --source sf:my-cpq-sandbox      --out runs/acme-2026-11
  cpq2rca run   --source folder:/path/to/export --out runs/acme
  cpq2rca extract --source sf:alias --out runs/acme       (extract only)
  cpq2rca analyse --out runs/acme [--ai]                   (re-explain an extract)
  cpq2rca docs    --out runs/acme                          (rebuild Word/Excel)
  cpq2rca serve   [--runs runs] [--port 8080]              (web UI)

Token source: --source token with env SF_INSTANCE_URL and SF_ACCESS_TOKEN.
Web UI password: env CPQ2RCA_WEB_PASSWORD (required when --host is not localhost, unless tester mode).
Tester mode ("Connect Salesforce org" login): env SF_OAUTH_CLIENT_ID etc., see cpq2rca/web/oauth.py.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

SAMPLE = Path(__file__).resolve().parent / "sample_data" / "northwind"


def make_connector(source: str):
    from .connectors.mock import FolderConnector
    from .connectors.salesforce import SalesforceConnector

    if source == "sample":
        if not SAMPLE.exists():
            import runpy
            runpy.run_path(str(SAMPLE.parent / "generate.py"), run_name="__main__")
        return FolderConnector(SAMPLE)
    if source.startswith("folder:"):
        return FolderConnector(source.split(":", 1)[1])
    if source.startswith("sf:"):
        return SalesforceConnector.from_sf_cli(source.split(":", 1)[1])
    if source == "token":
        return SalesforceConnector(os.environ["SF_INSTANCE_URL"], os.environ["SF_ACCESS_TOKEN"])
    raise SystemExit(f"Unknown source '{source}'. Use sample | folder:PATH | sf:ALIAS | token")


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def serve(runs: str, host: str, port: int) -> None:
    """Start the web UI. Run outputs contain client data, so listening beyond
    this machine requires a password (CPQ2RCA_WEB_PASSWORD)."""
    from .web.app import create_app

    password = os.environ.get("CPQ2RCA_WEB_PASSWORD")
    tester_mode = bool(os.environ.get("SF_OAUTH_CLIENT_ID"))
    if tester_mode:
        print("Tester mode: users connect their own Salesforce org and see only their own runs.",
              file=sys.stderr)
        if not os.environ.get("CPQ2RCA_ALLOWED_USERS"):
            print("WARNING: CPQ2RCA_ALLOWED_USERS is not set, so ANY Salesforce user can connect and run "
                  "analyses on this server. Set it to the testers' emails, @domains or org IDs.",
                  file=sys.stderr)
        if not os.environ.get("CPQ2RCA_SECRET_KEY"):
            print("WARNING: CPQ2RCA_SECRET_KEY is not set: testers must reconnect after every restart.",
                  file=sys.stderr)
        if not password:
            print("Note: no CPQ2RCA_WEB_PASSWORD, so there is no admin login (/admin).", file=sys.stderr)
    if host not in LOCAL_HOSTS:
        if not password and not tester_mode:
            raise SystemExit(
                f"Refusing to serve on {host}: other machines could read client data. "
                "Set CPQ2RCA_WEB_PASSWORD to require a password, set up tester mode "
                "(SF_OAUTH_CLIENT_ID), or use --host 127.0.0.1."
            )
        print(f"WARNING: serving on {host} over plain HTTP. Put it behind HTTPS (a reverse proxy) "
              "so passwords, sessions and data are encrypted.", file=sys.stderr)
    create_app(runs, password=password).run(host=host, port=port)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cpq2rca", description="CPQ to RCA migration tool - Phase 1")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "extract"):
        p = sub.add_parser(name)
        p.add_argument("--source", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--ai", action="store_true", help="polish explanations with Claude (needs API key)")
        p.add_argument("--logo", help="logo image for documents (default: bundled company logo)")
    for name in ("analyse", "docs"):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
        p.add_argument("--ai", action="store_true")
        p.add_argument("--logo")
    p = sub.add_parser("serve")
    p.add_argument("--runs", default="runs")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from . import pipeline
    from .extract import extract

    def say(msg):
        print(f"  {msg}", file=sys.stderr)

    if args.cmd == "serve":
        serve(args.runs, args.host, args.port)
        return
    if args.cmd in ("run", "extract"):
        conn = make_connector(args.source)
        print(f"Extracting from {args.source} -> {args.out}")
        m = extract(conn, args.out, progress=say)
        print(f"Extracted {sum(o.get('count', 0) for o in m['objects'].values())} records "
              f"from {sum(1 for o in m['objects'].values() if o['status'] == 'extracted')} objects")
        if args.cmd == "extract":
            return
    if args.cmd in ("run", "analyse"):
        r = pipeline.analyse(args.out, ai=args.ai, progress=say)
        s = r["summary"]
        print(f"Explained {s['items']} items | complexity {s['complexity_rating']} | "
              f"effort ~{s['effort_days']} days | approach {s['by_approach']}")
    files = pipeline.build_documents(args.out, logo=getattr(args, "logo", None))
    for f in files:
        print(f"Wrote {f}")


if __name__ == "__main__":
    main()
