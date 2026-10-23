"""Compare migrations on disk with recorded state: pending work and rebuild triggers."""

from __future__ import annotations

from dataclasses import dataclass, field

from dbss import __version__
from dbss.config import Config
from dbss.kinds import KINDS
from dbss.migrations import Migration
from dbss.state import State


@dataclass
class Analysis:
    migrations: list[Migration]
    modified: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    out_of_order: list[str] = field(default_factory=list)
    rebuild_reasons: list[str] = field(default_factory=list)
    # kind -> number of leading migrations (in current order) already incorporated
    watermarks: dict[str, int] = field(default_factory=dict)

    @property
    def rebuild_required(self) -> bool:
        return bool(self.rebuild_reasons)

    def pending_kinds(self, index: int) -> list[str]:
        return [k for k in KINDS if self.watermarks.get(k, 0) <= index]

    @property
    def pending_migrations(self) -> list[tuple[Migration, list[str]]]:
        out = []
        for i, m in enumerate(self.migrations):
            kinds = self.pending_kinds(i)
            if kinds:
                out.append((m, kinds))
        return out

    @property
    def fully_synchronized(self) -> bool:
        return not self.rebuild_required and all(self.watermarks.get(k, 0) == len(self.migrations) for k in KINDS)


def analyze(config: Config, state: State, migrations: list[Migration]) -> Analysis:
    a = Analysis(migrations=migrations)
    records = {r.name: r for r in state.migrations}
    on_disk = {m.name for m in migrations}

    for m in migrations:
        rec = records.get(m.name)
        if rec is not None and rec.completed and rec.sha256 != m.sha256:
            a.modified.append(m.name)
    a.missing = [r.name for r in state.migrations if r.completed and r.name not in on_disk]

    def done(m: Migration, kind: str) -> bool:
        rec = records.get(m.name)
        return rec is not None and rec.sha256 == m.sha256 and kind in rec.completed

    gaps: set[str] = set()
    for kind in KINDS:
        wm = 0
        while wm < len(migrations) and done(migrations[wm], kind):
            wm += 1
        a.watermarks[kind] = wm
        if any(done(m, kind) for m in migrations[wm:]):
            gap = migrations[wm].name
            if gap not in a.modified:
                gaps.add(gap)
    a.out_of_order = sorted(gaps)

    for name in a.modified:
        a.rebuild_reasons.append(f"{name} was modified after DBSS processed it")
    for name in a.missing:
        a.rebuild_reasons.append(f"{name} was processed but no longer exists")
    for name in a.out_of_order:
        a.rebuild_reasons.append(f"{name} sorts before migrations that were already processed")
    if not state.is_empty:
        if state.dbss_version != __version__:
            a.rebuild_reasons.append(f"DBSS version changed ({state.dbss_version} -> {__version__})")
        if state.config_fingerprint != config.fingerprint():
            a.rebuild_reasons.append("output-affecting settings in dbss.config.json changed")
    return a
