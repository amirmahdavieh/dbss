"""Product acceptance tests, run against a real scratch supabase/postgres container."""

from __future__ import annotations

import subprocess

import pytest

from tests.conftest import Project

pytestmark = pytest.mark.docker

PROFILES = """
CREATE TABLE public.profiles (
  id INT PRIMARY KEY,
  name TEXT,
  age INT
);
"""


def table_body(project: Project, rel: str) -> str:
    text = project.read(rel)
    return text[text.index("CREATE TABLE"):]


def test_init_twice_keeps_existing_files(tmp_path):
    p = Project(tmp_path / "p")
    p.root.mkdir()
    p.dbss("init")
    custom = p.sql / "tables" / "notes.txt"
    custom.write_text("mine")
    config = (p.root / "dbss.config.json").read_text()
    out = p.dbss("init").output
    assert "kept" in out and custom.read_text() == "mine"
    assert (p.root / "dbss.config.json").read_text() == config
    for folder in ("tables", "grants", "policies", "functions", "constraints", "indexes", "triggers", "types"):
        assert (p.sql / folder).is_dir()


def test_column_lifecycle(project: Project):
    project.migration("202610220001_create_profiles.sql", PROFILES)
    project.dbss("update")
    body = table_body(project, "tables/public.profiles.sql")
    assert body == "CREATE TABLE public.profiles (\n    id integer NOT NULL,\n    name text,\n    age integer\n);\n"
    pk = project.read("constraints/public.profiles.profiles_pkey.sql")
    assert "ADD CONSTRAINT profiles_pkey PRIMARY KEY (id);" in pk
    assert "PRIMARY KEY" not in body  # constraint ownership: never duplicated in the table file

    project.migration("202610220002_drop_age.sql", "ALTER TABLE public.profiles DROP COLUMN age;")
    project.dbss("update")
    body = table_body(project, "tables/public.profiles.sql")
    assert "age" not in body and "ALTER" not in project.read("tables/public.profiles.sql")

    project.migration("202610220003_more.sql", """
        ALTER TABLE public.profiles ADD COLUMN email text DEFAULT 'x@y.z' NOT NULL;
        ALTER TABLE public.profiles RENAME COLUMN name TO display_name;
        ALTER TABLE public.profiles ADD COLUMN tmp int;
        ALTER TABLE public.profiles DROP COLUMN tmp;
    """)
    project.dbss("update")
    assert table_body(project, "tables/public.profiles.sql") == (
        "CREATE TABLE public.profiles (\n"
        "    id integer NOT NULL,\n"
        "    display_name text,\n"
        "    email text DEFAULT 'x@y.z'::text NOT NULL\n"
        ");\n"
    )


def test_drop_table_removes_file_and_dependents(project: Project):
    project.migration("202610220001_create.sql", PROFILES + """
        CREATE INDEX profiles_name_idx ON public.profiles (name);
        ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
        CREATE POLICY "read" ON public.profiles FOR SELECT USING (true);
        CREATE FUNCTION public.touch() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;
        CREATE TRIGGER profiles_touch BEFORE UPDATE ON public.profiles FOR EACH ROW EXECUTE FUNCTION public.touch();
        REVOKE ALL ON public.profiles FROM anon;
        CREATE TABLE public.notes (id int PRIMARY KEY);
    """)
    project.dbss("update")
    assert {"tables/public.profiles.sql", "indexes/public.profiles.profiles_name_idx.sql",
            "policies/public.profiles.sql", "triggers/public.profiles.profiles_touch.sql",
            "grants/table.public.profiles.sql", "constraints/public.profiles.profiles_pkey.sql"} <= project.files()

    user_file = project.sql / "tables" / "README.md"
    user_file.write_text("my notes")
    project.migration("202610220002_drop.sql", "DROP TABLE public.profiles;")
    project.dbss("update")
    remaining = project.files()
    assert not any("public.profiles" in f for f in remaining), remaining
    assert "tables/public.notes.sql" in remaining and "functions/public.touch().sql" in remaining
    assert user_file.read_text() == "my notes"  # unmanaged files are preserved


def test_update_is_idempotent(project: Project):
    project.migration("202610220001_create.sql", PROFILES)
    project.dbss("update")
    before = project.snapshot()
    state_before = project.state()
    out = project.dbss("update").output
    assert "Already up to date" in out
    assert project.snapshot() == before and project.state() == state_before
    out = project.dbss("update", "--full").output
    assert "no file changes" in out and project.snapshot() == before


def test_partial_apply_keeps_other_kinds_pending(project: Project):
    project.migration("202610220001_create.sql", PROFILES)
    project.dbss("update")
    m2 = project.migration("202610220002_multi.sql", """
        ALTER TABLE public.profiles ADD COLUMN owner uuid;
        ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
        CREATE POLICY "own" ON public.profiles FOR ALL TO authenticated USING (owner = auth.uid());
        CREATE INDEX profiles_owner_idx ON public.profiles (owner);
    """)
    project.dbss("apply", "table", str(m2))
    assert "owner uuid" in project.read("tables/public.profiles.sql")
    assert not (project.sql / "policies/public.profiles.sql").exists()
    record = next(r for r in project.state()["migrations"] if r["name"] == m2.name)
    assert record["completed"] == ["table"]

    status = project.dbss("status", "--json").output
    assert '"rebuild_required": false' in status
    assert project.dbss("status", "--check", expect=1).exit_code == 1

    # applying the same kind again is a no-op
    assert "already incorporated" in project.dbss("apply", "tables", m2.name).output

    project.dbss("update")
    assert "TO authenticated" in project.read("policies/public.profiles.sql")
    assert (project.sql / "indexes/public.profiles.profiles_owner_idx.sql").exists()
    project.dbss("status", "--check")


