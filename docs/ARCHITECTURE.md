# DBSS architecture

## Stack choice

**Python 3.10+ with Typer.**

- Typer gives us help text, argument validation and exit codes with little code.
- Python is easy to distribute as a CLI (`pipx install`).
- No database driver is needed: every database interaction goes through `psql`
  *inside* the scratch container (`docker exec`). The container can therefore run
  with `--network none` and publish no port, which isolates migration code
  completely.
- psycopg will be added when the "existing local database" mode is built.

## Modules

```text
dbss/
  cli/app.py              Typer commands; error -> message/hint/exit code; records last_error
  config/                 dbss.config.json schema, validation, output-affecting fingerprint
  migrations/             discovery (<version>_<name>.sql), filename ordering, CRLF-normalized SHA-256
  database/docker.py      ScratchDatabase: pull/start/wait/bootstrap/run migration/query JSON/remove
  database/bootstrap/     builtin bootstrap SQL (builtin:supabase)
  introspection/          one catalog query -> JSON -> Catalog (search_path = pg_catalog)
  renderers/              Catalog -> RenderedObject per kind; scope selection vs. baseline
  sync/analysis.py        state vs. disk: per-kind watermarks, pending, rebuild reasons
  sync/files.py           plan (create/update/delete/conflict), staged + journaled commit
  sync/engine.py          update/apply orchestration, state persistence
  sync/status.py          data for `dbss status`
  state/                  .dbss/state.json model, atomic save
  utils/                  hashing, atomic writes, path containment, collision-safe file names
```

## Synchronization algorithm

1. Load and validate the config. Load the state (`.dbss/state.json`) and the
   journal, if any.
2. Discover migrations, sort them by file name (as the Supabase CLI does) and
   hash them.
3. Analyze:
   - For each kind, compute the **watermark**: how many leading migrations are
     recorded as incorporated with an unchanged hash.
   - Collect **rebuild reasons**: modified or deleted processed migrations, a new
     file inserted before processed ones, a DBSS version change, or a config
     fingerprint change.
4. Replay inside the scratch container:
   1. start `supabase/postgres` with `--network none`;
   2. run the bootstrap scripts as superuser;
   3. take the **baseline snapshot**;
   4. run migrations `1..N` as `migration_role`, each with
      `psql --single-transaction -v ON_ERROR_STOP=1`;
   5. take the **final snapshot**;
   6. remove the container (also on error or Ctrl-C).
5. Render the requested kinds from both snapshots. Keep an object if it is
   **in scope**:
   - `schemas: "auto"`: its identity is absent from the baseline, or its rendered
     text differs from the baseline rendering;
   - explicit schema list: its schema is listed.

   Objects that cannot be reconstructed carry an `error`. Any in-scope error
   aborts the run (exit 6) before any file is touched.
6. Plan file changes against the manifest:
   - create, update, unchanged or delete;
   - conflict when a managed file was hand-edited, or an unmanaged file sits at a
     target path.

   Conflicts abort (exit 4) unless `--force`.
7. Commit:
   1. write new contents to `.dbss/staging/<id>/` and verify their hashes;
   2. write `.dbss/journal.json`;
   3. `os.replace` each file into place and delete removed ones;
   4. save the state atomically;
   5. delete the journal and staging.

   If the process dies mid-commit, the journal lets the next run recognize the
   half-written files as DBSS's own rather than as hand edits.
8. Print a summary: created, updated, removed, unchanged, not represented, still
   pending.

`dbss update` with nothing pending, an intact manifest and no journal returns
immediately without starting Docker.

## Partial synchronization (`dbss apply <kind> <migration>`)

State tracks completion **per migration and per kind**. `apply K M`:

- refuses if a rebuild is required;
- is a no-op if `M` is already below K's watermark;
- otherwise replays migrations `1..M` and renders only kind `K`. Earlier pending
  migrations for `K` are incorporated too, because the snapshot includes them;
  the summary says so.
- marks `K` complete for every migration up to `M`.

Kinds whose watermark is lower stay pending, and `dbss update` processes them. A
full replay is the source of truth, so changes made to kind `K` by statements of
other categories (e.g. `DROP TABLE … CASCADE` removing policies) are never missed.

