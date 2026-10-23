"""Catalog snapshot: the introspected state of a scratch database."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from dbss.introspection.queries import snapshot_sql


class JsonQueryable(Protocol):
    def query_json(self, sql: str) -> Any: ...


@dataclass
class Catalog:
    raw: dict[str, Any]
    server_version_num: int = 0
    relations: dict[int, dict[str, Any]] = field(default_factory=dict)
    columns: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    sequences: list[dict[str, Any]] = field(default_factory=list)
    constraints: list[dict[str, Any]] = field(default_factory=list)
    indexes: list[dict[str, Any]] = field(default_factory=list)
    triggers: list[dict[str, Any]] = field(default_factory=list)
    policies: list[dict[str, Any]] = field(default_factory=list)
    functions: list[dict[str, Any]] = field(default_factory=list)
    types: list[dict[str, Any]] = field(default_factory=list)
    schemas: list[dict[str, Any]] = field(default_factory=list)
    roles: list[dict[str, Any]] = field(default_factory=list)
    default_acls: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Catalog:
        cat = cls(raw=data, server_version_num=data.get("server_version_num", 0))
        cat.relations = {r["oid"]: r for r in data["relations"]}
        for col in sorted(data["columns"], key=lambda c: (c["relid"], c["num"])):
            cat.columns.setdefault(col["relid"], []).append(col)
        cat.sequences = data["sequences"]
        cat.constraints = data["constraints"]
        cat.indexes = data["indexes"]
        cat.triggers = data["triggers"]
        cat.policies = data["policies"]
        cat.functions = data["functions"]
        cat.types = data["types"]
        cat.schemas = data["schemas"]
        cat.roles = data["roles"]
        cat.default_acls = data["default_acls"]
        return cat

    def table_columns(self, relid: int) -> list[dict[str, Any]]:
        return self.columns.get(relid, [])


def take_snapshot(db: JsonQueryable) -> Catalog:
    return Catalog.from_json(db.query_json(snapshot_sql()))
