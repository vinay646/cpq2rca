"""Generate a realistic synthetic CPQ org export ("Northwind Cloud", fictional).

The dataset deliberately includes common real-world issues (inactive rules,
rules without conditions, unused summary variables and schedules, colliding
evaluation orders, inactive products inside bundles) so the assessment has
something to find.

    python sample_data/generate.py  ->  sample_data/northwind/*.json
"""

from __future__ import annotations

import itertools
import json
from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).parent / "northwind"
DATA: dict[str, list[dict]] = defaultdict(list)
PREFIX = {"Product2": "01t", "Pricebook2": "01s", "PricebookEntry": "01u", "Account": "001"}
_custom = itertools.count(10)
_seq = defaultdict(itertools.count)


def new_id(obj: str) -> str:
    if obj not in PREFIX:
        PREFIX[obj] = f"a{next(_custom):02d}"[:3]
    return f"{PREFIX[obj]}5g00000{next(_seq[obj]) + 1:05d}AAA"


def add(obj: str, **fields) -> str:
    rid = new_id(obj)
    DATA[obj].append({"Id": rid, **fields})
    return rid


# ------------------------------------------------------------------ pricebooks
PB_STD = add("Pricebook2", Name="Standard Price Book", IsActive=True, IsStandard=True, Description=None)
PB_USD = add("Pricebook2", Name="USD Direct 2026", IsActive=True, IsStandard=False,
             Description="Direct sales list prices, USD")

# ------------------------------------------------------------------- products
P = {}


def product(code, name, family, price, **kw):
    base = dict(Name=name, ProductCode=code, Family=family, IsActive=True, Description=None,
                SBQQ__ConfigurationType__c=None, SBQQ__ConfigurationEvent__c=None,
                SBQQ__PricingMethod__c="List", SBQQ__SubscriptionPricing__c=None,
                SBQQ__SubscriptionTerm__c=None, SBQQ__SubscriptionType__c=None,
                SBQQ__ChargeType__c="One-Time", SBQQ__BillingType__c=None,
                SBQQ__BillingFrequency__c=None, SBQQ__DiscountSchedule__c=None,
                SBQQ__NonDiscountable__c=False, SBQQ__QuantityEditable__c=True,
                SBQQ__AssetConversion__c="One per quote line", SBQQ__BlockPricingField__c=None,
                SBQQ__ExternallyConfigurable__c=False)
    base.update(kw)
    pid = add("Product2", **base)
    P[code] = pid
    for pb in (PB_STD, PB_USD):
        add("PricebookEntry", Name=name, Pricebook2Id=pb, Product2Id=pid,
            UnitPrice=price if pb == PB_USD else price, IsActive=base["IsActive"])
    return pid


SUB = dict(SBQQ__SubscriptionPricing__c="Fixed Price", SBQQ__SubscriptionTerm__c=12,
           SBQQ__SubscriptionType__c="Renewable", SBQQ__ChargeType__c="Recurring",
           SBQQ__BillingType__c="Advance", SBQQ__BillingFrequency__c="Annual")
BUNDLE = dict(SBQQ__ConfigurationType__c="Required", SBQQ__ConfigurationEvent__c="Always")

product("BND-PRO", "Cloud Platform - Professional", "Platform", 0, **BUNDLE, **SUB,
        Description="Professional edition bundle for mid-market customers")
product("BND-ENT", "Cloud Platform - Enterprise", "Platform", 0, **BUNDLE, **SUB,
        Description="Enterprise edition bundle with premium support and SSO")
product("BND-EDGE", "Edge Appliance Kit", "Hardware", 0, **BUNDLE,
        Description="On-premise edge appliance with accessories")
