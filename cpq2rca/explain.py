"""Turn extracted CPQ records into business-readable explanations.

Every explained unit ("item") has the same shape so the document generator,
Excel inventory, web UI and the Phase 2 mapping workbook can all consume it:

    id, object, type, name, active, category,
    summary, when, logic, conditions[], actions[], applies_to[], example,
    details[] (extra lines, e.g. tiers or bundle tree), dependencies[],
    fields[], rca_target, approach, score, effort_hours, notes[], source{}

Explanations are generated deterministically from the record structure. An
optional AI pass (cpq2rca.ai) can polish summaries and examples afterwards.
"""

from __future__ import annotations

import math
import re

from .catalog import BY_NAME, RCA_OBJECT
from .model import Dataset, field_label, is_custom_field

EFFORT = {"Direct": 2, "Adapt": 6, "Redesign": 16, "Retire": 0.5}

OPS = {
    "equals": "is", "not equals": "is not", "less than": "is less than",
    "less or equals": "is at most", "greater than": "is greater than",
    "greater or equals": "is at least", "starts with": "starts with",
    "ends with": "ends with", "contains": "contains", "not contains": "does not contain",
}


# --------------------------------------------------------------------- utils
def _num(v):
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


def _fmt(n) -> str:
    if n is None:
        return ""
    if isinstance(n, float) and not n.is_integer():
        return f"{n:,.2f}"
    return f"{int(n):,}"


def _sample(op: str, value):
    """A value that satisfies 'op value' - used to build examples."""
    n = _num(value)
    if n is not None:
        if op == "less than":
            return _fmt(max(0, math.floor(n * 0.8)) if n > 1 else 0)
        if op == "greater than":
            return _fmt(math.ceil(n * 1.25) if n else 1)
        return _fmt(n)
    if op == "not equals":
        return "EMEA" if value in (None, "") else f"something other than '{value}'"
    return f"'{value}'" if value not in (None, "") else "blank"


def _approach(score: int, floor: str | None = None) -> str:
    a = "Direct" if score <= 3 else "Adapt" if score <= 7 else "Redesign"
    order = ["Direct", "Adapt", "Redesign"]
    if floor and order.index(floor) > order.index(a):
        a = floor
    return a


def _item(ds: Dataset, rec: dict, obj: str, typ: str, category: str, **kw) -> dict:
    item = {
        "id": rec["Id"], "object": obj, "type": typ, "name": rec.get("Name") or rec["Id"],
        "active": rec.get("SBQQ__Active__c", rec.get("sbaa__Active__c",
                          rec.get("IsActive", True))) is not False,
        "category": category, "summary": "", "when": "", "logic": "", "conditions": [],
        "actions": [], "applies_to": [], "example": "", "details": [], "dependencies": [],
        "fields": [], "rca_target": BY_NAME[obj].rca_target if obj in BY_NAME else "",
        "approach": "Direct", "score": 1, "effort_hours": EFFORT["Direct"], "notes": [],
        "source": {"object": obj, "id": rec["Id"], "children": []},
    }
    item.update(kw)
    return item


def _finish(item: dict, score: int, floor: str | None = None):
    item["score"] = score
    if not item["active"]:
        item["approach"] = "Retire"
        item["notes"].append("Inactive in CPQ - confirm whether it should be retired rather than migrated.")
    else:
        item["approach"] = _approach(score, floor)
    item["effort_hours"] = item.get("effort_override") or EFFORT[item["approach"]]
    item.pop("effort_override", None)
    item["fields"] = sorted(set(f for f in item["fields"] if f))
    seen, deps = set(), []
    for d in item["dependencies"]:
        if d["id"] and d["id"] not in seen:
            seen.add(d["id"])
            deps.append(d)
    item["dependencies"] = deps
    return item


def _dep(ds: Dataset, rid: str | None, relation: str) -> dict | None:
    if not rid:
        return None
    return {"id": rid, "name": ds.name(rid), "type": ds.type_of.get(rid, "missing"),
            "relation": relation}


def _subject(ds: Dataset, obj: str | None, field: str | None, var: str | None) -> str:
    if var:
        return f"summary variable '{ds.name(var)}'"
    obj = obj or "Quote Line"
    return f"{obj} {field_label(field)}".strip()


def _condition_text(ds, obj, field, var, op, value, fvar=None, formula=None) -> tuple[str, str]:
    """Return (condition sentence, example fragment)."""
    op = (op or "equals").lower()
    if formula:
        return f"formula `{formula}` {OPS.get(op, op)} {value}", f"the formula result {OPS.get(op, op)} {value}"
    if fvar:
        rhs = f"summary variable '{ds.name(fvar)}'"
        return f"{_subject(ds, obj, field, var)} {OPS.get(op, op)} {rhs}", ""
    # friendlier phrasing for product-code checks
    if field == "SBQQ__ProductCode__c" and op in ("equals", "not equals"):
        prod = ds.product_by_code().get(value)
        pname = f"{prod['Name']} ({value})" if prod else value
        neg = op == "not equals"
        if obj == "Product Option":
            return (f"option {pname} is {'not ' if neg else ''}selected in the bundle",
                    f"{pname} is {'not ' if neg else ''}selected")
        if obj == "Quote Line":
            return (f"the quote line {'is not' if neg else 'is'} {pname}",
                    f"the quote {'does not contain' if neg else 'contains'} {pname}")
    if value in (None, "") and op == "not equals":
        s = f"{_subject(ds, obj, field, var)} is not blank"
        return s, f"{_subject(ds, obj, field, var)} is {_sample(op, value)}"
    if value in (None, "") and op == "equals":
        s = f"{_subject(ds, obj, field, var)} is blank"
        return s, s
    disp = f"'{value}'" if _num(value) is None else _fmt(_num(value))
    s = f"{_subject(ds, obj, field, var)} {OPS.get(op, op)} {disp}"
    return s, f"{_subject(ds, obj, field, var)} is {_sample(op, value)}"


def _logic(met: str | None, adv: str | None, n: int) -> str:
    if n == 0:
        return "No conditions - the rule always fires."
    if n == 1:
        return "Fires when the condition is true."
    if (met or "All") == "All":
        return "Fires when ALL conditions are true."
    if met == "Any":
        return "Fires when ANY condition is true."
    return f"Fires when the custom logic `{adv}` is true (numbers refer to the conditions)."


def _satisfying(adv: str | None, met: str | None, n: int) -> list[int]:
    """Indexes (1-based) of a minimal set of conditions that makes the rule fire."""
    if n == 0:
        return []
    met = met or "All"
    if met == "All" or not adv and met != "Any":
        return list(range(1, n + 1))
    if met == "Any":
        return [1]
    import itertools
    expr = re.sub(r"\bAND\b", " and ", re.sub(r"\bOR\b", " or ", re.sub(r"\bNOT\b", " not ", adv.upper())))
    if not re.fullmatch(r"[\d\s()andornt]+", expr):
        return list(range(1, n + 1))
    for k in range(1, n + 1):
        for combo in itertools.combinations(range(1, n + 1), k):
            env = re.sub(r"\d+", lambda m: str(int(m.group()) in combo), expr)
            try:
                if eval(env, {"__builtins__": {}}):  # noqa: S307 - sanitised boolean expression
                    return list(combo)
            except Exception:  # noqa: BLE001
                return list(range(1, n + 1))
    return list(range(1, n + 1))


