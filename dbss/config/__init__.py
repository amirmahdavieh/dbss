"""Project configuration (``dbss.config.json``) loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbss.errors import ConfigError
from dbss.utils.fs import is_within, sha256_file, sha256_text

CONFIG_FILENAME = "dbss.config.json"
STATE_DIRNAME = ".dbss"
DEFAULT_IMAGE = "supabase/postgres:17.6.1.178"
BUILTIN_PREFIX = "builtin:"
BUILTIN_BOOTSTRAPS = ("supabase",)

DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": 1,
    "migrations_dir": "supabase/migrations",
    "output_dir": "sql",
    "schemas": "auto",
    "exclude_schemas": [],
    "mode": "docker",
    "strict": False,
    "docker": {
        "image": DEFAULT_IMAGE,
        "startup_timeout_seconds": 120,
        "migration_role": "postgres",
    },
    "bootstrap": ["builtin:supabase"],
}

_KNOWN_KEYS = set(DEFAULT_CONFIG)
_KNOWN_DOCKER_KEYS = set(DEFAULT_CONFIG["docker"])


@dataclass(frozen=True)
class DockerSettings:
    image: str = DEFAULT_IMAGE
    startup_timeout_seconds: int = 120
    migration_role: str = "postgres"


@dataclass(frozen=True)
class Config:
    root: Path
    migrations_dir: str = "supabase/migrations"
    output_dir: str = "sql"
    # None means "auto": manage every object the migrations created or changed.
    schemas: tuple[str, ...] | None = None
    exclude_schemas: tuple[str, ...] = ()
    mode: str = "docker"
    strict: bool = False
    docker: DockerSettings = field(default_factory=DockerSettings)
    bootstrap: tuple[str, ...] = ("builtin:supabase",)

    @property
    def migrations_path(self) -> Path:
        return (self.root / self.migrations_dir).resolve()

    @property
    def output_path(self) -> Path:
        return (self.root / self.output_dir).resolve()

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIRNAME

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    def bootstrap_paths(self) -> list[tuple[str, Path | None]]:
        """(label, path) per bootstrap entry; path is None for builtin scripts."""
        out: list[tuple[str, Path | None]] = []
        for entry in self.bootstrap:
            if entry.startswith(BUILTIN_PREFIX):
                out.append((entry, None))
            else:
                out.append((entry, (self.root / entry).resolve()))
        return out

    def fingerprint(self) -> str:
        """Hash of every setting that influences generated output.

        A change forces a full rebuild, because previously generated files may
        no longer match what the new settings would produce.
        """
        bootstrap_hashes = []
        for label, path in self.bootstrap_paths():
            if path is None:
                bootstrap_hashes.append(label)
            else:
                digest = sha256_file(path) if path.is_file() else "missing"
                bootstrap_hashes.append(f"{label}:{digest}")
        payload = {
            "migrations_dir": self.migrations_dir,
            "output_dir": self.output_dir,
            "schemas": list(self.schemas) if self.schemas is not None else "auto",
            "exclude_schemas": sorted(self.exclude_schemas),
            "mode": self.mode,
            "image": self.docker.image,
            "migration_role": self.docker.migration_role,
            "bootstrap": bootstrap_hashes,
        }
        return sha256_text(json.dumps(payload, sort_keys=True))


def find_project_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_FILENAME).is_file():
            return candidate
    return None


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ConfigError(f"Invalid {CONFIG_FILENAME}: {message}")


def _relative_dir(raw: Any, key: str, root: Path) -> str:
    _require(isinstance(raw, str) and raw.strip() != "", f"'{key}' must be a non-empty string")
    path = Path(raw)
    _require(not path.is_absolute(), f"'{key}' must be relative to the project root, got '{raw}'")
    _require(is_within(root / path, root), f"'{key}' must stay inside the project root, got '{raw}'")
    return path.as_posix()


def parse_config(data: Any, root: Path) -> Config:
    _require(isinstance(data, dict), "top level must be a JSON object")
    unknown = set(data) - _KNOWN_KEYS
    _require(not unknown, f"unknown keys: {', '.join(sorted(unknown))}")
    version = data.get("config_version", 1)
    _require(version == 1, f"unsupported config_version {version!r} (this DBSS understands 1)")

    migrations_dir = _relative_dir(data.get("migrations_dir", DEFAULT_CONFIG["migrations_dir"]), "migrations_dir", root)
    output_dir = _relative_dir(data.get("output_dir", DEFAULT_CONFIG["output_dir"]), "output_dir", root)

    out_path = (root / output_dir).resolve()
    mig_path = (root / migrations_dir).resolve()
    state_path = (root / STATE_DIRNAME).resolve()
    _require(out_path != root.resolve(), "'output_dir' cannot be the project root")
    _require(not is_within(mig_path, out_path) and not is_within(out_path, mig_path),
             "'output_dir' and 'migrations_dir' must not contain each other")
    _require(not is_within(state_path, out_path) and not is_within(out_path, state_path),
             f"'output_dir' must not contain or be inside {STATE_DIRNAME}/")

    schemas_raw = data.get("schemas", "auto")
    schemas: tuple[str, ...] | None
    if schemas_raw == "auto":
        schemas = None
    else:
        _require(isinstance(schemas_raw, list) and schemas_raw and all(isinstance(s, str) and s for s in schemas_raw),
                 "'schemas' must be \"auto\" or a non-empty list of schema names")
        schemas = tuple(dict.fromkeys(schemas_raw))

    exclude_raw = data.get("exclude_schemas", [])
    _require(isinstance(exclude_raw, list) and all(isinstance(s, str) for s in exclude_raw),
             "'exclude_schemas' must be a list of schema names")

    mode = data.get("mode", "docker")
    _require(mode == "docker", f"'mode' must be \"docker\" (got {mode!r}); other modes are not implemented yet")

    strict = data.get("strict", False)
    _require(isinstance(strict, bool), "'strict' must be true or false")

    docker_raw = data.get("docker", {})
    _require(isinstance(docker_raw, dict), "'docker' must be an object")
    unknown = set(docker_raw) - _KNOWN_DOCKER_KEYS
    _require(not unknown, f"unknown keys in 'docker': {', '.join(sorted(unknown))}")
    image = docker_raw.get("image", DEFAULT_IMAGE)
    _require(isinstance(image, str) and image.strip() != "", "'docker.image' must be a non-empty string")
    timeout = docker_raw.get("startup_timeout_seconds", 120)
    _require(isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0,
             "'docker.startup_timeout_seconds' must be a positive integer")
    role = docker_raw.get("migration_role", "postgres")
    _require(isinstance(role, str) and role != "", "'docker.migration_role' must be a non-empty string")

    bootstrap_raw = data.get("bootstrap", DEFAULT_CONFIG["bootstrap"])
    _require(isinstance(bootstrap_raw, list) and all(isinstance(b, str) and b for b in bootstrap_raw),
             "'bootstrap' must be a list of builtin:<name> entries or relative SQL file paths")
    for entry in bootstrap_raw:
        if entry.startswith(BUILTIN_PREFIX):
            name = entry[len(BUILTIN_PREFIX):]
            _require(name in BUILTIN_BOOTSTRAPS,
                     f"unknown bootstrap '{entry}' (available: {', '.join(BUILTIN_PREFIX + b for b in BUILTIN_BOOTSTRAPS)})")
        else:
            _relative_dir(entry, "bootstrap", root)

    return Config(
        root=root.resolve(),
        migrations_dir=migrations_dir,
        output_dir=output_dir,
        schemas=schemas,
        exclude_schemas=tuple(exclude_raw),
        mode=mode,
        strict=strict,
        docker=DockerSettings(image=image, startup_timeout_seconds=timeout, migration_role=role),
        bootstrap=tuple(bootstrap_raw),
    )


def load_config(root: Path) -> Config:
    path = root / CONFIG_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"No {CONFIG_FILENAME} found in {root}.", hint="Run `dbss init` first.") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    config = parse_config(data, root)
    for label, bpath in config.bootstrap_paths():
        if bpath is not None and not bpath.is_file():
            raise ConfigError(f"Bootstrap file '{label}' listed in {CONFIG_FILENAME} does not exist.")
    return config


def resolve_project(project_dir: Path | None) -> Config:
    if project_dir is not None:
        return load_config(project_dir.resolve())
    root = find_project_root(Path.cwd())
    if root is None:
        raise ConfigError(
            f"No {CONFIG_FILENAME} found in this directory or any parent.",
            hint="Run `dbss init` in your project root, or pass --project-dir.",
        )
    return load_config(root)