product("LIC-CORE", "Core Platform License (per user)", "Platform", 1200, **SUB)
product("ADD-ANL", "Analytics Add-on", "Add-on", 300, **SUB)
product("ADD-AI", "AI Assistant", "Add-on", 450, **SUB)
product("ADD-SSO", "SSO Connector", "Add-on", 5000, **{**SUB, "SBQQ__QuantityEditable__c": False})
product("ADD-SBX", "Full Sandbox Environment", "Add-on", 15000, **SUB)
product("ADD-API", "API Calls Pack (10k)", "Add-on", 100, **SUB)
product("STO-1TB", "Data Storage (TB)", "Storage", 0, **{**SUB, "SBQQ__PricingMethod__c": "Block"})
product("SUP-STD", "Standard Support", "Support", 0, **{**SUB, "SBQQ__SubscriptionPricing__c": "Percent Of Total"})
product("SUP-PREM", "Premium Support", "Support", 0, **{**SUB, "SBQQ__SubscriptionPricing__c": "Percent Of Total"})
product("SRV-IMPL", "Implementation Services", "Services", 5000)
product("SRV-TRN", "Training Day", "Services", 2500)
product("HW-EDGE", "Edge Appliance E200", "Hardware", 8900)
product("HW-RACK", "Rack Mount Kit", "Hardware", 350)
product("HW-PSU", "Redundant Power Supply", "Hardware", 600)
product("LEG-RPT", "Legacy Reporting Module", "Add-on", 200, **{**SUB, "IsActive": False})

# ------------------------------------------------------- discount schedules
def schedule(name, typ, unit, product_code, tiers, desc=None):
    sid = add("SBQQ__DiscountSchedule__c", Name=name, SBQQ__Type__c=typ, SBQQ__DiscountUnit__c=unit,
              SBQQ__AggregationScope__c="Quote", SBQQ__CrossOrders__c=False,
              SBQQ__CrossProducts__c=False,
              SBQQ__Product__c=P.get(product_code), SBQQ__Description__c=desc)
    for i, (lo, hi, disc) in enumerate(tiers, 1):
        add("SBQQ__DiscountTier__c", Name=f"Tier {i}", SBQQ__Schedule__c=sid, SBQQ__Number__c=i,
            SBQQ__LowerBound__c=lo, SBQQ__UpperBound__c=hi, SBQQ__Discount__c=disc,
            SBQQ__DiscountAmount__c=None, SBQQ__Price__c=None)
    return sid


DS_API = schedule("API Calls Volume", "Range", "Percent", "ADD-API",
                  [(1, 100, 0), (100, 500, 10), (500, 1000, 15), (1000, None, 25)],
                  "Volume discount on API packs")
DS_USERS = schedule("User Slab Pricing", "Slab", "Percent", "LIC-CORE",
                    [(1, 51, 0), (51, 201, 8), (201, 1001, 15), (1001, None, 22)],
                    "Slab discount for per-user licenses")
schedule("Legacy Hardware Schedule", "Range", "Percent", None,
         [(1, 10, 0), (10, None, 5)], "No longer referenced")
for r in DATA["Product2"]:
    if r["Id"] == P["ADD-API"]:
        r["SBQQ__DiscountSchedule__c"] = DS_API
    if r["Id"] == P["LIC-CORE"]:
        r["SBQQ__DiscountSchedule__c"] = DS_USERS

# --------------------------------------------------------------- block prices
for lo, hi, price in [(1, 6, 2000), (6, 11, 3500), (11, 21, 6000), (21, None, 9500)]:
    add("SBQQ__BlockPrice__c", Name=f"Storage {lo}-{hi or '+'} TB", SBQQ__Product__c=P["STO-1TB"],
        SBQQ__PriceBook2__c=PB_USD, SBQQ__LowerBound__c=lo, SBQQ__UpperBound__c=hi, SBQQ__Price__c=price)

# ---------------------------------------------------- contracted prices/costs
ACCTS = [add("Account", Name=n) for n in ("Contoso Health", "Fabrikam Retail", "Tailspin Air")]
for acct, code, price in [(ACCTS[0], "LIC-CORE", 1000), (ACCTS[1], "ADD-ANL", 250),
                          (ACCTS[2], "HW-EDGE", 7900)]:
    add("SBQQ__ContractedPrice__c", Name=f"CP-{code}", SBQQ__Account__c=acct, SBQQ__Product__c=P[code],
        SBQQ__Price__c=price, SBQQ__Discount__c=None, SBQQ__EffectiveDate__c="2026-01-01",
        SBQQ__ExpirationDate__c="2026-12-31")