_FUNCS = {"MAX": max, "MIN": min, "CEILING": math.ceil, "FLOOR": math.floor, "ABS": abs,
          "ROUND": lambda x, d=0: round(x, int(d))}


def _formula_example(formula: str, sample: float = 300) -> str:
    """Evaluate a simple numeric CPQ formula with sample inputs, e.g. for docs."""
    fields = sorted(set(re.findall(r"\b[A-Za-z_]\w*__c\b", formula)))
    expr = formula
    for f in fields:
        expr = re.sub(rf"\b{re.escape(f)}\b", str(sample), expr)
    if not re.fullmatch(r"[\d\s.+\-*/(),A-Z]+", expr) or set(re.findall(r"[A-Z]+", expr)) - set(_FUNCS):
        return ""
    try:
        val = eval(expr, {"__builtins__": {}}, _FUNCS)  # noqa: S307 - whitelisted tokens only
    except Exception:  # noqa: BLE001
        return ""
    inputs = ", ".join(f"{field_label(f)} = {_fmt(sample)}" for f in fields)
    return f"{_fmt(val)} (with {inputs})" if inputs else _fmt(val)


def _tidy(sentence: str) -> str:
    sentence = re.sub(r"\s{2,}", " ", sentence).strip()
    return sentence.replace('.".', '."').replace("..", ".")


def _join(parts: list[str], word="and") -> str:
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + f" {word} " + parts[-1]


# ------------------------------------------------------------ product rules
PR_EVENTS = {"Always": "whenever the configuration or quote is loaded, edited or saved",
             "Load": "when the configuration or quote is loaded", "Edit": "when the user edits",
             "Save": "when the user saves", "Load;Save": "on load and on save",
             "Edit;Save": "on edit and on save"}

PA_TEXT = {
    "Add": "Automatically adds {p}", "Remove": "Automatically removes {p}",
    "Enable": "Makes {p} selectable", "Disable": "Prevents selection of {p}",
    "Show": "Shows {p}", "Hide": "Hides {p}",
    "Enable & Add": "Makes {p} selectable and adds it", "Disable & Remove": "Removes {p} and prevents re-selecting it",
}


def explain_product_rules(ds: Dataset) -> list[dict]:
    items = []
    bundles_for = {}
    for cr in ds.get("SBQQ__ConfigurationRule__c"):
        bundles_for.setdefault(cr["SBQQ__ProductRule__c"], []).append(cr)

    for rule in ds.get("SBQQ__ProductRule__c"):
        rid = rule["Id"]
        typ = rule.get("SBQQ__Type__c") or "Validation"
        scope = rule.get("SBQQ__Scope__c") or "Product"
        conds = ds.children("SBQQ__ErrorCondition__c", "SBQQ__Rule__c", rid, sort="SBQQ__Index__c")
        acts = ds.children("SBQQ__ProductAction__c", "SBQQ__Rule__c", rid)
        lqs = ds.children("SBQQ__LookupQuery__c", "SBQQ__ProductRule__c", rid)
        crs = bundles_for.get(rid, [])
        bundles = [ds.name(c["SBQQ__Product__c"]) for c in crs]

        it = _item(ds, rule, "SBQQ__ProductRule__c", f"Product Rule - {typ}", "Product Rules")
        it["applies_to"] = bundles or (["All quotes"] if scope == "Quote" else ["(not attached to any bundle)"])
        where = f"the configurator for {_join(bundles)}" if scope == "Product" and bundles else \
                "the quote line editor" if scope == "Quote" else "the configurator"
        it["when"] = f"Runs in {where}, {PR_EVENTS.get(rule.get('SBQQ__EvaluationEvent__c'), (rule.get('SBQQ__EvaluationEvent__c') or 'always').lower())} (evaluation order {_fmt(_num(rule.get('SBQQ__EvaluationOrder__c'))) or '-'})."

        ex_parts, svs = [], set()
        for c in conds:
            text, ex = _condition_text(ds, c.get("SBQQ__TestedObject__c"), c.get("SBQQ__TestedField__c"),
                                       c.get("SBQQ__TestedVariable__c"), c.get("SBQQ__Operator__c"),
                                       c.get("SBQQ__FilterValue__c"), c.get("SBQQ__FilterVariable__c"))
            it["conditions"].append(f"{_fmt(_num(c.get('SBQQ__Index__c')))}. {text}")
            ex_parts.append(ex)
            it["fields"].append(c.get("SBQQ__TestedField__c"))
            for v in (c.get("SBQQ__TestedVariable__c"), c.get("SBQQ__FilterVariable__c")):
                if v:
                    svs.add(v)
                    it["dependencies"].append(_dep(ds, v, "uses summary variable"))
            it["source"]["children"].append(c["Id"])
        it["logic"] = _logic(rule.get("SBQQ__ConditionsMet__c"), rule.get("SBQQ__AdvancedCondition__c"), len(conds))

        msg = rule.get("SBQQ__ErrorMessage__c")
        lookup = rule.get("SBQQ__LookupObject__c")
        outcome = ""
        if typ == "Validation":
            it["actions"].append(f"Blocks the user from continuing and shows: \"{msg}\"")
            outcome = f"the user cannot save and sees \"{msg}\""
        elif typ == "Alert":
            it["actions"].append(f"Shows a warning the user can acknowledge and continue: \"{msg}\"")
            outcome = f"a warning appears: \"{msg}\""
        for a in acts:
            at = a.get("SBQQ__Type__c") or "Add"
            prod = ds.name(a.get("SBQQ__Product__c")) if a.get("SBQQ__Product__c") else None
            if at in ("Default Filter", "Optional Filter"):
                t = (f"{'Applies' if at == 'Default Filter' else 'Offers optional'} product filter: "
                     f"{field_label(a.get('SBQQ__FilterField__c'))} {OPS.get((a.get('SBQQ__Operator__c') or 'equals'), a.get('SBQQ__Operator__c'))} '{a.get('SBQQ__FilterValue__c')}'")
                it["fields"].append(a.get("SBQQ__FilterField__c"))
            elif prod:
                t = PA_TEXT.get(at, at + " {p}").format(p=prod)
                if a.get("SBQQ__Required__c"):
                    t += " (user cannot deselect it)"
                it["dependencies"].append(_dep(ds, a.get("SBQQ__Product__c"), f"action: {at}"))
            elif lookup:
                t = PA_TEXT.get(at, at + " {p}").format(p=f"the products returned by lookup table {lookup}")
            else:
                t = f"{at} (no product specified)"
            it["actions"].append(t)
            it["source"]["children"].append(a["Id"])
        if typ in ("Selection", "Filter") and it["actions"]:
            outcome = "the configurator " + _join([a[0].lower() + a[1:] for a in it["actions"]])
        for lq in lqs:
            it["details"].append(
                f"Lookup: {lookup}.{lq.get('SBQQ__LookupField__c')} {OPS.get(lq.get('SBQQ__Operator__c') or 'equals')} "
                f"{field_label(lq.get('SBQQ__TestedField__c'))}")
            it["fields"] += [lq.get("SBQQ__TestedField__c")]
            it["source"]["children"].append(lq["Id"])
        for c in crs:
            it["dependencies"].append(_dep(ds, c["SBQQ__Product__c"], "attached to bundle"))
            it["source"]["children"].append(c["Id"])

        # summary + example
        verb = {"Validation": "Prevents", "Alert": "Warns about", "Selection": "Automates product selection",
                "Filter": "Filters the products a user can pick"}[typ] if typ in (
            "Validation", "Alert", "Selection", "Filter") else typ
        if typ in ("Validation", "Alert"):
            it["summary"] = f"{verb} invalid configurations: {msg}" if typ == "Validation" else f"{verb}: {msg}"
        elif typ == "Selection":
            acts_l = [a.rstrip('.') for a in it['actions']]
            it["summary"] = f"{verb}: {_join([acts_l[0]] + [a[0].lower() + a[1:] for a in acts_l[1:]])}."
        else:
            it["summary"] = f"{verb}: {_join(it['actions'])}."
        ctx = f"a rep configures {bundles[0]}" if scope == "Product" and bundles else "a rep is working on a quote"
        pick = _satisfying(rule.get("SBQQ__AdvancedCondition__c"), rule.get("SBQQ__ConditionsMet__c"), len(ex_parts))
        cond_ex = _join([ex_parts[i - 1] for i in pick])
        it["example"] = _tidy(f"Example: {ctx}{' and ' + cond_ex if cond_ex else ''} - {outcome or 'the rule fires'}.")

        # complexity
        score = 2 + max(0, len(conds) - 2) + len(svs) * 2
        if rule.get("SBQQ__ConditionsMet__c") == "Custom":
            score += 3
        if lookup:
            score += 4
            it["notes"].append(f"Uses lookup object {lookup}; plan a Decision Table or CML table in RCA.")
        if typ == "Filter":
            score += 2
            it["rca_target"] = "Product Discovery qualification rules / CML (confirm)"
        elif scope == "Quote":
            score += 1
            it["rca_target"] = "Business Rules Engine / transaction validation (confirm)"
        else:
            it["rca_target"] = "Advanced Configurator constraint rule (CML)"
        if any((a.get("SBQQ__Type__c") or "") in ("Show", "Hide", "Enable", "Disable") for a in acts):
            score += 1
        if not conds:
            it["notes"].append("Rule has no conditions and fires every time it is evaluated.")
        if scope == "Product" and not crs:
            it["notes"].append("Product-scoped rule is not attached to any bundle via Configuration Rules, so it never runs.")
        items.append(_finish(it, score))
    return items


