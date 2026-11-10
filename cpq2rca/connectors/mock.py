"""Offline connector reading a folder of <Object>.json files.

Used for demos, tests, and for clients who hand over an export instead of
granting API access. Expected layout:
    folder/_org.json            (optional org metadata)
    folder/<SObjectApiName>.json  (list of records)
    folder/_counts.json         (optional {sobject: count} for transactional objects)
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

    def count(self, sobject: str) -> int:
        p = self.folder / "_counts.json"
        if p.exists():
            counts = json.loads(p.read_text())
            if sobject in counts:
                return counts[sobject]
        try:
            return len(self._load(sobject))
        except ObjectNotFound:
            raise
