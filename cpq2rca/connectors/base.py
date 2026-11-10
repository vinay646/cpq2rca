from __future__ import annotations

from abc import ABC, abstractmethod


class ObjectNotFound(Exception):
    pass


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
    def count(self, sobject: str) -> int: ...