# --------------------------------------------------------------- price rules
def explain_price_rules(ds: Dataset) -> list[dict]:
    items = []
    for rule in ds.get("SBQQ__PriceRule__c"):
        rid = rule["Id"]
        target = rule.get("SBQQ__TargetObject__c") or "Calculator"
        conds = ds.children("SBQQ__PriceCondition__c", "SBQQ__Rule__c", rid, sort="SBQQ__Index__c")
        acts = ds.children("SBQQ__PriceAction__c", "SBQQ__Rule__c", rid, sort="SBQQ__Order__c")
        lqs = ds.children("SBQQ__LookupQuery__c", "SBQQ__PriceRule2__c", rid)
        lookup = rule.get("SBQQ__LookupObject__c")

        it = _item(ds, rule, "SBQQ__PriceRule__c", f"Price Rule - {target}", "Price Rules")
        order = _fmt(_num(rule.get("SBQQ__EvaluationOrder__c"))) or "-"
        if target == "Configurator":
            it["when"] = f"Runs in the configurator on {(rule.get('SBQQ__ConfiguratorEvaluationEvent__c') or 'Save').lower()} (order {order})."
            if rule.get("SBQQ__Product__c"):
                it["applies_to"] = [ds.name(rule["SBQQ__Product__c"])]
                it["dependencies"].append(_dep(ds, rule["SBQQ__Product__c"], "configurator bundle"))
        else:
            it["when"] = f"Runs during quote calculation at the '{rule.get('SBQQ__EvaluationEvent__c')}' stage (order {order})."
        if not it["applies_to"]:
            it["applies_to"] = ["All quotes"]

        ex_parts, svs = [], set()
        for c in conds:
            text, ex = _condition_text(ds, c.get("SBQQ__Object__c"), c.get("SBQQ__Field__c"),
                                       c.get("SBQQ__TestedVariable__c"), c.get("SBQQ__Operator__c"),
                                       c.get("SBQQ__Value__c"), c.get("SBQQ__FilterVariable__c"),
                                       c.get("SBQQ__TestedFormula__c"))
            it["conditions"].append(f"{_fmt(_num(c.get('SBQQ__Index__c')))}. {text}")
            ex_parts.append(ex)
            it["fields"].append(c.get("SBQQ__Field__c"))
            for v in (c.get("SBQQ__TestedVariable__c"), c.get("SBQQ__FilterVariable__c")):
                if v:
                    svs.add(v)
                    it["dependencies"].append(_dep(ds, v, "uses summary variable"))
            it["source"]["children"].append(c["Id"])
        it["logic"] = _logic(rule.get("SBQQ__ConditionsMet__c"), rule.get("SBQQ__AdvancedCondition__c"), len(conds))

        formulas = 0
        outcomes = []
        for a in acts:
            tgt = a.get("SBQQ__TargetObject__c") or "Quote Line"
            fld = field_label(a.get("SBQQ__Field__c"))
            it["fields"].append(a.get("SBQQ__Field__c"))
            worked = ""
            if a.get("SBQQ__Formula__c"):
                formulas += 1
                src = f"the formula `{a['SBQQ__Formula__c']}`"
                worked = _formula_example(a["SBQQ__Formula__c"])
                it["fields"] += re.findall(r"\b\w+__c\b", a["SBQQ__Formula__c"])
            elif a.get("SBQQ__SourceVariable__c"):
                src = f"summary variable '{ds.name(a['SBQQ__SourceVariable__c'])}'"
            elif a.get("SBQQ__SourceLookupField__c"):
                src = f"{a['SBQQ__SourceLookupField__c']} from lookup table {lookup}"
                it["fields"].append(a["SBQQ__SourceLookupField__c"])
            elif a.get("SBQQ__ValueField__c"):
                src = f"the value of {field_label(a['SBQQ__ValueField__c'])}"
            else:
                src = f"{a.get('SBQQ__Value__c')}"
            if a.get("SBQQ__SourceVariable__c"):
                svs.add(a["SBQQ__SourceVariable__c"])
                it["dependencies"].append(_dep(ds, a["SBQQ__SourceVariable__c"], "uses summary variable"))
            it["actions"].append(f"Sets {tgt} {fld} to {src}")
            outcomes.append(f"{tgt} {fld} becomes {worked}" if worked else f"{tgt} {fld} becomes {src}")
            it["source"]["children"].append(a["Id"])
        for lq in lqs:
            it["details"].append(
                f"Lookup match: {lookup}.{lq.get('SBQQ__LookupField__c')} {OPS.get(lq.get('SBQQ__Operator__c') or 'equals')} "
                f"{field_label(lq.get('SBQQ__TestedField__c'))}")
            it["fields"].append(lq.get("SBQQ__TestedField__c"))
            it["source"]["children"].append(lq["Id"])

        cond_short = _join([c.split(". ", 1)[1] for c in it["conditions"]],
                           "and" if (rule.get("SBQQ__ConditionsMet__c") or "All") == "All" else "or")
        it["summary"] = (f"When {cond_short}, " if cond_short else "Always ") + \
            _join([a[0].lower() + a[1:] for a in it["actions"]]) + "."
        it["summary"] = it["summary"][0].upper() + it["summary"][1:]
        pick = _satisfying(rule.get("SBQQ__AdvancedCondition__c"), rule.get("SBQQ__ConditionsMet__c"), len(ex_parts))
        cond_ex = _join([ex_parts[i - 1] for i in pick])
        ctx = "on a configuration" if target == "Configurator" else "on a quote"
        it["example"] = _tidy(f"Example: {ctx} where {cond_ex or 'any line exists'}, {_join(outcomes) or 'nothing changes'}.")

        score = 2 + max(0, len(conds) - 2) + formulas * 2 + len(svs) * 2 + max(0, len(acts) - 1)
        if lookup:
            score += 4
            it["notes"].append(f"Uses lookup object {lookup}: migrate its data to an RCA Decision Table.")
            it["rca_target"] = "Pricing Procedure element + Decision Table"
        if target == "Configurator":
            score += 2
            it["rca_target"] = "Configurator rule (CML) / default value (confirm)"
        if rule.get("SBQQ__ConditionsMet__c") == "Custom":
            score += 3
        if formulas:
            it["notes"].append("Formula actions must be rebuilt as Pricing Procedure formula elements; confirm referenced fields exist in the RCA context definition.")
        items.append(_finish(it, score))
    return items


