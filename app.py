"""BlancoByte ClickHouse Console v4.0
Run:  python3 app.py  →  http://localhost:5000
"""
import gzip, json, logging, logging.handlers, os, re, shutil, subprocess, sys, threading, time, uuid
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, send_file
import zipfile, io, csv

APP_DIR    = Path(__file__).parent.resolve()
STATIC_DIR = APP_DIR / "static"
LOG_DIR        = APP_DIR / "logs"
LOG_GLOBAL_DIR = LOG_DIR / "global"
LOG_USERS_DIR  = LOG_DIR / "users"
LOG_GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
LOG_USERS_DIR.mkdir(parents=True, exist_ok=True)
# Legacy single-file location (kept for back-compat — see _migrate_legacy_logs)
LOG_FILE   = APP_DIR / "console.log"

# ─── Monthly log rotation ─────────────────────────────────────────────────
# All log files rotate at calendar-month boundaries. Layout:
#
#   logs/global/console-YYYY-MM.log         server log (current month, active)
#   logs/global/console-YYYY-MM.log.gz      server log (past month, gzipped)
#   logs/global/activity-YYYY-MM.log        UI activity log, ALL users
#   logs/global/activity-YYYY-MM.log.gz     past month, gzipped
#   logs/users/<username>/activity-YYYY-MM.log     per-user activity log
#   logs/users/<username>/activity-YYYY-MM.log.gz  per-user past month, gzipped
#
# Old months are gzipped and kept FOREVER. The active file always carries the
# current YYYY-MM in its name so writes are deterministic.

def _month_tag(ts=None):
    return time.strftime("%Y-%m", time.localtime(ts) if ts else time.localtime())

def _gzip_compress_and_remove(src_path: Path):
    """Compress src_path → src_path.gz, then delete the original. No-op if
    src is empty or the .gz already exists (won't double-archive)."""
    try:
        if not src_path.exists() or src_path.stat().st_size == 0:
            try: src_path.unlink(missing_ok=True)
            except Exception: pass
            return
        gz_path = src_path.with_suffix(src_path.suffix + ".gz")
        if gz_path.exists():
            return
        with open(src_path, "rb") as fin, gzip.open(gz_path, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout)
        src_path.unlink()
    except Exception as e:
        print(f"[log-rotate] failed to gzip {src_path}: {e}", file=sys.stderr)

def _archive_prior_months_in(directory: Path, prefix: str):
    """Find every <prefix>-YYYY-MM.log in `directory` that is NOT the current
    month and gzip it. Idempotent — safe to run repeatedly."""
    if not directory.exists(): return
    cur_tag = _month_tag()
    for p in directory.glob(f"{prefix}-*.log"):
        try:
            m = re.search(r"-(\d{4})-(\d{2})\.log$", p.name)
            if not m: continue
            tag = f"{m.group(1)}-{m.group(2)}"
            if tag != cur_tag:
                _gzip_compress_and_remove(p)
        except Exception:
            continue

def _archive_all_user_logs():
    """Walk every logs/users/<username>/ directory and archive prior-month
    activity logs found inside."""
    if not LOG_USERS_DIR.exists(): return
    for user_dir in LOG_USERS_DIR.iterdir():
        if user_dir.is_dir():
            _archive_prior_months_in(user_dir, "activity")

def _migrate_legacy_logs():
    """One-time migration from older single-file or single-folder layouts:
       - APP_DIR/console.log            → logs/global/console-<mtime-month>.log
       - APP_DIR/console-activity.log   → logs/global/activity-<mtime-month>.log
       - logs/console-YYYY-MM.log[.gz]  → logs/global/console-YYYY-MM.log[.gz]
       - logs/console-activity-YYYY-MM.log[.gz] → logs/global/activity-YYYY-MM.log[.gz]
    """
    # Step 1: app-root legacy files
    for legacy_name, new_basename in [
        ("console.log",          "console"),
        ("console-activity.log", "activity"),
    ]:
        legacy = APP_DIR / legacy_name
        if not legacy.exists() or legacy.stat().st_size == 0:
            continue
        tag = _month_tag(legacy.stat().st_mtime)
        target = LOG_GLOBAL_DIR / f"{new_basename}-{tag}.log"
        try:
            if target.exists():
                with open(legacy, "rb") as fin, open(target, "ab") as fout:
                    shutil.copyfileobj(fin, fout)
                legacy.unlink()
            else:
                shutil.move(str(legacy), str(target))
        except Exception as e:
            print(f"[log-migrate] {legacy_name}: {e}", file=sys.stderr)

    # Step 2: flat logs/ layout from v3.3 → logs/global/
    # IMPORTANT: rename console-activity-* → activity-* FIRST so the more
    # generic console-* glob below doesn't grab them with the old name.
    for p in LOG_DIR.glob("console-activity-*.log*"):
        try:
            new_name = p.name.replace("console-activity-", "activity-", 1)
            shutil.move(str(p), str(LOG_GLOBAL_DIR / new_name))
        except Exception: pass
    # Now the truly-server logs (console-YYYY-MM.log only, NOT console-activity-*)
    for p in LOG_DIR.glob("console-2*.log"):     # YYYY starts with 2 (2020+)
        try: shutil.move(str(p), str(LOG_GLOBAL_DIR / p.name))
        except Exception: pass
    for p in LOG_DIR.glob("console-2*.log.gz"):
        try: shutil.move(str(p), str(LOG_GLOBAL_DIR / p.name))
        except Exception: pass

_migrate_legacy_logs()
_archive_prior_months_in(LOG_GLOBAL_DIR, "console")
_archive_prior_months_in(LOG_GLOBAL_DIR, "activity")
_archive_all_user_logs()

def _server_log_path():
    return LOG_GLOBAL_DIR / f"console-{_month_tag()}.log"

def _global_activity_log_path():
    return LOG_GLOBAL_DIR / f"activity-{_month_tag()}.log"

def _user_activity_log_path(username: str):
    """Per-user activity log path. The username is validated by the caller
    (must already match [A-Za-z0-9._-]{1,32}) so it is filesystem-safe."""
    if not username: return None
    user_dir = LOG_USERS_DIR / username
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / f"activity-{_month_tag()}.log"

class _MonthlyRotatingHandler(logging.Handler):
    """Custom handler that always writes to the current-month server log file.
    On the first emit after a month boundary it gzips the prior month and
    opens a fresh file. Failures fall back to stderr so logging never breaks
    the request path."""
    def __init__(self):
        super().__init__()
        self._lock_obj = threading.Lock()
        self._current_month = _month_tag()
        self._stream = None
        self._open_stream()
    def _open_stream(self):
        try:
            self._stream = open(_server_log_path(), "a", encoding="utf-8")
        except Exception as e:
            print(f"[log] cannot open {_server_log_path()}: {e}", file=sys.stderr)
            self._stream = None
    def emit(self, record):
        try:
            with self._lock_obj:
                tag = _month_tag()
                if tag != self._current_month:
                    if self._stream:
                        try: self._stream.close()
                        except Exception: pass
                    _archive_prior_months_in(LOG_GLOBAL_DIR, "console")
                    self._current_month = tag
                    self._open_stream()
                if self._stream:
                    self._stream.write(self.format(record) + "\n")
                    self._stream.flush()
        except Exception:
            self.handleError(record)
    def close(self):
        try:
            if self._stream: self._stream.close()
        except Exception: pass
        super().close()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        _MonthlyRotatingHandler(),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("ch_console")

app = Flask(__name__, static_folder=str(STATIC_DIR))

# Max rows a single interactive query returns to the grid. Caps huge SELECTs
# (e.g. 60M-row tables with no LIMIT) at the engine so they return the first N
# instead of trying to materialize the whole result. Override via env.
RESULT_ROW_CAP = int(os.environ.get("RESULT_ROW_CAP", "10000"))

# ═══════════════════════════════════════════════════════════════════════════
# IDENTITY  ── multi-user foundation (Step 1)
#   - SQLite-backed users, sessions and audit log
#   - PBKDF2-SHA256 password hashing (salted)
#   - Session cookie auth gate on /api/* (server-enforced, not client-trusted)
#   - One-time migration from console-users.json on first run
# ═══════════════════════════════════════════════════════════════════════════
import hashlib, secrets, hmac, base64
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import g

# v4 (Postgres backend) — db module replaces direct sqlite3 usage. Provides
# get_connection() returning a SQLite-compatible facade over psycopg3, and
# IntegrityError re-exported for legacy except handlers.
import db as _dbmod
IntegrityError = _dbmod.IntegrityError

# Phase 2 (Redis sessions) — session_store replaces the Postgres `sessions`
# table. Symmetric to db.py: it speaks only Redis. Session functions below
# (create_session/get_session_user/etc.) delegate their storage primitives
# to it; audit logging and sessions.log stay here in app.py.
import session_store

# Phase 4z-k: LDAP. We import lazily inside the LDAP code path so that an
# installation without LDAP configured doesn't hard-fail at startup if the
# ldap3 package happens to be missing — useful in stripped-down test
# images. The actual functions below import at call time and surface a
# clear error message if the package isn't installed.
try:
    import ldap3 as _ldap3
    _LDAP_AVAILABLE = True
except ImportError:
    _LDAP_AVAILABLE = False

try:
    from croniter import croniter as _croniter
    _CRONITER_AVAILABLE = True
except ImportError:
    _CRONITER_AVAILABLE = False

# Phase 4 (bug-fix) — job_store moves async job state (query Run/Run All and
# the generic script-runner) out of in-process dicts and into Redis. Under
# gunicorn's multiple workers an in-process dict is per-worker, so the poll
# request frequently hit a worker that did not have the job → "not found".
# job_store is shared by every worker. Reuses session_store's Redis client.
import job_store
# Login brute-force protection (Redis-backed failure counters). Reuses
# session_store's Redis client; fail-open if Redis is unreachable.
import rate_limit

DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# ── Legacy paths kept for non-DB state ───────────────────────────────────
# Application data is now in Postgres (see SCHEMA_MAPPING.md). The data/
# directory survives ONLY for install-level secrets and holds exactly one
# subfolder:
#     data/global/   master.key, instance.id, license.lic
# There are NO per-user folders under data/ in v4. Per-user state that is
# still on the filesystem — the activity/audit logs — lives under logs/:
#     logs/global/              server + global activity logs
#     logs/users/<username>/    that user's activity (audit) logs
GLOBAL_DIR = DATA_DIR / "global"
# Back-compat constants — DB_PATH and GLOBAL_DB_PATH are no longer used for
# anything DB-related, but a handful of older non-DB code paths reference
# GLOBAL_DIR for secret files.
GLOBAL_DB_PATH = GLOBAL_DIR / "global.db"   # vestigial; only referenced by removed code
DB_PATH = GLOBAL_DB_PATH                     # vestigial

def _ensure_dir(path: Path, mode=0o700):
    path.mkdir(parents=True, exist_ok=True)
    try: os.chmod(path, mode)
    except: pass

def _user_dir(username: str) -> Path:
    """The one per-user directory in v4: logs/users/<username>/, which holds
    that user's activity (audit) log files. (Earlier versions put a per-user
    folder under data/ — that was wrong for v4 and produced empty, confusing
    data/<username>/ directories; the audit logs were always under logs/.)"""
    return LOG_USERS_DIR / username

def _user_db_path(username: str) -> Path:
    """DEPRECATED — kept only because a few legacy code paths still call it.
    In v4 (Postgres) there is no per-user .db file. Returns a path that
    never exists, so any .exists() check fails safely.
    """
    return _user_dir(username) / f"{username}.db"

SESSION_COOKIE_NAME = "ch_session"
SESSION_TTL_DAYS    = int(os.environ.get("SESSION_TTL_DAYS", "7"))
ROLES = ("admin", "developer", "monitoring", "readonly")

# ── DB connections (per-request) ──────────────────────────────────────────
# v4: backed by a Postgres connection pool. The three helpers below return
# the SAME pooled connection within one request (cached on Flask g) so
# existing patterns like:
#     db().execute(...); db().commit()
# continue to work without holding a connection across the .commit().
#
# Per-user isolation, formerly enforced by separate .db files, is now
# enforced by user_id columns. Each per-user query MUST include the user_id
# of the caller in WHERE / INSERT. The db_user() helper resolves the
# username argument to a user_id and exposes it via the returned wrapper's
# .user_id attribute, so call sites do not need a second lookup.

class _ScopedDb:
    """Thin proxy that exposes both the shared per-request connection and
    the resolved user_id for per-user table access. Delegates all method
    calls to the underlying db.Database, so existing code:
        db_user(uname).execute("...", (...)); db_user(uname).commit()
    keeps working. New code that needs the user_id reads .user_id.
    """
    __slots__ = ("_conn", "user_id", "username")
    def __init__(self, conn, user_id, username):
        self._conn = conn
        self.user_id = user_id
        self.username = username
    def execute(self, sql, params=()): return self._conn.execute(sql, params)
    def commit(self): return self._conn.commit()
    def rollback(self): return self._conn.rollback()


def db_global():
    """Return the per-request pooled Postgres connection (lazy init)."""
    if "db_global" not in g:
        g.db_global = _dbmod.get_connection()
    return g.db_global


def db_user(username_or_id):
    """Return a _ScopedDb wrapping the per-request connection + the resolved
    user_id for per-user table operations (query_history, query_favorites,
    user_credentials). Returns None if the user can't be found.
    Accepts username (str) or user_id (int).
    """
    if isinstance(username_or_id, int):
        row = db_global().execute(
            "SELECT id, username FROM users WHERE id=?",
            (username_or_id,)).fetchone()
        if not row: return None
        uid, uname = row["id"], row["username"]
    else:
        uname = str(username_or_id)
        row = db_global().execute(
            "SELECT id FROM users WHERE username=?", (uname,)).fetchone()
        if not row: return None
        uid = row["id"]
    # Cache so a single request hitting db_user(uname) multiple times
    # doesn't repeat the lookup.
    if not hasattr(g, "_user_scopes") or g._user_scopes is None:
        g._user_scopes = {}
    if uname not in g._user_scopes:
        g._user_scopes[uname] = _ScopedDb(db_global(), uid, uname)
    return g._user_scopes[uname]


# Back-compat alias: existing code can keep calling db() for global tables.
def db():
    return db_global()


@app.teardown_appcontext
def _close_db(exc=None):
    d = g.pop("db_global", None)
    if d is not None:
        # If the request failed (or an IntegrityError left the connection in
        # an aborted transaction state), roll back before returning to pool
        # so the next request gets a clean connection.
        try:
            if exc is not None:
                d.rollback()
            else:
                # Best-effort: rollback any uncommitted state. Idempotent —
                # if there's nothing to roll back this is a no-op.
                try: d.rollback()
                except Exception: pass
        except Exception: pass
        try: d.close()
        except Exception: pass
    g.pop("_user_scopes", None)
    # Legacy: very old code path
    d = g.pop("db", None)
    if d is not None and d is not getattr(g, "db_global", None):
        try: d.close()
        except Exception: pass


# ── Schema bootstrap (Postgres) ───────────────────────────────────────────
# The full schema is in schema.sql and applied at startup via init_db().
# Per-user DB files no longer exist — all per-user tables (query_history,
# query_favorites, user_credentials) live in the shared Postgres DB with a
# user_id column.

# Path to the DDL file shipped alongside app.py.
SCHEMA_SQL_PATH = APP_DIR / "schema.sql"

def _init_user_db(username: str):
    """Vestigial. Per-user DBs no longer exist. We still ensure the per-user
    log directory exists (logs are still on the filesystem in v4).
    """
    try:
        _ensure_dir(_user_dir(username))
    except Exception:
        pass

def init_db():
    """Apply the schema to Postgres. Idempotent. Replaces the previous
    SQLite bootstrap and the legacy data/app.db migration path.

    Side effects:
      - Postgres connection pool is lazily initialized on first DB access.
      - schema.sql is applied (all CREATE TABLE / INDEX use IF NOT EXISTS).
      - data/global/ directory is created for non-DB install secrets
        (master.key, instance.id, license.lic).
    """
    _ensure_dir(GLOBAL_DIR)
    if not SCHEMA_SQL_PATH.exists():
        logger.error(f"schema.sql not found at {SCHEMA_SQL_PATH} — cannot bootstrap DB")
        raise RuntimeError("schema.sql missing")
    try:
        _dbmod.apply_schema(str(SCHEMA_SQL_PATH))
        logger.info(f"Postgres schema applied from {SCHEMA_SQL_PATH.name}")
    except Exception as e:
        logger.error(f"Schema apply failed: {e}")
        raise


# ── Password hashing (PBKDF2-SHA256, 200k rounds, 16-byte salt) ───────────
def hash_password(plain: str) -> str:
    if not isinstance(plain, str) or not plain:
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_bytes(16)
    key  = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, 200_000)
    return f"pbkdf2_sha256$200000${base64.b64encode(salt).decode()}${base64.b64encode(key).decode()}"

def verify_password(plain: str, stored: str) -> bool:
    if not stored or not plain: return False
    try:
        # New format: pbkdf2_sha256$<rounds>$<b64salt>$<b64hash>
        if stored.startswith("pbkdf2_sha256$"):
            _, rounds, salt_b64, key_b64 = stored.split("$")
            key = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"),
                                      base64.b64decode(salt_b64), int(rounds))
            return hmac.compare_digest(key, base64.b64decode(key_b64))
        # Legacy format from console-users.json: plain SHA256 (insecure, supported for migration only)
        return hmac.compare_digest(hashlib.sha256(plain.encode()).hexdigest(), stored)
    except Exception:
        return False

# ── Sessions ──────────────────────────────────────────────────────────────
def create_session(user_id: int, ip: str = "", ua: str = "") -> tuple:
    """Create a new session. Single-session-per-user is enforced for admin,
    developer, and readonly roles. The monitoring role is exempt — monitoring
    operators routinely keep multiple dashboards open in different browsers
    or tabs, so any new login leaves existing monitoring sessions intact.
    When supersession does occur, the displaced session(s) are recorded as
    "Session Superseded" audit events so an investigator can see the kick."""
    # Look up identity once. Phase 2: username/role/email are denormalized
    # into the Redis session hash so the auth-gate hot path never JOINs
    # back to Postgres. role also decides the single-session enforcement
    # policy below.
    urow = db().execute("SELECT username, role, email FROM users WHERE id=?",
                        (user_id,)).fetchone()
    uname = urow["username"] if urow else f"id={user_id}"
    user_role = urow["role"] if urow else ""
    user_email = (urow["email"] if urow else "") or ""
    enforce_single = user_role != "monitoring"
    superseded = []
    if enforce_single:
        # Phase 2: sessions live in Redis. Read the displaced sessions
        # (needed for the audit detail below) then revoke them all.
        superseded = session_store.list_user_sessions(user_id)
        if superseded:
            session_store.del_user_tokens(user_id)
    token = secrets.token_urlsafe(32)
    exp_dt = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    exp = exp_dt.strftime("%Y-%m-%d %H:%M:%S")
    created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # Phase 2: write the session to Redis (hash + per-user index) with a
    # TTL matching expires_at. No Postgres write, no commit.
    session_store.put_session(token, {
        "user_id":    user_id,
        "username":   uname,
        "role":       user_role,
        "email":      user_email,
        "created_at": created,
        "expires_at": exp,
        "ip":         ip,
        "user_agent": ua[:300],
    }, SESSION_TTL_DAYS * 86400)
    # Write the supersession audit (best-effort). The audit trail itself
    # stays in Postgres + the activity log files — only session state moved.
    if superseded:
        try:
            for s in superseded:
                # Forge the audit event with the supersed user as the actor
                from datetime import datetime as _dt
                ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
                ip_old = s["ip"] or ""
                ua_old = (s["user_agent"] or "")[:120]
                detail = (
                    f"single-session-per-user enforced; previous session displaced\n"
                    f"prior ip={ip_old}  prior ua={ua_old}\n"
                    f"prior created_at={s['created_at']}\n"
                    f"new ip={ip}  new ua={(ua or '')[:120]}"
                )
                import json as _json
                entry = {
                    "ts": ts, "console_user": uname,
                    "console_role": user_role,
                    "panel": "auth", "action": "Session Superseded",
                    "detail": detail, "conn_host": "", "conn_user": "",
                    "ip": ip or "", "result": "ok",
                }
                line = _json.dumps(entry, ensure_ascii=False) + "\n"
                try:
                    with open(_activity_log_writer_open_global(), "a", encoding="utf-8") as f: f.write(line)
                except Exception: pass
                if uname and re.match(r"^[A-Za-z0-9._-]{1,32}$", uname):
                    try:
                        ul = _user_activity_log_path(uname)
                        if ul:
                            with open(ul, "a", encoding="utf-8") as f: f.write(line)
                    except Exception: pass
                try:
                    db_global().execute("""
                        INSERT INTO audit_events(user_id,username,role,action,panel,detail,ip,result)
                        VALUES(?,?,?,?,?,?,?,?)""",
                        (user_id, uname, user_role,
                         "Session Superseded", "auth", detail, ip or "", "ok"))
                    db_global().commit()
                except Exception: pass
        except Exception as e:
            logger.warning(f"Session-supersede audit failed: {e}")
    # Always refresh sessions.log snapshot on any session change
    try: _refresh_sessions_log()
    except Exception: pass
    return token, int(exp_dt.timestamp())

def get_session_user(token: str):
    """Resolve a session cookie to the signed-in user. Phase 2: reads from
    Redis only — username/role/email are denormalized into the session
    hash, so the auth-gate hot path never touches Postgres.

    is_active is NOT re-checked here. Instead, deactivating a user or
    changing their role explicitly revokes their sessions (see the
    console_users_* paths), so the existence of a live session already
    implies an active user with the stored role.

    Fails closed: any Redis error → None (treated as unauthenticated)
    rather than raising. A Redis outage therefore logs everyone out
    instead of letting requests through or turning the auth gate into a
    500 — the safe direction."""
    if not token: return None
    try:
        s = session_store.get_session(token)
    except Exception as e:
        logger.warning(f"session lookup failed (Redis) — treating as unauthenticated: {e}")
        return None
    if not s: return None
    # TTL already removes expired keys; this explicit check is a guard
    # against clock skew between the app and Redis hosts.
    if s.get("expires_at") and s["expires_at"] < datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"):
        return None
    try:
        uid = int(s["user_id"])
    except (KeyError, ValueError, TypeError):
        return None
    return {"id": uid, "username": s.get("username", ""),
            "email": s.get("email", ""), "role": s.get("role", "")}

def delete_session(token: str):
    if not token: return
    try:
        session_store.del_session(token)
    except Exception as e:
        logger.warning(f"session delete failed (Redis): {e}")
    try: _refresh_sessions_log()
    except Exception: pass

def cleanup_expired_sessions():
    """Phase 2 no-op. Redis key TTL expires session hashes automatically,
    and the per-user index is pruned lazily on every read (see
    session_store._prune_user_index). Kept as a stub so the startup call
    site and any external callers remain valid."""
    return

def _refresh_sessions_log():
    """Write the current set of active sessions to logs/global/sessions.log.
    This file is overwritten (NOT appended) on every session change so an
    operator with shell access can `cat logs/global/sessions.log` and see who
    is signed in right now, without booting the UI. The file is JSON-lines:
    one active session per line."""
    try:
        import json as _json
        from datetime import datetime as _dt
        # Phase 2: enumerate live sessions from Redis instead of the
        # Postgres `sessions` table. Same JSON-lines output format so an
        # operator's `cat logs/global/sessions.log` keeps working.
        rows = sorted(
            session_store.scan_sessions(),
            key=lambda s: s.get("created_at", ""), reverse=True,
        )
        path = LOG_GLOBAL_DIR / "sessions.log"
        tmp  = LOG_GLOBAL_DIR / "sessions.log.new"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"# active sessions snapshot at {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# count={len(rows)}\n")
            for r in rows:
                entry = {
                    "token_prefix": (r.get("token") or "")[:12],
                    "username":     r.get("username", ""),
                    "role":         r.get("role", ""),
                    "email":        r.get("email") or "",
                    "created_at":   r.get("created_at", ""),
                    "expires_at":   r.get("expires_at", ""),
                    "ip":           r.get("ip") or "",
                    "user_agent":   (r.get("user_agent") or "")[:200],
                }
                f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        tmp.replace(path)
    except Exception as e:
        logger.warning(f"sessions.log refresh failed: {e}")

# ── Audit logging to DB (in addition to existing file log) ────────────────
# Constant advisory-lock key so concurrent audit() inserts serialize and the
# hash chain stays strictly linear (no forks). Audit volume is low, so holding
# this per-insert is negligible.
_AUDIT_CHAIN_LOCK = 0x41554449  # 'AUDI'

def _audit_entry_hash(prev_hash: str, fields: dict) -> str:
    """Keyed hash over (prev_hash + canonical JSON of the row's fields). Uses
    HMAC keyed with the server master key, so a database-only attacker (a
    malicious DBA, a stolen DB credential, or SQL injection into the audit
    table) cannot forge a valid hash to cover an edit — they would need the
    master key, which never lives in the database. Stable key ordering +
    compact separators make the value reproducible by the verifier."""
    import json as _json, hmac as _hmac
    payload = (prev_hash or "") + "\n" + _json.dumps(
        fields, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str)
    key = hashlib.sha256(b"audit-chain-v1|" + (MASTER_KEY or b"")).digest()
    return _hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def audit(action: str, panel: str = "", detail: str = "",
          conn_host: str = "", conn_port: str = "", conn_user: str = "", result: str = "ok"):
    """Write a structured audit event. Triple-writes:
      1. Postgres audit_events table                       — DB master (used by admin UI)
      2. logs/global/activity-YYYY-MM.log                  — global text log (pretty block)
      3. logs/users/<username>/activity-YYYY-MM.log        — per-user text log (pretty block)
    The DB write is the source of truth; failures on the file logs are
    recorded as WARNING but do not fail the request.

    (v4 change: the per-user DB mirror was dropped — single master in
    Postgres, user_id-filtered on display.)
    """
    try:
        u = getattr(g, "user", None) if g else None
        uid       = u["id"]       if u else None
        uname     = u["username"] if u else None
        urole     = u["role"]     if u else None
        ip        = request.remote_addr or ""
        det       = (detail or "")[:1_000_000]
        port_str  = str(conn_port) if conn_port else ""
        # (1) DB master — written as a tamper-evident hash chain. A transaction
        # advisory lock serializes inserts so the chain is linear; each row is
        # linked to the previous row's entry_hash and stores its own. Any later
        # edit/delete/reorder is detectable via /api/audit/verify.
        conn = db_global()
        conn.execute("SELECT pg_advisory_xact_lock(?)", (_AUDIT_CHAIN_LOCK,))
        tip = conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev_hash = (tip["entry_hash"] if tip and tip["entry_hash"] else "")
        ts_dt  = datetime.now(timezone.utc)
        ts_iso = ts_dt.astimezone(timezone.utc).isoformat()
        chain_fields = {
            "ts": ts_iso, "user_id": uid, "username": uname, "role": urole,
            "action": action, "panel": panel, "detail": det,
            "conn_host": conn_host, "conn_port": port_str,
            "conn_user": conn_user, "ip": ip, "result": result,
        }
        entry_hash = _audit_entry_hash(prev_hash, chain_fields)
        conn.execute("""
            INSERT INTO audit_events(ts,user_id,username,role,action,panel,detail,
                                      conn_host,conn_port,conn_user,ip,result,
                                      prev_hash,entry_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts_dt, uid, uname, urole, action, panel, det,
             conn_host, port_str, conn_user, ip, result,
             prev_hash, entry_hash))
        conn.commit()
        # (Per-user audit DB mirror removed in v4: single master in Postgres,
        # filtered by user_id when displayed in the admin/audit UI.)
        # (3) + (4) Text log files — global always, per-user when known
        # Format: JSON-lines, one event per line. Compact for storage and
        # trivially machine-parseable; the CLI's read-log subcommand renders
        # the pretty block format on display.
        try:
            import json as _json
            from datetime import datetime as _datetime
            ts = _datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            host_field = f"{conn_host}:{conn_port}" if conn_host and conn_port else (conn_host or "")
            entry = {
                "ts": ts,
                "console_user": uname or "",
                "console_role": urole or "",
                "panel": panel,
                "action": action,
                "detail": det,
                "conn_host": host_field,
                "conn_user": conn_user,
                "ip": ip,
                "result": result,
            }
            line = _json.dumps(entry, ensure_ascii=False) + "\n"
            # Global text log
            try:
                with open(_activity_log_writer_open_global(), "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as e:
                logger.warning(f"global text-log write failed: {e}")
            # Per-user text log
            if uname and re.match(r"^[A-Za-z0-9._-]{1,32}$", uname):
                try:
                    user_log = _user_activity_log_path(uname)
                    if user_log:
                        with open(user_log, "a", encoding="utf-8") as f:
                            f.write(line)
                except Exception as e:
                    logger.warning(f"per-user text-log write failed for {uname}: {e}")
        except Exception as e:
            logger.warning(f"text-log audit write failed: {e}")
    except Exception as e:
        logger.warning(f"audit write failed: {e}")

# ── Auth gate (runs before every request) ─────────────────────────────────
# Public paths: served without authentication.
PUBLIC_PATH_PREFIXES = ("/static/", "/api/console/login", "/api/console/me", "/api/auth/", "/health", "/api/license/status")
PUBLIC_PATHS_EXACT   = {"/login", "/favicon.ico", "/health", "/healthz", "/readyz", "/metrics"}

@app.before_request
def _auth_gate():
    p = request.path
    # Allow public assets and the login endpoints
    if p in PUBLIC_PATHS_EXACT: return
    if any(p.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES): return

    token = request.cookies.get(SESSION_COOKIE_NAME)
    user  = get_session_user(token) if token else None

    if user is None:
        # Index page → bounce to /login. API → 401.
        if p == "/" or p == "":
            return redirect_to_login()
        if p.startswith("/api/"):
            return jsonify({"error": "auth_required", "code": "AUTH_REQUIRED"}), 401
        return redirect_to_login()

    g.user  = user
    g.token = token

# ── RBAC gate — URL-pattern → required role ───────────────────────────────
# Runs after auth_gate. Maps prefixes to allowed roles. First match wins.
# Anything not matched here is allowed for any authenticated user.
RBAC_RULES = [
    # (path_prefix or exact path, allowed roles tuple)
    # Admin-only
    ("/api/console/users",         ("admin",)),
    ("/api/admin",                 ("admin",)),
    ("/api/connections",           ("admin","developer","monitoring","readonly")),  # GET own/list — write below
    ("/api/backup",                ("admin",)),
    ("/api/pitr",                  ("admin",)),
    ("/api/branch/create",         ("admin",)),
    ("/api/branch/drop",           ("admin",)),
    ("/api/branch",                ("admin",)),
    ("/api/profiler",              ("admin",)),
    ("/api/log/read",              ("admin",)),
    ("/api/audit",                 ("admin",)),
    ("/api/security/user-activity",("admin",)),
    ("/api/security/grants",       ("admin",)),
    # User Cost exposes per-user spending/behaviour, so it is scoped to the
    # roles that analyse the cluster (admin + monitoring), not to the roles
    # being analysed (developer / readonly). Sidebar visibility mirrors this
    # via hidden_panels below.
    ("/api/cost/user-breakdown",   ("admin","monitoring")),
    ("/api/cost/user-trend",       ("admin","monitoring")),
    ("/api/cost/node-user-activity",("admin","monitoring")),
    ("/api/security/schema-drift", ("admin","developer","monitoring","readonly")),
    ("/api/storage/table-activity",("admin","developer","monitoring","readonly")),
    ("/api/compliance/export-pack",("admin",)),
    ("/api/activity/clear",        ("admin",)),
    # Mutations / writes
    ("/api/query/run",             ("admin","developer")),
    ("/api/query/page",            ("admin","developer")),
    ("/api/query/snapshot",        ("admin","developer")),
    ("/api/query/snapshot/page",   ("admin","developer")),
    ("/api/query/cancel",          ("admin","developer")),
    ("/api/query/kill",            ("admin","developer")),
    ("/api/monitor/kill",          ("admin","developer")),
    ("/api/mutations/kill",        ("admin","developer")),
    ("/api/dictionaries/reload",   ("admin","developer")),
    ("/api/dictionaries/reload-all",("admin","developer")),
    ("/api/mv/drop",                ("admin","developer")),
    ("/api/mv/refresh",             ("admin","developer")),
    # Cluster Health is intentionally NOT a developer feature — developers
    # focus on writing queries, while ops/SRE-type users (monitoring,
    # readonly) and admins want visibility into replication, distributed
    # queues, and ZK reachability.
    ("/api/cluster/health",         ("admin","developer","monitoring","readonly")),
    ("/api/queries",                ("admin","developer","monitoring","readonly")),
    ("/api/health/dashboard",       ("admin","developer","monitoring","readonly")),
    ("/api/health/history",         ("admin","developer","monitoring","readonly")),
    # SIEM forwarding — purely an admin / security-engineer concern.
    # Single /api/siem prefix covers every current and future SIEM
    # endpoint (destinations list, CRUD, test, log) via the longest-
    # prefix matcher below. Non-admin roles get 403 across the board.
    ("/api/siem",                   ("admin",)),
    # LDAP / Active Directory administration — admin only.
    ("/api/ldap",                   ("admin",)),
    ("/api/ttl/force",             ("admin","developer")),
    ("/api/dbusers",               ("admin",)),
    ("/api/users",                 ("admin",)),
    # Read-only paths default to all authenticated users
]
# Methods that are inherently mutating; for paths not in RBAC_RULES,
# any non-GET request still requires at least 'developer'.
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
READONLY_FALLBACK_DEVELOPER_NEEDED = False  # set True if you want stricter default

@app.before_request
def _rbac_gate():
    p = request.path
    u = getattr(g, "user", None)
    if u is None: return  # auth_gate has already handled (public path or 401)
    role = u["role"]
    # Match longest-prefix rule first
    rule_roles = None
    matched_len = -1
    for prefix, roles in RBAC_RULES:
        if p == prefix or p.startswith(prefix + "/") or p.startswith(prefix):
            if len(prefix) > matched_len:
                matched_len = len(prefix); rule_roles = roles
    if rule_roles is not None:
        if role not in rule_roles:
            return jsonify({"error":"forbidden","required_roles":list(rule_roles),"your_role":role}), 403
    # No matching rule → allow. (Most "read" endpoints in this app use POST
    # because they pass connection params in the body, so we cannot use HTTP
    # method as a heuristic. Mutations must be enumerated in RBAC_RULES.)

def redirect_to_login():
    from flask import redirect
    return redirect("/login")

# ── Role decorator ────────────────────────────────────────────────────────
def require_role(*roles):
    """@require_role('admin') → 403 unless g.user.role in roles."""
    def deco(fn):
        @wraps(fn)
        def wrapped(*a, **kw):
            u = getattr(g, "user", None)
            if not u or u["role"] not in roles:
                return jsonify({"error": "forbidden", "required_roles": list(roles)}), 403
            return fn(*a, **kw)
        return wrapped
    return deco

# ── One-time migration from console-users.json (legacy) ───────────────────
def migrate_legacy_users():
    """If users table is empty, import from console-users.json. Legacy SHA256
    hashes continue to work via verify_password.

    v4 note: INSERT OR IGNORE was translated to INSERT ... ON CONFLICT DO NOTHING
    for Postgres.
    """
    conn = _dbmod.get_connection()
    try:
        cur = conn.execute("SELECT count(*) FROM users").fetchone()
        if cur[0] > 0:
            return
        legacy = APP_DIR / "console-users.json"
        if legacy.exists():
            try:
                data = json.loads(legacy.read_text())
                for u in data.get("users", []):
                    role = u.get("role", "readonly")
                    if role not in ROLES: role = "readonly"
                    conn.execute(
                        "INSERT INTO users(username,email,password_hash,role) "
                        "VALUES(?,?,?,?) ON CONFLICT(username) DO NOTHING",
                        (u["username"], "", u["password_hash"], role))
                conn.commit()
                logger.info(f"Migrated {len(data.get('users',[]))} users from {legacy.name} to Postgres")
            except Exception as e:
                logger.warning(f"Legacy user migration failed: {e}")
                try: conn.rollback()
                except Exception: pass
        # First-run bootstrap: if no users exist yet, seed a default admin
        # (admin / admin123) so a fresh Community install can log in
        # immediately. The password is stored using the same pbkdf2 hash as
        # the CLI, and the operator is told (loudly) to change it at once.
        # Set CHC_NO_DEFAULT_ADMIN=1 to skip this and create the first admin
        # manually via `python app.py create-user ...`.
        cur2 = conn.execute("SELECT count(*) FROM users").fetchone()
        if cur2[0] == 0:
            if os.environ.get("CHC_NO_DEFAULT_ADMIN") == "1":
                logger.warning("="*60)
                logger.warning("No users exist. Create the first admin with:")
                logger.warning(f"  python {Path(__file__).name} create-user <username> --role admin --password '<password>'")
                logger.warning("="*60)
            else:
                try:
                    conn.execute("INSERT INTO users(username,email,password_hash,role) VALUES(?,?,?,?)",
                                 ("admin", "", hash_password("admin123"), "admin"))
                    conn.commit()
                    try: _init_user_db("admin")
                    except Exception: pass
                    logger.warning("="*60)
                    logger.warning("Seeded default administrator:  admin / admin123")
                    logger.warning("CHANGE THIS PASSWORD IMMEDIATELY after first login")
                    logger.warning("(My Profile -> account & password).")
                    logger.warning("="*60)
                except Exception as e:
                    try: conn.rollback()
                    except Exception: pass
                    logger.warning(f"Default admin seeding failed: {e}")
    finally:
        # A bare SELECT under autocommit=False leaves the connection in
        # INTRANS state. Roll back any read-only txn before put-back so the
        # pool does not log "rolling back returned connection".
        try: conn.rollback()
        except Exception: pass
        conn.close()

# ── Initialise DB on import (so workers/CLI both get a ready DB) ──────────
init_db()
migrate_legacy_users()
cleanup_expired_sessions()

# ═══════════════════════════════════════════════════════════════════════════
# LICENSING  (Step 5)  — offline RSA-signed JWT-style token
#   Community Edition: no license file is required or shipped. The server
#   always runs in COMMUNITY mode (max_users=3). An enterprise license file,
#   if present, is verified with public-key crypto and lifts the limits.
# ═══════════════════════════════════════════════════════════════════════════
LICENSE_FILE    = Path(os.environ.get("LICENSE_FILE", GLOBAL_DIR / "license.lic"))
PUBLIC_KEY_FILE = APP_DIR / "public_key.pem"
INSTANCE_ID_FILE = GLOBAL_DIR / "instance.id"
COMMUNITY_LIMITS = {"max_users": 3, "features": ["core"]}
LICENSE = None  # populated below

# ── Instance fingerprint — binds licenses to this specific install ────────
# Combines: (a) a long random secret stored on this server's persistent disk,
#           (b) host machine-id when available.
# If the .lic has a "fingerprint" field, it MUST match this value.
def compute_instance_fingerprint() -> str:
    parts = []
    # (a) per-install secret — generated on first run, lives in data/instance.id
    if not INSTANCE_ID_FILE.exists():
        INSTANCE_ID_FILE.write_text(secrets.token_hex(32))
        try: os.chmod(INSTANCE_ID_FILE, 0o600)
        except Exception: pass
    parts.append(INSTANCE_ID_FILE.read_text().strip())
    # (b) host machine-id (Linux). Optional — not all OSes have it.
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            mid = Path(p).read_text().strip()
            if mid: parts.append("host:"+mid); break
        except Exception: pass
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]

INSTANCE_FINGERPRINT = compute_instance_fingerprint()

def _verify_license_token(token: str, pubkey_pem: bytes) -> dict:
    """RS256 JWT-style: header.payload.sig — verify with provided RSA public key."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    parts = token.strip().split(".")
    if len(parts) != 3: raise ValueError("malformed token")
    h_b64, p_b64, s_b64 = parts
    def _b64u(s):
        s += "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s.encode())
    header = json.loads(_b64u(h_b64))
    if header.get("alg") != "RS256": raise ValueError(f"unsupported alg {header.get('alg')}")
    signing_input = (h_b64 + "." + p_b64).encode()
    pub = serialization.load_pem_public_key(pubkey_pem)
    pub.verify(_b64u(s_b64), signing_input, padding.PKCS1v15(), hashes.SHA256())
    return json.loads(_b64u(p_b64))

def load_license_state():
    state = {"valid": False, "mode": "community", "customer": "", "expires_at": "",
             "features": list(COMMUNITY_LIMITS["features"]), "max_users": COMMUNITY_LIMITS["max_users"],
             "warning": "", "bound": False, "bound_to": ""}
    if not LICENSE_FILE.exists():
        state["warning"] = f"No license at {LICENSE_FILE}. Running in COMMUNITY mode (max {COMMUNITY_LIMITS['max_users']} users)."
        return state
    if not PUBLIC_KEY_FILE.exists():
        state["warning"] = "public_key.pem missing — cannot verify license."
        return state
    try:
        token = LICENSE_FILE.read_text().strip()
        payload = _verify_license_token(token, PUBLIC_KEY_FILE.read_bytes())
    except Exception as e:
        state["warning"] = f"License invalid: {e}"
        return state
    # Instance binding check — license can be bound to one OR more fingerprints.
    # Field "fingerprint" may be a string (single host) or a list (multi-host / DR / migration).
    raw_fp = payload.get("fingerprint") or payload.get("fingerprints") or ""
    if isinstance(raw_fp, str):
        bound_list = [s.strip() for s in raw_fp.split(",") if s.strip()] if raw_fp else []
    elif isinstance(raw_fp, list):
        bound_list = [str(s).strip() for s in raw_fp if str(s).strip()]
    else:
        bound_list = []
    if bound_list:
        state["bound"] = True
        state["bound_to"] = ", ".join(fp[:8]+"…" for fp in bound_list)
        if INSTANCE_FINGERPRINT not in bound_list:
            state["warning"] = (f"License is bound to a different instance. "
                                f"This server's fingerprint is {INSTANCE_FINGERPRINT[:12]}…  "
                                f"Send this fingerprint to your vendor to receive a license bound to this installation.")
            return state
    exp = payload.get("expires_at", "")
    try:
        exp_dt = datetime.fromisoformat(exp.replace("Z","+00:00"))
        if datetime.now(timezone.utc) > exp_dt:
            state["warning"] = f"License expired on {exp}"
            return state
        days_left = (exp_dt - datetime.now(timezone.utc)).days
    except Exception:
        days_left = None
    state.update({"valid": True, "mode": "licensed",
                  "customer": payload.get("customer",""), "expires_at": exp,
                  "features": payload.get("features", ["full"]),
                  "max_users": int(payload.get("max_users") or 0) or 999999})
    if days_left is not None and days_left <= 30:
        state["warning"] = f"License expires in {days_left} days ({exp})."
    return state

def license_check_user_limit():
    """Returns (ok, message). Caps active user creation."""
    cap = LICENSE.get("max_users", COMMUNITY_LIMITS["max_users"]) if LICENSE else COMMUNITY_LIMITS["max_users"]
    if not cap: return True, ""
    cur = _dbmod.get_connection()
    try:
        n = cur.execute("SELECT count(*) FROM users WHERE is_active=1").fetchone()[0]
    finally:
        # Same INTRANS hygiene as migrate_legacy_users — read-only txn rollback.
        try: cur.rollback()
        except Exception: pass
        cur.close()
    if n >= cap:
        return False, f"License limit reached: {n}/{cap} active users. Contact your vendor to upgrade."
    return True, ""

LICENSE = load_license_state()
logger.info(f"License: mode={LICENSE['mode']} customer={LICENSE['customer'] or '-'} max_users={LICENSE['max_users']} features={','.join(LICENSE['features'])}")
logger.info(f"Instance fingerprint: {INSTANCE_FINGERPRINT}")
if LICENSE.get("warning"):
    logger.warning(f"LICENSE: {LICENSE['warning']}")
# ═══════════════════════════════════════════════════════════════════════════

# ── Master encryption key for credential vault ────────────────────────────
# Used to encrypt per-user ClickHouse passwords stored in user_credentials.
# Key precedence:  $MASTER_KEY env var  →  data/master.key  →  generated.
MASTER_KEY_FILE = GLOBAL_DIR / "master.key"
def _load_master_key():
    k = os.environ.get("MASTER_KEY", "").strip()
    if k: return k.encode() if isinstance(k, str) else k
    if MASTER_KEY_FILE.exists():
        return MASTER_KEY_FILE.read_bytes().strip()
    # Generate one
    try:
        from cryptography.fernet import Fernet
        new = Fernet.generate_key()
        MASTER_KEY_FILE.write_bytes(new)
        try: os.chmod(MASTER_KEY_FILE, 0o600)
        except Exception: pass
        logger.warning("="*60)
        logger.warning(f"GENERATED master key at {MASTER_KEY_FILE}")
        logger.warning("BACK THIS UP — losing it makes stored CH passwords unrecoverable.")
        logger.warning("In production, set MASTER_KEY env var instead.")
        logger.warning("="*60)
        return new
    except ImportError:
        logger.warning("'cryptography' not installed — credential vault disabled (server-side connections won't work)")
        return None

import key_provider as _keyprov
# Master key now loads through a pluggable provider (env / file / Vault / future
# KMS) so that where the key lives is independent of how the vault uses it, and
# so rotation can expose a previous key for decrypt-only use. See key_provider.py.
_KEY_PROVIDER = _keyprov.load_key_provider(GLOBAL_DIR, logger)
try:
    _MASTER_KEYS = _KEY_PROVIDER.get_keys()
except Exception as _e:
    logger.error(f"Master-key provider '{getattr(_KEY_PROVIDER,'name','?')}' failed: {_e}")
    _MASTER_KEYS = []
# Primary key: the active encrypt key, and the secret the CSRF token derives
# from. Any further keys are decrypt-only, kept during a rotation window.
MASTER_KEY = _MASTER_KEYS[0] if _MASTER_KEYS else b""
logger.info(f"Master key: provider={getattr(_KEY_PROVIDER,'name','?')} keys_loaded={len(_MASTER_KEYS)}")

def _fernet():
    # MultiFernet encrypts with the primary key and decrypts with any loaded
    # key — this is what lets a rotation window (new primary + old secondary)
    # work without downtime.
    if not _MASTER_KEYS: return None
    from cryptography.fernet import Fernet, MultiFernet
    return MultiFernet([Fernet(k) for k in _MASTER_KEYS])

def fernet_encrypt(plain: str) -> str:
    f = _fernet()
    if not f: raise RuntimeError("Credential vault not available — install 'cryptography' and configure MASTER_KEY")
    return f.encrypt((plain or "").encode()).decode()

def fernet_decrypt(token: str) -> str:
    f = _fernet()
    if not f: raise RuntimeError("Credential vault not available")
    return f.decrypt(token.encode()).decode()

# ── CSRF protection ──────────────────────────────────────────────────────────
# State-changing API requests must echo an X-CSRF-Token header equal to
# HMAC(master-key, session-token). The value is handed to the browser in a
# readable `csrf_token` cookie (set on every authenticated response) and the SPA
# returns it via a global fetch wrapper. Because the token is bound to the
# session and signed with the server's master key, a cross-site page cannot
# forge it — defence-in-depth above the SameSite=Lax session cookie. Under XSS
# all bets are off (same as any token), which is why CSP (below) matters too.
_CSRF_SECRET = hashlib.sha256(b"csrf-token-v1|" + (MASTER_KEY or b"")).digest()
CSRF_COOKIE_NAME = "csrf_token"

def _expected_csrf(session_token: str) -> str:
    return hmac.new(_CSRF_SECRET, (session_token or "").encode("utf-8"),
                    hashlib.sha256).hexdigest()

@app.before_request
def _csrf_gate():
    # Guard only mutating API calls. Unauthenticated requests (no session yet,
    # e.g. the login POST) have nothing to forge and are skipped. Registered
    # after _auth_gate, so g.token is already resolved when this runs.
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if not request.path.startswith("/api/"):
        return
    token = getattr(g, "token", None)
    if not token:
        return
    sent = request.headers.get("X-CSRF-Token", "")
    if not (sent and hmac.compare_digest(sent, _expected_csrf(token))):
        return jsonify({"error": "csrf_failed", "code": "CSRF_FAILED"}), 403

@app.after_request
def _set_csrf_cookie(resp):
    # Hand the current session's CSRF token to the browser so the SPA can echo
    # it. Readable by JS by design; on its own it is useless — it is only valid
    # combined with the HttpOnly session cookie it is derived from.
    try:
        token = getattr(g, "token", None)
        if token:
            resp.set_cookie(CSRF_COOKIE_NAME, _expected_csrf(token),
                            httponly=False, samesite="Lax", path="/",
                            max_age=SESSION_TTL_DAYS * 86400)
    except Exception:
        pass
    return resp

# ── Connection params resolver — supports both inline & registry modes ────
def cP_resolve(d: dict) -> dict:
    """Resolve connection params from request body. Two modes:
       1) connection_id → look up in connections + user_credentials (decrypted).
       2) inline {host, port, user, password} (legacy / browser-side connections).
    """
    cid = d.get("connection_id")
    if cid:
        try: cid = int(cid)
        except: raise ValueError("invalid connection_id")
        crow = db().execute("SELECT host, port, name FROM connections WHERE id=?", (cid,)).fetchone()
        if not crow:
            raise ValueError(f"connection_id {cid} not found in registry")
        u = getattr(g, "user", None)
        if not u: raise ValueError("not authenticated")
        dbu = db_user(u["username"])
        if not dbu:
            raise ValueError("user lookup failed")
        urow = dbu.execute(
            "SELECT ch_username, ch_password_enc FROM user_credentials "
            "WHERE user_id=? AND connection_id=?",
            (dbu.user_id, cid)).fetchone()
        if not urow:
            raise ValueError(f"no credentials set for connection '{crow['name']}'. Set them via Profile → Connections.")
        return {"host": crow["host"], "port": int(crow["port"]),
                "user": urow["ch_username"],
                "password": fernet_decrypt(urow["ch_password_enc"])}
    # Inline (legacy)
    return {"host": d.get("host","localhost"), "port": int(d.get("port") or 8123),
            "user": d.get("user","default"), "password": d.get("password","") or ""}
# ═══════════════════════════════════════════════════════════════════════════

# ── request logger ────────────────────────────────────────────────────────────
@app.after_request
def _log_response(response):
    path = request.path
    skip = ["/api/query/poll/", "/api/job/", "/api/monitor/", "/api/console-log"]
    if any(s in path for s in skip):
        return response
    ip = request.remote_addr or "-"
    detail = ""
    try:
        d = request.get_json(silent=True) or {}
        if "sql" in d:      detail = "sql=" + str(d["sql"])[:80].replace("\n"," ")
        elif "backup_type" in d: detail = f"type={d.get('backup_type')}"
        elif "host" in d and "user" in d: detail = f"host={d.get('host')}:{d.get('port')} user={d.get('user')}"
        elif "database" in d and "branch_name" in d: detail = f"{d.get('database')}.{d.get('table')} -> {d.get('branch_name')}"
    except: pass
    msg = f'{ip} "{request.method} {path}" {response.status_code}'
    if detail: msg += f" | {detail}"
    if response.status_code >= 500: logger.error(msg)
    elif response.status_code >= 400: logger.warning(msg)
    else: logger.info(msg)
    return response

@app.errorhandler(Exception)
def handle_exception(e):
    # Identifier/value validation raises ValueError — surface it as a clean 400
    # (bad client input) rather than a 500 (server fault).
    if isinstance(e, ValueError):
        return jsonify({"error": str(e)}), 400
    logger.error(f"Unhandled exception: {e}")
    return jsonify({"error": str(e)}), 500

# ── job store ─────────────────────────────────────────────────────────────────
# Job state lives in Redis (job_store) — NOT in per-process dicts — so that
# any gunicorn worker can serve the poll request for a job another worker
# started. See job_store.py for the full rationale.

def _jnew():
    jid = str(uuid.uuid4())[:8]
    job_store.job_create(jid)
    return jid

def _ja(jid, t, m):
    job_store.job_append_line(jid, t, m)

def _jdone(jid, ok=True, err=None):
    job_store.job_set_status(jid, "ok" if ok else "error", err)

def _run(script, args, jid):
    cmd = [sys.executable, str(APP_DIR/script)] + args
    safe = " ".join(a for i,a in enumerate(args) if not (i>0 and args[i-1]=="--password"))
    logger.info(f"JOB START  job={jid} script={script} args={safe[:120]}")
    _ja(jid,"info","$ python3 "+script+" "+safe[:120])
    try:
        proc = subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,cwd=str(APP_DIR))
        job_store.job_set_pid(jid, proc.pid)
        for raw in proc.stdout:
            if job_store.job_status(jid)=="cancelled":
                proc.terminate(); break
            line=re.sub(r'\033\[[0-9;]*m','',raw.rstrip())
            if not line: continue
            ll=line.lower()
            if   any(w in ll for w in ["error","failed","exception","traceback"]): _ja(jid,"err",line)
            elif any(w in ll for w in ["warning","warn"]):                          _ja(jid,"warn",line)
            elif any(w in ll for w in ["ok","success","complete","done","verified","frozen","attached","created","backup id"]): _ja(jid,"ok",line)
            else: _ja(jid,"out",line)
        proc.wait()
        if job_store.job_status(jid)=="cancelled":
            logger.warning(f"JOB CANCEL job={jid}"); return
        ok = proc.returncode==0
        _ja(jid,"ok" if ok else "err",f"Finished (exit {proc.returncode})")
        logger.info(f"JOB {'OK    ' if ok else 'FAILED'} job={jid} exit={proc.returncode}")
        _jdone(jid,ok)
    except Exception as e:
        _ja(jid,"err",f"Error: {e}")
        logger.error(f"JOB ERROR  job={jid} error={e}")
        _jdone(jid,False,str(e))

# ── static ────────────────────────────────────────────────────────────────────
def _index_file():
    # Opt-in: serve the minified single-file app when SERVE_MINIFIED=1 and a
    # built static/index.min.html exists (produced by scripts/minify.sh).
    # Defaults to the normal index.html so nothing changes unless enabled.
    if os.environ.get("SERVE_MINIFIED") == "1" and (STATIC_DIR / "index.min.html").exists():
        return "index.min.html"
    return "index.html"

# ── Content Security Policy (per-request script + style nonce) ────────────────
# The single inline <script> that boots the SPA, and every inline <style> block
# (the head stylesheet plus the styles written into printable report windows),
# are tagged with a fresh nonce on every page load. The CSP advertises that
# nonce via script-src and style-src-elem instead of 'unsafe-inline', so an
# injected inline <script> or <style> carries no valid nonce and is blocked.
# style-src-attr keeps 'unsafe-inline' for the unavoidable inline style
# attributes / element.style the UI relies on (a much smaller surface than a
# full <style> block). Because the nonce must match the served HTML, the CSP is
# set here at the app rather than statically at nginx.
# layer rather than in nginx; the other security headers stay in nginx.
def _csp_policy(nonce=None):
    script_src = "'self'" + (f" 'nonce-{nonce}'" if nonce else "") + " https://cdnjs.cloudflare.com"
    # style-src-elem locks <style>/<link> elements to the per-request nonce plus
    # the known external CSS origins, with NO 'unsafe-inline' — so an injected
    # <style> block (e.g. CSS-based data exfiltration) is refused. style-src-attr
    # keeps 'unsafe-inline' because the UI is built on inline style attributes
    # and element.style, which cannot carry a nonce; those are a far smaller risk
    # (a style attribute cannot @import or load an external resource). The plain
    # style-src line remains as the fallback for browsers that do not implement
    # the -elem / -attr directives, so there is no regression on older clients.
    style_elem = "'self'" + (f" 'nonce-{nonce}'" if nonce else "") + " https://cdnjs.cloudflare.com https://fonts.googleapis.com"
    return ("default-src 'self'; "
            f"script-src {script_src}; "
            "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
            f"style-src-elem {style_elem}; "
            "style-src-attr 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self' data: https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'self'; form-action 'self'")

_INDEX_CACHE = {}
def _read_index_html():
    fn = _index_file()
    if fn not in _INDEX_CACHE:
        _INDEX_CACHE[fn] = (STATIC_DIR / fn).read_text(encoding="utf-8")
    return _INDEX_CACHE[fn]

def _serve_index():
    # Inject a per-request nonce into the one inline <script> (external
    # <script src=...> tags carry attributes and are left untouched), and set a
    # matching nonce-based CSP on this response.
    nonce = secrets.token_urlsafe(16)
    html = _read_index_html().replace("<script>", f'<script nonce="{nonce}">', 1)
    # Nonce every <style> block: the one in <head> (governed by this response's
    # CSP) and the <style> strings the SPA writes into the printable report
    # windows it opens via window.open()+document.write — those about:blank
    # windows inherit this page's CSP, so their <style> must carry the same
    # nonce to satisfy style-src-elem. The nonce attribute is harmless on any
    # browser that does not apply the inherited policy.
    html = html.replace("<style>", f'<style nonce="{nonce}">')
    resp = app.make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Content-Security-Policy"] = _csp_policy(nonce)
    return resp

@app.route("/")
def index(): return _serve_index()
@app.route("/login")
def login_page(): return _serve_index()

@app.after_request
def _baseline_csp(resp):
    # Non-HTML responses (API JSON, etc.) get a script-src with no inline and no
    # nonce — they never carry inline script, so this is the strict default.
    if "Content-Security-Policy" not in resp.headers:
        resp.headers["Content-Security-Policy"] = _csp_policy()
    return resp
@app.route("/health")
def health(): return jsonify({"ok": True, "version": "4.0"})

# ── Request-ID + lightweight request counter (item 10) ───────────────────
import itertools as _itertools
_req_counter = _itertools.count()
_REQ_TOTAL = {"n": 0}
_START_TIME = time.time()

@app.before_request
def _assign_request_id():
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    g.request_id = rid
    g._req_t0 = time.time()
    try:
        _REQ_TOTAL["n"] = next(_req_counter)
    except Exception:
        pass

@app.after_request
def _emit_request_id(resp):
    try:
        rid = getattr(g, "request_id", None)
        if rid:
            resp.headers["X-Request-ID"] = rid
            # Structured-ish access line carrying the request id, method, path,
            # status and latency so logs can be correlated end to end.
            dt = (time.time() - getattr(g, "_req_t0", time.time())) * 1000
            app.logger.info("rid=%s %s %s -> %s %.1fms",
                            rid, request.method, request.path, resp.status_code, dt)
    except Exception:
        pass
    return resp

def _dep_health():
    """Quick Postgres + Redis reachability probe, returns (ok, detail)."""
    checks, ok = {}, True
    t0 = time.time()
    try:
        cur = db().execute("SELECT 1"); cur.fetchone()
        checks["postgres"] = {"ok": True, "ms": round((time.time()-t0)*1000, 1)}
    except Exception as e:
        ok = False
        checks["postgres"] = {"ok": False, "error": str(e)[:200]}
    t1 = time.time()
    try:
        rok = bool(session_store.ping())
        checks["redis"] = {"ok": rok, "ms": round((time.time()-t1)*1000, 1)}
        ok = ok and rok
    except Exception as e:
        ok = False
        checks["redis"] = {"ok": False, "error": str(e)[:200]}
    return ok, checks

@app.route("/healthz")
def healthz():
    # Liveness: process is up. No dependency checks, always 200 when serving.
    return jsonify({"status": "ok", "version": "4.0"}), 200

@app.route("/readyz")
def readyz():
    # Readiness: 200 only when dependencies are reachable, else 503 so a load
    # balancer / k8s pulls the instance out of rotation until it recovers.
    ok, checks = _dep_health()
    return jsonify({"status": "ready" if ok else "not_ready", "checks": checks}), (200 if ok else 503)

@app.route("/metrics")
def metrics():
    # Minimal Prometheus text exposition (no external client dependency).
    ok, checks = _dep_health()
    pg = 1 if checks.get("postgres", {}).get("ok") else 0
    rd = 1 if checks.get("redis", {}).get("ok") else 0
    lines = [
        "# HELP clickhouse_console_up 1 if the app process is serving",
        "# TYPE clickhouse_console_up gauge",
        "clickhouse_console_up 1",
        "# HELP clickhouse_console_ready 1 if all dependencies are reachable",
        "# TYPE clickhouse_console_ready gauge",
        "clickhouse_console_ready %d" % (1 if ok else 0),
        "# HELP clickhouse_console_postgres_up 1 if Postgres is reachable",
        "# TYPE clickhouse_console_postgres_up gauge",
        "clickhouse_console_postgres_up %d" % pg,
        "# HELP clickhouse_console_redis_up 1 if Redis is reachable",
        "# TYPE clickhouse_console_redis_up gauge",
        "clickhouse_console_redis_up %d" % rd,
        "# HELP clickhouse_console_uptime_seconds Process uptime in seconds",
        "# TYPE clickhouse_console_uptime_seconds gauge",
        "clickhouse_console_uptime_seconds %.1f" % (time.time() - _START_TIME),
        "# HELP clickhouse_console_http_requests_total Total HTTP requests seen",
        "# TYPE clickhouse_console_http_requests_total counter",
        "clickhouse_console_http_requests_total %d" % _REQ_TOTAL["n"],
        '# HELP clickhouse_console_build_info Build info',
        '# TYPE clickhouse_console_build_info gauge',
        'clickhouse_console_build_info{version="4.0"} 1',
        "",
    ]
    return ("\n".join(lines), 200, {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"})


@app.route("/health/deep")
def health_deep():
    """Structural reachability check of the state tier. Returns a JSON
    object with one entry per dependency, each carrying:
        ok    — boolean
        ms    — round-trip latency in milliseconds (or null on failure)
        error — error string on failure, otherwise omitted

    The endpoint always returns HTTP 200 with structured detail — the
    operator decides what counts as 'unhealthy' from the body rather
    than the status code. Add this endpoint to an LB-side liveness
    probe only when you want the application removed from rotation as
    soon as Postgres or Redis go away (fail-closed behaviour); use the
    shallow /health endpoint when you want it removed only when the
    application process itself is dead.
    """
    out = {"version": "4.0", "checks": {}}
    overall_ok = True

    # Postgres ------------------------------------------------------------
    pg_t0 = time.time()
    try:
        cur = db().execute("SELECT 1")
        cur.fetchone()
        out["checks"]["postgres"] = {"ok": True,
                                     "ms": round((time.time()-pg_t0)*1000, 1)}
    except Exception as e:
        overall_ok = False
        out["checks"]["postgres"] = {"ok": False,
                                     "ms": round((time.time()-pg_t0)*1000, 1),
                                     "error": str(e)[:200]}

    # Redis ---------------------------------------------------------------
    rd_t0 = time.time()
    try:
        if session_store.ping():
            out["checks"]["redis"] = {"ok": True,
                                      "ms": round((time.time()-rd_t0)*1000, 1)}
        else:
            overall_ok = False
            out["checks"]["redis"] = {"ok": False,
                                      "ms": round((time.time()-rd_t0)*1000, 1),
                                      "error": "ping returned false"}
    except Exception as e:
        overall_ok = False
        out["checks"]["redis"] = {"ok": False,
                                  "ms": round((time.time()-rd_t0)*1000, 1),
                                  "error": str(e)[:200]}

    out["ok"] = overall_ok
    return jsonify(out)
@app.route("/<path:p>")
def statics(p): return send_from_directory(STATIC_DIR,p)

# ── jobs ──────────────────────────────────────────────────────────────────────
@app.route("/api/job/<jid>")
def job_poll(jid):
    # Reads from Redis (job_store) so it works no matter which gunicorn
    # worker started the job. A Redis outage returns a clear 503 — never
    # the misleading "not found".
    try:
        j = job_store.job_get(jid)
    except Exception as e:
        return jsonify({"error": f"job store unreachable: {e}"}), 503
    return jsonify(j) if j else (jsonify({"error":"not found"}),404)

@app.route("/api/job/<jid>/cancel",methods=["POST"])
def job_cancel(jid):
    try:
        j = job_store.job_get(jid)
    except Exception as e:
        return jsonify({"error": f"job store unreachable: {e}"}), 503
    if not j: return jsonify({"error":"not found"}),404
    if j.get("status")=="running":
        job_store.job_set_status(jid,"cancelled")
        pid=j.get("pid")
        if pid:
            try:
                import signal; os.kill(pid,signal.SIGTERM)
            except: pass
        job_store.job_append_line(jid,"warn","Cancelled by user.")
    return jsonify({"ok":True})

# ── helpers ───────────────────────────────────────────────────────────────────
# ── ClickHouse connection pool ────────────────────────────────────────────────
# Until now every request opened a fresh clickhouse_connect client and closed it
# at the end — or leaked it on the error path. Under gunicorn with several gthread
# workers and hundreds of users that connection churn is a dominant latency cost
# and exhausts server-side sockets. This pool keeps a bounded set of live clients
# per (host, port, user, password, database) signature and hands them out / takes
# them back through a thin wrapper whose .close() is a check-in — so none of the
# ~80 existing `cl.close()` call sites have to change.
#
# Tunables (env):
#   CH_POOL_MAX       max live clients per connection signature   (default 8)
#   CH_POOL_TIMEOUT   seconds to wait for a free client           (default 10)
#   CH_POOL_IDLE      seconds an idle client may live unused      (default 300)
#   CH_POOL_PING_AGE  ping a reused client idle longer than this  (default 60)
#
# The pool lives in each worker process (gthread workers share it across their
# threads), so total connections per cluster ~= GUNICORN_WORKERS * CH_POOL_MAX.
import atexit as _atexit
import weakref as _weakref

class _CHConnPool:
    def __init__(self):
        self.max_per_key   = max(1, int(os.environ.get("CH_POOL_MAX", "8")))
        self.checkout_wait = float(os.environ.get("CH_POOL_TIMEOUT", "10"))
        self.idle_ttl      = float(os.environ.get("CH_POOL_IDLE", "300"))
        self.ping_age      = float(os.environ.get("CH_POOL_PING_AGE", "60"))
        self._idle  = {}   # key -> list[[client, last_used_ts], ...]
        self._inuse = {}   # key -> int (checked-out count)
        self._cond  = threading.Condition()
        _atexit.register(self._drain)

    def _drain(self):
        with self._cond:
            for lst in self._idle.values():
                for item in lst:
                    try: item[0].close()
                    except Exception: pass
            self._idle.clear()

    @staticmethod
    def _ping_ok(client):
        try: return bool(client.ping())
        except Exception: return False

    def checkout(self, key, factory):
        # Returns a live clickhouse_connect client, reusing an idle one when
        # possible. All network IO (ping / connect / close) happens OUTSIDE the
        # lock so a slow connection never stalls other threads' check-in/out.
        deadline = time.time() + self.checkout_wait
        while True:
            client = client_ts = None
            make_new = False
            expired = []
            with self._cond:
                idle = self._idle.setdefault(key, [])
                while idle:                      # LIFO keeps the warmest socket
                    c, ts = idle.pop()
                    if time.time() - ts > self.idle_ttl:
                        expired.append(c); continue
                    client, client_ts = c, ts
                    self._inuse[key] = self._inuse.get(key, 0) + 1
                    break
                if client is None:
                    if self._inuse.get(key, 0) + len(idle) < self.max_per_key:
                        self._inuse[key] = self._inuse.get(key, 0) + 1
                        make_new = True
                    elif time.time() < deadline:
                        self._cond.wait(timeout=deadline - time.time())
            # ---- lock released ----
            for c in expired:
                try: c.close()
                except Exception: pass
            if client is not None:
                if time.time() - client_ts > self.ping_age and not self._ping_ok(client):
                    try: client.close()
                    except Exception: pass
                    with self._cond:
                        self._inuse[key] = max(0, self._inuse.get(key, 0) - 1)
                        self._cond.notify()
                    continue                     # dead socket — get another
                return client
            if make_new:
                try:
                    return factory()
                except Exception:
                    with self._cond:
                        self._inuse[key] = max(0, self._inuse.get(key, 0) - 1)
                        self._cond.notify()
                    raise
            if time.time() >= deadline:
                raise RuntimeError(
                    "ClickHouse connection pool exhausted "
                    f"(CH_POOL_MAX={self.max_per_key}). Too many concurrent queries "
                    "against this cluster — retry shortly.")
            # woke from wait before the deadline → loop and retry

    def checkin(self, key, client):
        with self._cond:
            self._inuse[key] = max(0, self._inuse.get(key, 0) - 1)
            self._idle.setdefault(key, []).append([client, time.time()])
            self._cond.notify()

    def stats(self):
        with self._cond:
            return {"keys": len(set(self._idle) | set(self._inuse)),
                    "idle": sum(len(v) for v in self._idle.values()),
                    "in_use": sum(self._inuse.values())}

_CH_POOL = _CHConnPool()

def _ch_pool_reclaim(key, client, state):
    # weakref finalizer: return a client to the pool if its wrapper was GC'd
    # without an explicit .close() (e.g. an endpoint returned on its error path).
    # Keeps the pool leak-proof without try/finally at every call site.
    if state.get("closed"): return
    state["closed"] = True
    try: _CH_POOL.checkin(key, client)
    except Exception: pass

class _PooledCHClient:
    # Thin proxy: every attribute except close() is forwarded to the real
    # clickhouse_connect client; close() checks the client back into the pool.
    def __init__(self, real, key):
        self._real  = real
        self._key   = key
        self._state = {"closed": False}
        # Must NOT capture `self` in the finalizer or the wrapper would never be
        # collected; the shared state dict makes a post-close finalize a no-op.
        self._fin = _weakref.finalize(self, _ch_pool_reclaim, key, real, self._state)
    def __getattr__(self, name):
        if name in ("_real", "_key", "_state", "_fin"):
            raise AttributeError(name)        # guards against init-time recursion
        return getattr(self._real, name)
    def close(self):
        if self._state["closed"]: return
        self._state["closed"] = True
        _CH_POOL.checkin(self._key, self._real)
        self._fin.detach()
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        self.close()

# ── SQL identifier / value hardening ──────────────────────────────────────────
# db / table / column / partition / query_id arrive from the browser and used to
# be interpolated straight into f-string SQL (FROM {db}.{tbl}, WHERE
# database='{db}', ...). A validated identifier contains no quote or statement
# metacharacter, so validating once at the point it is read makes every
# downstream interpolation injection-safe in BOTH identifier and value position.
_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]*$')

def _safe_ident(name, kind="identifier", allow_empty=True):
    """Validate a ClickHouse identifier (db / table / column). Returns the name
    unchanged when valid, '' when empty (and allowed), else raises ValueError —
    which the endpoints' try/except turns into a clean error response."""
    s = "" if name is None else str(name).strip()
    if not s:
        if allow_empty:
            return ""
        raise ValueError(f"{kind} is required")
    if len(s) > 256 or not _IDENT_RE.match(s):
        raise ValueError(f"invalid {kind}: {name!r}")
    return s

def _qident(name, kind="identifier"):
    """Validated, backtick-quoted identifier for FROM / DDL positions."""
    return "`" + _safe_ident(name, kind, allow_empty=False) + "`"

def _safe_qid(qid, allow_empty=False):
    """ClickHouse query_id — UUID-ish; allow alnum and a few id-safe chars only."""
    s = "" if qid is None else str(qid).strip()
    if not s:
        if allow_empty:
            return ""
        raise ValueError("query_id is required")
    if len(s) > 128 or not re.match(r'^[A-Za-z0-9_.:+\-]+$', s):
        raise ValueError(f"invalid query_id: {qid!r}")
    return s

def _safe_partition(part, allow_empty=False):
    """A partition id / value. ClickHouse partition ids are alnum; partition
    *values* for custom keys may include a small punctuation set. We allow that
    conservative set and reject anything that could break out of a literal."""
    s = "" if part is None else str(part).strip()
    if not s:
        if allow_empty:
            return ""
        raise ValueError("partition is required")
    if len(s) > 256 or not re.match(r"^[A-Za-z0-9_.\-]+$", s):
        raise ValueError(f"invalid partition: {part!r}")
    return s

def _safe_branch(name):
    """A branch-name suffix appended to a table identifier (tbl_<branch>). Allow
    identifier chars only so it cannot break out of the backtick-quoted name."""
    s = "" if name is None else str(name).strip()
    if not s:
        raise ValueError("branch_name is required")
    if len(s) > 128 or not re.match(r'^[A-Za-z0-9_$]+$', s):
        raise ValueError(f"invalid branch_name: {name!r}")
    return s

def _safe_ident_star(name, kind="identifier"):
    """Like _safe_ident but also allows the GRANT wildcard '*' (e.g. ON db.*)."""
    s = "" if name is None else str(name).strip()
    if s in ("*", ""):
        return "*"
    return _safe_ident(s, kind, allow_empty=False)

def _qstr(s):
    """Escape a value for a ClickHouse single-quoted string literal (passwords,
    HOST patterns, etc.). Escapes backslash and single-quote so the value can
    never close the literal early and inject trailing SQL."""
    s = "" if s is None else str(s)
    return s.replace("\\", "\\\\").replace("'", "\\'")

def _safe_privilege(priv):
    """Validate a GRANT privilege token. ClickHouse privileges are keywords that
    may include spaces and an optional column list, e.g. 'SELECT',
    'ALTER UPDATE', 'SELECT(col1, col2)'. Allow that vocabulary's characters and
    reject anything (quotes, semicolons, back-ticks) that could break out of the
    privilege position."""
    s = "" if priv is None else str(priv).strip()
    if not s:
        raise ValueError("privilege is required")
    if len(s) > 200 or not re.match(r'^[A-Za-z][A-Za-z0-9_ ,()*.]*$', s):
        raise ValueError(f"invalid privilege: {priv!r}")
    return s

def _get_client(d):
    # Pooled clickhouse_connect client. The returned object behaves exactly like
    # a real client (all methods proxied) and its .close() returns it to the pool.
    # 'database' enables unqualified table references: when set, 'SELECT FROM t'
    # resolves to '<database>.t' server-side; cross-database refs still work.
    p = cP_resolve(d) if isinstance(d, dict) else d
    db = ((d.get("database") if isinstance(d, dict) else None) or "")
    db = (db or "").strip()
    key = (p.get("host", "localhost"), int(p.get("port", 8123)),
           p.get("user", "default"), p.get("password", "") or "", db)
    def _factory():
        import clickhouse_connect
        kwargs = dict(host=key[0], port=key[1], username=key[2],
                      password=key[3], connect_timeout=10, query_limit=0)
        if key[4]:
            kwargs["database"] = key[4]
        return clickhouse_connect.get_client(**kwargs)
    real = _CH_POOL.checkout(key, _factory)
    return _PooledCHClient(real, key)

def _conn_args(d):
    p = cP_resolve(d) if isinstance(d, dict) else d
    args=["--host",p.get("host","localhost"),"--port",str(p.get("port",8123)),"--username",p.get("user","default")]
    if (p.get("password") or "").strip(): args+=["--password",p["password"]]
    return args

def _store_args(d):
    st=d.get("storage","local")
    args=["--storage",st,"--backup-dir",d.get("backup_dir","/var/lib/clickhouse/backups"),
          "--catalog-file",d.get("catalog_file","/var/lib/clickhouse/backups/catalog.json")]
    if st=="s3":
        for fl,k in [("--s3-bucket","s3_bucket"),("--s3-prefix","s3_prefix"),("--s3-region","s3_region"),
                     ("--s3-endpoint","s3_endpoint"),("--s3-access-key","s3_key"),("--s3-secret-key","s3_secret")]:
            if d.get(k): args+=[fl,d[k]]
        if d.get("s3_path_style"): args.append("--s3-path-style")
    elif st=="gcs":
        if d.get("gcs_bucket"): args+=["--gcs-bucket",d["gcs_bucket"]]
        if d.get("gcs_creds"):  args+=["--gcs-credentials",d["gcs_creds"]]
    elif st=="azure":
        if d.get("az_account"):   args+=["--azure-account",d["az_account"]]
        if d.get("az_container"): args+=["--azure-container",d["az_container"]]
        if d.get("az_sas"):       args+=["--azure-sas-token",d["az_sas"]]
        if d.get("az_conn"):      args+=["--azure-conn-str",d["az_conn"]]
    return args

def _to_json(v):
    if v is None: return None
    if isinstance(v,(int,float,str,bool)): return v
    return str(v)

# ── connect ───────────────────────────────────────────────────────────────────
@app.route("/api/connect/test",methods=["POST"])
def conn_test():
    d=request.json or {}
    host=(d.get("host") or "").strip(); port=d.get("port",8123); user=d.get("user","default")
    # An empty host would round-trip to clickhouse-connect's default ("localhost"),
    # which produces a confusing "connection refused to localhost:8123" failure
    # in the app log when the operator thought they were testing a different
    # cluster. Reject the input here so the failure is obvious.
    if not host:
        logger.warning("CONNECT REJECTED reason=empty-host (form was submitted without a hostname)")
        return jsonify({"ok":False,"error":"Host is required."})
    try:
        # Push the cleaned host back into the dict so _get_client uses it
        d["host"]=host
        cl=_get_client(d)
        ver=cl.server_version
    except Exception as e:
        logger.warning(f"CONNECT FAILED host={host}:{port} user={user} error={e}")
        return jsonify({"ok":False,"error":str(e)})
    # Connection works. Counts are best-effort — on cluster setups that route
    # system queries through Distributed engines pointing at unreachable nodes,
    # the count query can hang or error. Don't let that fail the whole test.
    cnt=0; sys_cnt=0
    try:
        cnt=cl.query("SELECT count() FROM system.tables WHERE database NOT IN ('system','information_schema','INFORMATION_SCHEMA')").result_rows[0][0]
    except Exception as ce:
        logger.warning(f"connect_test: tables count skipped: {ce}")
    try:
        sys_cnt=cl.query("SELECT count() FROM system.tables WHERE database IN ('system','information_schema','INFORMATION_SCHEMA')").result_rows[0][0]
    except Exception as ce:
        logger.warning(f"connect_test: system tables count skipped: {ce}")
    try: cl.close()
    except: pass
    logger.info(f"CONNECT host={host}:{port} user={user} version={ver} tables={cnt}")
    return jsonify({"ok":True,"version":ver,"table_count":cnt,"sys_table_count":sys_cnt})

# ── backup ────────────────────────────────────────────────────────────────────
@app.route("/api/backup/run",methods=["POST"])
def backup_run():
    d=request.json or {}
    args=["backup"]+_conn_args(d)+_store_args(d)
    sel=d.get("table_sel","tables")
    if   sel=="all":      args.append("--all-tables")
    elif sel=="database": args+=["--database",d.get("db_name","")]
    else:
        t=(d.get("tables") or "").strip()
        if t: args+=["--tables"]+t.split()
    bt=d.get("backup_type","full")
    if bt=="differential": args.append("--differential")
    elif bt=="incremental": args.append("--incremental")
    if d.get("base_id"):       args+=["--base-backup-id",d["base_id"]]
    if d.get("flush"):         args.append("--flush-before-backup")
    if not d.get("compress",True): args.append("--no-compress")
    if not d.get("verify",True):   args.append("--no-verify")
    if d.get("parallel"):    args+=["--parallel-threads",str(d["parallel"])]
    if d.get("max_retries"): args+=["--max-retries",str(d["max_retries"])]
    if d.get("tag"):  args+=["--tag",d["tag"]]
    if d.get("note"): args+=["--note",d["note"]]
    jid=_jnew()
    logger.info(f"BACKUP START type={bt} job={jid}")
    audit("Start Legacy Backup", panel="backup",
          detail=f"type={bt} table_sel={sel} db={d.get('db_name','')} "
                 f"tables={(d.get('tables') or '')[:120]} job_id={jid} "
                 f"storage={d.get('storage','local')}")
    threading.Thread(target=_run,args=("clickhouse_pitr.py",args,jid),daemon=True).start()
    return jsonify({"job_id":jid})


# ─── Native ClickHouse BACKUP/RESTORE ────────────────────────────────────
# Bypass the legacy clickhouse_pitr.py script and use ClickHouse's
# built-in BACKUP TO File(path) directly. The script approach failed
# in deployments where the app host's filesystem differs from the
# ClickHouse server's. The native command runs entirely on the
# ClickHouse server — the path the operator supplies must be a
# directory accessible to the clickhouse-server process, and the
# server must be configured with <backups><allowed_path> covering
# it. See Installation Guide §12 for the required server-side config.

def _backup_file_clause(d):
    """File('/path/name.zip') destination for BACKUP/RESTORE. We use
    File rather than Disk so the operator doesn't have to pre-register
    a named disk in clickhouse-server config — only allowed_path needs
    to cover the directory."""
    path = (d.get("path") or "").strip().rstrip("/")
    name = (d.get("name") or "").strip()
    if not path or not name:
        return None, "path and name are required"
    if ".." in name or "/" in name or "\\" in name:
        return None, "name must be a simple filename, no slashes or '..'"
    if not name.endswith(".zip"):
        name = name + ".zip"
    # Single quotes inside a path would break the SQL literal — disallow.
    if "'" in path or "'" in name:
        return None, "path/name may not contain single quotes"
    return f"File('{path}/{name}')", None

def _backup_target_clause(d):
    """The BACKUP TABLE/DATABASE/ALL part."""
    sel = (d.get("target") or "database").strip()
    if sel == "all":
        return "ALL", None
    if sel == "database":
        db = (d.get("db_name") or "").strip()
        if not db:
            return None, "db_name is required when target=database"
        try:
            return f"DATABASE {_qident(db, 'database')}", None
        except ValueError as e:
            return None, str(e)
    if sel == "tables":
        raw = (d.get("tables") or "").strip()
        if not raw:
            return None, "tables list is required when target=tables"
        tables = [t.strip() for t in raw.replace(",", " ").split() if t.strip()]
        for t in tables:
            if "." not in t:
                return None, f"table '{t}' must be qualified as db.table"
        try:
            quoted = ", ".join(
                "TABLE " + _qident(t.split(".", 1)[0], "database")
                + "." + _qident(t.split(".", 1)[1], "table")
                for t in tables
            )
        except ValueError as e:
            return None, str(e)
        return quoted, None
    return None, f"unknown target '{sel}'"


def _find_latest_full_backup(client, path, name_glob):
    """Return (path, name) of the most recent successful full backup
    matching the given filename glob in the given directory, or
    (None, None) if not found. 'Full' is defined as: a system.backups
    row in BACKUP_CREATED state whose base_backup_name is empty
    (no base = full)."""
    try:
        # ClickHouse's system.backups schema across versions: id, name,
        # status, error, start_time, end_time, num_files, total_size,
        # uncompressed_size, compressed_size, files_read,
        # base_backup_name (or 'base_backup_name' may be absent on
        # very old versions — defend with a try/except).
        sql = f"""
            SELECT name FROM system.backups
            WHERE status = 'BACKUP_CREATED'
              AND name LIKE '%File(%' 
              AND positionCaseInsensitive(name, '{path}/') > 0
              AND (empty(base_backup_name) OR base_backup_name = '')
            ORDER BY start_time DESC
            LIMIT 50
        """
        rows = client.query(sql).result_rows or []
    except Exception:
        # Fall back to no base_backup_name filter — old CH versions
        sql = f"""
            SELECT name FROM system.backups
            WHERE status = 'BACKUP_CREATED'
              AND positionCaseInsensitive(name, '{path}/') > 0
            ORDER BY start_time DESC
            LIMIT 50
        """
        rows = client.query(sql).result_rows or []

    # name column is something like "File('/path/to/file.zip')" — parse it.
    import re as _re
    for r in rows:
        s = str(r[0])
        m = _re.search(r"File\('([^']+)'\)", s)
        if not m: continue
        full_path = m.group(1)
        # Filename must match glob
        basename = full_path.rsplit("/", 1)[-1]
        import fnmatch
        if fnmatch.fnmatch(basename, name_glob):
            return full_path.rsplit("/", 1)[0], basename
    return None, None


@app.route("/api/backup/native/run", methods=["POST"])
def backup_native_run():
    """Start a native BACKUP. ASYNC — returns the backup id assigned by
    ClickHouse so the UI can poll system.backups for status.

    backup_type:
      'full'         — no base
      'incremental'  — base = explicit base_backup_path + base_backup_name
                       (the last backup of any kind in this chain)
      'differential' — base = most recent full backup matching
                       base_search_glob in storage_path, looked up
                       server-side from system.backups
    """
    d = request.json or {}

    target, err = _backup_target_clause(d)
    if err: return jsonify({"error": err}), 400
    dest, err = _backup_file_clause(d)
    if err: return jsonify({"error": err}), 400

    settings = []
    bt = d.get("backup_type", "full")
    if bt == "incremental":
        base_path = (d.get("base_backup_path") or "").strip().rstrip("/")
        base_name = (d.get("base_backup_name") or "").strip()
        if not base_path or not base_name:
            return jsonify({"error": "Incremental backup needs base_backup_path + base_backup_name"}), 400
        if "'" in base_path or "'" in base_name:
            return jsonify({"error": "base path/name may not contain quotes"}), 400
        if not base_name.endswith(".zip"):
            base_name = base_name + ".zip"
        settings.append(f"base_backup = File('{base_path}/{base_name}')")
    elif bt == "differential":
        # Auto-find the most recent full backup in the same directory.
        # The user supplies a glob to constrain which 'full' to pick —
        # typically the database name embedded in the filename, e.g.
        # 'mydb_full_*.zip'. If no full is found we refuse the request
        # rather than silently turning it into a full backup.
        bpath = (d.get("path") or "").strip().rstrip("/")
        glob = (d.get("base_search_glob") or "*_full_*.zip").strip()
        if "'" in bpath or "'" in glob:
            return jsonify({"error": "path/glob may not contain quotes"}), 400
        try:
            client_lookup = _get_client(d)
            found_path, found_name = _find_latest_full_backup(client_lookup, bpath, glob)
        except Exception as e:
            return jsonify({"error": f"could not query system.backups: {e}"}), 500
        if not found_name:
            return jsonify({"error":
                f"Differential needs a full backup as base, but none found matching '{glob}' in {bpath}. "
                "Run a full backup first, or check the search glob."}), 400
        settings.append(f"base_backup = File('{found_path}/{found_name}')")
        # Also tell the caller which full we picked so the UI can
        # surface it.
        d["_resolved_base"] = f"{found_path}/{found_name}"

    sql = f"BACKUP {target} TO {dest}"
    if settings:
        sql += " SETTINGS " + ", ".join(settings)
    sql += " ASYNC"

    try:
        client = _get_client(d)
        result = client.query(sql)
        rows = result.result_rows or []
        if not rows:
            return jsonify({"ok": True, "message": "backup started"})
        backup_id = str(rows[0][0])
        status = str(rows[0][1]) if len(rows[0]) > 1 else ""
        audit("Start Native Backup", panel="backup",
              detail=f"type={bt} target={target} dest={dest} id={backup_id} status={status}"
                     + (f" base={d.get('_resolved_base')}" if d.get("_resolved_base") else ""))
        resp = {"ok": True, "id": backup_id, "status": status, "sql": sql}
        if d.get("_resolved_base"):
            resp["resolved_base"] = d["_resolved_base"]
        return jsonify(resp)
    except Exception as e:
        logger.error(f"Native backup failed: {e}")
        audit("Start Native Backup", panel="backup",
              detail=f"type={bt} target={target} dest={dest} error={str(e)[:200]}",
              result="error")
        return jsonify({"ok": False, "error": str(e)[:500]}), 500


@app.route("/api/backup/native/list", methods=["POST"])
def backup_native_list():
    """Read system.backups for in-flight and recent operations."""
    d = request.json or {}
    try:
        client = _get_client(d)
        result = client.query("""
            SELECT id, name, status, error,
                   toString(start_time), toString(end_time),
                   num_files, total_size,
                   num_entries, compressed_size, uncompressed_size
            FROM system.backups
            ORDER BY start_time DESC
            LIMIT 200
        """)
        rows = []
        for r in (result.result_rows or []):
            rows.append({
                "id":                str(r[0]),
                "name":              str(r[1]) if r[1] else "",
                "status":            str(r[2]),
                "error":             str(r[3]) if r[3] else "",
                "start_time":        str(r[4]) if r[4] else "",
                "end_time":          str(r[5]) if r[5] else "",
                "num_files":         int(r[6] or 0),
                "total_size":        int(r[7] or 0),
                "num_entries":       int(r[8] or 0),
                "compressed_size":   int(r[9] or 0),
                "uncompressed_size": int(r[10] or 0),
            })
        return jsonify({"backups": rows})
    except Exception as e:
        return jsonify({"error": str(e)[:500]}), 500


@app.route("/api/backup/native/restore", methods=["POST"])
def backup_native_restore():
    """Start a native RESTORE FROM File(...).

    Supports renaming on restore via:
      - restore_as_db   : when target='database', restores 'db_name' AS this
      - restore_as_tables : when target='tables', a parallel list to 'tables'
                            with destination names (same length, same order)
    ClickHouse syntax: RESTORE DATABASE src AS dst FROM File(...).
    """
    d = request.json or {}

    dest, err = _backup_file_clause(d)
    if err: return jsonify({"error": err}), 400

    sel = (d.get("target") or "database").strip()
    if sel == "all":
        target = "ALL"
    elif sel == "database":
        db = (d.get("db_name") or "").strip()
        if not db: return jsonify({"error": "db_name required"}), 400
        restore_as = (d.get("restore_as_db") or "").strip()
        if restore_as and restore_as != db:
            # Rename on restore.
            target = f"DATABASE {_qident(db,'database')} AS {_qident(restore_as,'database')}"
        else:
            target = f"DATABASE {_qident(db,'database')}"
    elif sel == "tables":
        raw = (d.get("tables") or "").strip()
        if not raw: return jsonify({"error": "tables required"}), 400
        tables = [t.strip() for t in raw.replace(",", " ").split() if t.strip()]
        for t in tables:
            if "." not in t:
                return jsonify({"error": f"table '{t}' must be db.table"}), 400

        # Parse parallel rename list if supplied
        as_raw = (d.get("restore_as_tables") or "").strip()
        as_list = [t.strip() for t in as_raw.replace(",", " ").split() if t.strip()] if as_raw else []
        if as_list and len(as_list) != len(tables):
            return jsonify({"error":
                f"restore_as_tables ({len(as_list)} entries) must match tables ({len(tables)} entries)"
            }), 400
        for at in as_list:
            if "." not in at:
                return jsonify({"error": f"rename target '{at}' must be db.table"}), 400

        parts = []
        for i, t in enumerate(tables):
            sdb, stbl = t.split(".", 1)
            src = "TABLE " + _qident(sdb, "database") + "." + _qident(stbl, "table")
            if as_list:
                ddb, dtbl = as_list[i].split(".", 1)
                # Only emit AS if it actually differs
                if (sdb, stbl) != (ddb, dtbl):
                    parts.append(src + " AS " + _qident(ddb, "database")
                                 + "." + _qident(dtbl, "table"))
                else:
                    parts.append(src)
            else:
                parts.append(src)
        target = ", ".join(parts)
    else:
        return jsonify({"error": f"unknown target '{sel}'"}), 400

    settings = []
    if d.get("allow_non_empty"):
        settings.append("allow_non_empty_tables = true")
    if d.get("structure_only"):
        settings.append("structure_only = true")

    sql = f"RESTORE {target} FROM {dest}"
    if settings:
        sql += " SETTINGS " + ", ".join(settings)
    sql += " ASYNC"

    try:
        client = _get_client(d)
        result = client.query(sql)
        rows = result.result_rows or []
        if not rows:
            return jsonify({"ok": True, "message": "restore started"})
        restore_id = str(rows[0][0])
        status = str(rows[0][1]) if len(rows[0]) > 1 else ""
        audit("Start Native Restore", panel="backup",
              detail=f"target={target} dest={dest} id={restore_id}")
        return jsonify({"ok": True, "id": restore_id, "status": status, "sql": sql})
    except Exception as e:
        logger.error(f"Native restore failed: {e}")
        audit("Start Native Restore", panel="backup",
              detail=f"target={target} dest={dest} error={str(e)[:200]}",
              result="error")
        return jsonify({"ok": False, "error": str(e)[:500]}), 500


@app.route("/api/backup/native/databases", methods=["POST"])
def backup_native_databases():
    """List user databases to populate the UI dropdown."""
    d = request.json or {}
    try:
        client = _get_client(d)
        result = client.query("""
            SELECT name FROM system.databases
            WHERE name NOT IN ('system','INFORMATION_SCHEMA','information_schema')
            ORDER BY name
        """)
        dbs = [str(r[0]) for r in (result.result_rows or [])]
        return jsonify({"databases": dbs})
    except Exception as e:
        return jsonify({"error": str(e)[:500]}), 500


@app.route("/api/backup/native/kill", methods=["POST"])
def backup_native_kill():
    """Cancel an in-flight BACKUP or RESTORE. ClickHouse exposes this as
    KILL QUERY WHERE query_id = '<uuid>' — backup/restore operations
    appear in system.processes with the same id ClickHouse returned
    when the BACKUP/RESTORE was started.

    Idempotent: if the operation already finished (succeeded or failed),
    KILL is a no-op and we report what we found in system.backups
    rather than treating it as an error.
    """
    d = request.json or {}
    backup_id = (d.get("id") or "").strip()
    if not backup_id:
        return jsonify({"error": "id is required"}), 400
    # Defensive: id must look like a UUID — alphanumeric, dashes only.
    import re as _re
    if not _re.match(r'^[a-zA-Z0-9\-]{8,64}$', backup_id):
        return jsonify({"error": "invalid id format"}), 400

    try:
        client = _get_client(d)
        # First, see whether the operation is still running. system.backups
        # tracks both backups and restores in the same table; in-flight
        # statuses are CREATING_BACKUP / RESTORING.
        row = client.query(
            f"SELECT status FROM system.backups WHERE id='{backup_id}' LIMIT 1"
        ).result_rows or []
        if not row:
            return jsonify({"ok": False,
                "error": f"No backup or restore with id={backup_id} found in system.backups. "
                         "It may have completed and rolled out of the in-memory ring."}), 404
        status = str(row[0][0])
        terminal = ('BACKUP_CREATED', 'BACKUP_FAILED', 'RESTORED', 'RESTORE_FAILED')
        if status in terminal:
            return jsonify({"ok": False, "already_done": True, "status": status,
                            "message": f"Operation already finished with status={status}; nothing to cancel."}), 200

        # Issue the KILL. ClickHouse matches by query_id which equals the
        # backup id for BACKUP/RESTORE operations. SYNC waits for the
        # process to actually go away so we can report a clean status.
        kill_result = client.query(
            f"KILL QUERY WHERE query_id = '{backup_id}' SYNC"
        )
        # Result rows: (kill_status, query_id) — kill_status is 'finished',
        # 'waiting', or empty if no match was found.
        kr = kill_result.result_rows or []
        kill_status = str(kr[0][0]) if kr else "no-match"

        audit("Cancel Backup/Restore", panel="backup",
              detail=f"id={backup_id} previous_status={status} kill_status={kill_status}")
        return jsonify({
            "ok": True,
            "id": backup_id,
            "previous_status": status,
            "kill_status": kill_status,
        })
    except Exception as e:
        # The most common reason for KILL to fail is that the operation
        # already completed between our status check and the KILL itself —
        # treat that as a normal race, not an error.
        msg = str(e)[:300]
        logger.warning(f"Backup kill failed: {msg}")
        audit("Cancel Backup/Restore", panel="backup",
              detail=f"id={backup_id} error={msg}", result="error")
        return jsonify({"ok": False, "error": msg}), 500


@app.route("/api/backup/native/inspect", methods=["POST"])
def backup_native_inspect():
    """Inspect what's inside a backup file without restoring it. Uses
    RESTORE ... ON CLUSTER ... ASYNC=0 SETTINGS structure_only=true is
    NOT what we want here — that actually restores DDL. Instead we
    parse system.backups for the most recent BACKUP_CREATED row matching
    this file path and read its catalog (names of databases / tables
    it contains).

    This is best-effort: ClickHouse versions differ in how they expose
    catalog metadata. If the inspection fails, we still let the user
    proceed and rely on their input — we just can't pre-fill helpfully.
    """
    d = request.json or {}
    path = (d.get("path") or "").strip().rstrip("/")
    name = (d.get("name") or "").strip()
    if not path or not name:
        return jsonify({"error":"path and name required"}), 400
    if not name.endswith(".zip"): name = name + ".zip"
    full = f"{path}/{name}"
    try:
        client = _get_client(d)
        # Find the BACKUP_CREATED row that produced this file. Its
        # 'name' column is "File('...')" — match by substring.
        rows = client.query(
            f"SELECT id, status, num_entries FROM system.backups "
            f"WHERE position(name, '{full}') > 0 AND status = 'BACKUP_CREATED' "
            f"ORDER BY start_time DESC LIMIT 1"
        ).result_rows or []
        if not rows:
            return jsonify({"ok": False,
                "warning": f"No BACKUP_CREATED row found for {full} in system.backups. "
                           f"ClickHouse keeps only recent rows; for older backups, "
                           f"this lookup is unavailable."})
        # Try to enumerate contents via system.backup_log if available.
        # Most reliable cross-version path: do an ASYNC=0 RESTORE with
        # structure_only and ON CLUSTER '' to a temp namespace, but that's
        # destructive. Skip the deep introspection and just report metadata.
        return jsonify({
            "ok": True,
            "id":          str(rows[0][0]),
            "status":      str(rows[0][1]),
            "num_entries": int(rows[0][2] or 0),
            "note": "Detailed object listing inside the archive isn't queryable "
                    "without restoring; supply the original source database / "
                    "table name as it was at backup time."
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:500]})


@app.route("/api/restore/run",methods=["POST"])
def restore_run():
    d=request.json or {}
    args=["restore"]+_conn_args(d)+_store_args(d)
    mode=d.get("restore_mode","id")
    if mode=="id":
        bid=(d.get("backup_id") or "").strip()
        if not bid: return jsonify({"error":"Please enter a Backup ID."}),400
        args+=["--backup-id",bid]
    else:
        tt=(d.get("target_time") or "").strip()
        if not tt: return jsonify({"error":"Please enter a target time."}),400
        args+=["--target-time",tt]
    rt=(d.get("restore_tables") or "").strip()
    if rt: args+=["--tables"]+rt.split()
    m=d.get("method","restore")
    if m=="attach":
        args+=["--method","attach"]
        if d.get("ch_data_dir"): args+=["--ch-data-dir",d["ch_data_dir"]]
    if d.get("dry_run"): args.append("--dry-run")
    jid=_jnew()
    audit("Start Legacy Restore", panel="backup",
          detail=f"mode={mode} backup_id={d.get('backup_id','')[:80]} "
                 f"target_time={d.get('target_time','')} method={m} "
                 f"tables={(d.get('restore_tables') or '')[:120]} "
                 f"dry_run={bool(d.get('dry_run'))} job_id={jid}")
    threading.Thread(target=_run,args=("clickhouse_pitr.py",args,jid),daemon=True).start()
    return jsonify({"job_id":jid})

@app.route("/api/backup/list",methods=["POST"])
def backup_list():
    d=request.json or {}
    catalog=d.get("catalog_file","/var/lib/clickhouse/backups/catalog.json")
    try:
        p=Path(catalog)
        if not p.exists(): return jsonify({"entries":[],"warning":"Catalog not found: "+catalog})
        raw=json.loads(p.read_text("utf-8"))
        entries=raw.get("entries",raw) if isinstance(raw,dict) else raw
        sf=d.get("status_filter","")
        if sf: entries=[e for e in entries if e.get("status")==sf]
        entries=sorted(entries,key=lambda e:e.get("timestamp",""),reverse=True)
        return jsonify({"entries":entries[:int(d.get("limit",50))]})
    except Exception as e: return jsonify({"error":str(e),"entries":[]})

@app.route("/api/backup/verify",methods=["POST"])
def backup_verify():
    d=request.json or {}
    args=["verify"]+_conn_args(d)+_store_args(d)
    if d.get("verify_id"): args+=["--backup-id",d["verify_id"]]
    else:                  args.append("--all")
    jid=_jnew()
    audit("Legacy Backup Verify", panel="backup",
          detail=f"verify_id={d.get('verify_id','')[:80] or '(all)'} job_id={jid}")
    threading.Thread(target=_run,args=("clickhouse_pitr.py",args,jid),daemon=True).start()
    return jsonify({"job_id":jid})

@app.route("/api/backup/prune",methods=["POST"])
def backup_prune():
    d=request.json or {}
    args=["prune"]+_store_args(d)
    if d.get("keep_days"):  args+=["--keep-days",str(d["keep_days"])]
    if d.get("keep_count"): args+=["--keep-count",str(d["keep_count"])]
    if d.get("dry_run"):    args.append("--dry-run")
    jid=_jnew()
    audit("Legacy Backup Prune", panel="backup",
          detail=f"keep_days={d.get('keep_days','')} keep_count={d.get('keep_count','')} "
          f"dry_run={bool(d.get('dry_run'))} job_id={jid}")
    threading.Thread(target=_run,args=("clickhouse_pitr.py",args,jid),daemon=True).start()
    return jsonify({"job_id":jid})

@app.route("/api/backup/chain",methods=["POST"])
def backup_chain():
    d=request.json or {}
    args=["chain","--catalog-file",d.get("catalog_file","/var/lib/clickhouse/backups/catalog.json")]
    if d.get("chain_id"):   args+=["--backup-id",d["chain_id"]]
    if d.get("chain_time"): args+=["--target-time",d["chain_time"]]
    jid=_jnew()
    audit("Legacy Backup Chain Inspect", panel="backup",
          detail=f"chain_id={d.get('chain_id','')[:80]} chain_time={d.get('chain_time','')} job_id={jid}")
    threading.Thread(target=_run,args=("clickhouse_pitr.py",args,jid),daemon=True).start()
    return jsonify({"job_id":jid})

@app.route("/api/backup/schedule",methods=["POST"])
def backup_schedule():
    d=request.json or {}
    args=["schedule","--cron",d.get("cron","0 2 * * *")]+_store_args(d)
    sel=d.get("table_sel","tables")
    if   sel=="all":      args.append("--all-tables")
    elif sel=="database": args+=["--database",d.get("db_name","")]
    elif d.get("tables"): args+=["--tables"]+d["tables"].strip().split()
    bt=d.get("backup_type","full")
    if bt=="differential": args.append("--differential")
    elif bt=="incremental": args.append("--incremental")
    if d.get("tag"): args+=["--tag",d["tag"]]
    jid=_jnew()
    audit("Legacy Backup Cron Schedule", panel="backup",
          detail=f"cron={d.get('cron','')[:80]} type={bt} table_sel={sel} "
                 f"db={d.get('db_name','')} tag={d.get('tag','')[:60]} job_id={jid}")
    threading.Thread(target=_run,args=("clickhouse_pitr.py",args,jid),daemon=True).start()
    return jsonify({"job_id":jid})

# ── log reader ────────────────────────────────────────────────────────────────
DEFAULT_LOG="/var/log/clickhouse-server/clickhouse-server.log"

@app.route("/api/log/read",methods=["POST"])
def log_read():
    d=request.json or {}
    logger.info(f"LOG READ path={d.get('path','')} lines={d.get('lines',50000)}")
    path=d.get("path",DEFAULT_LOG); lines=int(d.get("lines",50000))
    try:
        result=subprocess.run(["tail","-n",str(lines),path],capture_output=True,text=True,timeout=30)
        if result.returncode!=0: return jsonify({"error":f"Cannot read {path}: {result.stderr.strip()}"})
        return jsonify({"content":result.stdout,"path":path,"line_count":result.stdout.count('\n')})
    except FileNotFoundError: return jsonify({"error":"'tail' not found."})
    except subprocess.TimeoutExpired: return jsonify({"error":"Reading log timed out."})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/log/default-path")
def log_default_path():
    import os as _os
    p = Path(DEFAULT_LOG)
    exists = p.exists()
    readable = False
    permission_denied = False
    if exists:
        try:
            with open(p, 'rb') as f: f.read(1)
            readable = True
        except PermissionError:
            permission_denied = True
        except Exception:
            pass
    return jsonify({
        "path": DEFAULT_LOG,
        "exists": exists,
        "readable": readable,
        "permission_denied": permission_denied
    })

@app.route("/api/console-log")
def console_log():
    lines=int(request.args.get("lines",200))
    try:
        # Console Log viewer always shows the current month's active file.
        # Archived months are on disk as gzipped files under logs/ for
        # forensic retrieval but are not surfaced in the live viewer.
        active = _server_log_path()
        if not active.exists(): return jsonify({"lines":[],"path":str(active)})
        result=subprocess.run(["tail","-n",str(lines),str(active)],capture_output=True,text=True,timeout=5)
        return jsonify({"lines":result.stdout.strip().splitlines(),"path":str(active)})
    except Exception as e: return jsonify({"error":str(e),"lines":[]})

# ── profiler ──────────────────────────────────────────────────────────────────
# Live mode: pulls metrics directly from ClickHouse system.query_log via SQL.
# Works regardless of where ClickHouse-Console runs vs the ClickHouse server.
# Requires: query_log enabled on the customer's ClickHouse (default: yes since 19.x).
@app.route("/api/profile/live",methods=["POST"])
def profile_live():
    d=request.json or {}
    hours=max(1, min(24*30, int(d.get("hours") or 1)))  # clamp 1h..30d
    top_n=max(1, min(500, int(d.get("top_n") or 10)))
    sort_field=d.get("sort_field","ExecTime")
    sort_op=d.get("sort_op","max")
    sort_order="DESC" if d.get("sort_order","desc").lower()=="desc" else "ASC"
    min_calls=max(1, int(d.get("min_calls") or 1))
    logger.info(f"PROFILE LIVE hours={hours} top_n={top_n} sort={sort_field}/{sort_op}/{sort_order}")
    cl=None
    try:
        cl=_get_client(d)
        # Build WHERE clauses + parameters
        where=["event_time >= now() - toIntervalHour(%(h)s)",
               "type IN ('QueryFinish','ExceptionBeforeStart','ExceptionWhileProcessing')"]
        params={"h": hours}
        # Filter keys (user_filter / host_filter) are kept distinct from
        # connection keys (user / host) used by _get_client so they don't collide.
        # Backwards compat: also accept the old 'host_filt' name.
        _user_f = (d.get("user_filter") or "").strip()
        _host_f = (d.get("host_filter") or d.get("host_filt") or "").strip()
        if _user_f:
            where.append("user = %(user_f)s"); params["user_f"]=_user_f
        if _host_f:
            where.append("client_hostname = %(host_f)s"); params["host_f"]=_host_f
        if d.get("query_filter"):
            where.append("match(query, %(qf)s)"); params["qf"]=d["query_filter"]
        if d.get("errors_only"):
            where.append("type IN ('ExceptionBeforeStart','ExceptionWhileProcessing')")
        if d.get("completed_only"):
            where.append("type = 'QueryFinish'")
        if float(d.get("min_duration") or 0) > 0:
            where.append("query_duration_ms >= %(mind)s"); params["mind"]=int(float(d["min_duration"])*1000)
        if float(d.get("max_duration") or 0) > 0:
            where.append("query_duration_ms <= %(maxd)s"); params["maxd"]=int(float(d["max_duration"])*1000)
        wsql=" AND ".join(where)
        # ORDER BY expression — sort within the aggregated groups
        sort_map={
            ("ExecTime",   "max"): "max(query_duration_ms)",
            ("ExecTime",   "sum"): "sum(query_duration_ms)",
            ("ExecTime",   "avg"): "avg(query_duration_ms)",
            ("ExecTime",   "min"): "min(query_duration_ms)",
            ("RowsRead",   "max"): "max(read_rows)",
            ("RowsRead",   "sum"): "sum(read_rows)",
            ("RowsRead",   "avg"): "avg(read_rows)",
            ("BytesRead",  "max"): "max(read_bytes)",
            ("BytesRead",  "sum"): "sum(read_bytes)",
            ("BytesRead",  "avg"): "avg(read_bytes)",
            ("PeakMemory", "max"): "max(memory_usage)",
            ("PeakMemory", "sum"): "sum(memory_usage)",
            ("PeakMemory", "avg"): "avg(memory_usage)",
            ("QueryCount", "max"): "count()",
            ("QPS",        "max"): "count()",  # qps proportional to count over fixed window
        }
        order_expr=sort_map.get((sort_field,sort_op), "max(query_duration_ms)")
        # Aggregate per normalized_query_hash (ClickHouse normalises queries server-side)
        group_sql=f"""
            SELECT
                normalized_query_hash,
                any(query)                                         AS sample_query,
                count()                                            AS query_count,
                countIf(type != 'QueryFinish')                     AS error_count,
                max(query_duration_ms)/1000.0                      AS max_dur_sec,
                avg(query_duration_ms)/1000.0                      AS avg_dur_sec,
                sum(read_rows)                                     AS total_rows,
                sum(read_bytes)                                    AS total_bytes,
                max(memory_usage)                                  AS peak_mem,
                arraySlice(groupUniqArray(user), 1, 10)            AS users,
                arraySlice(groupUniqArray(client_hostname), 1, 10) AS hosts
            FROM system.query_log
            WHERE {wsql}
            GROUP BY normalized_query_hash
            HAVING query_count >= %(mc)s
            ORDER BY {order_expr} {sort_order}
            LIMIT %(top)s
        """
        params2=dict(params); params2["mc"]=min_calls; params2["top"]=top_n
        rows=cl.query(group_sql, parameters=params2).result_rows
        # Totals over the same window/filters
        totals_sql=f"""
            SELECT
                count()                                AS total_queries,
                countIf(type != 'QueryFinish')         AS total_errors,
                max(query_duration_ms)/1000.0          AS max_dur,
                avg(query_duration_ms)/1000.0          AS avg_dur
            FROM system.query_log
            WHERE {wsql}
        """
        t=cl.query(totals_sql, parameters=params).result_rows
        if t:
            tq, te, tmax, tavg = int(t[0][0]), int(t[0][1]), float(t[0][2] or 0), float(t[0][3] or 0)
        else:
            tq=te=0; tmax=tavg=0.0
        seconds_in_window=hours*3600
        return jsonify({
            "mode": "live",
            "source": "system.query_log",
            "window_hours": hours,
            "total_lines": tq,           # for UI compatibility with paste mode
            "total_queries": tq,
            "total_errors": te,
            "max_dur": tmax,
            "avg_dur": tavg,
            "groups":[{
                "rank":      i+1,
                "sample":    r[1],
                "count":     int(r[2]),
                "qps":       round(int(r[2]) / max(seconds_in_window, 1), 4),
                "max_dur":   float(r[4] or 0),
                "avg_dur":   float(r[5] or 0),
                "total_rows":int(r[6] or 0),
                "total_bytes":int(r[7] or 0),
                "peak_mem":  int(r[8] or 0),
                "errors":    int(r[3]),
                "users":     list(r[9] or []),
                "hosts":     list(r[10] or []),
            } for i,r in enumerate(rows)]
        })
    except Exception as e:
        msg=str(e)
        # Helpful hint when query_log isn't enabled
        if "system.query_log" in msg or "doesn't exist" in msg:
            msg=("system.query_log not available on this ClickHouse server. "
                 "Enable it in the server config: <query_log><database>system</database><table>query_log</table>"
                 "<flush_interval_milliseconds>7500</flush_interval_milliseconds></query_log>")
        return jsonify({"error": msg}), 500
    finally:
        try:
            if cl: cl.close()
        except Exception: pass

# ── profiler (legacy paste / auto-read modes — kept for offline log analysis) ──
@app.route("/api/profile/analyze",methods=["POST"])
def profile_analyze():
    d=request.json or {}
    log_text=d.get("log_text","")
    logger.info(f"PROFILE ANALYZE top_n={d.get('top_n',10)} lines={len(log_text.splitlines())}")
    if not log_text.strip(): return jsonify({"error":"Log text is empty"}),400
    min_dur=float(d.get("min_duration") or 0); max_dur=float(d.get("max_duration") or 0)
    top_n=int(d.get("top_n",10)); sort_field=d.get("sort_field","ExecTime")
    sort_op=d.get("sort_op","max"); sort_order=d.get("sort_order","desc"); min_calls=int(d.get("min_calls",1))
    try:
        sys.path.insert(0,str(APP_DIR))
        from clickhouse_profiler import ClickHouseLogParser,group_queries
        import io as _io
        parser=ClickHouseLogParser()
        for line in _io.StringIO(log_text):
            parser.total_lines+=1; parser._process_line(line.rstrip("\n"))
        queries=parser.all_queries
        if min_dur>0:  queries=[q for q in queries if q.duration_sec>=min_dur]
        if max_dur>0:  queries=[q for q in queries if q.duration_sec<=max_dur]
        if d.get("user"):       queries=[q for q in queries if q.user==d["user"]]
        if d.get("host_filt"):  queries=[q for q in queries if q.client_host==d["host_filt"]]
        if d.get("errors_only"):    queries=[q for q in queries if q.has_error]
        if d.get("completed_only"): queries=[q for q in queries if q.completed]
        if d.get("query_filter"):
            pat=re.compile(d["query_filter"],re.IGNORECASE)
            queries=[q for q in queries if pat.search(q.query)]
        if d.get("from_time"):
            from datetime import datetime; dt=datetime.fromisoformat(d["from_time"])
            queries=[q for q in queries if q.timestamp and q.timestamp>=dt]
        if d.get("to_time"):
            from datetime import datetime; dt=datetime.fromisoformat(d["to_time"])
            queries=[q for q in queries if q.timestamp and q.timestamp<=dt]
        groups=group_queries(queries)
        def sval(g):
            v=[q.duration_sec for q in g.queries]
            if sort_field=="RowsRead":    v=[float(q.rows_read) for q in g.queries]
            elif sort_field=="BytesRead": v=[q.bytes_read for q in g.queries]
            elif sort_field=="PeakMemory":v=[q.peak_memory_bytes for q in g.queries]
            elif sort_field=="QPS":       return g.qps
            elif sort_field=="QueryCount":return float(g.count)
            if not v: return 0
            if sort_op=="sum": return sum(v)
            if sort_op=="min": return min(v)
            if sort_op=="avg": return sum(v)/len(v)
            return max(v)
        grp=sorted([g for g in groups.values() if g.count>=min_calls],key=sval,reverse=(sort_order=="desc"))[:top_n]
        all_d=[q.duration_sec for q in queries]
        return jsonify({"total_lines":parser.total_lines,"total_queries":len(queries),
            "total_errors":sum(1 for q in queries if q.has_error),
            "max_dur":max(all_d) if all_d else 0,"avg_dur":sum(all_d)/len(all_d) if all_d else 0,
            "groups":[{"rank":i+1,"sample":g.queries[0].query if g.queries else "",
                "count":g.count,"qps":round(g.qps,3),
                "max_dur":max(q.duration_sec for q in g.queries),
                "avg_dur":sum(q.duration_sec for q in g.queries)/g.count,
                "total_rows":sum(q.rows_read for q in g.queries),
                "total_bytes":sum(q.bytes_read for q in g.queries),
                "peak_mem":max(q.peak_memory_bytes for q in g.queries),
                "errors":g.error_count,"users":list(g.users.keys()),"hosts":list(g.hosts.keys()),
            } for i,g in enumerate(grp)]})
    except ImportError: return jsonify({"error":"clickhouse_profiler.py not found."}),500
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/profile/export",methods=["POST"])
def profile_export():
    import tempfile
    d=request.json or {}; log_text=d.get("log_text",""); fmt=d.get("format","html"); top_n=str(d.get("top_n",10))
    if not log_text.strip(): return jsonify({"error":"No log text to export."}),400
    valid={"html","json","csv","text","md"}
    if fmt not in valid: return jsonify({"error":f"Invalid format '{fmt}'"}),400
    ext_map={"html":"html","json":"json","csv":"csv","text":"txt","md":"md"}
    log_path=out_path=None
    try:
        fd,log_path=tempfile.mkstemp(suffix=".log")
        with os.fdopen(fd,"w",encoding="utf-8") as f: f.write(log_text)
        fd2,out_path=tempfile.mkstemp(suffix="."+ext_map[fmt]); os.close(fd2)
        cmd=[sys.executable,str(APP_DIR/"clickhouse_profiler.py"),log_path,"-n",top_n,"-r",fmt,"-o",out_path]
        if d.get("min_duration"): cmd+=["--min-duration",str(d["min_duration"])]
        if d.get("user"):         cmd+=["--user",d["user"]]
        result=subprocess.run(cmd,capture_output=True,text=True,cwd=str(APP_DIR),timeout=120)
        if result.returncode!=0: return jsonify({"error":(result.stderr or "Export failed").strip()}),500
        with open(out_path,"r",encoding="utf-8",errors="replace") as f: content=f.read()
        if not content.strip(): return jsonify({"error":"Profiler produced empty output."}),500
        return jsonify({"content":content,"format":fmt,"ext":ext_map[fmt]})
    except subprocess.TimeoutExpired: return jsonify({"error":"Export timed out."}),500
    except Exception as e: return jsonify({"error":str(e)}),500
    finally:
        for p in (log_path,out_path):
            if p:
                try: os.unlink(p)
                except: pass

# ── branch ────────────────────────────────────────────────────────────────────
# Pure SQL implementation using ATTACH PARTITION FROM.
# ClickHouse server itself does the hard-link copy server-side; we never
# touch the ClickHouse data directory from this process. Works regardless of
# where ClickHouse-Console runs (same host, separate host, separate VPC).
@app.route("/api/branch/create",methods=["POST"])
def branch_create():
    d=request.json or {}; jid=_jnew()
    def _do():
        cl=None
        try:
            db=_safe_ident(d.get("database","default"),"database"); tbl=_safe_ident(d.get("table",""),"table"); br=_safe_branch(d.get("branch_name",""))
            if not tbl or not br:
                _ja(jid,"err","Table and branch name required"); _jdone(jid,False); return
            if not re.match(r'^[A-Za-z0-9_]+$', br):
                _ja(jid,"err","Branch name must contain only letters, digits, underscore"); _jdone(jid,False); return
            cl=_get_client(d)
            src=f"`{db}`.`{tbl}`"
            bname=f"{tbl}_{br}"
            dst=f"`{db}`.`{bname}`"
            logger.info(f"BRANCH CREATE {db}.{tbl} -> {db}.{bname} job={jid}")

            _ja(jid,"info",f"[1/4] Verifying source table: {db}.{tbl}")
            rows=cl.query(f"SELECT engine FROM system.tables WHERE database=%(d)s AND name=%(t)s",
                          parameters={"d":db,"t":tbl}).result_rows
            if not rows:
                _ja(jid,"err",f"Source table not found: {db}.{tbl}"); _jdone(jid,False); return
            engine=rows[0][0]
            if not any(k in engine for k in ("MergeTree","Replicated")):
                _ja(jid,"err",f"Branching requires a *MergeTree engine; got '{engine}'.")
                _ja(jid,"info","ATTACH PARTITION FROM only works between MergeTree-family tables.")
                _jdone(jid,False); return
            _ja(jid,"ok",f"      Engine: {engine}")

            _ja(jid,"info",f"[2/4] Checking branch table does not exist: {db}.{bname}")
            rows=cl.query(f"SELECT 1 FROM system.tables WHERE database=%(d)s AND name=%(t)s",
                          parameters={"d":db,"t":bname}).result_rows
            if rows:
                _ja(jid,"err",f"Branch table already exists: {db}.{bname}. Drop it first or pick a different branch name.")
                _jdone(jid,False); return
            _ja(jid,"ok","      Clear")

            _ja(jid,"info",f"[3/4] Creating empty branch table: {dst} (same schema as {src})")
            # CREATE TABLE ... AS source — copies schema (engine, partitioning, ordering, TTL).
            # Branch table is initially empty.
            cl.command(f"CREATE TABLE {dst} AS {src}")
            _ja(jid,"ok",f"      {db}.{bname} created (empty)")

            _ja(jid,"info",f"[4/4] Attaching partitions from {src} (zero-copy hard-links)")
            # Discover active partitions (excluding the synthetic 'tuple()' for non-partitioned tables).
            rows=cl.query(
                "SELECT DISTINCT partition_id FROM system.parts "
                "WHERE database=%(d)s AND table=%(t)s AND active "
                "ORDER BY partition_id",
                parameters={"d":db,"t":tbl}
            ).result_rows
            parts=[r[0] for r in rows]
            if not parts:
                _ja(jid,"warn","      Source table has no active parts — branch table remains empty.")
            else:
                _ja(jid,"info",f"      Found {len(parts)} partition(s)")
                for i,pid in enumerate(parts,1):
                    # Quote partition_id as a SQL string. ClickHouse accepts ID-form via:
                    #   ALTER TABLE dst ATTACH PARTITION ID 'X' FROM src
                    cl.command(f"ALTER TABLE {dst} ATTACH PARTITION ID %(pid)s FROM {src}",
                               parameters={"pid": pid})
                    _ja(jid,"ok",f"      [{i}/{len(parts)}] partition_id='{pid}' attached")
                _ja(jid,"ok",f"      All {len(parts)} partition(s) attached — branch is ready")

            # Quick row-count sanity check for the operator.
            try:
                src_n=cl.query(f"SELECT count() FROM {src}").result_rows[0][0]
                dst_n=cl.query(f"SELECT count() FROM {dst}").result_rows[0][0]
                _ja(jid,"info",f"      Row count — source: {src_n:,}  branch: {dst_n:,}")
            except Exception:
                pass

            logger.info(f"BRANCH OK  {db}.{bname} job={jid}")
            _ja(jid,"ok",f"Branch complete: {db}.{bname}")
            _jdone(jid,True)
        except Exception as e:
            _ja(jid,"err",f"Error: {e}"); _jdone(jid,False,str(e))
        finally:
            try:
                if cl: cl.close()
            except Exception: pass
    threading.Thread(target=_do,daemon=True).start()
    return jsonify({"job_id":jid})

@app.route("/api/branch/drop",methods=["POST"])
def branch_drop():
    """Drop a branch table. Pure SQL — no filesystem cleanup needed because
    ATTACH PARTITION uses ClickHouse-managed hard links that the server tracks
    in its own metadata. DROP TABLE removes the table and decrements link refs."""
    d=request.json or {}; jid=_jnew()
    def _do():
        cl=None
        try:
            cl=_get_client(d)
            db=_safe_ident(d.get("database","default"),"database"); tbl=_safe_ident(d.get("table",""),"table"); br=_safe_branch(d.get("branch_name",""))
            if not tbl or not br:
                _ja(jid,"err","Table and branch name required"); _jdone(jid,False); return
            bname=f"{tbl}_{br}"
            bfqn=f"`{db}`.`{bname}`"
            logger.info(f"BRANCH DROP {db}.{bname} job={jid}")
            _ja(jid,"warn",f"Dropping: {db}.{bname}")
            cl.command(f"DROP TABLE IF EXISTS {bfqn}")
            _ja(jid,"ok",f"{db}.{bname} dropped — server reclaims unreferenced parts on next merge")
            _jdone(jid,True)
        except Exception as e:
            _ja(jid,"err",str(e)); _jdone(jid,False,str(e))
        finally:
            try:
                if cl: cl.close()
            except Exception: pass
    threading.Thread(target=_do,daemon=True).start()
    return jsonify({"job_id":jid})

# ── query panel ───────────────────────────────────────────────────────────────
@app.route("/api/query/databases",methods=["POST"])
def query_databases():
    d=request.json or {}
    try:
        cl=_get_client(d)
        rows=cl.query("SELECT name FROM system.databases WHERE name NOT IN ('system','information_schema','INFORMATION_SCHEMA') ORDER BY name").result_rows
        cl.close(); return jsonify({"databases":[r[0] for r in rows]})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/query/tables",methods=["POST"])
def query_tables():
    d=request.json or {}; db=_safe_ident(d.get("database",""),"database")
    if not db: return jsonify({"error":"No database"})
    try:
        cl=_get_client(d)
        rows=cl.query(f"SELECT name,engine FROM system.tables WHERE database='{db}' ORDER BY name").result_rows
        cl.close(); return jsonify({"tables":[{"name":r[0],"engine":r[1]} for r in rows]})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/query/run",methods=["POST"])
def query_run():
    import time
    d=request.json or {}; sql=(d.get("sql") or "").strip()
    if not sql: return jsonify({"error":"No SQL"}),400
    jid=str(uuid.uuid4())[:8]
    try:
        job_store.qjob_create(jid)
    except Exception as e:
        # Redis unreachable — fail fast with a clear message instead of
        # spawning a thread whose result nobody could ever poll.
        return jsonify({"error":f"job store unreachable: {e}"}),503
    sql_preview=sql[:100].replace("\n"," ")
    logger.info(f"QUERY RUN  job={jid} sql={sql_preview}")
    # Snapshot user + connection context for history record
    _hist_user = getattr(g,"user",None)
    _hist_user_id = _hist_user["id"] if _hist_user else None
    _hist_username = _hist_user["username"] if _hist_user else None
    _hist_host = d.get("host","")
    _hist_port = str(d.get("port",""))
    _hist_chuser = d.get("user","")
    # Run All sends a shared batch_id with every statement so the history
    # endpoint can fold them into a single logical "Run All" entry.
    _hist_batch_id = (d.get("batch_id") or "")[:64] or None
    def _do():
        try:
            cl=_get_client(d); t0=time.monotonic()
            st={k:v for k,v in (d.get("settings") or {}).items() if str(v).strip()}
            # Cap rows at the engine so a huge SELECT (e.g. 60M rows) returns the
            # first N instead of trying to stream the whole table into memory
            # (which used to time out / OOM and surface as an empty result).
            # result_overflow_mode='break' stops cleanly at the cap with no error.
            # The user can override either setting from the Settings panel.
            st.setdefault("max_result_rows", str(RESULT_ROW_CAP))
            st.setdefault("result_overflow_mode", "break")
            # Assign an explicit query_id so the frontend can later
            # pivot this run into the Query Analyzer panel by id.
            # clickhouse-connect 0.5+ supports a query_id kwarg; on
            # older versions we fall back to letting the server
            # generate one and read it from the response.
            ch_query_id = str(uuid.uuid4())
            try:
                if st:
                    result = cl.query(sql, query_id=ch_query_id, settings=st)
                else:
                    result = cl.query(sql, query_id=ch_query_id)
            except TypeError:
                # Older clickhouse-connect — query_id kwarg unknown.
                # Run without it; harvest whatever id the server set.
                result = cl.query(sql, settings=st) if st else cl.query(sql)
                ch_query_id = getattr(result, 'query_id', None) or ''
            elapsed=round(time.monotonic()-t0,3)
            # Engine-side scan stats from the X-ClickHouse-Summary header.
            # These are the *real* cost figures — bytes_read can be GBs
            # even when total_rows is small (e.g. SELECT count() with
            # no index, full-scan filter). Surfacing them lets the
            # operator see what their query actually cost the cluster.
            _summary = getattr(result, 'summary', None) or {}
            try:
                _read_rows  = int(_summary.get('read_rows')  or 0)
                _read_bytes = int(_summary.get('read_bytes') or 0)
            except (TypeError, ValueError):
                _read_rows, _read_bytes = 0, 0
            cl.close()
            if job_store.qjob_status(jid)=="cancelled": return
            rows=result.result_rows
            try: cols=list(result.column_names)
            except:
                try: cols=[c.name if hasattr(c,"name") else str(c) for c in result.column_names]
                except: cols=[f"col{i}" for i in range(len(rows[0]) if rows else 0)]
            safe_rows=[[_to_json(c) for c in row] for row in rows[:RESULT_ROW_CAP]]
            logger.info(f"QUERY OK   job={jid} ch_qid={ch_query_id} rows={len(rows)} elapsed={elapsed}s read_bytes={_read_bytes}")
            job_store.qjob_set(jid,{"status":"ok","error":None,"columns":cols,"rows":safe_rows,
                "total_rows":len(rows),"elapsed":elapsed,"truncated":len(rows)>=RESULT_ROW_CAP,
                "read_rows":_read_rows,"read_bytes":_read_bytes,
                "query_id":ch_query_id})
            # Persist to query_history (Postgres, user_id-scoped)
            try:
                with app.app_context():
                    if _hist_username:
                        dbu = db_user(_hist_username)
                        if dbu:
                            dbu.execute(
                                "INSERT INTO query_history(user_id,conn_host,conn_port,conn_user,sql,duration_ms,rows_returned,error,job_id,batch_id)"
                                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (dbu.user_id,_hist_host,_hist_port,_hist_chuser,sql,int(elapsed*1000),len(rows),None,jid,_hist_batch_id)
                            )
                            dbu.commit()
            except Exception as he:
                logger.warning(f"query_history insert skipped: {he}")
        except Exception as e:
            if job_store.qjob_status(jid)!="cancelled":
                logger.error(f"QUERY FAIL job={jid} error={e}")
                job_store.qjob_set(jid,{"status":"error","error":str(e),"rows":0})
            # Persist failure too so the user can find what didn't work
            try:
                with app.app_context():
                    if _hist_username:
                        dbu = db_user(_hist_username)
                        if dbu:
                            dbu.execute(
                                "INSERT INTO query_history(user_id,conn_host,conn_port,conn_user,sql,duration_ms,rows_returned,error,job_id,batch_id)"
                                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (dbu.user_id,_hist_host,_hist_port,_hist_chuser,sql,None,None,str(e)[:500],jid,_hist_batch_id)
                            )
                            dbu.commit()
            except: pass
    threading.Thread(target=_do,daemon=True).start()
    return jsonify({"job_id":jid})

MAX_PAGE_SIZE = int(os.environ.get("QUERY_PAGE_MAX", "1000"))
_PAGINATE_SELECT_RE = re.compile(r'^\s*(with|select)\b', re.IGNORECASE | re.DOTALL)

# Upper bound on rows held in a server-side snapshot (see /api/query/snapshot).
# A snapshot materialises the query once into Redis so it can be paged through
# consistently; this caps how deep that frozen view goes. Beyond it the query
# returns the first N rows flagged as capped, and the operator is expected to
# refine the query (WHERE / ORDER BY / LIMIT) rather than browse millions of
# rows a page at a time.
SNAPSHOT_MAX_ROWS = int(os.environ.get("QUERY_SNAPSHOT_MAX_ROWS", "50000"))


def _browsable_sql(sql):
    """Return a cleaned single read query suitable for snapshot browsing, or
    raise ValueError. Same guard as _paginate_sql: rejects writes, DDL, and
    multi-statement batches so browsing can never alter data or run only part
    of a batch."""
    s = (sql or "").strip()
    s = re.sub(r';\s*$', '', s).strip()
    if not s:
        raise ValueError("empty query")
    if ';' in s:
        raise ValueError("browsing supports a single statement only")
    if not _PAGINATE_SELECT_RE.match(s):
        raise ValueError("browsing supports SELECT / WITH queries only")
    return s

def _paginate_sql(sql, page, page_size):
    """Wrap a single read query so the server returns one page of rows via
    LIMIT/OFFSET. Returns (wrapped_sql, page, page_size). Raises ValueError when
    the statement is not a single SELECT/WITH (a write, DDL, or multiple
    statements), so pagination can never alter a write or run only part of a
    multi-statement batch — the caller then falls back to a normal run."""
    s = (sql or "").strip()
    s = re.sub(r';\s*$', '', s).strip()   # drop one trailing ';'
    if not s:
        raise ValueError("empty query")
    if ';' in s:
        raise ValueError("pagination supports a single statement only")
    if not _PAGINATE_SELECT_RE.match(s):
        raise ValueError("pagination supports SELECT / WITH queries only")
    try:
        page = max(0, int(page)); page_size = int(page_size)
    except (TypeError, ValueError):
        raise ValueError("invalid page or page_size")
    if page_size < 1:
        page_size = 100
    page_size = min(page_size, MAX_PAGE_SIZE)
    offset = page * page_size
    # +1 row is fetched to detect whether a further page exists.
    wrapped = f"SELECT * FROM (\n{s}\n) AS _page LIMIT {page_size + 1} OFFSET {offset}"
    return wrapped, page, page_size


@app.route("/api/query/page", methods=["POST"])
def query_page():
    """Server-side pagination for a read query. Re-runs the query wrapped in
    LIMIT/OFFSET so the operator can browse past the materialisation cap without
    the full result set ever being held in memory at once — only one page (plus
    a single sentinel row) is fetched per request. Reuses the /api/query/run job
    poll mechanism; pages are navigation, so they are not written to history."""
    import time
    d = request.json or {}
    sql = (d.get("sql") or "").strip()
    if not sql:
        return jsonify({"error": "No SQL"}), 400
    try:
        wrapped, page, page_size = _paginate_sql(sql, d.get("page", 0), d.get("page_size", 100))
    except ValueError as e:
        return jsonify({"error": str(e), "paginable": False}), 400
    jid = str(uuid.uuid4())[:8]
    try:
        job_store.qjob_create(jid)
    except Exception as e:
        return jsonify({"error": f"job store unreachable: {e}"}), 503

    def _do():
        try:
            cl = _get_client(d); t0 = time.monotonic()
            st = {k: v for k, v in (d.get("settings") or {}).items() if str(v).strip()}
            # Bound memory to one page (+1 sentinel); no global cap re-applied.
            st["max_result_rows"] = str(page_size + 1)
            st["result_overflow_mode"] = "break"
            ch_query_id = str(uuid.uuid4())
            try:
                result = cl.query(wrapped, query_id=ch_query_id, settings=st)
            except TypeError:
                result = cl.query(wrapped, settings=st)
                ch_query_id = getattr(result, 'query_id', None) or ''
            elapsed = round(time.monotonic() - t0, 3)
            _summary = getattr(result, 'summary', None) or {}
            try:
                _read_rows = int(_summary.get('read_rows') or 0)
                _read_bytes = int(_summary.get('read_bytes') or 0)
            except (TypeError, ValueError):
                _read_rows, _read_bytes = 0, 0
            cl.close()
            if job_store.qjob_status(jid) == "cancelled":
                return
            rows = result.result_rows
            try:
                cols = list(result.column_names)
            except Exception:
                cols = [f"col{i}" for i in range(len(rows[0]) if rows else 0)]
            has_more = len(rows) > page_size
            page_rows = rows[:page_size]
            safe_rows = [[_to_json(c) for c in row] for row in page_rows]
            logger.info(f"QUERY PAGE job={jid} page={page} size={page_size} "
                        f"rows={len(page_rows)} more={has_more} elapsed={elapsed}s")
            job_store.qjob_set(jid, {
                "status": "ok", "error": None, "columns": cols, "rows": safe_rows,
                "total_rows": len(page_rows), "elapsed": elapsed,
                "truncated": False, "paged": True,
                "page": page, "page_size": page_size, "has_more": has_more,
                "row_offset": page * page_size,
                "read_rows": _read_rows, "read_bytes": _read_bytes,
                "query_id": ch_query_id})
        except Exception as e:
            if job_store.qjob_status(jid) != "cancelled":
                logger.error(f"QUERY PAGE FAIL job={jid} error={e}")
                job_store.qjob_set(jid, {"status": "error", "error": str(e), "rows": 0})

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/query/snapshot", methods=["POST"])
def query_snapshot():
    """Create a frozen, server-side snapshot of a read query and return its
    first page. The query runs ONCE (capped at SNAPSHOT_MAX_ROWS) and the rows
    are stored in Redis; subsequent pages come from that single execution via
    /api/query/snapshot/page, so browsing is consistent (no per-page re-run,
    no OFFSET re-scan) and any worker can serve any page. Reuses the run-job
    poll mechanism for the (potentially slow) creation step. Not written to
    history — browsing is navigation, not a new query of record."""
    import time
    d = request.json or {}
    sql = (d.get("sql") or "").strip()
    if not sql:
        return jsonify({"error": "No SQL"}), 400
    try:
        clean = _browsable_sql(sql)
    except ValueError as e:
        return jsonify({"error": str(e), "paginable": False}), 400
    try:
        page_size = int(d.get("page_size", 100))
    except (TypeError, ValueError):
        page_size = 100
    if page_size < 1:
        page_size = 100
    page_size = min(page_size, MAX_PAGE_SIZE)
    sid = str(uuid.uuid4())[:12]
    jid = str(uuid.uuid4())[:8]
    try:
        job_store.qjob_create(jid)
    except Exception as e:
        return jsonify({"error": f"job store unreachable: {e}"}), 503

    def _do():
        try:
            cl = _get_client(d); t0 = time.monotonic()
            st = {k: v for k, v in (d.get("settings") or {}).items() if str(v).strip()}
            # +1 sentinel row so we can tell the snapshot was capped (the real
            # result had more rows than we kept). 'break' stops cleanly at the
            # cap without raising.
            st["max_result_rows"] = str(SNAPSHOT_MAX_ROWS + 1)
            st["result_overflow_mode"] = "break"
            ch_query_id = str(uuid.uuid4())
            try:
                result = cl.query(clean, query_id=ch_query_id, settings=st)
            except TypeError:
                result = cl.query(clean, settings=st)
                ch_query_id = getattr(result, 'query_id', None) or ''
            elapsed = round(time.monotonic() - t0, 3)
            _summary = getattr(result, 'summary', None) or {}
            try:
                _read_rows = int(_summary.get('read_rows') or 0)
                _read_bytes = int(_summary.get('read_bytes') or 0)
            except (TypeError, ValueError):
                _read_rows, _read_bytes = 0, 0
            cl.close()
            if job_store.qjob_status(jid) == "cancelled":
                return
            rows = result.result_rows
            try:
                cols = list(result.column_names)
            except Exception:
                cols = [f"col{i}" for i in range(len(rows[0]) if rows else 0)]
            capped = len(rows) > SNAPSHOT_MAX_ROWS
            keep = rows[:SNAPSHOT_MAX_ROWS]
            safe_rows = [[_to_json(c) for c in row] for row in keep]
            total = len(safe_rows)
            # Persist the snapshot: metadata first, then rows in batches.
            job_store.snapshot_init(sid, {
                "columns": cols, "total": total, "capped": capped,
                "query_id": ch_query_id, "read_rows": _read_rows,
                "read_bytes": _read_bytes, "elapsed": elapsed})
            _BATCH = 2000
            for i in range(0, total, _BATCH):
                job_store.snapshot_push(sid, safe_rows[i:i + _BATCH])
            job_store.snapshot_finalize(sid)
            first = safe_rows[:page_size]
            logger.info(f"SNAPSHOT OK job={jid} sid={sid} rows={total} "
                        f"capped={capped} elapsed={elapsed}s")
            job_store.qjob_set(jid, {
                "status": "ok", "error": None,
                "snapshot": True, "snapshot_id": sid, "paged": True,
                "columns": cols, "rows": first,
                "page": 0, "page_size": page_size, "row_offset": 0,
                "has_more": total > page_size,
                "total_rows": total, "snapshot_total": total, "capped": capped,
                "read_rows": _read_rows, "read_bytes": _read_bytes,
                "elapsed": elapsed, "query_id": ch_query_id})
        except Exception as e:
            if job_store.qjob_status(jid) != "cancelled":
                logger.error(f"SNAPSHOT FAIL job={jid} error={e}")
                job_store.qjob_set(jid, {"status": "error", "error": str(e), "rows": 0})

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/query/snapshot/page", methods=["POST"])
def query_snapshot_page():
    """Return one page of an existing snapshot. Synchronous: a snapshot page is
    a single Redis LRANGE, so there is no job/thread — the response carries the
    rows directly. 410 if the snapshot has expired (the caller re-creates it)."""
    d = request.json or {}
    sid = (d.get("snapshot_id") or "").strip()
    if not sid:
        return jsonify({"error": "No snapshot_id"}), 400
    try:
        page = max(0, int(d.get("page", 0)))
        page_size = int(d.get("page_size", 100))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid page or page_size"}), 400
    if page_size < 1:
        page_size = 100
    page_size = min(page_size, MAX_PAGE_SIZE)
    try:
        meta = job_store.snapshot_get_meta(sid)
    except Exception as e:
        return jsonify({"error": f"snapshot store unreachable: {e}"}), 503
    if not meta:
        return jsonify({"error": "snapshot expired", "expired": True}), 410
    total = int(meta.get("total", 0))
    offset = page * page_size
    try:
        rows = job_store.snapshot_get_page(sid, offset, page_size)
    except Exception as e:
        return jsonify({"error": f"snapshot store unreachable: {e}"}), 503
    return jsonify({
        "snapshot": True, "snapshot_id": sid, "paged": True,
        "columns": meta.get("columns", []), "rows": rows,
        "page": page, "page_size": page_size, "row_offset": offset,
        "has_more": (offset + len(rows)) < total,
        "total_rows": total, "snapshot_total": total,
        "capped": bool(meta.get("capped", False)),
        "read_rows": meta.get("read_rows", 0),
        "read_bytes": meta.get("read_bytes", 0),
        "elapsed": meta.get("elapsed", 0),
        "query_id": meta.get("query_id", "")})


@app.route("/api/query-history",methods=["GET"])
def query_history_list():
    """Return the current user's query history scoped to a connection
    (host:port). Statements that were part of a Run All (sharing a
    batch_id) are folded into a single entry whose `sql` is the joined
    statement text, with `statement_count`, the sum of durations, the
    sum of rows, and an `is_batch:true` marker. Used by the toolbar
    History dropdown so a Run All shows up as ONE entry rather than N.
    """
    token=request.cookies.get(SESSION_COOKIE_NAME)
    user=get_session_user(token) if token else None
    if not user: return jsonify({"error":"unauthenticated"}),401
    host=(request.args.get("host") or "").strip()
    port=(request.args.get("port") or "").strip()
    limit=int(request.args.get("limit") or 50)
    if limit<1 or limit>200: limit=50
    if not host:
        return jsonify({"history":[]})
    dbu = db_user(user["username"])
    if not dbu:
        return jsonify({"history":[]})

    # Pull recent raw rows. We overshoot the limit because batches collapse
    # multiple rows into one entry and we want the resulting list to honour
    # the caller's limit. 5× is generous enough to handle 5-statement
    # batches that share the same batch_id without re-querying.
    raw_rows = dbu.execute(
        "SELECT id, sql, ts, duration_ms, rows_returned, error, batch_id "
        "FROM query_history "
        "WHERE user_id=? AND conn_host=? AND conn_port=? "
        "ORDER BY ts DESC, id DESC "
        "LIMIT ?",
        (dbu.user_id, host, port, limit * 5)
    ).fetchall()

    # Fold rows that share a batch_id into a single virtual entry. We
    # iterate newest-first; the first row of a given batch defines its
    # representative timestamp.
    out = []
    seen_batches = {}   # batch_id -> index into out
    seen_solo_sql = set()   # avoid duplicate non-batch SQLs (same as old DISTINCT)
    for r in raw_rows:
        bid = r["batch_id"]
        if bid:
            if bid in seen_batches:
                # Append this statement to the batch entry (older statements
                # appear after newer ones in our iteration because we
                # ORDER BY ts DESC; reverse later for natural reading).
                idx = seen_batches[bid]
                out[idx]["_stmts"].append(r["sql"])
                out[idx]["statement_count"] = len(out[idx]["_stmts"])
                if r["duration_ms"] is not None:
                    out[idx]["duration_ms"] = (out[idx]["duration_ms"] or 0) + r["duration_ms"]
                if r["rows_returned"] is not None:
                    out[idx]["rows_returned"] = (out[idx]["rows_returned"] or 0) + r["rows_returned"]
                if r["error"] and not out[idx]["error"]:
                    out[idx]["error"] = r["error"]
            else:
                seen_batches[bid] = len(out)
                out.append({
                    "sql": r["sql"], "ts": r["ts"],
                    "duration_ms": r["duration_ms"],
                    "rows_returned": r["rows_returned"],
                    "error": r["error"],
                    "is_batch": True, "batch_id": bid,
                    "statement_count": 1,
                    "_stmts": [r["sql"]],
                })
        else:
            # Solo statement — dedupe by SQL the same way the old endpoint did,
            # so the dropdown stays clean when the user re-runs the same query.
            if r["sql"] in seen_solo_sql:
                continue
            seen_solo_sql.add(r["sql"])
            out.append({
                "sql": r["sql"], "ts": r["ts"],
                "duration_ms": r["duration_ms"],
                "rows_returned": r["rows_returned"],
                "error": r["error"],
                "is_batch": False,
            })
        if len(out) >= limit:
            break

    # Reverse the stashed _stmts inside batches so they read in original
    # execution order (1, 2, 3) instead of newest-first.
    for item in out:
        if item.get("is_batch"):
            stmts = list(reversed(item.pop("_stmts")))
            item["sql"] = ";\n\n".join(s.rstrip(";") for s in stmts) + ";"

    return jsonify({"history": out})

@app.route("/api/query-history",methods=["DELETE"])
def query_history_clear():
    """Clear current user's query history for a specific connection (or all if no conn).
    Audited server-side as well as via the UI uiAudit() call, so a direct API
    invocation that bypasses the UI is still recorded in the audit trail.
    """
    token=request.cookies.get(SESSION_COOKIE_NAME)
    user=get_session_user(token) if token else None
    if not user: return jsonify({"error":"unauthenticated"}),401
    host=(request.args.get("host") or "").strip()
    port=(request.args.get("port") or "").strip()
    conn_db = db_user(user["username"])
    if not conn_db:
        return jsonify({"ok": True, "deleted": 0})
    if host:
        cur=conn_db.execute("DELETE FROM query_history WHERE user_id=? AND conn_host=? AND conn_port=?",
                            (conn_db.user_id, host, port))
    else:
        cur=conn_db.execute("DELETE FROM query_history WHERE user_id=?",
                            (conn_db.user_id,))
    conn_db.commit()
    deleted=cur.rowcount
    # Server-side audit (in addition to the UI-side uiAudit('Clear History'))
    audit("Clear History",
          panel="query",
          detail=f"Deleted {deleted} history entries for connection {host}:{port}" if host
                 else f"Deleted {deleted} history entries (all connections)",
          conn_host=host, conn_port=port)
    return jsonify({"ok":True,"deleted":deleted})

@app.route("/api/dashboards", methods=["GET"])
def dashboards_load():
    """Return the signed-in user's persisted dashboards (full board list
    with widgets) and which board they had active. Called by the client
    after login so the same dashboards come back regardless of browser."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user  = get_session_user(token) if token else None
    if not user: return jsonify({"error": "unauthenticated"}), 401
    row = db().execute(
        "SELECT boards_json, active_id FROM user_dashboards WHERE user_id=?",
        (user["id"],)
    ).fetchone()
    if not row:
        return jsonify({"boards": [], "active_id": None})
    raw = row["boards_json"]
    if isinstance(raw, (str, bytes)):
        try: boards = json.loads(raw) if raw else []
        except Exception: boards = []
    else:
        boards = raw or []
    return jsonify({"boards": boards, "active_id": row["active_id"] or None})


@app.route("/api/dashboards", methods=["POST"])
def dashboards_save():
    """Upsert the signed-in user's dashboards. Body shape:
        {boards: [{id, name, widgets: [...]}, ...], active_id}
    Hard limits to keep this row well-behaved: at most 20 boards per user,
    at most 50 widgets per board, 50KB of SQL per widget."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user  = get_session_user(token) if token else None
    if not user: return jsonify({"error": "unauthenticated"}), 401
    d = request.json or {}
    boards    = d.get("boards") or []
    active_id = d.get("active_id") or None
    if not isinstance(boards, list):
        return jsonify({"error": "boards must be an array"}), 400
    if len(boards) > 20:
        return jsonify({"error": "too many boards (max 20)"}), 400
    # Trim transient fields the client should not be sending back (value,
    # tsData, error are recomputed by each browser locally) and enforce
    # widget count and SQL length per board.
    cleaned = []
    for b in boards:
        if not isinstance(b, dict): continue
        wgs = b.get("widgets") or []
        if not isinstance(wgs, list): wgs = []
        if len(wgs) > 50:
            return jsonify({"error": f"board '{b.get('name','?')}' has too many widgets (max 50)"}), 400
        trimmed_wgs = []
        for w in wgs:
            if not isinstance(w, dict): continue
            sql = str(w.get("sql") or "")
            if len(sql) > 50000:
                return jsonify({"error": "widget SQL too large (max 50k chars)"}), 400
            trimmed_wgs.append({
                "id":      str(w.get("id") or "")[:64],
                "name":    str(w.get("name") or "")[:200],
                "type":    str(w.get("type") or "metric")[:32],
                "sql":     sql,
                "refresh": int(w.get("refresh") or 0),
                "threshold": str(w.get("threshold") or "")[:200],
                "unit":    str(w.get("unit") or "")[:32],
                "size":    (str(w.get("size") or "md")[:4] if w.get("size") in ("sm","md","lg","xl") else "md"),
                "special": str(w.get("special") or "")[:64],
                "metricCol": str(w.get("metricCol") or "")[:128],
                "hours":   w.get("hours"),
            })
        cleaned.append({
            "id":      str(b.get("id") or "")[:64],
            "name":    str(b.get("name") or "")[:200],
            "widgets": trimmed_wgs,
        })
    if active_id is not None:
        active_id = str(active_id)[:64]
    db().execute(
        "INSERT INTO user_dashboards(user_id,boards_json,active_id,updated_at) "
        "VALUES(?,?,?,now()) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "  boards_json=EXCLUDED.boards_json, active_id=EXCLUDED.active_id, updated_at=now()",
        (user["id"], json.dumps(cleaned), active_id)
    )
    db().commit()
    return jsonify({"ok": True, "saved_boards": len(cleaned)})


# ── Per-user saved connections (replaces localStorage savedConns/clusterList) ─
@app.route("/api/user/saved-connections", methods=["GET"])
def saved_connections_list():
    """Return the signed-in user's saved connections list. Sorted by
    sort_order, then by last_used_at desc (most recently used first
    among entries with the same explicit sort weight).
    """
    user_id = g.user["id"] if getattr(g, "user", None) else None
    if not user_id:
        return jsonify({"error": "not signed in"}), 401
    rows = db_global().execute(
        "SELECT id, name, host, port, username, db, folder, sort_order, "
        "       to_char(last_used_at, 'YYYY-MM-DD HH24:MI:SS') AS last_used_at, "
        "       to_char(created_at,   'YYYY-MM-DD HH24:MI:SS') AS created_at "
        "  FROM user_saved_connections "
        " WHERE user_id = ? "
        " ORDER BY COALESCE(NULLIF(folder, ''), 'zzz_ungrouped') ASC, "
        "          sort_order ASC, "
        "          last_used_at DESC NULLS LAST, created_at DESC",
        (user_id,)
    ).fetchall()
    return jsonify({"connections": [dict(r) for r in rows]})


@app.route("/api/user/saved-connections", methods=["POST"])
def saved_connections_save():
    """Idempotent save. If a row already exists for this user+host+port+username,
    its name is updated and last_used_at bumped — no duplicates created.
    The client calls this whenever the user clicks "Save to my list".
    """
    user_id = g.user["id"] if getattr(g, "user", None) else None
    if not user_id:
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    host = (d.get("host") or "").strip()
    if not host:
        return jsonify({"error": "host is required"}), 400
    try:
        port = int(d.get("port") or 8123)
    except (TypeError, ValueError):
        return jsonify({"error": "port must be an integer"}), 400
    username = (d.get("username") or d.get("user") or "default").strip() or "default"
    name = (d.get("name") or "").strip() or f"{host}:{port}"
    dbn = (d.get("db") or "").strip() or None
    # Folder: free-text label, optional. Empty / whitespace-only becomes
    # NULL so the UI can fold those entries into the "Ungrouped" section.
    # Capped at 64 chars to keep the UI readable and prevent abuse.
    folder = (d.get("folder") or "").strip()[:64] or None
    # Folder colour (optional). Validated as hex; ignored if no folder
    # is set. Same payload that POST /folder-settings would accept, so
    # the UI can save connection + folder colour in one click.
    folder_color_raw = (d.get("folder_color") or "").strip()[:16]
    folder_color = None
    if folder_color_raw and folder:
        if re.fullmatch(r"#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?", folder_color_raw):
            folder_color = folder_color_raw.lower()
        # silently ignore invalid colour values here — the dedicated
        # /folder-settings endpoint surfaces the validation error
        # when the colour is the only thing being submitted.

    # UPSERT — Postgres ON CONFLICT keyed on the (user_id, host, port, username) unique.
    db_global().execute(
        "INSERT INTO user_saved_connections "
        "    (user_id, name, host, port, username, db, folder, last_used_at) "
        " VALUES (?, ?, ?, ?, ?, ?, ?, now()) "
        " ON CONFLICT (user_id, host, port, username) "
        " DO UPDATE SET name   = EXCLUDED.name, "
        "               db     = EXCLUDED.db, "
        "               folder = EXCLUDED.folder, "
        "               last_used_at = now()",
        (user_id, name, host, port, username, dbn, folder)
    )
    # Folder colour piggy-backs on the same transaction.
    if folder and folder_color:
        db_global().execute(
            "INSERT INTO user_folder_settings (user_id, folder, color, updated_at) "
            " VALUES (?, ?, ?, now()) "
            " ON CONFLICT (user_id, folder) "
            " DO UPDATE SET color = EXCLUDED.color, updated_at = now()",
            (user_id, folder, folder_color)
        )
    db_global().commit()
    audit("Save Connection To My List", panel="connections",
          detail=f"name={name} target={host}:{port} user={username} folder={folder or '(none)'} color={folder_color or '-'}")
    # Return the freshly-saved row so the client can sync state without a re-list.
    row = db_global().execute(
        "SELECT id, name, host, port, username, db, folder, sort_order, "
        "       to_char(last_used_at, 'YYYY-MM-DD HH24:MI:SS') AS last_used_at, "
        "       to_char(created_at,   'YYYY-MM-DD HH24:MI:SS') AS created_at "
        "  FROM user_saved_connections "
        " WHERE user_id = ? AND host = ? AND port = ? AND username = ?",
        (user_id, host, port, username)
    ).fetchone()
    return jsonify({"ok": True, "connection": dict(row) if row else None,
                    "folder_color": folder_color})


@app.route("/api/user/saved-connections/folders", methods=["GET"])
def saved_connections_folders():
    """Return the distinct list of folder labels the user has used,
    each with its display settings (currently just colour). Drives:
      - the autocomplete dropdown on the Save form
      - the coloured folder headers in the Connections sidebar
        and the header Saved dropdown.
    """
    user_id = g.user["id"] if getattr(g, "user", None) else None
    if not user_id:
        return jsonify({"error": "not signed in"}), 401
    # LEFT JOIN so folders without a saved settings row still come
    # back, just with color=NULL. Union of distinct folders from
    # both tables so a folder that has settings but no connection
    # (rare, but possible) is still listed.
    rows = db_global().execute(
        "SELECT folder, color FROM ( "
        "  SELECT DISTINCT folder FROM user_saved_connections "
        "   WHERE user_id = ? AND folder IS NOT NULL AND folder <> '' "
        "  UNION "
        "  SELECT folder FROM user_folder_settings "
        "   WHERE user_id = ? "
        ") f LEFT JOIN ("
        "  SELECT folder, color FROM user_folder_settings WHERE user_id = ? "
        ") s USING (folder) "
        "ORDER BY folder ASC",
        (user_id, user_id, user_id)
    ).fetchall()
    return jsonify({"folders": [{"folder": r["folder"], "color": r["color"]} for r in rows]})


@app.route("/api/user/saved-connections/folder-settings", methods=["POST"])
def saved_connections_folder_settings():
    """UPSERT a folder's display settings. Currently just colour, but
    the table is structured to take more fields later (icon, sort
    order, default db) without another migration.
    """
    user_id = g.user["id"] if getattr(g, "user", None) else None
    if not user_id:
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    folder = (d.get("folder") or "").strip()[:64]
    if not folder:
        return jsonify({"error": "folder required"}), 400
    # Validate colour — accept a hex code (#rgb or #rrggbb), reject
    # anything else to keep injection-into-style attacks impossible.
    color_raw = (d.get("color") or "").strip()[:16]
    color = None
    if color_raw:
        if re.fullmatch(r"#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?", color_raw):
            color = color_raw.lower()
        else:
            return jsonify({"error": "invalid color (expected hex like #3b82f6)"}), 400
    db_global().execute(
        "INSERT INTO user_folder_settings (user_id, folder, color, updated_at) "
        " VALUES (?, ?, ?, now()) "
        " ON CONFLICT (user_id, folder) "
        " DO UPDATE SET color = EXCLUDED.color, updated_at = now()",
        (user_id, folder, color)
    )
    db_global().commit()
    audit("Update Folder Settings", panel="connections",
          detail=f"folder={folder} color={color or '(none)'}")
    return jsonify({"ok": True, "folder": folder, "color": color})


@app.route("/api/user/saved-connections/<int:sid>", methods=["DELETE"])
def saved_connections_delete(sid):
    user_id = g.user["id"] if getattr(g, "user", None) else None
    if not user_id:
        return jsonify({"error": "not signed in"}), 401
    row = db_global().execute(
        "SELECT name, host, port, username FROM user_saved_connections "
        " WHERE id = ? AND user_id = ?",
        (sid, user_id)
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    db_global().execute(
        "DELETE FROM user_saved_connections WHERE id = ? AND user_id = ?",
        (sid, user_id)
    )
    db_global().commit()
    audit("Remove Connection From My List", panel="connections",
          detail=f"name={row['name']} target={row['host']}:{row['port']} user={row['username']}")
    return jsonify({"ok": True})


@app.route("/api/query/tabs", methods=["GET"])
def query_tabs_load():
    """Return the signed-in user's saved query tabs for one connection
    (host:port). Used by doConnect on the client so a user resumes with the
    same tabs and SQL they left open last time they were on this cluster —
    including across logout / browser close.

    Persisted fields per tab: id, name, sql, history. The history field
    is the ↑↓ navigation buffer with up to 200 recent queries — kept so
    users don't lose their per-tab history on every relogin.

    Transient state (result, error, running, jobId) is never persisted;
    the client re-initialises those to defaults on restore.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user  = get_session_user(token) if token else None
    if not user: return jsonify({"error": "unauthenticated"}), 401
    host = (request.args.get("host") or "").strip()
    port = (request.args.get("port") or "").strip()
    if not host:
        return jsonify({"tabs": [], "active_id": None})
    row = db().execute(
        "SELECT tabs_json, active_id FROM query_tabs "
        "WHERE user_id=? AND conn_host=? AND conn_port=?",
        (user["id"], host, port)
    ).fetchone()
    if not row:
        return jsonify({"tabs": [], "active_id": None})
    # tabs_json may be returned as dict/list (psycopg JSONB) or as a string
    # depending on driver settings — handle both for robustness.
    raw = row["tabs_json"]
    if isinstance(raw, (str, bytes)):
        try:
            tabs = json.loads(raw) if raw else []
        except Exception:
            tabs = []
    else:
        tabs = raw or []
    return jsonify({"tabs": tabs, "active_id": row["active_id"] or None})


@app.route("/api/query/tabs", methods=["POST"])
def query_tabs_save():
    """Upsert the signed-in user's query tabs for one connection. The
    client calls this when tabs change (add, close, rename, switch) and on
    a short debounce while the user is typing SQL. Body shape:
        {host, port, tabs: [{id, name, sql}, ...], active_id}

    Server-side limits keep the table well-behaved: at most 50 tabs per
    connection, at most 200_000 characters of SQL per tab.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user  = get_session_user(token) if token else None
    if not user: return jsonify({"error": "unauthenticated"}), 401
    d = request.json or {}
    host = (d.get("host") or "").strip()
    port = (str(d.get("port") or "")).strip()
    tabs = d.get("tabs") or []
    active_id = d.get("active_id") or None
    if not host:
        return jsonify({"error": "host required"}), 400
    if not isinstance(tabs, list):
        return jsonify({"error": "tabs must be an array"}), 400
    if len(tabs) > 50:
        return jsonify({"error": "too many tabs (max 50)"}), 400
    # Normalise: keep durable fields only, enforce types and lengths.
    # 'history' is the per-tab query history (the ↑↓ navigation buffer
    # that drives the "History (N)" count). Without persisting it,
    # users lose their browsing history on every relogin. Bounded
    # defensively so a long session can't bloat tabs_json:
    #   - at most 200 history entries per tab
    #   - each entry's sql capped at 10_000 chars (anything longer is
    #     unlikely to be meaningful in a history pick anyway)
    HISTORY_MAX_ENTRIES = 200
    HISTORY_MAX_SQL_LEN = 10_000
    clean = []
    for t in tabs:
        if not isinstance(t, dict): continue
        tid  = str(t.get("id") or "")[:64]
        name = str(t.get("name") or "")[:200]
        sql  = str(t.get("sql") or "")
        if len(sql) > 200_000:
            return jsonify({"error": "SQL too large in one tab (max 200k chars)"}), 400
        if not tid: continue

        # Sanitise history. Accept either ["sql string", ...] or
        # [{"sql": "...", "ts": ...}, ...]; normalise to the latter
        # since the client expects an object shape with a timestamp.
        raw_hist = t.get("history") or []
        hist = []
        if isinstance(raw_hist, list):
            for h in raw_hist[-HISTORY_MAX_ENTRIES:]:
                if isinstance(h, str):
                    hist.append({"sql": h[:HISTORY_MAX_SQL_LEN]})
                elif isinstance(h, dict):
                    entry = {"sql": str(h.get("sql") or "")[:HISTORY_MAX_SQL_LEN]}
                    if h.get("ts"):       entry["ts"]       = str(h["ts"])[:40]
                    if h.get("duration"): entry["duration"] = h["duration"]
                    if h.get("rows") is not None: entry["rows"] = h["rows"]
                    if h.get("error"):    entry["error"]    = str(h["error"])[:500]
                    hist.append(entry)

        clean.append({"id": tid, "name": name, "sql": sql, "history": hist})
    if active_id is not None:
        active_id = str(active_id)[:64]
    db().execute(
        "INSERT INTO query_tabs(user_id,conn_host,conn_port,tabs_json,active_id,updated_at) "
        "VALUES(?,?,?,?,?,now()) "
        "ON CONFLICT(user_id,conn_host,conn_port) DO UPDATE SET "
        "  tabs_json=EXCLUDED.tabs_json, active_id=EXCLUDED.active_id, updated_at=now()",
        (user["id"], host, port, json.dumps(clean), active_id)
    )
    db().commit()
    return jsonify({"ok": True, "saved": len(clean)})


@app.route("/api/query/poll/<jid>")
def query_poll(jid):
    # Reads job state from Redis, so it resolves correctly no matter which
    # gunicorn worker ran the query. A Redis outage returns a clear error
    # rather than the misleading "not found".
    try:
        j=job_store.qjob_get(jid)
    except Exception as e:
        return jsonify({"status":"error","error":f"job store unreachable: {e}"}),503
    return jsonify(j) if j else (jsonify({"error":"not found"}),404)

@app.route("/api/query/cancel/<jid>",methods=["POST"])
def query_cancel(jid):
    try:
        job_store.qjob_mark_cancelled(jid)
    except Exception:
        pass
    return jsonify({"ok":True})

@app.route("/api/query/estimate", methods=["POST"])
def query_estimate():
    """Lightweight cost estimate for a SELECT — runs EXPLAIN ESTIMATE
    (no execution) plus a metadata lookup against system.parts to enrich
    the row/granule counts with byte-size information. Returns a
    structured summary the client renders as a human-readable card.
    Audit-logged. No threshold enforcement — frontend chooses what (if
    anything) to highlight.
    """
    d = request.json or {}
    sql = (d.get("sql") or "").strip()
    if not sql:
        return jsonify({"error": "No SQL"}), 400
    # Strip to first non-empty statement (same defensive pattern as
    # /api/query/explain).
    parts = [p.strip() for p in sql.split(";") if p.strip()]
    if not parts:
        return jsonify({"error": "No SQL after stripping semicolons"}), 400
    sql = parts[0]

    try:
        cl = _get_client(d)
        # 1) EXPLAIN ESTIMATE — ClickHouse's own optimizer-driven cost
        #    estimate. Returns one row per (database, table) the query
        #    will read, with parts / rows / marks counts. No execution.
        try:
            est_rows = cl.query("EXPLAIN ESTIMATE " + sql).result_rows
        except Exception as ex:
            cl.close()
            return jsonify({
                "error": f"EXPLAIN ESTIMATE failed: {ex}",
                "hint": "ClickHouse's EXPLAIN ESTIMATE only supports SELECT statements; INSERT, CREATE, ALTER, etc. cannot be estimated."
            }), 400

        tables = []
        total_rows = 0
        total_parts = 0
        total_marks = 0
        for r in (est_rows or []):
            # ClickHouse returns columns: database, table, parts, rows, marks
            try:
                db, tbl, p, rw, mk = r[0], r[1], int(r[2]), int(r[3]), int(r[4])
            except (IndexError, ValueError, TypeError):
                continue
            tables.append({
                "database": db, "table": tbl,
                "parts_to_read": p, "rows_to_read": rw, "marks_to_read": mk
            })
            total_rows += rw
            total_parts += p
            total_marks += mk

        # 2) Enrich each (db, table) with byte-size from system.parts so
        #    we can give the user "GB to be read" not just row counts.
        #    Compute ratio = rows_to_read / total_rows_in_table, then
        #    apply to data_compressed_bytes.
        total_compressed = 0
        total_uncompressed = 0
        for entry in tables:
            try:
                meta = cl.query(
                    "SELECT sum(rows) AS total_rows, "
                    "       sum(data_compressed_bytes) AS comp, "
                    "       sum(data_uncompressed_bytes) AS uncomp "
                    "  FROM system.parts "
                    " WHERE database = %(d)s AND table = %(t)s AND active",
                    parameters={"d": entry["database"], "t": entry["table"]}
                ).result_rows
                if meta and meta[0] and meta[0][0]:
                    table_rows = int(meta[0][0] or 0)
                    table_comp = int(meta[0][1] or 0)
                    table_uncomp = int(meta[0][2] or 0)
                    entry["table_total_rows"] = table_rows
                    entry["table_compressed_bytes"] = table_comp
                    entry["table_uncompressed_bytes"] = table_uncomp
                    # Pro-rate by row ratio; clamp to [0, 1].
                    if table_rows > 0:
                        ratio = min(1.0, max(0.0, entry["rows_to_read"] / table_rows))
                        entry["estimated_compressed_bytes"]   = int(table_comp   * ratio)
                        entry["estimated_uncompressed_bytes"] = int(table_uncomp * ratio)
                        total_compressed   += entry["estimated_compressed_bytes"]
                        total_uncompressed += entry["estimated_uncompressed_bytes"]
            except Exception:
                # Per-table metadata failure shouldn't sink the whole estimate.
                pass

        cl.close()

        audit("Run Cost Estimate", panel="query",
              detail=f"rows={total_rows} parts={total_parts} bytes_compressed={total_compressed}")

        return jsonify({
            "tables": tables,
            "summary": {
                "total_rows_to_read": total_rows,
                "total_parts_to_read": total_parts,
                "total_marks_to_read": total_marks,
                "total_compressed_bytes": total_compressed,
                "total_uncompressed_bytes": total_uncompressed,
                "table_count": len(tables)
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/query/explain", methods=["POST"])
def query_explain():
    d=request.json or {}; sql=(d.get("sql") or "").strip()
    mode=d.get("mode","PLAN")  # PLAN, PIPELINE, ESTIMATE
    if not sql: return jsonify({"error":"No SQL"}),400
    # Defensive: if the client sent multiple statements, EXPLAIN the first non-empty one.
    # The frontend already isolates the active statement by selection or cursor position;
    # this is just a safety net for older clients or edge cases.
    parts = [p.strip() for p in sql.split(";") if p.strip()]
    if not parts: return jsonify({"error":"No SQL after stripping semicolons"}),400
    sql = parts[0]
    try:
        cl=_get_client(d)
        # raw_query() sends SQL as-is over HTTP without appending FORMAT Native
        raw=cl.raw_query(f"EXPLAIN {mode} {sql}")
        lines=raw.decode("utf-8","replace").strip().splitlines()
        cl.close()
        return jsonify({"lines":lines,"mode":mode})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/query/explain-tree",methods=["POST"])
def query_explain_tree():
    """EXPLAIN with structured JSON output so the UI can render it as a tree.
    Returns the raw parsed JSON tree from ClickHouse's EXPLAIN PLAN json=1.
    """
    d=request.json or {}; sql=(d.get("sql") or "").strip()
    if not sql: return jsonify({"error":"No SQL"}),400
    parts = [p.strip() for p in sql.split(";") if p.strip()]
    if not parts: return jsonify({"error":"No SQL after stripping semicolons"}),400
    sql = parts[0]
    try:
        cl=_get_client(d)
        # json=1 → structured output; description=1 → step descriptions;
        # indexes=1 → which indexes are used; actions=1 → projected actions per step.
        raw=cl.raw_query(f"EXPLAIN PLAN json=1, description=1, indexes=1, actions=1 {sql} FORMAT TSVRaw")
        text=raw.decode("utf-8","replace").strip()
        cl.close()
        try:
            import json as _json
            data=_json.loads(text)
            return jsonify({"plan":data,"ok":True})
        except Exception as je:
            return jsonify({"error":"Could not parse EXPLAIN JSON: "+str(je),"raw":text[:5000]})
    except Exception as e:
        return jsonify({"error":str(e)})

# ── monitor ───────────────────────────────────────────────────────────────────
@app.route("/api/monitor/processes",methods=["POST"])
def monitor_processes():
    d=request.json or {}
    try:
        cl=_get_client(d)
        rows=cl.query("""
            SELECT query_id, user, elapsed, rows_read, memory_usage,
                   formatReadableSize(memory_usage) as mem_str,
                   is_cancelled, query
            FROM system.processes ORDER BY elapsed DESC
        """).result_rows
        cl.close()
        return jsonify({"processes":[{
            "query_id":r[0],"user":r[1],"elapsed":round(float(r[2]),2),
            "rows_read":int(r[3]),"memory_usage":int(r[4]),"mem_str":r[5],
            "is_cancelled":bool(r[6]),"query":str(r[7])[:300]
        } for r in rows]})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/monitor/kill",methods=["POST"])
def monitor_kill():
    d=request.json or {}; qid=_safe_qid(d.get("query_id",""), allow_empty=True)
    if not qid: return jsonify({"error":"No query_id"}),400
    try:
        cl=_get_client(d)
        cl.command(f"KILL QUERY WHERE query_id = '{qid}' ASYNC")
        cl.close()
        logger.warning(f"KILL QUERY query_id={qid}")
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/monitor/metrics",methods=["POST"])
def monitor_metrics():
    d=request.json or {}
    try:
        cl=_get_client(d)
        # Key system metrics
        metrics_rows=cl.query("""
            SELECT metric, value FROM system.metrics
            WHERE metric IN (
                'Query','BackgroundMergesAndMutationsPoolTask',
                'BackgroundFetchesPoolTask','ReplicatedChecks',
                'MemoryTracking','HTTPConnection','TCPConnection',
                'InterserverConnection','OpenFileForRead','OpenFileForWrite',
                'Read','Write','NetworkSend','NetworkReceive',
                'QueryPreempted','GlobalThread','GlobalThreadActive',
                'LocalThread','LocalThreadActive','PartsActive',
                'PartsCommitted','PartsCompact','PartsWide'
            )
        """).result_rows
        # Async metrics for hardware
        async_rows=cl.query("""
            SELECT metric, value FROM system.asynchronous_metrics
            WHERE metric IN (
                'MemoryResident','MemoryShared','MemoryCode','MemoryDataAndStack',
                'OSMemoryFreePlusCached','OSMemoryAvailable','OSMemoryTotal',
                'CPUFrequencyMHz','DiskAvailable_default','DiskTotal_default',
                'DiskUsed_default','FilesystemMainPathAvailableBytes',
                'FilesystemMainPathTotalBytes','FilesystemMainPathUsedBytes',
                'jemalloc.active','jemalloc.resident','jemalloc.allocated',
                'NumberOfDatabases','NumberOfTables','NumberOfDetachedParts',
                'TotalPartsOfMergeTreeTables','TotalRowsOfMergeTreeTables',
                'TotalBytesOfMergeTreeTables','MaxPartCountForPartition',
                'ReplicasSumQueueSize','ReplicasSumInsertsInQueue',
                'ReplicasSumMergesInQueue','UncompressedCacheBytes',
                'MarkCacheBytes','Uptime'
            )
        """).result_rows
        cl.close()
        metrics={r[0]:float(r[1]) for r in metrics_rows}
        async_m={r[0]:float(r[1]) for r in async_rows}
        return jsonify({"metrics":metrics,"async":async_m})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/monitor/merges",methods=["POST"])
def monitor_merges():
    d=request.json or {}
    try:
        cl=_get_client(d)
        rows=cl.query("""
            SELECT database, table, elapsed, progress, num_parts,
                   result_part_name, is_mutation,
                   formatReadableSize(total_size_bytes_compressed) as size_str,
                   formatReadableSize(bytes_read_uncompressed) as read_str
            FROM system.merges ORDER BY elapsed DESC
        """).result_rows
        cl.close()
        return jsonify({"merges":[{
            "database":r[0],"table":r[1],"elapsed":round(float(r[2]),1),
            "progress":round(float(r[3])*100,1),"num_parts":int(r[4]),
            "result_part":r[5],"is_mutation":bool(r[6]),
            "size":r[7],"read":r[8]
        } for r in rows]})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/monitor/replication",methods=["POST"])
def monitor_replication():
    d=request.json or {}
    try:
        cl=_get_client(d)
        _rq_cols=_sys_select(cl,"replication_queue",
            ["database","table","replica_name","position","node_name",
             "type","create_time","required_quorum","source_replica",
             "is_detach","is_currently_executing","num_tries","last_exception"])
        rows=cl.query(f"SELECT {_rq_cols} FROM system.replication_queue ORDER BY create_time DESC LIMIT 100").result_rows
        cl.close()
        return jsonify({"queue":[{
            "database":r[0],"table":r[1],"replica":r[2],"position":int(r[3] or 0),
            "node":r[4],"type":r[5],"create_time":str(r[6]),
            "quorum":int(r[7] or 0),"source":r[8],"is_detach":bool(r[9]),
            "executing":bool(r[10]),"tries":int(r[11] or 0),"exception":r[12] or ""
        } for r in rows]})
    except Exception as e: return jsonify({"error":str(e)})

# ── schema explorer ───────────────────────────────────────────────────────────
@app.route("/api/schema/tables",methods=["POST"])
def schema_tables():
    d=request.json or {}; db=_safe_ident(d.get("database",""),"database")
    if not db: return jsonify({"error":"No database"})
    try:
        cl=_get_client(d)
        rows=cl.query(f"""
            SELECT t.name, t.engine,
                   sum(p.rows) as rows,
                   sum(p.bytes_on_disk) as bytes,
                   count() as parts
            FROM system.tables t
            LEFT JOIN system.parts p ON p.database=t.database AND p.table=t.name AND p.active
            WHERE t.database='{db}'
            GROUP BY t.name, t.engine ORDER BY t.name
        """).result_rows
        cl.close()
        return jsonify({"tables":[{
            "name":r[0],"engine":r[1],
            "rows":int(r[2]) if r[2] else 0,
            "bytes":int(r[3]) if r[3] else 0,
            "parts":int(r[4]) if r[4] else 0,
        } for r in rows]})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/schema/columns-bulk", methods=["POST"])
def schema_columns_bulk():
    """Return every column of every table in a database, in one shot.
    Feeds the editor's SQL autocomplete cache so we don't have to
    round-trip per token. Query is metadata-only against
    system.columns; cluster impact is negligible.
    """
    d = request.json or {}
    db = _safe_ident((d.get("database") or "").strip(), "database")
    if not db:
        return jsonify({"error": "database required"}), 400
    try:
        cl = _get_client(d)
        rows = cl.query(
            "SELECT database, table, name "
            "  FROM system.columns "
            " WHERE database = %(db)s "
            " ORDER BY database, table, position",
            parameters={"db": db}
        ).result_rows
        cl.close()
        # Reshape into {table: [col1, col2, ...]} for direct use as a
        # CodeMirror sql-hint `tables` map.
        by_table = {}
        for r in (rows or []):
            tbl, col = r[1], r[2]
            if tbl not in by_table:
                by_table[tbl] = []
            by_table[tbl].append(col)
        return jsonify({"database": db, "tables": by_table,
                        "table_count": len(by_table),
                        "column_count": sum(len(v) for v in by_table.values())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/schema/table-detail",methods=["POST"])
def schema_table_detail():
    d=request.json or {}; db=_safe_ident(d.get("database",""),"database"); tbl=_safe_ident(d.get("table",""),"table")
    if not db or not tbl: return jsonify({"error":"database and table required"})
    try:
        cl=_get_client(d)
        # Columns
        cols=cl.query(f"""
            SELECT name,type,default_kind,default_expression,comment,
                   compression_codec,is_in_partition_key,is_in_sorting_key,
                   is_in_primary_key,is_in_sampling_key
            FROM system.columns WHERE database='{db}' AND table='{tbl}' ORDER BY position
        """).result_rows
        # Indexes
        try:
            idxs=cl.query(f"""
                SELECT name,type,expr,granularity
                FROM system.data_skipping_indices WHERE database='{db}' AND table='{tbl}'
            """).result_rows
        except: idxs=[]
        # Parts summary
        try:
            parts=cl.query(f"""
                SELECT count(),sum(rows),sum(bytes_on_disk),sum(data_compressed_bytes),
                       sum(data_uncompressed_bytes),min(min_date),max(max_date)
                FROM system.parts
                WHERE database='{db}' AND table='{tbl}' AND active
            """).result_rows
            ps=parts[0] if parts else [0]*7
        except: ps=[0]*7
        # min_date/max_date are only meaningful when:
        #   1. table has data (active parts > 0)
        #   2. table is partitioned by a Date column (otherwise CH returns 1970-01-01 sentinel)
        # Otherwise hide them entirely so the UI doesn't show misleading "Date Range".
        def _meaningful_date(v):
            if v is None: return False
            s = str(v).strip()
            return s not in ('', '1970-01-01', '0001-01-01', '0000-00-00', 'None', 'NULL')
        if int(ps[0] or 0) == 0 or not _meaningful_date(ps[5]):
            min_d = max_d = ''
        else:
            min_d = str(ps[5]); max_d = str(ps[6])
        # Per-column storage breakdown (which columns are fattest on disk).
        # Drives the "Top columns by disk" card in the Overview tab.
        try:
            csz=cl.query(f"""
                SELECT name, data_compressed_bytes, data_uncompressed_bytes
                FROM system.columns
                WHERE database='{db}' AND table='{tbl}'
                ORDER BY data_compressed_bytes DESC
            """).result_rows
        except Exception:
            csz=[]
        # Partition breakdown (rows / compressed size / parts per partition).
        # 'tuple()' partition value means the table is effectively unpartitioned.
        try:
            part_rows=cl.query(f"""
                SELECT partition, sum(rows), sum(data_compressed_bytes), count()
                FROM system.parts
                WHERE database='{db}' AND table='{tbl}' AND active
                GROUP BY partition
                ORDER BY sum(data_compressed_bytes) DESC
                LIMIT 50
            """).result_rows
        except Exception:
            part_rows=[]
        # DDL
        ddl_rows=cl.query(f"SHOW CREATE TABLE `{db}`.`{tbl}`").result_rows
        ddl=ddl_rows[0][0] if ddl_rows else ""
        cl.close()
        return jsonify({
            "columns":[{"name":r[0],"type":r[1],"default_kind":r[2],"default_expr":r[3],
                "comment":r[4],"codec":r[5],"in_partition":bool(r[6]),
                "in_sort":bool(r[7]),"in_primary":bool(r[8]),"in_sample":bool(r[9])} for r in cols],
            "indexes":[{"name":r[0],"type":r[1],"expr":r[2],"granularity":int(r[3])} for r in idxs],
            "parts_summary":{"count":int(ps[0]),"rows":int(ps[1]),"bytes":int(ps[2]),
                "compressed":int(ps[3]),"uncompressed":int(ps[4]),
                "min_date":min_d,"max_date":max_d},
            "ddl":ddl,
            "column_sizes":[{"name":r[0],"compressed":int(r[1] or 0),"uncompressed":int(r[2] or 0)} for r in csz],
            "partitions":[{"partition":str(r[0]),"rows":int(r[1] or 0),"compressed":int(r[2] or 0),"parts":int(r[3] or 0)} for r in part_rows],
        })
    except Exception as e: return jsonify({"error":str(e)})

# ── users ─────────────────────────────────────────────────────────────────────
@app.route("/api/users/list",methods=["POST"])
def users_list():
    d=request.json or {}
    try:
        cl=_get_client(d)
        users=cl.query("SELECT name,storage,auth_type,host_ip,host_names,default_roles_all,default_roles_list,default_database FROM system.users ORDER BY name").result_rows
        roles=cl.query("SELECT name,storage FROM system.roles ORDER BY name").result_rows
        grants=cl.query("SELECT user_name,role_name,access_type,database,table,column,is_partial_revoke FROM system.grants ORDER BY user_name,access_type").result_rows
        cl.close()
        return jsonify({
            "users":[{"name":r[0],"storage":r[1],"auth":r[2],"host_ip":str(r[3]),
                "host_names":str(r[4]),"default_roles_all":bool(r[5]),
                "default_roles":str(r[6]),"default_db":r[7]} for r in users],
            "roles":[{"name":r[0],"storage":r[1]} for r in roles],
            "grants":[{"user":r[0],"role":r[1],"access":r[2],"database":r[3],
                "table":r[4],"column":r[5],"partial_revoke":bool(r[6])} for r in grants],
        })
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/users/create",methods=["POST"])
def users_create():
    d=request.json or {}
    username=d.get("username","")
    # Use new_password to avoid collision with admin connection password
    new_password=d.get("new_password","")
    host=d.get("host_restriction","ANY"); default_db=d.get("default_database","")
    if not username: return jsonify({"error":"Username required"}),400
    try:
        uname_q = _qident(username, "username")
        cl=_get_client(d)
        h_upper=(host or "ANY").strip().upper()
        if h_upper in ("ANY","","*"):
            host_clause=""
        elif h_upper=="NONE":
            host_clause=" HOST NONE"
        elif h_upper.startswith("LIKE "):
            host_clause=f" HOST LIKE '{_qstr(host.strip()[5:].strip())}'"
        else:
            host_clause=f" HOST IP '{_qstr(host.strip())}'"
        db_clause=f" DEFAULT DATABASE {_qident(default_db,'database')}" if default_db else ""
        if new_password:
            sql=f"CREATE USER IF NOT EXISTS {uname_q}{host_clause} IDENTIFIED BY '{_qstr(new_password)}'{db_clause}"
        else:
            sql=f"CREATE USER IF NOT EXISTS {uname_q}{host_clause} IDENTIFIED WITH no_password{db_clause}"
        cl.command(sql)
        if d.get("grant_role"):
            cl.command(f"GRANT {_qident(d['grant_role'],'role')} TO {uname_q}")
        cl.close()
        logger.info(f"USER CREATE user={username}")
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/users/drop",methods=["POST"])
def users_drop():
    d=request.json or {}; username=d.get("username","")
    if not username: return jsonify({"error":"Username required"}),400
    try:
        cl=_get_client(d)
        cl.command(f"DROP USER IF EXISTS {_qident(username,'username')}")
        cl.close()
        logger.warning(f"USER DROP user={username}")
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/users/create-role",methods=["POST"])
def users_create_role():
    d=request.json or {}; role=d.get("role","")
    if not role: return jsonify({"error":"Role name required"}),400
    try:
        cl=_get_client(d)
        role_q=_qident(role,"role")
        cl.command(f"CREATE ROLE IF NOT EXISTS {role_q}")
        if d.get("grants"):
            for grant in d["grants"].split(","):
                g=grant.strip()
                if g: cl.command(f"GRANT {_safe_privilege(g)} ON *.* TO {role_q}")
        cl.close()
        logger.info(f"ROLE CREATE role={role}")
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/users/grant",methods=["POST"])
def users_grant():
    d=request.json or {}
    privilege=d.get("privilege",""); target_db=_safe_ident_star(d.get("database","*"),"database"); target_tbl=_safe_ident_star(d.get("table","*"),"table")
    grantee=d.get("grantee",""); is_role=d.get("is_role",False)
    if not privilege or not grantee: return jsonify({"error":"Privilege and grantee required"}),400
    try:
        cl=_get_client(d)
        db_ref = "*" if target_db == "*" else "`"+target_db+"`"
        tbl_ref = "*" if target_tbl == "*" else "`"+target_tbl+"`"
        cl.command(f"GRANT {_safe_privilege(privilege)} ON {db_ref}.{tbl_ref} TO {_qident(grantee,'grantee')}")
        cl.close()
        logger.info(f"GRANT {privilege} ON {target_db}.{target_tbl} TO {grantee}")
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)})


# ── query settings ────────────────────────────────────────────────────────────
COMMON_SETTINGS=[
    'max_memory_usage','max_bytes_before_external_group_by','max_bytes_before_external_sort',
    'max_threads','max_block_size','max_execution_time','timeout_overflow_mode',
    'join_algorithm','max_rows_in_join','join_overflow_mode',
    'group_by_overflow_mode','max_rows_to_group_by',
    'max_rows_to_read','max_bytes_to_read','read_overflow_mode',
    'max_result_rows','max_result_bytes','result_overflow_mode',
    'allow_experimental_analyzer','use_query_cache','query_cache_ttl',
    'enable_filesystem_cache','log_queries','log_query_threads',
    'distributed_product_mode','prefer_localhost_replica',
    'optimize_move_to_prewhere','optimize_read_in_order',
]

@app.route("/api/query/settings",methods=["POST"])
def query_settings_list():
    d=request.json or {}
    try:
        cl=_get_client(d)
        names="','".join(COMMON_SETTINGS)
        rows=cl.query(f"SELECT name,value,description,type,readonly FROM system.settings WHERE name IN ('{names}') ORDER BY name").result_rows
        cl.close()
        return jsonify({"settings":[{"name":r[0],"value":str(r[1]),"description":r[2],"type":r[3],"readonly":bool(r[4])} for r in rows]})
    except Exception as e: return jsonify({"error":str(e)})

# ── alerts ────────────────────────────────────────────────────────────────────
import smtplib, urllib.request as _urlreq
from email.mime.text import MIMEText

ALERT_CFG_FILE=APP_DIR/"alert_config.json"
_alert_history=[]
_alert_cooldowns={}

def _load_alert_cfg():
    if ALERT_CFG_FILE.exists():
        try: return json.loads(ALERT_CFG_FILE.read_text())
        except: pass
    return {"enabled":False,"interval":60,"cooldown":3600,
            "connection":{"host":"localhost","port":8123,"user":"default","password":""},
            "thresholds":{"disk_pct":80,"mem_pct":85,"long_query_sec":60,"replication_queue":50},
            "channels":{"email":{"enabled":False,"smtp_host":"smtp.gmail.com","smtp_port":587,"smtp_tls":True,"smtp_user":"","smtp_password":"","from":"","to":""},"webhook":{"enabled":False,"url":"","type":"slack"}}}

def _save_alert_cfg(cfg):
    ALERT_CFG_FILE.write_text(json.dumps(cfg,indent=2))

def _webhook(url,wtype,title,msg):
    if wtype=="teams":
        payload={"@type":"MessageCard","@context":"https://schema.org/extensions","themeColor":"FF0000","summary":title,"sections":[{"activityTitle":title,"text":msg}]}
    else:  # slack or generic
        payload={"text":f"*{title}*\n{msg}"}
    data=json.dumps(payload).encode()
    req=_urlreq.Request(url,data=data,headers={"Content-Type":"application/json"})
    _urlreq.urlopen(req,timeout=10)

def _email(cfg,subject,body):
    msg=MIMEText(body)
    msg["Subject"]=subject
    msg["From"]=cfg.get("from",cfg.get("smtp_user",""))
    recipients=[r.strip() for r in cfg.get("to","").split(",") if r.strip()]
    msg["To"]=", ".join(recipients)
    with smtplib.SMTP(cfg.get("smtp_host","localhost"),int(cfg.get("smtp_port",587))) as s:
        if cfg.get("smtp_tls",True): s.starttls()
        if cfg.get("smtp_user"): s.login(cfg["smtp_user"],cfg.get("smtp_password",""))
        s.send_message(msg)

def _fire(key,severity,title,msg,channels,cooldown):
    global _alert_cooldowns,_alert_history
    now=time.time()
    if now-_alert_cooldowns.get(key,0)<cooldown: return
    _alert_cooldowns[key]=now
    entry={"time":time.strftime("%Y-%m-%d %H:%M:%S"),"key":key,"severity":severity,"title":title,"message":msg}
    _alert_history.insert(0,entry); _alert_history[:]=_alert_history[:100]
    logger.warning(f"ALERT [{severity}] {title}: {msg[:120]}")
    wb=channels.get("webhook",{})
    if wb.get("enabled") and wb.get("url"):
        try: _webhook(wb["url"],wb.get("type","slack"),f"[{severity}] {title}",msg)
        except Exception as e: logger.error(f"Webhook failed: {e}")
    em=channels.get("email",{})
    if em.get("enabled") and em.get("to"):
        try: _email(em,f"[ClickHouse Alert] {title}",f"{title}\n\n{msg}\n\nTime: {entry['time']}")
        except Exception as e: logger.error(f"Email failed: {e}")

def _check_alerts(cfg):
    conn=cfg.get("connection",{})
    if not conn.get("host"): return
    thrs=cfg.get("thresholds",{}); channels=cfg.get("channels",{}); cooldown=int(cfg.get("cooldown",3600))
    try:
        import clickhouse_connect as _cc
        cl=_cc.get_client(host=conn.get("host","localhost"),port=int(conn.get("port",8123)),
            username=conn.get("user","default"),password=conn.get("password",""),connect_timeout=5,query_limit=0)
        am={r[0]:float(r[1]) for r in cl.query("SELECT metric,value FROM system.asynchronous_metrics WHERE metric IN ('FilesystemMainPathUsedBytes','FilesystemMainPathTotalBytes','MemoryResident','OSMemoryTotal')").result_rows}
        if "disk_pct" in thrs:
            pct=am.get("FilesystemMainPathUsedBytes",0)/max(am.get("FilesystemMainPathTotalBytes",1),1)*100
            if pct>float(thrs["disk_pct"]): _fire("disk_pct","CRITICAL","Disk Usage High",f"Disk at {pct:.1f}% (threshold {thrs['disk_pct']}%)",channels,cooldown)
        if "mem_pct" in thrs:
            pct=am.get("MemoryResident",0)/max(am.get("OSMemoryTotal",1),1)*100
            if pct>float(thrs["mem_pct"]): _fire("mem_pct","WARNING","Memory Usage High",f"Memory at {pct:.1f}% (threshold {thrs['mem_pct']}%)",channels,cooldown)
        if "long_query_sec" in thrs:
            rows=cl.query(f"SELECT count(),groupArray(substring(query,1,120)) FROM system.processes WHERE elapsed>{thrs['long_query_sec']}").result_rows
            cnt=int(rows[0][0]) if rows else 0
            if cnt>0: _fire("long_query","WARNING","Long Running Queries",f"{cnt} queries running > {thrs['long_query_sec']}s:\n"+"\n".join(str(q)[:120] for q in (rows[0][1][:3] if rows else [])),channels,cooldown)
        if "replication_queue" in thrs:
            cnt=int(cl.query("SELECT count() FROM system.replication_queue").result_rows[0][0])
            if cnt>int(thrs["replication_queue"]): _fire("replication_queue","WARNING","Replication Queue Large",f"Queue has {cnt} entries (threshold {thrs['replication_queue']})",channels,cooldown)
        cl.close()
    except Exception as e: logger.error(f"Alert check error: {e}")

def _alert_loop():
    while True:
        try:
            cfg=_load_alert_cfg()
            if cfg.get("enabled"): _check_alerts(cfg)
            time.sleep(max(10,int(cfg.get("interval",60))))
        except Exception as e:
            logger.error(f"Alert loop error: {e}"); time.sleep(60)

threading.Thread(target=_alert_loop,daemon=True,name="alert-loop").start()


# ╔════════════════════════════════════════════════════════════════════════╗
# ║  SIEM forwarding                                                       ║
# ║                                                                        ║
# ║  Background worker polls audit_events for each enabled destination,    ║
# ║  formats them, POSTs them, and advances last_forwarded_id only after   ║
# ║  HTTP 2xx — at-least-once delivery, no event drops on transient        ║
# ║  failures. Admin REST endpoints below let operators add / edit /       ║
# ║  delete / test destinations and inspect the forwarding log.            ║
# ╚════════════════════════════════════════════════════════════════════════╝

# ─── Formatters ──────────────────────────────────────────────────────────
# Each formatter takes a list of audit_events rows (dict-like) and returns
# the HTTP body to POST. Content-Type is set by the caller based on format.

def _siem_fmt_json(rows, source_host):
    """Generic JSON array — works with Datadog Logs intake, custom HTTP
    sinks, n8n / Zapier webhooks, anything that consumes JSON."""
    out = []
    for r in rows:
        out.append({
            "@timestamp": r["ts"].isoformat() if hasattr(r["ts"],"isoformat") else str(r["ts"]),
            "source":     "clickhouse-console",
            "source_host": source_host,
            "event_id":   r["id"],
            "user_id":    r["user_id"],
            "username":   r["username"] or "",
            "role":       r["role"] or "",
            "action":     r["action"],
            "panel":      r["panel"] or "",
            "detail":     (r["detail"] or "")[:8000],
            "ip":         r["ip"] or "",
            "conn_host":  r["conn_host"] or "",
            "conn_port":  r["conn_port"] or "",
            "conn_user":  r["conn_user"] or "",
            "result":     r["result"] or "ok",
        })
    return json.dumps(out, default=str)

def _siem_fmt_ecs(rows, source_host):
    """Elastic Common Schema — one ECS document per event, newline-
    delimited so an Elastic logstash / Filebeat / Elastic Agent HTTP
    pipeline reads them as a stream."""
    lines = []
    for r in rows:
        doc = {
            "@timestamp": r["ts"].isoformat() if hasattr(r["ts"],"isoformat") else str(r["ts"]),
            "event": {
                "kind":     "event",
                "category": ["database","authentication" if r["panel"]=="auth" else "process"],
                "action":   r["action"],
                "outcome":  "success" if (r["result"] or "ok")=="ok" else "failure",
                "module":   "clickhouse_console",
            },
            "user":   {"name": r["username"] or "", "id": r["user_id"], "roles": [r["role"] or ""]},
            "source": {"ip": r["ip"] or ""},
            "host":   {"name": source_host},
            "service": {"name": "clickhouse-console"},
            "labels": {
                "panel":     r["panel"] or "",
                "conn_host": r["conn_host"] or "",
                "conn_port": r["conn_port"] or "",
                "conn_user": r["conn_user"] or "",
            },
            "message": (r["detail"] or "")[:8000],
        }
        lines.append(json.dumps(doc, default=str))
    return "\n".join(lines)

def _siem_fmt_splunk_hec(rows, source_host):
    """Splunk HTTP Event Collector — one JSON object per line, each
    wrapped in {event:..., sourcetype:..., host:..., time:...}."""
    lines = []
    for r in rows:
        ts = r["ts"]
        epoch = ts.timestamp() if hasattr(ts,"timestamp") else None
        envelope = {
            "time":       epoch,
            "host":       source_host,
            "source":     "clickhouse-console",
            "sourcetype": "chconsole:audit",
            "event": {
                "event_id": r["id"],
                "username": r["username"] or "",
                "role":     r["role"] or "",
                "action":   r["action"],
                "panel":    r["panel"] or "",
                "detail":   (r["detail"] or "")[:8000],
                "ip":       r["ip"] or "",
                "conn_host":r["conn_host"] or "",
                "conn_user":r["conn_user"] or "",
                "result":   r["result"] or "ok",
            }
        }
        lines.append(json.dumps(envelope, default=str))
    return "\n".join(lines)

def _siem_fmt_slack(rows, source_host):
    """Slack incoming-webhook payload. Slack messages don't batch nicely;
    we send up to 5 events as attachments and summarise the rest as a
    'and N more...' footer so a noisy hour doesn't drown a channel."""
    SHOW = 5
    shown, extra = rows[:SHOW], len(rows) - SHOW
    color_for = lambda r: ("#d04040" if (r["result"] or "ok") != "ok"
                           else "#3a8efb" if (r["role"] or "") == "admin"
                           else "#7c8aa3")
    attachments = []
    for r in shown:
        attachments.append({
            "color": color_for(r),
            "title": f"[{r['username'] or '?'}] {r['action']}"
                     + (f" — {r['panel']}" if r["panel"] else ""),
            "text":  (r["detail"] or "")[:400],
            "fields": [
                {"title":"IP",        "value": r["ip"] or "—",        "short": True},
                {"title":"Cluster",   "value": r["conn_host"] or "—", "short": True},
                {"title":"Result",    "value": r["result"] or "ok",   "short": True},
                {"title":"Timestamp", "value": str(r["ts"]),          "short": True},
            ],
        })
    payload = {
        "text": f"*ClickHouse Console* — {len(rows)} audit event(s) from `{source_host}`",
        "attachments": attachments,
    }
    if extra > 0:
        payload["attachments"].append({"color":"#7c8aa3",
            "text": f"_… and {extra} more events not shown_"})
    return json.dumps(payload)

_SIEM_FORMATTERS = {
    "json":       (_siem_fmt_json,       "application/json"),
    "ecs":        (_siem_fmt_ecs,        "application/x-ndjson"),
    "splunk_hec": (_siem_fmt_splunk_hec, "application/json"),
    "slack":      (_siem_fmt_slack,      "application/json"),
}

# Source host label — added to every forwarded event so a multi-instance
# deployment can be filtered downstream. Falls back to the hostname.
import socket as _socket
_SIEM_SOURCE_HOST = os.environ.get("CHC_SIEM_SOURCE", _socket.gethostname())

# Per-destination concurrency lock — keep the forwarder cycle for one
# destination from racing with its REST PATCH (e.g., the admin disabling
# a destination mid-batch).
_siem_destination_locks = {}
_siem_destination_locks_lock = threading.Lock()
def _siem_dest_lock(dest_id):
    with _siem_destination_locks_lock:
        if dest_id not in _siem_destination_locks:
            _siem_destination_locks[dest_id] = threading.Lock()
        return _siem_destination_locks[dest_id]

# ─── Forwarder loop ──────────────────────────────────────────────────────
SIEM_BATCH = 100        # max events per HTTP request
SIEM_TICK  = 8          # seconds between cycles
SIEM_LOG_RETENTION_PER_DEST = 200  # forward-log rows to keep per destination

def _siem_forward_one(dest):
    """Forward a single batch for one destination. Updates destination
    state in place. Returns (events_sent, error_or_None).

    MUST be called from inside `with app.app_context():` because
    db_global() relies on Flask's `g` object. Calling it without an
    active app context fails silently inside the try/excepts and was
    the original cause of the 'PENDING forever, log empty' bug.
    """
    fmt = dest["format"]
    if fmt not in _SIEM_FORMATTERS:
        return 0, f"unknown format {fmt!r}"

    # Pull next batch
    try:
        rows = db_global().execute("""
            SELECT id, ts, user_id, username, role, action, panel, detail,
                   conn_host, conn_port, conn_user, ip, result
              FROM audit_events
             WHERE id > ?
             ORDER BY id ASC
             LIMIT ?
        """, (dest["last_forwarded_id"], SIEM_BATCH)).fetchall()
    except Exception as e:
        return 0, f"db read: {e}"

    if not rows:
        return 0, None        # nothing to do — not an error

    # Optional action filter
    if dest.get("filter_actions"):
        allowed = {a.strip() for a in dest["filter_actions"].split(",") if a.strip()}
        filtered = [r for r in rows if r["action"] in allowed]
    else:
        filtered = rows

    first_id = rows[0]["id"]
    last_id  = rows[-1]["id"]

    # If filter swallowed the whole batch we still need to advance the
    # watermark so we don't re-scan the same rows next tick.
    if not filtered:
        try:
            db_global().execute(
                "UPDATE siem_destinations SET last_forwarded_id=?, last_attempt_at=now(), "
                "last_status='ok', consecutive_failures=0, last_error=NULL WHERE id=?",
                (last_id, dest["id"]))
            db_global().commit()
        except Exception:
            pass
        return 0, None

    formatter, content_type = _SIEM_FORMATTERS[fmt]
    body = formatter(filtered, _SIEM_SOURCE_HOST)

    headers = {"Content-Type": content_type,
               "User-Agent":   "ClickHouseConsole/4.0 SIEM-Forwarder"}
    if dest.get("auth_header"):
        try:
            name, _, value = dest["auth_header"].partition(":")
            if name and value:
                headers[name.strip()] = value.strip()
        except Exception:
            pass

    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(dest["url"], data=body.encode("utf-8"),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            http_status = resp.status
            ok = 200 <= http_status < 300
            err_text = None if ok else f"HTTP {http_status}"
    except urllib.error.HTTPError as e:
        http_status = e.code
        ok = False
        err_text = f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        http_status = None
        ok = False
        err_text = str(e)[:300]

    # Persist outcome
    try:
        if ok:
            db_global().execute(
                "UPDATE siem_destinations SET last_forwarded_id=?, last_attempt_at=now(), "
                "last_status='ok', consecutive_failures=0, last_error=NULL WHERE id=?",
                (last_id, dest["id"]))
        else:
            db_global().execute(
                "UPDATE siem_destinations SET last_attempt_at=now(), "
                "last_status=?, consecutive_failures=consecutive_failures+1, last_error=? "
                "WHERE id=?",
                (f"error: {err_text}", err_text, dest["id"]))
        db_global().execute(
            "INSERT INTO siem_forward_log(destination_id,status,http_status,batch_size,"
            "first_event_id,last_event_id,error) VALUES(?,?,?,?,?,?,?)",
            (dest["id"], "ok" if ok else "failed", http_status,
             len(filtered), first_id, last_id, err_text))
        db_global().commit()
    except Exception as e:
        logger.warning(f"SIEM bookkeeping failed: {e}")

    return (len(filtered) if ok else 0), (None if ok else err_text)


def _siem_loop():
    """Poll enabled destinations forever. Trims the forward log to a small
    rolling window on each pass to keep the table from growing forever.

    The whole body runs under `with app.app_context():` — db_global()
    uses Flask's `g` object and would otherwise silently fail in this
    background thread (it was, originally — the symptom was the SIEM
    panel showing PENDING forever with an empty forward log even
    though the destination URL was reachable).
    """
    # Trim log once at startup
    with app.app_context():
        try:
            db_global().execute("""
                DELETE FROM siem_forward_log WHERE id IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (PARTITION BY destination_id ORDER BY ts DESC) AS rn
                          FROM siem_forward_log
                    ) t WHERE t.rn > ?
                )
            """, (SIEM_LOG_RETENTION_PER_DEST,))
            db_global().commit()
        except Exception:
            pass

    backoff_until = {}     # dest_id -> epoch; honor exponential backoff after failures
    while True:
        try:
            with app.app_context():
                destinations = db_global().execute(
                    "SELECT * FROM siem_destinations WHERE enabled=TRUE ORDER BY id"
                ).fetchall()
                now = time.time()
                for d in destinations:
                    if backoff_until.get(d["id"], 0) > now:
                        continue
                    lock = _siem_dest_lock(d["id"])
                    if not lock.acquire(blocking=False):
                        continue
                    try:
                        sent, err = _siem_forward_one(d)
                    except Exception as e:
                        sent, err = 0, str(e)[:300]
                        logger.warning(f"SIEM forward exception for dest {d['id']}: {e}")
                    finally:
                        lock.release()
                    if err:
                        # Exponential backoff capped at 5 minutes — keeps a dead
                        # destination from hammering the worker on every tick.
                        fails = (d["consecutive_failures"] or 0) + 1
                        delay = min(300, 8 * (2 ** min(fails, 5)))
                        backoff_until[d["id"]] = now + delay
                    # While we still have events to forward (sent==SIEM_BATCH),
                    # drain immediately on the next tick — don't sleep a full
                    # cycle if a destination is catching up after downtime.
                    if sent and sent < SIEM_BATCH:
                        backoff_until.pop(d["id"], None)
            time.sleep(SIEM_TICK)
        except Exception as e:
            logger.error(f"SIEM loop error: {e}")
            time.sleep(30)

threading.Thread(target=_siem_loop, daemon=True, name="siem-forwarder").start()


# ╔════════════════════════════════════════════════════════════════════════╗
# ║  Backup scheduler                                                      ║
# ║                                                                        ║
# ║  Background thread polls backup_schedules every minute. Due rows fire  ║
# ║  a native BACKUP SQL against ClickHouse with the right base_backup     ║
# ║  for the configured type. Differential schedules use the schedule's    ║
# ║  own last_full_name as the base; incremental schedules use their      ║
# ║  last_run_name (which may itself be a previous incremental).           ║
# ╚════════════════════════════════════════════════════════════════════════╝

# Standard cron presets surfaced in the UI; the actual schedule field
# accepts any 5-field cron expression for power users.
BACKUP_CRON_PRESETS = [
    ("@hourly",          "0 * * * *",   "Every hour"),
    ("@6hourly",         "0 */6 * * *", "Every 6 hours"),
    ("@daily-2am",       "0 2 * * *",   "Every day at 02:00"),
    ("@daily-4am",       "0 4 * * *",   "Every day at 04:00"),
    ("@weekly-sun-2am",  "0 2 * * 0",   "Every Sunday at 02:00"),
    ("@monthly-1st-2am", "0 2 1 * *",   "First day of each month, 02:00"),
]

def _backup_render_name(template, db_name, backup_type, dt=None):
    """Substitute placeholders in a filename template. Recognised:
       {db}        — database name (or 'all' for full-server backups)
       {type}      — full / differential / incremental
       {date}      — YYYYMMDD
       {time}      — HHMM
       {datetime}  — YYYYMMDD_HHMM
       {ts}        — Unix epoch seconds (for guaranteed uniqueness)
    """
    if dt is None:
        dt = datetime.now()
    out = (template or "{db}_{type}_{datetime}.zip")
    out = out.replace("{db}",       (db_name or "all"))
    out = out.replace("{type}",     (backup_type or "full"))
    out = out.replace("{date}",     dt.strftime("%Y%m%d"))
    out = out.replace("{time}",     dt.strftime("%H%M"))
    out = out.replace("{datetime}", dt.strftime("%Y%m%d_%H%M"))
    out = out.replace("{ts}",       str(int(dt.timestamp())))
    # Defensive: strip any path traversal a malicious template might
    # produce. Filenames are simple basenames only.
    out = out.replace("/", "_").replace("\\", "_").replace("..", "_")
    if not out.endswith(".zip"):
        out = out + ".zip"
    return out

def _backup_compute_next_run(cron_expr, base_time=None):
    """Compute next fire time for a cron expression. Returns datetime
    or None on parse error. base_time defaults to now()."""
    if not _CRONITER_AVAILABLE:
        return None
    if base_time is None:
        base_time = datetime.now()
    try:
        return _croniter(cron_expr, base_time).get_next(datetime)
    except Exception:
        return None

def _backup_fire_schedule(sched_row):
    """Execute one schedule row. Builds the same payload the manual
    endpoint accepts, calls the same code path, then updates the
    schedule's runtime state. Called from inside an app_context."""
    sched_id = sched_row["id"]
    now = datetime.now()
    backup_type = sched_row["backup_type"] or "full"
    db_name = sched_row["db_name"] or ""
    name = _backup_render_name(
        sched_row["name_template"], db_name, backup_type, now)
    path = (sched_row["storage_path"] or "").rstrip("/")

    # Resolve connection: schedules can target a specific connection
    # registered in connections table; if not set, fall back to the
    # legacy environment defaults (the same code path manual backups
    # use when no connection is selected).
    conn_payload = {}
    if sched_row.get("connection_id"):
        try:
            crow = db_global().execute(
                "SELECT host, port, username, password FROM connections WHERE id=?",
                (sched_row["connection_id"],)
            ).fetchone()
            if crow:
                conn_payload = {
                    "host":     crow["host"],
                    "port":     crow["port"],
                    "user":     crow["username"],
                    "password": crow["password"] or "",
                }
        except Exception as e:
            logger.warning(f"backup schedule {sched_id}: connection lookup failed: {e}")

    # Build the same shape backup_native_run() expects
    payload = {
        **conn_payload,
        "target":      sched_row["target"] or "database",
        "db_name":     db_name,
        "tables":      sched_row["tables"] or "",
        "path":        path,
        "name":        name,
        "backup_type": backup_type,
    }

    # Resolve base for differential / incremental
    if backup_type == "incremental":
        last_name = sched_row.get("last_run_name")
        if not last_name:
            # No prior run — incremental degrades to full on the first run.
            backup_type = "full"
            payload["backup_type"] = "full"
        else:
            payload["base_backup_path"] = path
            payload["base_backup_name"] = last_name
    elif backup_type == "differential":
        last_full = sched_row.get("last_full_name")
        if not last_full:
            # No prior full — same degradation as incremental.
            backup_type = "full"
            payload["backup_type"] = "full"
        else:
            # Use direct base reference rather than glob search — we know
            # the exact filename from our own state.
            payload["backup_type"] = "incremental"   # CH-level mechanism is the same
            payload["base_backup_path"] = path
            payload["base_backup_name"] = last_full

    # Build the SQL and fire it. We don't go through the HTTP endpoint
    # — that would require an internal HTTP call — instead inline the
    # same logic.
    target, err = _backup_target_clause(payload)
    if err:
        _backup_record_failure(sched_id, f"target error: {err}")
        return
    dest, err = _backup_file_clause(payload)
    if err:
        _backup_record_failure(sched_id, f"dest error: {err}")
        return

    settings = []
    if payload.get("base_backup_path") and payload.get("base_backup_name"):
        bp = payload["base_backup_path"].rstrip("/")
        bn = payload["base_backup_name"]
        if not bn.endswith(".zip"): bn = bn + ".zip"
        settings.append(f"base_backup = File('{bp}/{bn}')")

    sql = f"BACKUP {target} TO {dest}"
    if settings:
        sql += " SETTINGS " + ", ".join(settings)
    sql += " ASYNC"

    try:
        client = _get_client(payload)
        result = client.query(sql)
        rows = result.result_rows or []
        backup_id = str(rows[0][0]) if rows else ""
        # Update schedule state
        next_run = _backup_compute_next_run(sched_row["cron"])
        updates = {
            "last_run_at":          now,
            "last_run_name":        name,
            "last_status":          "ok",
            "last_error":           None,
            "last_backup_id":       backup_id,
            "next_run_at":          next_run,
            "consecutive_failures": 0,
        }
        if backup_type == "full":
            updates["last_full_name"] = name
        db_global().execute(
            "UPDATE backup_schedules SET "
            "last_run_at=?, last_run_name=?, last_status=?, last_error=?, "
            "last_backup_id=?, next_run_at=?, consecutive_failures=?"
            + (", last_full_name=?" if "last_full_name" in updates else "")
            + " WHERE id=?",
            (updates["last_run_at"], updates["last_run_name"],
             updates["last_status"], updates["last_error"],
             updates["last_backup_id"], updates["next_run_at"],
             updates["consecutive_failures"],
             *( (updates["last_full_name"],) if "last_full_name" in updates else () ),
             sched_id))
        db_global().commit()
        audit("Backup Schedule Fired", panel="backup",
              detail=f"schedule={sched_row['name']} type={backup_type} name={name} id={backup_id}")
    except Exception as e:
        _backup_record_failure(sched_id, str(e)[:300])

def _backup_record_failure(sched_id, err):
    try:
        next_run = None
        row = db_global().execute(
            "SELECT cron, consecutive_failures FROM backup_schedules WHERE id=?",
            (sched_id,)).fetchone()
        if row:
            next_run = _backup_compute_next_run(row["cron"])
            fails = (row["consecutive_failures"] or 0) + 1
        else:
            fails = 1
        db_global().execute(
            "UPDATE backup_schedules SET last_run_at=now(), "
            "last_status=?, last_error=?, consecutive_failures=?, "
            "next_run_at=? WHERE id=?",
            (f"error: {err[:60]}", err, fails, next_run, sched_id))
        db_global().commit()
        logger.warning(f"backup schedule {sched_id} failed: {err}")
    except Exception as e2:
        logger.error(f"backup schedule bookkeeping failed: {e2}")

def _backup_scheduler_loop():
    """Wake every 30 s; fire any schedules whose next_run_at has passed."""
    if not _CRONITER_AVAILABLE:
        logger.warning("croniter not installed — backup scheduler disabled")
        return
    while True:
        try:
            with app.app_context():
                # Backfill missing next_run_at on first sight (e.g. when a
                # schedule was just created and the trigger hasn't filled it).
                missing = db_global().execute(
                    "SELECT id, cron FROM backup_schedules "
                    "WHERE enabled=TRUE AND next_run_at IS NULL"
                ).fetchall()
                for r in missing:
                    nx = _backup_compute_next_run(r["cron"])
                    if nx:
                        db_global().execute(
                            "UPDATE backup_schedules SET next_run_at=? WHERE id=?",
                            (nx, r["id"]))
                if missing:
                    db_global().commit()

                # Due now?
                due = db_global().execute(
                    "SELECT * FROM backup_schedules "
                    "WHERE enabled=TRUE AND next_run_at IS NOT NULL "
                    "  AND next_run_at <= now() "
                    "ORDER BY next_run_at ASC LIMIT 5"
                ).fetchall()
                for s in due:
                    _backup_fire_schedule(dict(s))
            time.sleep(30)
        except Exception as e:
            logger.error(f"backup scheduler error: {e}")
            time.sleep(60)

threading.Thread(target=_backup_scheduler_loop, daemon=True,
                 name="backup-scheduler").start()


# ─── REST: backup schedules CRUD ─────────────────────────────────────────
def _schedule_to_dict(row):
    return {
        "id":              row["id"],
        "name":            row["name"],
        "enabled":         bool(row["enabled"]),
        "cron":            row["cron"],
        "target":          row["target"],
        "db_name":         row["db_name"] or "",
        "tables":          row["tables"] or "",
        "backup_type":     row["backup_type"],
        "storage_path":    row["storage_path"],
        "name_template":   row["name_template"],
        "connection_id":   row.get("connection_id"),
        "last_full_name":  row["last_full_name"] or "",
        "last_run_name":   row["last_run_name"] or "",
        "last_run_at":     str(row["last_run_at"]) if row["last_run_at"] else None,
        "last_status":     row["last_status"] or "",
        "last_error":      row["last_error"] or "",
        "last_backup_id":  row["last_backup_id"] or "",
        "next_run_at":     str(row["next_run_at"]) if row["next_run_at"] else None,
        "consecutive_failures": row["consecutive_failures"] or 0,
        "created_at":      str(row["created_at"]),
    }

@app.route("/api/backup/schedules", methods=["GET", "POST"])
def backup_schedules():
    if request.method == "GET":
        rows = db_global().execute(
            "SELECT * FROM backup_schedules ORDER BY name"
        ).fetchall()
        return jsonify({
            "schedules": [_schedule_to_dict(r) for r in rows],
            "presets":   [{"key":k,"cron":c,"label":l} for (k,c,l) in BACKUP_CRON_PRESETS],
            "croniter_installed": _CRONITER_AVAILABLE,
        })

    # POST = create
    d = request.json or {}
    name = (d.get("name") or "").strip()[:120]
    cron = (d.get("cron") or "").strip()[:64]
    if not name or not cron:
        return jsonify({"error": "name and cron are required"}), 400
    if not _CRONITER_AVAILABLE:
        return jsonify({"error": "croniter not installed on server"}), 500
    # Validate cron
    try:
        _croniter(cron, datetime.now())
    except Exception as e:
        return jsonify({"error": f"invalid cron expression: {e}"}), 400

    next_run = _backup_compute_next_run(cron)
    cur = db_global().execute(
        "INSERT INTO backup_schedules("
        "  name, enabled, cron, target, db_name, tables, backup_type, "
        "  storage_path, name_template, connection_id, next_run_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (name,
         bool(d.get("enabled", True)),
         cron,
         (d.get("target") or "database").strip(),
         (d.get("db_name") or "").strip()[:120],
         (d.get("tables") or "").strip()[:1000],
         (d.get("backup_type") or "full").strip(),
         (d.get("storage_path") or "").strip()[:500],
         (d.get("name_template") or "{db}_{type}_{datetime}.zip").strip()[:200],
         d.get("connection_id") or None,
         next_run,
         ))
    new_id = cur.fetchone()["id"]
    db_global().commit()
    audit("Create Backup Schedule", panel="backup",
          detail=f"id={new_id} name={name} cron={cron} type={d.get('backup_type')}")
    return jsonify({"id": new_id})

@app.route("/api/backup/schedules/<int:sid>", methods=["PATCH", "DELETE"])
def backup_schedule_one(sid):
    row = db_global().execute(
        "SELECT * FROM backup_schedules WHERE id=?", (sid,)
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404

    if request.method == "DELETE":
        db_global().execute("DELETE FROM backup_schedules WHERE id=?", (sid,))
        db_global().commit()
        audit("Delete Backup Schedule", panel="backup",
              detail=f"id={sid} name={row['name']}")
        return jsonify({"ok": True})

    d = request.json or {}
    fields, params = [], []
    def _f(name, val): fields.append(f"{name}=?"); params.append(val)
    for k in ("name","cron","target","db_name","tables","backup_type",
              "storage_path","name_template"):
        if k in d:
            _f(k, (d[k] or "").strip() if isinstance(d[k], str) else d[k])
    if "enabled" in d:        _f("enabled", bool(d["enabled"]))
    if "connection_id" in d:  _f("connection_id", d["connection_id"] or None)

    # If cron changed, recompute next_run_at
    if "cron" in d:
        try:
            _croniter(d["cron"], datetime.now())
        except Exception as e:
            return jsonify({"error": f"invalid cron: {e}"}), 400
        nx = _backup_compute_next_run(d["cron"])
        _f("next_run_at", nx)

    if not fields:
        return jsonify({"ok": True})
    fields.append("updated_at=now()")
    params.append(sid)
    db_global().execute(
        f"UPDATE backup_schedules SET {','.join(fields)} WHERE id=?", params)
    db_global().commit()
    audit("Update Backup Schedule", panel="backup",
          detail=f"id={sid} name={row['name']} fields={list(d.keys())}")
    return jsonify({"ok": True})

@app.route("/api/backup/schedules/<int:sid>/run-now", methods=["POST"])
def backup_schedule_run_now(sid):
    row = db_global().execute(
        "SELECT * FROM backup_schedules WHERE id=?", (sid,)
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    # Audit the manual trigger with the calling user's identity. The
    # subsequent _backup_fire_schedule call emits a separate system-level
    # "Backup Schedule Fired" event; joining the two by schedule name
    # gives full accountability — who triggered + what got produced.
    audit("Manual Trigger Backup Schedule", panel="backup",
          detail=f"schedule_id={sid} name={row['name']} "
                 f"type={row['backup_type']} target={row['target']} "
                 f"db={row['db_name'] or ''}")
    # Fire in-thread; this gives the caller an immediate result code
    # rather than just "fired"
    try:
        _backup_fire_schedule(dict(row))
        # Re-read updated state
        row2 = db_global().execute(
            "SELECT last_run_name, last_status, last_error, last_backup_id "
            "FROM backup_schedules WHERE id=?", (sid,)).fetchone()
        return jsonify({
            "ok": True,
            "last_run_name":  row2["last_run_name"],
            "last_status":    row2["last_status"],
            "last_error":     row2["last_error"],
            "last_backup_id": row2["last_backup_id"],
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:500]}), 500


# ─── REST endpoints ──────────────────────────────────────────────────────
def _siem_dest_to_dict(row, mask_auth=True):
    """Serialise a destination row for the API. Masks the auth header
    (only the value portion) so secrets aren't echoed back to clients."""
    auth = row["auth_header"] or ""
    if mask_auth and auth and ":" in auth:
        name, _, val = auth.partition(":")
        masked = name + ": " + ("•" * min(8, len(val.strip())))
        auth_display = masked
        has_auth = True
    else:
        auth_display = auth if auth else ""
        has_auth = bool(auth)
    return {
        "id":   row["id"],
        "name": row["name"],
        "url":  row["url"],
        "format": row["format"],
        "auth_header_display": auth_display,
        "has_auth": has_auth,
        "enabled": bool(row["enabled"]),
        "filter_actions": row["filter_actions"] or "",
        "last_forwarded_id":    row["last_forwarded_id"],
        "last_attempt_at":      str(row["last_attempt_at"]) if row["last_attempt_at"] else None,
        "last_status":          row["last_status"] or "",
        "last_error":           row["last_error"] or "",
        "consecutive_failures": row["consecutive_failures"] or 0,
        "created_at":           str(row["created_at"]),
    }

@app.route("/api/siem/destinations", methods=["GET","POST"])
def siem_destinations():
    if request.method == "GET":
        rows = db_global().execute(
            "SELECT * FROM siem_destinations ORDER BY id"
        ).fetchall()
        # Also surface global telemetry: total audit_events vs the largest
        # last_forwarded_id, so the UI can show a 'lag' metric.
        try:
            mx = db_global().execute(
                "SELECT COALESCE(MAX(id),0) AS m FROM audit_events"
            ).fetchone()
            max_event_id = mx["m"]
        except Exception:
            max_event_id = 0
        return jsonify({
            "destinations": [_siem_dest_to_dict(r) for r in rows],
            "max_audit_event_id": max_event_id,
        })

    # POST = create
    d = request.json or {}
    name = (d.get("name") or "").strip()[:120]
    url  = (d.get("url") or "").strip()[:500]
    fmt  = (d.get("format") or "json").strip()
    auth = (d.get("auth_header") or "").strip()[:500] or None
    enabled = bool(d.get("enabled", True))
    filter_actions = (d.get("filter_actions") or "").strip() or None
    if not name or not url:
        return jsonify({"error":"name and url are required"}), 400
    if fmt not in _SIEM_FORMATTERS:
        return jsonify({"error":f"format must be one of {list(_SIEM_FORMATTERS)}"}), 400
    if not (url.startswith("https://") or url.startswith("http://")):
        return jsonify({"error":"url must start with http:// or https://"}), 400

    # New destinations skip forward to the current tip of audit_events so a
    # freshly added destination doesn't get blasted with the entire backfill
    # the moment it's enabled. Operators who want backfill can manually
    # reset last_forwarded_id later.
    try:
        tip = db_global().execute("SELECT COALESCE(MAX(id),0) AS m FROM audit_events").fetchone()["m"]
    except Exception:
        tip = 0

    cur = db_global().execute(
        "INSERT INTO siem_destinations(name,url,format,auth_header,enabled,filter_actions,last_forwarded_id) "
        "VALUES(?,?,?,?,?,?,?) RETURNING id",
        (name,url,fmt,auth,enabled,filter_actions,tip))
    new_id = cur.fetchone()["id"]
    db_global().commit()
    audit("Create SIEM Destination", panel="siem",
          detail=f"name={name} url={url} format={fmt} enabled={enabled}")
    return jsonify({"id": new_id})

@app.route("/api/siem/destinations/<int:dest_id>", methods=["PATCH","DELETE"])
def siem_destination_one(dest_id):
    row = db_global().execute(
        "SELECT * FROM siem_destinations WHERE id=?", (dest_id,)
    ).fetchone()
    if not row:
        return jsonify({"error":"not found"}), 404

    if request.method == "DELETE":
        db_global().execute("DELETE FROM siem_destinations WHERE id=?", (dest_id,))
        db_global().commit()
        audit("Delete SIEM Destination", panel="siem",
              detail=f"id={dest_id} name={row['name']}")
        return jsonify({"ok": True})

    # PATCH — partial update. Empty/missing fields preserve existing value.
    d = request.json or {}
    fields = []
    params = []
    def _f(name, val):
        fields.append(f"{name}=?"); params.append(val)
    if "name" in d:           _f("name",           (d["name"] or "").strip()[:120])
    if "url" in d:
        u = (d["url"] or "").strip()[:500]
        if u and not (u.startswith("https://") or u.startswith("http://")):
            return jsonify({"error":"url must start with http:// or https://"}), 400
        _f("url", u)
    if "format" in d:
        if d["format"] not in _SIEM_FORMATTERS:
            return jsonify({"error":"invalid format"}), 400
        _f("format", d["format"])
    if "auth_header" in d:
        # Allow clearing with empty string; preserve current value if
        # client sent the masked display back unchanged.
        new_auth = (d["auth_header"] or "").strip()[:500]
        if "•" in new_auth:
            pass  # masked round-trip — don't overwrite
        else:
            _f("auth_header", new_auth or None)
    if "enabled" in d:        _f("enabled",        bool(d["enabled"]))
    if "filter_actions" in d: _f("filter_actions", (d["filter_actions"] or "").strip() or None)

    if not fields:
        return jsonify({"ok": True})
    fields.append("updated_at=now()")
    params.append(dest_id)
    db_global().execute(
        f"UPDATE siem_destinations SET {','.join(fields)} WHERE id=?", params)
    db_global().commit()
    audit("Update SIEM Destination", panel="siem",
          detail=f"id={dest_id} fields={list(d.keys())}")
    return jsonify({"ok": True})

@app.route("/api/siem/destinations/<int:dest_id>/test", methods=["POST"])
def siem_destination_test(dest_id):
    """Send a synthetic audit event to a destination so the operator can
    confirm credentials, URL, and downstream parsing without waiting for
    a real event to be generated."""
    row = db_global().execute(
        "SELECT * FROM siem_destinations WHERE id=?", (dest_id,)
    ).fetchone()
    if not row:
        return jsonify({"error":"not found"}), 404

    u = getattr(g, "user", None) or {}
    synthetic = [{
        "id": -1,
        "ts": datetime.now(timezone.utc),
        "user_id":   u.get("id"),
        "username":  u.get("username") or "siem-test",
        "role":      u.get("role") or "admin",
        "action":    "SIEM Test Event",
        "panel":     "siem",
        "detail":    "Synthetic event from /api/siem/destinations/{}/test — if you can see this, the destination is correctly wired.".format(dest_id),
        "conn_host": "",
        "conn_port": "",
        "conn_user": "",
        "ip":        request.remote_addr or "",
        "result":    "ok",
    }]
    formatter, content_type = _SIEM_FORMATTERS.get(row["format"], (_siem_fmt_json,"application/json"))
    body = formatter(synthetic, _SIEM_SOURCE_HOST)
    headers = {"Content-Type": content_type,
               "User-Agent":   "ClickHouseConsole/4.0 SIEM-Forwarder"}
    if row["auth_header"] and ":" in row["auth_header"]:
        name, _, value = row["auth_header"].partition(":")
        headers[name.strip()] = value.strip()
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(row["url"], data=body.encode("utf-8"),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            http_status = resp.status
            ok = 200 <= http_status < 300
            audit("Test SIEM Destination", panel="siem",
                  detail=f"id={dest_id} name={row['name']} http_status={http_status}")
            return jsonify({"ok": ok, "http_status": http_status,
                            "message": "Delivered" if ok else f"Non-2xx response: HTTP {http_status}"})
    except Exception as e:
        audit("Test SIEM Destination", panel="siem",
              detail=f"id={dest_id} name={row['name']} error={str(e)[:200]}",
              result="error")
        return jsonify({"ok": False, "error": str(e)[:300]})

@app.route("/api/siem/destinations/<int:dest_id>/log", methods=["GET"])
def siem_destination_log(dest_id):
    rows = db_global().execute(
        "SELECT id, ts, status, http_status, batch_size, first_event_id, "
        "       last_event_id, error "
        "  FROM siem_forward_log "
        " WHERE destination_id=? "
        " ORDER BY ts DESC LIMIT 50", (dest_id,)
    ).fetchall()
    return jsonify({"log":[dict(r) for r in rows]})


# ╔════════════════════════════════════════════════════════════════════════╗
# ║  LDAP / Active Directory authentication                                ║
# ║                                                                        ║
# ║  Hybrid login: every user picks either "Console User" (local) or       ║
# ║  "LDAP" at the login screen. The two paths never collide — a local     ║
# ║  account is identified by auth_source='local' and a present password   ║
# ║  hash; an LDAP account by auth_source='ldap' and a populated ldap_dn.  ║
# ║  The local admin always works even when LDAP is misconfigured or       ║
# ║  unreachable, which is the recovery escape hatch.                      ║
# ╚════════════════════════════════════════════════════════════════════════╝

# Priority order when resolving the user's effective role from multiple
# group mappings — highest privilege wins.
_LDAP_ROLE_PRIORITY = {"admin": 4, "developer": 3, "monitoring": 2, "readonly": 1}

def _ldap_load_config():
    """Return the singleton ldap_config row as a dict, or None if no row
    exists yet (fresh install). Safe to call without an app context if
    db_global() inherits one from the caller."""
    try:
        row = db_global().execute(
            "SELECT * FROM ldap_config WHERE id=1"
        ).fetchone()
        if not row:
            return None
        cfg = dict(row)
        # bind_password is stored fernet-encrypted; decrypt transparently so
        # callers keep receiving plaintext. Installs created before encryption
        # hold a plaintext value — if decryption fails, treat it as legacy
        # plaintext and leave it as-is (the next config save re-encrypts it).
        bp = cfg.get("bind_password")
        if bp:
            try:
                cfg["bind_password"] = fernet_decrypt(bp)
            except Exception:
                pass
        return cfg
    except Exception:
        return None

def _ldap_load_mappings():
    """Return list of {id, group_name, role} mappings."""
    try:
        rows = db_global().execute(
            "SELECT id, group_name, role FROM ldap_group_mappings ORDER BY group_name"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []

def _ldap_connect(cfg, bind_dn=None, bind_password=None, timeout=None):
    """Open an LDAP connection using ldap3. Caller supplies either the
    cfg.bind_dn/bind_password (service-account search) or a user DN +
    password (direct user bind). Returns the Connection object on success.
    Raises an Exception with a human-readable string on failure."""
    if not _LDAP_AVAILABLE:
        raise RuntimeError("ldap3 package is not installed on the server")
    url = (cfg.get("server_url") or "").strip()
    if not url:
        raise ValueError("LDAP server_url is not configured")
    use_starttls = bool(cfg.get("use_starttls"))
    to = int(timeout or cfg.get("timeout_seconds") or 10)
    # ldap3 wants the host parts separated; let Server() parse the URL.
    server = _ldap3.Server(url,
                           use_ssl=url.startswith("ldaps://"),
                           get_info=_ldap3.NONE,
                           connect_timeout=to)
    conn = _ldap3.Connection(server,
                             user=bind_dn,
                             password=bind_password,
                             auto_bind=False,
                             receive_timeout=to)
    if use_starttls and not url.startswith("ldaps://"):
        if not conn.start_tls():
            raise RuntimeError(f"StartTLS failed: {conn.result}")
    if not conn.bind():
        # Distinguish credential errors from network errors for the caller.
        rc = conn.result or {}
        desc = rc.get("description") or rc.get("message") or "bind failed"
        raise PermissionError(f"LDAP bind rejected: {desc}")
    return conn

def _ldap_find_user(cfg, username):
    """Search for a user by their login name using the service-account
    bind. Returns (user_dn, attrs_dict) or (None, None) if not found.

    The user_filter is a templated LDAP filter, e.g. "(uid={username})".
    We substitute {username} after escaping per RFC 4515 to make filter
    injection impossible — a malicious username containing parens or
    asterisks gets quoted into harmless characters before the filter is
    sent to the directory server.
    """
    bind_dn  = (cfg.get("bind_dn") or "").strip()
    bind_pwd = cfg.get("bind_password") or ""
    base     = (cfg.get("user_search_base") or "").strip()
    tmpl     = (cfg.get("user_filter") or "(uid={username})").strip()
    if not base:
        raise ValueError("LDAP user_search_base is not configured")
    safe_user = _ldap3.utils.conv.escape_filter_chars(username)
    flt = tmpl.replace("{username}", safe_user)
    conn = _ldap_connect(cfg, bind_dn=bind_dn or None, bind_password=bind_pwd or None)
    try:
        conn.search(search_base=base, search_filter=flt,
                    search_scope=_ldap3.SUBTREE,
                    attributes=["cn","mail","sn","givenName","memberOf"])
        if not conn.entries:
            return None, None
        e = conn.entries[0]
        attrs = {
            "cn":         (str(e.cn) if "cn" in e else ""),
            "mail":       (str(e.mail) if "mail" in e else ""),
            "sn":         (str(e.sn) if "sn" in e else ""),
            "givenName":  (str(e.givenName) if "givenName" in e else ""),
            "memberOf":   ([str(g) for g in e.memberOf] if "memberOf" in e else []),
        }
        return str(e.entry_dn), attrs
    finally:
        try: conn.unbind()
        except Exception: pass

def _ldap_user_groups(cfg, user_dn):
    """Discover which groups a user belongs to. Two strategies:
       1. If the user's entry already carries a memberOf list (AD does
          this; many OpenLDAP installations don't), trust it.
       2. Otherwise search the groups subtree for entries that list this
          user in their `member` attribute.
    nested_groups=True activates AD's transitive matching rule OID, which
    follows nested group memberships server-side."""
    bind_dn  = (cfg.get("bind_dn") or "").strip()
    bind_pwd = cfg.get("bind_password") or ""
    base     = (cfg.get("group_search_base") or "").strip()
    tmpl     = (cfg.get("group_filter") or "(member={user_dn})").strip()
    use_nested = bool(cfg.get("nested_groups"))
    if not base:
        return []
    safe_dn = _ldap3.utils.conv.escape_filter_chars(user_dn)
    # When nested-group resolution is on, swap the inner attribute name to
    # the AD-specific OID. AD-only — OpenLDAP ignores the OID and falls
    # back to literal matching.
    if use_nested and "member=" in tmpl:
        tmpl = tmpl.replace("member=", "member:1.2.840.113556.1.4.1941:=")
    flt = tmpl.replace("{user_dn}", safe_dn)
    conn = _ldap_connect(cfg, bind_dn=bind_dn or None, bind_password=bind_pwd or None)
    try:
        conn.search(search_base=base, search_filter=flt,
                    search_scope=_ldap3.SUBTREE,
                    attributes=["cn"])
        return [str(e.cn) for e in conn.entries if "cn" in e]
    finally:
        try: conn.unbind()
        except Exception: pass

def _ldap_resolve_role(group_names, mappings, default_role):
    """Highest-privilege role wins. group_names is the list of cn= values
    we observed for the user; mappings is the configured table."""
    by_group = {m["group_name"]: m["role"] for m in mappings}
    candidates = [by_group[g] for g in group_names if g in by_group]
    if not candidates:
        return default_role
    return max(candidates, key=lambda r: _LDAP_ROLE_PRIORITY.get(r, 0))

def _ldap_provision_or_update(username, ldap_dn, attrs, role):
    """Upsert the users row for an LDAP user. Auto-provisions on first
    login; updates role + last_login_at on subsequent logins so group
    changes in the directory take effect at the next login. Returns the
    users.id."""
    existing = db().execute(
        "SELECT id, role, auth_source FROM users WHERE username=?", (username,)
    ).fetchone()
    if existing:
        # Refuse to overwrite a local account with an LDAP login under the
        # same name — they're two distinct identities, never the same one.
        if existing["auth_source"] == "local":
            raise PermissionError(
                f"username '{username}' already exists as a local account; "
                "LDAP and local accounts cannot share a name"
            )
        # Existing LDAP user — refresh role + last_login. Group changes
        # in the directory propagate at the next login.
        db().execute(
            "UPDATE users SET role=?, ldap_dn=?, email=?, first_name=?, last_name=?, "
            "last_login_at=now() WHERE id=?",
            (role, ldap_dn, attrs.get("mail",""),
             attrs.get("givenName",""), attrs.get("sn",""), existing["id"]))
        db().commit()
        return existing["id"]
    # First login — provision a new row. password_hash stays NULL.
    cur = db().execute(
        "INSERT INTO users(username, email, first_name, last_name, password_hash, "
        "role, is_active, auth_source, ldap_dn, last_login_at) "
        "VALUES(?,?,?,?,NULL,?,1,'ldap',?,now()) RETURNING id",
        (username, attrs.get("mail",""), attrs.get("givenName",""),
         attrs.get("sn",""), role, ldap_dn))
    new_id = cur.fetchone()["id"]
    db().commit()
    return new_id

def _ldap_authenticate(username, password):
    """End-to-end LDAP login. Returns (user_id, role) on success.
    Raises an exception with a clear message on any failure."""
    cfg = _ldap_load_config()
    if not cfg or not cfg.get("enabled"):
        raise PermissionError("LDAP authentication is not enabled")
    user_dn, attrs = _ldap_find_user(cfg, username)
    if not user_dn:
        raise PermissionError("user not found in directory")
    # Direct bind as the user with the supplied password — the actual
    # password check. We don't pre-check via the search bind.
    try:
        user_conn = _ldap_connect(cfg, bind_dn=user_dn, bind_password=password)
        try: user_conn.unbind()
        except Exception: pass
    except PermissionError:
        raise PermissionError("invalid password")
    # Resolve group memberships and the effective role.
    groups = []
    # If memberOf came back in the user search, prefer it (one round trip
    # fewer than re-searching the groups subtree).
    if attrs and attrs.get("memberOf"):
        # memberOf carries full DNs; extract cn=X from each.
        for dn in attrs["memberOf"]:
            for part in dn.split(","):
                p = part.strip()
                if p.lower().startswith("cn="):
                    groups.append(p[3:]); break
    else:
        groups = _ldap_user_groups(cfg, user_dn)
    mappings = _ldap_load_mappings()
    role = _ldap_resolve_role(groups, mappings, cfg.get("default_role") or "readonly")
    user_id = _ldap_provision_or_update(username, user_dn, attrs or {}, role)
    return user_id, role, groups


# ─── REST: which auth methods does the login screen offer? ────────────
@app.route("/api/auth/methods", methods=["GET"])
def auth_methods():
    """Public (pre-login). The login screen calls this to decide which
    radio buttons to show. Returns minimal info — no secrets."""
    cfg = _ldap_load_config()
    return jsonify({
        "local": True,                              # always available
        "ldap":  bool(cfg and cfg.get("enabled") and _LDAP_AVAILABLE),
    })

# ─── REST: admin endpoints to manage LDAP config + group mappings ─────
@app.route("/api/ldap/config", methods=["GET","POST"])
def ldap_config():
    if request.method == "GET":
        cfg = _ldap_load_config() or {}
        # Mask bind password — only an indicator of presence is exposed.
        if cfg.get("bind_password"):
            cfg["bind_password_set"] = True
            cfg["bind_password"] = ""
        else:
            cfg["bind_password_set"] = False
        cfg["ldap3_installed"] = _LDAP_AVAILABLE
        return jsonify(cfg)
    # POST = upsert. Empty bind_password preserves the existing one (so
    # the operator can edit other fields without re-entering the secret).
    d = request.json or {}
    existing = _ldap_load_config() or {}
    new_pwd = d.get("bind_password")
    keep_pwd = (new_pwd is None or new_pwd == "")
    final_pwd = existing.get("bind_password") if keep_pwd else new_pwd
    # Encrypt at rest with the master key (same fernet as ClickHouse creds), so
    # the service-account secret is never stored in plaintext and is covered by
    # master-key rotation. Empty means "no bind password" — stored as-is.
    final_pwd_enc = fernet_encrypt(final_pwd) if final_pwd else ""
    db_global().execute("""
        INSERT INTO ldap_config(id, enabled, server_url, use_starttls, bind_dn,
                                bind_password, user_search_base, user_filter,
                                group_search_base, group_filter, default_role,
                                nested_groups, timeout_seconds, updated_at)
        VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
        ON CONFLICT (id) DO UPDATE SET
          enabled=EXCLUDED.enabled, server_url=EXCLUDED.server_url,
          use_starttls=EXCLUDED.use_starttls, bind_dn=EXCLUDED.bind_dn,
          bind_password=EXCLUDED.bind_password,
          user_search_base=EXCLUDED.user_search_base,
          user_filter=EXCLUDED.user_filter,
          group_search_base=EXCLUDED.group_search_base,
          group_filter=EXCLUDED.group_filter,
          default_role=EXCLUDED.default_role,
          nested_groups=EXCLUDED.nested_groups,
          timeout_seconds=EXCLUDED.timeout_seconds,
          updated_at=now()
    """, (
        bool(d.get("enabled")),
        (d.get("server_url") or "").strip()[:500],
        bool(d.get("use_starttls")),
        (d.get("bind_dn") or "").strip()[:500],
        final_pwd_enc,
        (d.get("user_search_base") or "").strip()[:500],
        (d.get("user_filter") or "(uid={username})").strip()[:500],
        (d.get("group_search_base") or "").strip()[:500],
        (d.get("group_filter") or "(member={user_dn})").strip()[:500],
        (d.get("default_role") or "readonly").strip()[:20],
        bool(d.get("nested_groups")),
        int(d.get("timeout_seconds") or 10),
    ))
    db_global().commit()
    audit("Update LDAP Config", panel="ldap",
          detail=f"enabled={bool(d.get('enabled'))} server={d.get('server_url','')}")
    return jsonify({"ok": True})

@app.route("/api/ldap/test", methods=["POST"])
def ldap_test():
    """Verify the configured (or supplied) LDAP settings without saving
    them. Body may contain a full config to test — useful for "test before
    save". Returns structured per-step success/failure so the UI can show
    exactly what's wrong."""
    d = request.json or {}
    # Caller may either send a full config to test, or send nothing in
    # which case we use the currently-saved config.
    if d.get("server_url"):
        cfg = dict(d)
        # If the caller submitted no password but a saved one exists,
        # use the saved one — same masked-edit behaviour as the form.
        if not cfg.get("bind_password"):
            saved = _ldap_load_config() or {}
            cfg["bind_password"] = saved.get("bind_password") or ""
    else:
        cfg = _ldap_load_config() or {}
    if not cfg.get("server_url"):
        return jsonify({"ok": False, "step": "config", "error": "no server URL"})
    if not _LDAP_AVAILABLE:
        return jsonify({"ok": False, "step": "package", "error": "ldap3 not installed"})

    # Step 1: bind with the service account
    try:
        conn = _ldap_connect(cfg, bind_dn=(cfg.get("bind_dn") or None),
                             bind_password=(cfg.get("bind_password") or None))
    except Exception as e:
        return jsonify({"ok": False, "step": "bind", "error": str(e)[:300]})
    # Step 2: search the user base to confirm reach + permissions
    try:
        base = (cfg.get("user_search_base") or "").strip()
        if base:
            conn.search(search_base=base, search_filter="(objectClass=*)",
                        search_scope=_ldap3.SUBTREE, attributes=["cn"], size_limit=5)
            n_users = len(conn.entries)
        else:
            n_users = 0
        conn.unbind()
    except Exception as e:
        try: conn.unbind()
        except Exception: pass
        return jsonify({"ok": False, "step": "search", "error": str(e)[:300]})

    audit("Test LDAP Connection", panel="ldap",
          detail=f"server={cfg.get('server_url')} users_found={n_users}")
    return jsonify({"ok": True, "step": "complete",
                    "message": f"Bind OK, search OK, found {n_users} entries under user base"})

@app.route("/api/ldap/mappings", methods=["GET","POST"])
def ldap_mappings():
    if request.method == "GET":
        return jsonify({"mappings": _ldap_load_mappings()})
    d = request.json or {}
    name = (d.get("group_name") or "").strip()[:200]
    role = (d.get("role") or "").strip()
    if not name or role not in _LDAP_ROLE_PRIORITY:
        return jsonify({"error": "group_name and a valid role required"}), 400
    db_global().execute(
        "INSERT INTO ldap_group_mappings(group_name, role) VALUES(?, ?) "
        "ON CONFLICT (group_name) DO UPDATE SET role=EXCLUDED.role",
        (name, role))
    db_global().commit()
    audit("Update LDAP Group Mapping", panel="ldap",
          detail=f"group={name} role={role}")
    return jsonify({"ok": True})

@app.route("/api/ldap/mappings/<int:mapping_id>", methods=["DELETE"])
def ldap_mapping_delete(mapping_id):
    row = db_global().execute(
        "SELECT group_name FROM ldap_group_mappings WHERE id=?", (mapping_id,)
    ).fetchone()
    if not row:
        return jsonify({"error":"not found"}), 404
    db_global().execute("DELETE FROM ldap_group_mappings WHERE id=?", (mapping_id,))
    db_global().commit()
    audit("Delete LDAP Group Mapping", panel="ldap",
          detail=f"group={row['group_name']}")
    return jsonify({"ok": True})


@app.route("/api/alerts/config",methods=["GET","POST"])
def alerts_config():
    if request.method=="GET": return jsonify(_load_alert_cfg())
    _save_alert_cfg(request.json or {}); logger.info("Alert config saved")
    return jsonify({"ok":True})

@app.route("/api/alerts/test",methods=["POST"])
def alerts_test():
    d=request.json or {}; ch=d.get("channel","webhook"); cfg=_load_alert_cfg(); channels=cfg.get("channels",{})
    try:
        if ch=="webhook":
            wb=channels.get("webhook",{})
            if not wb.get("url"): return jsonify({"error":"No webhook URL configured"})
            _webhook(wb["url"],wb.get("type","slack"),"[Test] ClickHouse Alert","Test alert from BlancoByte ClickHouse Console.")
        else:
            em=channels.get("email",{})
            if not em.get("to"): return jsonify({"error":"No email recipients configured"})
            _email(em,"[Test] ClickHouse Alert","Test alert from BlancoByte ClickHouse Console.")
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/alerts/status")
def alerts_status():
    cfg=_load_alert_cfg()
    return jsonify({"enabled":cfg.get("enabled",False),"interval":cfg.get("interval",60),
        "history":_alert_history[:30],
        "cooldowns":{k:time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(v)) for k,v in _alert_cooldowns.items()}})

# ── cluster topology & multi-node ────────────────────────────────────────────
_node_pool = {}
_node_pool_lock = threading.Lock()

def _node_client(host, port, user, password):
    key = f"{host}:{port}:{user}"
    with _node_pool_lock:
        if key not in _node_pool:
            import clickhouse_connect as _cc
            try:
                _node_pool[key] = _cc.get_client(
                    host=host, port=int(port), username=user,
                    password=password or '', connect_timeout=5, query_limit=0)
            except Exception as e:
                raise RuntimeError(f"Cannot connect to {host}:{port} — {e}")
    return _node_pool[key]

def _qsafe(cl, sql):
    try: return cl.query(sql).result_rows
    except Exception as e: logger.warning(f"Cluster query: {e}"); return []

def _sys_select(cl, table, cols):
    """Build a SELECT list for a system table that never references a column
    the connected server does not have: available names are read from
    system.columns (a query that cannot fail) and anything missing comes back
    as NULL under the same alias, so row shape / index mapping stay fixed
    across ClickHouse versions. Born out of code-47 UNKNOWN_IDENTIFIER
    failures (replication_queue.exception vs last_exception,
    mutations.parts_done) landing as failed queries in query_log."""
    try:
        rows = _qsafe(cl, f"SELECT name FROM system.columns WHERE database='system' AND table='{table}'")
        have = {str(r[0]) for r in rows}
    except Exception:
        have = set()
    return ", ".join([f"`{c}`" if c in have else f"NULL AS `{c}`" for c in cols])

def _zk_table_exists(cl):
    """system.zookeeper only exists when ClickHouse has a [Zoo]Keeper section
    configured (the prerequisite for replicated/sharded setups). Probing it
    blindly on keeper-less single nodes throws UNKNOWN_TABLE, and every probe
    lands as a *failed query* in system.query_log - so the console would
    manufacture the very failures its Failed Queries page then reports.
    This existence check goes through system.tables, which never fails."""
    try:
        chk = _qsafe(cl, "SELECT count() FROM system.tables WHERE database='system' AND name='zookeeper'")
        return bool(chk) and len(chk[0]) > 0 and int(chk[0][0] or 0) > 0
    except Exception:
        return False

@app.route("/api/cluster/topology", methods=["POST"])
def cluster_topology():
    d = request.json or {}
    try:
        cl = _get_client(d)
        clusters_rows = _qsafe(cl, "SELECT cluster,shard_num,replica_num,host_name,port,user,is_local FROM system.clusters ORDER BY cluster,shard_num,replica_num")
        replica_rows  = _qsafe(cl, "SELECT database,table,engine,replica_name,replica_path,is_leader,is_readonly,absolute_delay,queue_size,inserts_in_queue,merges_in_queue,total_replicas,active_replicas FROM system.replicas ORDER BY database,table")
        node_rows     = _qsafe(cl, "SELECT hostName(),version()")
        # Keeper reachability. Existence-gated: on keeper-less single nodes
        # the probe is skipped entirely so no failed query ever reaches
        # query_log; on replicated/sharded setups (keeper configured) the
        # probe runs exactly as before.
        zk_ok = False
        if _zk_table_exists(cl):
            try: _qsafe(cl,"SELECT * FROM system.zookeeper WHERE path='/' LIMIT 1"); zk_ok=True
            except: pass

        clusters = {}
        # ClickHouse ships with built-in dummy cluster definitions
        # (test_shard_localhost, test_cluster_two_shards, etc.) that
        # appear in system.clusters even when the operator hasn't
        # configured any. Filter them out — they inflate num_shards
        # in single-cluster deployments and clutter the picker.
        _BUILTIN_CLUSTERS = {
            'test_shard_localhost',
            'test_shard_localhost_secure',
            'test_cluster_one_shard_three_replicas_localhost',
            'test_cluster_two_shards',
            'test_cluster_two_shards_localhost',
            'test_cluster_two_shards_internal_replication',
            'test_unavailable_shard',
            'default',
        }
        for r in clusters_rows:
            cn=r[0]; sn=str(r[1])
            if cn in _BUILTIN_CLUSTERS: continue
            if cn not in clusters: clusters[cn]={"shards":{}}
            if sn not in clusters[cn]["shards"]: clusters[cn]["shards"][sn]=[]
            clusters[cn]["shards"][sn].append({"shard_num":int(r[1]),"replica_num":int(r[2]),"host":r[3],"port":int(r[4]),"user":r[5],"is_local":bool(r[6])})

        num_shards   = max((len(v["shards"]) for v in clusters.values()),default=0)
        num_replicas = max((max(len(s) for s in v["shards"].values()) for v in clusters.values()),default=0) if clusters else 0
        has_rep_tables = len(replica_rows) > 0   # ReplicatedMergeTree tables exist?

        # Classify primarily from topology (num_shards / num_replicas), not
        # from whether ReplicatedMergeTree tables happen to exist yet. An
        # empty replicated cluster is still a replicated cluster — its
        # `system.clusters` definition already says so, even before any
        # tables are created. Previous logic checked has_rep_tables first,
        # which silently classified a fresh replicated cluster as "single"
        # and hid the header badge.
        if num_shards > 1:
            ttype  = "sharded"
            tlabel = f"{num_shards} Shards"
        elif num_replicas > 1:
            ttype  = "replicated"
            tlabel = f"Replicated ({num_replicas} Replicas)"
        elif has_rep_tables:
            # Edge case: single-node deployment hosting ReplicatedMergeTree
            # tables (cross-node replication via an external Keeper).
            ttype  = "replicated"
            tlabel = f"Replicated ({len(replica_rows)} tables)"
        else:
            ttype  = "single"
            tlabel = "Single Node"

        replicas=[{"database":r[0],"table":r[1],"engine":r[2],"replica_name":r[3],
            "is_leader":bool(r[5]),"is_readonly":bool(r[6]),"delay":int(r[7]),
            "queue_size":int(r[8]),"inserts_queue":int(r[9]),"merges_queue":int(r[10]),
            "total_replicas":int(r[11]),"active_replicas":int(r[12])} for r in replica_rows]

        cl.close()
        return jsonify({"type":ttype,"label":tlabel,"clusters":clusters,"replicas":replicas,
            "current_node":{"host":node_rows[0][0],"version":node_rows[0][1]} if node_rows else {},
            "zookeeper":zk_ok,"num_shards":num_shards,"num_replicas":num_replicas})
    except Exception as e:
        return jsonify({"error":str(e),"type":"single","label":"Single Node","clusters":{},"replicas":[]})

@app.route("/api/cluster/nodes", methods=["POST"])
def cluster_nodes():
    d = request.json or {}
    try:
        cl = _get_client(d)
        # Read cluster topology from system.clusters. The errors_count column
        # tells us if the node has had recent failures (avoiding a slow per-node probe).
        rows = _qsafe(cl,"SELECT cluster,shard_num,replica_num,host_name,port,user,is_local,errors_count FROM system.clusters ORDER BY cluster,shard_num,replica_num")
        seen=set(); nodes=[]
        for r in rows:
            key=r[3]+':'+str(r[4])
            if key in seen: continue
            seen.add(key)
            errors=int(r[7]) if len(r)>7 and r[7] is not None else 0
            node={"cluster":r[0],"shard":int(r[1]),"replica":int(r[2]),"host":r[3],"port":int(r[4]),
                  "user":r[5],"is_local":bool(r[6]),
                  "status":"up" if (errors==0 or bool(r[6])) else "down","errors_count":errors}
            nodes.append(node)
        if not nodes:
            # Standalone server with empty system.clusters: synthesise the local
            # node so the UI (Overview/Nodes) still shows metrics via the main connection.
            hn="localhost"
            try:
                _idr=_qsafe(cl,"SELECT hostName()")
                if _idr and _idr[0]: hn=str(_idr[0][0])
            except Exception: pass
            nodes.append({"cluster":"","shard":1,"replica":1,"host":hn,"port":9000,
                          "user":d.get("user","default"),"is_local":True,"status":"up","errors_count":0})
        cl.close()
        return jsonify({"nodes":nodes})
    except Exception as e:
        return jsonify({"error":str(e),"nodes":[]})

@app.route("/api/cluster/node-metrics", methods=["POST"])
def cluster_node_metrics():
    d = request.json or {}
    host=d.get("target_host",d.get("host","localhost")); port=int(d.get("target_port",d.get("port",8123)))
    user=d.get("target_user",d.get("user","default")); pw=d.get("password","")
    use_main=bool(d.get("use_main"))
    try:
        cl=_get_client(d) if use_main else _node_client(host,port,user,pw)
        metrics={r[0]:float(r[1]) for r in _qsafe(cl,"SELECT metric,value FROM system.metrics WHERE metric IN ('Query','BackgroundMergesAndMutationsPoolTask','PartsActive','MemoryTracking','TCPConnection','HTTPConnection','InterserverConnection','MySQLConnection','PostgreSQLConnection')")}
        async_m={r[0]:float(r[1]) for r in _qsafe(cl,"SELECT metric,value FROM system.asynchronous_metrics WHERE metric IN ('MemoryResident','OSMemoryTotal','FilesystemMainPathUsedBytes','FilesystemMainPathTotalBytes','TotalBytesOfMergeTreeTables','Uptime')")}
        procs=[{"query_id":r[0],"user":r[1],"elapsed":round(float(r[2]),2),"memory_usage":int(r[3]),"mem_str":r[4],"query":str(r[5])[:250]}
            for r in _qsafe(cl,"SELECT query_id,user,elapsed,memory_usage,formatReadableSize(memory_usage),query FROM system.processes ORDER BY elapsed DESC")]
        merges=[{"database":r[0],"table":r[1],"elapsed":round(float(r[2]),1),"progress":round(float(r[3])*100,1),"is_mutation":bool(r[4])}
            for r in _qsafe(cl,"SELECT database,table,elapsed,progress,is_mutation FROM system.merges ORDER BY elapsed DESC")]
        if use_main:
            try: cl.close()
            except Exception: pass
        return jsonify({"host":host,"port":port,"metrics":metrics,"async":async_m,"processes":procs,"merges":merges})
    except Exception as e:
        return jsonify({"error":str(e),"host":host,"port":port})

@app.route("/api/cluster/node-kill", methods=["POST"])
def cluster_node_kill():
    d=request.json or {}; qid=_safe_qid(d.get("query_id",""), allow_empty=True)
    host=d.get("target_host",d.get("host","localhost")); port=int(d.get("target_port",d.get("port",8123)))
    user=d.get("target_user",d.get("user","default"))
    if not qid: return jsonify({"error":"No query_id"}),400
    try:
        cl=_node_client(host,port,user,d.get("password",""))
        cl.command(f"KILL QUERY WHERE query_id = '{qid}' ASYNC")
        logger.warning(f"KILL QUERY node={host}:{port} query_id={qid}")
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/cluster/replica-status", methods=["POST"])
def cluster_replica_status():
    d=request.json or {}
    try:
        cl=_get_client(d)
        rows=_qsafe(cl,"SELECT database,table,replica_name,is_leader,is_readonly,absolute_delay,queue_size,inserts_in_queue,merges_in_queue,total_replicas,active_replicas,last_queue_update FROM system.replicas ORDER BY absolute_delay DESC,database,table")
        cl.close()
        return jsonify({"replicas":[{"database":r[0],"table":r[1],"replica_name":r[2],"is_leader":bool(r[3]),"is_readonly":bool(r[4]),"delay":int(r[5]),"queue_size":int(r[6]),"inserts_queue":int(r[7]),"merges_queue":int(r[8]),"total_replicas":int(r[9]),"active_replicas":int(r[10]),"last_update":str(r[11])} for r in rows]})
    except Exception as e: return jsonify({"error":str(e),"replicas":[]})

@app.route("/api/cluster/shard-distribution", methods=["POST"])
@app.route("/api/cluster/non-replicated", methods=["POST"])
def cluster_non_replicated():
    d = request.json or {}
    try:
        cl = _get_client(d)
        rows = _qsafe(cl, """
            SELECT
                t.database, t.name, t.engine,
                sum(p.rows) as rows,
                sum(p.bytes_on_disk) as bytes,
                count() as parts
            FROM system.tables t
            LEFT JOIN system.parts p
                ON p.database = t.database AND p.table = t.name AND p.active
            WHERE t.engine IN (
                'MergeTree','SummingMergeTree','AggregatingMergeTree',
                'ReplacingMergeTree','CollapsingMergeTree',
                'VersionedCollapsingMergeTree','GraphiteMergeTree'
            )
            AND t.database NOT IN ('system','information_schema','INFORMATION_SCHEMA')
            GROUP BY t.database, t.name, t.engine
            ORDER BY bytes DESC, t.database, t.name
        """)
        cl.close()
        return jsonify({"tables": [{
            "database": r[0], "table": r[1], "engine": r[2],
            "rows": int(r[3] or 0), "bytes": int(r[4] or 0), "parts": int(r[5] or 0)
        } for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e), "tables": []})

def cluster_shard_distribution():
    d=request.json or {}; cn=d.get("cluster_name",""); db=_safe_ident(d.get("database",""),"database"); tbl=_safe_ident(d.get("table",""),"table")
    if not cn or not db or not tbl: return jsonify({"error":"cluster_name, database, table required"}),400
    try:
        cl=_get_client(d)
        rows=_qsafe(cl,f"SELECT hostName(),count(),sum(rows),sum(bytes_on_disk) FROM clusterAllReplicas('{cn}',system.parts) WHERE database='{db}' AND table='{tbl}' AND active GROUP BY hostName() ORDER BY hostName()")
        cl.close()
        return jsonify({"distribution":[{"node":r[0],"parts":int(r[1]),"rows":int(r[2]),"bytes":int(r[3])} for r in rows]})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/cluster/all-nodes-metrics", methods=["POST"])
def cluster_all_nodes_metrics():
    """Fetch metrics for ALL nodes via clusterAllReplicas — no direct node connections needed."""
    d = request.json or {}
    cluster_name = d.get("cluster_name", "")
    if not cluster_name:
        return jsonify({"error": "cluster_name required"}), 400
    try:
        cl = _get_client(d)
        # Processes on all nodes
        proc_rows = _qsafe(cl, f"""
            SELECT hostName() as node, query_id, user,
                   elapsed, rows_read, memory_usage,
                   formatReadableSize(memory_usage) as mem_str, query
            FROM clusterAllReplicas('{cluster_name}', system.processes)
            ORDER BY node, elapsed DESC
        """)
        # Metrics on all nodes
        metric_rows = _qsafe(cl, f"""
            SELECT hostName() as node, metric, value
            FROM clusterAllReplicas('{cluster_name}', system.metrics)
            WHERE metric IN ('Query','BackgroundMergesAndMutationsPoolTask','PartsActive','MemoryTracking','TCPConnection','HTTPConnection','InterserverConnection','MySQLConnection','PostgreSQLConnection')
        """)
        # Async metrics on all nodes
        async_rows = _qsafe(cl, f"""
            SELECT hostName() as node, metric, value
            FROM clusterAllReplicas('{cluster_name}', system.asynchronous_metrics)
            WHERE metric IN ('MemoryResident','OSMemoryTotal','FilesystemMainPathUsedBytes',
                             'FilesystemMainPathTotalBytes','TotalBytesOfMergeTreeTables','Uptime')
        """)
        # Merges on all nodes
        merge_rows = _qsafe(cl, f"""
            SELECT hostName() as node, database, table, elapsed, progress, is_mutation,
                   formatReadableSize(total_size_bytes_compressed) as size_str
            FROM clusterAllReplicas('{cluster_name}', system.merges)
            ORDER BY node, elapsed DESC
        """)

        # Group by node
        nodes = {}
        for r in proc_rows:
            node = r[0]
            if node not in nodes: nodes[node] = {"processes": [], "metrics": {}, "async": {}, "merges": []}
            nodes[node]["processes"].append({
                "query_id": r[1], "user": r[2], "elapsed": round(float(r[3]),2),
                "rows_read": int(r[4]), "memory_usage": int(r[5]), "mem_str": r[6],
                "query": str(r[7])[:250]
            })
        for r in metric_rows:
            node = r[0]
            if node not in nodes: nodes[node] = {"processes": [], "metrics": {}, "async": {}, "merges": []}
            nodes[node]["metrics"][r[1]] = float(r[2])
        for r in async_rows:
            node = r[0]
            if node not in nodes: nodes[node] = {"processes": [], "metrics": {}, "async": {}, "merges": []}
            nodes[node]["async"][r[1]] = float(r[2])
        for r in merge_rows:
            node = r[0]
            if node not in nodes: nodes[node] = {"processes": [], "metrics": {}, "async": {}, "merges": []}
            nodes[node]["merges"].append({
                "database": r[1], "table": r[2], "elapsed": round(float(r[3]),1),
                "progress": round(float(r[4])*100,1), "is_mutation": bool(r[5]), "size": r[6]
            })

        cl.close()
        return jsonify({"nodes": nodes, "cluster": cluster_name})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/mutations/list", methods=["POST"])
def mutations_list():
    d = request.json or {}
    db_filter = _safe_ident(d.get("database", ""), "database")
    tbl_filter = _safe_ident(d.get("table", ""), "table")
    status_filter = d.get("status", "")  # all | done | running | stuck
    try:
        cl = _get_client(d)
        where = []
        if db_filter: where.append(f"database='{db_filter}'")
        if tbl_filter: where.append(f"table='{tbl_filter}'")
        where_clause = "WHERE " + " AND ".join(where) if where else ""
        _mu_cols=_sys_select(cl,"mutations",
            ["database","table","mutation_id","command","create_time",
             "parts_to_do","parts_done","is_done","latest_failed_part",
             "latest_fail_time","latest_fail_reason"])
        rows = _qsafe(cl, f"""
            SELECT {_mu_cols}
            FROM system.mutations
            {where_clause}
            ORDER BY create_time DESC
            LIMIT 200
        """)
        mutations = [{
            "database": r[0], "table": r[1], "mutation_id": r[2],
            "command": str(r[3])[:300], "create_time": str(r[4]),
            "parts_to_do": int(r[5] or 0), "parts_done": (int(r[6]) if r[6] is not None else None),
            "is_done": bool(r[7]),
            "latest_failed_part": r[8] or "",
            "latest_fail_time": str(r[9]) if r[9] else "",
            "latest_fail_reason": str(r[10])[:200] if r[10] else "",
            "progress": (round(int(r[6]) / max(int(r[5] or 0) + int(r[6]), 1) * 100, 1) if r[6] is not None else (100.0 if bool(r[7]) else None)),
            "stuck": not bool(r[7]) and r[10] and len(str(r[10])) > 0
        } for r in rows]
        if status_filter == "done":    mutations = [m for m in mutations if m["is_done"]]
        elif status_filter == "running": mutations = [m for m in mutations if not m["is_done"] and not m["stuck"]]
        elif status_filter == "stuck":   mutations = [m for m in mutations if m["stuck"]]
        cl.close()
        return jsonify({"mutations": mutations})
    except Exception as e: return jsonify({"error": str(e), "mutations": []})

@app.route("/api/mutations/kill", methods=["POST"])
def mutations_kill():
    d = request.json or {}
    db = _safe_ident(d.get("database", ""), "database"); tbl = _safe_ident(d.get("table", ""), "table"); mid = d.get("mutation_id", "")
    if not all([db, tbl, mid]): return jsonify({"error": "database, table, mutation_id required"}), 400
    try:
        cl = _get_client(d)
        cl.command(f"KILL MUTATION WHERE database='{db}' AND table='{tbl}' AND mutation_id='{mid}' ASYNC")
        cl.close()
        logger.warning(f"KILL MUTATION {db}.{tbl} {mid}")
        return jsonify({"ok": True})
    except Exception as e: return jsonify({"error": str(e)})

# ── parts inspector ───────────────────────────────────────────────────────────
@app.route("/api/parts/summary", methods=["POST"])
def parts_summary():
    d = request.json or {}
    db = _safe_ident(d.get("database", ""), "database"); tbl = _safe_ident(d.get("table", ""), "table")
    if not db or not tbl: return jsonify({"error": "database and table required"}), 400
    try:
        cl = _get_client(d)
        # Partition summary
        parts_rows = _qsafe(cl, f"""
            SELECT partition, partition_id,
                   count() as parts,
                   sum(rows) as rows,
                   sum(bytes_on_disk) as bytes_disk,
                   sum(data_compressed_bytes) as compressed,
                   sum(data_uncompressed_bytes) as uncompressed,
                   min(min_date) as min_date,
                   max(max_date) as max_date,
                   sum(primary_key_bytes_in_memory) as pk_bytes,
                   countIf(is_frozen=1) as frozen_count,
                   max(modification_time) as last_mod
            FROM system.parts
            WHERE database='{db}' AND table='{tbl}' AND active
            GROUP BY partition, partition_id
            ORDER BY partition DESC
            LIMIT 500
        """)
        # All parts detail
        detail_rows = _qsafe(cl, f"""
            SELECT partition, name, part_type, rows,
                   bytes_on_disk, data_compressed_bytes, data_uncompressed_bytes,
                   marks, modification_time, min_date, max_date,
                   is_frozen, active
            FROM system.parts
            WHERE database='{db}' AND table='{tbl}'
            ORDER BY partition DESC, name
            LIMIT 2000
        """)
        cl.close()
        def _md(v):
            if v is None: return ''
            s = str(v).strip()
            return '' if s in ('', '1970-01-01', '0001-01-01', '0000-00-00', 'None', 'NULL') else s
        partitions = [{
            "partition": r[0], "partition_id": r[1],
            "parts": int(r[2]), "rows": int(r[3]),
            "bytes_disk": int(r[4]), "compressed": int(r[5]), "uncompressed": int(r[6]),
            "min_date": _md(r[7]), "max_date": _md(r[8]),
            "pk_bytes": int(r[9]), "frozen": int(r[10]),
            "last_mod": str(r[11]),
            "ratio": round(int(r[5]) / max(int(r[6]), 1) * 100, 1)
        } for r in parts_rows]
        parts = [{
            "partition": r[0], "name": r[1], "type": r[2],
            "rows": int(r[3]), "bytes_disk": int(r[4]),
            "compressed": int(r[5]), "uncompressed": int(r[6]),
            "marks": int(r[7]), "mod_time": str(r[8]),
            "min_date": _md(r[9]), "max_date": _md(r[10]),
            "frozen": bool(r[11]), "active": bool(r[12]),
        } for r in detail_rows]
        return jsonify({"partitions": partitions, "parts": parts,
                       "total_partitions": len(partitions), "total_parts": len(parts)})
    except Exception as e: return jsonify({"error": str(e)})

# ── zookeeper browser ─────────────────────────────────────────────────────────
@app.route("/api/zookeeper/ls", methods=["POST"])
def zookeeper_ls():
    d = request.json or {}
    path = d.get("path", "/")
    try:
        cl = _get_client(d)
        if not _zk_table_exists(cl):
            cl.close()
            return jsonify({"error": "ZooKeeper/Keeper is not configured on this server (single-node setup)",
                            "zk_configured": False, "path": path, "nodes": []})
        rows = _qsafe(cl, f"SELECT name, value, czxid, mzxid, ctime, mtime, dataLength, numChildren FROM system.zookeeper WHERE path='{path}' ORDER BY name")
        cl.close()
        nodes = [{
            "name": r[0], "value": str(r[1])[:200] if r[1] else "",
            "czxid": int(r[2]), "mzxid": int(r[3]),
            "ctime": str(r[4]), "mtime": str(r[5]),
            "data_len": int(r[6]), "children": int(r[7])
        } for r in rows]
        return jsonify({"path": path, "nodes": nodes})
    except Exception as e: return jsonify({"error": str(e), "nodes": []})

@app.route("/api/zookeeper/replica-status", methods=["POST"])
def zookeeper_replica_status():
    d = request.json or {}
    try:
        cl = _get_client(d)
        # Get clickhouse replica paths from system.replicas
        rep_rows = _qsafe(cl, """
            SELECT database, table, replica_path, replica_name,
                   is_leader, is_readonly, absolute_delay, queue_size
            FROM system.replicas ORDER BY database, table
        """)
        result = []
        # One existence check up front: if a keeper section was removed after
        # replicated tables were created, system.replicas still has rows but
        # system.zookeeper is absent - a blind per-replica query would then
        # write one failed query into query_log per table.
        zk_avail = _zk_table_exists(cl)
        for r in rep_rows:
            path = str(r[2])
            # Try to get ZK children count for this replica
            children = []
            if zk_avail:
                try:
                    zk = _qsafe(cl, f"SELECT name, numChildren FROM system.zookeeper WHERE path='{path}' LIMIT 20")
                    children = [{"name": z[0], "children": int(z[1])} for z in zk]
                except: children = []
            result.append({
                "database": r[0], "table": r[1], "path": path,
                "replica_name": r[3], "is_leader": bool(r[4]),
                "is_readonly": bool(r[5]), "delay": int(r[6]),
                "queue_size": int(r[7]), "zk_nodes": children
            })
        cl.close()
        return jsonify({"replicas": result})
    except Exception as e: return jsonify({"error": str(e), "replicas": []})


# ── dashboard widget query ────────────────────────────────────────────────────
@app.route("/api/dashboard/query", methods=["POST"])
def dashboard_query():
    import time as _time
    d = request.json or {}
    sql = (d.get("sql") or "").strip()
    if not sql: return jsonify({"error": "No SQL"}), 400
    try:
        cl = _get_client(d)
        t0 = _time.monotonic()
        result = cl.query(sql)
        elapsed = round(_time.monotonic()-t0, 3)
        rows = result.result_rows
        try: cols = list(result.column_names)
        except: cols = [f"col{i}" for i in range(len(rows[0]) if rows else 0)]
        safe_rows = [[_to_json(c) for c in row] for row in rows[:100]]
        cl.close()
        return jsonify({"columns": cols, "rows": safe_rows, "elapsed": elapsed,
                       "scalar": str(rows[0][0]) if rows and len(rows[0])==1 else None})
    except Exception as e:
        return jsonify({"error": str(e)})


# ── slow query log ────────────────────────────────────────────────────────────
@app.route("/api/querylog/slow", methods=["POST"])
def querylog_slow():
    d = request.json or {}
    min_dur = float(d.get("min_duration", 0) or 0)
    limit = int(d.get("limit", 50))
    user_filter = d.get("user_filter", "")
    from_time = d.get("from_time", "")
    to_time = d.get("to_time", "")
    query_filter = d.get("query_filter", "")
    event_type = d.get("event_type", "QueryFinish")
    try:
        cl = _get_client(d)
        # Auto-detect a cluster name so we can scan query_log on every node, not just the
        # one we're connected to. The user's complaint: slow queries from other nodes
        # weren't showing up. clusterAllReplicas() is server-side, no client connections.
        cluster_name = ""
        try:
            cn_rows = _qsafe(cl, "SELECT cluster FROM system.clusters WHERE cluster NOT IN ('test_shard_localhost','test_cluster_one_shard_three_replicas_localhost','test_cluster_two_shards_localhost','test_cluster_two_shards_internal_replication','test_unavailable_shard') GROUP BY cluster ORDER BY count() DESC LIMIT 1")
            if cn_rows: cluster_name = str(cn_rows[0][0])
        except: pass
        where = [f"query_duration_ms >= {int(min_dur*1000)}",
                 f"type='{event_type}'",
                 "query NOT LIKE '%system.%'"]
        if user_filter: where.append(f"user='{user_filter}'")
        if from_time:   where.append(f"event_time >= toDateTime('{from_time}')")
        if to_time:     where.append(f"event_time <= toDateTime('{to_time}')")
        if query_filter: where.append(f"query ILIKE '%{query_filter}%'")
        # Use clusterAllReplicas when we know a cluster name; fall back to local otherwise.
        source = f"clusterAllReplicas('{cluster_name}', system.query_log)" if cluster_name else "system.query_log"
        host_col = "hostName() as _host," if cluster_name else "'' as _host,"
        rows = _qsafe(cl, f"""
            SELECT {host_col} query_id, event_time, user, query_duration_ms,
                   read_rows, read_bytes, memory_usage,
                   formatReadableSize(read_bytes) as read_bytes_h,
                   formatReadableSize(memory_usage) as mem_h,
                   exception, normalizeQuery(query) as normalized,
                   query
            FROM {source}
            WHERE {" AND ".join(where)}
            ORDER BY query_duration_ms DESC
            LIMIT {limit}
        """)
        cl.close()
        return jsonify({"queries": [{
            "host": r[0], "query_id": r[1], "event_time": str(r[2]),
            "user": r[3], "duration_ms": int(r[4]),
            "read_rows": int(r[5]), "read_bytes": int(r[6]),
            "memory_usage": int(r[7]), "read_bytes_h": r[8], "mem_h": r[9],
            "exception": str(r[10]) if r[10] else "",
            "normalized": str(r[11])[:300], "query": str(r[12])[:500]
        } for r in rows], "cluster": cluster_name})
    except Exception as e: return jsonify({"error": str(e), "queries": []})


def _query_kind(q):
    """Best-effort statement type from the leading keyword (system.processes
    has query_kind only on newer servers, so derive it from the text instead)."""
    s = (q or "").lstrip()
    while s[:1] in ("(",):
        s = s[1:].lstrip()
    s = s[:14].upper()
    for k in ("SELECT","INSERT","CREATE","ALTER","DROP","SYSTEM","SHOW",
              "DESCRIBE","DESC","OPTIMIZE","TRUNCATE","RENAME","SET","GRANT",
              "REVOKE","KILL","CHECK","EXPLAIN","ATTACH","DETACH"):
        if s.startswith(k):
            return "SELECT" if k == "DESC" else k
    if s.startswith("WITH"):
        return "SELECT"
    return "OTHER"


@app.route("/api/queries/running", methods=["POST"])
def queries_running():
    """Currently executing queries (system.processes) plus a current-snapshot
    summary. Drives the live table and the SUMMARY card on the Running Queries
    page. Refreshed frequently by the client when Live is on."""
    d = request.json or {}
    try:
        cl = _get_client(d)
        rows = _qsafe(cl, """
            SELECT query_id, user, query, current_database,
                   elapsed, read_rows, read_bytes, total_rows_approx,
                   memory_usage, peak_memory_usage, is_cancelled,
                   length(thread_ids) AS threads, toString(address) AS address
            FROM system.processes
            ORDER BY elapsed DESC
        """)
        procs = []
        s_mem = s_rows = s_bytes = s_thr = 0
        for r in rows:
            q = str(r[2] or "")
            tot = int(r[7] or 0); rr = int(r[5] or 0)
            mem = int(r[8] or 0); by = int(r[6] or 0); thr = int(r[11] or 0)
            procs.append({
                "query_id": r[0], "user": r[1], "query": q[:600],
                "database": r[3] or "", "elapsed": round(float(r[4] or 0), 2),
                "read_rows": rr, "read_bytes": by, "total_rows": tot,
                "progress": (round(min(1.0, rr / tot), 4) if tot > 0 else None),
                "memory_usage": mem, "peak_memory": int(r[9] or 0),
                "is_cancelled": bool(r[10]), "threads": thr,
                "address": str(r[12] or ""), "kind": _query_kind(q)
            })
            s_mem += mem; s_rows += rr; s_bytes += by; s_thr += thr
        today = 0
        try:
            tr = _qsafe(cl, "SELECT count() FROM system.query_log "
                            "WHERE type='QueryFinish' AND event_date = today()")
            if tr: today = int(tr[0][0])
        except Exception:
            pass
        cl.close()
        return jsonify({"processes": procs, "summary": {
            "active": len(procs), "memory": s_mem, "rows_read": s_rows,
            "data_read": s_bytes, "threads": s_thr, "today": today
        }})
    except Exception as e:
        return jsonify({"error": str(e), "processes": [], "summary": {}})


@app.route("/api/queries/running/charts", methods=["POST"])
def queries_running_charts():
    """Range-based aggregations from system.query_log for the three chart cards:
    queries started over time, average query memory over time, and queries by
    user. Range is whitelisted to a fixed set of second-spans and the bucket
    size is derived from it, so only validated integers reach the SQL."""
    d = request.json or {}
    RANGES = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}
    sec = RANGES.get(str(d.get("range", "24h")), 86400)
    iv = max(60, int(sec / 80))
    try:
        cl = _get_client(d)
        cluster_name = ""
        try:
            cn = _qsafe(cl, "SELECT cluster FROM system.clusters WHERE cluster NOT IN "
                            "('test_shard_localhost','test_cluster_one_shard_three_replicas_localhost',"
                            "'test_cluster_two_shards_localhost','test_cluster_two_shards_internal_replication',"
                            "'test_unavailable_shard') GROUP BY cluster ORDER BY count() DESC LIMIT 1")
            if cn: cluster_name = str(cn[0][0])
        except Exception:
            pass
        src = (f"clusterAllReplicas('{cluster_name}', system.query_log)"
               if cluster_name else "system.query_log")
        where = f"event_time >= now() - INTERVAL {sec} SECOND"
        over = _qsafe(cl, f"SELECT toStartOfInterval(event_time, INTERVAL {iv} SECOND) t, "
                          f"count() v FROM {src} WHERE type='QueryStart' AND {where} "
                          f"GROUP BY t ORDER BY t")
        mem = _qsafe(cl, f"SELECT toStartOfInterval(event_time, INTERVAL {iv} SECOND) t, "
                         f"round(avg(memory_usage)) v FROM {src} WHERE type='QueryFinish' AND {where} "
                         f"GROUP BY t ORDER BY t")
        usr = _qsafe(cl, f"SELECT user, count() c FROM {src} WHERE type='QueryFinish' AND {where} "
                         f"GROUP BY user ORDER BY c DESC LIMIT 8")
        total = 0
        try:
            tr = _qsafe(cl, f"SELECT count() FROM {src} WHERE type='QueryStart' AND {where}")
            if tr: total = int(tr[0][0])
        except Exception:
            pass
        cl.close()
        return jsonify({
            "over_time": [{"t": str(r[0]), "v": int(r[1])} for r in over],
            "memory": [{"t": str(r[0]), "v": int(r[1] or 0)} for r in mem],
            "by_user": [{"user": r[0], "count": int(r[1])} for r in usr],
            "total": total, "cluster": cluster_name
        })
    except Exception as e:
        return jsonify({"error": str(e), "over_time": [], "memory": [], "by_user": []})


def _ql_cluster(cl):
    """Detect a real cluster name so query_log scans hit every node via
    clusterAllReplicas (server-side, no extra client connections)."""
    try:
        cn = _qsafe(cl, "SELECT cluster FROM system.clusters WHERE cluster NOT IN "
                        "('test_shard_localhost','test_cluster_one_shard_three_replicas_localhost',"
                        "'test_cluster_two_shards_localhost','test_cluster_two_shards_internal_replication',"
                        "'test_unavailable_shard') GROUP BY cluster ORDER BY count() DESC LIMIT 1")
        if cn:
            return str(cn[0][0])
    except Exception:
        pass
    return ""


_QL_RANGES = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}


@app.route("/api/queries/failed", methods=["POST"])
def queries_failed():
    """Failed queries from system.query_log (exception rows) over a range, plus
    chart aggregations: failures over time, by user, by exception code, and a
    summary. Range is whitelisted; only validated integers reach the SQL."""
    d = request.json or {}
    sec = _QL_RANGES.get(str(d.get("range", "24h")), 86400)
    iv = max(60, int(sec / 80)); limit = min(500, max(1, int(d.get("limit", 100))))
    try:
        cl = _get_client(d); cluster = _ql_cluster(cl)
        src = f"clusterAllReplicas('{cluster}', system.query_log)" if cluster else "system.query_log"
        cond = "type IN ('ExceptionWhileProcessing','ExceptionBeforeStart')"
        where = f"{cond} AND event_time >= now() - INTERVAL {sec} SECOND"
        rows = _qsafe(cl, f"""
            SELECT query_id, event_time, user, query_duration_ms, read_rows,
                   read_bytes, memory_usage, exception_code, exception, query
            FROM {src} WHERE {where} ORDER BY event_time DESC LIMIT {limit}""")
        over = _qsafe(cl, f"SELECT toStartOfInterval(event_time, INTERVAL {iv} SECOND) t, count() v "
                          f"FROM {src} WHERE {where} GROUP BY t ORDER BY t")
        usr = _qsafe(cl, f"SELECT user, count() c FROM {src} WHERE {where} GROUP BY user ORDER BY c DESC LIMIT 8")
        exc = _qsafe(cl, f"SELECT toString(exception_code) k, count() c FROM {src} WHERE {where} "
                         f"GROUP BY exception_code ORDER BY c DESC LIMIT 8")
        total = 0; today = 0
        try:
            tr = _qsafe(cl, f"SELECT count(), uniqExact(user) FROM {src} WHERE {where}")
            if tr: total = int(tr[0][0])
        except Exception:
            pass
        try:
            td = _qsafe(cl, f"SELECT count() FROM {src} WHERE {cond} AND event_date = today()")
            if td: today = int(td[0][0])
        except Exception:
            pass
        cl.close()
        return jsonify({
            "queries": [{
                "query_id": r[0], "event_time": str(r[1]), "user": r[2],
                "duration_ms": int(r[3] or 0), "read_rows": int(r[4] or 0),
                "read_bytes": int(r[5] or 0), "memory_usage": int(r[6] or 0),
                "exception_code": int(r[7] or 0), "exception": str(r[8] or "")[:600],
                "query": str(r[9] or "")[:600]
            } for r in rows],
            "over_time": [{"t": str(r[0]), "v": int(r[1])} for r in over],
            "by_user": [{"user": r[0], "count": int(r[1])} for r in usr],
            "by_exception": [{"code": r[0], "count": int(r[1])} for r in exc],
            "summary": {"total": total, "today": today,
                        "users": len(usr), "top_exception": (exc[0][0] if exc else "—")}
        })
    except Exception as e:
        return jsonify({"error": str(e), "queries": [], "over_time": [],
                        "by_user": [], "by_exception": [], "summary": {}})


@app.route("/api/queries/expensive", methods=["POST"])
def queries_expensive():
    """Most expensive completed queries from system.query_log over a range,
    ranked by a whitelisted metric, plus record-breaker stats and per-user
    totals. sort_by maps to a fixed column set, so no user text reaches SQL."""
    d = request.json or {}
    sec = _QL_RANGES.get(str(d.get("range", "24h")), 86400)
    SORTS = {"memory": "memory_usage", "duration": "query_duration_ms",
             "rows": "read_rows", "bytes": "read_bytes", "cpu": "__CPU__"}
    sort = SORTS.get(str(d.get("sort_by", "memory")), "memory_usage")
    iv = max(60, int(sec / 80)); limit = min(200, max(1, int(d.get("limit", 100))))
    try:
        cl = _get_client(d); cluster = _ql_cluster(cl)
        src = f"clusterAllReplicas('{cluster}', system.query_log)" if cluster else "system.query_log"
        # CPU cost comes from the ProfileEvents map (OSCPUVirtualTimeMicroseconds =
        # user+system CPU across all threads). Column-probed like _sys_select so
        # ancient servers without the map degrade to 0 instead of failing.
        try:
            _pec = _qsafe(cl, "SELECT count() FROM system.columns WHERE database='system' AND table='query_log' AND name='ProfileEvents'")
            _has_pe = bool(_pec) and int(_pec[0][0] or 0) > 0
        except Exception:
            _has_pe = False
        _cpu_expr = "ProfileEvents['OSCPUVirtualTimeMicroseconds']" if _has_pe else "0"
        if sort == "__CPU__":
            sort = _cpu_expr if _has_pe else "memory_usage"
        where = (f"type='QueryFinish' AND event_time >= now() - INTERVAL {sec} SECOND "
                 f"AND query NOT LIKE '%system.%'")
        rows = _qsafe(cl, f"""
            SELECT query_id, event_time, user, query_duration_ms, read_rows,
                   read_bytes, memory_usage, result_rows, query, {_cpu_expr} AS cpu_us
            FROM {src} WHERE {where} ORDER BY {sort} DESC LIMIT {limit}""")
        rb = _qsafe(cl, f"SELECT max(read_bytes), max(query_duration_ms), max(memory_usage), "
                        f"count(), sum(read_bytes), sum(read_rows) FROM {src} WHERE {where}")
        usr = _qsafe(cl, f"SELECT user, sum({sort}) c FROM {src} WHERE {where} "
                         f"GROUP BY user ORDER BY c DESC LIMIT 8")
        over = _qsafe(cl, f"SELECT toStartOfInterval(event_time, INTERVAL {iv} SECOND) t, "
                          f"round(avg({sort})) v FROM {src} WHERE {where} GROUP BY t ORDER BY t")
        b = rb[0] if rb else [0, 0, 0, 0, 0, 0]
        cl.close()
        return jsonify({
            "queries": [{
                "query_id": r[0], "event_time": str(r[1]), "user": r[2],
                "duration_ms": int(r[3] or 0), "read_rows": int(r[4] or 0),
                "read_bytes": int(r[5] or 0), "memory_usage": int(r[6] or 0),
                "result_rows": int(r[7] or 0), "query": str(r[8] or "")[:600],
                "cpu_us": int(r[9] or 0)
            } for r in rows],
            "by_user": [{"user": r[0], "value": int(r[1] or 0)} for r in usr],
            "over_time": [{"t": str(r[0]), "v": int(r[1] or 0)} for r in over],
            "record": {"largest_scan": int(b[0] or 0), "longest_ms": int(b[1] or 0),
                       "peak_memory": int(b[2] or 0), "total": int(b[3] or 0),
                       "total_scanned": int(b[4] or 0), "total_rows": int(b[5] or 0)},
            "sort_by": str(d.get("sort_by", "memory"))
        })
    except Exception as e:
        return jsonify({"error": str(e), "queries": [], "by_user": [],
                        "over_time": [], "record": {}})


@app.route("/api/queries/badges", methods=["POST"])
def queries_badges():
    """Tiny payload for the sidebar live badges: how many queries are running
    right now and how many failed in the last hour. Polled every ~30s by the
    client, so it must stay cheap: two count() reads, no row payloads.
    Auth: covered by the "/api/queries" RBAC prefix (all viewer roles)."""
    d = request.json or {}
    try:
        cl = _get_client(d)
        run = _qsafe(cl, "SELECT count() FROM system.processes "
                         "WHERE query NOT ILIKE '%system.processes%' "
                         "AND query NOT ILIKE '%system.query_log%'")
        cluster = _ql_cluster(cl)
        src = f"clusterAllReplicas('{cluster}', system.query_log)" if cluster else "system.query_log"
        failed = _qsafe(cl, f"SELECT count() FROM {src} "
                            f"WHERE type IN ('ExceptionWhileProcessing','ExceptionBeforeStart') "
                            f"AND event_time >= now() - INTERVAL 3600 SECOND")
        cl.close()
        return jsonify({"running": int(run[0][0]) if run else 0,
                        "failed_1h": int(failed[0][0]) if failed else 0})
    except Exception as e:
        # Badges are decoration; the client hides them on error.
        return jsonify({"running": None, "failed_1h": None, "error": str(e)})


@app.route("/api/query/analyze", methods=["POST"])
def query_analyze():
    """Deep-dive analysis of a single query, looked up by query_id.
    Pulls one row from system.query_log (clusterAllReplicas if a
    cluster is detectable, else local) plus a thread breakdown from
    system.query_thread_log. The frontend turns this into a tabbed
    modal — Overview, SQL, Profile Events, Settings, Threads, Tables.
    """
    d = request.json or {}
    qid = _safe_qid((d.get("query_id") or "").strip(), allow_empty=True)
    if not qid:
        return jsonify({"error": "query_id required"}), 400
    # Lookback bound is OPTIONAL. With a UUID query_id filter we're
    # already selecting at most a few rows out of the whole log, so
    # an all-time scan is cheap. Caller can still pass hours_back to
    # bound it manually on very large logs.
    hours_back = int(d.get("hours_back") or 0)
    time_filter = f" AND event_time >= now() - INTERVAL {hours_back} HOUR" if hours_back > 0 else ""
    try:
        cl = _get_client(d)

        # Cluster auto-detection mirrors what /api/querylog/slow does.
        cluster_name = ""
        try:
            cn_rows = _qsafe(cl, "SELECT cluster FROM system.clusters WHERE cluster NOT IN ('test_shard_localhost','test_cluster_one_shard_three_replicas_localhost','test_cluster_two_shards_localhost','test_cluster_two_shards_internal_replication','test_unavailable_shard') GROUP BY cluster ORDER BY count() DESC LIMIT 1")
            if cn_rows:
                cluster_name = str(cn_rows[0][0])
        except Exception:
            pass

        source = f"clusterAllReplicas('{cluster_name}', system.query_log)" if cluster_name else "system.query_log"
        thread_source = f"clusterAllReplicas('{cluster_name}', system.query_thread_log)" if cluster_name else "system.query_thread_log"
        host_col = "hostName() as _host," if cluster_name else "'' as _host,"

        # ClickHouse versions vary in which optional columns
        # system.query_log carries. Probe once and substitute typed
        # defaults for the ones that aren't present, so the SELECT
        # never fails on UNKNOWN_IDENTIFIER and the downstream code
        # can read fixed indices.
        opt_present = set()
        try:
            opt_rows = cl.query(
                "SELECT name FROM system.columns "
                " WHERE database='system' AND table='query_log' "
                "   AND name IN ('peak_memory_usage','used_storages','used_aggregate_functions','used_functions','used_dictionaries')"
            ).result_rows
            opt_present = {r[0] for r in (opt_rows or [])}
        except Exception:
            pass
        peak_mem_col = "peak_memory_usage" if "peak_memory_usage" in opt_present else "memory_usage as peak_memory_usage"
        used_funcs_col = "used_functions" if "used_functions" in opt_present else "[] as used_functions"
        used_aggs_col = "used_aggregate_functions" if "used_aggregate_functions" in opt_present else "[] as used_aggregate_functions"
        used_dicts_col = "used_dictionaries" if "used_dictionaries" in opt_present else "[] as used_dictionaries"
        used_storages_col = "used_storages" if "used_storages" in opt_present else "[] as used_storages"

        # query_log lookup. A single query_id can have multiple rows
        # (QueryStart + QueryFinish, or QueryStart + Exception). We
        # prefer terminal events (Finish / Exception) — they carry the
        # resource counters. If only QueryStart exists, we still
        # return it so the user sees that the query is mid-flight.
        # parameterised values defend against injection on the qid.
        rows = cl.query(
            f"""
            SELECT {host_col}
                   type, event_time, query_start_time, query_duration_ms,
                   query_kind, user, client_hostname, client_name,
                   query, normalizeQuery(query) as normalized_query,
                   read_rows, read_bytes, written_rows, written_bytes,
                   result_rows, result_bytes,
                   memory_usage, {peak_mem_col},
                   current_database, databases, tables, columns,
                   exception_code, exception, stack_trace,
                   is_initial_query, initial_query_id,
                   length(thread_ids) as thread_count,
                   ProfileEvents, Settings,
                   {used_funcs_col}, {used_aggs_col}, {used_dicts_col},
                   {used_storages_col}
              FROM {source}
             WHERE query_id = %(qid)s{time_filter}
             ORDER BY (type = 'QueryFinish' OR type LIKE 'Exception%%') DESC, event_time DESC
             LIMIT 1
            """,
            parameters={"qid": qid}
        ).result_rows

        if not rows:
            cl.close()
            return jsonify({"error": "query_id not found in system.query_log",
                            "query_id": qid,
                            "hint": "The row may have been purged by query_log_retention, or the query never reached the log (e.g. cancelled before logging).",
                            "found": False}), 404

        r = rows[0]
        # Column index aligns with the SELECT order above.
        host = r[0]
        # type, event_time, query_start_time, duration
        d_type, d_evt, d_qst, d_dur = r[1], r[2], r[3], r[4]
        d_kind, d_user, d_chost, d_cname = r[5], r[6], r[7], r[8]
        d_query, d_normalized = r[9], r[10]
        d_read_rows, d_read_bytes = r[11], r[12]
        d_written_rows, d_written_bytes = r[13], r[14]
        d_result_rows, d_result_bytes = r[15], r[16]
        d_mem, d_peak_mem = r[17], r[18]
        d_curdb, d_databases, d_tables, d_columns = r[19], r[20], r[21], r[22]
        d_exc_code, d_exc, d_stack = r[23], r[24], r[25]
        d_is_initial, d_initial_qid = r[26], r[27]
        d_thread_count = r[28]
        # ProfileEvents and Settings come back as dict-like Map types
        # from clickhouse-connect. Convert defensively.
        d_profile = dict(r[29] or {})
        d_settings = dict(r[30] or {})
        d_used_funcs = list(r[31] or [])
        d_used_aggs = list(r[32] or [])
        d_used_dicts = list(r[33] or [])
        d_used_storages = list(r[34] or [])

        # Thread breakdown — aggregated to keep the response light.
        # Each (thread_id) row gives the longest-running snapshot for
        # that thread; we surface the top 20 by duration.
        threads = []
        try:
            t_rows = cl.query(
                f"""
                SELECT thread_id, thread_name,
                       max(query_duration_ms) as dur_ms,
                       max(memory_usage) as mem_max,
                       max(peak_memory_usage) as mem_peak
                  FROM {thread_source}
                 WHERE query_id = %(qid)s{time_filter}
                 GROUP BY thread_id, thread_name
                 ORDER BY dur_ms DESC
                 LIMIT 20
                """,
                parameters={"qid": qid}
            ).result_rows
            for tr in (t_rows or []):
                threads.append({
                    "thread_id": int(tr[0] or 0),
                    "thread_name": str(tr[1] or ""),
                    "duration_ms": int(tr[2] or 0),
                    "memory_usage": int(tr[3] or 0),
                    "peak_memory_usage": int(tr[4] or 0),
                })
        except Exception:
            # query_thread_log can be disabled on some clusters.
            pass

        cl.close()

        # Sort profile events / settings by name for the table view.
        # The frontend lets the user re-sort by value if they want.
        profile_list = sorted(
            [{"name": k, "value": int(v) if isinstance(v, (int, float)) else str(v)}
             for k, v in d_profile.items()],
            key=lambda x: x["name"]
        )
        settings_list = sorted(
            [{"name": k, "value": str(v)} for k, v in d_settings.items()],
            key=lambda x: x["name"]
        )

        audit("Analyze Query", panel="qanalyzer",
              detail=f"query_id={qid} duration_ms={d_dur} user={d_user} type={d_type}")

        return jsonify({
            "found": True,
            "query_id": qid,
            "host": host,
            "overview": {
                "type": d_type,
                "event_time": str(d_evt) if d_evt else "",
                "query_start_time": str(d_qst) if d_qst else "",
                "query_duration_ms": int(d_dur or 0),
                "query_kind": d_kind or "",
                "user": d_user or "",
                "client_hostname": d_chost or "",
                "client_name": d_cname or "",
                "current_database": d_curdb or "",
                "databases": list(d_databases or []),
                "tables": list(d_tables or []),
                "columns": list(d_columns or []),
                "read_rows": int(d_read_rows or 0),
                "read_bytes": int(d_read_bytes or 0),
                "written_rows": int(d_written_rows or 0),
                "written_bytes": int(d_written_bytes or 0),
                "result_rows": int(d_result_rows or 0),
                "result_bytes": int(d_result_bytes or 0),
                "memory_usage": int(d_mem or 0),
                "peak_memory_usage": int(d_peak_mem or 0),
                "thread_count": int(d_thread_count or 0),
                "is_initial_query": bool(d_is_initial),
                "initial_query_id": d_initial_qid or "",
                "exception_code": int(d_exc_code or 0),
                "exception": d_exc or "",
                "stack_trace": d_stack or "",
            },
            "sql": d_query or "",
            "normalized_sql": d_normalized or "",
            "profile_events": profile_list,
            "settings": settings_list,
            "threads": threads,
            "used": {
                "functions": d_used_funcs,
                "aggregate_functions": d_used_aggs,
                "dictionaries": d_used_dicts,
                "storages": d_used_storages,
            },
        })
    except Exception as e:
        return jsonify({"error": str(e), "found": False}), 500


@app.route("/api/query/analyze/history", methods=["POST"])
def query_analyze_history():
    """Historical comparison for a single query: looks up its
    normalized_query_hash from system.query_log, then aggregates the
    last 30 days of runs for that same normalized shape. Used by the
    Query Analyzer panel to render 'this query was 3x slower than
    median' style insights.
    """
    d = request.json or {}
    qid = _safe_qid((d.get("query_id") or "").strip(), allow_empty=True)
    if not qid:
        return jsonify({"error": "query_id required"}), 400
    try:
        cl = _get_client(d)
        # Cluster auto-detection mirrors the main analyze endpoint.
        cluster_name = ""
        try:
            cn_rows = _qsafe(cl, "SELECT cluster FROM system.clusters WHERE cluster NOT IN ('test_shard_localhost','test_cluster_one_shard_three_replicas_localhost','test_cluster_two_shards_localhost','test_cluster_two_shards_internal_replication','test_unavailable_shard') GROUP BY cluster ORDER BY count() DESC LIMIT 1")
            if cn_rows:
                cluster_name = str(cn_rows[0][0])
        except Exception:
            pass
        source = f"clusterAllReplicas('{cluster_name}', system.query_log)" if cluster_name else "system.query_log"

        # First: get this query's normalized_query_hash.
        hash_rows = cl.query(
            f"SELECT normalized_query_hash, query_duration_ms FROM {source} "
            f" WHERE query_id = %(qid)s LIMIT 1",
            parameters={"qid": qid}
        ).result_rows
        if not hash_rows:
            cl.close()
            return jsonify({"error": "query_id not found",
                            "found": False}), 404
        nq_hash = hash_rows[0][0]
        this_dur = int(hash_rows[0][1] or 0)

        # Aggregate stats over the last 30 days.
        stats_rows = cl.query(
            f"""
            SELECT count(),
                   quantile(0.5)(query_duration_ms) as p50,
                   quantile(0.95)(query_duration_ms) as p95,
                   quantile(0.99)(query_duration_ms) as p99,
                   avg(query_duration_ms) as avg_ms,
                   min(query_duration_ms) as min_ms,
                   max(query_duration_ms) as max_ms,
                   sum(read_bytes) as total_bytes,
                   uniqExact(user) as unique_users
              FROM {source}
             WHERE normalized_query_hash = %(h)s
               AND type = 'QueryFinish'
               AND event_time >= now() - INTERVAL 30 DAY
            """,
            parameters={"h": nq_hash}
        ).result_rows

        # Recent runs for the sparkline.
        recent_rows = cl.query(
            f"""
            SELECT event_time, query_duration_ms, query_id, user
              FROM {source}
             WHERE normalized_query_hash = %(h)s
               AND type = 'QueryFinish'
               AND event_time >= now() - INTERVAL 30 DAY
             ORDER BY event_time DESC
             LIMIT 100
            """,
            parameters={"h": nq_hash}
        ).result_rows

        cl.close()

        if not stats_rows or int(stats_rows[0][0] or 0) == 0:
            return jsonify({"found": False, "count": 0,
                            "normalized_query_hash": str(nq_hash)})

        s = stats_rows[0]
        # Sort recent ASC for chart-friendly ordering.
        recent_sorted = sorted(recent_rows or [], key=lambda r: r[0])
        return jsonify({
            "found": True,
            "normalized_query_hash": str(nq_hash),
            "count": int(s[0]),
            "p50": int(s[1] or 0),
            "p95": int(s[2] or 0),
            "p99": int(s[3] or 0),
            "avg": int(s[4] or 0),
            "min": int(s[5] or 0),
            "max": int(s[6] or 0),
            "total_bytes": int(s[7] or 0),
            "unique_users": int(s[8] or 0),
            "this_duration_ms": this_dur,
            "recent": [
                {"event_time": str(r[0]), "duration_ms": int(r[1] or 0),
                 "query_id": r[2], "user": r[3]}
                for r in recent_sorted
            ]
        })
    except Exception as e:
        return jsonify({"error": str(e), "found": False}), 500


# ── dictionaries ──────────────────────────────────────────────────────────────
@app.route("/api/dictionaries/list", methods=["POST"])
def dictionaries_list():
    d = request.json or {}
    try:
        cl = _get_client(d)
        rows = _qsafe(cl, """
            SELECT database, name, status, origin, type,
                   key, attribute.names, attribute.types,
                   bytes_allocated, element_count, load_factor,
                   source, lifetime_min, lifetime_max,
                   loading_start_time, last_successful_update_time,
                   loading_duration, last_exception
            FROM system.dictionaries ORDER BY database, name
        """)
        cl.close()
        return jsonify({"dictionaries": [{
            "database": r[0], "name": r[1], "status": r[2],
            "origin": r[3], "type": r[4], "key": str(r[5]),
            "attribute_names": list(r[6]) if r[6] else [],
            "attribute_types": list(r[7]) if r[7] else [],
            "bytes": int(r[8]), "elements": int(r[9]),
            "load_factor": round(float(r[10]), 3),
            "source": str(r[11])[:100],
            "lifetime_min": int(r[12]), "lifetime_max": int(r[13]),
            "loading_start": str(r[14]), "last_update": str(r[15]),
            "loading_duration": round(float(r[16]), 3),
            "last_exception": str(r[17]) if r[17] else ""
        } for r in rows]})
    except Exception as e: return jsonify({"error": str(e), "dictionaries": []})

@app.route("/api/dictionaries/reload", methods=["POST"])
def dictionaries_reload():
    d = request.json or {}
    name = d.get("name", "")
    if not name: return jsonify({"error": "name required"}), 400
    try:
        cl = _get_client(d)
        cl.command(f"SYSTEM RELOAD DICTIONARY `{name}`")
        cl.close()
        logger.info(f"RELOAD DICTIONARY {name}")
        return jsonify({"ok": True})
    except Exception as e: return jsonify({"error": str(e)})


@app.route("/api/dictionaries/reload-all", methods=["POST"])
def dictionaries_reload_all():
    """SYSTEM RELOAD DICTIONARIES — refresh every dictionary on the
    cluster at once."""
    d = request.json or {}
    try:
        cl = _get_client(d)
        cl.command("SYSTEM RELOAD DICTIONARIES")
        cl.close()
        logger.info("RELOAD DICTIONARIES (all)")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500

# ── disk & storage policy ─────────────────────────────────────────────────────
@app.route("/api/storage/info", methods=["POST"])
def storage_info():
    d = request.json or {}
    try:
        cl = _get_client(d)
        disks = _qsafe(cl, """
            SELECT name, path, type, free_space, total_space, keep_free_space,
                   unreserved_space
            FROM system.disks ORDER BY name
        """)
        policies = _qsafe(cl, """
            SELECT policy_name, volume_name, disks, volume_priority,
                   max_data_part_size, move_factor
            FROM system.storage_policies ORDER BY policy_name, volume_priority
        """)
        # Tables per policy
        tbl_policies = _qsafe(cl, """
            SELECT storage_policy, count() as cnt, sum(bytes_on_disk) as bytes
            FROM system.tables
            WHERE storage_policy != ''
              AND database NOT IN ('system','information_schema','INFORMATION_SCHEMA')
            GROUP BY storage_policy ORDER BY bytes DESC
        """)
        cl.close()
        return jsonify({
            "disks": [{
                "name": r[0], "path": r[1], "type": r[2],
                "free": int(r[3]), "total": int(r[4]),
                "keep_free": int(r[5]), "unreserved": int(r[6]),
                "used": int(r[4]) - int(r[3]),
                "used_pct": round((int(r[4])-int(r[3]))/max(int(r[4]),1)*100, 1)
            } for r in disks],
            "policies": [{
                "policy": r[0], "volume": r[1],
                "disks": list(r[2]) if r[2] else [],
                "priority": int(r[3]),
                "max_part_size": int(r[4]), "move_factor": float(r[5])
            } for r in policies],
            "table_policies": [{"policy": r[0], "tables": int(r[1]), "bytes": int(r[2])} for r in tbl_policies]
        })
    except Exception as e: return jsonify({"error": str(e)})

# ── access control audit ──────────────────────────────────────────────────────
@app.route("/api/security/user-activity", methods=["POST"])
def security_user_activity():
    """Unified activity timeline for one console user: merges
    audit_events (every audited action) with query_history (the SQL
    they actually ran) into one chronologically-ordered list. Answers
    'what did this user do?' on a single screen for compliance and
    incident review.
    """
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    target_user_id = d.get("user_id")
    target_username = (d.get("username") or "").strip()
    hours = int(d.get("hours") or 168)  # default 7 days
    limit = min(int(d.get("limit") or 500), 2000)
    # Resolve a username to its user_id if that's what was supplied.
    if not target_user_id and target_username:
        try:
            row = db_global().execute(
                "SELECT id FROM users WHERE lower(username) = lower(?) LIMIT 1",
                (target_username,)
            ).fetchone()
            if row:
                target_user_id = row["id"]
            else:
                return jsonify({"error": f"no console user named '{target_username}'",
                                "events": []}), 404
        except Exception as e:
            return jsonify({"error": str(e), "events": []}), 500
    if not target_user_id:
        return jsonify({"error": "user_id or username required"}), 400
    # Time window: either an absolute from/to range, or the relative
    # 'last N hours' fallback.
    from_ts = (d.get("from_ts") or "").strip()
    to_ts = (d.get("to_ts") or "").strip()
    use_range = bool(from_ts and to_ts)
    if use_range:
        time_clause = "AND ts >= ?::timestamptz AND ts <= ?::timestamptz"
        time_params = [from_ts, to_ts]
    else:
        time_clause = "AND ts >= now() - (? || ' hours')::interval"
        time_params = [str(hours)]
    try:
        gdb = db_global()
        # Audit events for this user.
        audit_rows = gdb.execute(
            "SELECT to_char(ts,'YYYY-MM-DD HH24:MI:SS') AS ts, action, panel, detail, "
            "       conn_host, conn_port, conn_user, ip, result "
            "  FROM audit_events "
            " WHERE user_id = ? " + time_clause +
            " ORDER BY ts DESC LIMIT ?",
            tuple([target_user_id] + time_params + [limit])
        ).fetchall()
        # Query history for this user.
        qh_rows = gdb.execute(
            "SELECT to_char(ts,'YYYY-MM-DD HH24:MI:SS') AS ts, sql, duration_ms, "
            "       rows_returned, error, conn_host, conn_port, conn_user, job_id "
            "  FROM query_history "
            " WHERE user_id = ? " + time_clause +
            " ORDER BY ts DESC LIMIT ?",
            tuple([target_user_id] + time_params + [limit])
        ).fetchall()
        # Merge into one list, tagged by kind.
        events = []
        for r in audit_rows:
            events.append({
                "kind": "audit", "ts": r["ts"], "action": r["action"],
                "panel": r["panel"] or "", "detail": r["detail"] or "",
                "conn": (r["conn_host"] or "") + (":" + r["conn_port"] if r["conn_port"] else ""),
                "conn_user": r["conn_user"] or "", "ip": r["ip"] or "",
                "result": r["result"] or "ok"
            })
        for r in qh_rows:
            events.append({
                "kind": "query", "ts": r["ts"],
                "sql": r["sql"] or "", "duration_ms": r["duration_ms"],
                "rows_returned": r["rows_returned"], "error": r["error"] or "",
                "conn": (r["conn_host"] or "") + (":" + r["conn_port"] if r["conn_port"] else ""),
                "conn_user": r["conn_user"] or "", "job_id": r["job_id"] or ""
            })
        # Sort combined by timestamp desc (string sort works for this format).
        events.sort(key=lambda e: e["ts"], reverse=True)
        events = events[:limit]
        window_desc = (from_ts + ".." + to_ts) if use_range else ("last " + str(hours) + "h")
        audit("View User Activity", panel="useractivity",
              detail=f"target_user_id={target_user_id} window={window_desc} events={len(events)}")
        return jsonify({"events": events, "audit_count": len(audit_rows),
                        "query_count": len(qh_rows), "hours": hours})
    except Exception as e:
        return jsonify({"error": str(e), "events": []}), 500


@app.route("/api/security/grants", methods=["POST"])
def security_grants():
    """Effective ClickHouse permissions: who can access what. Pulls
    from system.grants (direct grants), system.role_grants (roles
    assigned to users/roles), and system.users. Lets a reviewer
    answer 'what can this user reach?' for a security audit.
    """
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    try:
        cl = _get_client(d)
        # Users list with their default roles / auth type.
        users = []
        try:
            urows = _qsafe(cl, """
                SELECT name,
                       default_roles_all,
                       default_roles_list,
                       arrayStringConcat(default_roles_list, ', ') AS roles_str
                  FROM system.users
                 ORDER BY name
            """)
            for r in (urows or []):
                users.append({"name": r[0], "all_roles": bool(r[1]),
                              "roles": r[3] or ""})
        except Exception:
            pass
        # Direct grants — per grantee (user or role).
        grants = []
        try:
            grows = _qsafe(cl, """
                SELECT user_name, role_name, access_type,
                       database, table, column, is_partial_revoke, grant_option
                  FROM system.grants
                 ORDER BY coalesce(user_name, role_name), database, table
            """)
            for r in (grows or []):
                grants.append({
                    "grantee": r[0] or r[1] or "",
                    "grantee_type": "user" if r[0] else "role",
                    "access_type": r[2] or "",
                    "database": r[3] if r[3] is not None else "*",
                    "table": r[4] if r[4] is not None else "*",
                    "column": r[5] if r[5] is not None else "",
                    "is_revoke": bool(r[6]),
                    "grant_option": bool(r[7])
                })
        except Exception:
            pass
        # Role assignments.
        role_grants = []
        try:
            rrows = _qsafe(cl, """
                SELECT user_name, role_name, granted_role_name, granted_role_is_default
                  FROM system.role_grants
                 ORDER BY coalesce(user_name, role_name)
            """)
            for r in (rrows or []):
                role_grants.append({
                    "grantee": r[0] or r[1] or "",
                    "grantee_type": "user" if r[0] else "role",
                    "granted_role": r[2] or "",
                    "is_default": bool(r[3])
                })
        except Exception:
            pass
        audit("View Grants", panel="grants",
              detail=f"users={len(users)} grants={len(grants)} role_grants={len(role_grants)}")
        return jsonify({"users": users, "grants": grants, "role_grants": role_grants})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/storage/disk-usage", methods=["POST"])
def storage_disk_usage():
    """Per-database and per-table disk usage from system.parts, for
    the treemap. Returns compressed + uncompressed bytes and row
    counts, aggregated to active parts only.
    """
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    try:
        cl = _get_client(d)
        rows = _qsafe(cl, """
            SELECT database, table,
                   sum(bytes_on_disk) AS disk_bytes,
                   sum(data_compressed_bytes) AS comp_bytes,
                   sum(data_uncompressed_bytes) AS uncomp_bytes,
                   sum(rows) AS row_count,
                   count() AS part_count
              FROM system.parts
             WHERE active
             GROUP BY database, table
             ORDER BY disk_bytes DESC
             LIMIT 500
        """)
        tables = []
        db_totals = {}
        for r in (rows or []):
            db, tbl = r[0], r[1]
            disk = int(r[2] or 0)
            tables.append({
                "database": db, "table": tbl,
                "disk_bytes": disk,
                "comp_bytes": int(r[3] or 0),
                "uncomp_bytes": int(r[4] or 0),
                "rows": int(r[5] or 0),
                "parts": int(r[6] or 0),
                "compression_ratio": (float(r[4]) / float(r[3])) if (r[3] and float(r[3]) > 0) else 0.0
            })
            db_totals[db] = db_totals.get(db, 0) + disk
        databases = sorted(
            [{"database": k, "disk_bytes": v} for k, v in db_totals.items()],
            key=lambda x: x["disk_bytes"], reverse=True
        )
        audit("View Disk Usage", panel="diskusage",
              detail=f"tables={len(tables)} databases={len(databases)}")
        return jsonify({"tables": tables, "databases": databases,
                        "total_bytes": sum(db_totals.values())})
    except Exception as e:
        return jsonify({"error": str(e), "tables": []}), 500


@app.route("/api/query/annotations/list", methods=["POST"])
def query_annotations_list():
    """List every annotation attached to a given query_id, oldest
    first (thread-like). Notes are collaborative — every signed-in
    user can read them, since the same person investigating an
    incident is the audience for prior notes."""
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    qid = _safe_qid((d.get("query_id") or "").strip(), allow_empty=True)
    if not qid:
        return jsonify({"error": "query_id required"}), 400
    try:
        rows = db_global().execute(
            "SELECT id, query_id, user_id, username, note, "
            "       to_char(ts,'YYYY-MM-DD HH24:MI:SS') AS ts "
            "  FROM query_annotations "
            " WHERE query_id = ? "
            " ORDER BY ts ASC, id ASC",
            (qid,)
        ).fetchall()
        cur_uid = g.user["id"]
        cur_role = g.user.get("role") or ""
        out = [{
            "id": r["id"], "query_id": r["query_id"],
            "user_id": r["user_id"], "username": r["username"],
            "note": r["note"], "ts": r["ts"],
            # Only the author or an admin can delete a note. The flag
            # is computed server-side so the UI just renders or hides
            # the delete button without having to know the rule.
            "can_delete": (r["user_id"] == cur_uid or cur_role == "admin")
        } for r in rows]
        return jsonify({"annotations": out})
    except Exception as e:
        return jsonify({"error": str(e), "annotations": []}), 500


@app.route("/api/query/annotations/add", methods=["POST"])
def query_annotations_add():
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    qid = _safe_qid((d.get("query_id") or "").strip(), allow_empty=True)
    note = (d.get("note") or "").strip()
    if not qid:
        return jsonify({"error": "query_id required"}), 400
    if not note:
        return jsonify({"error": "note required"}), 400
    if len(note) > 2000:
        return jsonify({"error": "note too long (max 2000 chars)"}), 400
    try:
        uid = g.user["id"]
        uname = g.user.get("username") or ""
        row = db_global().execute(
            "INSERT INTO query_annotations(query_id, user_id, username, note) "
            "     VALUES (?, ?, ?, ?) "
            "  RETURNING id, to_char(ts,'YYYY-MM-DD HH24:MI:SS') AS ts",
            (qid, uid, uname, note)
        ).fetchone()
        audit("Add Annotation", panel="qanalyzer",
              detail=f"query_id={qid} note_id={row['id']} chars={len(note)}")
        return jsonify({"ok": True, "id": row["id"], "ts": row["ts"],
                        "username": uname, "user_id": uid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/query/annotations/delete", methods=["POST"])
def query_annotations_delete():
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    try:
        nid = int(d.get("id") or 0)
    except Exception:
        return jsonify({"error": "id required"}), 400
    if not nid:
        return jsonify({"error": "id required"}), 400
    try:
        gdb = db_global()
        row = gdb.execute(
            "SELECT id, query_id, user_id FROM query_annotations WHERE id = ?",
            (nid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        # Authorisation: author OR admin.
        if row["user_id"] != g.user["id"] and g.user.get("role") != "admin":
            return jsonify({"error": "forbidden"}), 403
        gdb.execute("DELETE FROM query_annotations WHERE id = ?", (nid,))
        audit("Delete Annotation", panel="qanalyzer",
              detail=f"query_id={row['query_id']} note_id={nid}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/health/dashboard", methods=["POST"])
def health_dashboard():
    """Single-call composite health snapshot for the Health Score
    Dashboard. Aggregates five signals — replication, mutations, disk
    usage, recent errors, and currently-running queries — into per-
    section scores (0-100) and a weighted overall score. Each section
    is queried defensively so one failure (e.g. cluster without
    replicated tables) does not poison the rest. Returns the same
    shape every time so the UI can render incrementally.
    """
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    try:
        cl = _get_client(d)
    except Exception as e:
        return jsonify({"error": "connect: " + str(e)[:200]}), 502

    sections = {}

    # ── Replication ──────────────────────────────────────────────
    try:
        rep = _qsafe(cl, """
            SELECT countIf(is_readonly OR is_session_expired) AS broken,
                   countIf(absolute_delay > 60) AS lagging,
                   countIf(active_replicas < total_replicas AND total_replicas > 0) AS missing,
                   max(absolute_delay) AS max_delay,
                   count() AS total
              FROM system.replicas
        """)
        if rep:
            r = rep[0]
            broken = int(r[0] or 0); lagging = int(r[1] or 0)
            missing = int(r[2] or 0); max_delay = int(r[3] or 0)
            total = int(r[4] or 0)
            score = max(0, 100 - 30*broken - 15*lagging - 10*missing)
            sections["replication"] = {
                "ok": True, "score": score, "total_tables": total,
                "broken": broken, "lagging": lagging,
                "missing_replicas": missing, "max_delay_sec": max_delay
            }
        else:
            sections["replication"] = {"ok": True, "score": 100,
                                       "total_tables": 0, "broken": 0,
                                       "lagging": 0, "missing_replicas": 0,
                                       "max_delay_sec": 0}
    except Exception as e:
        sections["replication"] = {"ok": False, "error": str(e)[:200], "score": None}

    # ── Mutations ────────────────────────────────────────────────
    try:
        mut = _qsafe(cl, """
            SELECT countIf(NOT is_done) AS running,
                   countIf(NOT is_done AND latest_fail_reason != '') AS failing,
                   count() AS total
              FROM system.mutations
             WHERE create_time > now() - INTERVAL 7 DAY
        """)
        if mut:
            r = mut[0]
            running = int(r[0] or 0); failing = int(r[1] or 0)
            total = int(r[2] or 0)
            score = max(0, 100 - 25*failing - 2*max(0, running-5))
            sections["mutations"] = {
                "ok": True, "score": score, "running": running,
                "failing": failing, "total_7d": total
            }
        else:
            sections["mutations"] = {"ok": True, "score": 100,
                                     "running": 0, "failing": 0, "total_7d": 0}
    except Exception as e:
        sections["mutations"] = {"ok": False, "error": str(e)[:200], "score": None}

    # ── Disk ─────────────────────────────────────────────────────
    try:
        disk = _qsafe(cl, """
            SELECT name, free_space, total_space,
                   (total_space - free_space) / nullIf(total_space, 0) * 100 AS used_pct
              FROM system.disks
             WHERE total_space > 0
             ORDER BY used_pct DESC
        """)
        disks = []
        max_used = 0.0
        for r in disk:
            used = float(r[3] or 0)
            disks.append({
                "name": str(r[0]),
                "free": int(r[1] or 0),
                "total": int(r[2] or 0),
                "used_pct": round(used, 1)
            })
            if used > max_used: max_used = used
        # 70% used → 100; 90% → 40; 100% → 10. Linear above 70%.
        score = max(0, min(100, int(100 - max(0, max_used - 70) * 3)))
        sections["disk"] = {
            "ok": True, "score": score, "disks": disks,
            "max_used_pct": round(max_used, 1)
        }
    except Exception as e:
        sections["disk"] = {"ok": False, "error": str(e)[:200], "score": None}

    # ── Errors (last 1h) ─────────────────────────────────────────
    try:
        err = _qsafe(cl, """
            SELECT count() AS distinct_codes, sum(value) AS total_count
              FROM system.errors
             WHERE last_error_time > now() - INTERVAL 1 HOUR
        """)
        if err:
            r = err[0]
            distinct = int(r[0] or 0); total = int(r[1] or 0)
            score = max(0, 100 - 10*distinct)
            sections["errors"] = {
                "ok": True, "score": score,
                "distinct_1h": distinct, "total_count_1h": total
            }
        else:
            sections["errors"] = {"ok": True, "score": 100,
                                  "distinct_1h": 0, "total_count_1h": 0}
    except Exception as e:
        sections["errors"] = {"ok": False, "error": str(e)[:200], "score": None}

    # ── Queries running ──────────────────────────────────────────
    try:
        q = _qsafe(cl, """
            SELECT count() AS running,
                   countIf(elapsed > 60) AS long_running,
                   max(elapsed) AS max_elapsed
              FROM system.processes
             WHERE query != ''
        """)
        if q:
            r = q[0]
            running = int(r[0] or 0); longrun = int(r[1] or 0)
            max_el = float(r[2] or 0)
            score = max(0, 100 - 10*longrun - max(0, running-50))
            sections["queries"] = {
                "ok": True, "score": score, "running": running,
                "long_running": longrun, "max_elapsed_sec": int(max_el)
            }
        else:
            sections["queries"] = {"ok": True, "score": 100,
                                   "running": 0, "long_running": 0, "max_elapsed_sec": 0}
    except Exception as e:
        sections["queries"] = {"ok": False, "error": str(e)[:200], "score": None}

    # ── Actionable detail panels under the score cards ──────────────────────
    # Each turns a summary score into "what exactly / where". All defensive:
    # a failing panel yields [] / {} and never breaks the dashboard. Whitespace
    # in SQL snippets is normalised in the frontend, not here, to avoid regex
    # escaping in these embedded queries.
    panels = {}
    # A. Top error codes in the last hour (the ERRORS card's detail).
    try:
        er = _qsafe(cl, """
            SELECT name, code, value, toString(last_error_time)
              FROM system.errors
             WHERE last_error_time > now() - INTERVAL 1 HOUR
             ORDER BY value DESC LIMIT 8
        """)
        panels["top_errors"] = [
            {"name": r[0], "code": int(r[1] or 0), "count": int(r[2] or 0), "last_time": r[3]}
            for r in (er or [])
        ]
    except Exception:
        panels["top_errors"] = []
    # B. Currently running queries, longest first (the QUERIES card's detail).
    try:
        pq = _qsafe(cl, """
            SELECT query_id, user, round(elapsed, 1), formatReadableSize(memory_usage),
                   substring(query, 1, 200)
              FROM system.processes
             WHERE query != ''
             ORDER BY elapsed DESC LIMIT 8
        """)
        panels["running_queries"] = [
            {"query_id": r[0], "user": r[1], "elapsed": float(r[2] or 0),
             "memory": r[3], "query": r[4]}
            for r in (pq or [])
        ]
    except Exception:
        panels["running_queries"] = []
    # C. Largest tables on disk (the DISK card's detail — where the space is).
    try:
        tt = _qsafe(cl, """
            SELECT database, table, sum(bytes_on_disk), sum(rows), count()
              FROM system.parts
             WHERE active
             GROUP BY database, table
             ORDER BY sum(bytes_on_disk) DESC LIMIT 8
        """)
        panels["top_tables"] = [
            {"database": r[0], "table": r[1], "bytes": int(r[2] or 0),
             "rows": int(r[3] or 0), "parts": int(r[4] or 0)}
            for r in (tt or [])
        ]
    except Exception:
        panels["top_tables"] = []
    # D. Server vitals strip (metrics + asynchronous_metrics).
    try:
        mrows = _qsafe(cl, """
            SELECT metric, value FROM system.metrics
             WHERE metric IN ('TCPConnection','HTTPConnection',
                              'BackgroundMergesAndMutationsPoolTask','PartsActive')
        """)
        mm = {r[0]: float(r[1] or 0) for r in (mrows or [])}
        arows = _qsafe(cl, """
            SELECT metric, value FROM system.asynchronous_metrics
             WHERE metric IN ('Uptime','MemoryResident','OSMemoryTotal')
        """)
        am = {r[0]: float(r[1] or 0) for r in (arows or [])}
        panels["vitals"] = {
            "uptime_sec": int(am.get("Uptime", 0)),
            "mem_used": int(am.get("MemoryResident", 0)),
            "mem_total": int(am.get("OSMemoryTotal", 0)),
            "tcp_conn": int(mm.get("TCPConnection", 0)),
            "http_conn": int(mm.get("HTTPConnection", 0)),
            "merges_running": int(mm.get("BackgroundMergesAndMutationsPoolTask", 0)),
            "parts_active": int(mm.get("PartsActive", 0)),
        }
    except Exception:
        panels["vitals"] = {}
    # E. Pending / failing mutations (conditional — usually empty).
    try:
        mu = _qsafe(cl, """
            SELECT database, table, mutation_id,
                   substring(latest_fail_reason, 1, 200), toString(create_time)
              FROM system.mutations
             WHERE NOT is_done
             ORDER BY create_time ASC LIMIT 8
        """)
        panels["mutations_pending"] = [
            {"database": r[0], "table": r[1], "mutation_id": r[2],
             "reason": r[3], "created": r[4]}
            for r in (mu or [])
        ]
    except Exception:
        panels["mutations_pending"] = []
    # F. Active merges in progress (conditional — parts pressure / write load).
    try:
        mg = _qsafe(cl, """
            SELECT database, table, round(progress, 2), round(elapsed, 1),
                   formatReadableSize(memory_usage)
              FROM system.merges
             ORDER BY elapsed DESC LIMIT 8
        """)
        panels["active_merges"] = [
            {"database": r[0], "table": r[1], "progress": float(r[2] or 0),
             "elapsed": float(r[3] or 0), "memory": r[4]}
            for r in (mg or [])
        ]
    except Exception:
        panels["active_merges"] = []

    try: cl.close()
    except Exception: pass

    # Composite — weighted average of available scores. Weights chosen
    # so replication + disk + errors dominate; mutations + queries are
    # transient and noisier.
    weights = {"replication": 0.30, "disk": 0.25, "errors": 0.20,
               "mutations": 0.15, "queries": 0.10}
    total_w = 0.0; weighted = 0.0
    for k, w in weights.items():
        s = sections.get(k, {})
        if s.get("score") is not None:
            weighted += s["score"] * w
            total_w += w
    overall = int(round(weighted / total_w)) if total_w > 0 else 0
    band = "healthy" if overall >= 90 else "degraded" if overall >= 70 else "critical"

    # Append a history row — keeps a per-fetch time series the UI can
    # render as a sparkline. Failures here don't block the response.
    try:
        cluster_label = (d.get("host") or "") + ":" + str(d.get("port") or "")
        if not cluster_label.strip(":"):
            cluster_label = ""
        def _s(name):
            v = sections.get(name, {}).get("score")
            return v if v is not None else None
        db_global().execute(
            "INSERT INTO health_score_history "
            "(overall, band, replication, mutations, disk, errors, queries, recorded_by_id, cluster_label) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (overall, band,
             _s("replication"), _s("mutations"), _s("disk"),
             _s("errors"), _s("queries"),
             g.user["id"], cluster_label)
        )
    except Exception:
        pass

    audit("View Health Dashboard", panel="healthdash",
          detail=f"score={overall} band={band}")

    return jsonify({
        "overall": overall, "band": band,
        "sections": sections,
        "panels": panels,
        "ts": datetime.now(timezone.utc).isoformat()
    })


@app.route("/api/health/history", methods=["POST"])
def health_history():
    """Score time series for the Health Dashboard sparkline. Returns
    bucketed (min/avg/max) points so the sparkline scales to any
    window without sending thousands of rows.
    """
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    hours = int(d.get("hours") or 168)
    if hours <= 0: hours = 168
    if hours > 24*90: hours = 24*90
    # 60 buckets is enough to draw a clean sparkline.
    bucket_count = 60
    bucket_secs = max(60, int(hours * 3600 / bucket_count))
    try:
        rows = db_global().execute(
            "SELECT floor(extract(epoch FROM ts) / ?) * ? AS bucket, "
            "       min(overall) AS lo, "
            "       round(avg(overall))::int AS av, "
            "       max(overall) AS hi, "
            "       count(*) AS n "
            "  FROM health_score_history "
            " WHERE ts > now() - (? || ' hours')::interval "
            " GROUP BY bucket "
            " ORDER BY bucket ASC",
            (bucket_secs, bucket_secs, str(hours))
        ).fetchall()
        points = []
        for r in rows:
            points.append({
                "ts":  int(r["bucket"]),
                "lo":  int(r["lo"]),
                "avg": int(r["av"]),
                "hi":  int(r["hi"]),
                "n":   int(r["n"]),
            })
        # Also return the latest row (full sub-scores) for the
        # status badge — saves a second call.
        last_row = db_global().execute(
            "SELECT ts, overall, band, replication, mutations, disk, errors, queries "
            "  FROM health_score_history "
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last = None
        if last_row:
            last = {
                "ts": last_row["ts"].isoformat() if last_row["ts"] else None,
                "overall": last_row["overall"], "band": last_row["band"],
                "replication": last_row["replication"], "mutations": last_row["mutations"],
                "disk": last_row["disk"], "errors": last_row["errors"],
                "queries": last_row["queries"]
            }
        return jsonify({"points": points, "hours": hours,
                        "bucket_secs": bucket_secs, "last": last})
    except Exception as e:
        return jsonify({"error": str(e), "points": []}), 500


@app.route("/api/cost/user-breakdown", methods=["POST"])
def cost_user_breakdown():
    """Aggregate cost per console user (or per initial_user) over a
    relative or absolute time window, from system.query_log. Completes
    the cost trio: estimator (pre-run) → analyzer (post-mortem) →
    breakdown (aggregate over time).
    """
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    hours = int(d.get("hours") or 168)
    from_ts = (d.get("from_ts") or "").strip()
    to_ts = (d.get("to_ts") or "").strip()
    group_by = d.get("group_by") or "user"
    if group_by not in ("user", "initial_user"):
        group_by = "user"
    # Optional user filter — comma or semicolon-separated usernames.
    # Empty / unset / 'all' means no filter (all users).
    import re as _re_uc
    raw_unames = (d.get("usernames") or "").strip()
    user_list = []
    if raw_unames and raw_unames.lower() != "all":
        for x in _re_uc.split(r"[,;]", raw_unames):
            x = x.strip()
            if x and len(x) < 200:
                user_list.append(x)
    use_range = bool(from_ts and to_ts)
    try:
        cl = _get_client(d)
        cluster_name = ""
        try:
            cn_rows = _qsafe(cl, "SELECT cluster FROM system.clusters WHERE cluster NOT IN ('test_shard_localhost','test_cluster_one_shard_three_replicas_localhost','test_cluster_two_shards_localhost','test_cluster_two_shards_internal_replication','test_unavailable_shard') GROUP BY cluster ORDER BY count() DESC LIMIT 1")
            if cn_rows:
                cluster_name = str(cn_rows[0][0])
        except Exception:
            pass
        source = f"clusterAllReplicas('{cluster_name}', system.query_log)" if cluster_name else "system.query_log"
        if use_range:
            time_clause = f"event_time >= toDateTime(%(from_ts)s) AND event_time <= toDateTime(%(to_ts)s)"
            params = {"from_ts": from_ts, "to_ts": to_ts}
        else:
            time_clause = f"event_time >= now() - INTERVAL {hours} HOUR"
            params = {}
        if user_list:
            escaped = [u.replace("'", "''") for u in user_list]
            user_clause = " AND " + group_by + " IN (" + ",".join("'" + u + "'" for u in escaped) + ")"
        else:
            user_clause = ""
        rows = cl.query(f"""
            SELECT {group_by} AS u,
                   count() AS query_count,
                   sum(read_bytes) AS total_bytes,
                   sum(read_rows) AS total_rows,
                   sum(query_duration_ms) AS total_duration_ms,
                   avg(query_duration_ms) AS avg_duration_ms,
                   max(memory_usage) AS peak_memory,
                   countIf(type = 'QueryFinish') AS ok_count,
                   countIf(type != 'QueryFinish') AS fail_count
              FROM {source}
             WHERE type IN ('QueryFinish','ExceptionWhileProcessing','ExceptionBeforeStart')
               AND {time_clause}
               AND {group_by} != ''
               {user_clause}
             GROUP BY {group_by}
             ORDER BY total_bytes DESC
             LIMIT 200
        """, parameters=params).result_rows

        users = []
        for r in rows:
            users.append({
                "user": r[0],
                "query_count": int(r[1] or 0),
                "total_bytes": int(r[2] or 0),
                "total_rows": int(r[3] or 0),
                "total_duration_ms": int(r[4] or 0),
                "avg_duration_ms": int(r[5] or 0),
                "peak_memory": int(r[6] or 0),
                "ok_count": int(r[7] or 0),
                "fail_count": int(r[8] or 0)
            })
        cl.close()
        window_desc = (from_ts + ".." + to_ts) if use_range else ("last " + str(hours) + "h")
        filter_desc = ("filter=" + ",".join(user_list)) if user_list else "filter=all"
        audit("View User Cost", panel="usercost",
              detail=f"window={window_desc} group_by={group_by} {filter_desc} users={len(users)}")
        return jsonify({"users": users, "group_by": group_by,
                        "total_bytes": sum(u["total_bytes"] for u in users),
                        "total_queries": sum(u["query_count"] for u in users)})
    except Exception as e:
        return jsonify({"error": str(e), "users": []}), 500


@app.route("/api/cost/node-user-activity", methods=["POST"])
def cost_node_user_activity():
    """Per-node x per-user activity for replicated/sharded clusters: which
    users generate the most work on which node, from clusterAllReplicas
    (query_log) grouped by hostName(). Standalone servers (no entry in
    system.clusters) get an empty row list so the UI hides the section.
    Same window/filter semantics as /api/cost/user-breakdown."""
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    hours = int(d.get("hours") or 168)
    from_ts = (d.get("from_ts") or "").strip()
    to_ts = (d.get("to_ts") or "").strip()
    group_by = d.get("group_by") or "user"
    if group_by not in ("user", "initial_user"):
        group_by = "user"
    import re as _re_na
    raw_unames = (d.get("usernames") or "").strip()
    user_list = []
    if raw_unames and raw_unames.lower() != "all":
        for x in _re_na.split(r"[,;]", raw_unames):
            x = x.strip()
            if x and len(x) < 200:
                user_list.append(x)
    use_range = bool(from_ts and to_ts)
    try:
        cl = _get_client(d)
        cluster_name = ""
        try:
            cn_rows = _qsafe(cl, "SELECT cluster FROM system.clusters WHERE cluster NOT IN ('test_shard_localhost','test_cluster_one_shard_three_replicas_localhost','test_cluster_two_shards_localhost','test_cluster_two_shards_internal_replication','test_unavailable_shard') GROUP BY cluster ORDER BY count() DESC LIMIT 1")
            if cn_rows:
                cluster_name = str(cn_rows[0][0])
        except Exception:
            pass
        if not cluster_name:
            cl.close()
            return jsonify({"cluster": "", "rows": []})
        if use_range:
            time_clause = "event_time >= toDateTime(%(from_ts)s) AND event_time <= toDateTime(%(to_ts)s)"
            params = {"from_ts": from_ts, "to_ts": to_ts}
        else:
            time_clause = f"event_time >= now() - INTERVAL {hours} HOUR"
            params = {}
        if user_list:
            escaped = [u.replace("'", "''") for u in user_list]
            user_clause = " AND " + group_by + " IN (" + ",".join("'" + u + "'" for u in escaped) + ")"
        else:
            user_clause = ""
        rows = cl.query(f"""
            SELECT hostName() AS node, {group_by} AS u,
                   count() AS queries,
                   sum(query_duration_ms) AS dur_ms,
                   sum(read_bytes) AS bytes
              FROM clusterAllReplicas('{cluster_name}', system.query_log)
             WHERE type IN ('QueryFinish','ExceptionWhileProcessing','ExceptionBeforeStart')
               AND {time_clause}{user_clause}
             GROUP BY node, u
             ORDER BY node, queries DESC
             LIMIT 500
        """, parameters=params).result_rows
        cl.close()
        out = [{"node": str(r[0]), "user": str(r[1]), "queries": int(r[2] or 0),
                "dur_ms": int(r[3] or 0), "bytes": int(r[4] or 0)} for r in rows]
        audit("User Cost Node Activity", panel="usercost",
              detail=f"cluster={cluster_name} rows={len(out)} window={'range' if use_range else str(hours)+'h'}")
        return jsonify({"cluster": cluster_name, "rows": out})
    except Exception as e:
        return jsonify({"error": str(e), "rows": []})

@app.route("/api/cost/user-trend", methods=["POST"])
def cost_user_trend():
    """Daily time series of cost metrics — scanned bytes, total
    duration, query count — by user (or initial_user) over a chosen
    window. Drives the Cost Trend chart on the User Cost panel."""
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    days = int(d.get("days") or 7)
    if days < 1: days = 1
    if days > 90: days = 90
    group_by = d.get("group_by") or "user"
    if group_by not in ("user", "initial_user"):
        group_by = "user"
    import re as _re_ut
    raw = (d.get("usernames") or "").strip()
    user_list = []
    if raw and raw.lower() != "all":
        for x in _re_ut.split(r"[,;]", raw):
            x = x.strip()
            if x and len(x) < 200:
                user_list.append(x)
    try:
        cl = _get_client(d)
        cluster_name = ""
        try:
            cn = _qsafe(cl, "SELECT cluster FROM system.clusters WHERE cluster NOT IN ('test_shard_localhost','test_cluster_one_shard_three_replicas_localhost','test_cluster_two_shards_localhost','test_cluster_two_shards_internal_replication','test_unavailable_shard') GROUP BY cluster ORDER BY count() DESC LIMIT 1")
            if cn: cluster_name = str(cn[0][0])
        except Exception:
            pass
        source = f"clusterAllReplicas('{cluster_name}', system.query_log)" if cluster_name else "system.query_log"
        if user_list:
            escaped = [u.replace("'", "''") for u in user_list]
            user_clause = " AND " + group_by + " IN (" + ",".join("'" + u + "'" for u in escaped) + ")"
        else:
            user_clause = ""
        rows = cl.query(f"""
            SELECT toDate(event_time) AS d,
                   {group_by} AS u,
                   sum(read_bytes) AS bytes,
                   sum(query_duration_ms) AS duration_ms,
                   count() AS queries
              FROM {source}
             WHERE type IN ('QueryFinish','ExceptionWhileProcessing','ExceptionBeforeStart')
               AND event_time >= today() - {days}
               AND {group_by} != ''
               {user_clause}
             GROUP BY d, {group_by}
             ORDER BY d ASC, bytes DESC
        """).result_rows
        # Pivot into one series per user. Cap user count to keep the
        # client payload sane.
        by_user = {}
        all_dates = set()
        for r in rows:
            d_str = str(r[0])
            uname = str(r[1])
            all_dates.add(d_str)
            if uname not in by_user:
                by_user[uname] = {}
            by_user[uname][d_str] = {
                "bytes": int(r[2] or 0),
                "duration_ms": int(r[3] or 0),
                "queries": int(r[4] or 0),
            }
        dates = sorted(all_dates)
        # Order users by total scanned bytes (descending) and keep top 10
        # for the chart; the rest roll up into 'others' so the chart
        # stays readable.
        user_totals = []
        for u, by_date in by_user.items():
            tot = sum(v["bytes"] for v in by_date.values())
            user_totals.append((u, tot))
        user_totals.sort(key=lambda x: -x[1])
        TOP = 10
        keep = [u for u, _ in user_totals[:TOP]]
        rolled = user_totals[TOP:]
        series = []
        for u in keep:
            row = {"user": u, "points": []}
            for d_str in dates:
                v = by_user[u].get(d_str, {"bytes": 0, "duration_ms": 0, "queries": 0})
                row["points"].append({"date": d_str, **v})
            series.append(row)
        if rolled:
            others = {"user": "(others ×" + str(len(rolled)) + ")", "points": []}
            for d_str in dates:
                tot_bytes = sum(by_user[u].get(d_str, {}).get("bytes", 0) for u, _ in rolled)
                tot_dur   = sum(by_user[u].get(d_str, {}).get("duration_ms", 0) for u, _ in rolled)
                tot_q     = sum(by_user[u].get(d_str, {}).get("queries", 0) for u, _ in rolled)
                others["points"].append({"date": d_str, "bytes": tot_bytes,
                                         "duration_ms": tot_dur, "queries": tot_q})
            series.append(others)
        cl.close()
        audit("View User Cost Trend", panel="usercost",
              detail=f"days={days} group_by={group_by} filter={raw or 'all'} users={len(series)}")
        return jsonify({"series": series, "dates": dates, "days": days})
    except Exception as e:
        return jsonify({"error": str(e), "series": [], "dates": []}), 500


@app.route("/api/security/schema-drift", methods=["POST"])
def schema_drift():
    """DDL change feed from system.query_log: who altered what when,
    over a chosen window. Used by the Schema Drift Tracker — auditors
    care about every CREATE/ALTER/DROP/RENAME/TRUNCATE that touched
    the schema. We don't trust query_kind alone (it varies by CH
    version) so we union it with a query-prefix regex."""
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    hours = int(d.get("hours") or 168)
    if hours <= 0: hours = 168
    if hours > 24*90: hours = 24*90
    try:
        cl = _get_client(d)
        cluster_name = ""
        try:
            cn = _qsafe(cl, "SELECT cluster FROM system.clusters WHERE cluster NOT IN ('test_shard_localhost','test_cluster_one_shard_three_replicas_localhost','test_cluster_two_shards_localhost','test_cluster_two_shards_internal_replication','test_unavailable_shard') GROUP BY cluster ORDER BY count() DESC LIMIT 1")
            if cn: cluster_name = str(cn[0][0])
        except Exception: pass
        source = f"clusterAllReplicas('{cluster_name}', system.query_log)" if cluster_name else "system.query_log"
        rows = cl.query(f"""
            SELECT event_time, user, query_id, query_kind,
                   substring(query, 1, 1000) AS query_snippet,
                   databases, tables, exception_code, exception
              FROM {source}
             WHERE event_time >= now() - INTERVAL {hours} HOUR
               AND type IN ('QueryFinish','ExceptionWhileProcessing')
               AND (
                 query_kind IN ('Create','Alter','Drop','Rename','Truncate',
                                'CreateDatabase','DropDatabase','CreateView',
                                'CreateMaterializedView','CreateTable',
                                'DropTable','AlterTable','AlterDatabase',
                                'RenameTable','TruncateTable','AlterUser',
                                'CreateUser','DropUser','GrantQuery','RevokeQuery')
                 OR match(lower(substring(query,1,200)),
                          '^(create|alter|drop|rename|truncate|grant|revoke)\\\\s')
               )
             ORDER BY event_time DESC
             LIMIT 1000
        """).result_rows
        changes = []
        summary = {"create":0, "alter":0, "drop":0, "rename":0, "truncate":0, "grant_revoke":0, "other":0}
        for r in rows:
            kind_raw = str(r[3] or "")
            snippet = str(r[4] or "")
            head = snippet.strip().split(None, 1)[0].lower() if snippet.strip() else ""
            category = "other"
            if "create" in kind_raw.lower() or head == "create":   category = "create"
            elif "alter" in kind_raw.lower() or head == "alter":    category = "alter"
            elif "drop" in kind_raw.lower() or head == "drop":      category = "drop"
            elif "rename" in kind_raw.lower() or head == "rename":  category = "rename"
            elif "truncate" in kind_raw.lower() or head == "truncate": category = "truncate"
            elif head in ("grant", "revoke") or "grant" in kind_raw.lower() or "revoke" in kind_raw.lower():
                category = "grant_revoke"
            summary[category] = summary.get(category, 0) + 1
            tables = list(r[6]) if r[6] else []
            dbs = list(r[5]) if r[5] else []
            changes.append({
                "event_time": str(r[0]),
                "user": str(r[1] or ""),
                "query_id": str(r[2] or ""),
                "query_kind": kind_raw,
                "category": category,
                "query": snippet,
                "databases": dbs,
                "tables": tables,
                "exception_code": int(r[7] or 0),
                "exception": str(r[8] or ""),
                "failed": bool(r[7])
            })
        cl.close()
        audit("View Schema Drift", panel="schemadrift",
              detail=f"hours={hours} changes={len(changes)}")
        return jsonify({"changes": changes, "summary": summary, "hours": hours})
    except Exception as e:
        return jsonify({"error": str(e), "changes": [], "summary": {}}), 500


@app.route("/api/storage/table-activity", methods=["POST"])
def table_activity():
    """Per-table read/write activity from system.query_log, used by
    the Table Activity Heatmap. Aggregates SELECT (reads) and INSERT
    (writes) against each (db, table) plus the last-access time, so
    hot tables surface at the top and cold tables become archive
    candidates. System tables are excluded."""
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    d = request.json or {}
    hours = int(d.get("hours") or 168)
    if hours <= 0: hours = 168
    if hours > 24*90: hours = 24*90
    try:
        cl = _get_client(d)
        cluster_name = ""
        try:
            cn = _qsafe(cl, "SELECT cluster FROM system.clusters WHERE cluster NOT IN ('test_shard_localhost','test_cluster_one_shard_three_replicas_localhost','test_cluster_two_shards_localhost','test_cluster_two_shards_internal_replication','test_unavailable_shard') GROUP BY cluster ORDER BY count() DESC LIMIT 1")
            if cn: cluster_name = str(cn[0][0])
        except Exception: pass
        source = f"clusterAllReplicas('{cluster_name}', system.query_log)" if cluster_name else "system.query_log"
        rows = cl.query(f"""
            WITH arrayJoin(tables) AS qualified
            SELECT splitByChar('.', qualified)[1] AS db,
                   splitByChar('.', qualified)[2] AS tbl,
                   countIf(query_kind = 'Select') AS reads,
                   countIf(query_kind = 'Insert') AS writes,
                   count() AS total,
                   sum(read_bytes) AS bytes_read,
                   max(event_time) AS last_access,
                   uniqExact(user) AS distinct_users
              FROM {source}
             WHERE event_time >= now() - INTERVAL {hours} HOUR
               AND type = 'QueryFinish'
               AND notEmpty(tables)
               AND query_kind IN ('Select','Insert')
             GROUP BY db, tbl
            HAVING db NOT IN ('system','INFORMATION_SCHEMA','information_schema')
               AND tbl != ''
             ORDER BY total DESC
             LIMIT 500
        """).result_rows
        tables = []
        max_total = 0
        for r in rows:
            total = int(r[4] or 0)
            if total > max_total: max_total = total
            tables.append({
                "db": str(r[0]),
                "table": str(r[1]),
                "reads": int(r[2] or 0),
                "writes": int(r[3] or 0),
                "total": total,
                "bytes_read": int(r[5] or 0),
                "last_access": str(r[6] or ""),
                "distinct_users": int(r[7] or 0)
            })
        cl.close()
        audit("View Table Activity", panel="tableactivity",
              detail=f"hours={hours} tables={len(tables)}")
        return jsonify({"tables": tables, "hours": hours, "max_total": max_total})
    except Exception as e:
        return jsonify({"error": str(e), "tables": []}), 500


@app.route("/api/compliance/export-pack", methods=["POST"])
def compliance_export_pack():
    """One-click compliance evidence pack: a ZIP containing audit
    events, user roster, grant matrix, DDL change history, user
    activity summary, and a manifest mapping each file to the
    SOC 2 / ISO 27001 / GDPR control it satisfies. Admin-only —
    this output is sensitive (full audit trail + grants matrix)."""
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    if g.user.get("role") != "admin":
        return jsonify({"error": "forbidden — admin only"}), 403
    d = request.json or {}
    hours = int(d.get("hours") or 720)
    if hours <= 0: hours = 720
    if hours > 24*365: hours = 24*365

    # The pack supports two ways to express the window:
    #   (1) hours   — preset (24 / 168 / 720 / 2160 / 8760), the default
    #   (2) from/to — explicit ISO datetimes for an arbitrary range
    # When from/to are both present they win; otherwise hours is used to
    # derive a relative range ending at "now". Both paths converge on a
    # (from_ts, to_ts) tuple used everywhere downstream.
    from_raw = (d.get("from") or "").strip()
    to_raw   = (d.get("to")   or "").strip()
    to_ts   = datetime.now(timezone.utc)
    from_ts = None
    is_custom = False
    if from_raw and to_raw:
        try:
            # datetime.fromisoformat accepts '2026-05-31T12:00' and full
            # ISO with timezone. We normalise 'Z' to '+00:00' so both
            # work. Naive timestamps are treated as UTC.
            ft = datetime.fromisoformat(from_raw.replace('Z', '+00:00'))
            tt = datetime.fromisoformat(to_raw.replace('Z', '+00:00'))
            if ft.tzinfo is None: ft = ft.replace(tzinfo=timezone.utc)
            if tt.tzinfo is None: tt = tt.replace(tzinfo=timezone.utc)
            if tt <= ft:
                return jsonify({"error": "'to' must be after 'from'"}), 400
            if (tt - ft).days > 5 * 365:
                return jsonify({"error": "range too large (max 5 years)"}), 400
            from_ts = ft
            to_ts = tt
            is_custom = True
        except ValueError as e:
            return jsonify({"error": "invalid date range: " + str(e)}), 400
    if from_ts is None:
        from_ts = to_ts - timedelta(hours=hours)

    # Pre-format once for ClickHouse string-interpolated queries below.
    # Server-controlled datetimes, so injection is not a concern.
    ch_from = from_ts.strftime("%Y-%m-%d %H:%M:%S")
    ch_to   = to_ts.strftime("%Y-%m-%d %H:%M:%S")

    generated_at = datetime.now(timezone.utc).isoformat()
    buf = io.BytesIO()
    errors = []

    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        # ── audit_events.csv ─────────────────────────────────────
        try:
            rows = db_global().execute(
                "SELECT to_char(ts,'YYYY-MM-DD HH24:MI:SS') AS ts, "
                "       user_id, username, role, action, panel, detail, "
                "       conn_host, conn_port, conn_user, ip, result "
                "  FROM audit_events "
                " WHERE ts >= ? AND ts <= ? "
                " ORDER BY ts ASC",
                (from_ts, to_ts)
            ).fetchall()
            sio = io.StringIO()
            w = csv.writer(sio)
            w.writerow(["ts","user_id","username","role","action","panel","detail","conn_host","conn_port","conn_user","ip","result"])
            for r in rows:
                w.writerow([r["ts"], r["user_id"], r["username"], r["role"],
                            r["action"], r["panel"], r["detail"],
                            r["conn_host"], r["conn_port"], r["conn_user"],
                            r["ip"], r["result"]])
            zf.writestr("audit_events.csv", sio.getvalue())
            audit_rows = len(rows)
        except Exception as e:
            errors.append("audit_events: " + str(e)[:200])
            audit_rows = 0

        # ── users.csv ────────────────────────────────────────────
        try:
            rows = db_global().execute(
                "SELECT id, username, role, "
                "       to_char(created_at,'YYYY-MM-DD HH24:MI:SS') AS created_at, "
                "       to_char(last_login_at,'YYYY-MM-DD HH24:MI:SS') AS last_login_at "
                "  FROM users ORDER BY id"
            ).fetchall()
            sio = io.StringIO()
            w = csv.writer(sio)
            w.writerow(["id","username","role","created_at","last_login_at"])
            for r in rows:
                w.writerow([r["id"], r["username"], r["role"],
                            r["created_at"], r["last_login_at"]])
            zf.writestr("users.csv", sio.getvalue())
            user_rows = len(rows)
        except Exception as e:
            errors.append("users: " + str(e)[:200])
            user_rows = 0

        # ── user_activity_summary.csv ───────────────────────────
        try:
            rows = db_global().execute(
                "SELECT username, action, count(*) AS event_count, "
                "       to_char(min(ts),'YYYY-MM-DD HH24:MI:SS') AS first_seen, "
                "       to_char(max(ts),'YYYY-MM-DD HH24:MI:SS') AS last_seen "
                "  FROM audit_events "
                " WHERE ts >= ? AND ts <= ? "
                " GROUP BY username, action "
                " ORDER BY username, event_count DESC",
                (from_ts, to_ts)
            ).fetchall()
            sio = io.StringIO()
            w = csv.writer(sio)
            w.writerow(["username","action","event_count","first_seen","last_seen"])
            for r in rows:
                w.writerow([r["username"], r["action"], r["event_count"],
                            r["first_seen"], r["last_seen"]])
            zf.writestr("user_activity_summary.csv", sio.getvalue())
        except Exception as e:
            errors.append("user_activity_summary: " + str(e)[:200])

        # ── grants.csv + schema_drift_ddl.csv (need a CH connection) ─
        cl = None
        grant_rows = 0; ddl_rows = 0
        try:
            cl = _get_client(d)
            # grants.csv
            try:
                grants = _qsafe(cl, """
                    SELECT user_name, role_name, access_type, database, table, column,
                           is_partial_revoke, grant_option
                      FROM system.grants
                """)
                sio = io.StringIO()
                w = csv.writer(sio)
                w.writerow(["user_name","role_name","access_type","database","table","column","is_partial_revoke","grant_option"])
                for r in grants:
                    w.writerow([str(c) if c is not None else "" for c in r])
                zf.writestr("grants.csv", sio.getvalue())
                grant_rows = len(grants)
            except Exception as e:
                errors.append("grants: " + str(e)[:200])

            # schema_drift_ddl.csv
            try:
                ddl = cl.query(f"""
                    SELECT event_time, user, query_id, query_kind,
                           substring(query, 1, 1000) AS query,
                           arrayStringConcat(databases, ',') AS databases,
                           arrayStringConcat(tables, ',') AS tables,
                           exception_code
                      FROM system.query_log
                     WHERE event_time >= toDateTime('{ch_from}', 'UTC')
                       AND event_time <= toDateTime('{ch_to}', 'UTC')
                       AND type IN ('QueryFinish','ExceptionWhileProcessing')
                       AND (
                         query_kind IN ('Create','Alter','Drop','Rename','Truncate',
                                        'CreateDatabase','DropDatabase','CreateView',
                                        'CreateMaterializedView','CreateTable',
                                        'DropTable','AlterTable','AlterDatabase',
                                        'RenameTable','TruncateTable','GrantQuery',
                                        'RevokeQuery','CreateUser','DropUser','AlterUser')
                         OR match(lower(substring(query,1,200)),
                                  '^(create|alter|drop|rename|truncate|grant|revoke)\\\\s')
                       )
                     ORDER BY event_time ASC
                """).result_rows
                sio = io.StringIO()
                w = csv.writer(sio)
                w.writerow(["event_time","user","query_id","query_kind","query","databases","tables","exception_code"])
                for r in ddl:
                    w.writerow([str(r[0]), str(r[1] or ""), str(r[2] or ""),
                                str(r[3] or ""), str(r[4] or ""),
                                str(r[5] or ""), str(r[6] or ""),
                                int(r[7] or 0)])
                zf.writestr("schema_drift_ddl.csv", sio.getvalue())
                ddl_rows = len(ddl)
            except Exception as e:
                errors.append("schema_drift_ddl: " + str(e)[:200])
        except Exception as e:
            errors.append("clickhouse connection: " + str(e)[:200])
        finally:
            if cl:
                try: cl.close()
                except Exception: pass

        # ── manifest.json ────────────────────────────────────────
        if is_custom:
            window_label = f"from {ch_from} UTC to {ch_to} UTC"
        elif hours < 168:
            window_label = f"last {hours} hours"
        else:
            window_label = f"last {hours//24} days"
        manifest = {
            "generated_at_utc": generated_at,
            "generated_by": g.user.get("username", ""),
            "window_from_utc": from_ts.isoformat(),
            "window_to_utc":   to_ts.isoformat(),
            "window_hours":    hours if not is_custom else None,
            "window_label":    window_label,
            "window_is_custom": is_custom,
            "row_counts": {
                "audit_events": audit_rows,
                "users": user_rows,
                "grants": grant_rows,
                "schema_drift_ddl": ddl_rows,
            },
            "errors": errors,
            "files": [
                {"file": "audit_events.csv",
                 "what": "Every console action recorded in the window — sign-ins, panel views, exports, kills, mutations, RBAC changes.",
                 "controls": ["SOC 2 CC7.2 (system monitoring)",
                              "ISO 27001 A.12.4.1 (event logging)",
                              "GDPR Art. 30 (records of processing)"]},
                {"file": "users.csv",
                 "what": "Console user roster: id, username, role, created_at, last_login_at.",
                 "controls": ["SOC 2 CC6.1 (logical access)",
                              "ISO 27001 A.9.2.1 (user registration & deregistration)"]},
                {"file": "user_activity_summary.csv",
                 "what": "Per-user, per-action counts plus first/last seen timestamps in the window.",
                 "controls": ["SOC 2 CC7.2", "ISO 27001 A.12.4.3 (admin/operator logs)"]},
                {"file": "grants.csv",
                 "what": "Current GRANT matrix from system.grants — who can do what on which object.",
                 "controls": ["SOC 2 CC6.3 (access authorisation)",
                              "ISO 27001 A.9.2.3 (privileged access)",
                              "GDPR Art. 32 (security of processing)"]},
                {"file": "schema_drift_ddl.csv",
                 "what": "Every DDL/DCL statement run against the cluster in the window — CREATE/ALTER/DROP/RENAME/TRUNCATE plus GRANT/REVOKE.",
                 "controls": ["SOC 2 CC8.1 (change management)",
                              "ISO 27001 A.12.1.2 (change control)",
                              "GDPR Art. 30"]},
            ]
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        # ── README.md ────────────────────────────────────────────
        readme = (
            "# Compliance Evidence Pack\n\n"
            f"Generated: {generated_at}\n"
            f"By: {g.user.get('username','')}\n"
            f"Window: {window_label}\n"
            f"  from: {from_ts.isoformat()}\n"
            f"  to:   {to_ts.isoformat()}\n\n"
            "## Contents\n\n"
            "| File | What it covers | Primary controls |\n"
            "|---|---|---|\n"
            "| audit_events.csv | Full console action log | SOC 2 CC7.2 · ISO 27001 A.12.4.1 · GDPR Art. 30 |\n"
            "| users.csv | Console user roster | SOC 2 CC6.1 · ISO 27001 A.9.2.1 |\n"
            "| user_activity_summary.csv | Per-user action rollups | SOC 2 CC7.2 · ISO 27001 A.12.4.3 |\n"
            "| grants.csv | Current GRANT matrix | SOC 2 CC6.3 · ISO 27001 A.9.2.3 · GDPR Art. 32 |\n"
            "| schema_drift_ddl.csv | DDL/DCL change history | SOC 2 CC8.1 · ISO 27001 A.12.1.2 · GDPR Art. 30 |\n"
            "| manifest.json | Machine-readable index of this pack | — |\n\n"
            "## Notes\n\n"
            "- All timestamps are in UTC.\n"
            "- Audit data is append-only at the database level (cannot be silently mutated).\n"
            "- This pack is intended as evidence for compliance audits and should be handled accordingly.\n"
        )
        zf.writestr("README.md", readme)

    audit("Export Compliance Pack", panel="compliancepack",
          detail=f"window={window_label} size_bytes={buf.tell()} audit_rows={audit_rows} ddl_rows={ddl_rows} grant_rows={grant_rows}")

    buf.seek(0)
    if is_custom:
        fname = f"compliance-pack-from-{from_ts.strftime('%Y%m%d')}-to-{to_ts.strftime('%Y%m%d')}.zip"
    else:
        fname = f"compliance-pack-{generated_at[:10]}-{hours}h.zip"
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True, download_name=fname)


@app.route("/api/audit/verify", methods=["POST"])
def audit_verify():
    """Walk the audit hash chain and report integrity. Admin-only (enforced by
    the /api/audit RBAC prefix). Each row's entry_hash is recomputed from its
    stored fields + prev_hash and checked against both the stored hash and the
    link to the previous row, so any edit, deletion, or reorder is detected.
    Rows written before the chain existed have NULL hashes and are counted as
    'legacy (unhashed)' rather than treated as tampering."""
    if not getattr(g, "user", None):
        return jsonify({"error": "not signed in"}), 401
    rows = db_global().execute(
        "SELECT id, ts, user_id, username, role, action, panel, detail, "
        "       conn_host, conn_port, conn_user, ip, result, prev_hash, entry_hash "
        "  FROM audit_events ORDER BY id ASC"
    ).fetchall()
    prev = ""
    broken = []
    checked = 0
    legacy = 0
    first_hashed_id = None
    for r in rows:
        eh = r["entry_hash"]
        if not eh:
            legacy += 1
            prev = ""  # a pre-chain gap resets linkage for the rows that follow
            continue
        if first_hashed_id is None:
            first_hashed_id = r["id"]
        ts_iso = r["ts"].astimezone(timezone.utc).isoformat() if r["ts"] else ""
        fields = {
            "ts": ts_iso, "user_id": r["user_id"], "username": r["username"],
            "role": r["role"], "action": r["action"], "panel": r["panel"],
            "detail": r["detail"], "conn_host": r["conn_host"],
            "conn_port": r["conn_port"], "conn_user": r["conn_user"],
            "ip": r["ip"], "result": r["result"],
        }
        expect = _audit_entry_hash(r["prev_hash"] or "", fields)
        if expect != eh:
            broken.append({"id": r["id"], "reason": "hash_mismatch"})
        elif (r["prev_hash"] or "") != prev:
            broken.append({"id": r["id"], "reason": "broken_link"})
        prev = eh
        checked += 1
    return jsonify({
        "ok": len(broken) == 0,
        "total": len(rows),
        "checked": checked,
        "legacy_unhashed": legacy,
        "first_hashed_id": first_hashed_id,
        "broken": broken[:100],
    })


@app.route("/api/audit/summary", methods=["POST"])
def audit_summary():
    import datetime as _dt
    d = request.json or {}
    hours = int(d.get("hours", 24))
    user_filter = d.get("user_filter", "").strip()
    audit_log_path = d.get("audit_log_path", "/var/log/clickhouse-console-audit.log")
    save_log = d.get("save_log", False)
    try:
        cl = _get_client(d)
        user_where = f"AND user ILIKE '%{user_filter}%'" if user_filter else ""
        # Per-user summary
        user_rows = _qsafe(cl, f"""
            SELECT user,
                   count() as queries,
                   countIf(exception != '') as errors,
                   sum(query_duration_ms) as total_ms,
                   max(query_duration_ms) as max_ms,
                   sum(read_rows) as read_rows,
                   sum(read_bytes) as read_bytes,
                   sum(memory_usage) as memory,
                   formatReadableSize(sum(read_bytes)) as read_bytes_h,
                   formatReadableSize(sum(memory_usage)) as mem_h,
                   min(event_time) as first_seen,
                   max(event_time) as last_seen
            FROM system.query_log
            WHERE event_time >= now() - INTERVAL {hours} HOUR
              AND type = 'QueryFinish'
              {user_where}
            GROUP BY user ORDER BY queries DESC
        """)
        # Per-user recent queries (if user filter active)
        user_queries = []
        if user_filter:
            uq_rows = _qsafe(cl, f"""
                SELECT event_time, user, query_duration_ms,
                       read_rows, formatReadableSize(read_bytes) as rb_h,
                       formatReadableSize(memory_usage) as mem_h,
                       exception, query
                FROM system.query_log
                WHERE event_time >= now() - INTERVAL {hours} HOUR
                  AND type = 'QueryFinish'
                  AND user ILIKE '%{user_filter}%'
                ORDER BY event_time DESC LIMIT 50
            """)
            user_queries = [{
                "time": str(r[0]), "user": r[1], "duration_ms": int(r[2]),
                "read_rows": int(r[3]), "read_bytes_h": r[4], "mem_h": r[5],
                "exception": str(r[6]) if r[6] else "",
                "query": str(r[7])[:300]
            } for r in uq_rows]
        # Recent errors
        error_rows = _qsafe(cl, f"""
            SELECT event_time, user, exception, query
            FROM system.query_log
            WHERE event_time >= now() - INTERVAL {hours} HOUR
              AND exception != ''
              {user_where}
            ORDER BY event_time DESC LIMIT 30
        """)
        # Query type breakdown
        type_rows = _qsafe(cl, f"""
            SELECT user,
                   countIf(query ILIKE 'SELECT%') as selects,
                   countIf(query ILIKE 'INSERT%') as inserts,
                   countIf(query ILIKE 'ALTER%') as alters,
                   countIf(query ILIKE 'DROP%' OR query ILIKE 'TRUNCATE%') as drops,
                   countIf(query ILIKE 'CREATE%') as creates,
                   countIf(query ILIKE 'SYSTEM%') as system_cmds
            FROM system.query_log
            WHERE event_time >= now() - INTERVAL {hours} HOUR
              AND type='QueryFinish'
              {user_where}
            GROUP BY user ORDER BY selects DESC
        """)
        cl.close()
        users = [{"user": r[0], "queries": int(r[1]), "errors": int(r[2]),
            "total_ms": int(r[3]), "max_ms": int(r[4]), "read_rows": int(r[5]),
            "read_bytes": int(r[6]), "memory": int(r[7]),
            "read_bytes_h": r[8], "mem_h": r[9],
            "first_seen": str(r[10]), "last_seen": str(r[11])} for r in user_rows]
        errors = [{"time": str(r[0]), "user": r[1],
            "exception": str(r[2])[:200], "query": str(r[3])[:200]} for r in error_rows]
        types = [{"user": r[0], "selects": int(r[1]), "inserts": int(r[2]),
            "alters": int(r[3]), "drops": int(r[4]),
            "creates": int(r[5]), "system_cmds": int(r[6])} for r in type_rows]

        # Save audit log if requested
        log_size = None
        log_path_used = None
        if save_log:
            try:
                now_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                lines = [
                    f"=== AUDIT REPORT {now_str} | Last {hours}h" + (f" | User filter: {user_filter}" if user_filter else "") + " ===",
                    f"{'USER':<30} {'QUERIES':>8} {'ERRORS':>8} {'MAX_MS':>10} {'READ_BYTES':>14} {'LAST_SEEN':<20}",
                    "-" * 100
                ]
                for u in users:
                    lines.append(f"{u['user']:<30} {u['queries']:>8} {u['errors']:>8} {u['max_ms']:>10} {u['read_bytes_h']:>14} {u['last_seen'][:19]:<20}")
                if errors:
                    lines += ["", "--- RECENT ERRORS ---"]
                    for e in errors[:20]:
                        lines.append(f"{e['time'][:19]} | {e['user']:<20} | {e['exception'][:80]}")
                lines.append("")
                Path(audit_log_path).parent.mkdir(parents=True, exist_ok=True)
                with open(audit_log_path, "a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                log_size = Path(audit_log_path).stat().st_size
                log_path_used = audit_log_path
                logger.info(f"AUDIT LOG saved to {audit_log_path}")
            except Exception as le:
                logger.warning(f"Could not save audit log: {le}")

        # Always return current log file size if exists
        try:
            p = Path(audit_log_path)
            if p.exists():
                log_size = p.stat().st_size
                log_path_used = audit_log_path
        except: pass

        return jsonify({
            "users": users, "errors": errors, "types": types,
            "user_queries": user_queries,
            "hours": hours, "user_filter": user_filter,
            "log_size": log_size, "log_path": log_path_used
        })
    except Exception as e: return jsonify({"error": str(e)})


# ── table health score ────────────────────────────────────────────────────────
@app.route("/api/health/table", methods=["POST"])
def table_health():
    d = request.json or {}
    db = _safe_ident(d.get("database", ""), "database"); tbl = _safe_ident(d.get("table", ""), "table")
    if not db or not tbl: return jsonify({"error": "database and table required"}), 400
    try:
        cl = _get_client(d)
        checks = {}
        # Parts check
        parts_rows = _qsafe(cl, f"""
            SELECT count() as parts, sum(rows) as rows, sum(bytes_on_disk) as bytes,
                   sum(data_compressed_bytes) as compressed,
                   sum(data_uncompressed_bytes) as uncompressed,
                   max(bytes_on_disk) as max_part_bytes,
                   countIf(bytes_on_disk < 1048576) as tiny_parts
            FROM system.parts
            WHERE database='{db}' AND table='{tbl}' AND active
        """)
        if parts_rows:
            p = parts_rows[0]
            parts, rows, bytes_disk, compressed, uncompressed, max_part, tiny = (
                int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]), int(p[5]), int(p[6]))
            ratio = compressed / max(uncompressed, 1) * 100
            checks["parts"] = {"value": parts, "status": "ok" if parts < 300 else "warn" if parts < 1000 else "bad",
                "label": f"{parts} active parts", "detail": f"Tiny parts (<1MB): {tiny}"}
            checks["compression"] = {"value": round(ratio, 1), "status": "ok" if ratio < 50 else "warn" if ratio < 80 else "bad",
                "label": f"{round(ratio,1)}% compression ratio", "detail": f"{_to_json(compressed) if compressed else 0}B compressed"}
            checks["size"] = {"value": bytes_disk, "status": "ok",
                "label": f"{fmtB(bytes_disk) if bytes_disk else '0 B'} on disk", "detail": f"{rows} rows"}
        # Merges check
        merge_rows = _qsafe(cl, f"SELECT count() FROM system.merges WHERE database='{db}' AND table='{tbl}'")
        active_merges = int(merge_rows[0][0]) if merge_rows else 0
        checks["merges"] = {"value": active_merges, "status": "ok" if active_merges < 5 else "warn" if active_merges < 20 else "bad",
            "label": f"{active_merges} active merges", "detail": "Background merge operations"}
        # Replication check
        rep_rows = _qsafe(cl, f"""
            SELECT is_leader, is_readonly, absolute_delay, queue_size, active_replicas, total_replicas
            FROM system.replicas WHERE database='{db}' AND table='{tbl}'
        """)
        if rep_rows:
            r = rep_rows[0]
            delay, queue = int(r[2]), int(r[3])
            checks["replication"] = {"value": delay, "status": "ok" if delay < 10 else "warn" if delay < 60 else "bad",
                "label": f"Replication delay: {delay}s", "detail": f"Queue: {queue}, Active: {r[4]}/{r[5]}"}
        else:
            checks["replication"] = {"value": 0, "status": "ok", "label": "Not replicated", "detail": "MergeTree (no replication)"}
        # Mutations check
        mut_rows = _qsafe(cl, f"SELECT count() FROM system.mutations WHERE database='{db}' AND table='{tbl}' AND is_done=0")
        pending_muts = int(mut_rows[0][0]) if mut_rows else 0
        checks["mutations"] = {"value": pending_muts, "status": "ok" if pending_muts == 0 else "warn" if pending_muts < 3 else "bad",
            "label": f"{pending_muts} pending mutations", "detail": "ALTER UPDATE/DELETE operations"}
        # TTL check
        ttl_rows = _qsafe(cl, f"SELECT engine_full FROM system.tables WHERE database='{db}' AND name='{tbl}'")
        has_ttl = any("TTL" in str(r[0]) for r in ttl_rows)
        checks["ttl"] = {"value": 1 if has_ttl else 0, "status": "ok",
            "label": "TTL configured" if has_ttl else "No TTL", "detail": "Time-to-live expiration"}
        # Overall score
        status_scores = {"ok": 2, "warn": 1, "bad": 0}
        total = sum(status_scores[v["status"]] for v in checks.values())
        max_score = len(checks) * 2
        score = round(total / max_score * 100)
        overall = "ok" if score >= 75 else "warn" if score >= 50 else "bad"
        cl.close()
        return jsonify({"checks": checks, "score": score, "overall": overall,
                       "database": db, "table": tbl})
    except Exception as e: return jsonify({"error": str(e)})

def fmtB(b):
    b = int(b or 0)
    for u, t in [('TB',1e12),('GB',1e9),('MB',1e6),('KB',1e3)]:
        if b >= t: return f'{b/t:.2f} {u}'
    return f'{b} B'

# ── projection & index analyzer ───────────────────────────────────────────────
@app.route("/api/analyzer/explain", methods=["POST"])
def analyzer_explain():
    d = request.json or {}
    sql = (d.get("sql") or "").strip()
    if not sql: return jsonify({"error": "No SQL"}), 400
    try:
        cl = _get_client(d)
        plan = cl.raw_query(f"EXPLAIN PLAN indexes=1, projections=1 {sql}").decode("utf-8","replace").strip()
        pipeline = cl.raw_query(f"EXPLAIN PIPELINE {sql}").decode("utf-8","replace").strip()
        estimate = cl.raw_query(f"EXPLAIN ESTIMATE {sql}").decode("utf-8","replace").strip()
        cl.close()
        # Parse used indexes and projections from plan
        import re as _re
        indexes_used = _re.findall(r'(?:Index|Skipping|Projection).*', plan)
        return jsonify({
            "plan": plan, "pipeline": pipeline, "estimate": estimate,
            "indexes_used": indexes_used,
            "has_projection": "Projection" in plan,
            "has_index": "Index" in plan or "Skipping" in plan
        })
    except Exception as e: return jsonify({"error": str(e)})


# ── autocomplete ──────────────────────────────────────────────────────────────
@app.route("/api/query/search-tables", methods=["POST"])
def search_tables():
    d = request.json or {}
    q = (d.get("q") or "").strip()
    if not q or len(q) < 2:
        return jsonify({"results": []})
    try:
        cl = _get_client(d)
        rows = _qsafe(cl, f"""
            SELECT database, name, engine,
                   formatReadableSize(total_bytes) as size_str
            FROM system.tables
            WHERE database NOT IN ('system','information_schema','INFORMATION_SCHEMA')
              AND (name ILIKE '%{q}%' OR database ILIKE '%{q}%')
            ORDER BY database, name
            LIMIT 50
        """)
        cl.close()
        return jsonify({"results": [
            {"database": r[0], "table": r[1], "engine": r[2], "size": r[3]}
            for r in rows
        ]})
    except Exception as e:
        return jsonify({"error": str(e), "results": []})


@app.route("/api/autocomplete/tables", methods=["POST"])
def autocomplete_tables():
    d = request.json or {}
    db = _safe_ident(d.get("database", ""), "database")
    try:
        cl = _get_client(d)
        if db:
            rows = _qsafe(cl, f"SELECT name FROM system.tables WHERE database='{db}' ORDER BY name")
            tables = [r[0] for r in rows]
        else:
            rows = _qsafe(cl, """
                SELECT database, name FROM system.tables
                WHERE database NOT IN ('system','information_schema','INFORMATION_SCHEMA')
                ORDER BY database, name
            """)
            tables = [f"{r[0]}.{r[1]}" for r in rows]
        cl.close()
        return jsonify({"tables": tables})
    except Exception as e: return jsonify({"error": str(e), "tables": []})

@app.route("/api/autocomplete/columns", methods=["POST"])
def autocomplete_columns():
    d = request.json or {}
    db = _safe_ident(d.get("database", ""), "database"); tbl = _safe_ident(d.get("table", ""), "table")
    if not tbl: return jsonify({"columns": []})
    try:
        cl = _get_client(d)
        rows = _qsafe(cl, f"""
            SELECT name, type, comment
            FROM system.columns
            WHERE database='{db}' AND table='{tbl}'
            ORDER BY position
        """)
        cl.close()
        return jsonify({"columns": [{"name": r[0], "type": str(r[1]), "comment": r[2] or ""} for r in rows]})
    except Exception as e: return jsonify({"error": str(e), "columns": []})

# ── TTL manager ───────────────────────────────────────────────────────────────
@app.route("/api/ttl/list", methods=["POST"])
def ttl_list():
    d = request.json or {}
    try:
        cl = _get_client(d)
        rows = _qsafe(cl, """
            SELECT database, name, engine, engine_full,
                   total_rows, total_bytes,
                   metadata_modification_time
            FROM system.tables
            WHERE engine_full LIKE '%TTL%'
              AND database NOT IN ('system','information_schema','INFORMATION_SCHEMA')
            ORDER BY database, name
        """)
        result = []
        for r in rows:
            db, tbl, engine, engine_full = r[0], r[1], r[2], str(r[3])
            # Extract TTL expression from engine_full
            import re as _re
            ttl_matches = _re.findall(r'TTL\s+([^\n,]+(?:DELETE|TO DISK[^\n,]*|TO VOLUME[^\n,]*)?)', engine_full, _re.IGNORECASE)
            ttl_exprs = [m.strip() for m in ttl_matches]
            # Get row count estimate for TTL expiry
            expired_count = 0
            try:
                if ttl_exprs:
                    # Try to count expired rows (best effort)
                    first_ttl = ttl_exprs[0]
                    if 'DELETE' in first_ttl.upper() or 'TTL' in first_ttl.upper():
                        exp_rows = _qsafe(cl, f"""
                            SELECT count() FROM {db}.{tbl}
                            WHERE {first_ttl.replace('DELETE','').replace('TTL','').strip()} < now()
                            LIMIT 1
                        """)
                        expired_count = int(exp_rows[0][0]) if exp_rows else 0
            except: pass
            result.append({
                "database": db, "table": tbl, "engine": engine,
                "ttl_expressions": ttl_exprs,
                "total_rows": int(r[4]) if r[4] else 0,
                "total_bytes": int(r[5]) if r[5] else 0,
                "expired_estimate": expired_count,
                "last_modified": str(r[6])
            })
        cl.close()
        return jsonify({"tables": result})
    except Exception as e: return jsonify({"error": str(e), "tables": []})

@app.route("/api/ttl/force", methods=["POST"])
def ttl_force():
    d = request.json or {}
    db = _safe_ident(d.get("database",""),"database"); tbl = _safe_ident(d.get("table",""),"table")
    if not db or not tbl: return jsonify({"error": "database and table required"}), 400
    try:
        cl = _get_client(d)
        cl.command(f"OPTIMIZE TABLE {db}.{tbl} FINAL")
        cl.close()
        logger.info(f"TTL FORCE OPTIMIZE {db}.{tbl}")
        return jsonify({"ok": True})
    except Exception as e: return jsonify({"error": str(e)})

# ── replication queue ─────────────────────────────────────────────────────────
@app.route("/api/mv/list", methods=["POST"])
def mv_list():
    """Return every materialized view on the cluster, with the SELECT
    that produced it, its size, and last-modified time."""
    d = request.json or {}
    try:
        cl = _get_client(d)
        rows = _qsafe(cl, """
            SELECT database, name, engine, create_table_query,
                   total_rows, total_bytes, metadata_modification_time
              FROM system.tables
             WHERE engine LIKE '%MaterializedView%'
                OR engine = 'MaterializedView'
             ORDER BY database, name
        """)
        out = []
        for r in rows:
            out.append({
                "database": r[0], "name": r[1], "engine": r[2],
                "ddl": r[3] or "",
                "rows": int(r[4] or 0), "bytes": int(r[5] or 0),
                "modified": str(r[6]) if r[6] else "",
            })
        return jsonify({"mvs": out})
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/api/mv/drop", methods=["POST"])
def mv_drop():
    """DROP a materialized view. Audited. Body: {host, port, user,
    password, database, name, sync}. sync=true issues a SYNC DROP that
    waits for completion."""
    d = request.json or {}
    database = _safe_ident((d.get("database") or "").strip(), "database")
    name     = (d.get("name") or "").strip()
    sync     = bool(d.get("sync"))
    if not database or not name:
        return jsonify({"error": "database and name required"}), 400
    # Schema-name safety: ClickHouse identifier chars are letters, digits,
    # underscores; reject anything else outright. Keeps SQL injection out.
    import re as _re
    if not _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", database) or \
       not _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return jsonify({"error": "invalid identifier"}), 400
    try:
        cl = _get_client(d)
        sync_clause = " SYNC" if sync else ""
        cl.command(f"DROP TABLE IF EXISTS `{database}`.`{name}`{sync_clause}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/api/mv/refresh", methods=["POST"])
def mv_refresh():
    """REFRESH a refreshable materialized view (ClickHouse 24.x+). For
    classic MVs (which are continuous), this is not applicable — the
    UI hides the button for them. Body: {host, ..., database, name}."""
    d = request.json or {}
    database = _safe_ident((d.get("database") or "").strip(), "database")
    name     = (d.get("name") or "").strip()
    if not database or not name:
        return jsonify({"error": "database and name required"}), 400
    import re as _re
    if not _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", database) or \
       not _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return jsonify({"error": "invalid identifier"}), 400
    try:
        cl = _get_client(d)
        cl.command(f"SYSTEM REFRESH VIEW `{database}`.`{name}`")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/api/cluster/health", methods=["POST"])
def cluster_health():
    """One-shot snapshot of the customer ClickHouse cluster's overall
    operational health. Pulls four signals in a single round trip:

      • Replication state of every replicated table — delay, future
        parts, queue size, errors. Anything with absolute_delay > 60s
        or future_parts > 50 or is_readonly = 1 is flagged as
        'problematic' in the response.
      • Distributed queue backlog per (database, table). Long backlogs
        mean inserts are not flushing to remote shards.
      • ZooKeeper reachability — issues a SELECT against
        system.zookeeper and reports the round-trip latency.
      • Recent errors from system.errors over the last hour, grouped
        by error name so the operator sees patterns rather than
        a stream of individual events.

    Each section may fail independently (e.g. cluster has no
    ZooKeeper). A failure of one section does not poison the others —
    each carries its own ok / error fields.
    """
    d = request.json or {}
    out = {"replication": None, "distributed": None,
           "zookeeper": None,    "errors": None}

    try:
        cl = _get_client(d)
    except Exception as e:
        return jsonify({"error": "connect: " + str(e)[:200]}), 502

    # ─── Replication ────────────────────────────────────────────────
    try:
        rep_rows = _qsafe(cl, """
            SELECT database, table, is_leader, is_readonly, is_session_expired,
                   future_parts, parts_to_check, queue_size,
                   inserts_in_queue, merges_in_queue, log_max_index,
                   log_pointer, absolute_delay, total_replicas,
                   active_replicas
              FROM system.replicas
            ORDER BY absolute_delay DESC
        """)
        rep_list = []
        for r in rep_rows:
            rep_list.append({
                "database": r[0], "table": r[1],
                "is_leader": bool(r[2]), "is_readonly": bool(r[3]),
                "is_session_expired": bool(r[4]),
                "future_parts": int(r[5] or 0), "parts_to_check": int(r[6] or 0),
                "queue_size": int(r[7] or 0),
                "inserts_in_queue": int(r[8] or 0), "merges_in_queue": int(r[9] or 0),
                "log_max_index": int(r[10] or 0), "log_pointer": int(r[11] or 0),
                "absolute_delay": int(r[12] or 0),
                "total_replicas": int(r[13] or 0),
                "active_replicas": int(r[14] or 0),
            })
        problematic = [r for r in rep_list
                       if r["is_readonly"] or r["is_session_expired"]
                       or r["future_parts"] > 50 or r["absolute_delay"] > 60
                       or (r["total_replicas"] > 0 and r["active_replicas"] < r["total_replicas"])]
        out["replication"] = {
            "ok": True, "total": len(rep_list),
            "problematic_count": len(problematic),
            "problematic": problematic[:100],
            "max_delay": max((r["absolute_delay"] for r in rep_list), default=0),
        }
    except Exception as e:
        out["replication"] = {"ok": False, "error": str(e)[:200]}

    # ─── Distributed queue ─────────────────────────────────────────
    try:
        dist_rows = _qsafe(cl, """
            SELECT database, table, count() AS queue_size,
                   sum(data_compressed_bytes) AS bytes
              FROM system.distribution_queue
             GROUP BY database, table
             ORDER BY queue_size DESC
        """)
        out["distributed"] = {
            "ok": True,
            "tables": [{"database": r[0], "table": r[1],
                        "queue_size": int(r[2] or 0),
                        "bytes": int(r[3] or 0)} for r in dist_rows],
        }
    except Exception as e:
        out["distributed"] = {"ok": False, "error": str(e)[:200]}

    # ─── ZooKeeper ─────────────────────────────────────────────────
    # Do NOT issue SELECT FROM system.zookeeper unless we already know
    # the table exists — on standalone (non-replicated) clusters the
    # table is absent, and a blind query lands a UNKNOWN_TABLE entry in
    # system.errors on every refresh. The Cluster Health panel then
    # reads system.errors and reports the very errors it just caused.
    zk_t0 = time.time()
    try:
        chk = _qsafe(cl, "SELECT count() FROM system.tables WHERE database='system' AND name='zookeeper'")
        zk_table_exists = bool(chk) and len(chk[0]) > 0 and int(chk[0][0] or 0) > 0
    except Exception:
        zk_table_exists = False

    if not zk_table_exists:
        out["zookeeper"] = {
            "ok": True, "reachable": False, "configured": False,
            "round_trip_ms": round((time.time()-zk_t0)*1000, 1),
            "note": "ZooKeeper not configured on this cluster",
        }
    else:
        try:
            zk_rows = _qsafe(cl, "SELECT count() FROM system.zookeeper WHERE path = '/'")
            root_children = int(zk_rows[0][0]) if (zk_rows and len(zk_rows[0]) > 0 and zk_rows[0][0] is not None) else 0
            out["zookeeper"] = {
                "ok": True, "reachable": True, "configured": True,
                "root_children": root_children,
                "round_trip_ms": round((time.time()-zk_t0)*1000, 1),
            }
        except Exception as e:
            out["zookeeper"] = {
                "ok": False, "reachable": False, "configured": True,
                "error": str(e)[:200],
                "round_trip_ms": round((time.time()-zk_t0)*1000, 1),
            }

    # ─── Recent errors ─────────────────────────────────────────────
    # Two buckets: "cluster" (real operational errors worth a human
    # eyeball) and "auth" (per-user authentication failures — useful but
    # not signal about cluster health itself, so we file them
    # separately). We also actively filter out a small set of error
    # kinds caused by the diagnostic queries this very endpoint
    # issues — UNKNOWN_TABLE for system.zookeeper, UNKNOWN_IDENTIFIER
    # for column drift between CH versions — because the UI used to
    # show "ERRORS (LAST HOUR): N" and N would tick up by exactly the
    # count that this same query just produced. That made the page
    # report its own activity as a problem.
    AUTH_ERROR_NAMES = {"WRONG_PASSWORD","REQUIRED_PASSWORD","UNKNOWN_USER","AUTHENTICATION_FAILED"}
    SELF_NOISE_HINTS = (
        "system.zookeeper",        # our ZK probe (now pre-gated, but stale entries linger)
        "postponed_till",           # column drift in older CH for rep queue / mutations
    )
    try:
        err_rows = _qsafe(cl, """
            SELECT name, value, last_error_time, last_error_message
              FROM system.errors
             WHERE last_error_time > now() - INTERVAL 1 HOUR
             ORDER BY value DESC
             LIMIT 80
        """)
        cluster_items = []
        auth_items = []
        cluster_total = 0
        auth_total = 0
        for r in err_rows:
            name = r[0] or ""
            cnt  = int(r[1] or 0)
            msg  = (r[3] or "")
            # Drop errors that are obviously our own diagnostic probes
            if any(h in msg for h in SELF_NOISE_HINTS):
                continue
            item = {
                "name": name, "count": cnt,
                "last_time": str(r[2]) if r[2] else "",
                "last_message": msg[:300],
            }
            if name in AUTH_ERROR_NAMES:
                auth_items.append(item); auth_total += cnt
            else:
                cluster_items.append(item); cluster_total += cnt
        out["errors"] = {
            "ok": True,
            "items": cluster_items,
            "total": cluster_total,
            "auth_items": auth_items,
            "auth_total": auth_total,
        }
    except Exception as e:
        out["errors"] = {"ok": False, "error": str(e)[:200]}

    # ─── Topology (cluster × shard × replica matrix) ────────────────
    # One row per cluster-shard-replica from system.clusters, with
    # errors_count + estimated_recovery_time so the UI can colour each
    # node by health. estimated_recovery_time may not exist on very old
    # CH versions — fall back to a query without it.
    try:
        topo_rows = []
        try:
            topo_rows = _qsafe(cl, """
                SELECT cluster, shard_num, replica_num, host_name, port,
                       is_local, errors_count, estimated_recovery_time
                  FROM system.clusters
                 WHERE cluster NOT LIKE 'test_%'
                ORDER BY cluster, shard_num, replica_num
            """)
        except Exception:
            topo_rows = _qsafe(cl, """
                SELECT cluster, shard_num, replica_num, host_name, port,
                       is_local, errors_count, 0 AS estimated_recovery_time
                  FROM system.clusters
                 WHERE cluster NOT LIKE 'test_%'
                ORDER BY cluster, shard_num, replica_num
            """)
        clusters = {}
        for r in topo_rows:
            cname = str(r[0])
            clusters.setdefault(cname, []).append({
                "shard_num":   int(r[1] or 0),
                "replica_num": int(r[2] or 0),
                "host_name":   str(r[3] or ""),
                "port":        int(r[4] or 0),
                "is_local":    bool(r[5]),
                "errors_count": int(r[6] or 0),
                "estimated_recovery_time": int(r[7] or 0),
            })
        out["topology"] = {"ok": True, "clusters": clusters}
    except Exception as e:
        out["topology"] = {"ok": False, "error": str(e)[:200]}

    return jsonify(out)


@app.route("/api/replication/queue", methods=["POST"])
def replication_queue():
    d = request.json or {}
    try:
        cl = _get_client(d)
        rows = _qsafe(cl, """
            SELECT database, table, replica_name, position, node_name,
                   type, create_time, required_quorum,
                   source_replica, new_part_name, parts_to_merge,
                   is_detach, is_currently_executing, num_tries,
                   last_exception, last_attempt_time, num_postponed,
                   postpone_reason, postponed_till, merge_type
            FROM system.replication_queue
            ORDER BY create_time DESC
            LIMIT 200
        """)
        queue = [{
            "database": r[0], "table": r[1], "replica": r[2],
            "position": int(r[3]), "node_name": r[4], "type": r[5],
            "create_time": str(r[6]), "quorum": int(r[7]),
            "source_replica": r[8], "new_part": r[9],
            "parts_to_merge": list(r[10]) if r[10] else [],
            "is_detach": bool(r[11]), "executing": bool(r[12]),
            "num_tries": int(r[13]),
            "last_exception": str(r[14]) if r[14] else "",
            "last_attempt": str(r[15]) if r[15] else "",
            "num_postponed": int(r[16]),
            "postpone_reason": r[17] or "", "postponed_till": str(r[18]) if r[18] else "",
            "merge_type": r[19] or "",
            "wait_secs": 0
        } for r in rows]
        # Calculate wait time
        import datetime as _dt
        now = _dt.datetime.now()
        for q in queue:
            try:
                ct = _dt.datetime.fromisoformat(q["create_time"])
                q["wait_secs"] = int((now-ct).total_seconds())
            except: pass
        cl.close()
        return jsonify({"queue": queue, "total": len(queue),
                       "executing": sum(1 for q in queue if q["executing"]),
                       "failed": sum(1 for q in queue if q["last_exception"])})
    except Exception as e: return jsonify({"error": str(e), "queue": []})

# ── column stats ──────────────────────────────────────────────────────────────
@app.route("/api/schema/column-stats", methods=["POST"])
def column_stats():
    d = request.json or {}
    db = _safe_ident(d.get("database",""),"database"); tbl = _safe_ident(d.get("table",""),"table"); col = _safe_ident(d.get("column",""),"column"); col_type = d.get("col_type","")
    if not all([db, tbl, col]): return jsonify({"error": "database, table, column required"}), 400
    try:
        cl = _get_client(d)
        is_numeric = any(t in col_type.upper() for t in ['INT','FLOAT','DECIMAL','DOUBLE','UINT','NUMERIC'])
        is_date = any(t in col_type.upper() for t in ['DATE','TIME'])
        stats = {}
        # Basic stats
        base = _qsafe(cl, f"""
            SELECT count(), countIf({col} IS NULL OR {col} = ''),
                   uniq({col})
            FROM {db}.{tbl}
        """)
        if base:
            stats["count"] = int(base[0][0])
            stats["nulls"] = int(base[0][1])
            stats["unique"] = int(base[0][2])
        # Top values
        top = _qsafe(cl, f"""
            SELECT {col}, count() as cnt
            FROM {db}.{tbl}
            WHERE {col} IS NOT NULL
            GROUP BY {col} ORDER BY cnt DESC LIMIT 10
        """)
        stats["top_values"] = [{"value": str(r[0]), "count": int(r[1])} for r in top]
        # Numeric stats
        if is_numeric or is_date:
            num = _qsafe(cl, f"""
                SELECT min({col}), max({col}), avg({col}), median({col})
                FROM {db}.{tbl}
            """)
            if num:
                stats["min"] = str(num[0][0])
                stats["max"] = str(num[0][1])
                stats["avg"] = str(round(float(num[0][2]),4)) if num[0][2] else None
                stats["median"] = str(num[0][3]) if num[0][3] else None
        cl.close()
        return jsonify({"stats": stats, "column": col, "type": col_type})
    except Exception as e: return jsonify({"error": str(e)})

# ── backup comparison ─────────────────────────────────────────────────────────
@app.route("/api/backup/compare", methods=["POST"])
def backup_compare():
    d = request.json or {}
    id1 = d.get("backup_id_1",""); id2 = d.get("backup_id_2","")
    catalog_file = d.get("catalog_file", "/var/lib/clickhouse/backups/catalog.json")
    if not id1 or not id2: return jsonify({"error": "Two backup IDs required"}), 400
    try:
        from pathlib import Path as _P
        import json as _json
        cat = _json.loads(_P(catalog_file).read_text())
        entries = {e["backup_id"]: e for e in cat.get("entries",[])}
        e1 = entries.get(id1); e2 = entries.get(id2)
        if not e1: return jsonify({"error": f"Backup {id1} not found"})
        if not e2: return jsonify({"error": f"Backup {id2} not found"})
        t1 = set(e1.get("tables",[])); t2 = set(e2.get("tables",[]))
        added = list(t2 - t1); removed = list(t1 - t2); common = list(t1 & t2)
        return jsonify({
            "backup1": {"id": id1, "timestamp": e1.get("timestamp",""), "size": e1.get("size_bytes",0), "type": e1.get("backup_type","")},
            "backup2": {"id": id2, "timestamp": e2.get("timestamp",""), "size": e2.get("size_bytes",0), "type": e2.get("backup_type","")},
            "tables_added": added, "tables_removed": removed, "tables_common": common,
            "size_diff": (e2.get("size_bytes",0) or 0) - (e1.get("size_bytes",0) or 0),
            "status_changed": e1.get("status") != e2.get("status")
        })
    except Exception as e: return jsonify({"error": str(e)})


# ── timeseries data for dashboard line charts ──────────────────────────────
@app.route("/api/dashboard/timeseries", methods=["POST"])
def dashboard_timeseries():
    import time as _time
    d = request.json or {}
    metric   = d.get("metric", "ActiveQueries")       # metric_log column or special
    hours    = float(d.get("hours", 1))
    points   = int(d.get("points", 60))               # number of data points
    special  = d.get("special", "")                   # query_log, error_rate, etc.
    try:
        cl = _get_client(d)
        interval_sec = max(1, int(hours * 3600 / points))
        # Custom absolute window (from/to) overrides the relative `hours` lookback.
        # Validated strictly (date-time chars only) so it is injection-safe inline.
        _from = (d.get("from") or "").strip().replace("T", " ")
        _to   = (d.get("to") or "").strip().replace("T", " ")
        def _ts_ok(s): return bool(re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?$', s))
        # Absolute custom window. Epoch seconds (from_ts/to_ts) are preferred: the
        # browser sends the user's local wall-clock converted to a UTC instant, and
        # fromUnixTimestamp() compares it in the server's own frame, so the window
        # lines up with event_time regardless of any browser/server timezone offset.
        # The literal from/to strings remain only as a backward-compatible fallback
        # (timezone-naive: they assume the server reads them in its own zone, which
        # is exactly what made custom ranges miss recent data across a tz offset).
        try:
            _fts = int(d.get("from_ts")); _tts = int(d.get("to_ts"))
        except (TypeError, ValueError):
            _fts = _tts = 0
        if _fts and _tts and 0 < _fts < _tts:
            where_time = (f"event_time >= fromUnixTimestamp({_fts}) "
                          f"AND event_time <= fromUnixTimestamp({_tts})")
            interval_sec = max(1, int((_tts - _fts) / points))
        elif _ts_ok(_from) and _ts_ok(_to) and _from < _to:
            where_time = f"event_time >= '{_from}' AND event_time <= '{_to}'"
            try:
                _norm = lambda s: s if len(s) > 16 else s + ":00"
                _span = int(_time.mktime(_time.strptime(_norm(_to),   "%Y-%m-%d %H:%M:%S")) -
                            _time.mktime(_time.strptime(_norm(_from), "%Y-%m-%d %H:%M:%S")))
                interval_sec = max(1, int(max(1, _span) / points))
            except Exception:
                pass
        else:
            where_time = f"event_time >= now() - INTERVAL {int(hours*3600)} SECOND"
        rows = []

        if special == "query_count":
            rows = _qsafe(cl, f"""
                SELECT toStartOfInterval(event_time, INTERVAL {interval_sec} SECOND) as t,
                       count() as v
                FROM system.query_log
                WHERE {where_time}
                  AND type = 'QueryFinish'
                GROUP BY t ORDER BY t
            """)
        elif special == "error_rate":
            rows = _qsafe(cl, f"""
                SELECT toStartOfInterval(event_time, INTERVAL {interval_sec} SECOND) as t,
                       countIf(exception != '') as v
                FROM system.query_log
                WHERE {where_time}
                  AND type = 'QueryFinish'
                GROUP BY t ORDER BY t
            """)
        elif special == "query_duration_p99":
            rows = _qsafe(cl, f"""
                SELECT toStartOfInterval(event_time, INTERVAL {interval_sec} SECOND) as t,
                       round(quantile(0.99)(query_duration_ms), 0) as v
                FROM system.query_log
                WHERE {where_time}
                  AND type = 'QueryFinish'
                GROUP BY t ORDER BY t
            """)
        elif special == "insert_rows":
            rows = _qsafe(cl, f"""
                SELECT toStartOfInterval(event_time, INTERVAL {interval_sec} SECOND) as t,
                       sum(written_rows) as v
                FROM system.query_log
                WHERE {where_time}
                  AND type = 'QueryFinish'
                  AND query ILIKE 'INSERT%'
                GROUP BY t ORDER BY t
            """)
        elif metric.startswith("async:"):
            # Pull from system.asynchronous_metric_log (key/value rows, not columns).
            # Used for OS-level metrics like OSUserTimeNormalized, TotalPartsOfMergeTreeTables, etc.
            async_metric_name = metric[6:].strip()
            # Validate the metric name to prevent SQL injection
            import re as _re
            if not _re.match(r'^[A-Za-z0-9_]+$', async_metric_name):
                raise ValueError(f"Invalid async metric name: {async_metric_name!r}")
            rows = _qsafe(cl, f"""
                SELECT toStartOfInterval(event_time, INTERVAL {interval_sec} SECOND) as t,
                       avg(value) as v
                FROM system.asynchronous_metric_log
                WHERE {where_time}
                  AND metric = '{async_metric_name}'
                GROUP BY t ORDER BY t
            """)
        else:
            # Try system.metric_log for real-time metrics. `metric` is a column
            # name interpolated into the projection, so it must be validated as a
            # bare identifier — same guard the async path already applied — or a
            # crafted metricCol on a saved widget could inject SQL.
            if not re.match(r'^[A-Za-z0-9_]+$', metric or ''):
                raise ValueError(f"Invalid metric name: {metric!r}")
            rows = _qsafe(cl, f"""
                SELECT toStartOfInterval(event_time, INTERVAL {interval_sec} SECOND) as t,
                       avg({metric}) as v
                FROM system.metric_log
                WHERE {where_time}
                GROUP BY t ORDER BY t
            """)

        cl.close()
        result = [{"t": str(r[0]), "v": round(float(r[1]), 2) if r[1] is not None else 0} for r in rows]
        if result:
            vals = [p["v"] for p in result]
            return jsonify({
                "points": result,
                "min": min(vals), "max": max(vals),
                "avg": round(sum(vals)/len(vals), 2),
                "last": vals[-1]
            })
        return jsonify({"points": [], "min": 0, "max": 0, "avg": 0, "last": 0})
    except Exception as e:
        return jsonify({"error": str(e), "points": []})



# ── Console User System ───────────────────────────────────────────────────────
import hashlib as _hashlib
CONSOLE_USERS_FILE = APP_DIR / "console-users.json"

ROLE_PERMISSIONS = {
    "admin": {
        "panels": ["all"],
        "can_run_queries": True,
        "can_write_queries": True,
        "can_kill": True,
        "can_backup": True,
        "can_manage_db_users": True,
        "can_manage_console_users": True,
        "can_edit_dashboard": True,
        "can_force_ttl": True,
        "can_kill_mutation": True,
        "can_branch": True,
        "can_reload_dicts": True,
        "can_view_alerts": True,
        "can_view_audit": True,
    },
    "developer": {
        "hidden_panels": ["users","alerts","activitylog","dbusers","audit","useractivity","grants","compliancepack","pitr","profiler","branch","consolelog","settings","usercost"],
        "can_run_queries": True,
        "can_write_queries": True,
        "can_kill": True,
        "can_backup": False,
        "can_manage_db_users": False,
        "can_manage_console_users": False,
        "can_edit_dashboard": False,
        "can_force_ttl": True,
        "can_kill_mutation": True,
        "can_branch": False,
        "can_reload_dicts": True,
        "can_view_alerts": False,
        "can_view_audit": False,
    },
    "monitoring": {
        # Show Monitoring section panels + Part Inspector + Storage. Everything else hidden.
        # Visible: monitor, cluster, dashboard, mutations, repqueue, health, parts, storage
        "hidden_panels": ["query","schema","slowlog","ttl","dicts","analyzer",
                          "zookeeper","pitr","profiler","branch","users","connections","dbusers",
                          "audit","useractivity","grants","compliancepack","activitylog","alerts","settings","consolelog"],
        "can_run_queries": True,         # needed for dashboard widget queries
        "can_write_queries": False,
        "can_kill": False,
        "can_backup": False,
        "can_manage_db_users": False,
        "can_manage_console_users": False,
        "can_edit_dashboard": True,      # ← the distinguishing capability
        "can_force_ttl": False,
        "can_kill_mutation": False,
        "can_branch": False,
        "can_reload_dicts": False,
        "can_view_alerts": False,
        "can_view_audit": False,
    },
    "readonly": {
        "hidden_panels": ["users","alerts","activitylog","dbusers","audit","useractivity","grants","compliancepack","pitr","profiler","branch","consolelog","usercost"],
        "can_run_queries": True,
        "can_write_queries": False,
        "can_kill": False,
        "can_backup": False,
        "can_manage_db_users": False,
        "can_manage_console_users": False,
        "can_edit_dashboard": False,
        "can_force_ttl": False,
        "can_kill_mutation": False,
        "can_branch": False,
        "can_reload_dicts": False,
        "can_view_alerts": False,
        "can_view_audit": False,
    }
}

def _client_ip():
    """Real client IP for security decisions (rate limiting), read from the
    trusted reverse proxy's X-Real-IP, then the first X-Forwarded-For hop,
    falling back to the direct peer. nginx sets X-Real-IP to the true client
    address regardless of any client-supplied header, so it cannot be spoofed
    as long as the app is reached only through the proxy. Using remote_addr
    here would key every user to the proxy's own IP and let one attacker lock
    out all logins, so the forwarded value is required."""
    xri = (request.headers.get("X-Real-IP", "") or "").strip()
    if xri:
        return xri
    xff = request.headers.get("X-Forwarded-For", "") or ""
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or ""


@app.route("/api/console/login", methods=["POST"])
def console_login():
    d = request.json or {}
    username = (d.get("username") or "").strip()
    password = d.get("password") or ""
    # auth_method explicitly selected by the user via the login screen
    # radio buttons. Defaults to "local" so existing API clients (and the
    # SSO/OIDC handler) continue to work unchanged.
    auth_method = (d.get("auth_method") or "local").strip().lower()

    # Brute-force gate: refuse before touching credentials if this username or
    # source IP has exceeded the failed-attempt budget for the current window.
    _ip = _client_ip()
    _blocked, _retry, _scope = rate_limit.check(username, _ip)
    if _blocked:
        audit("Login.Blocked", "auth",
              f"{_scope} lockout username={username} ip={_ip} retry_after={_retry}s",
              result="denied")
        _mins = max(1, (_retry + 59) // 60)
        resp = jsonify({"ok": False,
                        "error": f"Too many failed login attempts. Try again in {_mins} minute(s)."})
        resp.headers["Retry-After"] = str(_retry)
        return resp, 429

    # ─── LDAP path ──────────────────────────────────────────────────
    if auth_method == "ldap":
        try:
            user_id, role, groups = _ldap_authenticate(username, password)
        except PermissionError as pe:
            rate_limit.record_failure(username, _ip)
            audit("Login.Failed", "auth",
                  f"ldap username={username} reason={str(pe)[:120]}", result="fail")
            return jsonify({"ok": False, "error": "Invalid LDAP credentials"}), 401
        except Exception as e:
            logger.error(f"LDAP login error: {e}")
            audit("Login.Failed", "auth",
                  f"ldap username={username} error={str(e)[:120]}", result="fail")
            return jsonify({"ok": False,
                            "error": f"LDAP error: {str(e)[:200]}"}), 500
        rate_limit.clear(username)
        token, exp_ts = create_session(user_id, request.remote_addr or "",
                                       request.headers.get("User-Agent","")[:300])
        g.user = {"id": user_id, "username": username, "email": "", "role": role}
        audit("Login", "auth",
              f"LDAP login: {username} ({role}); groups={','.join(groups) or '(none mapped)'}")
        perms = ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["readonly"])
        resp = jsonify({"ok": True, "username": username, "role": role,
                        "permissions": perms, "auth_source": "ldap"})
        resp.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, samesite="Lax",
                        max_age=SESSION_TTL_DAYS*86400, path="/")
        return resp

    # ─── Local path (unchanged behaviour) ───────────────────────────
    try:
        row = db().execute("SELECT id,username,password_hash,role,is_active,auth_source FROM users WHERE username=?",
                           (username,)).fetchone()
        # Local login deliberately rejects rows whose auth_source is 'ldap'
        # — those accounts have NULL password_hash, and we don't want a
        # silent fallthrough that confuses operators ("my LDAP password
        # doesn't work in local mode" is the kind of bug worth heading
        # off explicitly).
        if not row or row["auth_source"] == "ldap" or not row["is_active"] \
                or not verify_password(password, row["password_hash"]):
            rate_limit.record_failure(username, _ip)
            audit("Login.Failed", "auth", f"local username={username}", result="fail")
            return jsonify({"ok": False, "error": "Invalid username or password"}), 401
        # Upgrade legacy SHA256 hash to PBKDF2 on successful login
        if not row["password_hash"].startswith("pbkdf2_sha256$"):
            db().execute("UPDATE users SET password_hash=? WHERE id=?",
                         (hash_password(password), row["id"]))
        db().execute("UPDATE users SET last_login_at=now() WHERE id=?", (row["id"],))
        db().commit()
        rate_limit.clear(row["username"])
        token, exp_ts = create_session(row["id"], request.remote_addr or "",
                                       request.headers.get("User-Agent","")[:300])
        # Make g.user available for the audit() call below
        g.user = {"id": row["id"], "username": row["username"], "email": "", "role": row["role"]}
        audit("Login", "auth", f"User logged in: {row['username']} ({row['role']})")
        perms = ROLE_PERMISSIONS.get(row["role"], ROLE_PERMISSIONS["readonly"])
        resp = jsonify({"ok": True, "username": row["username"], "role": row["role"],
                        "permissions": perms, "auth_source": "local"})
        resp.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, samesite="Lax",
                        max_age=SESSION_TTL_DAYS*86400, path="/")
        return resp
    except Exception as e:
        logger.error(f"login error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/console/logout", methods=["POST"])
def console_logout():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        try:
            u = get_session_user(token)
            if u:
                g.user = u
                audit("Logout", "auth", f"User signed out: {u['username']} ({u['role']})")
            delete_session(token)
        except Exception: pass
    resp = jsonify({"ok": True})
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp

@app.route("/api/console/profile", methods=["GET"])
def console_profile():
    """Returns the signed-in user's profile: identity, role, email, creation
    time, last login, and the list of their own currently-active sessions
    (so a user can see "where am I signed in" without admin rights)."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user  = get_session_user(token) if token else None
    if not user: return jsonify({"error": "unauthenticated"}), 401
    row = db().execute(
        "SELECT id, username, email, first_name, last_name, role, is_active, "
        "created_at, last_login_at "
        "FROM users WHERE id=?", (user["id"],)
    ).fetchone()
    if not row: return jsonify({"error": "user not found"}), 404
    # User's own active sessions — never expose full tokens, only prefixes.
    # Phase 2: sourced from Redis (newest first, mirroring the old
    # ORDER BY created_at DESC).
    srows = sorted(
        session_store.list_user_sessions(user["id"]),
        key=lambda s: s.get("created_at", ""), reverse=True,
    )
    own_sessions = []
    for s in srows:
        own_sessions.append({
            "token_prefix": (s.get("token") or "")[:12],
            "created_at":   s.get("created_at", ""),
            "expires_at":   s.get("expires_at", ""),
            "ip":           s.get("ip") or "",
            "user_agent":   (s.get("user_agent") or "")[:200],
            "is_current":   s.get("token") == token,
        })
    return jsonify({
        "user": {
            "username":      row["username"],
            "email":         row["email"] or "",
            "first_name":    row["first_name"] or "",
            "last_name":     row["last_name"] or "",
            "role":          row["role"],
            "is_active":     bool(row["is_active"]),
            "created_at":    row["created_at"],
            "last_login_at": row["last_login_at"] or "",
        },
        "sessions": own_sessions,
        "session_count": len(own_sessions),
    })

@app.route("/api/console/password", methods=["POST"])
def console_password_change():
    """A user changes their OWN password. Requires the current password as
    verification — admins changing other users' passwords use a separate
    admin endpoint. On success, all of the user's OTHER sessions are revoked
    so a leaked credential cannot keep an attacker signed in elsewhere; the
    current session is preserved so the user does not need to re-login."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user  = get_session_user(token) if token else None
    if not user: return jsonify({"error": "unauthenticated"}), 401
    d = request.json or {}
    old_pw = d.get("current_password") or ""
    new_pw = d.get("new_password") or ""
    # Connection context (the cluster the user was on), sent by the UI so this
    # audit event's "Connected To" matches every other logged action.
    _ch = d.get("conn_host", "")
    _cu = d.get("conn_user", "")
    if not old_pw or not new_pw:
        return jsonify({"error": "current_password and new_password required"}), 400
    if len(new_pw) < 8:
        audit(action="Change Own Password", panel="profile",
              detail="rejected: new password must be at least 8 characters",
              conn_host=_ch, conn_user=_cu, result="failed")
        return jsonify({"error": "new password must be at least 8 characters"}), 400
    if new_pw == old_pw:
        audit(action="Change Own Password", panel="profile",
              detail="rejected: new password is the same as the current password",
              conn_host=_ch, conn_user=_cu, result="failed")
        return jsonify({"error": "New password must be different from the current password"}), 400
    # Verify current password
    row = db().execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if not row or not verify_password(old_pw, row["password_hash"]):
        audit(action="Change Own Password", panel="profile",
              detail="rejected: current password did not verify",
              conn_host=_ch, conn_user=_cu, result="failed")
        return jsonify({"error": "current password is incorrect"}), 401
    # Update
    new_hash = hash_password(new_pw)
    db().execute("UPDATE users SET password_hash=? WHERE id=?", (new_hash, user["id"]))
    db().commit()
    # Revoke ALL other sessions (keep current). Phase 2: Redis. A leaked
    # credential can't keep an attacker signed in elsewhere.
    other = session_store.del_user_tokens(user["id"], except_token=token)
    try: _refresh_sessions_log()
    except Exception: pass
    audit(action="Change Own Password", panel="profile",
          detail=f"password changed successfully · {other} other session(s) revoked",
          conn_host=_ch, conn_user=_cu, result="ok")
    return jsonify({"ok": True, "other_sessions_revoked": other})

@app.route("/api/console/me", methods=["GET"])
def console_me():
    """Returns current user (or 401). Frontend calls this on page load to restore session."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user  = get_session_user(token) if token else None
    if not user:
        return jsonify({"authenticated": False}), 401
    perms = ROLE_PERMISSIONS.get(user["role"], ROLE_PERMISSIONS["readonly"])
    return jsonify({"authenticated": True, "username": user["username"],
                    "role": user["role"], "email": user["email"], "permissions": perms})

# ── per-user query favorites ──────────────────────────────────────────────────
# Each user sees only their own favorites. Scoped per connection (host:port@db).
@app.route("/api/favorites", methods=["GET"])
def favorites_list():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user  = get_session_user(token) if token else None
    if not user: return jsonify({"error": "unauthenticated"}), 401
    conn = (request.args.get("conn") or "").strip()
    if not conn: return jsonify({"favorites": []})
    dbu = db_user(user["username"])
    if not dbu:
        return jsonify({"favorites": []})
    rows = dbu.execute(
        "SELECT id, name, sql, created_at FROM query_favorites "
        "WHERE user_id=? AND conn_label=? ORDER BY name",
        (dbu.user_id, conn,)
    ).fetchall()
    return jsonify({"favorites": [dict(r) for r in rows]})

@app.route("/api/favorites", methods=["POST"])
def favorites_save():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user  = get_session_user(token) if token else None
    if not user: return jsonify({"error": "unauthenticated"}), 401
    d = request.json or {}
    conn = (d.get("conn") or "").strip()
    name = (d.get("name") or "").strip()
    sql  = (d.get("sql")  or "").strip()
    if not (conn and name and sql):
        return jsonify({"error": "conn, name, and sql are required"}), 400
    if len(name) > 100 or len(sql) > 100000:
        return jsonify({"error": "name max 100 chars, sql max 100000"}), 400
    # UPSERT on (user_id, conn_label, name) — explicit user_id since per-user DB
    # files no longer exist (v4: shared Postgres).
    conn_db = db_user(user["username"])
    if not conn_db:
        return jsonify({"error": "user lookup failed"}), 500
    conn_db.execute(
        "INSERT INTO query_favorites(user_id, conn_label, name, sql) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id, conn_label, name) DO UPDATE SET sql=excluded.sql, "
        "created_at=now()",
        (conn_db.user_id, conn, name, sql)
    )
    conn_db.commit()
    row = conn_db.execute(
        "SELECT id, name, sql, created_at FROM query_favorites "
        "WHERE user_id=? AND conn_label=? AND name=?",
        (conn_db.user_id, conn, name)
    ).fetchone()
    return jsonify({"ok": True, "favorite": dict(row) if row else None})

@app.route("/api/favorites/<int:fav_id>", methods=["DELETE"])
def favorites_delete(fav_id):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user  = get_session_user(token) if token else None
    if not user: return jsonify({"error": "unauthenticated"}), 401
    # User scoping via explicit user_id (v4: shared Postgres, no per-user DB).
    conn_db = db_user(user["username"])
    if not conn_db:
        return jsonify({"error": "user lookup failed"}), 500
    cur = conn_db.execute("DELETE FROM query_favorites WHERE user_id=? AND id=?",
                          (conn_db.user_id, fav_id))
    conn_db.commit()
    return jsonify({"ok": True, "deleted": cur.rowcount})

@app.route("/api/console/users", methods=["GET"])
@require_role("admin")
def console_users_list():
    rows = db().execute("""SELECT id,username,email,first_name,last_name,role,is_active,
                                  COALESCE(to_char(created_at,'YYYY-MM-DD HH24:MI:SS'),'') as created,
                                  COALESCE(to_char(last_login_at,'YYYY-MM-DD HH24:MI:SS'),'') as last_login
                           FROM users ORDER BY id""").fetchall()
    return jsonify({"users": [dict(r) for r in rows], "roles": list(ROLE_PERMISSIONS.keys())})

@app.route("/api/license/status", methods=["GET"])
def license_status():
    """Public — frontend reads this to show license banner. No PII."""
    return jsonify({
        "valid":      LICENSE.get("valid", False),
        "mode":       LICENSE.get("mode", "community"),
        "customer":   LICENSE.get("customer", ""),
        "expires_at": LICENSE.get("expires_at", ""),
        "max_users":  LICENSE.get("max_users", COMMUNITY_LIMITS["max_users"]),
        "features":   LICENSE.get("features", []),
        "warning":    LICENSE.get("warning", ""),
    })

# ── Admin-only settings: license apply/remove + system info ────────────────
@app.route("/api/admin/sessions", methods=["GET"])
@require_role("admin")
def admin_sessions_list():
    """Return all currently-active (non-expired) console sessions. The admin
    UI's Active Sessions panel polls this endpoint to render a live view of
    who is signed in, from where, and since when."""
    # Phase 2: enumerate from Redis. username/role/email are denormalized
    # into each session hash, so no JOIN is needed.
    rows = sorted(
        session_store.scan_sessions(),
        key=lambda s: s.get("created_at", ""), reverse=True,
    )
    # Mask the token: never expose the full token; the first 12 chars are
    # enough to identify a session for revocation. The revoke endpoint
    # accepts that prefix as a token_prefix parameter.
    current_token = request.cookies.get(SESSION_COOKIE_NAME)
    sessions = []
    for r in rows:
        try: uid = int(r.get("user_id"))
        except (TypeError, ValueError): uid = None
        sessions.append({
            "token_prefix": (r.get("token") or "")[:12],
            "user_id":      uid,
            "username":     r.get("username", ""),
            "role":         r.get("role", ""),
            "email":        r.get("email") or "",
            "created_at":   r.get("created_at", ""),
            "expires_at":   r.get("expires_at", ""),
            "ip":           r.get("ip") or "",
            "user_agent":   (r.get("user_agent") or "")[:200],
            "is_current":   r.get("token") == current_token,
        })
    return jsonify({"sessions": sessions, "count": len(sessions)})

@app.route("/api/admin/sessions/revoke", methods=["POST"])
@require_role("admin")
def admin_sessions_revoke():
    """Forcibly revoke a session by its token prefix. The session's user is
    signed out on their next request. An audit event is recorded."""
    d = request.json or {}
    prefix = (d.get("token_prefix") or "").strip()
    if not prefix or len(prefix) < 8:
        return jsonify({"error": "token_prefix required (at least 8 chars)"}), 400
    # Phase 2: find the session by token prefix in Redis. The token is part
    # of the key, so a prefix scan does the work the old SQL LIKE did.
    matches = session_store.scan_by_prefix(prefix)
    if not matches:
        return jsonify({"error": "session not found"}), 404
    if len(matches) > 1:
        return jsonify({"error": "token_prefix is ambiguous — provide more characters"}), 400
    row = matches[0]
    # Protect against accidental self-revoke without explicit confirmation.
    # (Bug fix: this previously checked cookie "session" — the real cookie
    # name is SESSION_COOKIE_NAME ("ch_session") — so the guard never fired.)
    if row.get("token") == request.cookies.get(SESSION_COOKIE_NAME) and not d.get("confirm_self_revoke"):
        return jsonify({"error": "you would revoke your own session — pass confirm_self_revoke=true to proceed"}), 400
    session_store.del_session(row["token"])
    try: _refresh_sessions_log()
    except Exception: pass
    audit(action="Session Revoked", panel="security",
          detail=f"admin revoked session of {row.get('username','')} ({row.get('role','')})\n"
                 f"prefix={prefix}  ip={row.get('ip','')}  ua={(row.get('user_agent') or '')[:120]}",
          result="ok")
    return jsonify({"ok": True, "revoked_user": row.get("username", "")})

@app.route("/api/admin/license", methods=["GET"])
@require_role("admin")
def admin_license_get():
    return jsonify({
        "license":       LICENSE,
        "license_file":  str(LICENSE_FILE),
        "license_present": LICENSE_FILE.exists(),
        "public_key_present": PUBLIC_KEY_FILE.exists(),
        "active_users": db().execute("SELECT count(*) FROM users WHERE is_active=1").fetchone()[0],
    })

@app.route("/api/admin/license", methods=["POST"])
@require_role("admin")
def admin_license_apply():
    """Apply a new license token. Validates RSA signature first."""
    global LICENSE
    d = request.json or {}
    token = (d.get("token") or "").strip()
    if not token:
        return jsonify({"error": "license token required"}), 400
    if not PUBLIC_KEY_FILE.exists():
        return jsonify({"error": f"public_key.pem not found at {PUBLIC_KEY_FILE}. Cannot verify license."}), 500
    try:
        payload = _verify_license_token(token, PUBLIC_KEY_FILE.read_bytes())
    except Exception as e:
        return jsonify({"error": f"License signature invalid: {e}"}), 400
    # Check expiry up front so we don't overwrite a valid license with a stale one
    exp = payload.get("expires_at", "")
    try:
        exp_dt = datetime.fromisoformat(exp.replace("Z","+00:00"))
        if datetime.now(timezone.utc) > exp_dt:
            return jsonify({"error": f"License already expired ({exp})"}), 400
    except Exception:
        pass
    # Check instance binding — refuse to apply a license bound to a different instance.
    raw_fp = payload.get("fingerprint") or payload.get("fingerprints") or ""
    if isinstance(raw_fp, str):
        bound_list = [s.strip() for s in raw_fp.split(",") if s.strip()] if raw_fp else []
    elif isinstance(raw_fp, list):
        bound_list = [str(s).strip() for s in raw_fp if str(s).strip()]
    else:
        bound_list = []
    if bound_list and INSTANCE_FINGERPRINT not in bound_list:
        return jsonify({
            "error": (f"License is bound to a different instance. "
                      f"Send your instance fingerprint ({INSTANCE_FINGERPRINT}) to your vendor "
                      f"to receive a license valid for this installation."),
            "fingerprint_mismatch": True,
            "this_instance_fingerprint": INSTANCE_FINGERPRINT,
            "license_bound_to": [fp[:12]+"…" for fp in bound_list],
        }), 400
    try:
        LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LICENSE_FILE.write_text(token)
        try: os.chmod(LICENSE_FILE, 0o600)
        except Exception: pass
    except Exception as e:
        return jsonify({"error": f"failed to save license: {e}"}), 500
    LICENSE = load_license_state()
    audit("LicenseApply", "settings",
          f"customer={payload.get('customer','')} max_users={payload.get('max_users','')} expires={exp}"
          + (f" bound_to={len(bound_list)}_fp" if bound_list else " unbound"))
    return jsonify({"ok": True, "license": LICENSE})

@app.route("/api/admin/license", methods=["DELETE"])
@require_role("admin")
def admin_license_remove():
    """Remove license file → revert to community mode."""
    global LICENSE
    if LICENSE_FILE.exists():
        try: LICENSE_FILE.unlink()
        except Exception as e: return jsonify({"error": str(e)}), 500
    LICENSE = load_license_state()
    audit("LicenseRemove", "settings", "")
    return jsonify({"ok": True, "license": LICENSE})

@app.route("/api/admin/system/info", methods=["GET"])
@require_role("admin")
def admin_system_info():
    n_total  = db().execute("SELECT count(*) FROM users").fetchone()[0]
    n_active = db().execute("SELECT count(*) FROM users WHERE is_active=1").fetchone()[0]
    n_admin  = db().execute("SELECT count(*) FROM users WHERE role='admin' AND is_active=1").fetchone()[0]
    n_sess   = session_store.count_sessions()
    return jsonify({
        "version":        "4.0",
        "license":        LICENSE,
        "license_file":   str(LICENSE_FILE),
        "license_present": LICENSE_FILE.exists(),
        "public_key_file": str(PUBLIC_KEY_FILE),
        "public_key_present": PUBLIC_KEY_FILE.exists(),
        "instance_fingerprint": INSTANCE_FINGERPRINT,
        "vault_available": MASTER_KEY is not None,
        "master_key_source": ("env" if os.environ.get("MASTER_KEY")
                              else (f"file:{MASTER_KEY_FILE}" if MASTER_KEY_FILE.exists() else "missing")),
        "sso_enabled":    oidc_enabled(),
        "sso_discovery_url": OIDC_CONFIG["discovery_url"] if oidc_enabled() else "",
        "sso_default_role":  OIDC_CONFIG["default_role"] if oidc_enabled() else "",
        "session_ttl_days": SESSION_TTL_DAYS,
        "db_path":        str(DB_PATH),
        "user_counts":    {"total": n_total, "active": n_active, "admin": n_admin},
        "active_sessions": n_sess,
    })

# ═══════════════════════════════════════════════════════════════════════════
# SSO (OIDC)  (Step 2)
#   Configure via env vars; if any required var is missing, endpoints return
#   501 and the "Sign in with SSO" button stays hidden in the UI.
#   Tested against:  Keycloak, Google, Okta, Azure AD providers should work.
# ═══════════════════════════════════════════════════════════════════════════
import urllib.request, urllib.parse, ssl as _ssl

OIDC_CONFIG = {
    "discovery_url": os.environ.get("OIDC_DISCOVERY_URL", "").strip(),
    "client_id":     os.environ.get("OIDC_CLIENT_ID", "").strip(),
    "client_secret": os.environ.get("OIDC_CLIENT_SECRET", "").strip(),
    "redirect_uri":  os.environ.get("OIDC_REDIRECT_URI", "").strip(),
    "default_role":  os.environ.get("OIDC_DEFAULT_ROLE", "readonly").strip(),
    "role_claim":    os.environ.get("OIDC_ROLE_CLAIM", "").strip(),
    "role_mapping":  os.environ.get("OIDC_ROLE_MAPPING", "{}").strip(),
    "scope":         os.environ.get("OIDC_SCOPE", "openid email profile").strip(),
}
_OIDC_DISCOVERY_CACHE = None
_SSO_STATES = {}  # state nonce → {created} for CSRF protection

def oidc_enabled():
    c = OIDC_CONFIG
    return all([c["discovery_url"], c["client_id"], c["client_secret"], c["redirect_uri"]])

def _oidc_discovery():
    global _OIDC_DISCOVERY_CACHE
    if _OIDC_DISCOVERY_CACHE: return _OIDC_DISCOVERY_CACHE
    if not OIDC_CONFIG["discovery_url"]: return None
    try:
        ctx = _ssl.create_default_context()
        with urllib.request.urlopen(OIDC_CONFIG["discovery_url"], timeout=10, context=ctx) as r:
            _OIDC_DISCOVERY_CACHE = json.loads(r.read().decode())
        return _OIDC_DISCOVERY_CACHE
    except Exception as e:
        logger.error(f"OIDC discovery failed: {e}")
        return None

@app.route("/api/auth/sso/providers", methods=["GET"])
def sso_providers():
    if not oidc_enabled(): return jsonify({"providers": []})
    return jsonify({"providers": [{"id": "oidc", "name": "Single Sign-On"}]})

@app.route("/api/auth/sso/login", methods=["GET"])
def sso_login():
    if not oidc_enabled(): return jsonify({"error":"SSO not configured (set OIDC_* env vars)"}), 501
    disc = _oidc_discovery()
    if not disc or not disc.get("authorization_endpoint"):
        return jsonify({"error":"OIDC discovery failed"}), 502
    state = secrets.token_urlsafe(24)
    _SSO_STATES[state] = {"created": time.time()}
    # purge stale states (>10 min)
    for k, v in list(_SSO_STATES.items()):
        if time.time() - v["created"] > 600: _SSO_STATES.pop(k, None)
    params = {
        "response_type": "code",
        "client_id":     OIDC_CONFIG["client_id"],
        "redirect_uri":  OIDC_CONFIG["redirect_uri"],
        "scope":         OIDC_CONFIG["scope"],
        "state":         state,
    }
    from flask import redirect
    return redirect(disc["authorization_endpoint"] + "?" + urllib.parse.urlencode(params))

@app.route("/api/auth/sso/callback", methods=["GET"])
def sso_callback():
    if not oidc_enabled(): return jsonify({"error":"SSO not configured"}), 501
    disc = _oidc_discovery()
    if not disc: return jsonify({"error":"OIDC discovery failed"}), 502
    code  = request.args.get("code")
    state = request.args.get("state")
    err   = request.args.get("error")
    if err: return f"<h1>SSO error</h1><pre>{err}: {request.args.get('error_description','')}</pre>", 400
    if not code or not state or state not in _SSO_STATES:
        return "<h1>Invalid SSO callback</h1><p>state mismatch or expired</p>", 400
    _SSO_STATES.pop(state, None)
    # Exchange code → token
    body = urllib.parse.urlencode({
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  OIDC_CONFIG["redirect_uri"],
        "client_id":     OIDC_CONFIG["client_id"],
        "client_secret": OIDC_CONFIG["client_secret"],
    }).encode()
    try:
        ctx = _ssl.create_default_context()
        req = urllib.request.Request(disc["token_endpoint"], data=body,
            headers={"Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            tokens = json.loads(r.read().decode())
        access_token = tokens.get("access_token")
        if not access_token:
            return f"<h1>SSO failed</h1><pre>token response missing access_token</pre>", 502
        ui_req = urllib.request.Request(disc["userinfo_endpoint"],
            headers={"Authorization": "Bearer " + access_token})
        with urllib.request.urlopen(ui_req, timeout=10, context=ctx) as r:
            userinfo = json.loads(r.read().decode())
    except Exception as e:
        logger.error(f"SSO token/userinfo exchange failed: {e}")
        return f"<h1>SSO failed</h1><pre>{e}</pre>", 502
    sub = userinfo.get("sub")
    if not sub: return "<h1>SSO failed</h1><pre>no 'sub' in userinfo</pre>", 502
    email = (userinfo.get("email") or "").strip()
    display = (userinfo.get("preferred_username") or userinfo.get("name") or email or sub)
    # Map role
    role = OIDC_CONFIG["default_role"] if OIDC_CONFIG["default_role"] in ROLES else "readonly"
    if OIDC_CONFIG["role_claim"]:
        try:
            mapping = json.loads(OIDC_CONFIG["role_mapping"] or "{}")
            claim_val = userinfo.get(OIDC_CONFIG["role_claim"], [])
            if isinstance(claim_val, str): claim_val = [claim_val]
            for v in claim_val:
                if v in mapping and mapping[v] in ROLES:
                    role = mapping[v]; break
        except Exception: pass
    # Find or create local user
    row = db().execute("SELECT id, username, role, is_active FROM users WHERE sso_provider=? AND sso_subject=?",
                       ("oidc", sub)).fetchone()
    if row:
        if not row["is_active"]:
            return "<h1>Account disabled</h1>", 403
        user_id, username, user_role = row["id"], row["username"], row["role"]
        db().execute("UPDATE users SET last_login_at=datetime('now') WHERE id=?", (user_id,))
        db().commit()
    else:
        ok, msg = license_check_user_limit()
        if not ok: return f"<h1>License limit reached</h1><p>{msg}</p>", 402
        # Generate unique username
        base = re.sub(r"[^a-zA-Z0-9_-]", "_", display).lower()[:32] or ("sso_" + sub[:8])
        username = base; n = 1
        while db().execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            n += 1; username = f"{base}{n}"
        random_pw = hash_password(secrets.token_urlsafe(32))
        db().execute("""INSERT INTO users(username,email,password_hash,role,sso_subject,sso_provider,last_login_at)
                        VALUES(?,?,?,?,?,?,datetime('now'))""",
                     (username, email, random_pw, role, sub, "oidc"))
        db().commit()
        user_id = db().execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
        user_role = role
    # Issue session
    g.user = {"id": user_id, "username": username, "email": email, "role": user_role}
    audit("Login.SSO", "auth", f"SSO login: {username} ({user_role})")
    token, _ = create_session(user_id, request.remote_addr or "", request.headers.get("User-Agent","")[:300])
    from flask import redirect, make_response
    resp = make_response(redirect("/"))
    resp.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, samesite="Lax",
                    max_age=SESSION_TTL_DAYS*86400, path="/")
    return resp

@app.route("/api/console/users/create", methods=["POST"])
@require_role("admin")
def console_users_create():
    d = request.json or {}
    username   = (d.get("username") or "").strip()
    password   = d.get("password") or ""
    role       = d.get("role") or "readonly"
    email      = (d.get("email") or "").strip()
    first_name = (d.get("first_name") or "").strip()
    last_name  = (d.get("last_name") or "").strip()
    if not username or not password: return jsonify({"error": "Username and password required"}), 400
    if role not in ROLES: return jsonify({"error": "Invalid role"}), 400
    if len(password) < 6: return jsonify({"error": "Password must be at least 6 characters"}), 400
    # Username must be filesystem-safe (it becomes the logs/users/<username>/ folder name)
    import re as _re
    if not _re.match(r"^[A-Za-z0-9._-]{1,32}$", username):
        return jsonify({"error": "Username must be 1-32 chars, letters/digits/._- only"}), 400
    ok, msg = license_check_user_limit()
    if not ok: return jsonify({"error": msg, "license_block": True}), 402
    try:
        db().execute(
            "INSERT INTO users(username,email,first_name,last_name,password_hash,role) "
            "VALUES(?,?,?,?,?,?)",
            (username, email, first_name, last_name, hash_password(password), role),
        )
        db().commit()
        # Create per-user folder + DB so they're ready on first login
        _init_user_db(username)
        audit("UserCreate", "users", f"Created user: {username} ({role})")
        return jsonify({"ok": True})
    except IntegrityError:
        try: db().rollback()
        except Exception: pass
        return jsonify({"error": "User already exists"}), 409
    except Exception as e:
        try: db().rollback()
        except Exception: pass
        return jsonify({"error": str(e)}), 500

@app.route("/api/console/users/delete", methods=["POST"])
@require_role("admin")
def console_users_delete():
    d = request.json or {}
    username = (d.get("username") or "").strip()
    if not username: return jsonify({"error":"Username required"}), 400
    if username == g.user["username"]: return jsonify({"error":"Cannot delete yourself"}), 400
    row = db().execute("SELECT id,role FROM users WHERE username=?", (username,)).fetchone()
    if not row: return jsonify({"error":"User not found"}), 404
    # Prevent deleting the last admin
    if row["role"] == "admin":
        adm = db().execute("SELECT count(*) FROM users WHERE role='admin' AND is_active=1").fetchone()[0]
        if adm <= 1: return jsonify({"error":"Cannot delete the last admin"}), 400
    # Soft-delete: deactivate the account instead of physically removing the
    # row. The username, role, audit history and per-user logs are ALL kept;
    # the account simply can no longer log in. An admin can reactivate it
    # later by setting is_active back to 1.
    db().execute("UPDATE users SET is_active=0 WHERE id=?", (row["id"],))
    db().commit()
    # Revoke the deactivated user's Redis sessions so they are signed out
    # everywhere immediately — a deactivated account must not stay logged in.
    try:
        session_store.del_user_tokens(row["id"])
    except Exception as e:
        logger.warning(f"could not revoke sessions for deactivated user {username}: {e}")
    # NOTE: the per-user log directory logs/users/<username>/ is intentionally
    # left completely untouched here — NOT renamed, NOT deleted. The activity /
    # audit logs stay exactly where they are, both because the account may be
    # reactivated and because those logs are an append-only forensic record.
    audit("UserDelete", "users", f"Deactivated user (soft-delete): {username}")
    return jsonify({"ok": True})

@app.route("/api/console/users/change-password", methods=["POST"])
@require_role("admin")
def console_users_change_password():
    d = request.json or {}
    username = (d.get("username") or "").strip()
    new_pw   = d.get("password") or ""
    # Connection context (the cluster the admin was on), sent by the UI so this
    # audit event's "Connected To" matches every other logged action.
    _ch = d.get("conn_host", "")
    _cu = d.get("conn_user", "")
    if not username or not new_pw: return jsonify({"error":"Username and password required"}), 400
    if len(new_pw) < 6: return jsonify({"error": "Password must be at least 6 characters"}), 400
    # Look the user up first — needed both to confirm they exist and to check
    # the "new" password is not simply their current one.
    urow = db().execute("SELECT id, password_hash FROM users WHERE username=?",
                        (username,)).fetchone()
    if not urow: return jsonify({"error":"User not found"}), 404
    # Reject if the new password is actually the user's current password. This
    # covers an admin resetting anyone's password — including their own — to a
    # value it already has.
    if verify_password(new_pw, urow["password_hash"]):
        audit("UserPasswordChange", "users",
              f"rejected: new password is the same as the current password for {username}",
              conn_host=_ch, conn_user=_cu, result="failed")
        return jsonify({"error": "New password must be different from the current password"}), 400
    db().execute("UPDATE users SET password_hash=? WHERE id=?",
                 (hash_password(new_pw), urow["id"]))
    db().commit()
    # Invalidate all sessions for that user (force re-login). Phase 2: Redis.
    try: session_store.del_user_tokens(urow["id"])
    except Exception as e: logger.warning(f"session revoke failed for {username}: {e}")
    try: _refresh_sessions_log()
    except Exception: pass
    audit("UserPasswordChange", "users", f"Password reset for: {username}",
          conn_host=_ch, conn_user=_cu)
    return jsonify({"ok": True})

@app.route("/api/console/users/set-role", methods=["POST"])
@require_role("admin")
def console_users_set_role():
    d = request.json or {}
    username = (d.get("username") or "").strip()
    role     = d.get("role") or ""
    if role not in ROLES: return jsonify({"error":"Invalid role"}), 400
    if username == g.user["username"] and role != "admin":
        return jsonify({"error":"Cannot demote yourself"}), 400
    cur = db().execute("UPDATE users SET role=? WHERE username=?", (role, username))
    db().commit()
    if cur.rowcount == 0: return jsonify({"error":"User not found"}), 404
    # Phase 2 (decision A): role is denormalized into the Redis session
    # hash, so a role change must revoke the user's sessions — otherwise
    # the old role stays in effect until they expire. Forcing a re-login
    # is also the tighter security behavior.
    urow = db().execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    revoked = 0
    if urow:
        try: revoked = session_store.del_user_tokens(urow["id"])
        except Exception as e: logger.warning(f"session revoke failed for {username}: {e}")
    try: _refresh_sessions_log()
    except Exception: pass
    audit("UserSetRole", "users", f"{username} → {role} · {revoked} session(s) revoked")
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════════════════════════
# CONNECTION REGISTRY  (Step 4)  — admin curates, users set their own creds
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/connections", methods=["GET"])
def connections_list():
    """Lists registry connections. Each row says whether *this* user has set creds.
    Splits the query across global (connections) and per-user (credentials)."""
    rows = db_global().execute("""
        SELECT c.id, c.name, c.host, c.port, c.cluster_name, c.notes, c.created_at
        FROM connections c ORDER BY c.name""").fetchall()
    # Per-user: which connection_ids has this user set creds for?
    dbu = db_user(g.user["username"])
    cred_ids = set()
    if dbu:
        cred_ids = {r["connection_id"] for r in
                    dbu.execute(
                        "SELECT connection_id FROM user_credentials WHERE user_id=?",
                        (dbu.user_id,)).fetchall()}
    out = []
    for r in rows:
        d = dict(r)
        d["has_creds"] = 1 if d["id"] in cred_ids else None
        out.append(d)
    return jsonify({"connections": out, "vault_available": MASTER_KEY is not None})

@app.route("/api/connections", methods=["POST"])
@require_role("admin")
def connections_create():
    d = request.json or {}
    name = (d.get("name") or "").strip()
    host = (d.get("host") or "").strip()
    port = int(d.get("port") or 8123)
    cluster_name = (d.get("cluster_name") or "").strip()
    notes = (d.get("notes") or "").strip()
    if not name or not host: return jsonify({"error": "name and host required"}), 400
    try:
        db().execute("INSERT INTO connections(name,host,port,cluster_name,notes,created_by) VALUES(?,?,?,?,?,?)",
                     (name, host, port, cluster_name, notes, g.user["id"]))
        db().commit()
        audit("ConnectionAdd", "connections", f"{name} → {host}:{port}")
        return jsonify({"ok": True})
    except IntegrityError:
        try: db().rollback()
        except Exception: pass
        return jsonify({"error": "connection name already exists"}), 409

@app.route("/api/connections/<int:cid>", methods=["PATCH"])
@require_role("admin")
def connections_update(cid):
    d = request.json or {}
    fields = []; vals = []
    for k in ("name","host","cluster_name","notes"):
        if k in d: fields.append(f"{k}=?"); vals.append((d.get(k) or "").strip())
    if "port" in d: fields.append("port=?"); vals.append(int(d["port"] or 8123))
    if not fields: return jsonify({"error":"nothing to update"}), 400
    vals.append(cid)
    cur = db().execute(f"UPDATE connections SET {','.join(fields)} WHERE id=?", vals)
    db().commit()
    if not cur.rowcount: return jsonify({"error":"connection not found"}), 404
    audit("ConnectionEdit", "connections", f"id={cid}")
    return jsonify({"ok": True})

@app.route("/api/connections/<int:cid>", methods=["DELETE"])
@require_role("admin")
def connections_delete(cid):
    row = db().execute("SELECT name FROM connections WHERE id=?", (cid,)).fetchone()
    if not row: return jsonify({"error":"connection not found"}), 404
    db().execute("DELETE FROM connections WHERE id=?", (cid,))
    db().commit()
    audit("ConnectionDelete", "connections", f"{row['name']} (id={cid})")
    return jsonify({"ok": True})

@app.route("/api/connections/<int:cid>/credentials", methods=["POST"])
def credentials_set(cid):
    """The current user sets their own ClickHouse credentials for this connection."""
    if not MASTER_KEY: return jsonify({"error":"credential vault disabled — install 'cryptography' and set MASTER_KEY"}), 503
    d = request.json or {}
    ch_user = (d.get("ch_username") or "").strip()
    ch_pw   = d.get("ch_password") or ""
    if not ch_user: return jsonify({"error":"ch_username required"}), 400
    if not db().execute("SELECT 1 FROM connections WHERE id=?", (cid,)).fetchone():
        return jsonify({"error":"connection not found"}), 404
    enc = fernet_encrypt(ch_pw)
    dbu = db_user(g.user["username"])
    if not dbu:
        return jsonify({"error": "user lookup failed"}), 500
    dbu.execute("""INSERT INTO user_credentials(user_id,connection_id,ch_username,ch_password_enc,updated_at)
                   VALUES(?,?,?,?,now())
                   ON CONFLICT(user_id,connection_id) DO UPDATE
                   SET ch_username=excluded.ch_username,
                       ch_password_enc=excluded.ch_password_enc,
                       updated_at=now()""",
                (dbu.user_id, cid, ch_user, enc))
    dbu.commit()
    audit("CredentialSet", "connections", f"connection_id={cid} ch_user={ch_user}")
    return jsonify({"ok": True})

@app.route("/api/connections/<int:cid>/credentials", methods=["DELETE"])
def credentials_clear(cid):
    dbu = db_user(g.user["username"])
    if not dbu:
        return jsonify({"error": "user lookup failed"}), 500
    dbu.execute("DELETE FROM user_credentials WHERE user_id=? AND connection_id=?",
                (dbu.user_id, cid))
    dbu.commit()
    audit("CredentialClear", "connections", f"connection_id={cid}")
    return jsonify({"ok": True})

@app.route("/api/connections/<int:cid>/test", methods=["POST"])
def connections_test_registry(cid):
    """Test a registry connection using the current user's stored credentials."""
    try:
        cl = _get_client({"connection_id": cid})
        ver = cl.server_version
        try: cl.close()
        except: pass
        return jsonify({"ok": True, "version": ver})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ── Console User System ───────────────────────────────────────────────────────
# Activity log is monthly-rotating, split into a global file and per-user files.
# logs/global/activity-YYYY-MM.log     ← every event from every user (admin view)
# logs/users/<user>/activity-YYYY-MM.log ← only this user's events (forensic)
# Both rotate independently; prior months are gzipped, nothing is deleted.

_act_current_month = _month_tag()

def _activity_log_writer_open_global():
    """Open the current-month global activity log, rotating if a month boundary
    was crossed since the last write."""
    global _act_current_month
    tag = _month_tag()
    if tag != _act_current_month:
        _archive_prior_months_in(LOG_GLOBAL_DIR, "activity")
        _archive_all_user_logs()
        _act_current_month = tag
    return _global_activity_log_path()

# Back-compat shim — anywhere the old code referenced ACTIVITY_LOG, it now
# resolves to the current month's global file at call time.
class _ActivityLogPath:
    def __fspath__(self):  return str(_activity_log_writer_open_global())
    def __str__(self):     return str(_activity_log_writer_open_global())
    def exists(self):      return _activity_log_writer_open_global().exists()
    def __truediv__(self, x): return _activity_log_writer_open_global() / x
ACTIVITY_LOG = _ActivityLogPath()

def _format_audit_block(ts, console_user, console_role, conn_host, conn_user, ip, panel, action, detail):
    user_str = f"{console_user} ({console_role})" if console_user else "anonymous"
    target   = f" @ {conn_host}" + (f"/{conn_user}" if conn_user else "")
    detail_indented = "\n".join("    " + line for line in (detail or "").splitlines()) or "    (none)"
    return (
        "============================================================\n"
        f"[{ts}] {user_str}{target}  · ip={ip}\n"
        f"Panel:  {panel}\n"
        f"Action: {action}\n"
        f"Detail:\n{detail_indented}\n"
        "\n"
    )

@app.route("/api/activity/log", methods=["POST"])
def activity_log_write():
    """Receive a UI-side audit event and route it through audit(). audit()
    handles all four writes: global DB, per-user DB, global text log, per-user
    text log. The text-log block format is generated inside audit() so the
    on-disk record is identical regardless of whether the event originated
    from the UI or from a server-side path (login, logout, connect, etc.)."""
    d = request.json or {}
    try:
        audit(
            action    = d.get("action", ""),
            panel     = d.get("panel", ""),
            detail    = d.get("detail", ""),
            conn_host = d.get("conn_host", ""),
            conn_user = d.get("conn_user", ""),
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/activity/read", methods=["GET"])
def activity_log_read():
    """Return recent UI activity events. Reads from global.db.audit_events
    (the master copy from the per-user dual-write). The legacy console-activity.log
    text file still receives every event but is no longer the source for the UI;
    using the DB lets admins filter across users and gives consistent output even
    after log rotation. Optional query params: user, action, panel, lines.
    """
    try:
        lines_n = int(request.args.get("lines", 1000))
        if lines_n < 1: lines_n = 50
        if lines_n > 1000: lines_n = 1000   # hard cap: UI never displays more than the last 1000
        uf  = (request.args.get("user")   or "").strip()
        af  = (request.args.get("action") or "").strip()
        pf  = (request.args.get("panel")  or "").strip()
        # Build query
        where, params = [], []
        if uf: where.append("username LIKE ?");      params.append(f"%{uf}%")
        if af: where.append("action LIKE ?");        params.append(f"%{af}%")
        if pf: where.append("panel LIKE ?");         params.append(f"%{pf}%")
        sql = ("SELECT ts, action, panel, detail, username AS console_user, "
               "role AS console_role, conn_user, conn_host, conn_port, ip, result "
               "FROM audit_events")
        if where: sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(lines_n)
        rows = db_global().execute(sql, params).fetchall()
        entries = []
        for r in rows:
            d = dict(r)
            # Keep the original wire shape so the UI doesn't need changes
            entries.append({
                "ts": d["ts"], "action": d["action"], "panel": d["panel"] or "",
                "detail": d["detail"] or "",
                "console_user": d["console_user"] or "",
                "console_role": d["console_role"] or "",
                "conn_user": d["conn_user"] or "",
                "conn_host": d["conn_host"] or "",
                "result": d["result"] or "ok",
            })
        # Reverse to chronological order at the end so the most-recent stays on top
        return jsonify({"entries": entries, "path": "global.db.audit_events",
                       "exists": True, "total": len(entries)})
    except Exception as e:
        return jsonify({"error": str(e), "entries": []})

@app.route("/api/activity/clear", methods=["POST"])
def activity_log_clear():
    """Disabled by design. The activity log is append-only and retained for the
    lifetime of the install. This endpoint exists only to give a clear error to
    any client (UI or otherwise) that still attempts to wipe it.
    """
    audit("ActivityLogClearAttempt", "activitylog",
          "Clear request denied (append-only retention policy)",
          result="denied")
    return jsonify({
        "ok": False,
        "error": "Activity log is append-only and cannot be cleared. Retention required for audit and forensic purposes."
    }), 403

if __name__=="__main__":
    # ─── CLI commands ────────────────────────────────────────────────
    # Usage:
    #   python app.py create-user <username> [--role admin|developer|readonly] [--email ...]
    #   python app.py reset-password <username>
    #   python app.py list-users
    #   python app.py set-role <username> <role>
    if len(sys.argv) >= 2 and sys.argv[1] in ("create-user","reset-password","list-users","set-role","delete-user","export-audit","list-logs","read-log","migrate-logs"):
        import getpass, argparse
        cmd = sys.argv[1]
        ap = argparse.ArgumentParser(prog=f"{Path(__file__).name} {cmd}")
        if cmd == "create-user":
            ap.add_argument("username")
            ap.add_argument("--role", default="readonly", choices=ROLES)
            ap.add_argument("--email", default="")
            ap.add_argument("--password", help="if omitted, prompt securely")
            args = ap.parse_args(sys.argv[2:])
            pw = args.password or getpass.getpass(f"Password for {args.username}: ")
            if not pw or len(pw) < 6:
                print("Password must be at least 6 characters."); sys.exit(2)
            # Basic username sanity — keep it filesystem-safe since it becomes a folder
            import re as _re
            if not _re.match(r"^[A-Za-z0-9._-]{1,32}$", args.username):
                print("Username must be 1-32 chars, [A-Za-z0-9._-] only (folder-safe).")
                sys.exit(2)
            init_db()
            conn = _dbmod.get_connection()
            try:
                conn.execute("INSERT INTO users(username,email,password_hash,role) VALUES(?,?,?,?)",
                             (args.username, args.email, hash_password(pw), args.role))
                conn.commit()
                # Per-user log directory (no longer a DB file — v4 stores per-user
                # data in Postgres with user_id columns).
                _init_user_db(args.username)
                print(f"✓ Created user '{args.username}' with role '{args.role}'.")
                print(f"  → per-user audit log dir at logs/users/{args.username}/")
            except IntegrityError:
                conn.rollback()
                print(f"✗ User '{args.username}' already exists.")
                sys.exit(2)
            conn.close(); sys.exit(0)
        elif cmd == "reset-password":
            ap.add_argument("username"); ap.add_argument("--password")
            args = ap.parse_args(sys.argv[2:])
            pw = args.password or getpass.getpass(f"New password for {args.username}: ")
            if not pw or len(pw) < 6:
                print("Password must be at least 6 characters."); sys.exit(2)
            init_db()
            conn = _dbmod.get_connection()
            cur = conn.execute("UPDATE users SET password_hash=? WHERE username=?",
                               (hash_password(pw), args.username))
            urow = conn.execute("SELECT id FROM users WHERE username=?",
                                (args.username,)).fetchone()
            conn.commit()
            # Phase 2: sessions live in Redis, not Postgres.
            if urow:
                try: session_store.del_user_tokens(urow["id"])
                except Exception as e: print(f"  (warning: Redis session revoke failed: {e})")
            print(f"✓ Password reset, sessions invalidated." if cur.rowcount else f"✗ User not found.")
            conn.close(); sys.exit(0 if cur.rowcount else 2)
        elif cmd == "list-users":
            init_db()
            conn = _dbmod.get_connection()
            print(f"{'ID':<4} {'USERNAME':<24} {'ROLE':<11} {'ACTIVE':<7} {'LAST LOGIN':<20} {'CREATED':<20}")
            print("-"*90)
            for r in conn.execute("SELECT id,username,role,is_active,"
                                  "COALESCE(to_char(last_login_at,'YYYY-MM-DD HH24:MI:SS'),'') AS last_login,"
                                  "COALESCE(to_char(created_at,'YYYY-MM-DD HH24:MI:SS'),'') AS created "
                                  "FROM users ORDER BY id"):
                print(f"{r[0]:<4} {r[1]:<24} {r[2]:<11} {('yes' if r[3] else 'no'):<7} {r[4]:<20} {r[5]:<20}")
            conn.close(); sys.exit(0)
        elif cmd == "set-role":
            ap.add_argument("username"); ap.add_argument("role", choices=ROLES)
            args = ap.parse_args(sys.argv[2:])
            init_db()
            conn = _dbmod.get_connection()
            cur = conn.execute("UPDATE users SET role=? WHERE username=?", (args.role, args.username))
            conn.commit()
            print(f"✓ Role updated." if cur.rowcount else f"✗ User not found.")
            conn.close(); sys.exit(0 if cur.rowcount else 2)
        elif cmd == "delete-user":
            ap.add_argument("username")
            args = ap.parse_args(sys.argv[2:])
            init_db()
            conn = _dbmod.get_connection()
            # Soft-delete, consistent with the web UI: deactivate the account
            # (is_active=0) rather than removing the row. The username, role,
            # audit history and per-user logs are all kept; reactivate later
            # with: UPDATE users SET is_active=1 WHERE username='...'
            cur = conn.execute("UPDATE users SET is_active=0 WHERE username=?", (args.username,))
            conn.commit()
            print(f"✓ User deactivated (soft-delete)." if cur.rowcount else f"✗ User not found.")
            conn.close(); sys.exit(0 if cur.rowcount else 2)

        elif cmd == "export-audit":
            # Usage:
            #   python app.py export-audit --month 2026-05 --format csv --out audit.csv
            #   python app.py export-audit --user cansayin --format json
            #   python app.py export-audit --from 2026-05-01 --to 2026-05-31 --format tsv
            # Reads the master audit_events table and exports a clean,
            # consumable format. Multi-line details are escaped (CSV quoting
            # for csv/tsv; native strings for json).
            ap.add_argument("--month",  help="Filter by month YYYY-MM (alternative to --from/--to)")
            ap.add_argument("--from",   dest="from_ts", help="Inclusive start, YYYY-MM-DD or YYYY-MM-DD HH:MM:SS")
            ap.add_argument("--to",     dest="to_ts",   help="Inclusive end, YYYY-MM-DD or YYYY-MM-DD HH:MM:SS")
            ap.add_argument("--user",   help="Filter by console username (substring)")
            ap.add_argument("--action", help="Filter by action name (substring)")
            ap.add_argument("--panel",  help="Filter by panel (substring)")
            ap.add_argument("--format", choices=("csv","tsv","json","pretty"), default="csv")
            ap.add_argument("--out",    help="Output file (default: stdout)")
            args = ap.parse_args(sys.argv[2:])
            init_db()
            where, params = [], []
            if args.month:
                # In v4 (Postgres) ts is TIMESTAMPTZ — LIKE on a timestamp would
                # raise a type error. to_char emits 'YYYY-MM' which we compare
                # directly to the --month argument (already 'YYYY-MM' shape).
                where.append("to_char(ts, 'YYYY-MM') = ?"); params.append(args.month)
            if args.from_ts:
                where.append("ts >= ?"); params.append(args.from_ts)
            if args.to_ts:
                where.append("ts <= ?"); params.append(args.to_ts + " 23:59:59" if len(args.to_ts)==10 else args.to_ts)
            if args.user:
                where.append("username LIKE ?"); params.append(f"%{args.user}%")
            if args.action:
                where.append("action LIKE ?"); params.append(f"%{args.action}%")
            if args.panel:
                where.append("panel LIKE ?"); params.append(f"%{args.panel}%")
            sql = ("SELECT ts, username, role, panel, action, detail, "
                   "conn_host, conn_port, conn_user, ip, result "
                   "FROM audit_events")
            if where: sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY id ASC"
            conn = _dbmod.get_connection()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()

            out_stream = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout

            if args.format == "csv":
                import csv as _csv
                w = _csv.writer(out_stream)
                w.writerow(["ts","username","role","panel","action","detail","conn_host","conn_port","conn_user","ip","result"])
                for r in rows: w.writerow([r[k] for k in r.keys()])
            elif args.format == "tsv":
                # Tab-separated; replace embedded tabs/newlines in detail with literal escape sequences
                out_stream.write("\t".join(["ts","username","role","panel","action","detail","conn_host","conn_port","conn_user","ip","result"]) + "\n")
                for r in rows:
                    cols = []
                    for k in r.keys():
                        v = r[k] or ""
                        v = str(v).replace("\\","\\\\").replace("\t","\\t").replace("\n","\\n")
                        cols.append(v)
                    out_stream.write("\t".join(cols) + "\n")
            elif args.format == "json":
                import json as _json
                out_stream.write("[\n")
                for i, r in enumerate(rows):
                    out_stream.write("  " + _json.dumps({k: r[k] for k in r.keys()}, ensure_ascii=False))
                    out_stream.write(",\n" if i < len(rows)-1 else "\n")
                out_stream.write("]\n")
            elif args.format == "pretty":
                # Same block format as the on-disk text log
                for r in rows:
                    user_str = f"{r['username']} ({r['role']})" if r['username'] else "anonymous"
                    target   = f" @ {r['conn_host']}" + (f"/{r['conn_user']}" if r['conn_user'] else "")
                    detail = (r['detail'] or '')
                    detail_ind = "\n".join("    " + ln for ln in detail.splitlines()) or "    (none)"
                    out_stream.write(
                        "============================================================\n"
                        f"[{r['ts']}] {user_str}{target}  · ip={r['ip'] or ''}\n"
                        f"Panel:  {r['panel'] or ''}\n"
                        f"Action: {r['action']}\n"
                        f"Detail:\n{detail_ind}\n\n"
                    )
            if args.out:
                out_stream.close()
                print(f"✓ Exported {len(rows)} audit events to {args.out}")
            sys.exit(0)

        elif cmd == "list-logs":
            # Usage:
            #   python app.py list-logs                         all logs grouped
            #   python app.py list-logs --user cansayin         only this user
            #   python app.py list-logs --global                global only
            ap.add_argument("--user", help="Show only this user's per-user logs")
            ap.add_argument("--global", dest="globalonly", action="store_true",
                            help="Show only global logs (server + global activity)")
            args = ap.parse_args(sys.argv[2:])
            def _bucket(d, label):
                if not d.exists(): return
                files = sorted(d.iterdir())
                if not files: return
                print(f"\n{label}  ({d})")
                print("-" * (len(label) + len(str(d)) + 4))
                for p in files:
                    if p.is_file():
                        size = p.stat().st_size
                        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
                        suffix = " (gzipped)" if p.name.endswith(".gz") else ""
                        active = " ← ACTIVE" if p.name.endswith(f"-{_month_tag()}.log") else ""
                        print(f"  {p.name:<40s}  {size:>10d} B   {mtime}{suffix}{active}")
            if not args.user:
                _bucket(LOG_GLOBAL_DIR, "GLOBAL LOGS")
            if not args.globalonly:
                if args.user:
                    user_dir = LOG_USERS_DIR / args.user
                    if not user_dir.exists():
                        print(f"✗ No logs for user '{args.user}'"); sys.exit(2)
                    _bucket(user_dir, f"USER LOGS — {args.user}")
                else:
                    if LOG_USERS_DIR.exists():
                        for ud in sorted(LOG_USERS_DIR.iterdir()):
                            if ud.is_dir(): _bucket(ud, f"USER LOGS — {ud.name}")
            print()
            sys.exit(0)

        elif cmd == "read-log":
            # Usage:
            #   python app.py read-log --user cansayin --month 2026-05
            #   python app.py read-log --global --month 2026-04        (reads .gz transparently)
            #   python app.py read-log --user admin --grep "Run Query"
            # The on-disk log is JSON-lines (one event per line). This command
            # parses each line and renders it as a human-readable block.
            ap.add_argument("--user", help="Read this user's log (omit for global)")
            ap.add_argument("--global", dest="globalonly", action="store_true")
            ap.add_argument("--month", help="YYYY-MM (default: current month)")
            ap.add_argument("--grep",  help="Filter to events containing this substring (case-insensitive, matches any field)")
            ap.add_argument("--last",  type=int, default=0,
                            help="Show only the last N events (default: all)")
            ap.add_argument("--raw",   action="store_true",
                            help="Print raw JSON-lines instead of rendered blocks")
            args = ap.parse_args(sys.argv[2:])
            month = args.month or _month_tag()
            if args.user:
                base = LOG_USERS_DIR / args.user
                if not base.exists():
                    print(f"✗ No logs for user '{args.user}'"); sys.exit(2)
                fname_log    = base / f"activity-{month}.log"
                fname_gz     = base / f"activity-{month}.log.gz"
            else:
                base = LOG_GLOBAL_DIR
                fname_log    = base / f"activity-{month}.log"
                fname_gz     = base / f"activity-{month}.log.gz"
            data = None
            if fname_log.exists():
                with open(fname_log, "r", encoding="utf-8") as f: data = f.read()
            elif fname_gz.exists():
                with gzip.open(fname_gz, "rt", encoding="utf-8") as f: data = f.read()
            else:
                print(f"✗ No log for {month} (looked for {fname_log.name} and .gz)"); sys.exit(2)

            # Parse the file. Auto-detect format:
            # - New format: one JSON object per line
            # - Old format (pre-JSON-lines change): pretty block separated by
            #   60-char === rules. We parse those too so historical logs
            #   remain readable without forced migration.
            import json as _json
            events = []
            lines = data.splitlines()
            # Try JSON-lines first if at least one line looks like JSON
            json_candidates = [ln for ln in lines if ln.strip().startswith("{") and ln.strip().endswith("}")]
            looks_json = len(json_candidates) > 0 and len(json_candidates) >= len([ln for ln in lines if ln.strip()]) // 2
            if looks_json:
                for ln_no, raw in enumerate(lines, 1):
                    raw = raw.strip()
                    if not raw: continue
                    try:
                        events.append(_json.loads(raw))
                    except Exception:
                        # Skip non-JSON noise (e.g. a stray block from before migration)
                        continue
            else:
                # Old pretty-block format — parse into the same dict shape
                # used by JSON-lines so the renderer below is unchanged.
                blocks = [b for b in data.split("=" * 60) if b.strip()]
                for blk in blocks:
                    e = {"ts":"", "console_user":"", "console_role":"", "panel":"",
                         "action":"", "detail":"", "conn_host":"", "conn_user":"",
                         "ip":"", "result":""}
                    detail_lines = []
                    in_detail = False
                    for line in blk.splitlines():
                        if not line.strip() and not in_detail: continue
                        # Header line: [TS] user (role) @ host/chuser · ip=X
                        m = re.match(r"\s*\[([^\]]+)\]\s+(\S+)\s*\(([^)]*)\)\s*(?:@\s*([^/\s·]+)(?:/(\S+))?)?\s*·\s*ip=(\S*)", line)
                        if m:
                            e["ts"], e["console_user"], e["console_role"] = m.group(1), m.group(2), m.group(3)
                            e["conn_host"], e["conn_user"], e["ip"] = m.group(4) or "", m.group(5) or "", m.group(6) or ""
                            continue
                        if line.startswith("Panel:"):  e["panel"]  = line.split(":", 1)[1].strip(); continue
                        if line.startswith("Action:"): e["action"] = line.split(":", 1)[1].strip(); continue
                        if line.startswith("Detail:"): in_detail = True; continue
                        if in_detail:
                            # Strip the 4-space indent applied by the old writer
                            detail_lines.append(line[4:] if line.startswith("    ") else line)
                    e["detail"] = "\n".join(detail_lines).rstrip()
                    if e["action"] or e["ts"]:
                        events.append(e)

            if args.grep:
                needle = args.grep.lower()
                events = [e for e in events
                          if any(needle in str(v).lower() for v in e.values())]
            if args.last and args.last > 0:
                events = events[-args.last:]

            print(f"# {fname_log.name if fname_log.exists() else fname_gz.name}"
                  f" — {len(events)} event(s)" + (f" matching '{args.grep}'" if args.grep else ""))
            print()
            if args.raw:
                for e in events:
                    print(_json.dumps(e, ensure_ascii=False))
            else:
                # Render the same pretty block format the previous on-disk
                # layout used, but built from JSON for crisp typing.
                for e in events:
                    user_str = (f"{e.get('console_user','')} ({e.get('console_role','')})"
                                if e.get('console_user') else "anonymous")
                    target_host = e.get('conn_host','')
                    target_user = e.get('conn_user','')
                    target = f" @ {target_host}" + (f"/{target_user}" if target_user else "") if target_host else ""
                    detail = e.get('detail','') or ''
                    detail_ind = "\n".join("    " + ln for ln in detail.splitlines()) or "    (none)"
                    print("=" * 60)
                    print(f"[{e.get('ts','')}] {user_str}{target}  · ip={e.get('ip','')}")
                    print(f"Panel:  {e.get('panel','')}")
                    print(f"Action: {e.get('action','')}")
                    print(f"Detail:\n{detail_ind}")
                    print()
            sys.exit(0)

        elif cmd == "migrate-logs":
            # Convert any legacy pretty-block log files (logs/global/*.log and
            # logs/users/*/*.log) into the new JSON-lines format. Idempotent —
            # files already in JSON-lines are left untouched. Operates in
            # place by writing to a .new file then renaming, so an
            # interruption won't corrupt the original.
            ap.add_argument("--dry-run", action="store_true",
                            help="Show what would be converted without modifying files")
            args = ap.parse_args(sys.argv[2:])
            import json as _json
            from datetime import datetime as _datetime

            def _parse_pretty_to_events(text):
                events = []
                for blk in text.split("=" * 60):
                    blk = blk.strip()
                    if not blk: continue
                    e = {"ts":"", "console_user":"", "console_role":"", "panel":"",
                         "action":"", "detail":"", "conn_host":"", "conn_user":"",
                         "ip":"", "result":"ok"}
                    detail_lines = []
                    in_detail = False
                    for line in blk.splitlines():
                        if not line.strip() and not in_detail: continue
                        m = re.match(r"\s*\[([^\]]+)\]\s+(\S+)\s*\(([^)]*)\)\s*(?:@\s*([^/\s·]+)(?:/(\S+))?)?\s*·\s*ip=(\S*)", line)
                        if m:
                            e["ts"], e["console_user"], e["console_role"] = m.group(1), m.group(2), m.group(3)
                            e["conn_host"], e["conn_user"], e["ip"] = m.group(4) or "", m.group(5) or "", m.group(6) or ""
                            continue
                        if line.startswith("Panel:"):  e["panel"]  = line.split(":", 1)[1].strip(); continue
                        if line.startswith("Action:"): e["action"] = line.split(":", 1)[1].strip(); continue
                        if line.startswith("Detail:"): in_detail = True; continue
                        if in_detail:
                            detail_lines.append(line[4:] if line.startswith("    ") else line)
                    e["detail"] = "\n".join(detail_lines).rstrip()
                    if e["action"] or e["ts"]:
                        events.append(e)
                return events

            def _file_is_pretty(text):
                lines = [ln for ln in text.splitlines() if ln.strip()]
                if not lines: return False
                json_count = sum(1 for ln in lines if ln.strip().startswith("{") and ln.strip().endswith("}"))
                return json_count < len(lines) // 2  # majority NOT JSON → pretty

            def _migrate_file(path):
                try:
                    with open(path, "r", encoding="utf-8") as f: text = f.read()
                    if not _file_is_pretty(text):
                        return None  # already JSON-lines (or empty)
                    events = _parse_pretty_to_events(text)
                    if not events: return None
                    if args.dry_run:
                        return len(events)
                    tmp = path.with_suffix(path.suffix + ".new")
                    with open(tmp, "w", encoding="utf-8") as f:
                        for e in events:
                            f.write(_json.dumps(e, ensure_ascii=False) + "\n")
                    # Atomic replace
                    tmp.replace(path)
                    return len(events)
                except Exception as e:
                    print(f"  ✗ {path}: {e}", file=sys.stderr)
                    return None

            converted = 0
            skipped   = 0
            # Walk global + every user dir
            targets = []
            if LOG_GLOBAL_DIR.exists():
                for p in LOG_GLOBAL_DIR.glob("activity-*.log"):
                    targets.append(p)
            if LOG_USERS_DIR.exists():
                for ud in LOG_USERS_DIR.iterdir():
                    if ud.is_dir():
                        for p in ud.glob("activity-*.log"):
                            targets.append(p)
            print(f"Found {len(targets)} activity .log file(s) under logs/")
            print(f"{'[DRY-RUN] ' if args.dry_run else ''}Converting legacy pretty-block files to JSON-lines...\n")
            for p in targets:
                result = _migrate_file(p)
                if result is None:
                    print(f"  · {p}  (already JSON-lines or empty — skipped)")
                    skipped += 1
                else:
                    verb = "would convert" if args.dry_run else "converted"
                    print(f"  ✓ {p}  ({result} events {verb})")
                    converted += 1
            print(f"\nDone. {converted} file(s) {'would be ' if args.dry_run else ''}converted, {skipped} skipped.")
            sys.exit(0)

    # ─── Server startup ──────────────────────────────────────────────
    # v4: DB is Postgres. Render a sanitized DSN (no password) so operators
    # can verify connectivity at a glance without leaking credentials.
    _db_host = os.environ.get("DB_HOST", "127.0.0.1")
    _db_port = os.environ.get("DB_PORT", "5432")
    _db_name = os.environ.get("DB_NAME", "?")
    _db_user_disp = os.environ.get("DB_USER", "?")
    _db_summary = f"postgres://{_db_user_disp}@{_db_host}:{_db_port}/{_db_name}"
    logger.info("="*50)
    logger.info("BlancoByte ClickHouse Console v4.0 starting")
    logger.info(f"Log:     {LOG_DIR} (monthly rotation, prior months gzipped)")
    logger.info(f"DB:      {_db_summary}")
    logger.info(f"License: {LICENSE['mode']} ({LICENSE['customer'] or '-'})")
    logger.info(f"SSO:     {'enabled (oidc)' if oidc_enabled() else 'disabled'}")
    logger.info(f"Vault:   {'available' if MASTER_KEY else 'disabled'}")
    logger.info("="*50)
    print("="*55)
    print("  BlancoByte ClickHouse Console v4.0 (multi-user)")
    print("  http://localhost:5000")
    print(f"  DB:      {_db_summary}")
    print(f"  License: {LICENSE['mode']}")
    print(f"  SSO:     {'on' if oidc_enabled() else 'off'}    Vault: {'on' if MASTER_KEY else 'off'}")
    print("="*55)
    app.run(host="0.0.0.0",port=5000,debug=False,threaded=True)
