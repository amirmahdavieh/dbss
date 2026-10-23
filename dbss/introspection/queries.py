"""Catalog queries. All run in one psql call and return a single JSON document.

The session uses ``search_path = pg_catalog`` so every ``pg_get_*def``,
``format_type`` and ``regclass`` output is fully schema-qualified and does not
depend on the replay session's search path.
"""

from __future__ import annotations

NSP_FILTER = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp\\_%'"
)

PREAMBLE = """
SET search_path = pg_catalog;
SET DateStyle = ISO, YMD;
SET IntervalStyle = postgres;
SET extra_float_digits = 3;
"""


def acl(expr: str) -> str:
    """JSON array of {grantee, privilege, grantable} for an aclitem[] expression."""
    return (
        "(SELECT coalesce(json_agg(json_build_object("
        "'grantee', CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END, "
        "'privilege', a.privilege_type, 'grantable', a.is_grantable)), '[]'::json) "
        f"FROM aclexplode({expr}) a)"
    )


def ext_member(classid: str, objid: str) -> str:
    return (
        f"EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = '{classid}'::regclass "
        f"AND d.objid = {objid} AND d.deptype = 'e')"
    )


_REL_ACL_TYPE = "(CASE WHEN c.relkind = 'S' THEN 's' ELSE 'r' END)::\"char\""
_DEFACL_BUILTIN = (
    "acldefault((CASE d.defaclobjtype WHEN 'S' THEN 's' ELSE d.defaclobjtype::text END)::\"char\", d.defaclrole)"
)