# ---------------------------------------------------------- summary variables
def explain_summary_variables(ds: Dataset) -> list[dict]:
    items = []
    for v in ds.get("SBQQ__SummaryVariable__c"):
        it = _item(ds, v, "SBQQ__SummaryVariable__c", "Summary Variable", "Product Rules")
        flt = ""
        if v.get("SBQQ__FilterField__c"):
            flt = (f" where {field_label(v['SBQQ__FilterField__c'])} "
                   f"{OPS.get(v.get('SBQQ__Operator__c') or 'equals')} '{v.get('SBQQ__FilterValue__c')}'")
            it["fields"].append(v["SBQQ__FilterField__c"])
        it["fields"].append(v.get("SBQQ__AggregateField__c"))
        fn = (v.get("SBQQ__AggregateFunction__c") or "Sum").lower()
        tgt = v.get("SBQQ__TargetObject__c") or "Quote Line"
        it["summary"] = (f"Calculates the {fn} of {field_label(v.get('SBQQ__AggregateField__c'))} "
                         f"across {tgt.lower()}s{flt}.")
        it["when"] = f"Evaluated whenever a rule that references it runs (scope: {v.get('SBQQ__Scope__c') or 'Quote'})."
        unit = "configuration with two matching options" if "Option" in tgt else "quote with two matching lines"
        it["example"] = (f"Example: a {unit} of quantity 30 and 20 gives "
                         f"{v['Name']} = {50 if fn == 'sum' else 2 if fn == 'count' else 30 if fn == 'max' else 20 if fn == 'min' else 25}.")
        if v.get("SBQQ__CombineWith__c"):
            it["details"].append(f"Combined with '{ds.name(v['SBQQ__CombineWith__c'])}' using {v.get('SBQQ__CompositeOperator__c')}.")
            it["dependencies"].append(_dep(ds, v["SBQQ__CombineWith__c"], "combined with"))
        score = 2 + (2 if v.get("SBQQ__CombineWith__c") else 0)
        items.append(_finish(it, score))
    return items


# --------------------------------------------------------------------- bundles
def explain_bundles(ds: Dataset) -> list[dict]:
    items = []
    for p in ds.get("Product2"):
        feats = ds.children("SBQQ__ProductFeature__c", "SBQQ__ConfiguredSKU__c", p["Id"], sort="SBQQ__Number__c")
        opts = ds.children("SBQQ__ProductOption__c", "SBQQ__ConfiguredSKU__c", p["Id"], sort="SBQQ__Number__c")
        if not (feats or opts or p.get("SBQQ__ConfigurationType__c")):
            continue
        it = _item(ds, p, "Product2", "Bundle", "Bundles")
        it["name"] = ds.name(p["Id"])
        it["rca_target"] = "Product2 (Bundle) + ProductComponentGroup + ProductRelatedComponent"
        included = [o for o in opts if o.get("SBQQ__Selected__c") or o.get("SBQQ__Required__c")]
        it["summary"] = (f"Configurable bundle with {len(feats)} feature group(s) and {len(opts)} option(s); "
                         f"{len(included)} included by default.")
        it["when"] = f"Configuration: {p.get('SBQQ__ConfigurationType__c') or 'n/a'}, opens {(p.get('SBQQ__ConfigurationEvent__c') or 'n/a').lower()}."
        inactive_opts = []
        for f in feats + [None]:
            fopts = [o for o in opts if (o.get("SBQQ__Feature__c") == (f["Id"] if f else None))]
            if f is None and not fopts:
                continue
            mn, mx = (f or {}).get("SBQQ__MinOptionCount__c"), (f or {}).get("SBQQ__MaxOptionCount__c")
            rng = f" (pick {_fmt(_num(mn)) or 0}-{_fmt(_num(mx)) or 'any'})" if f and (mn or mx) else ""
            it["details"].append(f"{f['Name'] if f else 'No feature'}{rng}")
            for o in fopts:
                op = ds.by_id.get(o.get("SBQQ__OptionalSKU__c"), {})
                flags = [x for x, on in (("required", o.get("SBQQ__Required__c")),
                                         ("selected by default", o.get("SBQQ__Selected__c")),
                                         ("price included in bundle", o.get("SBQQ__Bundled__c")),
                                         (o.get("SBQQ__Type__c", "").lower(), o.get("SBQQ__Type__c") not in (None, "Component")))
                         if on]
                if op and op.get("IsActive") is False:
                    flags.append("PRODUCT INACTIVE")
                    inactive_opts.append(op.get("Name"))
                q = _fmt(_num(o.get("SBQQ__Quantity__c")))
                it["details"].append(f"    - {ds.name(o.get('SBQQ__OptionalSKU__c'))}{' x' + q if q and q != '1' else ''}"
                                     f"{' [' + ', '.join(flags) + ']' if flags else ''}")
                it["dependencies"].append(_dep(ds, o.get("SBQQ__OptionalSKU__c"), "option"))
                it["source"]["children"].append(o["Id"])
            if f:
                it["source"]["children"].append(f["Id"])
        for a in ds.children("SBQQ__ConfigurationAttribute__c", "SBQQ__Product__c", p["Id"]):
            it["details"].append(f"Attribute: {a['Name']} -> {field_label(a.get('SBQQ__TargetField__c'))}"
                                 f"{' (required)' if a.get('SBQQ__Required__c') else ''}")
            it["fields"].append(a.get("SBQQ__TargetField__c"))
            it["source"]["children"].append(a["Id"])
        it["applies_to"] = [it["name"]]
        first_opt = next((o for o in opts if not o.get("SBQQ__Required__c")), None)
        it["example"] = (f"Example: a rep adds {it['name']} to a quote; "
                         f"{_join([ds.name(o['SBQQ__OptionalSKU__c']) for o in included][:3]) or 'no options'} "
                         f"{'are' if len(included) > 1 else 'is'} pre-selected"
                         + (f", and they can optionally add {ds.name(first_opt['SBQQ__OptionalSKU__c'])}." if first_opt else "."))
        if inactive_opts:
            it["notes"].append(f"Bundle contains inactive product(s): {', '.join(inactive_opts)} - clean up before migration.")
        score = 2 + len(opts) // 10 + len(feats) // 4
        items.append(_finish(it, score))
    return items


