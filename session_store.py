"""
session_store.py — Redis-backed session storage (Phase 2).

Symmetric to db.py: db.py speaks Postgres, this module speaks Redis. It
holds ONLY the Redis primitives. Audit logging and the logs/global/
sessions.log snapshot stay in app.py — this module never touches them.

Replaces the Postgres `sessions` table. Session lifetime is enforced by
Redis key TTL (no cleanup job needed).

Data model
----------
  session:<token>          Redis HASH
      fields: user_id, username, role, email,
              created_at, expires_at, ip, user_agent
      TTL:    SESSION_TTL_DAYS * 86400  (set on write)

  user_sessions:<user_id>  Redis SORTED SET
      member = token, score = expires_at epoch seconds
      Secondary index so we can list/revoke all of a user's sessions.
      A session:<token> key vanishing on TTL does NOT auto-remove its
      ZSET entry, so every read prunes stale members first
      (ZREMRANGEBYSCORE ... 0 <now>). The index never grows unbounded.

Listing all sessions (admin panel, sessions.log) uses SCAN MATCH
session:* — no global index to keep in sync. Token-prefix revoke uses
SCAN MATCH session:<prefix>* since the token is in the key.

Configuration via environment
-----------------------------
  REDIS_HOST      (default: 192.168.105.4 — the Phase 2 Redis VM)
  REDIS_PORT      (default: 6379)
  REDIS_PASSWORD  (default: empty)
  REDIS_DB        (default: 0)

Public API
----------
  ping()                                  -> bool   connectivity check
  put_session(token, fields, ttl)         -> None   write hash + index
  get_session(token)                      -> dict | None
  del_session(token)                      -> None   remove hash + index
  list_user_tokens(user_id)               -> list[str]
  list_user_sessions(user_id)             -> list[dict]   (token included)
  del_user_tokens(user_id, except_token)  -> int    revoked count
  scan_sessions()                         -> list[dict]   all live sessions
  scan_by_prefix(prefix)                  -> list[dict]   prefix match
  count_sessions()                        -> int
"""
import os
import time
from typing import Optional

import redis


# ── Session hash field set ──────────────────────────────────────────────
_FIELDS = (
    "user_id", "username", "role", "email",
    "created_at", "expires_at", "ip", "user_agent",
)

_SESSION_PREFIX = "session:"
_USER_INDEX_PREFIX = "user_sessions:"


def _skey(token: str) -> str:
    return f"{_SESSION_PREFIX}{token}"


def _ukey(user_id) -> str:
    return f"{_USER_INDEX_PREFIX}{user_id}"


# ── Connection pool (lazy singleton) ────────────────────────────────────
_client: Optional[redis.Redis] = None


def _get_client() -> redis.Redis:
    """Return the shared Redis client (lazy init). decode_responses=True so
    every read comes back as str — matches how app.py treats session rows
    (string comparisons, JSON serialization)."""
    global _client
    if _client is not None:
        return _client
    host = os.environ.get("REDIS_HOST", "192.168.105.4")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD", "") or None
    db = int(os.environ.get("REDIS_DB", "0"))
    _client = redis.Redis(
        host=host,
        port=port,
        password=password,
        db=db,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
        health_check_interval=30,
    )
    return _client


def ping() -> bool:
    """Connectivity check. Raises redis.RedisError if unreachable."""
    return bool(_get_client().ping())


def get_client() -> redis.Redis:
    """Public accessor for the shared Redis client.

    job_store.py (Phase 4) uses this so the whole application talks to Redis
    through ONE connection pool and ONE set of REDIS_* environment variables,
    instead of each module configuring its own connection.
    """
    return _get_client()


# ── Per-user index pruning ──────────────────────────────────────────────
def _prune_user_index(user_id) -> None:
    """Drop expired tokens from user_sessions:<user_id>. Cheap, idempotent;
    called before every read of the index so it never grows unbounded."""
    try:
        _get_client().zremrangebyscore(_ukey(user_id), 0, int(time.time()))
    except redis.RedisError:
        # Best-effort housekeeping — never fail a caller over pruning.
        pass


