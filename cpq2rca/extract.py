"""Phase 1 extractor: pull every catalogued CPQ object into JSON.

Output (run_dir/extract/):
    <SObject>.json   records with original Ids and lookup values intact
    manifest.json    org info, timestamp, per-object counts, fields requested /
                     missing, transactional counts, file checksums

A failure on one object (permissions, query error, timeout) is recorded in the
manifest as status "error" and the extract carries on with the other objects.
Only an exhausted API limit stops the run, because every later call would fail.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .catalog import CATALOG, CONDITIONAL_FIELDS, load_order
from .connectors.base import ApiLimitExceeded, Connector, ObjectNotFound

log = logging.getLogger(__name__)


def extract(conn: Connector, run_dir: str | Path, include_inactive_products: bool = True,
            progress=None) -> dict:
    run_dir = Path(run_dir)
    out = run_dir / "extract"
    out.mkdir(parents=True, exist_ok=True)

    manifest = {
        "tool_version": __version__,
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "org": conn.org_info(),
        "objects": {},
        "transactions": {},
        "load_order": load_order(),
        "warnings": [],
    }

    for spec in CATALOG:
        if progress:
            progress(spec.name)
        try:
            _extract_one(conn, spec, out, manifest, include_inactive_products)
        except ApiLimitExceeded:
            raise
        except Exception as e:  # noqa: BLE001 - isolate per-object failures
            msg = f"{e.__class__.__name__}: {str(e)[:500]}"
            log.warning("%s: extraction failed - %s", spec.name, msg)
            manifest["warnings"].append(f"{spec.name} could not be extracted ({msg})")
            if spec.kind == "transaction":
                manifest["transactions"][spec.name] = None
            else:
                manifest["objects"][spec.name] = {"status": "error", "count": 0, "error": msg}

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def _extract_one(conn: Connector, spec, out: Path, manifest: dict, include_inactive_products: bool) -> None:
    if spec.kind == "transaction":
        try:
            manifest["transactions"][spec.name] = conn.count(spec.name)
        except ObjectNotFound:
            manifest["transactions"][spec.name] = None
        return

    try:
        available = conn.describe_fields(spec.name)
    except ObjectNotFound:
        manifest["objects"][spec.name] = {"status": "not_installed" if spec.optional else "missing", "count": 0}
        if not spec.optional:
            manifest["warnings"].append(f"{spec.name} not found in org")
        return

    fields = [f for f in spec.fields if f in available]
    missing = [f for f in spec.fields if f not in available and f not in CONDITIONAL_FIELDS]
    where = spec.where
    if spec.name == "Product2" and not include_inactive_products:
        where = "IsActive = true"
    records = conn.query_all(spec.name, fields, where)

    path = out / f"{spec.name}.json"
    payload = json.dumps(records, indent=1, default=str)
    path.write_text(payload)
    manifest["objects"][spec.name] = {
        "status": "extracted",
        "count": len(records),
        "fields": fields,
        "missing_fields": missing,
        "sha256": hashlib.sha256(payload.encode()).hexdigest(),
    }
    if missing:
        log.info("%s: %d catalogued fields not present: %s", spec.name, len(missing), missing)
