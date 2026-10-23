"""Synchronization state (``.dbss/state.json``) and the commit journal.

State records, per migration, its content hash and which object kinds have
incorporated it, plus a manifest of every generated file DBSS owns.
It is only written after generated files were replaced successfully.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dbss import __version__
from dbss.errors import StateError
from dbss.kinds import KINDS
from dbss.utils.fs import atomic_write_json

STATE_FORMAT = 1
JOURNAL_NAME = "journal.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class MigrationRecord:
    name: str
    sha256: str
    completed: set[str] = field(default_factory=set)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "sha256": self.sha256, "completed": [k for k in KINDS if k in self.completed]}


@dataclass
class FileRecord:
    kind: str
    sha256: str
    object: str
    table: str | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "sha256": self.sha256, "object": self.object}
        if self.table is not None:
            data["table"] = self.table
        return data


@dataclass
class State:
    dbss_version: str = __version__
    config_fingerprint: str | None = None
    migrations: list[MigrationRecord] = field(default_factory=list)
    files: dict[str, FileRecord] = field(default_factory=dict)
    last_sync: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None

    def record(self, name: str) -> MigrationRecord | None:
        for rec in self.migrations:
            if rec.name == name:
                return rec
        return None

    @property
    def is_empty(self) -> bool:
        return not self.files and not any(r.completed for r in self.migrations)

    def to_json(self) -> dict[str, Any]:
        return {
            "state_format": STATE_FORMAT,
            "dbss_version": self.dbss_version,
            "config_fingerprint": self.config_fingerprint,
            "migrations": [r.to_json() for r in self.migrations],
            "files": {p: self.files[p].to_json() for p in sorted(self.files)},
            "last_sync": self.last_sync,
            "last_error": self.last_error,
        }


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read {path}: {exc}",
                         hint="Delete the file and run `dbss update` to rebuild state from scratch.") from None
    if not isinstance(data, dict) or data.get("state_format") != STATE_FORMAT:
        raise StateError(f"{path} has an unsupported format.",
                         hint="Delete the file and run `dbss update` to rebuild state from scratch.")
    try:
        migrations = [
            MigrationRecord(name=m["name"], sha256=m["sha256"], completed=set(m.get("completed", [])) & set(KINDS))
            for m in data.get("migrations", [])
        ]
        files = {
            p: FileRecord(kind=f["kind"], sha256=f["sha256"], object=f.get("object", ""), table=f.get("table"))
            for p, f in data.get("files", {}).items()
        }
    except (KeyError, TypeError) as exc:
        raise StateError(f"{path} is malformed ({exc}).",
                         hint="Delete the file and run `dbss update` to rebuild state from scratch.") from None
    return State(
        dbss_version=data.get("dbss_version", "unknown"),
        config_fingerprint=data.get("config_fingerprint"),
        migrations=migrations,
        files=files,
        last_sync=data.get("last_sync"),
        last_error=data.get("last_error"),
    )


def save_state(path: Path, state: State) -> None:
    atomic_write_json(path, state.to_json())


def record_error(path: Path, command: str, message: str) -> None:
    """Best-effort: remember the last failure without touching sync progress."""
    try:
        state = load_state(path)
        state.last_error = {"at": utc_now(), "command": command, "message": message}
        if path.parent.is_dir():
            save_state(path, state)
    except Exception:  # noqa: BLE001 - never mask the original error
        pass
