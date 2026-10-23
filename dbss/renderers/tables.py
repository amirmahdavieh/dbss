"""Table definitions: columns, defaults, identity/generated columns, storage options, comments.

Ownership rules (see docs/ARCHITECTURE.md):
* constraints (PK/UNIQUE/FK/CHECK/EXCLUDE) live in ``constraints/``; only NOT NULL is inline;
* RLS enable/force lives in ``policies/``; privileges live in ``grants/``;
* sequences owned by a column (serial / OWNED BY) are part of the table file.
"""

from __future__ import annotations

from typing import Any

from dbss.introspection import Catalog
from dbss.renderers.common import RenderContext, RenderedObject, comment_on, owner_statement, parenthesize
from dbss.utils.naming import object_filename

_INT_LIMITS = {
    "smallint": (-32768, 32767),
    "integer": (-2147483648, 2147483647),
    "bigint": (-9223372036854775808, 9223372036854775807),
}
_SERIAL = {"smallint": "smallserial", "integer": "serial", "bigint": "bigserial"}


def _seq_defaults(seq: dict[str, Any]) -> dict[str, int] | None:
    limits = _INT_LIMITS.get(seq["type"])
    if limits is None:
        return None
    inc = int(seq["increment"])
    lo, hi = limits
    dmin, dmax = (1, hi) if inc > 0 else (lo, -1)
    return {"min": dmin, "max": dmax}


def sequence_options(seq: dict[str, Any], *, column_type: str | None) -> list[str]:
    """Options that differ from what CREATE SEQUENCE / IDENTITY would choose by default."""
    opts: list[str] = []
    if column_type is not None and seq["type"] != column_type:
        opts.append(f"AS {seq['type']}")
    defaults = _seq_defaults(seq) or {"min": None, "max": None}
    inc = int(seq["increment"])
    smin, smax, start = int(seq["min"]), int(seq["max"]), int(seq["start"])
    if inc != 1:
        opts.append(f"INCREMENT BY {inc}")
    if smin != defaults["min"]:
        opts.append(f"MINVALUE {smin}")
    if smax != defaults["max"]:
        opts.append(f"MAXVALUE {smax}")
    if start != (smin if inc > 0 else smax):
        opts.append(f"START WITH {start}")
    if int(seq["cache"]) != 1:
        opts.append(f"CACHE {seq['cache']}")
    if seq["cycle"]:
        opts.append("CYCLE")
    return opts


def _default_seq_name(table: str, column: str) -> str:
    return f"{table}_{column}_seq"


