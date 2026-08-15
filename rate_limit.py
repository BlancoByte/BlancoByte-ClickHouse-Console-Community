"""
rate_limit.py — Redis-backed login brute-force protection.

Counts FAILED login attempts per username and per source IP within a fixed
window. When a counter reaches its limit the principal is locked out for the
remainder of the window. A successful login clears the username counter, so a
legitimate user who mistypes a few times is never penalised once they get in.

Design notes
------------
* Two independent counters per attempt:
    - per username  → stops an attacker hammering one account.
    - per source IP → stops an attacker spraying many usernames from one host.
  The IP limit is higher than the username limit because several legitimate
  users can sit behind one NAT / office IP.

* Fail-OPEN. If Redis is unreachable the limiter allows the attempt. Login
  also needs Redis to create a session, so a Redis outage already blocks login
  by another path; we must not additionally hard-fail every login here, and we
  must never lock everyone out because the limiter's own store went down.

* The per-IP counter is NOT cleared on a successful login — otherwise an
  attacker who happens to own one valid account could reset the IP window at
  will. Only the username counter is cleared on success.

* The caller is responsible for passing the REAL client IP (e.g. from the
  trusted reverse proxy's X-Real-IP), not the proxy's own address — otherwise
  every user shares one counter and a single attacker could lock all logins.

Tunables (environment)
----------------------
  LOGIN_FAIL_WINDOW_SEC   window length in seconds          (default 900 = 15m)
  LOGIN_FAIL_MAX_USER     failures per username per window   (default 5)
  LOGIN_FAIL_MAX_IP       failures per source IP per window  (default 20)
"""
import os
import logging

import session_store

logger = logging.getLogger("clickhouse-console")

WINDOW   = int(os.environ.get("LOGIN_FAIL_WINDOW_SEC", "900"))
MAX_USER = int(os.environ.get("LOGIN_FAIL_MAX_USER", "5"))
MAX_IP   = int(os.environ.get("LOGIN_FAIL_MAX_IP", "20"))

_PREFIX = "loginfail:"


def _r():
    # Reuse the shared session Redis client; same store, same connection pool.
    return session_store._get_client()


def _key(scope, ident):
    return f"{_PREFIX}{scope}:{ident}"


def check(username, ip):
    """Read-only pre-check, called BEFORE verifying credentials.

    Returns (blocked: bool, retry_after_sec: int, scope: str). Does not mutate
    any counter. On any Redis error returns (False, 0, "") — fail open.
    """
    try:
        r = _r()
        for scope, ident, limit in (("user", (username or "").strip().lower(), MAX_USER),
                                     ("ip",   (ip or "").strip(),               MAX_IP)):
            if not ident:
                continue
            cnt = r.get(_key(scope, ident))
            if cnt is not None and int(cnt) >= limit:
                ttl = r.ttl(_key(scope, ident))
                return True, (ttl if ttl and ttl > 0 else WINDOW), scope
        return False, 0, ""
    except Exception as e:
        logger.warning(f"rate_limit.check skipped (redis): {e}")
        return False, 0, ""


def record_failure(username, ip):
    """Increment the username and IP failure counters. Sets the window TTL on
    the first failure of each window. Returns the new username count (for
    logging). Fail open on Redis error.
    """
    n = 0
    try:
        r = _r()
        for scope, ident in (("user", (username or "").strip().lower()),
                             ("ip",   (ip or "").strip())):
            if not ident:
                continue
            k = _key(scope, ident)
            c = r.incr(k)
            if c == 1:
                r.expire(k, WINDOW)
            if scope == "user":
                n = c
    except Exception as e:
        logger.warning(f"rate_limit.record_failure skipped (redis): {e}")
    return n


def clear(username):
    """Clear the username counter after a successful login. The IP counter is
    deliberately left intact. Fail open on Redis error.
    """
    try:
        ident = (username or "").strip().lower()
        if ident:
            _r().delete(_key("user", ident))
    except Exception as e:
        logger.warning(f"rate_limit.clear skipped (redis): {e}")