for code, cost in [("HW-EDGE", 5200), ("HW-RACK", 120), ("HW-PSU", 210)]:
    add("SBQQ__Cost__c", Name=f"Cost {code}", SBQQ__Product__c=P[code], SBQQ__UnitCost__c=cost,
        SBQQ__Active__c=True)
add("SBQQ__Dimension__c", Name="Yearly", SBQQ__Product__c=P["LIC-CORE"], SBQQ__Type__c="Year",
    SBQQ__PriceEditable__c=True)

# -------------------------------------------------------------------- bundles
F, O = {}, {}


def feature(bundle, name, num, mn=None, mx=None):
    fid = add("SBQQ__ProductFeature__c", Name=name, SBQQ__ConfiguredSKU__c=P[bundle],
              SBQQ__Category__c=None, SBQQ__Number__c=num,
              SBQQ__MinOptionCount__c=mn, SBQQ__MaxOptionCount__c=mx)
    F[(bundle, name)] = fid
    return fid


def option(bundle, feat, code, num, qty=1, required=False, selected=False, typ="Component", **kw):
    oid = add("SBQQ__ProductOption__c", Name=f"{bundle}:{code}",
              SBQQ__ConfiguredSKU__c=P[bundle], SBQQ__OptionalSKU__c=P[code],
              SBQQ__Feature__c=F[(bundle, feat)], SBQQ__Number__c=num, SBQQ__Quantity__c=qty,
              SBQQ__MinQuantity__c=kw.get("mn"), SBQQ__MaxQuantity__c=kw.get("mx"),
              SBQQ__Required__c=required, SBQQ__Selected__c=selected, SBQQ__Type__c=typ,
              SBQQ__Bundled__c=kw.get("bundled", False), SBQQ__QuantityEditable__c=not required,
              SBQQ__DiscountedByPackage__c=False)
    O[(bundle, code)] = oid
    return oid


for b in ("BND-PRO", "BND-ENT"):
    feature(b, "Licenses", 10, 1, 1)
    feature(b, "Add-ons", 20)
    feature(b, "Support", 30, 1, 1)
    feature(b, "Services", 40)
    option(b, "Licenses", "LIC-CORE", 10, qty=10, required=True, selected=True, mn=1)
    option(b, "Add-ons", "ADD-ANL", 20)
    option(b, "Add-ons", "ADD-AI", 30)
    option(b, "Add-ons", "ADD-API", 40)
    option(b, "Add-ons", "STO-1TB", 50)
    option(b, "Support", "SUP-STD", 60, selected=(b == "BND-PRO"))
    option(b, "Support", "SUP-PREM", 70, selected=(b == "BND-ENT"))
    option(b, "Services", "SRV-IMPL", 80, typ="Accessory")
    option(b, "Services", "SRV-TRN", 90, typ="Accessory")
option("BND-PRO", "Add-ons", "LEG-RPT", 99)  # inactive product still in bundle
option("BND-ENT", "Add-ons", "ADD-SSO", 35, required=True, selected=True, bundled=True)
option("BND-ENT", "Add-ons", "ADD-SBX", 45)
feature("BND-EDGE", "Hardware", 10, 1, None)
feature("BND-EDGE", "Accessories", 20)
option("BND-EDGE", "Hardware", "HW-EDGE", 10, required=True, selected=True, mn=1, mx=50)
option("BND-EDGE", "Accessories", "HW-RACK", 20)
option("BND-EDGE", "Accessories", "HW-PSU", 30)

