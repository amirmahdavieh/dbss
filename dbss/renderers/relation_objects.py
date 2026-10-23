"""Objects attached to tables: constraints, indexes, triggers and RLS policies."""

from __future__ import annotations

from typing import Any

from dbss.introspection import Catalog
from dbss.renderers.common import RenderContext, RenderedObject, comment_on, parenthesize
from dbss.utils.naming import object_filename

_TABLE_KINDS = ("r", "p")


def _table(cat: Catalog, relid: int, kinds: tuple[str, ...] = _TABLE_KINDS) -> dict[str, Any] | None:
    rel = cat.relations.get(relid)
    if rel is None or rel["relkind"] not in kinds:
        return None
    return rel


def _table_identity(rel: dict[str, Any]) -> str:
    return f"{rel['schema']}.{rel['name']}"


def _replica_and_cluster(cat: Catalog, rel: dict[str, Any], index_oid: int) -> list[str]:
    idx = next((i for i in cat.indexes if i["oid"] == index_oid), None)
    if idx is None:
        return []
    out = []
    if idx["clustered"]:
        out.append(f"ALTER TABLE {rel['qname']} CLUSTER ON {idx['q']};")
    if idx["replident"] and rel["replident"] == "i":
        out.append(f"ALTER TABLE {rel['qname']} REPLICA IDENTITY USING INDEX {idx['q']};")
    return out


def render_constraints(cat: Catalog, ctx: RenderContext) -> list[RenderedObject]:
    out = []
    for con in cat.constraints:
        if con["type"] not in ("p", "u", "f", "c", "x") or con["relid"] == 0:
            continue
        if not con["is_local"] or con["parentid"]:
            continue  # inherited / cloned onto partitions: owned by the parent's definition
        rel = _table(cat, con["relid"])
        if rel is None or rel["is_extension_member"]:
            continue
        stmts = [f"ALTER TABLE {rel['qname']}\n    ADD CONSTRAINT {con['q']} {con['def']};"]
        if con["type"] in ("p", "u", "x") and con["indid"]:
            stmts.extend(_replica_and_cluster(cat, rel, con["indid"]))
        stmts.extend(comment_on("CONSTRAINT", f"{con['q']} ON {rel['qname']}", con["comment"]))
        out.append(RenderedObject(
            kind="constraint",
            identity=f"{rel['schema']}.{rel['name']}.{con['name']}",
            filename=object_filename(rel["schema"], rel["name"], con["name"]),
            label=f"constraint {con['q']} on {rel['qname']}",
            schema=rel["schema"],
            table=_table_identity(rel),
            statements=stmts,
        ))
    return out


def render_indexes(cat: Catalog, ctx: RenderContext) -> list[RenderedObject]:
    out = []
    for idx in cat.indexes:
        if idx["constraint_backed"] or idx["is_partition_child"] or idx["is_extension_member"]:
            continue
        rel = _table(cat, idx["relid"], ("r", "p", "m"))
        if rel is None or rel["is_extension_member"]:
            continue
        obj = RenderedObject(
            kind="index",
            identity=f"{rel['schema']}.{rel['name']}.{idx['name']}",
            filename=object_filename(rel["schema"], rel["name"], idx["name"]),
            label=f"index {idx['qname']} on {rel['qname']}",
            schema=rel["schema"],
            table=_table_identity(rel),
        )
        if not idx["valid"]:
            obj.error = (f"index {idx['qname']} is INVALID (a failed CREATE INDEX CONCURRENTLY?); "
                         "drop or rebuild it in a migration")
            out.append(obj)
            continue
        definition = idx["def"]
        if rel["relkind"] == "p":
            # pg_get_indexdef says "ON ONLY parent"; replaying that leaves the index invalid until
            # per-partition indexes are attached. The recursive form creates and attaches them.
            definition = definition.replace(f" ON ONLY {rel['qname']} ", f" ON {rel['qname']} ", 1)
        obj.statements = [definition + ";", *_replica_and_cluster(cat, rel, idx["oid"]),
                          *comment_on("INDEX", idx["qname"], idx["comment"])]
        obj.statements = [s for s in obj.statements if s]
        out.append(obj)
    return out