# ── Writes ──────────────────────────────────────────────────────────────
def put_session(token: str, fields: dict, ttl_seconds: int) -> None:
    """Create/replace a session: write the hash, set its TTL, and register
    the token in the owner's sorted-set index scored by expiry epoch.

    `fields` must contain at least user_id; the full _FIELDS set is
    expected (app.py always supplies it).
    """
    c = _get_client()
    user_id = fields["user_id"]
    expires_epoch = int(time.time()) + int(ttl_seconds)
    mapping = {k: ("" if fields.get(k) is None else str(fields.get(k)))
               for k in _FIELDS}
    pipe = c.pipeline()
    pipe.delete(_skey(token))           # replace cleanly if token reused
    pipe.hset(_skey(token), mapping=mapping)
    pipe.expire(_skey(token), int(ttl_seconds))
    pipe.zadd(_ukey(user_id), {token: expires_epoch})
    pipe.expire(_ukey(user_id), int(ttl_seconds))  # index outlives last session
    pipe.execute()


# ── Reads ───────────────────────────────────────────────────────────────
def get_session(token: str) -> Optional[dict]:
    """Return the session hash as a dict, or None if the key is gone
    (never existed, deleted, or expired via TTL). Raises redis.RedisError
    on connectivity problems — the caller (app.py) decides policy
    (auth gate fails closed)."""
    if not token:
        return None
    data = _get_client().hgetall(_skey(token))
    if not data:
        return None
    data["token"] = token
    return data


def list_user_tokens(user_id) -> list:
    """All live tokens for a user (expired ones pruned first)."""
    _prune_user_index(user_id)
    return _get_client().zrange(_ukey(user_id), 0, -1)


def list_user_sessions(user_id) -> list:
    """All live sessions for a user as dicts (token field included).
    Tokens whose hash is already gone are skipped."""
    out = []
    for tok in list_user_tokens(user_id):
        s = get_session(tok)
        if s:
            out.append(s)
    return out


# ── Deletes ─────────────────────────────────────────────────────────────
def del_session(token: str) -> None:
    """Remove a session: delete the hash and drop it from the owner index.
    The owner is read from the hash before deletion so the index stays
    consistent."""
    if not token:
        return
    c = _get_client()
    user_id = c.hget(_skey(token), "user_id")
    pipe = c.pipeline()
    pipe.delete(_skey(token))
    if user_id is not None:
        pipe.zrem(_ukey(user_id), token)
    pipe.execute()


def del_user_tokens(user_id, except_token: Optional[str] = None) -> int:
    """Revoke all of a user's sessions, optionally keeping one (used by
    'change my password' which preserves the current session). Returns the
    number of sessions actually revoked."""
    tokens = list_user_tokens(user_id)
    victims = [t for t in tokens if t != except_token]
    if not victims:
        return 0
    c = _get_client()
    pipe = c.pipeline()
    for t in victims:
        pipe.delete(_skey(t))
        pipe.zrem(_ukey(user_id), t)
    pipe.execute()
    return len(victims)


# ── Enumeration (admin panel, sessions.log) ─────────────────────────────
def _scan_keys(match: str) -> list:
    """SCAN (non-blocking) for keys matching a pattern. COUNT hint keeps
    round-trips low; SCAN never blocks the server like KEYS would."""
    c = _get_client()
    found = []
    cursor = 0
    while True:
        cursor, batch = c.scan(cursor=cursor, match=match, count=200)
        found.extend(batch)
        if cursor == 0:
            break
    return found


def scan_sessions() -> list:
    """Every live session as a dict (token field included). Used by the
    admin Active Sessions panel and the sessions.log snapshot writer."""
    out = []
    for key in _scan_keys(f"{_SESSION_PREFIX}*"):
        token = key[len(_SESSION_PREFIX):]
        s = get_session(token)
        if s:
            out.append(s)
    return out


def scan_by_prefix(prefix: str) -> list:
    """Sessions whose token starts with `prefix` (admin revoke-by-prefix).
    The token is part of the key, so a MATCH glob does the filtering. The
    token alphabet is [A-Za-z0-9_-] — no glob metacharacters — so a direct
    match pattern is safe."""
    if not prefix:
        return []
    out = []
    for key in _scan_keys(f"{_SESSION_PREFIX}{prefix}*"):
        token = key[len(_SESSION_PREFIX):]
        s = get_session(token)
        if s:
            out.append(s)
    return out


def count_sessions() -> int:
    """Number of live sessions (admin system-info)."""
    return len(_scan_keys(f"{_SESSION_PREFIX}*"))
