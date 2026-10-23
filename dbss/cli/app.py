"""Command-line interface."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer

from dbss import __version__
from dbss.config import CONFIG_FILENAME, DEFAULT_CONFIG, STATE_DIRNAME, load_config, resolve_project
from dbss.errors import DbssError
from dbss.kinds import FOLDERS, KINDS, parse_kind
from dbss.state import State, record_error, save_state
from dbss.sync.engine import SyncReport, run_apply, run_update
from dbss.sync.status import collect_status
from dbss.utils.fs import atomic_write_json, atomic_write_text

app = typer.Typer(
    help=(
        "DBSS - Database Schema Synchronizer.\n\n"
        "Turns Supabase/PostgreSQL migrations into current-state SQL definitions, one file per object, "
        "by replaying them in a disposable local PostgreSQL container. DBSS never touches your remote "
        "database and never runs git."
    ),
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

_options: dict[str, object] = {"project_dir": None, "quiet": False}


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"dbss {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    project_dir: Optional[Path] = typer.Option(
        None, "--project-dir", "-C", help="Project root (defaults to the nearest directory with dbss.config.json)."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress progress messages."),
    version: bool = typer.Option(False, "--version", callback=_version_callback, is_eager=True,
                                 help="Show the DBSS version and exit."),
) -> None:
    _options["project_dir"] = project_dir
    _options["quiet"] = quiet


def _progress(message: str) -> None:
    if not _options["quiet"]:
        typer.echo(f"> {message}", err=True)


def _fail(exc: DbssError, command: str, state_path: Path | None) -> None:
    typer.secho(f"error: {exc.message}", fg=typer.colors.RED, err=True)
    for line in exc.details:
        typer.echo(f"  {line}", err=True)
    if exc.hint:
        typer.echo(f"hint: {exc.hint}", err=True)
    if state_path is not None:
        record_error(state_path, command, exc.message)
    raise typer.Exit(exc.exit_code)


def _run(command: str, fn) -> None:  # noqa: ANN001
    state_path = None
    try:
        config = resolve_project(_options["project_dir"])  # type: ignore[arg-type]
        state_path = config.state_path
        fn(config)
    except DbssError as exc:
        _fail(exc, command, state_path)
    except KeyboardInterrupt:
        typer.echo("Interrupted; generated files and state were left unchanged or will be recovered on the next run.",
                   err=True)
        raise typer.Exit(130) from None


def _print_report(report: SyncReport) -> None:
    for warning in report.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)
    for note in report.notes:
        typer.echo(f"note: {note}")
    if report.skipped_reason:
        typer.echo(report.skipped_reason)
        return
    plan = report.plan
    prefix = "Would synchronize" if report.dry_run else "Synchronized"
    through = report.replayed[-1] if report.replayed else "(no migrations)"
    typer.echo(f"{prefix} {', '.join(report.kinds)} through {through}")
    for action, word, color in (("create", "created", typer.colors.GREEN), ("update", "updated", typer.colors.CYAN),
                                ("delete", "removed", typer.colors.RED)):
        for path in plan.paths(action):
            typer.secho(f"  {word:8} {path}", fg=color)
    unchanged = len(plan.paths("unchanged"))
    if not plan.has_changes:
        typer.echo("  no file changes" + (f" ({unchanged} unchanged)" if unchanged else ""))
    elif unchanged:
        typer.echo(f"  {unchanged} unchanged")
    if report.not_represented:
        typer.echo("Not represented by DBSS (no matching object kind; informational):")
        for item in report.not_represented:
            typer.echo(f"  - {item}")
    if report.dry_run:
        typer.echo("Dry run: no files or state were changed.")
    elif report.pending_after:
        typer.echo(f"Still pending: {len(report.pending_after)} migration(s); run `dbss status` for details.")


@app.command()
def init() -> None:
    """Create dbss.config.json, the output folders and .dbss/ (idempotent; never overwrites)."""
    root = (_options["project_dir"] or Path.cwd()).resolve()  # type: ignore[union-attr]
    try:
        config_path = root / CONFIG_FILENAME
        if config_path.exists():
            config = load_config(root)
            typer.echo(f"kept     {CONFIG_FILENAME}")
        else:
            atomic_write_json(config_path, DEFAULT_CONFIG)
            config = load_config(root)
            typer.echo(f"created  {CONFIG_FILENAME}")
        for kind in KINDS:
            folder = config.output_path / FOLDERS[kind]
            existed = folder.is_dir()
            folder.mkdir(parents=True, exist_ok=True)
            typer.echo(f"{'kept' if existed else 'created':8} {config.output_dir}/{FOLDERS[kind]}/")
        config.state_dir.mkdir(exist_ok=True)
        if not config.state_path.exists():
            save_state(config.state_path, State())
            typer.echo(f"created  {STATE_DIRNAME}/state.json")
        else:
            typer.echo(f"kept     {STATE_DIRNAME}/state.json")
        ignore = config.state_dir / ".gitignore"
        if not ignore.exists():
            atomic_write_text(ignore, "staging/\njournal.json\n")
        if not config.migrations_path.is_dir():
            typer.secho(f"note: migrations directory {config.migrations_dir}/ does not exist yet.",
                        fg=typer.colors.YELLOW)
        typer.echo("Run `dbss update` to generate definitions from your migrations.")
    except DbssError as exc:
        _fail(exc, "init", None)


@app.command()
def apply(
    kind: str = typer.Argument(..., help="Object kind: " + ", ".join(KINDS) + " (plural accepted)."),
    migration: str = typer.Argument(..., help="Migration file (path or file name) inside the migrations directory."),
    force: bool = typer.Option(False, "--force", help="Overwrite generated files edited by hand / unmanaged files."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change without writing anything."),
) -> None:
    """Incorporate a migration into the local definitions of ONE object kind.

    This updates DBSS's generated files; it does not run anything against your Supabase project.
    Other kinds stay pending until `dbss update` (or `dbss apply <kind>`) processes them.
    """
    def go(config):  # noqa: ANN001, ANN202
        _print_report(run_apply(config, parse_kind(kind), migration, force=force, dry_run=dry_run,
                                progress=_progress))
    _run(f"apply {kind}", go)


@app.command()
def update(
    force: bool = typer.Option(False, "--force", help="Overwrite generated files edited by hand / unmanaged files."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change without writing anything."),
    full: bool = typer.Option(False, "--full", help="Replay and regenerate even if nothing appears pending."),
) -> None:
    """Process all pending migrations and synchronize every object kind."""
    def go(config):  # noqa: ANN001, ANN202
        _print_report(run_update(config, force=force, dry_run=dry_run, full=full, progress=_progress))
    _run("update", go)


@app.command()
def status(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    check: bool = typer.Option(False, "--check", help="Exit with code 1 unless everything is synchronized."),
) -> None:
    """Show processed, pending and changed migrations, per-kind progress and errors."""
    def go(config):  # noqa: ANN001, ANN202
        info = collect_status(config)
        if as_json:
            typer.echo(json.dumps(info, indent=2))
        else:
            _print_status(info)
        if check and not info["up_to_date"]:
            raise typer.Exit(1)
    _run("status", go)


def _print_status(info: dict) -> None:
    m = info["migrations"]
    typer.echo(f"Project:     {info['project_root']}")
    typer.echo(f"Migrations:  {info['migrations_dir']}/ ({m['total']} found, {m['processed']} fully processed, "
               f"{len(m['pending'])} pending)")
    typer.echo(f"Output:      {info['output_dir']}/ ({info['managed_files']['count']} managed files)")
    typer.echo("")
    typer.echo("Per kind:")
    for kind, k in info["kinds"].items():
        through = k["through"] or "-"
        state = "up to date" if k["pending"] == 0 else f"{k['pending']} pending"
        typer.echo(f"  {kind:10} {state:12} through {through}")
    if m["pending"]:
        typer.echo("")
        typer.echo("Pending migrations:")
        for p in m["pending"]:
            kinds = "all kinds" if len(p["kinds"]) == len(KINDS) else ", ".join(p["kinds"])
            typer.echo(f"  {p['name']}  ({kinds})")
    if info["rebuild_required"]:
        typer.echo("")
        typer.secho("Full rebuild required (run `dbss update`):", fg=typer.colors.YELLOW)
        for reason in info["rebuild_reasons"]:
            typer.echo(f"  - {reason}")
    mf = info["managed_files"]
    if mf["edited"] or mf["missing"]:
        typer.echo("")
        typer.secho("Managed files changed outside DBSS:", fg=typer.colors.YELLOW)
        for rel in mf["edited"]:
            typer.echo(f"  edited   {rel}")
        for rel in mf["missing"]:
            typer.echo(f"  missing  {rel}")
    if info["interrupted_sync"]:
        typer.secho("A previous synchronization was interrupted; the next `dbss update` will recover.",
                    fg=typer.colors.YELLOW)
    for warning in info["warnings"]:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW)
    if info["last_error"]:
        e = info["last_error"]
        typer.echo("")
        typer.secho(f"Last error ({e['at']}, dbss {e['command']}): {e['message']}", fg=typer.colors.RED)
    if info["last_sync"]:
        s = info["last_sync"]
        typer.echo("")
        typer.echo(f"Last sync:   {s['at']} (dbss {s['command']}, through {s['through'] or '-'})")
    typer.echo("")
    typer.echo("Everything is synchronized." if info["up_to_date"] else "Not fully synchronized.")


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
