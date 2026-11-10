"""Readiness assessment: inventory, complexity, effort, hygiene findings,
custom-field inventory and the dependency graph."""

from __future__ import annotations

from collections import Counter, defaultdict

import networkx as nx

from .catalog import BY_NAME, CATEGORY_ORDER
from .model import Dataset, field_label

SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}


def _finding(sev, area, title, detail, refs=None, action=""):
    return {"severity": sev, "area": area, "title": title, "detail": detail,
            "refs": refs or [], "action": action}


def findings(ds: Dataset, items: list[dict]) -> list[dict]:
    out = []
    # dangling references
    dangling = []
    for obj, recs in ds.records.items():
        spec = BY_NAME.get(obj)
        if not spec:
            continue
        for r in recs:
            for f, target in spec.refs.items():
                v = r.get(f)
                if v and v not in ds.by_id and ds.manifest["objects"].get(target, {}).get("status") == "extracted":
                    dangling.append(f"{obj}.{f} on {r['Id']} -> {v}")
    if dangling:
        out.append(_finding("High", "Data integrity", f"{len(dangling)} broken lookup reference(s)",
                            "Records point to parents that were not extracted (deleted or filtered).",
                            dangling[:50], "Fix or remove before transformation."))

    for it in items:
        for n in it["notes"]:
            if "never runs" in n:
                out.append(_finding("Medium", "Product Rules", f"Rule '{it['name']}' never runs", n, [it["id"]],
                                    "Attach to a bundle or retire."))
            elif "no conditions" in n:
                out.append(_finding("Low", "Product Rules", f"Rule '{it['name']}' has no conditions", n, [it["id"]],
                                    "Confirm intent; consider a static message or retire."))
            elif "inactive product" in n:
                out.append(_finding("Medium", "Bundles", f"{it['name']} contains inactive products", n, [it["id"]],
                                    "Remove inactive options before migration."))
            elif "candidate for retirement" in n:
                out.append(_finding("Low", it["category"], f"{it['type']} '{it['name']}' is unused", n, [it["id"]],
                                    "Retire instead of migrating."))

    inactive = [it for it in items if not it["active"]]
    if inactive:
        out.append(_finding("Low", "Hygiene", f"{len(inactive)} inactive rule(s) / record(s)",
                            "Inactive configuration usually does not need migrating.",
                            [f"{i['type']}: {i['name']}" for i in inactive], "Confirm and exclude from scope."))

    # evaluation-order collisions
    for obj, event_field, label in (("SBQQ__PriceRule__c", "SBQQ__EvaluationEvent__c", "Price Rules"),
                                    ("SBQQ__ProductRule__c", "SBQQ__EvaluationEvent__c", "Product Rules")):
        groups = defaultdict(list)
        for r in ds.get(obj):
            if r.get("SBQQ__Active__c") is False:
                continue
            groups[(r.get(event_field), r.get("SBQQ__EvaluationOrder__c"))].append(r["Name"])
        for (ev, order), names in groups.items():
            if len(names) > 1 and order is not None:
                out.append(_finding("Medium", label, f"{len(names)} active rules share evaluation order {order:g} ({ev})",
                                    "Execution sequence between these rules is not deterministic; RCA needs an explicit order.",
                                    names, "Agree the intended sequence before mapping."))

    # near-duplicate rules (same name stem)
    names = Counter(it["name"].split(" (")[0].lower() for it in items if it["type"].startswith(("Price Rule", "Product Rule")))
    for stem, n in names.items():
        if n > 1:
            out.append(_finding("Low", "Hygiene", f"{n} rules named like '{stem}'",
                                "Possible duplicates or superseded versions.", [], "Consolidate."))

    for it in items:
        if it["type"] == "Quote Calculator Plugin":
            out.append(_finding("High", "Code", f"Quote Calculator Plugin '{it['name']}' requires redesign",
                                it["summary"], [it["id"]],
                                "Decompose each hook into RCA pricing procedure steps; build parity tests."))
        if it["type"] == "Approval Rule" and it["active"]:
            pass
    appr = [it for it in items if it["type"] == "Approval Rule" and it["active"]]
    if appr:
        out.append(_finding("Medium", "Approvals", f"{len(appr)} Advanced Approvals rule(s) to rebuild",
                            "Advanced Approvals does not carry over; rebuild with Flow approval processes.",
                            [a["name"] for a in appr]))

    tx = ds.manifest.get("transactions", {})
    subs = (tx.get("SBQQ__Subscription__c") or 0) + (tx.get("Asset") or 0)
    if subs:
        out.append(_finding("High", "Transactions", f"{subs:,} subscriptions/assets in flight",
                            "Active contracts must be represented as RCA assets for renewals and amendments to work.",
                            [], "Decide transactional migration and cut-over strategy."))

    for obj, meta in ds.manifest["objects"].items():
        if meta.get("missing_fields"):
            out.append(_finding("Low", "Extraction", f"{obj}: {len(meta['missing_fields'])} field(s) not in org",
                                "Older CPQ package version or field removed.", meta["missing_fields"]))
    for w in ds.manifest.get("warnings", []):
        out.append(_finding("Medium", "Extraction", w, "Expected object was not found."))
    out.sort(key=lambda f: SEVERITY_ORDER[f["severity"]])
    return out


