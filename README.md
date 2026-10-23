# DBSS — Database Schema Synchronizer

**See what your PostgreSQL database looks like *now*, not just how it got there.**

DBSS turns a folder of SQL migrations into a clean, current-state view of your
schema: one readable `.sql` file per table, policy, function, trigger, index,
constraint, type and grant. Every file always shows the latest definition.

It works with any PostgreSQL project, and supports **Supabase out of the box**.

> Migrations answer *“how did the database change?”*
> DBSS answers *“what does it look like now?”*

```text
supabase/migrations/
  20261020000001_init.sql      CREATE TABLE public.profiles (id uuid PRIMARY KEY, username text, bio text, age int, ...);
  20261020000003_changes.sql   ALTER TABLE public.profiles DROP COLUMN age;
                               ALTER TABLE public.profiles RENAME COLUMN bio TO about;

sql/tables/public.profiles.sql
  CREATE TABLE public.profiles (
      id uuid NOT NULL,
      username text NOT NULL,
      about text,
      created_at timestamp with time zone DEFAULT now()
  );
```

The generated file is a **complete definition**, not a changelog. It contains no
leftover `ALTER` statements.

DBSS is **local-only**. It never connects to your real database, never modifies a
live database, never edits your migrations and never runs git. The generated files
are yours to commit, or not.

---

## Contents