def explain_option_constraints(ds: Dataset) -> list[dict]:
    items = []
    for c in ds.get("SBQQ__OptionConstraint__c"):
        a = ds.by_id.get(c.get("SBQQ__ConstrainingOption__c"), {})
        b = ds.by_id.get(c.get("SBQQ__ConstrainedOption__c"), {})
        an, bn = ds.name(a.get("SBQQ__OptionalSKU__c")), ds.name(b.get("SBQQ__OptionalSKU__c"))
        bundle = ds.name(c.get("SBQQ__ConfiguredSKU__c"))
        it = _item(ds, c, "SBQQ__OptionConstraint__c", f"Option Constraint - {c.get('SBQQ__Type__c')}", "Bundles")
        it["applies_to"] = [bundle]
        it["when"] = f"Enforced live in the configurator for {bundle}."
        if c.get("SBQQ__Type__c") == "Exclusion":
            it["summary"] = f"{an} and {bn} cannot be selected together."
            it["example"] = f"Example: in {bundle}, selecting {an} disables {bn}."
        else:
            it["summary"] = f"{bn} can only be selected when {an} is selected."
            it["example"] = f"Example: in {bundle}, {bn} stays unavailable until {an} is selected."
        it["rca_target"] = "CML constraint: " + ("exclude" if c.get("SBQQ__Type__c") == "Exclusion" else "require")
        for d, rel in ((c.get("SBQQ__ConfiguredSKU__c"), "bundle"), (a.get("SBQQ__OptionalSKU__c"), "constraining"),
                       (b.get("SBQQ__OptionalSKU__c"), "constrained")):
            it["dependencies"].append(_dep(ds, d, rel))
        items.append(_finish(it, 1))
    return items


# ------------------------------------------------------------------ pricing
def _range(lo, hi) -> str:
    lo, hi = _num(lo), _num(hi)
    if hi is None:
        return f"{_fmt(lo)}+"
    return f"{_fmt(lo)} to {_fmt(hi - 1) if isinstance(hi, int) else _fmt(hi)}"


def explain_discount_schedules(ds: Dataset) -> list[dict]:
    items = []
    users = {}
    for p in ds.get("Product2"):
        if p.get("SBQQ__DiscountSchedule__c"):
            users.setdefault(p["SBQQ__DiscountSchedule__c"], []).append(p["Id"])
    for s in ds.get("SBQQ__DiscountSchedule__c"):
        tiers = ds.children("SBQQ__DiscountTier__c", "SBQQ__Schedule__c", s["Id"], sort="SBQQ__LowerBound__c")
        prods = set(users.get(s["Id"], []))
        if s.get("SBQQ__Product__c"):
            prods.add(s["SBQQ__Product__c"])
        it = _item(ds, s, "SBQQ__DiscountSchedule__c", f"Discount Schedule - {s.get('SBQQ__Type__c')}", "Pricing")
        unit = s.get("SBQQ__DiscountUnit__c") or "Percent"
        sym = "%" if unit == "Percent" else ""
        it["applies_to"] = [ds.name(p) for p in prods] or ["(not used by any product)"]
        how = ("every unit gets the discount of the tier the total quantity falls in" if s.get("SBQQ__Type__c") == "Range"
               else "each tier's units are discounted at that tier's rate (like tax brackets)")
        it["summary"] = f"{s.get('SBQQ__Type__c')} volume discount ({unit.lower()}): {how}."
        it["when"] = f"Applied during quote calculation; quantities aggregated per {(s.get('SBQQ__AggregationScope__c') or 'Quote').lower()}."
        for t in tiers:
            val = t.get("SBQQ__Discount__c") if unit == "Percent" else t.get("SBQQ__DiscountAmount__c") or t.get("SBQQ__Price__c")
            it["details"].append(f"{_range(t.get('SBQQ__LowerBound__c'), t.get('SBQQ__UpperBound__c'))} units: {_fmt(_num(val))}{sym}")
            it["source"]["children"].append(t["Id"])
        # worked example
        if tiers and unit == "Percent":
            last = tiers[-1]
            q = int((_num(last.get("SBQQ__LowerBound__c")) or 1) * 1.2) if len(tiers) > 1 else 10
            if s.get("SBQQ__Type__c") == "Range":
                hit = next((t for t in tiers if (_num(t["SBQQ__LowerBound__c"]) or 0) <= q and
                            (t.get("SBQQ__UpperBound__c") is None or q < _num(t["SBQQ__UpperBound__c"]))), tiers[-1])
                it["example"] = f"Example: buying {_fmt(q)} units - all {_fmt(q)} get {_fmt(_num(hit['SBQQ__Discount__c']))}% off."
            else:
                parts, remaining = [], q
                for t in tiers:
                    lo = _num(t["SBQQ__LowerBound__c"]) or 1
                    hi = _num(t.get("SBQQ__UpperBound__c"))
                    size = (hi - lo) if hi is not None else remaining
                    n = min(size, remaining)
                    if n <= 0:
                        break
                    parts.append(f"{_fmt(n)} at {_fmt(_num(t['SBQQ__Discount__c']))}%")
                    remaining -= n
                it["example"] = f"Example: buying {_fmt(q)} units - {_join(parts)}."
        for p in prods:
            it["dependencies"].append(_dep(ds, p, "used by product"))
        if not prods:
            it["notes"].append("Not referenced by any product - candidate for retirement.")
        score = 2 + (2 if s.get("SBQQ__CrossOrders__c") or s.get("SBQQ__CrossProducts__c") else 0) + \
            (2 if (s.get("SBQQ__AggregationScope__c") or "Quote") != "Quote" else 0)
        if s.get("SBQQ__CrossOrders__c"):
            it["notes"].append("Cross-order aggregation needs a custom design in RCA pricing.")
        items.append(_finish(it, score))
    return items


