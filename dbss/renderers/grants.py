"""Privileges: object GRANT/REVOKE, column privileges, default privileges and roles.

Grants files describe each object's ACL *relative to the privileges the object
would get when created by its owner on the bare platform*: PostgreSQL's built-in
defaults plus the platform's (baseline) ``ALTER DEFAULT PRIVILEGES``, e.g.
Supabase's grants to anon/authenticated/service_role in ``public``. Recreating
the object on a fresh platform and running its grants file reproduces the
current ACL. Objects whose ACL equals those defaults produce no file. Default
privileges added by migrations are rendered separately (``default_privileges.*``)
and only affect objects created after them, so apply them last.
"""

from __future__ import annotations

from typing import Any

from dbss.introspection import Catalog
from dbss.renderers.common import RenderContext, RenderedObject, literal
from dbss.renderers.routines_types import function_filename, function_identity, is_user_function, is_user_type
from dbss.utils.naming import object_filename

Acl = dict[tuple[str, str], bool]  # (grantee, privilege) -> with grant option

PRIV_ORDER = [
    "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN",
    "EXECUTE", "USAGE", "CREATE", "CONNECT", "TEMPORARY",
]
_DEFACL_WORD = {"r": "TABLES", "S": "SEQUENCES", "f": "FUNCTIONS", "T": "TYPES", "n": "SCHEMAS"}


def to_acl(entries: list[dict[str, Any]] | None) -> Acl:
    acl: Acl = {}
    for e in entries or []:
        key = (e["grantee"], e["privilege"])
        acl[key] = acl.get(key, False) or bool(e["grantable"])
    return acl


def union(a: Acl, b: Acl) -> Acl:
    out = dict(a)
    for key, grantable in b.items():
        out[key] = out.get(key, False) or grantable
    return out


def _grantee_key(grantee: str) -> tuple[int, str]:
    return (0, "") if grantee == "PUBLIC" else (1, grantee)


def _privs_text(privs: set[str], all_privs: set[str]) -> str:
    if len(all_privs) > 1 and privs == all_privs:
        return "ALL"
    return ", ".join(p for p in PRIV_ORDER if p in privs) + "".join(
        f", {p}" for p in sorted(privs - set(PRIV_ORDER)))


def acl_statements(on: str, expected: Acl, actual: Acl, all_privs: set[str], prefix: str = "") -> list[str]:
    """REVOKE/GRANT statements turning ``expected`` into ``actual``.

    Grantees receiving the same change are combined into one statement.
    """
    grantees = sorted({g for g, _ in expected} | {g for g, _ in actual}, key=_grantee_key)
    groups: dict[tuple[int, str], list[str]] = {}
    for grantee in grantees:
        exp = {p: go for (g, p), go in expected.items() if g == grantee}
        act = {p: go for (g, p), go in actual.items() if g == grantee}
        changes = (
            {p for p in exp if p not in act},  # 0: revoke
            {p for p in exp if p in act and exp[p] and not act[p]},  # 1: revoke grant option
            {p for p in act if p not in exp and not act[p]},  # 2: grant
            {p for p in act if act[p] and not exp.get(p, False)},  # 3: grant with grant option
        )
        for op, privs in enumerate(changes):
            if privs:
                groups.setdefault((op, _privs_text(privs, all_privs)), []).append(grantee)
    out = []
    for (op, privs), who_list in sorted(groups.items(), key=lambda kv: (kv[0][0], _grantee_key(kv[1][0]), kv[0][1])):
        who = ", ".join(who_list)
        if op == 0:
            out.append(f"{prefix}REVOKE {privs} ON {on} FROM {who};")
        elif op == 1:
            out.append(f"{prefix}REVOKE GRANT OPTION FOR {privs} ON {on} FROM {who};")
        elif op == 2:
            out.append(f"{prefix}GRANT {privs} ON {on} TO {who};")
        else:
            out.append(f"{prefix}GRANT {privs} ON {on} TO {who} WITH GRANT OPTION;")
    return out


def _all_privs(builtin: Acl) -> set[str]:
    return {p for _, p in builtin}


class _DefaultAcls:
    def __init__(self, cat: Catalog):
        self.entries: dict[tuple[str, str | None, str], Acl] = {}
        for row in cat.default_acls:
            self.entries[(row["role"], row["schema"], row["objtype"])] = to_acl(row["acl"])

    def expected(self, builtin: Acl, owner: str, schema: str | None, objtype: str) -> Acl:
        base = self.entries.get((owner, None, objtype))
        acl = dict(builtin if base is None else base)
        if schema is not None and objtype != "n":
            acl = union(acl, self.entries.get((owner, schema, objtype), {}))
        return acl