- [Why](#why)
- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Output layout and ownership](#output-layout-and-ownership)
- [Configuration](#configuration)
  - [Supabase (default)](#supabase-default)
  - [Plain PostgreSQL](#plain-postgresql)
- [Change detection and safety](#change-detection-and-safety)
- [Limitations](#limitations)
- [Development](#development)

## Why

Any PostgreSQL project that keeps its schema in SQL migrations builds up dozens of
them over time. To answer a simple question like *“which columns does `profiles`
have, and which RLS policies protect it?”* you either read every `CREATE`, `ALTER`
and `DROP` that ever touched it, or you dump the whole schema and search through
it.

DBSS keeps an always-current, per-object view of your schema next to your
migrations. That makes the schema easy to:

- **review**: pull-request diffs show the real change to a table or policy,
  not just the migration that made it;
- **navigate**: open `sql/policies/public.profiles.sql` and see every policy
  on that table;
- **hand to tools and teammates**: one small file per object, deterministic
  formatting.

Unlike ORM-based or schema-first tools, DBSS works with the SQL migrations you
already have. There's no new workflow to adopt and no second schema definition to
maintain. It rebuilds the current database structure in an isolated local
container and generates one organized SQL file per table, constraint, index,
function, policy, trigger, type and grant. The schema becomes easy to explore,
review and version-control, without connecting to a live database.

## Features

- **Eight object kinds**: tables, constraints, indexes, triggers, RLS policies,
  functions (one file per overload), types (enum, domain, composite, range) and
  grants.
- **Real PostgreSQL semantics.** Renames, `CASCADE` drops, quoted identifiers,
  `DO $$` blocks and multi-statement migrations are handled by Postgres itself.
  DBSS never pattern-matches SQL text.
- **Any PostgreSQL project.** Point DBSS at your migrations folder and the
  Postgres image that matches your server version.
- **Supabase-aware.** The default configuration understands objects that live on
  platform tables, such as triggers on `auth.users` and policies on
  `storage.objects`. Supabase's default grants are not repeated in every file.
- **Incremental and partial.** `dbss update` processes only what is pending.
  `dbss apply <kind>` synchronizes a single kind, and DBSS tracks which kinds
  still need each migration.
- **Safe by design.** Files are staged and committed atomically, hand edits are
  never overwritten silently, edited history forces a full rebuild, and DBSS only
  deletes files it created.
- **Deterministic output.** The same migrations always produce byte-identical
  files.
- **CI-friendly.** `dbss status --check` exits non-zero when the generated files
  are out of date.

## How it works

1. DBSS starts a **disposable PostgreSQL Docker container** (`supabase/postgres`
   by default, or any `postgres` image) with `--network none`, so migration code
   cannot reach anything outside it.
2. It runs the optional bootstrap SQL (for Supabase, the parts of `auth` and
   `storage` that migrations usually reference) and takes a **baseline
   snapshot**.
3. It replays your migrations in filename order, each in its own transaction with
   `ON_ERROR_STOP`.
4. It reads the resulting PostgreSQL catalogs (`pg_get_*def`, `pg_policy`, ACLs,
   …) and renders deterministic SQL. Only objects that your migrations created or
   changed are kept.
5. It stages the files, checks them for conflicts, writes them, and only then
   records progress in `.dbss/state.json`.
6. It removes the container, including after an error or Ctrl-C.

The full design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Requirements

- **Python 3.10+**
- **Docker** (Docker Desktop or Docker Engine), running.
- The first run pulls the configured image. The default,
  `supabase/postgres:17.6.1.178`, is about **1.7 GB**. Plain PostgreSQL projects
  can use the much smaller official `postgres` image (see
  [Plain PostgreSQL](#plain-postgresql)).

## Install

From a clone of this repository:

```bash
pipx install .
```

or, inside a virtual environment:

```bash
pip install .
```

Check the installation:

```bash
dbss --version
```

## Quick start

Run these from the root of your project. The defaults expect a Supabase layout
(`supabase/migrations/`). For any other project, edit `dbss.config.json` after
`dbss init` as shown in [Plain PostgreSQL](#plain-postgresql).

```bash
dbss init
```

```bash
dbss update
```

```text
> Starting scratch database (supabase/postgres:17.6.1.178)...
> Bootstrapping platform schema (builtin:supabase)...
> Replaying 3 migration(s) in the scratch database...
> Introspecting catalogs...
Synchronized table, constraint, index, trigger, policy, function, type, grant through 20261020000003_changes.sql
  created  constraints/public.posts.posts_pkey.sql
  created  functions/public.handle_new_user().sql
  created  functions/public.post_count(uuid).sql
  created  functions/public.post_count(uuid, public.post_status).sql
  created  indexes/public.posts.posts_author_idx.sql
  created  policies/public.profiles.sql
  created  tables/public.posts.sql
  created  tables/public.profiles.sql
  created  triggers/auth.users.on_auth_user_created.sql
  created  types/public.post_status.sql
  ...
Not represented by DBSS (no matching object kind; informational):
  - view public.published_posts
```

After you add a new migration, run `dbss update` again. Only affected files
change, and if nothing is pending it returns immediately without starting Docker.
A sync takes a few seconds, most of it spent starting the container.

An example policy file, `sql/policies/public.profiles.sql`:

```sql
-- Generated by DBSS: row level security for public.profiles.
-- Do not edit by hand; regenerate with `dbss update`.

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Profiles are public" ON public.profiles
    FOR SELECT
    TO public
    USING (true);

CREATE POLICY "Users update own profile" ON public.profiles
    FOR UPDATE
    TO authenticated
    USING (( SELECT auth.uid() AS uid) = id)
    WITH CHECK (( SELECT auth.uid() AS uid) = id);
```

## Commands

| Command | What it does |
|---|---|
| `dbss init` | Creates `dbss.config.json`, the `sql/<kind>/` folders and `.dbss/`. Idempotent; it never overwrites existing files. |
| `dbss update [--dry-run] [--force] [--full]` | Processes all pending migrations and synchronizes every kind. `--full` replays even when nothing is pending. |
| `dbss apply <kind> <migration> [--dry-run] [--force]` | Brings one kind up to and including `<migration>`. The other kinds stay pending. |
| `dbss status [--json] [--check]` | Shows processed, pending and changed migrations, per-kind progress and the last error. |

- `<kind>` is one of `table`, `constraint`, `index`, `trigger`, `policy`,
  `function`, `type` or `grant`. Plural forms are accepted.
- `--force` overwrites generated files that were edited by hand, and unmanaged
  files that are in the way.
- `--dry-run` shows what would change without writing anything.
- `status --check` exits with code 1 unless everything is synchronized.

**Global options** go *before* the command, for example `dbss -q update`:

| Option | Meaning |
|---|---|
| `-C, --project-dir <path>` | Project root (default: the nearest directory containing `dbss.config.json`) |
| `-q, --quiet` | Suppress progress messages |
| `--version` | Print the version |

> **“Apply” means updating DBSS's local files.** It does **not** run the migration
> against any database you use.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Other error, or `status --check` found pending work |
| 2 | Configuration or usage error |
| 3 | Missing prerequisite (Docker not installed or not running, image unavailable) |
| 4 | Conflict: a generated file was edited by hand, or an unmanaged file is in the way (use `--force`) |
| 5 | A migration failed during replay (the psql error and line number are shown) |
| 6 | An object cannot be reconstructed safely (or, in strict mode, is not represented) |
| 7 | Migration history changed; `apply` refuses, so run `dbss update` |
| 8 | `.dbss/state.json` is unreadable |

On any failure, the generated files and the synchronization progress are left
unchanged.

### Using DBSS in CI

```bash
dbss update && git diff --exit-code sql/
```

This fails the build if someone added a migration without committing the
regenerated files. Alternatively, run `dbss status --check` against a committed
`.dbss/state.json`.

## Output layout and ownership

```text
sql/
  tables/       public.profiles.sql                    columns, defaults, identity/generated, collation,
                                                       owned sequences, storage options, replica identity, comments
  constraints/  public.profiles.profiles_pkey.sql      PK / UNIQUE / FK / CHECK / EXCLUDE (ALTER TABLE … ADD CONSTRAINT)
  indexes/      public.posts.posts_author_idx.sql      indexes that do not back a constraint
  triggers/     auth.users.on_auth_user_created.sql
  policies/     public.profiles.sql                    ENABLE/FORCE ROW LEVEL SECURITY + every policy on that table
  functions/    public.add(integer, integer).sql       one file per overload
  types/        public.mood.sql                        enum, domain (with its CHECKs), composite, range
  grants/       table.public.posts.sql                 GRANT/REVOKE per object (including column grants),
                function.public.add(integer, integer).sql, schema.app.sql, sequence.*, type.*,
                default_privileges.<role>.schema.<schema>.sql, role.<name>.sql
```

Each object has **exactly one owner file**, so combining all the files never
duplicates a statement:

- `NOT NULL` is written inline in the table. Every other constraint lives in
  `constraints/`.
- An index that backs a PK, UNIQUE or EXCLUDE constraint belongs to that
  constraint, not to `indexes/`.
- RLS settings live with the policies. Privileges live in `grants/`.
- A table's `serial`/`OWNED BY` sequences are part of the table file.

**Grants** list only what differs from the privileges a new object would get on
the bare platform. On Supabase, this means the default grants to `anon`,
`authenticated` and `service_role` in `public` are not repeated in every file.
Objects with only default privileges get no grants file.

**File names** are `<schema>.<object>.sql`. Characters other than letters,
digits, space, `_` and `-` are percent-encoded, so `"a.b"."c"` and `"a"."b.c"`
cannot collide. Names that differ only by case get a short hash suffix, because
Windows and macOS file systems are case-insensitive.

DBSS only modifies or deletes files recorded in its manifest
(`.dbss/state.json`). Your own files in `sql/` are left alone.

## Configuration

Everything is configured in `dbss.config.json`, which `dbss init` creates.

| Key | Meaning |
|---|---|
| `migrations_dir` | Where your migrations live. |
| `output_dir` | Where generated files are written. |
| `schemas` | `"auto"` manages every object your migrations created or changed, in any schema, but nothing the image or bootstrap created. A list such as `["public", "app"]` manages every object in those schemas. |
| `exclude_schemas` | Schemas to ignore. |
| `strict` | Fail (instead of only reporting) when migrations create objects DBSS has no kind for, such as views, extensions or publications. |
| `docker.image` | The Postgres image migrations are replayed in. Use the major version your project runs. |
| `docker.startup_timeout_seconds` | How long to wait for the container to accept connections. |
| `docker.migration_role` | The role migrations run as. |
| `bootstrap` | SQL run as superuser *before* the baseline snapshot: `builtin:supabase` and/or SQL file paths relative to the project root. Objects created here are treated as platform objects and are not written out. |

Changing a setting that affects the output triggers a full rebuild on the next
`dbss update`.

### Supabase (default)

`dbss init` writes this configuration, which suits a standard Supabase CLI
project:

```json
{
  "config_version": 1,
  "migrations_dir": "supabase/migrations",
  "output_dir": "sql",
  "schemas": "auto",
  "exclude_schemas": [],
  "mode": "docker",
  "strict": false,
  "docker": {
    "image": "supabase/postgres:17.6.1.178",
    "startup_timeout_seconds": 120,
    "migration_role": "postgres"
  },
  "bootstrap": ["builtin:supabase"]
}
```

With `schemas: "auto"`, objects your migrations add to platform tables, such as
policies on `storage.objects` and triggers on `auth.users`, are included.

**About `builtin:supabase`:** the bare `supabase/postgres` image lacks most of
what the Auth and Storage services create at runtime. `builtin:supabase` adds the
commonly referenced parts: `auth.users` columns, `auth.identities`,
`auth.sessions`, `auth.jwt()`, `storage.buckets`, `storage.objects` and the
`storage.foldername`, `filename` and `extension` helpers. For full fidelity, add a
schema-only dump of your local `auth`/`storage` schemas as an extra bootstrap
file.

### Plain PostgreSQL

For a project that is not on Supabase, run `dbss init`, then point the config at
your migrations folder and the official `postgres` image, and remove the
Supabase bootstrap:

```json
{
  "config_version": 1,
  "migrations_dir": "db/migrations",
  "output_dir": "sql",
  "schemas": "auto",
  "exclude_schemas": [],
  "mode": "docker",
  "strict": false,
  "docker": {
    "image": "postgres:17",
    "startup_timeout_seconds": 120,
    "migration_role": "postgres"
  },
  "bootstrap": []
}
```

- **Image:** use the tag that matches your server's major version, such as
  `postgres:16`. If your migrations need extensions that aren't in the official
  image (PostGIS, pgvector, …), use an image that includes them, such as
  `postgis/postgis` or `pgvector/pgvector`.
- **Pre-existing objects:** if your migrations assume roles, schemas or
  extensions that are created outside them, put that SQL in a file and list it
  in `bootstrap`, for example `"bootstrap": ["db/bootstrap.sql"]`.
- **`migration_role`:** set it to the role your migrations normally run as, and
  create that role in a bootstrap file if the image doesn't have it.
- **Existing `sql/` folder:** DBSS leaves files it didn't create alone, but if
  `output_dir` already holds hand-written SQL, a separate folder (for example
  `"output_dir": "schema"`) keeps the two apart.

#### Migration file names

DBSS reads `.sql` files directly inside `migrations_dir` whose names start with a
number and an underscore, such as `20261020093000_create_users.sql` or
`0001_init.sql`. It replays them in **filename order**, and each number must be
unique.

- Timestamp prefixes (Supabase CLI, dbmate) work as they are.
- With counters, pad them to the same width (`0001_`, `0002_`, …, `0010_`) so
  filename order matches the intended order.
- Files that don't match the pattern, such as Flyway's `V1__init.sql`, are skipped
  with a warning. Separate up/down files (`0001_init.up.sql` and
  `0001_init.down.sql`) aren't supported; DBSS stops with a duplicate-version
  error.

## Change detection and safety

`.dbss/state.json` records the SHA-256 of every migration (with line endings
normalized) and which kinds have incorporated it. `dbss update` performs a **full
rebuild** (a replay from scratch) when:

- an already-processed migration was edited or deleted;
- a new file sorts *before* migrations that were already processed;
- the DBSS version changed;
- the configuration changed.

`dbss apply` refuses to run in those cases and asks you to run `dbss update`.

Safety guarantees:

- Migrations are only read, never modified.
- Migration code runs only inside a network-isolated, throwaway container. No
  production credentials are involved.
- Generated files are staged and committed atomically. Progress is recorded only
  after the files are written successfully.
- A managed file that you edited is never overwritten without `--force`.
- No git commands, no telemetry, no network access other than pulling the Docker
  image.

## Limitations

DBSS is at **v0.1**. Known gaps:

- **Not generated yet:** views, materialized views, standalone sequences,
  extensions, schemas, event triggers, rules, publications, statistics objects
  and collations. They are listed as “not represented” after every sync, so
  nothing is silently missing.
- **Reported as errors (exit 6)** instead of being rendered: aggregates,
  base/shell types, and privileges on foreign servers, FDWs, parameters, large
  objects and tablespaces.
- Role passwords are not reproduced.
- A partition's child indexes are recreated by the parent index under
  auto-generated names.
- Only Docker replay is supported. Introspecting an existing local database is
  planned.

Issues and pull requests are welcome.

## Development

```bash
python -m venv .venv
```

Install in editable mode with the dev dependencies (on macOS/Linux use
`.venv/bin/pip`):

```bash
.venv/Scripts/pip install -e ".[dev]"
```

Run the fast unit tests:

```bash
pytest -m "not docker"
```

Run everything. This needs Docker and the pulled image, and takes about 3
minutes:

```bash
pytest
```

The integration tests run every acceptance criterion against real containers.
They include a **round trip**: the generated files for
`tests/fixtures/full_project` are executed in a fresh database, and DBSS must
regenerate byte-identical files.

The module layout and synchronization algorithm are described in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