Dependencies between kinds: when an apply removes a table file while another
kind's files still describe that table (e.g. its policies, because `policy` is
behind), DBSS warns. Those files are removed once that kind catches up.

## Definition ownership

| Concern | Owner |
|---|---|
| Columns, `NOT NULL`, defaults, identity, generated columns, collation | table file |
| `serial` and other `OWNED BY` sequences | table file (`serial` when all parameters are default; otherwise `CREATE SEQUENCE` + `OWNED BY`) |
| Identity sequence options (non-default only) | inline in the table's `GENERATED … AS IDENTITY (…)` |
| Table storage, statistics, compression, `REPLICA IDENTITY FULL/NOTHING`, owner (when not `migration_role`), comments | table file |
| PK / UNIQUE / FK / CHECK / EXCLUDE, including their backing index, `CLUSTER ON` and `REPLICA IDENTITY USING INDEX` | constraint file |
| Constraints inherited from a parent or cloned onto partitions | the parent's constraint file only |
| Other indexes | index file. On partitioned tables the recursive `CREATE INDEX … ON parent` is emitted; partition indexes come from it. |
| Triggers, including constraint triggers and enable/disable state (internal FK triggers are skipped) | trigger file |
| `ENABLE`/`FORCE ROW LEVEL SECURITY` and all policies of a table | one policy file per table |
| Function/procedure definition (`pg_get_functiondef`), owner, comment | function file, one per overload |
| Enum, domain (incl. CHECK constraints), composite and range types | type file; implicit array, multirange and range-constructor objects are omitted |
| Object and column privileges, default privileges, roles and memberships | grants files |

### Grants contract

A grants file contains the REVOKE/GRANT statements that turn the ACL a new object
would get *on the bare platform* into its current ACL. The starting ACL is
PostgreSQL's `acldefault` plus the **baseline** `pg_default_acl` entries.
Grantees with identical changes share one statement, and `ALL` is used when every
privilege of the object type is granted.

Default privileges added by migrations are written to
`default_privileges.<role>.(global|schema.<schema>).sql`. They only influence
objects created after them, so when recreating a schema from these files, apply
them last.

Roles:

- A new role gets `CREATE ROLE`, its settings and the memberships that involve
  it.
- A platform role only gets a file when its own attributes, settings or
  memberships among platform roles change.
- The implicit `ADMIN` grant that PostgreSQL 16+ gives a role's creator is not
  emitted.
- Privileges DBSS cannot express (FDWs, foreign servers, parameters, large
  objects, tablespaces) raise an error when they differ from the baseline.

## State file

```json
{
  "state_format": 1,
  "dbss_version": "0.1.0",
  "config_fingerprint": "<sha256 of output-affecting config + bootstrap file hashes>",
  "migrations": [
    {"name": "202610220001_create_profiles.sql", "sha256": "…", "completed": ["table", "constraint", "…"]}
  ],
  "files": {
    "tables/public.profiles.sql": {"kind": "table", "sha256": "…", "object": "table public.profiles",
                                   "table": "public.profiles"}
  },
  "last_sync": {"at": "…", "command": "update", "kinds": ["…"], "through": "…"},
  "last_error": null
}
```

Only a successful commit updates `migrations`, `files` and `last_sync`. A failure
only sets `last_error`.

## Determinism

- Introspection runs with `search_path = pg_catalog`, `DateStyle = ISO, YMD` and
  `IntervalStyle = postgres`, so every name is schema-qualified and literals print
  stably.
- Migration and bootstrap text has its line endings normalized before replay, and
  generated files always use LF. Output is therefore identical on Windows and
  Unix.
- Objects, policies, grantees, columns and enum labels have a fixed order.
- File headers name the object, not the migration, so unrelated migrations never
  rewrite a file.

## Testing

- `tests/test_units.py`: naming, config validation, discovery and hashing,
  watermark/rebuild analysis, file plans, conflicts, journaled recovery, ACL
  diffing and sequence options.
- `tests/test_acceptance.py`: the product acceptance criteria, run against
  real containers.
- `tests/test_objects.py`: a broad fixture covering every kind, quoted names,
  overloads, partitions and inheritance, followed by a **round trip**. The
  generated files are executed as one migration in a fresh project and must
  regenerate identically.
