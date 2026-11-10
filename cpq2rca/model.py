"""In-memory view over an extract folder with id lookups and child indexes."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from .catalog import BY_NAME


class Dataset:
    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir)
        ext = self.run_dir / "extract"
        self.manifest = json.loads((ext / "manifest.json").read_text())
        self.records: dict[str, list[dict]] = {}
        self.by_id: dict[str, dict] = {}
        self.type_of: dict[str, str] = {}
        for obj, meta in self.manifest["objects"].items():
            if meta.get("status") != "extracted":
                self.records[obj] = []
                continue
            recs = json.loads((ext / f"{obj}.json").read_text())
            self.records[obj] = recs
            for r in recs:
                self.by_id[r["Id"]] = r
                self.type_of[r["Id"]] = obj
        # child index: (child_obj, lookup_field, parent_id) -> [records]
        self._children: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for obj, recs in self.records.items():
            spec = BY_NAME.get(obj)
            if not spec:
                continue
            for r in recs:
                for f in spec.refs:
                    if r.get(f):
                        self._children[(obj, f, r[f])].append(r)

    def get(self, obj: str) -> list[dict]:
        return self.records.get(obj, [])

    def children(self, obj: str, field: str, parent_id: str, sort: str | None = None) -> list[dict]:
        out = list(self._children.get((obj, field, parent_id), []))
        if sort:
            out.sort(key=lambda r: (r.get(sort) is None, r.get(sort)))
        return out

    def name(self, rid: str | None) -> str:
        if not rid:
            return ""
        r = self.by_id.get(rid)
        if not r:
            return f"[missing {rid}]"
        if self.type_of.get(rid) == "Product2" and r.get("ProductCode"):
            return f"{r['Name']} ({r['ProductCode']})"
        return r.get("Name") or rid

    def product_by_code(self) -> dict[str, dict]:
        return {p["ProductCode"]: p for p in self.get("Product2") if p.get("ProductCode")}


# ------------------------------------------------------------- field helpers
_LABEL_OVERRIDES = {
    "SBQQ__NetTotal__c": "Net Total", "SBQQ__AdditionalDiscount__c": "Additional Discount (%)",
    "SBQQ__CustomerDiscount__c": "Customer Discount (%)", "SBQQ__SpecialPrice__c": "Special Price",
    "SBQQ__ProductCode__c": "Product Code", "SBQQ__ProductFamily__c": "Product Family",
    "SBQQ__Quantity__c": "Quantity", "SBQQ__SubscriptionTerm__c": "Subscription Term",
    "SBQQ__AverageCustomerDiscount__c": "Average Customer Discount (%)",
    "SBQQ__UnitCost__c": "Unit Cost",
}


def field_label(api: str | None) -> str:
    if not api:
        return ""
    if api in _LABEL_OVERRIDES:
        return _LABEL_OVERRIDES[api]
    s = re.sub(r"^(SBQQ|sbaa)__", "", api)
    s = re.sub(r"__c$", "", s)
    s = s.replace("_", " ")
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    return s.strip()


def is_custom_field(api: str | None) -> bool:
    return bool(api) and api.endswith("__c") and not api.startswith(("SBQQ__", "sbaa__"))