QUERIES: dict[str, str] = {
    "schemas": f"""
SELECT n.oid::bigint AS oid, n.nspname AS name, quote_ident(n.nspname) AS q,
       pg_get_userbyid(n.nspowner) AS owner,
       {ext_member('pg_namespace', 'n.oid')} AS is_extension_member,
       {acl("coalesce(n.nspacl, acldefault('n', n.nspowner))")} AS acl,
       {acl("acldefault('n', n.nspowner)")} AS builtin_acl
FROM pg_namespace n
WHERE {NSP_FILTER}
""",
    "relations": f"""
SELECT c.oid::bigint AS oid, n.nspname AS schema, c.relname AS name,
       format('%I.%I', n.nspname, c.relname) AS qname,
       c.relkind::text AS relkind, c.relpersistence::text AS persistence,
       pg_get_userbyid(c.relowner) AS owner,
       c.relrowsecurity AS rls, c.relforcerowsecurity AS force_rls,
       c.relreplident::text AS replident, c.reloptions AS options,
       c.relispartition AS is_partition,
       CASE WHEN c.relispartition THEN pg_get_expr(c.relpartbound, c.oid) END AS partbound,
       CASE WHEN c.relkind = 'p' THEN pg_get_partkeydef(c.oid) END AS partkey,
       (SELECT json_agg(i.inhparent::regclass::text ORDER BY i.inhseqno)
          FROM pg_inherits i WHERE i.inhrelid = c.oid) AS parents,
       CASE WHEN c.reloftype <> 0 THEN format_type(c.reloftype, NULL) END AS of_type,
       am.amname AS access_method,
       ts.spcname AS tablespace,
       obj_description(c.oid, 'pg_class') AS comment,
       {ext_member('pg_class', 'c.oid')} AS is_extension_member,
       {acl(f"coalesce(c.relacl, acldefault({_REL_ACL_TYPE}, c.relowner))")} AS acl,
       {acl(f"acldefault({_REL_ACL_TYPE}, c.relowner)")} AS builtin_acl
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_am am ON am.oid = c.relam
LEFT JOIN pg_tablespace ts ON ts.oid = c.reltablespace
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S', 'c') AND {NSP_FILTER}
""",
    "columns": f"""
SELECT a.attrelid::bigint AS relid, a.attnum AS num, a.attname AS name, quote_ident(a.attname) AS q,
       format_type(a.atttypid, a.atttypmod) AS type,
       a.attnotnull AS notnull,
       pg_get_expr(ad.adbin, ad.adrelid) AS "default",
       a.attidentity::text AS identity, a.attgenerated::text AS generated,
       CASE WHEN a.attcollation <> 0 AND a.attcollation <> t.typcollation
            THEN format('%I.%I', cn.nspname, co.collname) END AS collation,
       a.attislocal AS is_local,
       a.attstattarget::int AS stattarget,
       a.attstorage::text AS storage, t.typstorage::text AS type_storage,
       a.attcompression::text AS compression,
       col_description(a.attrelid, a.attnum) AS comment,
       {acl("a.attacl")} AS acl
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
LEFT JOIN pg_collation co ON co.oid = a.attcollation
LEFT JOIN pg_namespace cn ON cn.oid = co.collnamespace
WHERE a.attnum > 0 AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'c') AND {NSP_FILTER}
""",
    "sequences": f"""
SELECT s.seqrelid::bigint AS oid, n.nspname AS schema, c.relname AS name,
       format('%I.%I', n.nspname, c.relname) AS qname,
       format_type(s.seqtypid, NULL) AS type,
       s.seqstart::text AS start, s.seqincrement::text AS increment,
       s.seqmax::text AS max, s.seqmin::text AS min, s.seqcache::text AS cache, s.seqcycle AS cycle,
       d.refobjid::bigint AS owner_relid, d.refobjsubid AS owner_attnum, d.deptype::text AS deptype,
       format('nextval(%L::regclass)', s.seqrelid::regclass::text) AS nextval_expr
FROM pg_sequence s
JOIN pg_class c ON c.oid = s.seqrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_depend d ON d.classid = 'pg_class'::regclass AND d.objid = s.seqrelid
     AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
WHERE {NSP_FILTER}
""",
    "constraints": f"""
SELECT co.oid::bigint AS oid, co.conname AS name, quote_ident(co.conname) AS q,
       co.contype::text AS type, co.conrelid::bigint AS relid, co.contypid::bigint AS typid,
       pg_get_constraintdef(co.oid) AS def, co.convalidated AS validated,
       co.conislocal AS is_local, co.conparentid::bigint AS parentid, co.conindid::bigint AS indid,
       obj_description(co.oid, 'pg_constraint') AS comment
FROM pg_constraint co
JOIN pg_namespace n ON n.oid = co.connamespace
WHERE {NSP_FILTER}
""",
    "indexes": f"""
SELECT i.indexrelid::bigint AS oid, ic.relname AS name, quote_ident(ic.relname) AS q,
       n.nspname AS schema, format('%I.%I', n.nspname, ic.relname) AS qname,
       i.indrelid::bigint AS relid, pg_get_indexdef(i.indexrelid) AS def,
       EXISTS (SELECT 1 FROM pg_constraint co WHERE co.conindid = i.indexrelid
               AND co.conrelid = i.indrelid AND co.contype IN ('p', 'u', 'x')) AS constraint_backed,
       EXISTS (SELECT 1 FROM pg_inherits h WHERE h.inhrelid = i.indexrelid) AS is_partition_child,
       i.indisclustered AS clustered, i.indisreplident AS replident, i.indisvalid AS valid,
       obj_description(i.indexrelid, 'pg_class') AS comment,
       {ext_member('pg_class', 'i.indexrelid')} AS is_extension_member
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_namespace n ON n.oid = ic.relnamespace
WHERE {NSP_FILTER}
""",
    "triggers": f"""
SELECT t.oid::bigint AS oid, t.tgname AS name, quote_ident(t.tgname) AS q,
       t.tgrelid::bigint AS relid, pg_get_triggerdef(t.oid) AS def, t.tgenabled::text AS enabled,
       t.tgparentid::bigint AS parentid, t.tgconstraint::bigint AS constraint_oid,
       obj_description(t.oid, 'pg_trigger') AS comment
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT t.tgisinternal AND {NSP_FILTER}
""",
    "policies": f"""
SELECT pol.oid::bigint AS oid, pol.polname AS name, quote_ident(pol.polname) AS q,
       pol.polrelid::bigint AS relid, pol.polcmd::text AS cmd, pol.polpermissive AS permissive,
       (SELECT json_agg(CASE WHEN r = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(r)) END)
          FROM unnest(pol.polroles) r) AS roles,
       pg_get_expr(pol.polqual, pol.polrelid) AS qual,
       pg_get_expr(pol.polwithcheck, pol.polrelid) AS with_check,
       obj_description(pol.oid, 'pg_policy') AS comment
FROM pg_policy pol
JOIN pg_class c ON c.oid = pol.polrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE {NSP_FILTER}
""",
    "functions": f"""
SELECT p.oid::bigint AS oid, n.nspname AS schema, p.proname AS name,
       format('%I.%I', n.nspname, p.proname) AS qname, p.prokind::text AS kind,
       pg_get_function_identity_arguments(p.oid) AS identity_args,
       (SELECT coalesce(json_agg(format_type(u.t, NULL) ORDER BY u.o), '[]'::json)
          FROM unnest(p.proargtypes::oid[]) WITH ORDINALITY AS u(t, o)) AS argtypes,
       CASE WHEN p.prokind IN ('f', 'p', 'w') AND NOT x.is_ext THEN pg_get_functiondef(p.oid) END AS def,
       pg_get_userbyid(p.proowner) AS owner,
       obj_description(p.oid, 'pg_proc') AS comment,
       x.is_ext AS is_extension_member,
       EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid
               AND d.deptype = 'i') AS is_internal,
       {acl("coalesce(p.proacl, acldefault('f', p.proowner))")} AS acl,
       {acl("acldefault('f', p.proowner)")} AS builtin_acl
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
CROSS JOIN LATERAL (SELECT {ext_member('pg_proc', 'p.oid')} AS is_ext) x
WHERE {NSP_FILTER}
""",
    "types": f"""
SELECT t.oid::bigint AS oid, n.nspname AS schema, t.typname AS name,
       format('%I.%I', n.nspname, t.typname) AS qname, t.typtype::text AS typtype,
       t.typrelid::bigint AS relid, rc.relkind::text AS relkind,
       t.typelem::bigint AS elem, t.typcategory::text AS category, t.typisdefined AS defined,
       pg_get_userbyid(t.typowner) AS owner,
       obj_description(t.oid, 'pg_type') AS comment,
       {ext_member('pg_type', 't.oid')} AS is_extension_member,
       CASE WHEN t.typtype = 'e' THEN
         (SELECT json_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_enum e WHERE e.enumtypid = t.oid)
       END AS labels,
       CASE WHEN t.typtype = 'd' THEN format_type(t.typbasetype, t.typtypmod) END AS base_type,
       t.typnotnull AS notnull,
       pg_get_expr(t.typdefaultbin, 0) AS "default",
       CASE WHEN t.typtype = 'd' AND t.typcollation <> 0 AND t.typcollation <> bt.typcollation
            THEN (SELECT format('%I.%I', cn.nspname, co.collname) FROM pg_collation co
                  JOIN pg_namespace cn ON cn.oid = co.collnamespace WHERE co.oid = t.typcollation)
       END AS collation,
       CASE WHEN t.typtype = 'r' THEN
         (SELECT json_build_object(
             'subtype', format_type(r.rngsubtype, NULL),
             'opclass', CASE WHEN NOT opc.opcdefault THEN format('%I.%I', ocn.nspname, opc.opcname) END,
             'collation', CASE WHEN r.rngcollation <> 0 THEN
                 (SELECT format('%I.%I', cn.nspname, co.collname) FROM pg_collation co
                  JOIN pg_namespace cn ON cn.oid = co.collnamespace WHERE co.oid = r.rngcollation) END,
             'canonical', CASE WHEN r.rngcanonical <> 0 THEN r.rngcanonical::regproc::text END,
             'subdiff', CASE WHEN r.rngsubdiff <> 0 THEN r.rngsubdiff::regproc::text END,
             'multirange', format_type(r.rngmultitypid, NULL))
          FROM pg_range r
          JOIN pg_opclass opc ON opc.oid = r.rngsubopc
          JOIN pg_namespace ocn ON ocn.oid = opc.opcnamespace
          WHERE r.rngtypid = t.oid)
       END AS range,
       {acl("coalesce(t.typacl, acldefault('T', t.typowner))")} AS acl,
       {acl("acldefault('T', t.typowner)")} AS builtin_acl
FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace
LEFT JOIN pg_type bt ON bt.oid = t.typbasetype
LEFT JOIN pg_class rc ON rc.oid = t.typrelid
WHERE {NSP_FILTER} AND t.typtype IN ('b', 'c', 'd', 'e', 'r', 'm', 'p')
""",
    "roles": """
SELECT r.oid::bigint AS oid, r.rolname AS name, quote_ident(r.rolname) AS q,
       r.rolsuper AS super, r.rolinherit AS inherit, r.rolcreaterole AS createrole,
       r.rolcreatedb AS createdb, r.rolcanlogin AS login, r.rolreplication AS replication,
       r.rolbypassrls AS bypassrls, r.rolconnlimit AS connlimit, r.rolvaliduntil::text AS validuntil,
       r.rolconfig AS config,
       (SELECT json_agg(json_build_object(
                  'role', quote_ident(g.rolname),
                  'admin', m.admin_option,
                  'grantor', pg_get_userbyid(m.grantor),
                  'inherit', (to_jsonb(m) ->> 'inherit_option')::boolean,
                  'set', (to_jsonb(m) ->> 'set_option')::boolean))
          FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
         WHERE m.member = r.oid) AS member_of
FROM pg_roles r
WHERE r.rolname NOT LIKE 'pg\\_%'
""",
    "default_acls": f"""
SELECT pg_get_userbyid(d.defaclrole) AS role, quote_ident(pg_get_userbyid(d.defaclrole)) AS role_q,
       n.nspname AS schema, quote_ident(n.nspname) AS schema_q,
       d.defaclobjtype::text AS objtype,
       {acl("d.defaclacl")} AS acl,
       {acl(_DEFACL_BUILTIN)} AS builtin_acl
FROM pg_default_acl d
LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
""",
    "databases": f"""
SELECT d.datname AS name, quote_ident(d.datname) AS q, pg_get_userbyid(d.datdba) AS owner,
       {acl("coalesce(d.datacl, acldefault('d', d.datdba))")} AS acl,
       {acl("acldefault('d', d.datdba)")} AS builtin_acl
FROM pg_database d
WHERE d.datname = current_database()
""",
    "languages": f"""
SELECT l.lanname AS name, quote_ident(l.lanname) AS q, pg_get_userbyid(l.lanowner) AS owner,
       {acl("coalesce(l.lanacl, acldefault('l', l.lanowner))")} AS acl,
       {acl("acldefault('l', l.lanowner)")} AS builtin_acl
FROM pg_language l
WHERE l.lanname NOT IN ('internal', 'c')
""",
    "other_acls": """
SELECT 'foreign data wrapper ' || quote_ident(fdwname) AS target, fdwacl::text AS acl
  FROM pg_foreign_data_wrapper WHERE fdwacl IS NOT NULL
UNION ALL
SELECT 'foreign server ' || quote_ident(srvname), srvacl::text FROM pg_foreign_server WHERE srvacl IS NOT NULL
UNION ALL
SELECT 'parameter ' || parname, paracl::text FROM pg_parameter_acl
UNION ALL
SELECT 'large object ' || oid::text, lomacl::text FROM pg_largeobject_metadata WHERE lomacl IS NOT NULL
UNION ALL
SELECT 'tablespace ' || quote_ident(spcname), spcacl::text FROM pg_tablespace WHERE spcacl IS NOT NULL
""",
    "extensions": """
SELECT e.extname AS name, n.nspname AS schema, e.extversion AS version
FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
""",
    "event_triggers": """
SELECT e.evtname AS name FROM pg_event_trigger e
""",
    "rules": f"""
SELECT format('%I.%I', n.nspname, c.relname) AS relation, r.rulename AS name
FROM pg_rewrite r
JOIN pg_class c ON c.oid = r.ev_class
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE r.rulename <> '_RETURN' AND {NSP_FILTER}
""",
    "publications": """
SELECT p.pubname AS name, p.puballtables AS all_tables,
       (SELECT json_agg(format('%I.%I', pt.schemaname, pt.tablename) ORDER BY 1)
          FROM pg_publication_tables pt WHERE pt.pubname = p.pubname) AS tables
FROM pg_publication p
""",
    "statistics": f"""
SELECT format('%I.%I', n.nspname, s.stxname) AS qname
FROM pg_statistic_ext s JOIN pg_namespace n ON n.oid = s.stxnamespace
WHERE {NSP_FILTER}
""",
    "collations": f"""
SELECT format('%I.%I', n.nspname, c.collname) AS qname
FROM pg_collation c JOIN pg_namespace n ON n.oid = c.collnamespace
WHERE {NSP_FILTER}
""",
}


def snapshot_sql() -> str:
    parts = ",\n".join(
        f"'{key}', (SELECT coalesce(json_agg(dbss_row), '[]'::json) FROM ({sql}) dbss_row)"
        for key, sql in QUERIES.items()
    )
    return PREAMBLE + f"SELECT json_build_object(\n'server_version_num', current_setting('server_version_num')::int,\n{parts});\n"