def _object(identity: str, filename: str, label: str, schema: str | None, stmts: list[str],
            table: str | None = None) -> RenderedObject:
    return RenderedObject(kind="grant", identity=identity, filename=filename, label=label,
                          schema=schema, statements=stmts, table=table)


def _column_grants(on: str, columns: list[dict[str, Any]]) -> list[str]:
    groups: dict[tuple[str, bool, str], list[str]] = {}
    for col in columns:
        for (grantee, priv), grantable in to_acl(col["acl"]).items():
            groups.setdefault((grantee, grantable, priv), []).append(col["q"])
    out = []
    for (grantee, grantable, priv) in sorted(groups, key=lambda k: (_grantee_key(k[0]), k[1],
                                                                  PRIV_ORDER.index(k[2]) if k[2] in PRIV_ORDER else 99)):
        cols = ", ".join(groups[(grantee, grantable, priv)])
        suffix = " WITH GRANT OPTION" if grantable else ""
        out.append(f"GRANT {priv} ({cols}) ON {on} TO {grantee}{suffix};")
    return out


_ROLE_FLAGS = (("super", "SUPERUSER"), ("createdb", "CREATEDB"), ("createrole", "CREATEROLE"),
               ("login", "LOGIN"), ("replication", "REPLICATION"), ("bypassrls", "BYPASSRLS"))
_LIST_SETTINGS = ("search_path", "temp_tablespaces", "session_preload_libraries", "local_preload_libraries")


def _is_implicit_creator_grant(member: dict[str, Any], m: dict[str, Any], migration_role: str) -> bool:
    """PostgreSQL 16+ grants a role's (non-superuser) creator ADMIN on it automatically."""
    return member["name"] == migration_role and m["admin"] and m.get("inherit") is False and m.get("set") is False


def _membership(member: dict[str, Any], m: dict[str, Any]) -> str:
    opts = []
    if m["admin"]:
        opts.append("ADMIN TRUE")
    if m.get("inherit") is not None and m["inherit"] != member["inherit"]:
        opts.append(f"INHERIT {'TRUE' if m['inherit'] else 'FALSE'}")
    if m.get("set") is False:
        opts.append("SET FALSE")
    return f"GRANT {m['role']} TO {member['q']}" + (f" WITH {', '.join(opts)}" if opts else "") + ";"


def render_roles(cat: Catalog, ctx: RenderContext) -> list[RenderedObject]:
    """Roles created or changed by migrations.

    A membership is written to the file of the role the migrations created
    (the member if it is new, otherwise the granted role), so platform roles
    such as ``authenticated`` only get a file when they themselves change.
    """
    baseline_names = {r["name"] for r in ctx.baseline.roles}
    by_q = {r["q"]: r for r in cat.roles}
    grants: dict[str, list[str]] = {r["name"]: [] for r in cat.roles}
    for member in cat.roles:
        for m in member["member_of"] or []:
            if _is_implicit_creator_grant(member, m, ctx.migration_role):
                continue
            granted = by_q.get(m["role"])
            owner = member["name"]
            if member["name"] in baseline_names and granted is not None and granted["name"] not in baseline_names:
                owner = granted["name"]
            grants[owner].append(_membership(member, m))

    out = []
    for role in cat.roles:
        attrs = [word for flag, word in _ROLE_FLAGS if role[flag]]
        if not role["inherit"]:
            attrs.append("NOINHERIT")
        if role["connlimit"] != -1:
            attrs.append(f"CONNECTION LIMIT {role['connlimit']}")
        if role["validuntil"]:
            attrs.append(f"VALID UNTIL {literal(role['validuntil'])}")
        with_attrs = (" WITH " + " ".join(attrs)) if attrs else ""
        if role["name"] in baseline_names:
            stmts = [f"-- Role {role['q']} is created by the platform; only its current settings are listed."]
            if attrs:
                stmts.append(f"ALTER ROLE {role['q']}{with_attrs};")
        else:
            stmts = [f"CREATE ROLE {role['q']}{with_attrs};"]
            if role["login"]:
                stmts.append("-- The role's password (if any) is not reproduced by DBSS.")
        settings = []
        for entry in sorted(role["config"] or []):
            name, _, value = entry.partition("=")
            value_sql = value if name in _LIST_SETTINGS else literal(value)
            settings.append(f"ALTER ROLE {role['q']} SET {name} TO {value_sql};")
        if settings:
            stmts.append("\n".join(settings))
        if grants[role["name"]]:
            stmts.append("\n".join(sorted(grants[role["name"]])))
        out.append(_object(f"role.{role['name']}", object_filename("role", role["name"]),
                           f"role {role['q']}", None, stmts))
    return out


