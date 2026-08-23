"""Shared low-level SQLite connection setup for queue commands and projections."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a queue connection with rows addressable by column name."""
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    return connection
