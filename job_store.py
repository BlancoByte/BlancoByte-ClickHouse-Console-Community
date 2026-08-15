"""
job_store.py — Redis-backed async job storage (Phase 4 bug-fix).

Why this module exists
----------------------
Two features run work asynchronously: the SQL query editor (Run / Run All)
and the generic script-runner behind Backups, PITR, Branching and the
Profiler. In each case a request submits work, receives a short job id, and
the browser then polls a *second* endpoint until the job finishes.

Until now the job state lived in two ordinary Python dicts inside app.py
(`_jobs` and `_query_jobs`). That was correct only as long as the whole app
was a single process. Phase 3 switched the deployment to gunicorn with
several worker processes — and each worker has its OWN copy of those dicts.
A query submitted to worker A is therefore invisible to worker B, and
because gunicorn spreads requests across workers, the poll call lands on a
worker that has never heard of the job a large fraction of the time. The
user sees the result come back as "not found".

This module fixes that by moving job state into Redis, which every gunicorn
worker already shares (the same Redis used for sessions). It reuses
session_store's Redis client, so there is ONE connection pool for the whole
app and ONE set of REDIS_* environment variables to configure.

Data model
----------
  qjob:<jid>          Redis STRING (JSON)   — query-editor jobs
                      Faithfully mirrors the old _query_jobs[jid] dict; the
                      background thread replaces the whole object a couple
                      of times, exactly as the in-memory version did.
                      TTL: 1 hour.

  job:<jid>           Redis HASH            — generic script-runner jobs
                      fields: status, error, pid
  job:<jid>:lines     Redis LIST            — one JSON object per line,
                      appended with RPUSH. RPUSH is atomic, so the streaming
                      subprocess thread and a concurrent cancel request can
                      never clobber each other (a whole-object read-modify-
                      write would have that race).
                      TTL: 6 hours (backups / PITR can run long).

Every key carries a TTL so finished jobs do not accumulate in Redis forever.

Failure behaviour
-----------------
These functions let redis.RedisError propagate. Callers in app.py wrap the
poll/submit endpoints and turn a Redis outage into a clear HTTP 503 with a
real message — never the confusing "not found". In practice a Redis outage
also fails the session gate closed (see session_store), so an unauthenticated
caller cannot reach these endpoints at all.
"""
import json
import os

import session_store

# ── key prefixes / TTLs ─────────────────────────────────────────────────
_QJOB_PREFIX = "qjob:"
_JOB_PREFIX = "job:"
_QJOB_TTL = 3600          # 1 hour  — query results need not outlive that
_JOB_TTL = 6 * 3600       # 6 hours — backups / PITR can run long


def _c():
    """Shared Redis client — the same connection pool session_store uses."""
    return session_store.get_client()


# ════════════════════════════════════════════════════════════════════════
# Query-editor jobs   (replaces app.py `_query_jobs`)
# ════════════════════════════════════════════════════════════════════════
def qjob_create(jid: str) -> None:
    """Register a new query job in the 'running' state."""
    qjob_set(jid, {"status": "running", "result": None, "error": None})


def qjob_set(jid: str, obj: dict) -> None:
    """Replace the whole job object (mirrors `_query_jobs[jid] = {...}`)."""
    _c().set(_QJOB_PREFIX + jid, json.dumps(obj), ex=_QJOB_TTL)


def qjob_get(jid: str):
    """Return the job object as a dict, or None if unknown / expired."""
    raw = _c().get(_QJOB_PREFIX + jid)
    return json.loads(raw) if raw else None


def qjob_status(jid: str):
    """Return just the status string, or None if the job is unknown."""
    j = qjob_get(jid)
    return j.get("status") if j else None


def qjob_mark_cancelled(jid: str) -> bool:
    """Flip an existing job to 'cancelled'. Returns False if unknown.

    Mirrors the old `_query_jobs[jid]['status'] = 'cancelled'` mutation; the
    background thread checks qjob_status() before writing its result, so a
    cancel that arrives while the query is still running takes effect.
    """
    raw = _c().get(_QJOB_PREFIX + jid)
    if not raw:
        return False
    obj = json.loads(raw)
    obj["status"] = "cancelled"
    obj["error"] = "Cancelled"
    _c().set(_QJOB_PREFIX + jid, json.dumps(obj), ex=_QJOB_TTL)
    return True


# ════════════════════════════════════════════════════════════════════════
# Generic script-runner jobs   (replaces app.py `_jobs`)
# ════════════════════════════════════════════════════════════════════════
def job_create(jid: str) -> None:
    """Register a new generic job in the 'running' state (clean slate)."""
    c = _c()
    k = _JOB_PREFIX + jid
    pipe = c.pipeline()
    pipe.delete(k, k + ":lines")                       # reset if id reused
    pipe.hset(k, mapping={"status": "running", "error": "", "pid": ""})
    pipe.expire(k, _JOB_TTL)
    pipe.execute()


