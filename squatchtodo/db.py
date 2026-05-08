"""SQLite connection management and migration runner.

The migration runner is intentionally tiny: it scans ``migrations/*.sql`` in
lexical order and applies any whose filename hasn't been recorded in the
``schema_migrations`` table yet. Each migration runs in its own transaction.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterable, Iterator

MIGRATIONS_PACKAGE = "squatchtodo.migrations"
MIGRATION_FILENAME_RE = re.compile(r"^(\d+)_[a-z0-9_]+\.sql$")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a tuned SQLite connection.

    WAL mode lets the web UI read while the MCP server writes. ``foreign_keys``
    is off by default in SQLite — it must be enabled per connection.
    """
    conn = sqlite3.connect(
        db_path,
        isolation_level=None,  # autocommit; we manage transactions explicitly
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )


def _applied_versions(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


def _iter_migration_resources() -> Iterable[tuple[str, str]]:
    """Yield ``(filename, sql_text)`` for each bundled migration, sorted."""
    files = []
    for entry in resources.files(MIGRATIONS_PACKAGE).iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if not MIGRATION_FILENAME_RE.match(name):
            continue
        files.append((name, entry.read_text(encoding="utf-8")))
    files.sort(key=lambda item: item[0])
    return files


def run_migrations(db_path: str | Path) -> list[str]:
    """Apply any pending migrations. Returns the list of versions newly applied.

    Each migration runs inside its own transaction. We embed ``BEGIN;`` /
    ``COMMIT;`` *inside* the executescript call rather than wrapping it from
    Python, because ``executescript`` implicitly commits any pending
    transaction on entry — wrapping it externally is broken.

    The version literal is interpolated rather than parameterised because
    ``executescript`` takes no parameters; the filename is regex-restricted to
    ``^\\d+_[a-z0-9_]+\\.sql$`` upstream so injection isn't possible.
    """
    applied: list[str] = []
    conn = connect(db_path)
    try:
        _ensure_migrations_table(conn)
        already = _applied_versions(conn)
        for filename, sql in _iter_migration_resources():
            if filename in already:
                continue
            assert MIGRATION_FILENAME_RE.match(filename), filename
            script = (
                "BEGIN;\n"
                f"{sql}\n"
                f"INSERT INTO schema_migrations(version) VALUES ('{filename}');\n"
                "COMMIT;\n"
            )
            conn.executescript(script)
            applied.append(filename)
    finally:
        conn.close()
    return applied


def cli_migrate(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply SquatchTodo migrations.")
    parser.add_argument(
        "--db",
        required=True,
        help="Path to the SQLite database file (will be created if missing).",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    applied = run_migrations(db_path)
    if applied:
        for version in applied:
            print(f"applied {version}")
    else:
        print("no pending migrations")
    return 0


if __name__ == "__main__":
    sys.exit(cli_migrate())
