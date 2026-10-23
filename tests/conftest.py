from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dbss.cli.app import app
from dbss.config import DEFAULT_IMAGE

FIXTURES = Path(__file__).parent / "fixtures"


def _docker_ready() -> str | None:
    docker = shutil.which("docker")
    if docker is None:
        return "docker not installed"
    if subprocess.run([docker, "version"], capture_output=True).returncode != 0:
        return "docker daemon not running"
    if subprocess.run([docker, "image", "inspect", DEFAULT_IMAGE], capture_output=True).returncode != 0:
        return f"image {DEFAULT_IMAGE} not pulled (docker pull {DEFAULT_IMAGE})"
    return None


def pytest_collection_modifyitems(config, items):  # noqa: ANN001
    if not any("docker" in item.keywords for item in items):
        return
    reason = _docker_ready()
    if reason:
        skip = pytest.mark.skip(reason=f"integration test skipped: {reason}")
        for item in items:
            if "docker" in item.keywords:
                item.add_marker(skip)


class Project:
    def __init__(self, root: Path):
        self.root = root
        self.migrations = root / "supabase" / "migrations"
        self.sql = root / "sql"
        self.runner = CliRunner()

    def dbss(self, *args: str, expect: int | None = 0):
        result = self.runner.invoke(app, ["-C", str(self.root), "-q", *args])
        if expect is not None and result.exit_code != expect:
            raise AssertionError(f"dbss {' '.join(args)} exited {result.exit_code} (expected {expect}):\n"
                                 f"{result.output}\n{result.exception!r}")
        return result

    def migration(self, name: str, sql: str) -> Path:
        self.migrations.mkdir(parents=True, exist_ok=True)
        path = self.migrations / name
        path.write_text(sql, encoding="utf-8")
        return path

    def read(self, rel: str) -> str:
        return (self.sql / rel).read_text(encoding="utf-8")

    def files(self) -> set[str]:
        return {p.relative_to(self.sql).as_posix() for p in self.sql.rglob("*") if p.is_file()}

    def snapshot(self) -> dict[str, str]:
        return {rel: self.read(rel) for rel in sorted(self.files())}

    def state(self) -> dict:
        return json.loads((self.root / ".dbss" / "state.json").read_text(encoding="utf-8"))


@pytest.fixture
def project(tmp_path: Path) -> Project:
    p = Project(tmp_path / "proj")
    p.root.mkdir()
    p.dbss("init")
    return p
