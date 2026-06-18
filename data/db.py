"""SQLite connection + helpers — the single source of truth (PLAN.md §5.2, §6).

Every component communicates only through this database, never by calling each
other directly (FR-DP-2). This module owns: opening a connection, creating the
schema if it is missing, and small generic insert/query helpers. It contains no
decision logic — it just reads and writes rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# storage.db lives at the repo root (PLAN.md §4) and is gitignored.
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "storage.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# The five tables defined in schema.sql, for validation/inspection.
TABLES = (
    "market_data",
    "onchain_data",
    "sentiment_data",
    "signals",
    "trades",
)


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with sensible defaults and ensure the schema exists.

    Rows are returned as ``sqlite3.Row`` (dict-like) and foreign keys are
    enforced. The schema is created on first connect (init-if-missing).
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create any missing tables by running schema.sql (idempotent)."""
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def insert(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> int:
    """Insert one row from a column->value mapping; return its new id."""
    _require_known_table(table)
    columns = ", ".join(row.keys())
    placeholders = ", ".join(["?"] * len(row))
    sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
    cursor = conn.execute(sql, tuple(row.values()))
    conn.commit()
    return cursor.lastrowid


def query(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] | Mapping[str, Any] = (),
) -> list[sqlite3.Row]:
    """Run a SELECT (or any read) and return all rows."""
    return conn.execute(sql, params).fetchall()


def query_all(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    """Return every row of a table, oldest id first."""
    _require_known_table(table)
    return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def _require_known_table(table: str) -> None:
    """Guard against typos / SQL injection via the table name."""
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}; expected one of {TABLES}")
