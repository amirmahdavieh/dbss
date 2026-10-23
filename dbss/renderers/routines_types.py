"""Functions/procedures and user-defined types (enum, domain, composite, range)."""

from __future__ import annotations

from typing import Any

from dbss.introspection import Catalog
from dbss.renderers.common import RenderContext, RenderedObject, comment_on, literal, owner_statement
from dbss.utils.naming import object_filename

_ROUTINE_WORD = {"f": "FUNCTION", "w": "FUNCTION", "p": "PROCEDURE", "a": "AGGREGATE"}


def function_identity(fn: dict[str, Any]) -> str:
    return f"{fn['schema']}.{fn['name']}({', '.join(fn['argtypes'])})"


def function_filename(prefix: tuple[str, ...], fn: dict[str, Any]) -> str:
    return object_filename(*prefix, fn["schema"], fn["name"], signature=fn["argtypes"])


def is_user_function(fn: dict[str, Any]) -> bool:
    """Excludes extension members and functions created implicitly (e.g. range constructors)."""
    return not fn["is_extension_member"] and not fn["is_internal"]


def render_functions(cat: Catalog, ctx: RenderContext) -> list[RenderedObject]:
    migration_role = ctx.migration_role
    out = []
    for fn in cat.functions:
        if not is_user_function(fn):
            continue
        word = _ROUTINE_WORD.get(fn["kind"], "FUNCTION")
        target = f"{fn['qname']}({fn['identity_args']})"
        obj = RenderedObject(
            kind="function",
            identity=function_identity(fn),
            filename=function_filename((), fn),
            label=f"{word.lower()} {target}",
            schema=fn["schema"],
        )
        if fn["kind"] == "a" or fn["def"] is None:
            obj.error = (f"aggregate {target} cannot be reconstructed yet "
                         "(aggregates are not supported by DBSS)")
            out.append(obj)
            continue
        definition = fn["def"].rstrip()
        obj.statements = [
            definition + ";",
            *owner_statement(word, target, fn["owner"], migration_role),
            *comment_on(word, target, fn["comment"]),
        ]
        out.append(obj)
    return out


def _domain(cat: Catalog, typ: dict[str, Any]) -> list[str]:
    lines = [f"CREATE DOMAIN {typ['qname']} AS {typ['base_type']}"]
    if typ["collation"]:
        lines.append(f"    COLLATE {typ['collation']}")
    if typ["default"] is not None:
        lines.append(f"    DEFAULT {typ['default']}")
    if typ["notnull"]:
        lines.append("    NOT NULL")
    later = []
    checks = sorted((c for c in cat.constraints if c["typid"] == typ["oid"] and c["type"] == "c"),
                    key=lambda c: c["name"])
    for con in checks:
        if con["validated"]:
            lines.append(f"    CONSTRAINT {con['q']} {con['def']}")
        else:
            later.append(f"ALTER DOMAIN {typ['qname']} ADD CONSTRAINT {con['q']} {con['def']};")
        later.extend(comment_on("CONSTRAINT", f"{con['q']} ON DOMAIN {typ['qname']}", con["comment"]))
    return ["\n".join(lines) + ";", *later]


def _composite(cat: Catalog, typ: dict[str, Any]) -> list[str]:
    attrs = []
    for col in cat.table_columns(typ["relid"]):
        line = f"    {col['q']} {col['type']}"
        if col["collation"]:
            line += f" COLLATE {col['collation']}"
        attrs.append(line)
    body = ",\n".join(attrs)
    stmts = [f"CREATE TYPE {typ['qname']} AS (\n{body}\n);" if attrs else f"CREATE TYPE {typ['qname']} AS ();"]
    for col in cat.table_columns(typ["relid"]):
        stmts.extend(comment_on("COLUMN", f"{typ['qname']}.{col['q']}", col["comment"]))
    return stmts


def _range(typ: dict[str, Any]) -> list[str]:
    rng = typ["range"]
    opts = [f"    SUBTYPE = {rng['subtype']}"]
    if rng["opclass"]:
        opts.append(f"    SUBTYPE_OPCLASS = {rng['opclass']}")
    if rng["collation"]:
        opts.append(f"    COLLATION = {rng['collation']}")
    if rng["canonical"]:
        opts.append(f"    CANONICAL = {rng['canonical']}")
    if rng["subdiff"]:
        opts.append(f"    SUBTYPE_DIFF = {rng['subdiff']}")
    opts.append(f"    MULTIRANGE_TYPE_NAME = {rng['multirange']}")
    return [f"CREATE TYPE {typ['qname']} AS RANGE (\n" + ",\n".join(opts) + "\n);"]


def is_user_type(typ: dict[str, Any]) -> bool:
    """Types DBSS represents in types/ (array, multirange and table row types are implicit)."""
    if typ["is_extension_member"]:
        return False
    if typ["typtype"] == "m":
        return False
    if typ["typtype"] == "c":
        return typ["relkind"] == "c"
    if typ["typtype"] == "b" and typ["category"] == "A" and typ["elem"]:
        return False
    return True


def render_types(cat: Catalog, ctx: RenderContext) -> list[RenderedObject]:
    migration_role = ctx.migration_role
    out = []
    for typ in cat.types:
        if not is_user_type(typ):
            continue
        word = "DOMAIN" if typ["typtype"] == "d" else "TYPE"
        obj = RenderedObject(
            kind="type",
            identity=f"{typ['schema']}.{typ['name']}",
            filename=object_filename(typ["schema"], typ["name"]),
            label=f"{word.lower()} {typ['qname']}",
            schema=typ["schema"],
        )
        if typ["typtype"] == "e":
            labels = ",\n".join(f"    {literal(v)}" for v in (typ["labels"] or []))
            stmts = [f"CREATE TYPE {typ['qname']} AS ENUM (\n{labels}\n);" if labels
                     else f"CREATE TYPE {typ['qname']} AS ENUM ();"]
        elif typ["typtype"] == "d":
            stmts = _domain(cat, typ)
        elif typ["typtype"] == "c":
            stmts = _composite(cat, typ)
        elif typ["typtype"] == "r":
            stmts = _range(typ)
        else:
            what = "shell type" if not typ["defined"] else "base type"
            obj.error = f"{what} {typ['qname']} cannot be reconstructed (only enum, domain, composite and range types are supported)"
            out.append(obj)
            continue
        stmts.extend(owner_statement(word, typ["qname"], typ["owner"], migration_role))
        stmts.extend(comment_on(word, typ["qname"], typ["comment"]))
        obj.statements = stmts
        out.append(obj)
    return out