def custom_fields(items: list[dict]) -> list[dict]:
    usage = defaultdict(list)
    for it in items:
        for f in it.get("custom_fields", []):
            usage[f].append(it["name"])
    return [{"field": f, "label": field_label(f), "used_by": sorted(set(u)), "count": len(set(u)),
             "rca_note": "Map into RCA context definition / ensure field exists on Quote or QuoteLineItem"}
            for f, u in sorted(usage.items(), key=lambda kv: -len(set(kv[1])))]


def graph(ds: Dataset, items: list[dict]) -> dict:
    g = nx.DiGraph()
    by_id = {it["id"]: it for it in items}
    for it in items:
        g.add_node(it["id"], label=it["name"], type=it["type"], approach=it["approach"])
        for d in it["dependencies"]:
            if d["id"] not in by_id and d["id"] not in g:
                g.add_node(d["id"], label=d["name"], type=d["type"], approach="")
            g.add_edge(it["id"], d["id"], relation=d["relation"])
    return {"nodes": [{"id": n, **a} for n, a in g.nodes(data=True)],
            "edges": [{"source": u, "target": v, **a} for u, v, a in g.edges(data=True)]}


def summary(ds: Dataset, items: list[dict], fnds: list[dict]) -> dict:
    by_type = Counter(it["type"].split(" - ")[0] for it in items)
    by_approach = Counter(it["approach"] for it in items)
    hours = sum(it["effort_hours"] for it in items)
    per_cat = defaultdict(lambda: {"items": 0, "hours": 0.0, "Direct": 0, "Adapt": 0, "Redesign": 0, "Retire": 0})
    for it in items:
        c = per_cat[it["category"]]
        c["items"] += 1
        c["hours"] += it["effort_hours"]
        c[it["approach"]] += 1
    active = [it for it in items if it["active"]]
    avg = sum(it["score"] for it in active) / max(1, len(active))
    redesign_share = by_approach.get("Redesign", 0) / max(1, len(active))
    rating = "High" if avg > 6 or redesign_share > 0.25 else "Medium" if avg > 3.5 or redesign_share > 0.1 else "Low"
    return {
        "org": ds.manifest.get("org", {}),
        "extracted_at": ds.manifest.get("extracted_at"),
        "record_counts": {o: m.get("count", 0) for o, m in ds.manifest["objects"].items()},
        "total_records": sum(m.get("count", 0) for m in ds.manifest["objects"].values()),
        "transactions": ds.manifest.get("transactions", {}),
        "items": len(items),
        "by_type": dict(by_type),
        "by_approach": {k: by_approach.get(k, 0) for k in ("Direct", "Adapt", "Redesign", "Retire")},
        "by_category": {c: dict(per_cat[c]) for c in CATEGORY_ORDER if c in per_cat},
        "effort_hours": round(hours, 1),
        "effort_days": round(hours / 8, 1),
        "complexity_rating": rating,
        "avg_score": round(avg, 1),
        "findings": Counter(f["severity"] for f in fnds),
        "products": len(ds.get("Product2")),
        "active_products": sum(1 for p in ds.get("Product2") if p.get("IsActive") is not False),
    }
