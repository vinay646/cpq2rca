from __future__ import annotations

from abc import ABC, abstractmethod


class ObjectNotFound(Exception):
    pass


class ApiLimitExceeded(RuntimeError):
    """The org's API request limit is exhausted. Fatal for the whole run: every
    further call would fail too, so the extractor must stop rather than skip."""


class Connector(ABC):
    """Minimal read interface the extractor needs from a source org."""

    @abstractmethod
    def org_info(self) -> dict: ...

    @abstractmethod
    def describe_fields(self, sobject: str) -> set[str]:
        """Return available field API names; raise ObjectNotFound if absent."""

    @abstractmethod
    def query_all(self, sobject: str, fields: list[str], where: str = "") -> list[dict]: ...

    @abstractmethod
    def count(self, sobject: str, where: str = "") -> int: ...

    # ---- optional capabilities: sources that cannot provide them use these defaults

    def describe(self, sobject: str) -> list[dict]:
        """Field metadata: [{name, label, type, custom}]. Raise ObjectNotFound if absent."""
        return [{"name": f, "label": f, "type": "", "custom": f.endswith("__c")}
                for f in sorted(self.describe_fields(sobject))]

    def count_segment(self, sobject: str, segment: str, where: str) -> int:
        """Count of a named subset (e.g. "active" subscriptions)."""
        return self.count(sobject, where)

    def automation(self, objects: list[str]) -> dict | None:
        """Flows, Apex triggers and validation rules on the given objects:
        {"apex_triggers": [...], "validation_rules": [...], "flows": [...], "errors": [...]}.
        None means the source cannot provide it (e.g. a data-only export)."""
        return None
