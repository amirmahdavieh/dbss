"""Migration discovery, ordering and hashing (Supabase ``<version>_<name>.sql`` convention)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from dbss.errors import ConfigError
from dbss.utils.fs import normalize_newlines, sha256_file

MIGRATION_RE = re.compile(r"^(?P<version>[0-9]+)_(?P<name>.*)\.sql$")


@dataclass(frozen=True)
class Migration:
    name: str  # file name, e.g. 20261022000100_create_profiles.sql
    version: str
    path: Path
    sha256: str

    def read_sql(self) -> str:
        """Migration text with normalized newlines (CRLF/LF checkouts replay identically)."""
        return normalize_newlines(self.path.read_text(encoding="utf-8-sig"))


def discover_migrations(directory: Path) -> tuple[list[Migration], list[str]]:
    """Return migrations sorted by file name (as the Supabase CLI does) and warnings."""
    warnings: list[str] = []
    if not directory.is_dir():
        return [], [f"Migrations directory {directory} does not exist."]
    migrations: list[Migration] = []
    seen_versions: dict[str, str] = {}
    for path in sorted(directory.iterdir(), key=lambda p: p.name):
        if not path.is_file() or not path.name.endswith(".sql"):
            continue
        match = MIGRATION_RE.match(path.name)
        if not match:
            warnings.append(f"Skipping {path.name}: name does not match <version>_<name>.sql")
            continue
        version = match.group("version")
        if version in seen_versions:
            raise ConfigError(
                f"Duplicate migration version {version}: {seen_versions[version]} and {path.name}.",
                hint="Each migration needs a unique timestamp prefix.",
            )
        seen_versions[version] = path.name
        migrations.append(Migration(name=path.name, version=version, path=path, sha256=sha256_file(path)))
    return migrations, warnings


def find_migration(migrations: list[Migration], migrations_dir: Path, arg: str) -> int:
    """Index of the migration designated by a CLI path or bare file name."""
    candidate = Path(arg)
    resolved = candidate.resolve() if candidate.exists() else None
    for i, m in enumerate(migrations):
        if resolved is not None and m.path.resolve() == resolved:
            return i
        if resolved is None and candidate.name == m.name:
            return i
    if resolved is not None and resolved.parent != migrations_dir.resolve():
        raise ConfigError(
            f"{arg} is not inside the configured migrations directory ({migrations_dir}).",
            hint="DBSS only processes migrations from `migrations_dir` so that history stays ordered.",
        )
    raise ConfigError(f"Migration '{arg}' was not found in {migrations_dir}.")
