"""Staged, journaled replacement of generated files.

Only files recorded in the manifest are ever modified or deleted. A file that
exists but is not in the manifest, or a managed file whose content no longer
matches the recorded hash (edited by hand), is a conflict unless ``--force``.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from dbss.errors import DbssError
from dbss.renderers.common import RenderedObject
from dbss.state import JOURNAL_NAME, FileRecord, utc_now
from dbss.utils.fs import atomic_write_json, atomic_write_text, is_within, sha256_file, sha256_text


@dataclass
class FileChange:
    path: str
    action: str  # create | update | delete | unchanged
    content: str | None = None
    record: FileRecord | None = None


@dataclass
class FilePlan:
    changes: list[FileChange] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def paths(self, action: str) -> list[str]:
        return [c.path for c in self.changes if c.action == action]

    @property
    def has_changes(self) -> bool:
        return any(c.action != "unchanged" for c in self.changes)


def load_journal(state_dir: Path) -> dict | None:
    path = state_dir / JOURNAL_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ops": []}


def _target(output: Path, rel: str) -> Path:
    target = output / rel
    if not is_within(target, output):
        raise DbssError(f"Refusing to touch {rel}: path escapes the output directory.")
    return target


def plan_files(output: Path, manifest: dict[str, FileRecord], rendered: dict[str, RenderedObject],
               kinds: set[str], *, force: bool, journal: dict | None) -> FilePlan:
    plan = FilePlan()
    journal_hashes = {op["path"]: op.get("sha256") for op in (journal or {}).get("ops", []) if op.get("op") == "write"}

    for rel, obj in sorted(rendered.items()):
        content = obj.content
        new_hash = sha256_text(content)
        record = FileRecord(kind=obj.kind, sha256=new_hash, object=obj.label, table=obj.table)
        target = _target(output, rel)
        if not target.exists():
            plan.changes.append(FileChange(rel, "create", content, record))
            continue
        disk_hash = sha256_file(target)
        existing = manifest.get(rel)
        if existing is not None:
            if disk_hash not in (existing.sha256, journal_hashes.get(rel)) and disk_hash != new_hash and not force:
                plan.conflicts.append(f"{rel}: generated file was edited outside DBSS")
                continue
        elif disk_hash != new_hash and not force:
            plan.conflicts.append(f"{rel}: file exists but is not managed by DBSS")
            continue
        action = "unchanged" if disk_hash == new_hash else "update"
        plan.changes.append(FileChange(rel, action, content, record))

    for rel, existing in sorted(manifest.items()):
        if existing.kind not in kinds or rel in rendered:
            continue
        target = _target(output, rel)
        if target.exists():
            disk_hash = sha256_file(target)
            if disk_hash not in (existing.sha256, journal_hashes.get(rel)) and not force:
                plan.conflicts.append(f"{rel}: generated file was edited outside DBSS but its object no longer exists")
                continue
        plan.changes.append(FileChange(rel, "delete"))
    return plan


def commit_files(plan: FilePlan, output: Path, state_dir: Path) -> Path:
    """Stage, journal, then replace/delete. Returns the staging dir (removed by ``finish_commit``)."""
    staging = state_dir / "staging" / secrets.token_hex(6)
    writes = [c for c in plan.changes if c.action in ("create", "update")]
    deletes = [c for c in plan.changes if c.action == "delete"]

    for change in writes:
        staged = staging / change.path
        atomic_write_text(staged, change.content or "")
        if sha256_file(staged) != change.record.sha256:  # type: ignore[union-attr]
            raise DbssError(f"Staged file {change.path} failed verification; nothing was changed.")

    atomic_write_json(state_dir / JOURNAL_NAME, {
        "started_at": utc_now(),
        "ops": [{"op": "write", "path": c.path, "sha256": c.record.sha256} for c in writes]  # type: ignore[union-attr]
        + [{"op": "delete", "path": c.path} for c in deletes],
    })

    for change in writes:
        target = _target(output, change.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staging / change.path, target)
        except OSError:
            atomic_write_text(target, change.content or "")
    for change in deletes:
        _target(output, change.path).unlink(missing_ok=True)
    return staging


def finish_commit(state_dir: Path, staging: Path | None) -> None:
    (state_dir / JOURNAL_NAME).unlink(missing_ok=True)
    if staging is not None:
        shutil.rmtree(staging, ignore_errors=True)
    staging_root = state_dir / "staging"
    if staging_root.is_dir() and not any(staging_root.iterdir()):
        staging_root.rmdir()
