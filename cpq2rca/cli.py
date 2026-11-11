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
Web UI password: env CPQ2RCA_WEB_PASSWORD (required when --host is not localhost).
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
    if host not in LOCAL_HOSTS:
        if not password:
            raise SystemExit(
                f"Refusing to serve on {host}: other machines could read client data. "
                "Set CPQ2RCA_WEB_PASSWORD to require a password, or use --host 127.0.0.1."
            )
        print(f"WARNING: serving on {host} with password protection over plain HTTP. "
              "Put it behind HTTPS (a reverse proxy) so the password and data are encrypted.",
              file=sys.stderr)
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
