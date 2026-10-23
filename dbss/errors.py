"""Error types. Every user-facing failure is a DbssError carrying a process exit code."""

from __future__ import annotations


class DbssError(Exception):
    """Base class for actionable DBSS failures."""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details or []


class ConfigError(DbssError):
    exit_code = 2


class PrerequisiteError(DbssError):
    """A required external tool (Docker, an image) is missing or not running."""

    exit_code = 3


class ConflictError(DbssError):
    """Generated output would overwrite files DBSS does not own or that were edited by hand."""

    exit_code = 4


class MigrationError(DbssError):
    """A migration failed while being replayed in the scratch database."""

    exit_code = 5


class UnsupportedObjectError(DbssError):
    """An object exists that DBSS cannot reconstruct safely."""

    exit_code = 6


class RebuildRequiredError(DbssError):
    """Migration history changed; only a full `dbss update` can resynchronize."""

    exit_code = 7


class StateError(DbssError):
    exit_code = 8
