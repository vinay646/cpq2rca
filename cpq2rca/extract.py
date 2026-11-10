"""Phase 1 extractor: pull every catalogued CPQ object into JSON.

Output (run_dir/extract/):
    <SObject>.json   records with original Ids and lookup values intact
    manifest.json    org info, timestamp, per-object counts, fields requested /
                     missing, transactional counts, file checksums
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .catalog import CATALOG, load_order
from .connectors.base import Connector, ObjectNotFound

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
        if spec.kind == "transaction":
            try:
                manifest["transactions"][spec.name] = conn.count(spec.name)
            except ObjectNotFound:
                manifest["transactions"][spec.name] = None
            continue

        try:
            available = conn.describe_fields(spec.name)
        except ObjectNotFound:
            entry = {"status": "not_installed" if spec.optional else "missing", "count": 0}
            manifest["objects"][spec.name] = entry
            if not spec.optional:
                manifest["warnings"].append(f"{spec.name} not found in org")
            continue

        fields = [f for f in spec.fields if f in available]
        missing = [f for f in spec.fields if f not in available]
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

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest
