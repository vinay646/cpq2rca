"""Readiness assessment: inventory, complexity, effort, hygiene findings,
custom-field inventory and the dependency graph."""

from __future__ import annotations

from collections import Counter, defaultdict

import networkx as nx

from .catalog import BY_NAME, CATEGORY_ORDER, RCA_OBJECT, SCHEMA_OBJECTS, TWIN_PAIRS
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

    out.extend(_transaction_findings(ds))
    out.extend(_automation_findings(ds, items))
    out.extend(_schema_findings(ds, items))

    for obj, meta in ds.manifest["objects"].items():
        if meta.get("missing_fields"):
            out.append(_finding("Low", "Extraction", f"{obj}: {len(meta['missing_fields'])} field(s) not in org",
                                "Older CPQ package version or field removed.", meta["missing_fields"]))
    for obj, meta in ds.manifest["objects"].items():
        if meta.get("status") == "error":
            out.append(_finding("High", "Extraction", f"{obj} could not be extracted",
                                "The object exists but the query failed, so this area of the assessment is "
                                "incomplete. Usually caused by missing permissions for the integration user "
                                "or a timeout.", [meta.get("error", "")],
                                "Fix access for the integration user and re-run the extract."))
    for w in ds.manifest.get("warnings", []):
        if "could not be extracted" in w:
            continue  # already reported above / transactional count failure
        out.append(_finding("Medium", "Extraction", w, "Expected object was not found."))
    for obj, n in (ds.manifest.get("transactions") or {}).items():
        if n is None and any(obj in w and "could not be extracted" in w for w in ds.manifest.get("warnings", [])):
            out.append(_finding("Medium", "Extraction", f"{obj} could not be counted",
                                "The record count query failed, so in-flight volumes are understated.", [],
                                "Check the integration user's access and re-run."))

    out.extend(_currency_findings(ds))
    out.sort(key=lambda f: SEVERITY_ORDER[f["severity"]])
    return out


def _segment(ds: Dataset, obj: str, label: str):
    return (ds.manifest.get("transaction_segments") or {}).get(obj, {}).get(label)


def _transaction_findings(ds: Dataset) -> list[dict]:
    tx = ds.manifest.get("transactions", {})
    total = (tx.get("SBQQ__Subscription__c") or 0) + (tx.get("Asset") or 0)
    if not total:
        return []
    active_parts = [_segment(ds, o, "active") for o in ("SBQQ__Subscription__c", "Asset") if tx.get(o)]
    if active_parts and all(n is not None for n in active_parts):
        active = sum(active_parts)
        refs = [f"{BY_NAME[o].label}: {_segment(ds, o, 'active'):,} active of {tx[o]:,}"
                for o in ("SBQQ__Subscription__c", "Asset") if tx.get(o)]
        c = _segment(ds, "Contract", "active")
        if c is not None and tx.get("Contract"):
            refs.append(f"Contract: {c:,} active of {tx['Contract']:,}")
        if not active:
            return [_finding("Low", "Transactions", f"No active subscriptions or assets (of {total:,} in total)",
                             "Every subscription and asset has ended or been terminated.", refs,
                             "Confirm whether historical records need migrating at all.")]
        return [_finding("High", "Transactions", f"{active:,} active subscriptions/assets in flight (of {total:,} in total)",
                         "Active contracts must be represented as RCA assets for renewals and amendments to work. "
                         "Ended or terminated records can usually be archived instead of migrated.", refs,
                         "Decide transactional migration and cut-over strategy.")]
    return [_finding("High", "Transactions", f"{total:,} subscriptions/assets in flight",
                     "Active contracts must be represented as RCA assets for renewals and amendments to work. "
                     "The active share could not be counted, so this total includes ended records.",
                     [], "Decide transactional migration and cut-over strategy.")]


def _automation_findings(ds: Dataset, items: list[dict]) -> list[dict]:
    status = (ds.manifest.get("automation") or {}).get("status")
    if status in (None, "not_available"):
        return [_finding("Medium", "Automation", "Flows, Apex triggers and validation rules were not inventoried",
                         "This source does not provide automation metadata, so custom logic outside CPQ "
                         "configuration is not in this assessment.", [],
                         "Run against the live org, or review automation on CPQ objects manually.")]
    out = []
    auto = [it for it in items if it["category"] == "Automation" and it["active"]]
    moved = [it for it in auto if it["approach"] == "Redesign"]
    if moved:
        out.append(_finding("High", "Automation",
                            f"{len(moved)} custom automation(s) on CPQ objects must be rebuilt",
                            "These Apex triggers and flows run on CPQ objects (or are Process Builder processes). "
                            "They stop running when quoting moves to RCA objects and often hold pricing or "
                            "quoting logic that is not visible in CPQ configuration.",
                            [f"{it['type']}: {it['name']} ({it['object']})" for it in moved],
                            "Review each one and add its logic to the RCA design."))
    vr = [it for it in auto if it["type"] == "Validation Rule" and it["approach"] != "Direct"]
    if vr:
        out.append(_finding("Medium", "Automation", f"{len(vr)} validation rule(s) to recreate on RCA objects",
                            "Validation rules on CPQ quote objects must be rewritten for Quote / QuoteLineItem.",
                            [f"{it['name']} ({it['object']})" for it in vr]))
    if status == "partial":
        out.append(_finding("Medium", "Automation", "Automation inventory is incomplete",
                            "Part of the automation metadata could not be read; usually the integration user "
                            "lacks the 'View Setup and Configuration' permission.",
                            ds.manifest["automation"].get("errors", []),
                            "Grant the permission and re-run."))
    return out


def schema_inventory(ds: Dataset, items: list[dict]) -> dict:
    """Custom fields on quote/product/order objects, which CPQ logic uses them,
    and twin-field pairs that CPQ keeps in sync."""
    used = defaultdict(set)
    for it in items:
        for f in it.get("custom_fields", []):
            used[f].add(it["name"])
    by_obj = {o: {f["name"]: f for f in ds.schema.get(o, [])} for o in SCHEMA_OBJECTS if o in ds.schema}
    twins = []
    for src, dst in TWIN_PAIRS:
        if src not in by_obj or dst not in by_obj:
            continue
        for name in sorted(set(by_obj[src]) & set(by_obj[dst])):
            a, b = by_obj[src][name], by_obj[dst][name]
            twins.append({"field": name, "label": a.get("label") or name, "from": src, "to": dst,
                          "from_type": a.get("type"), "to_type": b.get("type"),
                          "type_match": not a.get("type") or not b.get("type") or a.get("type") == b.get("type")})
    twin_of = defaultdict(list)
    for t in twins:
        twin_of[(t["from"], t["field"])].append(t["to"])
        twin_of[(t["to"], t["field"])].append(t["from"])
    objects = {}
    for o, fields in by_obj.items():
        objects[o] = [{"name": n, "label": f.get("label") or n, "type": f.get("type") or "",
                       "formula": bool(f.get("formula")), "used_by": sorted(used.get(n, set())),
                       "twins": sorted(twin_of.get((o, n), [])), "rca_object": RCA_OBJECT.get(o, o)}
                      for n, f in sorted(fields.items())]
    return {"objects": objects, "twins": twins}


def _schema_findings(ds: Dataset, items: list[dict]) -> list[dict]:
    inv = schema_inventory(ds, items)
    out = []
    to_move = [(o, f) for o, fs in inv["objects"].items() if o in RCA_OBJECT for f in fs]
    if to_move:
        per = Counter(o for o, _ in to_move)
        out.append(_finding("Medium", "Data model",
                            f"{len(to_move)} custom field(s) on CPQ quote objects to recreate in RCA",
                            "Records move to the standard RCA objects, so these fields must be created there and "
                            "added to the context definition if pricing uses them.",
                            [f"{BY_NAME[o].label if o in BY_NAME else o} -> {RCA_OBJECT[o]}: {n} field(s)"
                             for o, n in per.items()],
                            "Agree which fields are still needed before Phase 2 mapping."))
    if inv["twins"]:
        out.append(_finding("Medium", "Data model", f"{len(inv['twins'])} twin field pair(s) kept in sync by CPQ",
                            "CPQ copies these values automatically (e.g. product to quote line, quote line to "
                            "order product). RCA has no twin-field sync, so each copy needs a flow, the context "
                            "definition or a mapping.",
                            [f"{t['field']}: {t['from']} -> {t['to']}" for t in inv["twins"]][:50],
                            "Decide how each value is carried over in RCA."))
    bad = [t for t in inv["twins"] if not t["type_match"]]
    if bad:
        out.append(_finding("Low", "Data model", f"{len(bad)} twin field pair(s) with different field types",
                            "CPQ only syncs twin fields of compatible types, so these values may not be copied today.",
                            [f"{t['field']}: {t['from']} ({t['from_type']}) -> {t['to']} ({t['to_type']})" for t in bad]))
    return out


def currencies(ds: Dataset) -> list[str]:
    """ISO codes in use: the org's active currencies plus any CurrencyIsoCode on extracted records."""
    codes = {c["iso_code"] for c in ds.manifest.get("org", {}).get("currencies", []) if c.get("iso_code")}
    for recs in ds.records.values():
        codes.update(r["CurrencyIsoCode"] for r in recs if r.get("CurrencyIsoCode"))
    return sorted(codes)


def _currency_findings(ds: Dataset) -> list[dict]:
    codes = currencies(ds)
    if len(codes) < 2:
        return []
    out = [_finding("Medium", "Pricing", f"Multi-currency org: {len(codes)} currencies ({', '.join(codes)})",
                    "Price book entries, contracted prices, block prices, discount schedules and costs are "
                    "held per currency. The RCA price book, price adjustment schedule and pricing procedure "
                    "design must cover every currency.",
                    codes, "Confirm which currencies are in scope and how conversion rates are maintained.")]
    # Same product + price book in several currencies looks like duplicate entries
    # unless the currency is taken into account.
    per_key = defaultdict(set)
    for r in ds.get("PricebookEntry"):
        if r.get("CurrencyIsoCode"):
            per_key[(r.get("Pricebook2Id"), r.get("Product2Id"))].add(r["CurrencyIsoCode"])
    multi = [f"{ds.name(pb)} / {ds.name(prod)}: {', '.join(sorted(c))}"
             for (pb, prod), c in per_key.items() if len(c) > 1]
    if multi:
        out.append(_finding("Low", "Pricing", f"{len(multi)} product price(s) defined in several currencies",
                            "Each currency needs its own price book entry in RCA; do not de-duplicate these.",
                            multi[:50]))
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
        "transaction_segments": ds.manifest.get("transaction_segments", {}),
        "automation": {"status": (ds.manifest.get("automation") or {}).get("status", "not_available"),
                       **((ds.manifest.get("automation") or {}).get("counts") or {})},
        "items": len(items),
        "by_type": dict(by_type),
        "by_approach": {k: by_approach.get(k, 0) for k in ("Direct", "Adapt", "Redesign", "Retire")},
        "by_category": {c: dict(per_cat[c]) for c in CATEGORY_ORDER if c in per_cat},
        "effort_hours": round(hours, 1),
        "effort_days": round(hours / 8, 1),
        "complexity_rating": rating,
        "avg_score": round(avg, 1),
        "findings": Counter(f["severity"] for f in fnds),
        "currencies": currencies(ds),
        "products": len(ds.get("Product2")),
        "active_products": sum(1 for p in ds.get("Product2") if p.get("IsActive") is not False),
    }