for b in ("BND-PRO", "BND-ENT"):
    add("SBQQ__OptionConstraint__c", Name=f"{b} support exclusive",
        SBQQ__ConfiguredSKU__c=P[b], SBQQ__ConstrainingOption__c=O[(b, "SUP-PREM")],
        SBQQ__ConstrainedOption__c=O[(b, "SUP-STD")], SBQQ__Type__c="Exclusion", SBQQ__Active__c=True)
    add("SBQQ__OptionConstraint__c", Name=f"{b} AI needs Analytics",
        SBQQ__ConfiguredSKU__c=P[b], SBQQ__ConstrainingOption__c=O[(b, "ADD-ANL")],
        SBQQ__ConstrainedOption__c=O[(b, "ADD-AI")], SBQQ__Type__c="Dependency", SBQQ__Active__c=True)
add("SBQQ__OptionConstraint__c", Name="Rack kit needs appliance",
    SBQQ__ConfiguredSKU__c=P["BND-EDGE"], SBQQ__ConstrainingOption__c=O[("BND-EDGE", "HW-EDGE")],
    SBQQ__ConstrainedOption__c=O[("BND-EDGE", "HW-RACK")], SBQQ__Type__c="Dependency",
    SBQQ__Active__c=True)

add("SBQQ__ConfigurationAttribute__c", Name="Deployment Region", SBQQ__Product__c=P["BND-ENT"],
    SBQQ__Feature__c=F[("BND-ENT", "Licenses")], SBQQ__TargetField__c="Deployment_Region__c",
    SBQQ__Required__c=True, SBQQ__AppliedImmediately__c=True, SBQQ__DefaultField__c=None)
add("SBQQ__ConfigurationAttribute__c", Name="Contract Term (months)", SBQQ__Product__c=P["BND-PRO"],
    SBQQ__Feature__c=F[("BND-PRO", "Licenses")], SBQQ__TargetField__c="SBQQ__SubscriptionTerm__c",
    SBQQ__Required__c=False, SBQQ__AppliedImmediately__c=False,
    SBQQ__DefaultField__c="SBQQ__SubscriptionTerm__c")

# ----------------------------------------------------------- summary variables
def sv(name, fn, field, target="Quote Line", ff=None, op=None, fv=None, scope="Quote"):
    return add("SBQQ__SummaryVariable__c", Name=name, SBQQ__AggregateFunction__c=fn,
               SBQQ__AggregateField__c=field, SBQQ__TargetObject__c=target,
               SBQQ__FilterField__c=ff, SBQQ__Operator__c=op, SBQQ__FilterValue__c=fv,
               SBQQ__CombineWith__c=None, SBQQ__CompositeOperator__c=None, SBQQ__Scope__c=scope)


SV_USERS = sv("Total Users", "Sum", "SBQQ__Quantity__c", ff="SBQQ__ProductCode__c", op="equals",
              fv="LIC-CORE")
SV_HW = sv("Hardware Units", "Sum", "SBQQ__Quantity__c", ff="SBQQ__ProductFamily__c", op="equals",
           fv="Hardware")
SV_OPT_USERS = sv("Configured Users", "Sum", "SBQQ__Quantity__c", target="Product Option",
                  ff="SBQQ__ProductCode__c", op="equals", fv="LIC-CORE", scope="Configurator")
sv("Total Services Amount", "Sum", "SBQQ__NetTotal__c", ff="SBQQ__ProductFamily__c",
   op="equals", fv="Services")  # never referenced

# --------------------------------------------------------------- product rules
def prule(name, typ, scope, events, order, met="All", adv=None, msg=None, active=True, lookup=None,
          desc=None):
    return add("SBQQ__ProductRule__c", Name=name, SBQQ__Active__c=active, SBQQ__Type__c=typ,
               SBQQ__Scope__c=scope, SBQQ__EvaluationEvent__c=events,
               SBQQ__EvaluationOrder__c=order, SBQQ__ConditionsMet__c=met,
               SBQQ__AdvancedCondition__c=adv, SBQQ__ErrorMessage__c=msg,
               SBQQ__LookupObject__c=lookup, SBQQ__Description__c=desc)


