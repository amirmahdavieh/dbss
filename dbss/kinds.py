"""Object kinds handled by DBSS and their output folders."""

from __future__ import annotations

from dbss.errors import ConfigError

KINDS: tuple[str, ...] = (
    "table",
    "constraint",
    "index",
    "trigger",
    "policy",
    "function",
    "type",
    "grant",
)

FOLDERS: dict[str, str] = {
    "table": "tables",
    "constraint": "constraints",
    "index": "indexes",
    "trigger": "triggers",
    "policy": "policies",
    "function": "functions",
    "type": "types",
    "grant": "grants",
}

_ALIASES: dict[str, str] = {}
for _kind, _folder in FOLDERS.items():
    _ALIASES[_kind] = _kind
    _ALIASES[_folder] = _kind
_ALIASES["indices"] = "index"


def parse_kind(value: str) -> str:
    """Accept singular or plural kind names (case-insensitive)."""
    kind = _ALIASES.get(value.strip().lower())
    if kind is None:
        raise ConfigError(
            f"Unknown object kind '{value}'.",
            hint="Valid kinds: " + ", ".join(KINDS) + " (plural forms are accepted too).",
        )
    return kind