def test_apply_incorporates_earlier_pending_migrations(project: Project):
    project.migration("202610220001_a.sql", "CREATE TABLE public.a (id int);")
    m2 = project.migration("202610220002_b.sql", "CREATE TABLE public.b (id int);")
    out = project.dbss("apply", "table", m2.name).output
    assert "Also incorporates 1 earlier pending migration" in out
    assert {"tables/public.a.sql", "tables/public.b.sql"} <= project.files()


def test_modified_history_forces_rebuild(project: Project):
    m1 = project.migration("202610220001_create.sql", PROFILES)
    m2 = project.migration("202610220002_index.sql", "CREATE INDEX profiles_name_idx ON public.profiles (name);")
    project.dbss("update")
    m1.write_text(PROFILES + "\nALTER TABLE public.profiles ADD COLUMN extra text;\n", encoding="utf-8")

    result = project.dbss("apply", "table", m2.name, expect=7)
    assert "modified after DBSS processed it" in result.output
    assert "rebuild" in project.dbss("status").output.lower()

    out = project.dbss("update").output
    assert "Full rebuild" in out
    assert "extra text" in project.read("tables/public.profiles.sql")
    project.dbss("status", "--check")


def test_failure_never_claims_success(project: Project):
    project.migration("202610220001_create.sql", PROFILES)
    project.dbss("update")
    state_before = project.state()
    files_before = project.snapshot()
    project.migration("202610220002_ok.sql", "ALTER TABLE public.profiles ADD COLUMN ok int;")
    project.migration("202610220003_broken.sql", "ALTER TABLE public.missing ADD COLUMN x int;")
    result = project.dbss("update", expect=5)
    assert "202610220003_broken.sql" in result.output and "does not exist" in result.output
    after = project.state()
    assert after["migrations"] == state_before["migrations"] and after["files"] == state_before["files"]
    assert after["last_error"]["command"] == "update"
    assert project.snapshot() == files_before
    assert "Last error" in project.dbss("status").output


def test_hand_edited_files_are_not_overwritten(project: Project):
    project.migration("202610220001_create.sql", PROFILES)
    project.dbss("update")
    target = project.sql / "tables/public.profiles.sql"
    target.write_text("-- my edit\n")
    project.migration("202610220002_alter.sql", "ALTER TABLE public.profiles ADD COLUMN x int;")
    result = project.dbss("update", expect=4)
    assert "edited outside DBSS" in result.output
    assert target.read_text() == "-- my edit\n"
    project.dbss("update", "--force")
    assert "x integer" in target.read_text()


def test_unsupported_objects_fail_clearly(project: Project):
    project.migration("202610220001_agg.sql", """
        CREATE FUNCTION public.add2(int, int) RETURNS int LANGUAGE sql AS 'select $1 + $2';
        CREATE AGGREGATE public.mysum(int) (SFUNC = public.add2, STYPE = int);
    """)
    result = project.dbss("update", expect=6)
    assert "aggregate public.mysum(integer)" in result.output
    assert project.state()["migrations"] == []


def test_dry_run_changes_nothing(project: Project):
    project.migration("202610220001_create.sql", PROFILES)
    out = project.dbss("update", "--dry-run").output
    assert "created  tables/public.profiles.sql" in out
    assert not any(project.files()) and project.state()["migrations"] == []


def test_never_runs_git_or_networked_containers(project: Project, monkeypatch):
    calls: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(args, *a, **kw):  # noqa: ANN001
        calls.append([str(x) for x in args])
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", recording_run)
    project.migration("202610220001_create.sql", PROFILES)
    project.dbss("update")
    assert calls
    assert all(c[0].lower().replace("\\", "/").split("/")[-1].startswith("docker") for c in calls), calls
    run_cmd = next(c for c in calls if c[1] == "run")
    assert run_cmd[run_cmd.index("--network") + 1] == "none"
    assert not any("git" == c[0] for c in calls)


def test_case_colliding_names_get_distinct_files(project: Project):
    project.migration("202610220001_case.sql", 'CREATE TABLE public."Users" (id int); CREATE TABLE public.users (id int);')
    project.dbss("update")
    tables = sorted(f for f in project.files() if f.startswith("tables/"))
    assert len(tables) == 2 and tables[0].lower() != tables[1].lower()
    assert any('public."Users"' in project.read(t) for t in tables)


def test_partial_apply_warns_about_dependents_of_dropped_tables(project: Project):
    project.migration("202610220001_create.sql", PROFILES + """
        ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
        CREATE POLICY "read" ON public.profiles FOR SELECT USING (true);
    """)
    project.dbss("update")
    m2 = project.migration("202610220002_drop.sql", "DROP TABLE public.profiles;")
    result = project.dbss("apply", "table", m2.name)
    assert "policies/public.profiles.sql still describes dropped table public.profiles" in result.output
    assert (project.sql / "policies/public.profiles.sql").exists()
    project.dbss("update")
    assert not (project.sql / "policies/public.profiles.sql").exists()


def test_strict_mode_fails_on_unrepresented_objects(project: Project):
    config = project.root / "dbss.config.json"
    config.write_text(config.read_text().replace('"strict": false', '"strict": true'))
    project.migration("202610220001_view.sql", PROFILES + "CREATE VIEW public.v AS SELECT id FROM public.profiles;")
    result = project.dbss("update", expect=6)
    assert "view public.v" in result.output