def explain_block_prices(ds: Dataset) -> list[dict]:
    items = []
    by_prod = {}
    for b in ds.get("SBQQ__BlockPrice__c"):
        by_prod.setdefault(b["SBQQ__Product__c"], []).append(b)
    for pid, blocks in by_prod.items():
        blocks.sort(key=lambda b: _num(b.get("SBQQ__LowerBound__c")) or 0)
        p = ds.by_id.get(pid, {"Id": pid, "Name": pid})
        it = _item(ds, {**p, "Id": f"block:{pid}"}, "SBQQ__BlockPrice__c", "Block Pricing", "Pricing")
        it["name"] = f"Block pricing - {ds.name(pid)}"
        it["active"] = True
        it["applies_to"] = [ds.name(pid)]
        it["summary"] = "Price is a flat amount per quantity block rather than per unit."
        it["when"] = "Applied during quote calculation based on line quantity."
        for b in blocks:
            it["details"].append(f"{_range(b.get('SBQQ__LowerBound__c'), b.get('SBQQ__UpperBound__c'))}: {_fmt(_num(b.get('SBQQ__Price__c')))} flat")
            it["source"]["children"].append(b["Id"])
        b = blocks[min(1, len(blocks) - 1)]
        it["example"] = f"Example: quantity {_fmt(_num(b['SBQQ__LowerBound__c']))} is priced at {_fmt(_num(b['SBQQ__Price__c']))} in total."
        it["dependencies"].append(_dep(ds, pid, "product"))
        it["source"]["id"] = blocks[0]["Id"]
        items.append(_finish(it, 3))
    return items


