"""`dbss status` data model (rendered as text or JSON by the CLI)."""

from __future__ import annotations

from typing import Any

from dbss.config import Config
from dbss.kinds import KINDS
from dbss.sync.engine import load_context
from dbss.utils.fs import sha256_file


def collect_status(config: Config) -> dict[str, Any]:
    ctx = load_context(config)
    a = ctx.analysis
    migrations = ctx.migrations

    edited, missing_files = [], []
    for rel, rec in sorted(ctx.state.files.items()):
        target = config.output_path / rel
        if not target.is_file():
            missing_files.append(rel)
        elif sha256_file(target) != rec.sha256:
            edited.append(rel)

    kinds = {}
    for kind in KINDS:
        wm = a.watermarks.get(kind, 0)
        kinds[kind] = {"through": migrations[wm - 1].name if wm else None, "pending": len(migrations) - wm}

    pending = [{"name": m.name, "kinds": k} for m, k in a.pending_migrations]
    return {
        "project_root": str(config.root),
        "migrations_dir": config.migrations_dir,
        "output_dir": config.output_dir,
        "migrations": {
            "total": len(migrations),
            "processed": len(migrations) - len(pending),
            "pending": pending,
        },
        "kinds": kinds,
        "changed_after_processing": {"modified": a.modified, "missing": a.missing, "out_of_order": a.out_of_order},
        "rebuild_required": a.rebuild_required,
        "rebuild_reasons": a.rebuild_reasons,
        "managed_files": {"count": len(ctx.state.files), "edited": edited, "missing": missing_files},
        "interrupted_sync": ctx.journal is not None,
        "last_sync": ctx.state.last_sync,
        "last_error": ctx.state.last_error,
        "warnings": ctx.discovery_warnings,
        "up_to_date": a.fully_synchronized and not edited and not missing_files and ctx.journal is None,
    }
