"""Catalog of Salesforce CPQ (SBQQ) objects the tool extracts.

Each entry declares:
  fields      - fields to request (intersected with the org's describe, so
                missing fields on older package versions are skipped and logged)
  refs        - lookup fields -> target object, used to rebuild relationships
                and to compute the dependency-ordered load sequence
  category    - grouping used in documents and the UI
  optional    - True if the object belongs to an add-on package (e.g. Advanced
                Approvals) and may legitimately be absent
  kind        - "config" (migrated as configuration) or "transaction" (counted
                for the assessment; full extract is opt-in)
  rca_target  - suggested Revenue Cloud Advanced target. Always a suggestion,
                confirmed during Phase 2 mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ObjectSpec:
    name: str
    label: str
    category: str
    fields: tuple[str, ...]
    refs: dict[str, str] = field(default_factory=dict)
    optional: bool = False
    kind: str = "config"
    rca_target: str = ""
    where: str = ""


# Fields that only exist in some orgs by design (e.g. CurrencyIsoCode exists only
# when multi-currency is enabled). Their absence is expected, so it is not reported
# as a missing field.
CONDITIONAL_FIELDS = frozenset({"CurrencyIsoCode"})


def _o(name, label, category, fields, refs=None, **kw) -> ObjectSpec:
    return ObjectSpec(name, label, category, tuple(["Id", "Name", *fields]), refs or {}, **kw)


CATALOG: list[ObjectSpec] = [
    # ---------------------------------------------------------------- Catalog
    _o("Pricebook2", "Price Book", "Pricing",
       ["IsActive", "IsStandard", "Description"],
       rca_target="Pricebook2 (reused)"),
    _o("Product2", "Product", "Catalog",
       ["ProductCode", "Family", "IsActive", "Description",
        "SBQQ__ConfigurationType__c", "SBQQ__ConfigurationEvent__c",
        "SBQQ__PricingMethod__c", "SBQQ__SubscriptionPricing__c",
        "SBQQ__SubscriptionTerm__c", "SBQQ__SubscriptionType__c",
        "SBQQ__ChargeType__c", "SBQQ__BillingType__c", "SBQQ__BillingFrequency__c",
        "SBQQ__DiscountSchedule__c", "SBQQ__NonDiscountable__c",
        "SBQQ__QuantityEditable__c", "SBQQ__AssetConversion__c",
        "SBQQ__BlockPricingField__c", "SBQQ__ExternallyConfigurable__c"],
       {"SBQQ__DiscountSchedule__c": "SBQQ__DiscountSchedule__c"},
       rca_target="Product2 + ProductSellingModel / ProductSellingModelOption + ProductClassification"),
    _o("PricebookEntry", "Price Book Entry", "Pricing",
       ["Pricebook2Id", "Product2Id", "UnitPrice", "IsActive", "CurrencyIsoCode"],
       {"Pricebook2Id": "Pricebook2", "Product2Id": "Product2"},
       rca_target="PricebookEntry (with ProductSellingModelId)"),
    _o("SBQQ__ProductFeature__c", "Product Feature", "Bundles",
       ["SBQQ__ConfiguredSKU__c", "SBQQ__Category__c", "SBQQ__Number__c",
        "SBQQ__MinOptionCount__c", "SBQQ__MaxOptionCount__c"],
       {"SBQQ__ConfiguredSKU__c": "Product2"},
       rca_target="ProductComponentGroup"),
    _o("SBQQ__ProductOption__c", "Product Option", "Bundles",
       ["SBQQ__ConfiguredSKU__c", "SBQQ__OptionalSKU__c", "SBQQ__Feature__c",
        "SBQQ__Number__c", "SBQQ__Quantity__c", "SBQQ__MinQuantity__c",
        "SBQQ__MaxQuantity__c", "SBQQ__Required__c", "SBQQ__Selected__c",
        "SBQQ__Type__c", "SBQQ__Bundled__c", "SBQQ__QuantityEditable__c",
        "SBQQ__DiscountedByPackage__c"],
       {"SBQQ__ConfiguredSKU__c": "Product2", "SBQQ__OptionalSKU__c": "Product2",
        "SBQQ__Feature__c": "SBQQ__ProductFeature__c"},
       rca_target="ProductRelatedComponent (+ ProductRelationshipType)"),
    _o("SBQQ__OptionConstraint__c", "Option Constraint", "Bundles",
       ["SBQQ__ConfiguredSKU__c", "SBQQ__ConstrainingOption__c",
        "SBQQ__ConstrainedOption__c", "SBQQ__Type__c", "SBQQ__Active__c"],
       {"SBQQ__ConfiguredSKU__c": "Product2",
        "SBQQ__ConstrainingOption__c": "SBQQ__ProductOption__c",
        "SBQQ__ConstrainedOption__c": "SBQQ__ProductOption__c"},
       rca_target="Configurator constraint (CML require / exclude)"),
    _o("SBQQ__ConfigurationAttribute__c", "Configuration Attribute", "Bundles",
       ["SBQQ__Product__c", "SBQQ__Feature__c", "SBQQ__TargetField__c",
        "SBQQ__Required__c", "SBQQ__AppliedImmediately__c", "SBQQ__DefaultField__c"],
       {"SBQQ__Product__c": "Product2", "SBQQ__Feature__c": "SBQQ__ProductFeature__c"},
       rca_target="AttributeDefinition + ProductAttributeDefinition"),
    # ---------------------------------------------------------- Product rules
    _o("SBQQ__SummaryVariable__c", "Summary Variable", "Product Rules",
       ["SBQQ__AggregateFunction__c", "SBQQ__AggregateField__c", "SBQQ__TargetObject__c",
        "SBQQ__FilterField__c", "SBQQ__Operator__c", "SBQQ__FilterValue__c",
        "SBQQ__CombineWith__c", "SBQQ__CompositeOperator__c", "SBQQ__Scope__c"],
       {"SBQQ__CombineWith__c": "SBQQ__SummaryVariable__c"},
       rca_target="Pricing procedure aggregation / context aggregate"),
    _o("SBQQ__ProductRule__c", "Product Rule", "Product Rules",
       ["SBQQ__Active__c", "SBQQ__Type__c", "SBQQ__Scope__c", "SBQQ__EvaluationEvent__c",
        "SBQQ__EvaluationOrder__c", "SBQQ__ConditionsMet__c", "SBQQ__AdvancedCondition__c",
        "SBQQ__ErrorMessage__c", "SBQQ__LookupObject__c", "SBQQ__Description__c"],
       rca_target="Configurator constraint rule / Business Rules Engine"),
    _o("SBQQ__ErrorCondition__c", "Error Condition", "Product Rules",
       ["SBQQ__Rule__c", "SBQQ__Index__c", "SBQQ__TestedObject__c", "SBQQ__TestedField__c",
        "SBQQ__TestedVariable__c", "SBQQ__Operator__c", "SBQQ__FilterType__c",
        "SBQQ__FilterValue__c", "SBQQ__FilterVariable__c"],
       {"SBQQ__Rule__c": "SBQQ__ProductRule__c",
        "SBQQ__TestedVariable__c": "SBQQ__SummaryVariable__c",
        "SBQQ__FilterVariable__c": "SBQQ__SummaryVariable__c"},
       rca_target="(part of parent rule)"),
    _o("SBQQ__ProductAction__c", "Product Action", "Product Rules",
       ["SBQQ__Rule__c", "SBQQ__Type__c", "SBQQ__Product__c", "SBQQ__Required__c",
        "SBQQ__FilterField__c", "SBQQ__Operator__c", "SBQQ__FilterValue__c",
        "SBQQ__ValueField__c"],
       {"SBQQ__Rule__c": "SBQQ__ProductRule__c", "SBQQ__Product__c": "Product2"},
       rca_target="(part of parent rule)"),
    _o("SBQQ__ConfigurationRule__c", "Configuration Rule", "Product Rules",
       ["SBQQ__Product__c", "SBQQ__ProductRule__c", "SBQQ__ProductFeature__c",
        "SBQQ__Active__c"],
       {"SBQQ__Product__c": "Product2", "SBQQ__ProductRule__c": "SBQQ__ProductRule__c",
        "SBQQ__ProductFeature__c": "SBQQ__ProductFeature__c"},
       rca_target="Rule-to-product assignment in configurator model"),
    # ------------------------------------------------------------ Price rules
    _o("SBQQ__PriceRule__c", "Price Rule", "Price Rules",
       ["SBQQ__Active__c", "SBQQ__TargetObject__c", "SBQQ__EvaluationEvent__c",
        "SBQQ__ConfiguratorEvaluationEvent__c", "SBQQ__EvaluationOrder__c",
        "SBQQ__ConditionsMet__c", "SBQQ__AdvancedCondition__c", "SBQQ__Product__c",
        "SBQQ__LookupObject__c"],
       {"SBQQ__Product__c": "Product2"},
       rca_target="Pricing Procedure element (+ Decision Table for lookups)"),
    _o("SBQQ__PriceCondition__c", "Price Condition", "Price Rules",
       ["SBQQ__Rule__c", "SBQQ__Index__c", "SBQQ__Object__c", "SBQQ__Field__c",
        "SBQQ__TestedVariable__c", "SBQQ__TestedFormula__c", "SBQQ__Operator__c",
        "SBQQ__FilterType__c", "SBQQ__Value__c", "SBQQ__FilterVariable__c",
        "SBQQ__FilterFormula__c"],
       {"SBQQ__Rule__c": "SBQQ__PriceRule__c",
        "SBQQ__TestedVariable__c": "SBQQ__SummaryVariable__c",
        "SBQQ__FilterVariable__c": "SBQQ__SummaryVariable__c"},
       rca_target="(part of parent rule)"),
    _o("SBQQ__PriceAction__c", "Price Action", "Price Rules",
       ["SBQQ__Rule__c", "SBQQ__TargetObject__c", "SBQQ__Field__c", "SBQQ__Value__c",
        "SBQQ__ValueField__c", "SBQQ__Formula__c", "SBQQ__SourceVariable__c",
        "SBQQ__SourceLookupField__c", "SBQQ__Order__c"],
       {"SBQQ__Rule__c": "SBQQ__PriceRule__c",
        "SBQQ__SourceVariable__c": "SBQQ__SummaryVariable__c"},
       rca_target="(part of parent rule)"),
    _o("SBQQ__LookupQuery__c", "Lookup Query", "Price Rules",
       ["SBQQ__ProductRule__c", "SBQQ__PriceRule2__c", "SBQQ__TestedField__c",
        "SBQQ__TestedValue__c", "SBQQ__Operator__c", "SBQQ__LookupField__c",
        "SBQQ__MatchType__c"],
       {"SBQQ__ProductRule__c": "SBQQ__ProductRule__c",
        "SBQQ__PriceRule2__c": "SBQQ__PriceRule__c"},
       rca_target="Decision Table input"),
    # --------------------------------------------------------------- Pricing
    _o("SBQQ__DiscountSchedule__c", "Discount Schedule", "Pricing",
       ["SBQQ__Type__c", "SBQQ__DiscountUnit__c", "SBQQ__AggregationScope__c",
        "SBQQ__CrossOrders__c", "SBQQ__CrossProducts__c", "SBQQ__Product__c",
        "SBQQ__Description__c", "CurrencyIsoCode"],
       {"SBQQ__Product__c": "Product2"},
       rca_target="PriceAdjustmentSchedule"),
    _o("SBQQ__DiscountTier__c", "Discount Tier", "Pricing",
       ["SBQQ__Schedule__c", "SBQQ__Number__c", "SBQQ__LowerBound__c",
        "SBQQ__UpperBound__c", "SBQQ__Discount__c", "SBQQ__DiscountAmount__c",
        "SBQQ__Price__c"],
       {"SBQQ__Schedule__c": "SBQQ__DiscountSchedule__c"},
       rca_target="PriceAdjustmentTier"),
    _o("SBQQ__BlockPrice__c", "Block Price", "Pricing",
       ["SBQQ__Product__c", "SBQQ__PriceBook2__c", "SBQQ__LowerBound__c",
        "SBQQ__UpperBound__c", "SBQQ__Price__c", "CurrencyIsoCode"],
       {"SBQQ__Product__c": "Product2", "SBQQ__PriceBook2__c": "Pricebook2"},
       rca_target="Tiered / block pricing in pricing procedure (confirm)"),
    _o("SBQQ__ContractedPrice__c", "Contracted Price", "Pricing",
       ["SBQQ__Account__c", "SBQQ__Product__c", "SBQQ__Price__c", "SBQQ__Discount__c",
        "SBQQ__EffectiveDate__c", "SBQQ__ExpirationDate__c", "CurrencyIsoCode"],
       {"SBQQ__Product__c": "Product2"},
       rca_target="Account-specific pricing (decision table / price book) - confirm"),
    _o("SBQQ__Cost__c", "Cost", "Pricing",
       ["SBQQ__Product__c", "SBQQ__UnitCost__c", "SBQQ__Active__c", "CurrencyIsoCode"],
       {"SBQQ__Product__c": "Product2"},
       rca_target="Cost book / cost element in pricing procedure (confirm)"),
    _o("SBQQ__Dimension__c", "Price Dimension (MDQ)", "Pricing",
       ["SBQQ__Product__c", "SBQQ__Type__c", "SBQQ__PriceEditable__c"],
       {"SBQQ__Product__c": "Product2"},
       rca_target="Ramp deal / segmented selling (confirm)"),
    # ------------------------------------------------------- Code & process
    _o("SBQQ__CustomScript__c", "Quote Calculator Plugin (QCP)", "Code & Process",
       ["SBQQ__Code__c", "SBQQ__QuoteFields__c", "SBQQ__QuoteLineFields__c",
        "SBQQ__GroupFields__c"],
       rca_target="Pricing Procedure custom elements / Apex (redesign)"),
    _o("SBQQ__CustomAction__c", "Custom Action", "Code & Process",
       ["SBQQ__Active__c", "SBQQ__Location__c", "SBQQ__Type__c", "SBQQ__DisplayOrder__c",
        "SBQQ__URLTarget__c"],
       rca_target="Transaction Line Editor action / LWC (redesign)"),
    _o("SBQQ__QuoteProcess__c", "Guided Selling Process", "Code & Process",
       ["SBQQ__Default__c", "SBQQ__GuidedOnly__c", "SBQQ__ProductAutoSelected__c"],
       rca_target="Product Discovery guided selection (confirm)"),
    _o("SBQQ__ProcessInput__c", "Guided Selling Input", "Code & Process",
       ["SBQQ__QuoteProcess__c", "SBQQ__InputField__c", "SBQQ__ProductField__c",
        "SBQQ__Operator__c", "SBQQ__DisplayOrder__c"],
       {"SBQQ__QuoteProcess__c": "SBQQ__QuoteProcess__c"},
       rca_target="(part of guided process)"),
    # ---------------------------------------------------- Output & approvals
    _o("SBQQ__QuoteTemplate__c", "Quote Template", "Documents & Approvals",
       ["SBQQ__Default__c", "SBQQ__DeploymentStatus__c"],
       rca_target="Document Generation template"),
    _o("SBQQ__TemplateContent__c", "Template Content", "Documents & Approvals",
       ["SBQQ__Type__c"],
       rca_target="(part of document template)"),
    _o("SBQQ__TemplateSection__c", "Template Section", "Documents & Approvals",
       ["SBQQ__Template__c", "SBQQ__Content__c", "SBQQ__DisplayOrder__c"],
       {"SBQQ__Template__c": "SBQQ__QuoteTemplate__c",
        "SBQQ__Content__c": "SBQQ__TemplateContent__c"},
       rca_target="(part of document template)"),
    _o("sbaa__ApprovalRule__c", "Approval Rule", "Documents & Approvals",
       ["sbaa__Active__c", "sbaa__TargetObject__c", "sbaa__ApprovalStep__c",
        "sbaa__ConditionsMet__c"],
       optional=True, rca_target="Flow Approval Process (redesign)"),
    _o("sbaa__ApprovalCondition__c", "Approval Condition", "Documents & Approvals",
       ["sbaa__ApprovalRule__c", "sbaa__TestedField__c", "sbaa__Operator__c",
        "sbaa__FilterValue__c"],
       {"sbaa__ApprovalRule__c": "sbaa__ApprovalRule__c"},
       optional=True, rca_target="(part of approval process)"),
    # ---------------------------------------------- Transactions (count only)
    _o("SBQQ__Quote__c", "Quote", "Transactions", [], kind="transaction",
       rca_target="Quote / QuoteLineItem"),
    _o("SBQQ__QuoteLine__c", "Quote Line", "Transactions", [], kind="transaction",
       rca_target="QuoteLineItem + QuoteLineRelationship"),
    _o("Contract", "Contract", "Transactions", [], kind="transaction",
       rca_target="Contract"),
    _o("SBQQ__Subscription__c", "Subscription", "Transactions", [], kind="transaction",
       rca_target="Asset + AssetStatePeriod / AssetActionSource"),
    _o("Asset", "Asset", "Transactions", [], kind="transaction",
       rca_target="Asset + AssetStatePeriod"),
]

BY_NAME: dict[str, ObjectSpec] = {s.name: s for s in CATALOG}

CATEGORY_ORDER = ["Catalog", "Bundles", "Pricing", "Product Rules", "Price Rules",
                  "Code & Process", "Documents & Approvals", "Transactions"]


def config_objects() -> list[ObjectSpec]:
    return [s for s in CATALOG if s.kind == "config"]


def load_order() -> list[str]:
    """Dependency-ordered list of config objects (parents before children).

    Self references (e.g. Summary Variable CombineWith) are ignored; they are
    handled with a second-pass update during load.
    """
    import networkx as nx

    g = nx.DiGraph()
    for spec in config_objects():
        g.add_node(spec.name)
        for target in spec.refs.values():
            if target != spec.name and target in BY_NAME:
                g.add_edge(target, spec.name)
    # Product2 <-> DiscountSchedule is a genuine cycle in CPQ; break it by
    # loading Product2 first and back-filling the schedule lookup.
    if g.has_edge("SBQQ__DiscountSchedule__c", "Product2") and g.has_edge("Product2", "SBQQ__DiscountSchedule__c"):
        g.remove_edge("SBQQ__DiscountSchedule__c", "Product2")
    return list(nx.lexicographical_topological_sort(g))