class _TableRenderer:
    def __init__(self, cat: Catalog, migration_role: str):
        self.cat = cat
        self.migration_role = migration_role
        self.owned_seqs: dict[int, list[dict[str, Any]]] = {}
        for seq in cat.sequences:
            if seq["owner_relid"] is not None:
                self.owned_seqs.setdefault(seq["owner_relid"], []).append(seq)
        self.index_names = {i["oid"]: i["q"] for i in cat.indexes}

    def render(self, rel: dict[str, Any]) -> RenderedObject:
        qname = rel["qname"]
        obj = RenderedObject(
            kind="table",
            identity=f"{rel['schema']}.{rel['name']}",
            filename=object_filename(rel["schema"], rel["name"]),
            label=f"table {qname}",
            schema=rel["schema"],
            table=f"{rel['schema']}.{rel['name']}",
        )
        seqs = self.owned_seqs.get(rel["oid"], [])
        serial_seq_oids: set[int] = set()
        column_lines: list[str] = []
        post: list[str] = []
        cols = [c for c in self.cat.table_columns(rel["oid"]) if c["is_local"] or not (rel["parents"] or rel["is_partition"])]

        for col in cols:
            line, serial_oid = self._column(rel, col, seqs)
            if serial_oid is not None:
                serial_seq_oids.add(serial_oid)
            column_lines.append(line)
            post.extend(self._column_options(qname, col))

        pre: list[str] = []
        for seq in sorted(seqs, key=lambda s: s["qname"]):
            if seq["deptype"] != "a" or seq["oid"] in serial_seq_oids:
                continue
            owner_col = next((c for c in cols if c["num"] == seq["owner_attnum"]), None)
            opts = sequence_options(seq, column_type=None)
            if seq["type"] != "bigint":
                opts.insert(0, f"AS {seq['type']}")
            pre.append(f"CREATE SEQUENCE {seq['qname']}" + ("".join(f"\n    {o}" for o in opts)) + ";")
            if owner_col is not None:
                post.append(f"ALTER SEQUENCE {seq['qname']} OWNED BY {qname}.{owner_col['q']};")

        unlogged = "UNLOGGED " if rel["persistence"] == "u" else ""
        if rel["is_partition"]:
            parent = (rel["parents"] or ["?"])[0]
            create = f"CREATE {unlogged}TABLE {qname} PARTITION OF {parent}\n    {rel['partbound']}"
            if rel["relkind"] == "p":
                create += f"\n    PARTITION BY {rel['partkey']}"
        elif rel["of_type"]:
            create = f"CREATE {unlogged}TABLE {qname} OF {rel['of_type']}"
        else:
            body = ",\n".join(f"    {line}" for line in column_lines)
            create = f"CREATE {unlogged}TABLE {qname} (\n{body}\n)" if column_lines else f"CREATE {unlogged}TABLE {qname} ()"
            if rel["parents"]:
                create += f"\nINHERITS ({', '.join(rel['parents'])})"
            if rel["relkind"] == "p":
                create += f"\nPARTITION BY {rel['partkey']}"
        if rel["access_method"] and rel["access_method"] != "heap" and rel["relkind"] == "r":
            create += f"\nUSING {rel['access_method']}"
        if rel["options"]:
            create += f"\nWITH ({', '.join(rel['options'])})"
        if rel["tablespace"]:
            create += f"\nTABLESPACE {rel['tablespace']}"
        create += ";"

        if rel["replident"] == "f":
            post.append(f"ALTER TABLE {qname} REPLICA IDENTITY FULL;")
        elif rel["replident"] == "n":
            post.append(f"ALTER TABLE {qname} REPLICA IDENTITY NOTHING;")

        post.extend(owner_statement("TABLE", qname, rel["owner"], self.migration_role))
        comments = comment_on("TABLE", qname, rel["comment"])
        for col in cols:
            comments.extend(comment_on("COLUMN", f"{qname}.{col['q']}", col["comment"]))

        obj.statements = [*pre, create, *([("\n".join(post))] if post else []), *([("\n".join(comments))] if comments else [])]
        return obj

    def _column(self, rel: dict[str, Any], col: dict[str, Any], seqs: list[dict[str, Any]]) -> tuple[str, int | None]:
        parts = [col["q"]]
        default = col["default"]
        serial_oid: int | None = None
        col_type = col["type"]

        serial_seq = self._serial_sequence(rel, col, seqs) if default else None
        if serial_seq is not None:
            parts.append(_SERIAL[col_type])
            serial_oid = serial_seq["oid"]
            if col["collation"]:
                parts.append(f"COLLATE {col['collation']}")
            return " ".join(parts), serial_oid

        parts.append(col_type)
        if col["collation"]:
            parts.append(f"COLLATE {col['collation']}")
        if col["identity"] in ("a", "d"):
            mode = "ALWAYS" if col["identity"] == "a" else "BY DEFAULT"
            ident = f"GENERATED {mode} AS IDENTITY"
            seq = next((s for s in seqs if s["deptype"] == "i" and s["owner_attnum"] == col["num"]), None)
            if seq is not None:
                opts = sequence_options(seq, column_type=col_type)
                if seq["schema"] != rel["schema"] or seq["name"] != _default_seq_name(rel["name"], col["name"]):
                    opts.insert(0, f"SEQUENCE NAME {seq['qname']}")
                if opts:
                    ident += " (" + " ".join(opts) + ")"
            parts.append(ident)
            return " ".join(parts), None
        if col["generated"] == "s":
            parts.append(f"GENERATED ALWAYS AS {parenthesize(default)} STORED")
        elif col["generated"] == "v":
            parts.append(f"GENERATED ALWAYS AS {parenthesize(default)} VIRTUAL")
        elif default is not None:
            parts.append(f"DEFAULT {default}")
        if col["notnull"]:
            parts.append("NOT NULL")
        return " ".join(parts), None

    def _serial_sequence(self, rel: dict[str, Any], col: dict[str, Any],
                         seqs: list[dict[str, Any]]) -> dict[str, Any] | None:
        if col["type"] not in _SERIAL or not col["notnull"] or col["identity"] or col["generated"]:
            return None
        for seq in seqs:
            if (
                seq["deptype"] == "a"
                and seq["owner_attnum"] == col["num"]
                and col["default"] == seq["nextval_expr"]
                and seq["type"] == col["type"]
                and seq["schema"] == rel["schema"]
                and seq["name"] == _default_seq_name(rel["name"], col["name"])
                and not sequence_options(seq, column_type=col["type"])
            ):
                return seq
        return None

    def _column_options(self, qname: str, col: dict[str, Any]) -> list[str]:
        out = []
        target = f"ALTER TABLE ONLY {qname} ALTER COLUMN {col['q']}"
        if col["stattarget"] is not None and col["stattarget"] >= 0:
            out.append(f"{target} SET STATISTICS {col['stattarget']};")
        if col["storage"] != col["type_storage"]:
            storage = {"p": "PLAIN", "e": "EXTERNAL", "m": "MAIN", "x": "EXTENDED"}[col["storage"]]
            out.append(f"{target} SET STORAGE {storage};")
        if col["compression"]:
            method = {"p": "pglz", "l": "lz4"}.get(col["compression"], col["compression"])
            out.append(f"{target} SET COMPRESSION {method};")
        return out


def render_tables(cat: Catalog, ctx: RenderContext) -> list[RenderedObject]:
    renderer = _TableRenderer(cat, ctx.migration_role)
    out = []
    for rel in cat.relations.values():
        if rel["relkind"] not in ("r", "p") or rel["is_extension_member"]:
            continue
        out.append(renderer.render(rel))
    return out