def econd(rule, idx, obj, field=None, op="equals", value=None, var=None, fvar=None, ftype="Value"):
    add("SBQQ__ErrorCondition__c", Name=f"C{idx}", SBQQ__Rule__c=rule, SBQQ__Index__c=idx,
        SBQQ__TestedObject__c=obj, SBQQ__TestedField__c=field, SBQQ__TestedVariable__c=var,
        SBQQ__Operator__c=op, SBQQ__FilterType__c=ftype, SBQQ__FilterValue__c=value,
        SBQQ__FilterVariable__c=fvar)


def paction(rule, typ, code=None, required=False, ff=None, op=None, fv=None):
    add("SBQQ__ProductAction__c", Name=typ, SBQQ__Rule__c=rule, SBQQ__Type__c=typ,
        SBQQ__Product__c=P.get(code), SBQQ__Required__c=required, SBQQ__FilterField__c=ff,
        SBQQ__Operator__c=op, SBQQ__FilterValue__c=fv, SBQQ__ValueField__c=None)


def cfg_rule(rule, bundle, feat=None):
    add("SBQQ__ConfigurationRule__c", Name=f"{bundle} rule", SBQQ__Product__c=P[bundle],
        SBQQ__ProductRule__c=rule, SBQQ__ProductFeature__c=F.get((bundle, feat)) if feat else None,
        SBQQ__Active__c=True)


r = prule("Enterprise minimum 50 users", "Validation", "Product", "Always", 10,
          msg="Enterprise edition requires at least 50 Core Platform users.")
econd(r, 1, "Summary Variable", var=SV_OPT_USERS, op="less than", value="50")
cfg_rule(r, "BND-ENT")

r = prule("Large hardware order review", "Alert", "Quote", "Save", 20,
          msg="Orders above 20 edge appliances require supply-chain confirmation of lead times.")
econd(r, 1, "Summary Variable", var=SV_HW, op="greater than", value="20")

r = prule("Enterprise includes Premium Support", "Selection", "Product", "Always", 30)
econd(r, 1, "Product Option", "SBQQ__ProductCode__c", "equals", "SUP-STD")
paction(r, "Disable & Remove", "SUP-STD")
paction(r, "Enable & Add", "SUP-PREM", required=True)
cfg_rule(r, "BND-ENT", "Support")

r = prule("Redundant PSU for 10+ appliances", "Selection", "Product", "Edit", 40)
econd(r, 1, "Product Option", "SBQQ__ProductCode__c", "equals", "HW-EDGE")
econd(r, 2, "Product Option", "SBQQ__Quantity__c", "greater or equals", "10")
paction(r, "Add", "HW-PSU", required=True)
cfg_rule(r, "BND-EDGE")

r = prule("Storage options for regulated industries", "Filter", "Product", "Always", 50)
econd(r, 1, "Quote", "Account_Industry__c", "equals", "Healthcare")
paction(r, "Default Filter", ff="Family", op="equals", fv="Storage")
cfg_rule(r, "BND-ENT", "Add-ons")

r = prule("Partner discount cap", "Validation", "Quote", "Save", 60, met="Custom",
          adv="1 AND (2 OR 3)",
          msg="Silver partners cannot exceed 15% additional discount without deal desk approval.")
econd(r, 1, "Quote", "Partner_Tier__c", "equals", "Silver")
econd(r, 2, "Quote", "SBQQ__CustomerDiscount__c", "greater than", "15")
econd(r, 3, "Quote Line", "SBQQ__AdditionalDiscount__c", "greater than", "15")

r = prule("Block Legacy Reporting", "Validation", "Quote", "Save", 70, active=False,
          msg="Legacy Reporting is end-of-sale.")
econd(r, 1, "Quote Line", "SBQQ__ProductCode__c", "equals", "LEG-RPT")

prule("Remind rep to attach SOW", "Alert", "Quote", "Save", 80,
      msg="Remember to attach the signed SOW for services.")  # no conditions

