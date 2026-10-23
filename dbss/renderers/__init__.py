"""Render introspected catalogs into per-kind generated files.

Scope: in ``"schemas": "auto"`` mode an object is managed when it does not exist
in the *baseline* snapshot (taken after bootstrap, before any migration) or when
its rendered SQL differs from the baseline rendering. That captures everything
your migrations created or changed, including e.g. policies on
``storage.objects`` or triggers on ``auth.users``, and nothing the platform
image created. With an explicit schema list, every object in those schemas is
managed (cluster-level objects such as roles still use the baseline rule).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from dbss.config import Config
from dbss.introspection import Catalog
from dbss.kinds import FOLDERS
from dbss.renderers.common import RenderContext, RenderedObject
from dbss.renderers.grants import render_grants
from dbss.renderers.relation_objects import render_constraints, render_indexes, render_policies, render_triggers
from dbss.renderers.routines_types import render_functions, render_types
from dbss.renderers.tables import render_tables
from dbss.utils.naming import disambiguate_case

Renderer = Callable[[Catalog, RenderContext], list[RenderedObject]]

RENDERERS: dict[str, Renderer] = {
    "table": render_tables,
    "constraint": render_constraints,
    "index": render_indexes,
    "trigger": render_triggers,
    "policy": render_policies,
    "function": render_functions,
    "type": render_types,
    "grant": render_grants,
}


@dataclass
class KindOutput:
    kind: str
    files: dict[str, RenderedObject] = field(default_factory=dict)  # relative path -> object
    errors: list[str] = field(default_factory=list)


def _in_scope(obj: RenderedObject, baseline: dict[str, str], config: Config) -> bool:
    if obj.schema is not None and obj.schema in config.exclude_schemas:
        return False
    if config.schemas is not None and obj.schema is not None:
        return obj.schema in config.schemas
    base = baseline.get(obj.identity)
    return base is None or base != obj.content


def render_kind(kind: str, final: Catalog, baseline: Catalog, config: Config) -> KindOutput:
    ctx = RenderContext(migration_role=config.docker.migration_role, baseline=baseline)
    render = RENDERERS[kind]
    baseline_content = {o.identity: o.content for o in render(baseline, ctx)}
    selected = [o for o in render(final, ctx) if _in_scope(o, baseline_content, config)]

    out = KindOutput(kind)
    folder = FOLDERS[kind]
    paths = disambiguate_case({o.identity: f"{folder}/{o.filename}" for o in selected})
    for obj in sorted(selected, key=lambda o: paths[o.identity]):
        if obj.error:
            out.errors.append(obj.error)
        else:
            out.files[paths[obj.identity]] = obj
    return out


def not_represented(final: Catalog, baseline: Catalog) -> list[str]:
    """Objects created by migrations that belong to no DBSS kind (reported, never rendered)."""

    def items(cat: Catalog) -> set[str]:
        found: set[str] = set()
        kinds = {"v": "view", "m": "materialized view", "f": "foreign table"}
        owned = {s["oid"] for s in cat.sequences if s["deptype"] in ("a", "i")}
        for rel in cat.relations.values():
            if rel["is_extension_member"]:
                continue
            if rel["relkind"] in kinds:
                found.add(f"{kinds[rel['relkind']]} {rel['qname']}")
            elif rel["relkind"] == "S" and rel["oid"] not in owned:
                found.add(f"sequence {rel['qname']}")
        for sch in cat.schemas:
            if not sch["is_extension_member"]:
                found.add(f"schema {sch['q']}")
        for ext in cat.raw.get("extensions", []):
            found.add(f"extension {ext['name']} (schema {ext['schema']})")
        for evt in cat.raw.get("event_triggers", []):
            found.add(f"event trigger {evt['name']}")
        for rule in cat.raw.get("rules", []):
            found.add(f"rule {rule['name']} on {rule['relation']}")
        for pub in cat.raw.get("publications", []):
            found.add(f"publication {pub['name']}")
            for table in pub["tables"] or []:
                if not pub["all_tables"]:
                    found.add(f"publication {pub['name']} membership of {table}")
        for stat in cat.raw.get("statistics", []):
            found.add(f"statistics object {stat['qname']}")
        for coll in cat.raw.get("collations", []):
            found.add(f"collation {coll['qname']}")
        return found

    return sorted(items(final) - items(baseline))