def explain_contracted_prices(ds: Dataset) -> list[dict]:
    recs = ds.get("SBQQ__ContractedPrice__c")
    if not recs:
        return []
    accts = {r.get("SBQQ__Account__c") for r in recs}
    it = _item(ds, {"Id": "contracted-prices", "Name": "Contracted prices"}, "SBQQ__ContractedPrice__c",
               "Contracted Pricing", "Pricing")
    it["summary"] = f"{len(recs)} account-specific price(s) across {len(accts)} account(s) override list price."
    it["when"] = "Applied during quote calculation when the quote account matches."
    it["applies_to"] = sorted({ds.name(r.get("SBQQ__Product__c")) for r in recs})
    for r in recs[:25]:
        it["details"].append(f"{ds.name(r.get('SBQQ__Product__c'))}: {_fmt(_num(r.get('SBQQ__Price__c')))} "
                             f"({r.get('SBQQ__EffectiveDate__c') or '?'} to {r.get('SBQQ__ExpirationDate__c') or 'open'})")
    it["example"] = "Example: a quote for a contracted account uses the agreed price instead of the price book price."
    it["source"]["children"] = [r["Id"] for r in recs]
    return [_finish(it, 3 + len(recs) // 200)]


# ------------------------------------------------------------------- QCP code
QCP_HOOKS = {
    "onInit": "runs once when the quote loads in the line editor - typically sets default line values",
    "onBeforeCalculate": "runs before CPQ's price waterfall - often queries data or pre-sets discounts",
    "onBeforePriceRules": "runs before price rules are applied",
    "onAfterPriceRules": "runs after price rules are applied",
    "onAfterCalculate": "runs after pricing completes - often computes totals, margin or rollups",
    "isFieldEditable": "controls which line fields users can edit",
    "isFieldVisible": "controls which line fields users can see",
    "isFieldEditableForObject": "controls field editability per object",
    "isFieldVisibleForObject": "controls field visibility per object",
}


def analyse_qcp(code: str) -> dict:
    hooks = [h for h in QCP_HOOKS if re.search(rf"\bexport\s+function\s+{h}\b", code)]
    writes = sorted(set(re.findall(r"record\[['\"](\w+)['\"]\]\s*=(?!=)", code)))
    reads = sorted(set(re.findall(r"\b(\w+__c)\b", code)) - set(writes))
    queries = re.findall(r"conn\.query\(\s*[\"'`](.+?)[\"'`]", code)
    loc = len([l for l in code.splitlines() if l.strip() and not l.strip().startswith(("//", "*", "/*"))])
    return {"hooks": hooks, "writes": writes, "reads": reads, "queries": queries, "loc": loc,
            "callouts": bool(re.search(r"conn\.(apex|requestPost|request)\b", code))}


def explain_qcp(ds: Dataset) -> list[dict]:
    items = []
    for s in ds.get("SBQQ__CustomScript__c"):
        code = s.get("SBQQ__Code__c") or ""
        a = analyse_qcp(code)
        it = _item(ds, s, "SBQQ__CustomScript__c", "Quote Calculator Plugin", "Code & Process")
        it["summary"] = (f"JavaScript plugin ({a['loc']} lines of logic) that customises pricing through "
                         f"{len(a['hooks'])} hook(s); writes {len(a['writes'])} field(s).")
        it["when"] = "Runs inside every quote calculation (line editor and background calculation)."
        it["actions"] = [f"{h}: {QCP_HOOKS[h]}" for h in a["hooks"]]
        if a["writes"]:
            it["details"].append("Fields written: " + ", ".join(a["writes"]))
        if a["reads"]:
            it["details"].append("Fields read: " + ", ".join(a["reads"]))
        for q in a["queries"]:
            it["details"].append(f"SOQL query: {q.strip()}")
        it["fields"] = a["writes"] + a["reads"]
        it["example"] = "See code hooks; behaviour must be reproduced in RCA pricing and verified with side-by-side quote tests."
        it["notes"].append("No direct equivalent in RCA: redesign as Pricing Procedure elements, decision tables or Apex hooks.")
        if a["queries"]:
            it["notes"].append("Plugin queries Salesforce data at run time; plan context definition mappings or lookups.")
        it["qcp"] = a
        it["effort_override"] = 16 + a["loc"] / 5 + 4 * len(a["hooks"])
        score = 8 + a["loc"] // 40 + 2 * len(a["hooks"]) + (3 if a["queries"] else 0)
        items.append(_finish(it, score, floor="Redesign"))
    return items


# --------------------------------------------------- other configuration units
def explain_other(ds: Dataset) -> list[dict]:
    items = []
    for a in ds.get("SBQQ__CustomAction__c"):
        it = _item(ds, a, "SBQQ__CustomAction__c", "Custom Action", "Code & Process")
        it["summary"] = f"{a.get('SBQQ__Type__c')} shown in the {a.get('SBQQ__Location__c')} that users click to run custom behaviour."
        it["when"] = "On user click."
        it["example"] = f"Example: a rep clicks '{a['Name']}' in the {a.get('SBQQ__Location__c')}."
        items.append(_finish(it, 5, floor="Adapt"))
    for qp in ds.get("SBQQ__QuoteProcess__c"):
        inputs = ds.children("SBQQ__ProcessInput__c", "SBQQ__QuoteProcess__c", qp["Id"], sort="SBQQ__DisplayOrder__c")
        it = _item(ds, qp, "SBQQ__QuoteProcess__c", "Guided Selling", "Code & Process")
        it["summary"] = f"Guided selling questionnaire with {len(inputs)} question(s) that filters the product list."
        it["when"] = "When a rep starts adding products" + (" (default process)." if qp.get("SBQQ__Default__c") else ".")
        for i in inputs:
            it["conditions"].append(f"Question '{i['Name']}' -> matches product field {field_label(i.get('SBQQ__ProductField__c'))}")
            it["fields"] += [i.get("SBQQ__InputField__c"), i.get("SBQQ__ProductField__c")]
            it["source"]["children"].append(i["Id"])
        it["example"] = "Example: the rep answers the questions and only matching products are listed."
        items.append(_finish(it, 4 + len(inputs)))
    for t in ds.get("SBQQ__QuoteTemplate__c"):
        secs = ds.children("SBQQ__TemplateSection__c", "SBQQ__Template__c", t["Id"], sort="SBQQ__DisplayOrder__c")
        it = _item(ds, t, "SBQQ__QuoteTemplate__c", "Quote Template", "Documents & Approvals")
        it["summary"] = f"Quote output document with {len(secs)} section(s)" + (" (default template)." if t.get("SBQQ__Default__c") else ".")
        it["when"] = "When a rep generates the quote document."
        for s in secs:
            content = ds.by_id.get(s.get("SBQQ__Content__c"), {})
            it["details"].append(f"{s['Name']} ({content.get('SBQQ__Type__c', '?')})")
            it["source"]["children"].append(s["Id"])
        it["example"] = "Example: rep clicks Generate Document and receives a PDF proposal."
        items.append(_finish(it, 4 + len(secs), floor="Adapt"))
    for r in ds.get("sbaa__ApprovalRule__c"):
        conds = ds.children("sbaa__ApprovalCondition__c", "sbaa__ApprovalRule__c", r["Id"])
        it = _item(ds, r, "sbaa__ApprovalRule__c", "Approval Rule", "Documents & Approvals")
        it["when"] = f"On quote submission for approval (step {_fmt(_num(r.get('sbaa__ApprovalStep__c')))})."
        ex = []
        for c in conds:
            t, e = _condition_text(ds, "Quote", c.get("sbaa__TestedField__c"), None, c.get("sbaa__Operator__c"),
                                   c.get("sbaa__FilterValue__c"))
            it["conditions"].append(t)
            ex.append(e)
            it["fields"].append(c.get("sbaa__TestedField__c"))
            it["source"]["children"].append(c["Id"])
        it["logic"] = _logic(r.get("sbaa__ConditionsMet__c"), None, len(conds))
        it["summary"] = f"Requires approval: {r['Name']}."
        it["example"] = f"Example: a quote where {_join(ex)} is routed for approval."
        it["notes"].append("Advanced Approvals rules are rebuilt as Flow approval processes.")
        items.append(_finish(it, 5, floor="Redesign"))
    return items


def explain_search_filters(ds: Dataset) -> list[dict]:
    items = []
    for f in ds.get("SBQQ__SearchFilter__c"):
        it = _item(ds, f, "SBQQ__SearchFilter__c", "Search Filter", "Code & Process")
        target = field_label(f.get("SBQQ__TargetField__c")) or "a product field"
        op = OPS.get((f.get("SBQQ__Operator__c") or "equals").lower(), f.get("SBQQ__Operator__c"))
        value = f.get("SBQQ__FilterValue__c")
        action = ds.name(f.get("SBQQ__Action__c")) if f.get("SBQQ__Action__c") else "the product search"
        hidden = f.get("SBQQ__Hidden__c")
        it["summary"] = (f"{'Hidden' if hidden else 'Visible'} filter on {action}: only products where "
                         f"{target} {op} '{value}' are listed.")
        it["when"] = "When a rep searches for products to add to a quote."
        it["conditions"].append(f"{target} {op} '{value}'")
        it["fields"].append(f.get("SBQQ__TargetField__c"))
        it["example"] = (f"Example: a rep opens {action} and sees only products whose {target} {op} "
                         f"'{value}'" + (" without being able to change the filter." if hidden else "."))
        dep = _dep(ds, f.get("SBQQ__Action__c"), "filters")
        if dep:
            it["dependencies"].append(dep)
        items.append(_finish(it, 3))
    return items


def explain_quote_terms(ds: Dataset) -> list[dict]:
    items = []
    quote_specific = 0
    for t in ds.get("SBQQ__QuoteTerm__c"):
        if t.get("SBQQ__Quote__c"):
            quote_specific += 1  # edited copy on one quote: transactional, not configuration
            continue
        conds = ds.children("SBQQ__TermCondition__c", "SBQQ__QuoteTerm__c", t["Id"], sort="SBQQ__Index__c")
        it = _item(ds, t, "SBQQ__QuoteTerm__c", "Quote Term", "Documents & Approvals")
        status = t.get("SBQQ__Status__c")
        if status and status not in ("Approved", "Active"):
            it["active"] = False
        body = re.sub(r"<[^>]+>", " ", t.get("SBQQ__Body__c") or "")
        body = re.sub(r"\s+", " ", body).strip()
        it["summary"] = (f"Contract clause printed in the quote document"
                         + (" when its conditions are met." if conds else " on every quote.")
                         + (" Locked: reps cannot edit it." if t.get("SBQQ__Locked__c") else ""))
        it["when"] = "When the quote document is generated."
        ex = []
        for c in conds:
            txt, e = _condition_text(ds, "Quote", c.get("SBQQ__TestedField__c"), c.get("SBQQ__TestedVariable__c"),
                                     c.get("SBQQ__Operator__c"), c.get("SBQQ__Value__c"))
            it["conditions"].append(txt)
            ex.append(e)
            it["fields"].append(c.get("SBQQ__TestedField__c"))
            it["source"]["children"].append(c["Id"])
            dep = _dep(ds, c.get("SBQQ__TestedVariable__c"), "tests")
            if dep:
                it["dependencies"].append(dep)
        it["logic"] = _logic(t.get("SBQQ__ConditionsMet__c"), None, len(conds)) if conds else ""
        if body:
            it["details"].append(f"Text: {body[:300]}{'...' if len(body) > 300 else ''}")
        if t.get("SBQQ__PrintOrder__c") is not None:
            it["details"].append(f"Print order: {_fmt(_num(t['SBQQ__PrintOrder__c']))}")
        it["example"] = (f"Example: on a quote where {_join([e for e in ex if e])}, the document includes this clause."
                         if any(ex) else "Example: every generated proposal includes this clause.")
        items.append(_finish(it, 2 + 2 * len(conds), floor="Adapt" if conds else None))
    if quote_specific and items:
        items[0]["notes"].append(f"{quote_specific} quote-specific term edit(s) were not treated as configuration.")
    return items


def explain_localizations(ds: Dataset) -> list[dict]:
    """One item per language: translations can number in the thousands."""
    by_lang: dict[str, list[dict]] = {}
    for r in ds.get("SBQQ__Localization__c"):
        by_lang.setdefault(r.get("SBQQ__Language__c") or "(no language)", []).append(r)
    items = []
    kinds = (("SBQQ__Product__c", "products"), ("SBQQ__ProductFeature__c", "features"),
             ("SBQQ__ProductOption__c", "options"), ("SBQQ__ProductRule__c", "product rule messages"),
             ("SBQQ__QuoteTemplate__c", "quote templates"), ("SBQQ__TemplateContent__c", "template content"))
    for lang, recs in sorted(by_lang.items()):
        rec = {"Id": f"localization-{lang}", "Name": f"Translations: {lang}"}
        it = _item(ds, rec, "SBQQ__Localization__c", "Localization", "Catalog")
        covered = [f"{sum(1 for r in recs if r.get(f))} {label}" for f, label in kinds if any(r.get(f) for r in recs)]
        it["summary"] = f"{len(recs)} CPQ translation(s) into {lang}" + (f", covering {_join(covered)}." if covered else ".")
        it["when"] = f"When a quote or catalog is shown to users or customers in {lang}."
        it["details"] = [f"{r.get('SBQQ__Label__c') or r.get('SBQQ__APIName__c') or r['Id']}: "
                         f"{(r.get('SBQQ__Text__c') or '')[:80]}" for r in recs[:20]]
        if len(recs) > 20:
            it["details"].append(f"... and {len(recs) - 20} more")
        it["source"]["children"] = [r["Id"] for r in recs]
        it["example"] = f"Example: a customer receives a proposal with product names in {lang}."
        it["fields"] = [r.get("SBQQ__APIName__c") for r in recs]
        items.append(_finish(it, 3 + min(4, len(recs) // 100), floor="Adapt"))
    return items


def _a(word: str) -> str:
    return f"{'an' if word[:1].lower() in 'aeiou' else 'a'} {word}"


def _obj_label(obj: str) -> str:
    spec = BY_NAME.get(obj)
    return spec.label if spec else re.sub(r"(?<=[a-z])(?=[A-Z])", " ", obj)


def explain_automation(ds: Dataset) -> list[dict]:
    """Custom Flows, Apex triggers and validation rules on CPQ objects. They hold
    pricing and quoting logic outside CPQ's own configuration and stop running
    once quoting moves to the RCA objects."""
    auto = ds.automation or {}
    items = []

    def base(rec, typ, obj):
        moved = obj in RCA_OBJECT or obj.startswith(("SBQQ__", "sbaa__"))
        target = RCA_OBJECT.get(obj) or (BY_NAME[obj].rca_target if obj in BY_NAME else obj)
        it = _item(ds, {"Id": rec["id"], "Name": rec["name"]}, obj, typ, "Automation",
                   active=rec.get("active", True) is not False, rca_target=target)
        it["source"] = {"object": obj, "id": rec["id"], "children": [], "automation": True}
        return it, moved, target

    for t in auto.get("apex_triggers", []):
        obj = t["object"]
        it, moved, target = base(t, "Apex Trigger", obj)
        events = ", ".join(t.get("events") or []) or "save"
        it["summary"] = (f"Custom Apex code that runs on {_obj_label(obj)} records ({events})"
                         + (f", about {t['lines']:,} characters of code." if t.get("lines") else "."))
        it["when"] = f"Whenever {_a(_obj_label(obj))} record is {'saved or deleted' if 'Delete' in events else 'saved'}."
        it["example"] = f"Example: a rep saves {_a(_obj_label(obj))} and the trigger '{t['name']}' runs custom logic."
        it["notes"].append("The trigger body is not extracted - review the code for pricing or quoting logic.")
        if moved:
            it["notes"].append(f"Records move to {target} in RCA, so this trigger stops running and must be rebuilt.")
        if t.get("api_version") and float(t["api_version"]) < 50:
            it["notes"].append(f"Old API version {t['api_version']}.")
        items.append(_finish(it, 9 if moved else 5, floor="Redesign" if moved else "Adapt"))

    for v in auto.get("validation_rules", []):
        obj = v["object"]
        it, moved, target = base(v, "Validation Rule", obj)
        msg = (v.get("error_message") or "").strip()
        it["summary"] = f"Stops {_a(_obj_label(obj))} from being saved" + (f" with the message: '{msg}'" if msg else "") + "."
        it["when"] = f"Whenever {_a(_obj_label(obj))} record is saved."
        if v.get("description"):
            it["details"].append(f"Description: {v['description']}")
        if v.get("error_field"):
            it["fields"].append(v["error_field"])
        it["example"] = f"Example: a rep saves {_a(_obj_label(obj))} that breaks the rule and sees '{msg or 'an error'}'."
        if moved:
            it["notes"].append(f"Recreate the rule on {target}; field names change, so rewrite the formula.")
        items.append(_finish(it, 4 if moved else 2, floor="Adapt" if moved else None))

    for f in auto.get("flows", []):
        obj = f["object"]
        it, moved, target = base(f, f"Flow ({f.get('process_type') or 'Flow'})", obj)
        pb = f.get("process_type") == "Workflow"
        kind = "Process Builder process" if pb else "Record-triggered flow"
        it["summary"] = f"{kind} on {_obj_label(obj)}" + (f": {f['description']}" if f.get("description") else ".")
        it["when"] = {"RecordBeforeSave": "Before a record is saved.", "RecordAfterSave": "After a record is saved.",
                      "RecordBeforeDelete": "Before a record is deleted."}.get(f.get("trigger_type"),
                                                                                "When a record changes.")
        it["details"].append(f"API name: {f.get('api_name')}")
        it["example"] = f"Example: a rep saves {_a(_obj_label(obj))} and '{f['name']}' updates related data."
        if pb:
            it["notes"].append("Process Builder is retired by Salesforce - rebuild as a record-triggered flow.")
        if moved:
            it["notes"].append(f"Records move to {target} in RCA, so this flow stops running and must be rebuilt.")
        items.append(_finish(it, 8 if (moved or pb) else 4, floor="Redesign" if (moved or pb) else "Adapt"))
    return items


# ------------------------------------------------------------------- entry
def explain_all(ds: Dataset) -> list[dict]:
    items = []
    for fn in (explain_bundles, explain_option_constraints, explain_product_rules,
               explain_summary_variables, explain_price_rules, explain_discount_schedules,
               explain_block_prices, explain_contracted_prices, explain_qcp, explain_other,
               explain_search_filters, explain_quote_terms, explain_localizations, explain_automation):
        items.extend(fn(ds))
    # back-references: which rules use each summary variable / product
    used_by: dict[str, list[str]] = {}
    for it in items:
        for d in it["dependencies"]:
            used_by.setdefault(d["id"], []).append(it["id"])
    for it in items:
        it["used_by"] = [u for u in used_by.get(it["id"], []) if u != it["id"]]
        unused_sched = it["type"].startswith("Discount Schedule") and it["applies_to"] == ["(not used by any product)"]
        if (it["type"] == "Summary Variable" and not it["used_by"]) or unused_sched:
            if not unused_sched:
                it["notes"].append("Not referenced by any rule - candidate for retirement.")
            if it["approach"] != "Retire":
                it["approach"], it["effort_hours"] = "Retire", EFFORT["Retire"]
        it["custom_fields"] = [f for f in it["fields"] if is_custom_field(f)]
    return items