r = prule("Region-specific add-ons", "Selection", "Product", "Always", 90,
          lookup="Region_Product_Map__c")
econd(r, 1, "Configuration Attributes", "Deployment_Region__c", "not equals", "")
add("SBQQ__LookupQuery__c", Name="Region match", SBQQ__ProductRule__c=r, SBQQ__PriceRule2__c=None,
    SBQQ__TestedField__c="Deployment_Region__c", SBQQ__TestedValue__c=None,
    SBQQ__Operator__c="equals", SBQQ__LookupField__c="Region__c", SBQQ__MatchType__c="Field Value")
paction(r, "Add", None)
cfg_rule(r, "BND-ENT")

# ----------------------------------------------------------------- price rules
def price_rule(name, target, event, order, met="All", adv=None, active=True, lookup=None,
               cfg_event=None, product_code=None):
    return add("SBQQ__PriceRule__c", Name=name, SBQQ__Active__c=active, SBQQ__TargetObject__c=target,
               SBQQ__EvaluationEvent__c=event, SBQQ__ConfiguratorEvaluationEvent__c=cfg_event,
               SBQQ__EvaluationOrder__c=order, SBQQ__ConditionsMet__c=met,
               SBQQ__AdvancedCondition__c=adv, SBQQ__Product__c=P.get(product_code),
               SBQQ__LookupObject__c=lookup)


def pcond(rule, idx, obj, field=None, op="equals", value=None, var=None, formula=None):
    add("SBQQ__PriceCondition__c", Name=f"C{idx}", SBQQ__Rule__c=rule, SBQQ__Index__c=idx,
        SBQQ__Object__c=obj, SBQQ__Field__c=field, SBQQ__TestedVariable__c=var,
        SBQQ__TestedFormula__c=formula, SBQQ__Operator__c=op, SBQQ__FilterType__c="Value",
        SBQQ__Value__c=value, SBQQ__FilterVariable__c=None, SBQQ__FilterFormula__c=None)


def pact(rule, target, field, value=None, formula=None, var=None, lookup_field=None, order=1):
    add("SBQQ__PriceAction__c", Name=field, SBQQ__Rule__c=rule, SBQQ__TargetObject__c=target,
        SBQQ__Field__c=field, SBQQ__Value__c=value, SBQQ__ValueField__c=None,
        SBQQ__Formula__c=formula, SBQQ__SourceVariable__c=var,
        SBQQ__SourceLookupField__c=lookup_field, SBQQ__Order__c=order)


r = price_rule("Implementation fee scales with users", "Calculator", "On Calculate", 20)
pcond(r, 1, "Quote Line", "SBQQ__ProductCode__c", "equals", "SRV-IMPL")
pact(r, "Quote Line", "SBQQ__SpecialPrice__c", formula="MAX(5000, Total_Users__c * 25)")

r = price_rule("Education sector discount", "Calculator", "Before Calculate", 10)
pcond(r, 1, "Quote", "Account_Industry__c", "equals", "Education")
pcond(r, 2, "Quote Line", "SBQQ__ProductFamily__c", "not equals", "Hardware")
pact(r, "Quote Line", "SBQQ__AdditionalDiscount__c", value="20")

r = price_rule("Regional price uplift", "Calculator", "On Calculate", 20,
               lookup="Regional_Uplift__c")
pcond(r, 1, "Quote", "Deployment_Region__c", "not equals", "US")
add("SBQQ__LookupQuery__c", Name="Region", SBQQ__ProductRule__c=None, SBQQ__PriceRule2__c=r,
    SBQQ__TestedField__c="Deployment_Region__c", SBQQ__TestedValue__c=None,
    SBQQ__Operator__c="equals", SBQQ__LookupField__c="Region__c", SBQQ__MatchType__c="Field Value")
add("SBQQ__LookupQuery__c", Name="Family", SBQQ__ProductRule__c=None, SBQQ__PriceRule2__c=r,
    SBQQ__TestedField__c="SBQQ__ProductFamily__c", SBQQ__TestedValue__c=None,
    SBQQ__Operator__c="equals", SBQQ__LookupField__c="Product_Family__c",
    SBQQ__MatchType__c="Field Value")
