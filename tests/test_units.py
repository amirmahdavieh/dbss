"""Fast unit tests (no Docker)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbss.config import DEFAULT_CONFIG, parse_config
from dbss.errors import ConfigError
from dbss.kinds import KINDS, parse_kind
from dbss.migrations import discover_migrations, find_migration
from dbss.renderers.common import RenderedObject, parenthesize
from dbss.renderers.grants import acl_statements
from dbss.renderers.tables import sequence_options
from dbss.state import FileRecord, MigrationRecord, State, load_state, save_state
from dbss.sync.analysis import analyze
from dbss.sync.files import commit_files, finish_commit, plan_files
from dbss.utils.fs import sha256_text
from dbss.utils.naming import disambiguate_case, object_filename


# -- naming ------------------------------------------------------------------------------

def test_filenames_are_collision_safe():
    assert object_filename("public", "profiles") == "public.profiles.sql"
    assert object_filename("a.b", "c") != object_filename("a", "b.c")
    assert object_filename("public", 'My "Table"') == "public.My %22Table%22.sql"
    assert object_filename(" x ", "y") == "%20x%20.y.sql"
    assert object_filename("public", "../etc") == "public.%2E%2E%2Fetc.sql"
    assert object_filename("con", "x").startswith("%63on.")


def test_function_signature_filenames():
    assert object_filename("public", "add", signature=["integer", "integer"]) == "public.add(integer, integer).sql"
    assert object_filename("public", "f", signature=[]) == "public.f().sql"
    assert object_filename("public", "f", signature=["timestamp with time zone", "text[]"]) == \
        "public.f(timestamp with time zone, text[]).sql"
    assert object_filename("public", "f", signature=['public."a,b"']) == "public.f(public.%22a%2Cb%22).sql"


def test_long_filenames_are_truncated_deterministically():
    name = object_filename("public", "x" * 63, signature=["character varying"] * 10)
    assert len(name) <= 180 and name.endswith(".sql")
    assert name == object_filename("public", "x" * 63, signature=["character varying"] * 10)


def test_case_collisions_get_disambiguated():
    out = disambiguate_case({"public.Users": "tables/public.Users.sql", "public.users": "tables/public.users.sql",
                             "public.other": "tables/public.other.sql"})
    assert out["public.other"] == "tables/public.other.sql"
    assert out["public.Users"] != out["public.users"]
    assert out["public.Users"].lower() != out["public.users"].lower()


def test_parse_kind_accepts_plural():
    assert parse_kind("tables") == "table"
    assert parse_kind("Policy") == "policy"
    assert parse_kind("indexes") == "index"
    with pytest.raises(ConfigError):
        parse_kind("view")


# -- config ------------------------------------------------------------------------------

def test_default_config_parses(tmp_path: Path):
    cfg = parse_config(DEFAULT_CONFIG, tmp_path)
    assert cfg.output_dir == "sql" and cfg.schemas is None and cfg.docker.migration_role == "postgres"


@pytest.mark.parametrize("patch, fragment", [
    ({"output_dir": "../outside"}, "inside the project root"),
    ({"output_dir": "."}, "cannot be the project root"),
    ({"output_dir": "supabase/migrations/sql"}, "must not contain each other"),
    ({"output_dir": ".dbss/out"}, ".dbss"),
    ({"schemas": []}, "schemas"),
    ({"bogus": 1}, "unknown keys"),
    ({"bootstrap": ["builtin:nope"]}, "unknown bootstrap"),
    ({"mode": "remote"}, "mode"),
])
def test_invalid_config_is_rejected(tmp_path: Path, patch, fragment):
    with pytest.raises(ConfigError) as exc:
        parse_config({**DEFAULT_CONFIG, **patch}, tmp_path)
    assert fragment in exc.value.message


def test_fingerprint_tracks_output_settings(tmp_path: Path):
    a = parse_config(DEFAULT_CONFIG, tmp_path)
    b = parse_config({**DEFAULT_CONFIG, "schemas": ["public"]}, tmp_path)
    c = parse_config({**DEFAULT_CONFIG, "docker": {**DEFAULT_CONFIG["docker"], "startup_timeout_seconds": 5}}, tmp_path)
    assert a.fingerprint() != b.fingerprint()
    assert a.fingerprint() == c.fingerprint()


# -- migrations --------------------------------------------------------------------------

def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_discovery_sorts_and_skips_invalid_names(tmp_path: Path):
    d = tmp_path / "m"
    _write(d / "20260102_b.sql", "select 2;")
    _write(d / "20260101_a.sql", "select 1;")
    _write(d / "notes.sql", "")
    _write(d / "README.md", "")
    migrations, warnings = discover_migrations(d)
    assert [m.name for m in migrations] == ["20260101_a.sql", "20260102_b.sql"]
    assert any("notes.sql" in w for w in warnings)


def test_duplicate_versions_fail(tmp_path: Path):
    d = tmp_path / "m"
    _write(d / "1_a.sql", "")
    _write(d / "1_b.sql", "")
    with pytest.raises(ConfigError):
        discover_migrations(d)


def test_crlf_and_lf_hash_identically(tmp_path: Path):
    a = _write(tmp_path / "a" / "1_x.sql", "")
    a.write_bytes(b"select 1;\r\nselect 2;\r\n")
    b = _write(tmp_path / "b" / "1_x.sql", "")
    b.write_bytes(b"select 1;\nselect 2;\n")
    assert discover_migrations(a.parent)[0][0].sha256 == discover_migrations(b.parent)[0][0].sha256


def test_find_migration_rejects_outside_files(tmp_path: Path):
    d = tmp_path / "m"
    _write(d / "1_a.sql", "")
    other = _write(tmp_path / "1_a.sql", "")
    migrations, _ = discover_migrations(d)
    assert find_migration(migrations, d, str(d / "1_a.sql")) == 0
    assert find_migration(migrations, d, "1_a.sql") == 0
    with pytest.raises(ConfigError):
        find_migration(migrations, d, str(other))


# -- analysis ----------------------------------------------------------------------------

def _project(tmp_path: Path, names: list[str]):
    cfg = parse_config(DEFAULT_CONFIG, tmp_path)
    for n in names:
        _write(cfg.migrations_path / n, f"-- {n}\n")
    return cfg, discover_migrations(cfg.migrations_path)[0]


def _state(cfg, migrations, completed: dict[str, set[str]]) -> State:
    return State(config_fingerprint=cfg.fingerprint(), files={"tables/x.sql": FileRecord("table", "h", "t")},
                 migrations=[MigrationRecord(m.name, m.sha256, completed[m.name]) for m in migrations
                             if m.name in completed])


def test_watermarks_and_pending(tmp_path: Path):
    cfg, migs = _project(tmp_path, ["1_a.sql", "2_b.sql", "3_c.sql"])
    state = _state(cfg, migs, {"1_a.sql": set(KINDS), "2_b.sql": {"table"}})
    a = analyze(cfg, state, migs)
    assert not a.rebuild_required
    assert a.watermarks["table"] == 2 and a.watermarks["policy"] == 1
    pending = {m.name: kinds for m, kinds in a.pending_migrations}
    assert "table" not in pending["2_b.sql"] and "policy" in pending["2_b.sql"]
    assert set(pending["3_c.sql"]) == set(KINDS)


def test_modified_missing_and_inserted_history_require_rebuild(tmp_path: Path):
    cfg, migs = _project(tmp_path, ["1_a.sql", "3_c.sql"])
    state = _state(cfg, migs, {m.name: set(KINDS) for m in migs})
    _write(cfg.migrations_path / "2_b.sql", "-- inserted\n")
    _write(cfg.migrations_path / "1_a.sql", "-- edited\n")
    (cfg.migrations_path / "3_c.sql").unlink()
    migs2 = discover_migrations(cfg.migrations_path)[0]
    a = analyze(cfg, state, migs2)
    assert a.modified == ["1_a.sql"] and a.missing == ["3_c.sql"]
    assert a.rebuild_required


def test_new_migration_inserted_before_processed_one(tmp_path: Path):
    cfg, migs = _project(tmp_path, ["1_a.sql", "3_c.sql"])
    state = _state(cfg, migs, {m.name: set(KINDS) for m in migs})
    _write(cfg.migrations_path / "2_b.sql", "-- inserted\n")
    a = analyze(cfg, state, discover_migrations(cfg.migrations_path)[0])
    assert a.out_of_order == ["2_b.sql"] and a.rebuild_required


def test_config_change_requires_rebuild(tmp_path: Path):
    cfg, migs = _project(tmp_path, ["1_a.sql"])
    state = _state(cfg, migs, {"1_a.sql": set(KINDS)})
    other = parse_config({**DEFAULT_CONFIG, "output_dir": "generated"}, tmp_path)
    assert analyze(other, state, migs).rebuild_required
    assert not analyze(cfg, state, migs).rebuild_required


# -- state -------------------------------------------------------------------------------

def test_state_roundtrip(tmp_path: Path):
    s = State(config_fingerprint="abc", migrations=[MigrationRecord("1_a.sql", "h", {"table", "grant"})],
              files={"tables/t.sql": FileRecord("table", "x", "table t", table="public.t")})
    save_state(tmp_path / "state.json", s)
    loaded = load_state(tmp_path / "state.json")
    assert loaded.migrations[0].completed == {"table", "grant"}
    assert loaded.files["tables/t.sql"].table == "public.t"
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["migrations"][0]["completed"] == ["table", "grant"]


# -- file plans --------------------------------------------------------------------------

def _obj(kind: str, identity: str, body: str) -> RenderedObject:
    return RenderedObject(kind=kind, identity=identity, filename=f"{identity}.sql", label=f"{kind} {identity}",
                          schema="public", statements=[body])


def test_plan_create_update_delete_and_conflicts(tmp_path: Path):
    out = tmp_path / "sql"
    keep = _obj("table", "public.keep", "CREATE TABLE keep ();")
    old = _obj("table", "public.old", "CREATE TABLE old ();")
    _write(out / "tables/public.keep.sql", keep.content)
    _write(out / "tables/public.old.sql", old.content)
    _write(out / "tables/public.user.sql", "-- hand written\n")
    manifest = {
        "tables/public.keep.sql": FileRecord("table", sha256_text(keep.content), "t"),
        "tables/public.old.sql": FileRecord("table", sha256_text(old.content), "t"),
        "policies/public.p.sql": FileRecord("policy", "zzz", "p"),
    }
    rendered = {
        "tables/public.keep.sql": keep,
        "tables/public.new.sql": _obj("table", "public.new", "CREATE TABLE new ();"),
    }
    plan = plan_files(out, manifest, rendered, {"table"}, force=False, journal=None)
    assert plan.paths("create") == ["tables/public.new.sql"]
    assert plan.paths("unchanged") == ["tables/public.keep.sql"]
    assert plan.paths("delete") == ["tables/public.old.sql"]  # policy file untouched: other kind
    assert not plan.conflicts

    rendered["tables/public.user.sql"] = _obj("table", "public.user", "CREATE TABLE \"user\" ();")
    plan = plan_files(out, manifest, rendered, {"table"}, force=False, journal=None)
    assert any("not managed by DBSS" in c for c in plan.conflicts)
    assert plan_files(out, manifest, rendered, {"table"}, force=True, journal=None).conflicts == []

    _write(out / "tables/public.keep.sql", "-- edited by hand\n")
    plan = plan_files(out, manifest, {"tables/public.keep.sql": keep}, {"table"}, force=False, journal=None)
    assert any("edited outside DBSS" in c for c in plan.conflicts)


def test_commit_applies_plan_and_cleans_up(tmp_path: Path):
    out, state_dir = tmp_path / "sql", tmp_path / ".dbss"
    state_dir.mkdir()
    obj = _obj("table", "public.a", "CREATE TABLE a ();")
    _write(out / "tables/public.gone.sql", "x")
    manifest = {"tables/public.gone.sql": FileRecord("table", sha256_text("x"), "t")}
    plan = plan_files(out, manifest, {"tables/public.a.sql": obj}, {"table"}, force=False, journal=None)
    staging = commit_files(plan, out, state_dir)
    assert (state_dir / "journal.json").exists()
    finish_commit(state_dir, staging)
    assert (out / "tables/public.a.sql").read_text() == obj.content
    assert not (out / "tables/public.gone.sql").exists()
    assert not (state_dir / "journal.json").exists() and not (state_dir / "staging").exists()


def test_rendered_paths_cannot_escape_output(tmp_path: Path):
    from dbss.errors import DbssError

    bad = _obj("table", "x", "select 1;")
    with pytest.raises(DbssError):
        plan_files(tmp_path / "sql", {}, {"../evil.sql": bad}, {"table"}, force=False, journal=None)


# -- rendering helpers -------------------------------------------------------------------

def test_parenthesize():
    assert parenthesize("(a = b)") == "(a = b)"
    assert parenthesize("(a) = (b)") == "((a) = (b))"
    assert parenthesize("true") == "(true)"
    assert parenthesize("(x = ')')") == "(x = ')')"
    assert parenthesize("auth.uid() = id") == "(auth.uid() = id)"


def test_acl_statements():
    expected = {("PUBLIC", "EXECUTE"): False, ("postgres", "EXECUTE"): False, ("anon", "EXECUTE"): False}
    actual = {("postgres", "EXECUTE"): False, ("app", "EXECUTE"): True}
    stmts = acl_statements("FUNCTION f()", expected, actual, {"EXECUTE"})
    assert stmts == [
        "REVOKE EXECUTE ON FUNCTION f() FROM PUBLIC, anon;",
        "GRANT EXECUTE ON FUNCTION f() TO app WITH GRANT OPTION;",
    ]
    all_tab = {"SELECT", "INSERT", "UPDATE", "DELETE"}
    assert acl_statements("TABLE t", {}, {("anon", p): False for p in all_tab}, all_tab) == \
        ["GRANT ALL ON TABLE t TO anon;"]
    assert acl_statements("TABLE t", {("a", "SELECT"): True}, {("a", "SELECT"): False}, all_tab) == \
        ["REVOKE GRANT OPTION FOR SELECT ON TABLE t FROM a;"]


def test_sequence_options():
    base = {"type": "bigint", "increment": "1", "min": "1", "max": "9223372036854775807", "start": "1",
            "cache": "1", "cycle": False}
    assert sequence_options(base, column_type="bigint") == []
    assert sequence_options({**base, "start": "100"}, column_type="bigint") == ["START WITH 100"]
    assert sequence_options({**base, "type": "integer", "max": "2147483647"}, column_type="bigint") == ["AS integer"]
    assert sequence_options({**base, "increment": "-1", "min": "-9223372036854775808", "max": "-1", "start": "-1"},
                            column_type="bigint") == ["INCREMENT BY -1"]


def test_interrupted_commit_is_recoverable(tmp_path: Path):
    """Files written by a commit whose state was never saved are not treated as hand edits."""
    out, state_dir = tmp_path / "sql", tmp_path / ".dbss"
    state_dir.mkdir()
    v1 = _obj("table", "public.a", "CREATE TABLE a (x int);")
    v2 = _obj("table", "public.a", "CREATE TABLE a (x int, y int);")
    _write(out / "tables/public.a.sql", v1.content)
    manifest = {"tables/public.a.sql": FileRecord("table", sha256_text(v1.content), "t")}
    plan = plan_files(out, manifest, {"tables/public.a.sql": v2}, {"table"}, force=False, journal=None)
    commit_files(plan, out, state_dir)  # crash here: state.json still records v1
    from dbss.sync.files import load_journal

    journal = load_journal(state_dir)
    assert journal is not None
    v3 = _obj("table", "public.a", "CREATE TABLE a (x int, y int, z int);")
    assert plan_files(out, manifest, {"tables/public.a.sql": v3}, {"table"}, force=False, journal=None).conflicts
    plan = plan_files(out, manifest, {"tables/public.a.sql": v3}, {"table"}, force=False, journal=journal)
    assert not plan.conflicts and plan.paths("update") == ["tables/public.a.sql"]
