"""Isolated scratch PostgreSQL databases used to replay migrations."""

from dbss.database.docker import ScratchDatabase

__all__ = ["ScratchDatabase"]
