# CPQ to RCA Migration Tool — Phase 1 (Extract, Explain, Assess)

**Streamforce Solutions**

Connects (read-only) to a Salesforce CPQ org, extracts the CPQ configuration to JSON with IDs and
relationships intact, explains every rule in plain English with a business example, scores its migration
complexity against Revenue Cloud Advanced (RCA), and produces the client-facing deliverables.

| Output | What it is |
|---|---|
| `extract/*.json` + `manifest.json` | Source records per object, original IDs, field list, checksums, load order |
| `analysis.json` | Explained items, findings, custom-field inventory, dependency graph |
| `CPQ_Configuration_Document.docx` | Branded business-readable configuration document |
| `CPQ_Inventory_and_Assessment.xlsx` | Inventory + assessment; the *Configuration Items* sheet has blank Phase 2 review columns (Approved RCA target, Decision, Owner, Review status) to seed the mapping workbook |

## Quick start

```bash
pip install -e .                       # Python 3.10+
cpq2rca run --source sample --out runs/demo     # synthetic "Northwind Cloud" org
cpq2rca serve --runs runs                        # http://127.0.0.1:8080
pytest -q
```

## Running on GitHub

Two GitHub Actions workflows are included:

| Workflow | Trigger | What it does |
|---|---|---|
| **CI** (`.github/workflows/ci.yml`) | every push / PR | Runs the tests on Python 3.10 and 3.12, then does a demo run on the sample org and uploads the Word, Excel and JSON outputs as a build artifact |
| **CPQ Assessment** (`.github/workflows/cpq-assessment.yml`) | manual (*Actions → CPQ Assessment → Run workflow*) | Logs in to a client org and runs extract, explain and assess, then uploads all the deliverables as an artifact that is kept for 14 days |

**One-time setup for live orgs:**

1. Create a GitHub **environment** called `cpq-clients` (*Settings → Environments*). Add required reviewers if you want approval before each run.
2. Get an SFDX auth URL for a read-only CPQ user. Locally, run `sf org login web -a acme`, then `sf org display -o acme --verbose --json`, and copy `sfdxAuthUrl`.
3. Add that value as an environment secret, e.g. `SFDX_AUTH_URL` (or one per client: `SFDX_AUTH_URL_ACME`, etc.).
4. Optional, for AI polish: add the secret `ANTHROPIC_API_KEY` and the variable `CPQ2RCA_AI_MODEL`.
5. Run the workflow with a run name and the secret name. Download the results from the run page under *Artifacts*.

The repository should be **private**. Run outputs contain client data and are git-ignored, so they never get committed.

## Connecting to a real org

| Source | How |
|---|---|
| Salesforce CLI (recommended) | `sf org login web -a client-sbx` then `cpq2rca run --source sf:client-sbx --out runs/client` |
| Access token | `SF_INSTANCE_URL=... SF_ACCESS_TOKEN=... cpq2rca run --source token --out runs/client` |
| Client-supplied export | Folder of `<SObject>.json` files: `--source folder:/path` |
| OAuth flows in code | `SalesforceConnector.client_credentials(...)` / `.password_flow(...)` |

The user needs read access to the SBQQ (and optional sbaa) objects. Only describe and SOQL queries are issued.
Fields that do not exist in the org's CPQ package version are skipped and logged in the manifest.

## Web UI security

`cpq2rca serve` listens on `127.0.0.1` by default, so only your own machine can reach it. Run outputs contain
client data, so to share it on a network you must set a password:

```bash
CPQ2RCA_WEB_PASSWORD='choose-a-strong-one' cpq2rca serve --host 0.0.0.0
```

Without the password it refuses to start on a non-local host. The browser asks for the password (any username
works). The built-in server is plain HTTP, so put it behind an HTTPS reverse proxy when it is reachable beyond
your machine. Starting a run never overwrites an existing one: if the name is taken, a number is added.
Job status is kept in each run folder, so it survives a restart.

## What it covers

Products, price books, bundles (features, options, option constraints, configuration attributes), product rules
(error conditions, actions, configuration rules, lookup queries), summary variables, price rules (conditions,
actions, lookups), discount schedules and tiers, block prices, contracted prices, costs, MDQ dimensions, Quote
Calculator Plugin (hook, field and SOQL analysis), custom actions, guided selling, quote templates, Advanced
Approvals, and record counts for quotes, quote lines, contracts, subscriptions and assets.

**Approach scoring:**

- **Direct:** there is an RCA equivalent.
- **Adapt:** there is an equivalent, but it needs restructuring.
- **Redesign:** there is no equivalent.
- **Retire:** the item is inactive or unused.

Effort rates are in `explain.EFFORT` and cover configuration only. Calibrate them after the first real migration.

**Findings:**

- broken lookups
- rules that never run
- rules with no conditions
- inactive products inside bundles
- unused summary variables and discount schedules
- price or product rules that share an evaluation order
- likely duplicate rules
- QCP and approvals redesign
- in-flight subscriptions

## Optional AI polish

`--ai` (or the checkbox in the UI) rewrites rule summaries and examples with Claude. It sends only the
structured explanation, never transaction data. Set `ANTHROPIC_API_KEY` and `CPQ2RCA_AI_MODEL`. If a call
fails, the deterministic text is kept.

## Layout

```
cpq2rca/catalog.py        CPQ object catalog, refs, suggested RCA targets, load order
cpq2rca/connectors/       salesforce.py (REST, OAuth), mock.py (folder)
cpq2rca/extract.py        Phase 1 extract
cpq2rca/explain.py        rule explainer + complexity scoring
cpq2rca/assess.py         findings, custom fields, graph, summary
cpq2rca/report/           docx and xlsx generators, branding (report/logo.png, bundled in the package)
cpq2rca/web/              Flask UI
cpq2rca/sample_data/      synthetic CPQ org with deliberate issues
```

## Known limits / next steps

- RCA targets are suggestions. Validate them against the target org's RCA release during mapping (Phase 2).
- Uses REST query paging. Add Bulk API 2.0 before extracting large transactional volumes.
- Not yet tested against a live org. Run it first on a client sandbox and compare its counts with SOQL.
- Next phases: generate the mapping workbook, then the transform engine, a loader with a cross-reference table,
  and pricing-parity tests.
