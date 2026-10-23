"""Synchronization: replay -> introspect -> render -> stage files -> persist state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from dbss import __version__
from dbss.config import Config
from dbss.database import ScratchDatabase
from dbss.errors import ConflictError, RebuildRequiredError, UnsupportedObjectError
from dbss.introspection import Catalog, take_snapshot
from dbss.kinds import KINDS
from dbss.migrations import Migration, discover_migrations, find_migration
from dbss.renderers import not_represented, render_kind
from dbss.renderers.common import RenderedObject
from dbss.state import MigrationRecord, State, load_state, save_state, utc_now
from dbss.sync.analysis import Analysis, analyze
from dbss.sync.files import FilePlan, commit_files, finish_commit, load_journal, plan_files
from dbss.utils.fs import sha256_file

Progress = Callable[[str], None]


@dataclass
class SyncReport:
    command: str
    kinds: list[str]
    dry_run: bool = False
    skipped_reason: str | None = None
    replayed: list[str] = field(default_factory=list)
    plan: FilePlan = field(default_factory=FilePlan)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    not_represented: list[str] = field(default_factory=list)
    pending_after: list[tuple[str, list[str]]] = field(default_factory=list)


@dataclass
class Context:
    config: Config
    state: State
    migrations: list[Migration]
    discovery_warnings: list[str]
    analysis: Analysis
    journal: dict | None


def load_context(config: Config) -> Context:
    state = load_state(config.state_path)
    migrations, warnings = discover_migrations(config.migrations_path)
    return Context(config, state, migrations, warnings, analyze(config, state, migrations),
                   load_journal(config.state_dir))


def manifest_intact(ctx: Context) -> bool:
    for rel, rec in ctx.state.files.items():
        target = ctx.config.output_path / rel
        if not target.is_file() or sha256_file(target) != rec.sha256:
            return False
    return True


def _replay(ctx: Context, upto: int, progress: Progress) -> tuple[Catalog, Catalog]:
    with ScratchDatabase(ctx.config, progress) as db:
        baseline = take_snapshot(db)
        to_run = ctx.migrations[:upto]
        if to_run:
            progress(f"Replaying {len(to_run)} migration(s) in the scratch database...")
        for m in to_run:
            db.run_migration(m)
        progress("Introspecting catalogs...")
        final = take_snapshot(db)
    return baseline, final


def _render(ctx: Context, kinds: list[str], baseline: Catalog, final: Catalog) -> dict[str, RenderedObject]:
    rendered: dict[str, RenderedObject] = {}
    errors: list[str] = []
    for kind in kinds:
        out = render_kind(kind, final, baseline, ctx.config)
        errors.extend(f"[{kind}] {e}" for e in out.errors)
        rendered.update(out.files)
    if errors:
        raise UnsupportedObjectError(
            "Some objects cannot be reconstructed safely; nothing was synchronized.",
            hint="Change or remove these objects in a migration, or exclude their schema in dbss.config.json.",
            details=errors,
        )
    return rendered


def _dependency_warnings(ctx: Context, kinds: list[str], plan: FilePlan) -> list[str]:
    """After a partial apply, report files of lagging kinds that still describe dropped tables."""
    dropped = {ctx.state.files[p].table for p in plan.paths("delete")
               if ctx.state.files[p].kind == "table" and ctx.state.files[p].table}
    out = []
    for rel, rec in sorted(ctx.state.files.items()):
        if rec.kind not in kinds and rec.table in dropped:
            out.append(f"{rel} still describes dropped table {rec.table}; "
                       f"it will be removed once '{rec.kind}' is synchronized (dbss update)")
    return out


def _synchronize(ctx: Context, *, command: str, kinds: list[str], upto: int, force: bool, dry_run: bool,
                 progress: Progress, report: SyncReport) -> SyncReport:
    baseline, final = _replay(ctx, upto, progress)
    report.replayed = [m.name for m in ctx.migrations[:upto]]
    rendered = _render(ctx, kinds, baseline, final)

    report.not_represented = not_represented(final, baseline)
    if ctx.config.strict and report.not_represented:
        raise UnsupportedObjectError(
            "strict mode: migrations create objects that DBSS does not represent.",
            hint="Set \"strict\": false in dbss.config.json to only report them.",
            details=report.not_represented,
        )

    plan = plan_files(ctx.config.output_path, ctx.state.files, rendered, set(kinds), force=force, journal=ctx.journal)
    report.plan = plan
    if plan.conflicts:
        raise ConflictError(
            "Generated output conflicts with files DBSS does not own; nothing was changed.",
            hint="Move or restore these files, or rerun with --force to overwrite them.",
            details=plan.conflicts,
        )
    if set(kinds) != set(KINDS):
        report.warnings.extend(_dependency_warnings(ctx, kinds, plan))
    if dry_run:
        return report

    staging = commit_files(plan, ctx.config.output_path, ctx.config.state_dir)

    state = ctx.state
    if command == "update":
        state.migrations = [MigrationRecord(m.name, m.sha256, set(KINDS)) for m in ctx.migrations[:upto]]
    else:
        existing = {r.name: r for r in state.migrations}
        records = []
        for i, m in enumerate(ctx.migrations):
            rec = existing.get(m.name)
            if rec is None or rec.sha256 != m.sha256:
                rec = MigrationRecord(m.name, m.sha256, set())
            if i < upto:
                rec.completed |= set(kinds)
            if rec.completed:
                records.append(rec)
        state.migrations = records
    for rel in [p for p, r in state.files.items() if r.kind in kinds]:
        del state.files[rel]
    for change in plan.changes:
        if change.action != "delete" and change.record is not None:
            state.files[change.path] = change.record
    state.dbss_version = __version__
    state.config_fingerprint = ctx.config.fingerprint()
    state.last_sync = {
        "at": utc_now(),
        "command": command,
        "kinds": kinds,
        "through": ctx.migrations[upto - 1].name if upto else None,
    }
    state.last_error = None
    save_state(ctx.config.state_path, state)
    finish_commit(ctx.config.state_dir, staging)

    report.pending_after = [(m.name, k) for m, k in analyze(ctx.config, state, ctx.migrations).pending_migrations]
    return report


def run_update(config: Config, *, force: bool = False, dry_run: bool = False, full: bool = False,
               progress: Progress = lambda _m: None) -> SyncReport:
    ctx = load_context(config)
    report = SyncReport(command="update", kinds=list(KINDS), dry_run=dry_run)
    report.warnings.extend(ctx.discovery_warnings)
    if ctx.analysis.rebuild_required:
        report.notes.append("Full rebuild: " + "; ".join(ctx.analysis.rebuild_reasons))
    if ctx.journal is not None:
        report.notes.append("Recovering from an interrupted previous synchronization.")
    if (not full and ctx.analysis.fully_synchronized and ctx.journal is None and manifest_intact(ctx)
            and ctx.state.config_fingerprint is not None):
        report.skipped_reason = "Already up to date; no migrations are pending."
        return report
    if not ctx.migrations and not ctx.state.files:
        report.skipped_reason = f"No migrations found in {config.migrations_dir}; nothing to do."
        return report
    return _synchronize(ctx, command="update", kinds=list(KINDS), upto=len(ctx.migrations), force=force,
                        dry_run=dry_run, progress=progress, report=report)


def run_apply(config: Config, kind: str, migration_arg: str, *, force: bool = False, dry_run: bool = False,
              progress: Progress = lambda _m: None) -> SyncReport:
    ctx = load_context(config)
    report = SyncReport(command=f"apply {kind}", kinds=[kind], dry_run=dry_run)
    report.warnings.extend(ctx.discovery_warnings)
    if ctx.analysis.rebuild_required:
        raise RebuildRequiredError(
            "Migration history changed since the last synchronization; a partial apply is not safe.",
            hint="Run `dbss update` to rebuild all generated files from a full replay.",
            details=ctx.analysis.rebuild_reasons,
        )
    index = find_migration(ctx.migrations, config.migrations_path, migration_arg)
    watermark = ctx.analysis.watermarks[kind]
    if index < watermark:
        report.skipped_reason = f"{ctx.migrations[index].name} is already incorporated into {kind} files."
        return report
    earlier = [m.name for m in ctx.migrations[watermark:index]]
    if earlier:
        report.notes.append(
            f"Also incorporates {len(earlier)} earlier pending migration(s) for '{kind}': " + ", ".join(earlier))
    return _synchronize(ctx, command=f"apply {kind}", kinds=[kind], upto=index + 1, force=force, dry_run=dry_run,
                        progress=progress, report=report)
