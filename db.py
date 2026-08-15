"""
db.py — Postgres backend with SQLite-compatible interface.

Drop-in replacement for the SQLite helpers in app.py. Wraps psycopg3 with:
  - ?           → %s     placeholder translation
  - datetime('now')      → now()
  - strftime(...,'now')  → now()
  - dict + tuple row access (compatible with sqlite3.Row usage in legacy code)
  - Connection pooling (psycopg_pool)
  - Per-request connection lifecycle managed by app.py via get_connection()/release

Configuration via environment:
  DB_HOST       (default: 127.0.0.1)
  DB_PORT       (default: 5432)
  DB_USER       (required)
  DB_PASSWORD   (required)
  DB_NAME       (required)
  DB_POOL_MIN   (default: 2)
  DB_POOL_MAX   (default: 20)

Public API:
  get_connection()        → Database (wrapper) — caller MUST call .close() to return to pool
  IntegrityError          — psycopg.errors.IntegrityError, re-exported under sqlite3-compatible name
  init_pool()             — explicit pool initialization (optional; first call to get_connection lazy-inits)
  apply_schema(sql_path)  — execute a .sql file at startup
"""
import os
import re
import atexit
from typing import Any, Optional, Sequence

import psycopg
from psycopg.errors import IntegrityError as _PgIntegrityError
from psycopg_pool import ConnectionPool


# ── Compatibility re-export ─────────────────────────────────────────────
# Existing code does `except sqlite3.IntegrityError`. We re-export the
# psycopg equivalent under the same name so catch sites can be updated to
# `except db.IntegrityError` without learning two APIs.
IntegrityError = _PgIntegrityError


# ── Row class: dict + int-index access (mimics sqlite3.Row) ─────────────
import datetime as _dt


def _to_compat(value):
    """Convert Postgres-native values to their SQLite-string equivalents.

    SQLite stored timestamps as ISO-like text. The codebase compares row
    values to strings, JSON-serializes them, and writes them to log files.
    Returning a datetime here would break all those code paths.

    Conversions:
      datetime → 'YYYY-MM-DD HH:MM:SS' (matches datetime('now') output)
      date     → 'YYYY-MM-DD'

    Booleans pass through. Postgres BOOLEAN is mapped to int (0/1) for
    legacy `WHERE is_active=1` style comparisons in Python — at the SQL
    level we keep INTEGER columns (see schema.sql) to avoid touching every
    call site.
    """
    if isinstance(value, _dt.datetime):
        # Drop microseconds and timezone for output parity with SQLite
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, _dt.date):
        return value.strftime("%Y-%m-%d")
    return value


class CompatRow(dict):
    """Row supporting both row['name'] and row[0], like sqlite3.Row.

    Most code uses string keys, but legacy paths use [0] (e.g. checking
    `cur.execute(...).fetchone()[0]` for a single-column SELECT). We support
    both transparently.

    Values are normalized to SQLite-style scalars via _to_compat — most
    importantly datetimes become 'YYYY-MM-DD HH:MM:SS' strings so existing
    string-comparison and JSON-serialization code keeps working.
    """
    __slots__ = ("_values",)

    def __init__(self, values, keys):
        converted = tuple(_to_compat(v) for v in values)
        super().__init__(zip(keys, converted))
        self._values = converted

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _compat_row_factory(cursor):
    """psycopg row factory producing CompatRow instances."""
    desc = cursor.description
    if not desc:
        return lambda values: values
    keys = [d.name for d in desc]
    def make_row(values):
        return CompatRow(values, keys)
    return make_row


# ── SQL translation: SQLite-isms → Postgres ─────────────────────────────
# The codebase contains SQL written for SQLite. Rather than rewriting every
# query, we translate the small set of differences at execution time.
#
# Handled:
#   ?                                       → %s
#   datetime('now')                         → now()
#   strftime('%Y-%m-%d %H:%M:%f','now')     → now()
#   strftime('%Y-%m-%d %H:%M:%S','now')     → now()
#
# Not handled (would require call-site awareness — fix at call site):
#   INSERT OR IGNORE / INSERT OR REPLACE   (Postgres equivalent: ON CONFLICT)
#
# We respect single-quoted string literals: '?' inside a literal is NOT a
# placeholder. The regex captures literals as alternation, the substitution
# returns them unchanged.

# Match (a) single-quoted string literal (supporting '' escape), or (b) a ?
_LITERAL_OR_PLACEHOLDER_RE = re.compile(r"'(?:''|[^'])*'|\?")

# Match SQLite datetime calls — applied AFTER placeholder substitution since
# none of these contain ?. The regexes preserve string literals indirectly:
# datetime('now') and strftime(...) only appear in code-written SQL, not in
# user-controlled values, so a naive replace is safe in practice. We still
# anchor to avoid false matches in column names.
_DATETIME_NOW_RE = re.compile(r"\bdatetime\(\s*'now'\s*\)")
_STRFTIME_NOW_RE = re.compile(r"\bstrftime\(\s*'[^']*'\s*,\s*'now'\s*\)")


