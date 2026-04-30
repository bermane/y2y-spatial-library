"""SQLite connection helpers — the source of truth lives here.

The catalogue is one SQLite file: ``inventory/inventory.db``. Every
read/write goes through ``connect()``, which:

* Enables foreign-key enforcement (``PRAGMA foreign_keys = ON``). This
  is **per-connection** — without it, the schema's FK declarations are
  ignored at runtime.
* Sets ``journal_mode = WAL`` so a long-running reader (e.g. the steward
  scrolling the exported xlsx) doesn't block writers.
* Makes rows behave like dicts (``row_factory = sqlite3.Row``) so
  callers can do ``row["dataset_id"]`` instead of positional indexing.

Bootstrap behaviour: ``connect()`` applies ``schema.sql`` if the
database file doesn't exist yet. Subsequent connections skip the apply
(the schema's own ``CREATE TABLE IF NOT EXISTS`` makes it idempotent
either way; the existence check is just an optimisation).

PostGIS portability note: STRICT mode and TEXT/INTEGER-only column
types come from ``schema.sql``, not from this module. ``connect()``
only handles SQLite-specific runtime concerns (WAL, FKs).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_FILENAME = "inventory.db"

# Path to the canonical schema, applied on first connect.
_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open a connection with the project's standard PRAGMAs.

    If ``db_path`` doesn't exist, the schema is applied automatically.
    Caller is responsible for closing the connection (or use
    :func:`connect` as a context manager).
    """
    db_path = Path(db_path)
    fresh = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    if fresh:
        init_schema(conn)
    return conn


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Context-manager wrapper around :func:`get_connection`."""
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply ``pipeline/schema.sql`` to ``conn``. Idempotent."""
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