def render_grants(cat: Catalog, ctx: RenderContext) -> list[RenderedObject]:
    # Expected ACLs use the platform's default privileges (baseline), so a grants file lists
    # exactly what differs from what a fresh object would get before any migration ran.
    defacls = _DefaultAcls(ctx.baseline)
    out: list[RenderedObject] = []

    def add_acl_object(prefix: str, keyword: str, target: str, schema: str | None, name_parts: tuple[str, ...],
                       row: dict[str, Any], objtype: str | None, extra: list[str] | None = None,
                       filename: str | None = None, identity: str | None = None, table: str | None = None) -> None:
        builtin = to_acl(row["builtin_acl"])
        expected = defacls.expected(builtin, row["owner"], schema, objtype) if objtype else builtin
        stmts = acl_statements(f"{keyword} {target}", expected, to_acl(row["acl"]), _all_privs(builtin))
        stmts += extra or []
        if not stmts:
            return
        out.append(_object(
            identity or ".".join((prefix, *name_parts)),
            filename or object_filename(prefix, *name_parts),
            f"privileges on {keyword.lower()} {target}",
            schema,
            ["\n".join(stmts)],
            table=table,
        ))

    for sch in cat.schemas:
        if not sch["is_extension_member"]:
            add_acl_object("schema", "SCHEMA", sch["q"], sch["name"], (sch["name"],), sch, "n")

    for rel in cat.relations.values():
        if rel["is_extension_member"] or rel["relkind"] == "c":
            continue
        is_seq = rel["relkind"] == "S"
        prefix, keyword = ("sequence", "SEQUENCE") if is_seq else ("table", "TABLE")
        extra = [] if is_seq else _column_grants(f"TABLE {rel['qname']}", cat.table_columns(rel["oid"]))
        table = f"{rel['schema']}.{rel['name']}" if rel["relkind"] in ("r", "p") else None
        add_acl_object(prefix, keyword, rel["qname"], rel["schema"], (rel["schema"], rel["name"]), rel,
                       "S" if is_seq else "r", extra, table=table)

    for fn in cat.functions:
        if not is_user_function(fn):
            continue
        keyword = "PROCEDURE" if fn["kind"] == "p" else "FUNCTION"
        add_acl_object("function", keyword, f"{fn['qname']}({fn['identity_args']})", fn["schema"], (), fn, "f",
                       filename=function_filename(("function",), fn), identity="function." + function_identity(fn))

    for typ in cat.types:
        if is_user_type(typ):
            keyword = "DOMAIN" if typ["typtype"] == "d" else "TYPE"
            add_acl_object("type", keyword, typ["qname"], typ["schema"], (typ["schema"], typ["name"]), typ, "T")

    for db in cat.raw.get("databases", []):
        add_acl_object("database", "DATABASE", db["q"], None, (db["name"],), db, None)
    for lang in cat.raw.get("languages", []):
        add_acl_object("language", "LANGUAGE", lang["q"], None, (lang["name"],), lang, None)

    # Default privileges, one file per (role, schema-or-global).
    groups: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for row in cat.default_acls:
        groups.setdefault((row["role"], row["schema"]), []).append(row)
    for (role, schema), rows in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        stmts = []
        for row in sorted(rows, key=lambda r: list(_DEFACL_WORD).index(r["objtype"])):
            builtin = to_acl(row["builtin_acl"])
            expected = builtin if schema is None else {}
            prefix = f"ALTER DEFAULT PRIVILEGES FOR ROLE {row['role_q']}"
            if schema is not None:
                prefix += f" IN SCHEMA {row['schema_q']}"
            stmts += acl_statements(_DEFACL_WORD[row["objtype"]], expected, to_acl(row["acl"]),
                                    _all_privs(builtin), prefix=prefix + "\n    ")
        if not stmts:
            continue
        if schema is None:
            identity, filename = f"default_privileges.{role}", object_filename("default_privileges", role, "global")
            label = f"default privileges for role {rows[0]['role_q']}"
        else:
            identity = f"default_privileges.{role}.{schema}"
            filename = object_filename("default_privileges", role, "schema", schema)
            label = f"default privileges for role {rows[0]['role_q']} in schema {rows[0]['schema_q']}"
        out.append(_object(identity, filename, label, schema, ["\n\n".join(stmts)]))

    out.extend(render_roles(cat, ctx))

    for other in cat.raw.get("other_acls", []):
        obj = _object(f"other.{other['target']}", object_filename("other", other["target"]),
                      f"privileges on {other['target']}", None, [f"-- {other['acl']}"])
        obj.error = (f"privileges on {other['target']} ({other['acl']}) are outside the set DBSS can represent "
                     "(schemas, tables, columns, sequences, functions, types, database, languages, default privileges, roles)")
        out.append(obj)
    return out