def _translate(sql: str) -> str:
    """Translate SQLite-flavored SQL to Postgres-flavored SQL."""
    # 1) ? → %s, preserving string literals
    def _repl(m):
        s = m.group(0)
        return s if s.startswith("'") else "%s"
    sql = _LITERAL_OR_PLACEHOLDER_RE.sub(_repl, sql)
    # 2) datetime('now') and strftime(..., 'now') → now()
    sql = _DATETIME_NOW_RE.sub("now()", sql)
    sql = _STRFTIME_NOW_RE.sub("now()", sql)
    return sql


# ── Connection pool (lazy singleton) ────────────────────────────────────
_pool: Optional[ConnectionPool] = None


def init_pool() -> ConnectionPool:
    """Initialize the global connection pool. Idempotent."""
    global _pool
    if _pool is not None:
        return _pool
    host = os.environ.get("DB_HOST", "127.0.0.1")
    port = int(os.environ.get("DB_PORT", "5432"))
    user = os.environ["DB_USER"]
    password = os.environ["DB_PASSWORD"]
    name = os.environ["DB_NAME"]
    pool_min = int(os.environ.get("DB_POOL_MIN", "2"))
    pool_max = int(os.environ.get("DB_POOL_MAX", "20"))
    conninfo = (
        f"host={host} port={port} user={user} "
        f"password={password} dbname={name}"
    )
    _pool = ConnectionPool(
        conninfo=conninfo,
        min_size=pool_min,
        max_size=pool_max,
        open=True,
    )
    return _pool


def _get_pool() -> ConnectionPool:
    return _pool if _pool is not None else init_pool()


def close_pool() -> None:
    """Close the connection pool and stop its background worker threads.

    Registered with atexit so short-lived CLI invocations (list-users,
    reset-password, etc.) exit promptly. Without this, psycopg_pool's
    maintenance threads keep the interpreter alive for ~10s after the
    command finishes and print a 'couldn't stop thread' warning. The
    long-running server path is unaffected — atexit only fires at actual
    process shutdown. Idempotent.
    """
    global _pool
    if _pool is None:
        return
    try:
        _pool.close()
    except Exception:
        pass
    finally:
        _pool = None


# Fire on interpreter shutdown for any process that imported this module
# (CLI commands, the server, test harnesses). No-op if the pool was never
# opened.
atexit.register(close_pool)


# ── Cursor wrapper (SQLite-compatible methods) ──────────────────────────
class Cursor:
    """Wraps a psycopg cursor with the subset of sqlite3.Cursor API used by app.py."""

    __slots__ = ("_cur",)

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


# ── Connection wrapper (SQLite-compatible methods) ──────────────────────
class Database:
    """SQLite-compatible facade over a pooled psycopg connection.

    Created via get_connection(). Caller MUST eventually call .close() to
    return the underlying connection to the pool. In app.py this is done
    by the Flask teardown_appcontext handler.

    Implements the subset of sqlite3.Connection used by the codebase:
      execute(sql, params)  → Cursor
      executescript(script) → None      (multi-statement DDL)
      commit()              → None
      rollback()            → None
      close()               → None      (returns connection to pool)
    """

    __slots__ = ("_conn",)

    def __init__(self, conn):
        self._conn = conn
        # SQLite's default behavior is to autocommit DDL but require explicit
        # commit for DML. Postgres autocommit=False matches the DML side.
        # Code paths that issue DDL call commit() explicitly already.
        self._conn.autocommit = False

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Cursor:
        cur = self._conn.cursor(row_factory=_compat_row_factory)
        cur.execute(_translate(sql), tuple(params) if params else None)
        return Cursor(cur)

    def executescript(self, script: str) -> None:
        """Execute a multi-statement SQL script (DDL bootstrap).

        psycopg's libpq layer accepts multiple statements in one execute()
        call. We translate first, then run in autocommit so individual
        CREATE TABLE / CREATE INDEX statements don't get rolled back as a
        batch on the first 'already exists' notice.
        """
        prior = self._conn.autocommit
        self._conn.autocommit = True
        try:
            with self._conn.cursor() as cur:
                cur.execute(_translate(script))
        finally:
            self._conn.autocommit = prior

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        """Return the underlying connection to the pool."""
        pool = _get_pool()
        try:
            pool.putconn(self._conn)
        except Exception:
            # If putconn fails for any reason, close the raw connection
            # to avoid leaking it.
            try:
                self._conn.close()
            except Exception:
                pass


# ── Public entry point ──────────────────────────────────────────────────
def get_connection() -> Database:
    """Get a wrapped Postgres connection. Caller must call .close() when done."""
    pool = _get_pool()
    return Database(pool.getconn())


def apply_schema(sql_path: str) -> None:
    """Apply a .sql file at startup. Idempotent if the file uses IF NOT EXISTS."""
    with open(sql_path, "r", encoding="utf-8") as f:
        script = f.read()
    db = get_connection()
    try:
        db.executescript(script)
        # The autocommit toggle inside executescript leaves a residual
        # transaction state. Clear it before returning to pool so put_back
        # does not emit a "rolling back returned connection" warning.
        try: db.rollback()
        except Exception: pass
    finally:
        db.close()