_TRIGGER_ENABLE = {"D": "DISABLE TRIGGER", "R": "ENABLE REPLICA TRIGGER", "A": "ENABLE ALWAYS TRIGGER"}


def render_triggers(cat: Catalog, ctx: RenderContext) -> list[RenderedObject]:
    out = []
    for trg in cat.triggers:
        if trg["parentid"]:
            continue  # clone of a trigger defined on the partitioned parent
        rel = cat.relations.get(trg["relid"])
        if rel is None or rel["is_extension_member"]:
            continue
        stmts = [trg["def"] + ";"]
        if trg["enabled"] in _TRIGGER_ENABLE:
            stmts.append(f"ALTER TABLE {rel['qname']} {_TRIGGER_ENABLE[trg['enabled']]} {trg['q']};")
        stmts.extend(comment_on("TRIGGER", f"{trg['q']} ON {rel['qname']}", trg["comment"]))
        out.append(RenderedObject(
            kind="trigger",
            identity=f"{rel['schema']}.{rel['name']}.{trg['name']}",
            filename=object_filename(rel["schema"], rel["name"], trg["name"]),
            label=f"trigger {trg['q']} on {rel['qname']}",
            schema=rel["schema"],
            table=_table_identity(rel),
            statements=stmts,
        ))
    return out


_POLICY_CMD = {"r": "SELECT", "a": "INSERT", "w": "UPDATE", "d": "DELETE", "*": "ALL"}


def _role_sort_key(role: str) -> tuple[int, str]:
    return (0, "") if role == "PUBLIC" else (1, role)


def render_policies(cat: Catalog, ctx: RenderContext) -> list[RenderedObject]:
    """One file per table: RLS enable/force settings followed by its policies, by name."""
    by_table: dict[int, list[dict[str, Any]]] = {}
    for pol in cat.policies:
        by_table.setdefault(pol["relid"], []).append(pol)
    out = []
    for rel in cat.relations.values():
        if rel["relkind"] not in _TABLE_KINDS or rel["is_extension_member"]:
            continue
        policies = sorted(by_table.get(rel["oid"], []), key=lambda p: p["name"])
        if not (rel["rls"] or rel["force_rls"] or policies):
            continue
        settings = []
        if rel["rls"]:
            settings.append(f"ALTER TABLE {rel['qname']} ENABLE ROW LEVEL SECURITY;")
        if rel["force_rls"]:
            settings.append(f"ALTER TABLE {rel['qname']} FORCE ROW LEVEL SECURITY;")
        stmts = ["\n".join(settings)] if settings else []
        if policies and not rel["rls"]:
            stmts.append("-- NOTE: row level security is DISABLED on this table, so these policies are not enforced.")
        for pol in policies:
            lines = [f"CREATE POLICY {pol['q']} ON {rel['qname']}"]
            if not pol["permissive"]:
                lines.append("    AS RESTRICTIVE")
            lines.append(f"    FOR {_POLICY_CMD[pol['cmd']]}")
            roles = sorted(pol["roles"] or ["PUBLIC"], key=_role_sort_key)
            lines.append("    TO " + ", ".join("public" if r == "PUBLIC" else r for r in roles))
            if pol["qual"] is not None:
                lines.append(f"    USING {parenthesize(pol['qual'])}")
            if pol["with_check"] is not None:
                lines.append(f"    WITH CHECK {parenthesize(pol['with_check'])}")
            stmt = "\n".join(lines) + ";"
            comment = comment_on("POLICY", f"{pol['q']} ON {rel['qname']}", pol["comment"])
            stmts.append("\n".join([stmt, *comment]))
        out.append(RenderedObject(
            kind="policy",
            identity=f"{rel['schema']}.{rel['name']}",
            filename=object_filename(rel["schema"], rel["name"]),
            label=f"row level security for {rel['qname']}",
            schema=rel["schema"],
            table=_table_identity(rel),
            statements=stmts,
        ))
    return out
