"""Optional AI polish for explanations (off by default).

Sends only the structured, already-extracted rule explanation (no customer
transaction data) to the Claude API and asks for a clearer business summary and
example. Enable with `--ai` and set:
    ANTHROPIC_API_KEY   API key
    CPQ2RCA_AI_MODEL    model id to use (required, e.g. the current Claude Sonnet id)
Deterministic text is kept if the call fails, so the run never depends on AI.
"""

from __future__ import annotations

import json
import os

import requests

PROMPT = """You are documenting a Salesforce CPQ configuration item for business reviewers
planning a migration to Revenue Cloud Advanced. Using ONLY the facts below, return JSON
{{"summary": "...", "example": "..."}}: summary = one or two plain-English sentences on the
business purpose; example = one concrete scenario starting "Example:". Do not invent products,
fields or values that are not in the facts.

FACTS:
{facts}"""


def enrich(items: list[dict], progress=None, types=("Product Rule", "Price Rule", "Quote Calculator Plugin")) -> int:
    key, model = os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("CPQ2RCA_AI_MODEL")
    if not key or not model:
        raise RuntimeError("Set ANTHROPIC_API_KEY and CPQ2RCA_AI_MODEL to use --ai")
    done = 0
    for it in items:
        if not it["type"].startswith(types):
            continue
        facts = {k: it[k] for k in ("type", "name", "summary", "when", "logic", "conditions",
                                    "actions", "applies_to", "details", "example")}
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
                "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": model, "max_tokens": 400,
                      "messages": [{"role": "user", "content": PROMPT.format(facts=json.dumps(facts, indent=1))}]})
            r.raise_for_status()
            text = r.json()["content"][0]["text"]
            out = json.loads(text[text.find("{"): text.rfind("}") + 1])
            it["summary_generated"], it["example_generated"] = it["summary"], it["example"]
            it["summary"], it["example"] = out["summary"].strip(), out["example"].strip()
            it["ai_enriched"] = True
            done += 1
            if progress:
                progress(f"AI: {it['name']}")
        except Exception as e:  # noqa: BLE001 - keep deterministic text
            it.setdefault("notes", []).append(f"AI enrichment skipped: {e.__class__.__name__}")
    return done