pact(r, "Quote Line", "Uplift_Percent__c", lookup_field="Uplift_Percent__c")

r = price_rule("Default storage from configured users", "Configurator", None, 5,
               cfg_event="Save", product_code="BND-ENT")
pcond(r, 1, "Summary Variable", var=SV_OPT_USERS, op="greater than", value="0")
pact(r, "Product Option", "SBQQ__Quantity__c", formula="CEILING(Configured_Users__c / 100)")

r = price_rule("Hardware freight charge", "Calculator", "After Calculate", 30)
pcond(r, 1, "Summary Variable", var=SV_HW, op="greater than", value="0")
pact(r, "Quote", "Freight_Amount__c", formula="Hardware_Units__c * 45", var=SV_HW)

r = price_rule("Education sector discount (old)", "Calculator", "Before Calculate", 10,
               active=False)
pcond(r, 1, "Quote", "Account_Industry__c", "equals", "Education")
pact(r, "Quote Line", "SBQQ__AdditionalDiscount__c", value="15")

# -------------------------------------------------------------------- QCP code
QCP = r"""/**
 * Northwind Quote Calculator Plugin
 */
export function onInit(lines, conn) {
  lines.forEach(function (line) {
    if (line.record['SBQQ__ProductFamily__c'] === 'Hardware') {
      line.record['Requires_Shipping__c'] = true;
    }
  });
  return Promise.resolve();
}

export function onBeforeCalculate(quote, lines, conn) {
  var accountId = quote.record['SBQQ__Account__c'];
  if (!accountId) { return Promise.resolve(); }
  return conn.query("SELECT Id, Strategic_Tier__c FROM Account WHERE Id = '" + accountId + "'")
    .then(function (res) {
      var tier = res.records.length ? res.records[0].Strategic_Tier__c : null;
      quote.record['Strategic_Tier__c'] = tier;
      if (tier === 'Platinum') {
        lines.forEach(function (line) {
          if (line.record['SBQQ__ProductFamily__c'] !== 'Hardware') {
            line.record['SBQQ__AdditionalDiscount__c'] = Math.max(line.record['SBQQ__AdditionalDiscount__c'] || 0, 5);
          }
        });
      }
    });
}

export function onAfterCalculate(quote, lines, conn) {
  var margin = 0, total = 0;
  lines.forEach(function (line) {
    var cost = (line.record['SBQQ__UnitCost__c'] || 0) * (line.record['SBQQ__Quantity__c'] || 0);
    total += line.record['SBQQ__NetTotal__c'] || 0;
    margin += (line.record['SBQQ__NetTotal__c'] || 0) - cost;
  });
  quote.record['Margin_Percent__c'] = total ? (margin / total) * 100 : null;
  return Promise.resolve();
}

export function isFieldEditableForObject(fieldName, line, conn, objectName) {
  if (objectName === 'QuoteLine__c' && fieldName === 'SBQQ__AdditionalDiscount__c') {
    return line['SBQQ__ProductFamily__c'] !== 'Hardware';
  }
  return null;
}
"""
add("SBQQ__CustomScript__c", Name="Northwind QCP", SBQQ__Code__c=QCP,
    SBQQ__QuoteFields__c="SBQQ__Account__c\nStrategic_Tier__c\nMargin_Percent__c",
    SBQQ__QuoteLineFields__c="SBQQ__ProductFamily__c\nSBQQ__UnitCost__c\nRequires_Shipping__c",
    SBQQ__GroupFields__c=None)

# ------------------------------------------------- custom actions & guided sel
add("SBQQ__CustomAction__c", Name="Apply Partner Discount", SBQQ__Active__c=True,
    SBQQ__Location__c="Line Editor", SBQQ__Type__c="Button", SBQQ__DisplayOrder__c=10,
    SBQQ__URLTarget__c=None)
