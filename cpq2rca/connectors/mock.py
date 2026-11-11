"""Offline connector reading a folder of <Object>.json files.

Used for demos, tests, and for clients who hand over an export instead of
granting API access. Expected layout:
    folder/_org.json            (optional org metadata)
    folder/<SObjectApiName>.json  (list of records)
    folder/_counts.json         (optional {sobject: count} for transactional objects;
                                 "<sobject>.<segment>" keys for segment counts)
    folder/_schema.json         (optional {sobject: [field metadata]} for custom fields)
    folder/_automation.json     (optional flows / Apex triggers / validation rules)
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import Connector, ObjectNotFound


class FolderConnector(Connector):
    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        if not self.folder.is_dir():
            raise FileNotFoundError(self.folder)
        self._cache: dict[str, list[dict]] = {}

    def _load(self, sobject: str) -> list[dict]:
        if sobject not in self._cache:
            p = self.folder / f"{sobject}.json"
            if not p.exists():
                raise ObjectNotFound(sobject)
            self._cache[sobject] = json.loads(p.read_text())
        return self._cache[sobject]

    def org_info(self) -> dict:
        p = self.folder / "_org.json"
        info = json.loads(p.read_text()) if p.exists() else {}
        info.setdefault("source", f"folder:{self.folder.name}")
        return info

    def describe_fields(self, sobject: str) -> set[str]:
        recs = self._load(sobject)
        fields = {"Id", "Name"}
        for r in recs:
            fields.update(r.keys())
        return fields

    def query_all(self, sobject: str, fields: list[str], where: str = "") -> list[dict]:
        return [{f: r.get(f) for f in fields} for r in self._load(sobject)]

    def _counts(self) -> dict:
        p = self.folder / "_counts.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def count(self, sobject: str, where: str = "") -> int:
        counts = self._counts()
        if sobject in counts:
            return counts[sobject]
        return len(self._load(sobject))

    def count_segment(self, sobject: str, segment: str, where: str) -> int:
        key = f"{sobject}.{segment}"
        counts = self._counts()
        if key not in counts:
            raise ObjectNotFound(key)
        return counts[key]

    def describe(self, sobject: str) -> list[dict]:
        p = self.folder / "_schema.json"
        if p.exists():
            schema = json.loads(p.read_text())
            if sobject in schema:
                return schema[sobject]
        if not (self.folder / f"{sobject}.json").exists():
            raise ObjectNotFound(sobject)
        return super().describe(sobject)

    def automation(self, objects: list[str]) -> dict | None:
        p = self.folder / "_automation.json"
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        wanted = set(objects)
        for key in ("apex_triggers", "validation_rules", "flows"):
            data[key] = [r for r in data.get(key, []) if r.get("object") in wanted]
        data.setdefault("errors", [])
        return data