def job_append_line(jid: str, ltype: str, text: str) -> None:
    """Append one output line. RPUSH is atomic — the streaming subprocess
    thread and a concurrent cancel never corrupt the list."""
    c = _c()
    k = _JOB_PREFIX + jid
    if not c.exists(k):
        return
    pipe = c.pipeline()
    pipe.rpush(k + ":lines", json.dumps({"type": ltype, "text": str(text)}))
    pipe.expire(k + ":lines", _JOB_TTL)
    pipe.execute()


def job_set_status(jid: str, status: str, error=None) -> None:
    """Update status (and optionally error) of a generic job."""
    c = _c()
    k = _JOB_PREFIX + jid
    if not c.exists(k):
        return
    mapping = {"status": status}
    if error is not None:
        mapping["error"] = str(error)
    c.hset(k, mapping=mapping)
    c.expire(k, _JOB_TTL)


def job_set_pid(jid: str, pid) -> None:
    """Record the subprocess pid so any worker can cancel the job."""
    c = _c()
    k = _JOB_PREFIX + jid
    if c.exists(k):
        c.hset(k, "pid", str(pid))


def job_status(jid: str):
    """Return just the status string of a generic job, or None if unknown."""
    return _c().hget(_JOB_PREFIX + jid, "status")


def job_get(jid: str):
    """Reconstruct the full generic-job object in the shape the UI expects:
    {status, error, pid, lines: [{type, text}, ...]}  — or None if unknown."""
    c = _c()
    k = _JOB_PREFIX + jid
    meta = c.hgetall(k)
    if not meta:
        return None
    lines = [json.loads(x) for x in c.lrange(k + ":lines", 0, -1)]
    pid = meta.get("pid") or None
    try:
        pid = int(pid) if pid else None
    except (TypeError, ValueError):
        pid = None
    return {
        "status": meta.get("status", "running"),
        "error": meta.get("error") or None,
        "pid": pid,
        "lines": lines,
    }


# ════════════════════════════════════════════════════════════════════════
# Result snapshots — frozen, bounded, server-side pageable result sets
# ════════════════════════════════════════════════════════════════════════
# A snapshot is created by running a read query ONCE and storing up to
# SNAPSHOT_MAX_ROWS rows in a Redis LIST. The user then pages through that
# frozen set: each page is a single LRANGE (random access, O(page_size)), so
# navigation is cheap and consistent — every page comes from the one execution,
# unlike offset pagination which re-runs the query per page. Both keys carry a
# TTL so an abandoned browse session evicts itself; no background sweeper and
# no per-worker state (any worker serves any page from Redis).
_SNAP_PREFIX = "qsnap:"
_SNAP_TTL = int(os.environ.get("QUERY_SNAPSHOT_TTL_SEC", "600"))   # 10 minutes


def _snap_meta_key(sid: str) -> str:
    return _SNAP_PREFIX + sid + ":meta"


def _snap_rows_key(sid: str) -> str:
    return _SNAP_PREFIX + sid + ":rows"


def snapshot_init(sid: str, meta: dict) -> None:
    """Create the metadata key (columns, total, capped, query_id, stats)."""
    _c().set(_snap_meta_key(sid), json.dumps(meta), ex=_SNAP_TTL)


def snapshot_push(sid: str, rows: list) -> None:
    """Append a batch of rows (list of row-lists) to the snapshot's Redis LIST.
    Uses RPUSH with multiple values so a batch is one round trip."""
    if not rows:
        return
    vals = [json.dumps(r) for r in rows]
    k = _snap_rows_key(sid)
    c = _c()
    # RPUSH variadic — push the whole batch in one command.
    c.rpush(k, *vals)
    c.expire(k, _SNAP_TTL)


def snapshot_finalize(sid: str) -> None:
    """Refresh the TTL on both keys after all rows are pushed."""
    c = _c()
    c.expire(_snap_meta_key(sid), _SNAP_TTL)
    c.expire(_snap_rows_key(sid), _SNAP_TTL)


def snapshot_get_meta(sid: str):
    """Return the snapshot metadata dict, or None if expired / unknown."""
    v = _c().get(_snap_meta_key(sid))
    return json.loads(v) if v else None


def snapshot_get_page(sid: str, offset: int, size: int) -> list:
    """Return rows [offset, offset+size) from the snapshot via one LRANGE."""
    if size <= 0:
        return []
    raw = _c().lrange(_snap_rows_key(sid), offset, offset + size - 1)
    return [json.loads(x) for x in raw]


def snapshot_drop(sid: str) -> None:
    """Delete a snapshot (both keys). Safe to call on an already-expired id."""
    _c().delete(_snap_meta_key(sid), _snap_rows_key(sid))
