"""Disposable PostgreSQL containers.

The container runs with ``--network none``: migrations are executed in full
isolation (no outbound network, no published port) and DBSS talks to the server
only through ``docker exec psql``. The container is removed on exit, also on
errors and Ctrl-C.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable
from importlib import resources
from typing import Any

from dbss.config import BUILTIN_PREFIX, Config
from dbss.errors import DbssError, MigrationError, PrerequisiteError
from dbss.migrations import Migration

Progress = Callable[[str], None]

_STMT_TIMEOUT_SECONDS = 900


def _builtin_bootstrap_sql(name: str) -> str:
    return resources.files("dbss.database.bootstrap").joinpath(f"{name}.sql").read_text(encoding="utf-8")


class ScratchDatabase:
    """Context manager around a throwaway ``supabase/postgres`` container."""

    def __init__(self, config: Config, progress: Progress | None = None):
        self.config = config
        self.settings = config.docker
        self.progress = progress or (lambda _msg: None)
        self.name = f"dbss-scratch-{secrets.token_hex(4)}"
        self._password = secrets.token_hex(16)
        self._docker = shutil.which("docker")
        self._started = False
        self.admin_role = "supabase_admin"

    # -- lifecycle -----------------------------------------------------------------

    def __enter__(self) -> ScratchDatabase:
        self._check_docker()
        self._ensure_image()
        self._start()
        try:
            self._wait_ready()
            self._run_bootstrap()
        except BaseException:
            self._remove()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self._remove()

    def _docker_cmd(self, args: list[str], *, input_bytes: bytes | None = None,
                    timeout: float | None = 120) -> subprocess.CompletedProcess[bytes]:
        assert self._docker is not None
        return subprocess.run([self._docker, *args], input=input_bytes, capture_output=True, timeout=timeout)

    def _check_docker(self) -> None:
        if self._docker is None:
            raise PrerequisiteError(
                "Docker is required: DBSS replays migrations in a disposable PostgreSQL container.",
                hint="Install Docker Desktop (or Docker Engine) and make sure `docker` is on PATH.",
            )
        try:
            proc = self._docker_cmd(["version", "--format", "{{.Server.Version}}"], timeout=30)
        except subprocess.TimeoutExpired:
            raise PrerequisiteError("Timed out talking to the Docker daemon.") from None
        if proc.returncode != 0:
            raise PrerequisiteError(
                "The Docker daemon is not reachable.",
                hint="Start Docker Desktop (or the docker service) and retry.",
                details=[proc.stderr.decode(errors="replace").strip()],
            )

    def _ensure_image(self) -> None:
        image = self.settings.image
        if self._docker_cmd(["image", "inspect", image], timeout=60).returncode == 0:
            return
        self.progress(f"Pulling {image} (first run only; this can take a few minutes)...")
        proc = self._docker_cmd(["pull", image], timeout=None)
        if proc.returncode != 0:
            raise PrerequisiteError(
                f"Could not pull image {image}.",
                hint="Check the image name in dbss.config.json (docker.image) and your network connection.",
                details=[proc.stderr.decode(errors="replace").strip()],
            )

    def _start(self) -> None:
        self.progress(f"Starting scratch database ({self.settings.image})...")
        proc = self._docker_cmd([
            "run", "--detach", "--rm",
            "--name", self.name,
            "--network", "none",
            "--label", "dbss.scratch=1",
            "--env", f"POSTGRES_PASSWORD={self._password}",
            self.settings.image,
        ], timeout=300)
        if proc.returncode != 0:
            raise PrerequisiteError(
                "Failed to start the scratch PostgreSQL container.",
                details=[proc.stderr.decode(errors="replace").strip()],
            )
        self._started = True

    def _remove(self) -> None:
        if self._started and self._docker:
            try:
                self._docker_cmd(["rm", "--force", "--volumes", self.name], timeout=120)
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass
            self._started = False

    def _container_running(self) -> bool:
        proc = self._docker_cmd(["inspect", "--format", "{{.State.Running}}", self.name], timeout=30)
        return proc.returncode == 0 and proc.stdout.strip() == b"true"

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.settings.startup_timeout_seconds
        while time.monotonic() < deadline:
            if not self._container_running():
                logs = self._docker_cmd(["logs", "--tail", "40", self.name], timeout=30)
                raise PrerequisiteError(
                    "The scratch PostgreSQL container exited during startup.",
                    details=(logs.stdout + logs.stderr).decode(errors="replace").strip().splitlines()[-20:],
                )
            ready = self._docker_cmd(["exec", self.name, "pg_isready", "-q", "-h", "localhost", "-U", "postgres"],
                                     timeout=30)
            if ready.returncode == 0 and self._try_admin():
                return
            time.sleep(0.5)
        raise PrerequisiteError(
            f"The scratch database did not become ready within {self.settings.startup_timeout_seconds}s.",
            hint="Increase docker.startup_timeout_seconds in dbss.config.json.",
        )

    def _try_admin(self) -> bool:
        for role in ("supabase_admin", "postgres"):
            proc = self._psql(role, "SELECT 1", tuples=True, check=False)
            if proc.returncode == 0:
                self.admin_role = role
                return True
        return False

    # -- psql ----------------------------------------------------------------------

    def _psql(self, role: str, sql: str, *, tuples: bool = False, single_transaction: bool = False,
              check: bool = True, timeout: float = _STMT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[bytes]:
        args = [
            "exec", "-i",
            "--env", f"PGPASSWORD={self._password}",
            "--env", "PGCLIENTENCODING=UTF8",
            "--env", "PGOPTIONS=-c client_min_messages=warning",
            self.name,
            "psql", "-X", "-q", "-h", "localhost", "-U", role, "-d", "postgres",
            "-v", "ON_ERROR_STOP=1",
        ]
        if tuples:
            args += ["-t", "-A"]
        if single_transaction:
            args.append("--single-transaction")
        args += ["-f", "-"]
        try:
            proc = self._docker_cmd(args, input_bytes=sql.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DbssError(f"psql timed out after {timeout}s in the scratch database.") from None
        if check and proc.returncode != 0:
            raise DbssError("Query failed in the scratch database.",
                            details=proc.stderr.decode("utf-8", errors="replace").strip().splitlines())
        return proc

    def _run_script(self, role: str, label: str, sql: str, error_cls: type[DbssError]) -> None:
        proc = self._psql(role, sql, single_transaction=True, check=False)
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").replace("<stdin>", label)
            lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
            raise error_cls(
                f"{label} failed in the scratch database; nothing was synchronized.",
                hint="Fix the migration (or bootstrap) SQL; DBSS state and generated files were left unchanged.",
                details=lines[-25:],
            )

    def _run_bootstrap(self) -> None:
        for label, path in self.config.bootstrap_paths():
            if path is None:
                sql = _builtin_bootstrap_sql(label[len(BUILTIN_PREFIX):])
            else:
                sql = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
            self.progress(f"Bootstrapping platform schema ({label})...")
            self._run_script(self.admin_role, f"bootstrap {label}", sql, DbssError)

    def run_migration(self, migration: Migration) -> None:
        """Execute one migration as the configured migration role, in one transaction."""
        self._run_script(self.settings.migration_role, migration.name, migration.read_sql(), MigrationError)

    def query_json(self, sql: str) -> Any:
        """Run a query returning a single JSON value as the superuser and decode it."""
        proc = self._psql(self.admin_role, sql, tuples=True)
        out = proc.stdout.decode("utf-8").strip()
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise DbssError(f"Unexpected catalog query output ({exc}).") from None