add("SBQQ__CustomAction__c", Name="Open Margin Calculator", SBQQ__Active__c=True,
    SBQQ__Location__c="Quote", SBQQ__Type__c="URL", SBQQ__DisplayOrder__c=20,
    SBQQ__URLTarget__c="New Window")
qp = add("SBQQ__QuoteProcess__c", Name="Find the right edition", SBQQ__Default__c=True,
         SBQQ__GuidedOnly__c=False, SBQQ__ProductAutoSelected__c=False)
add("SBQQ__ProcessInput__c", Name="Company size", SBQQ__QuoteProcess__c=qp,
    SBQQ__InputField__c="Company_Size__c", SBQQ__ProductField__c="Target_Segment__c",
    SBQQ__Operator__c="equals", SBQQ__DisplayOrder__c=10)
add("SBQQ__ProcessInput__c", Name="Deployment", SBQQ__QuoteProcess__c=qp,
    SBQQ__InputField__c="Deployment_Type__c", SBQQ__ProductField__c="Family",
    SBQQ__Operator__c="equals", SBQQ__DisplayOrder__c=20)

# ---------------------------------------------------------- templates/approvals
qt = add("SBQQ__QuoteTemplate__c", Name="Northwind Standard Proposal", SBQQ__Default__c=True,
         SBQQ__DeploymentStatus__c="Deployed")
for i, (n, t) in enumerate([("Cover Letter", "HTML"), ("Line Items", "Line Items"),
                            ("Terms & Conditions", "Quote Terms")], 1):
    c = add("SBQQ__TemplateContent__c", Name=n, SBQQ__Type__c=t)
    add("SBQQ__TemplateSection__c", Name=n, SBQQ__Template__c=qt, SBQQ__Content__c=c,
        SBQQ__DisplayOrder__c=i * 10)

ar = add("sbaa__ApprovalRule__c", Name="Discount over 20% - Sales Director", sbaa__Active__c=True,
         sbaa__TargetObject__c="SBQQ__Quote__c", sbaa__ApprovalStep__c=1, sbaa__ConditionsMet__c="All")
add("sbaa__ApprovalCondition__c", Name="Discount", sbaa__ApprovalRule__c=ar,
    sbaa__TestedField__c="SBQQ__AverageCustomerDiscount__c", sbaa__Operator__c="greater than",
    sbaa__FilterValue__c="20")
ar = add("sbaa__ApprovalRule__c", Name="Margin below 40% - Finance", sbaa__Active__c=True,
         sbaa__TargetObject__c="SBQQ__Quote__c", sbaa__ApprovalStep__c=2, sbaa__ConditionsMet__c="All")
add("sbaa__ApprovalCondition__c", Name="Margin", sbaa__ApprovalRule__c=ar,
    sbaa__TestedField__c="Margin_Percent__c", sbaa__Operator__c="less than", sbaa__FilterValue__c="40")

# ------------------------------------------------------------------- write out
if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.json"):
        f.unlink()
    DATA.pop("Account", None)
    for obj, recs in DATA.items():
        (OUT / f"{obj}.json").write_text(json.dumps(recs, indent=2))
    (OUT / "_org.json").write_text(json.dumps({
        "org_id": "00D5g0000000NWCAA2", "org_name": "Northwind Cloud (synthetic demo)",
        "edition": "Enterprise Edition", "is_sandbox": True,
        "packages": [{"namespace": "SBQQ", "name": "Salesforce CPQ", "version": "250.4"},
                     {"namespace": "sbaa", "name": "Advanced Approvals", "version": "250.1"}]},
        indent=2))
    (OUT / "_counts.json").write_text(json.dumps({
        "SBQQ__Quote__c": 4210, "SBQQ__QuoteLine__c": 38122, "Contract": 812,
        "SBQQ__Subscription__c": 6940, "Asset": 2210}, indent=2))
    print(f"Wrote {sum(len(v) for v in DATA.values())} records across {len(DATA)} objects to {OUT}")
