# Security

This document records the application-level security controls in the
BlancoByte ClickHouse Console. It is kept alongside `harden.sql`
(database-level audit hardening) and section 13 ("Security notes") of
`INSTALLATION.md` (deployment hardening).

---

## SQL identifier / injection hardening (phase4z-ed)

### Background

Several endpoints receive object names from the browser — `database`,
`table`, `column`, `query_id`, `branch_name` — and historically interpolated
them straight into f-string SQL, e.g.:

```python
FROM {db}.{tbl}
WHERE database='{db}' AND table='{tbl}'
SHOW CREATE TABLE {db}.{tbl}
```

Because these names are placed both in **identifier position**
(`FROM {db}.{tbl}`) and in **value position** (`WHERE database='{db}'`), an
attacker-supplied name containing a quote or a backtick could break out of the
statement. This is an identifier-injection vector and is closed as described
below.

### Approach: validate once, at the read site

A validated identifier contains no quote, backtick, semicolon, whitespace or
other statement metacharacter. Validating each name **once, where it is read
from the request**, therefore makes every downstream interpolation safe in
*both* identifier and value position — without having to touch every individual
query. Invalid input raises `ValueError`, which the global error handler turns
into a clean `400`.

### Helpers (`app.py`)

| Helper | Purpose | Accepted charset |
| --- | --- | --- |
| `_safe_ident(name, kind, allow_empty=True)` | db / table / column names | `^[A-Za-z_][A-Za-z0-9_$]*$`, max 256 |
| `_qident(name, kind)` | validated **and backtick-quoted** identifier for `FROM` / DDL positions | same as above |
| `_safe_qid(qid, allow_empty=False)` | ClickHouse `query_id` (UUID-ish) | `^[A-Za-z0-9_.:+-]+$`, max 128 |
| `_safe_branch(name)` | branch-name suffix appended to a table name (`tbl_<branch>`) | `^[A-Za-z0-9_$]+$`, max 128 |
| `_safe_ident_star(name, kind)` | identifier **or** the GRANT wildcard `*` | identifier rules, or literal `*` |
| `_safe_partition(part, allow_empty=False)` | conservative partition-id guard (see note below) | `^[A-Za-z0-9_.-]+$`, max 256 |

Empty input is permitted for the optional cases (returns `""`); the endpoints'
existing required-field checks then produce their normal error. Non-empty input
that fails validation is rejected.

### What is now validated

* **`database`, `table`, `column`, `query_id`** — every read site, across all
  `d.get(...)` spelling variants (`d.get("table","")`, `d.get("table", "")`,
  `(d.get("database") or "").strip()`, `d.get("database","default")`, …) is
  wrapped in the appropriate helper. 37 read sites in total.
* **`branch_name`** — appended to a table identifier when creating a branch
  table (`tbl_<branch>`); validated with `_safe_branch` so it cannot escape the
  backtick-quoted name. This was a real escape vector and is closed.
* **GRANT `target_db` / `target_tbl`** — validated with `_safe_ident_star`,
  which additionally allows the `*` wildcard used in `GRANT … ON db.*`.

### What was already safe (left unchanged)

* **`partition_id`** is passed as a bound query parameter (`%(pid)s` with
  `parameters={...}`), e.g.
  `ALTER TABLE {dst} ATTACH PARTITION ID %(pid)s FROM {src}`. The
  `system.parts` lookups likewise bind `database`/`table` via
  `parameters={"d": db, "t": tbl}`. Bound parameters are not string-interpolated,
  so these were never injectable.
* **The `database` connection setting** read inside `_get_client` is handed to
  `clickhouse-connect` as a client setting, not interpolated into SQL, so it is
  not an injection vector.

### Error handling

The global `@app.errorhandler(Exception)` now distinguishes validation errors:

```python
if isinstance(e, ValueError):
    return jsonify({"error": str(e)}), 400   # bad client input
...
return jsonify({"error": str(e)}), 500       # server fault
```

So a rejected identifier returns a clear `400` with the offending value echoed
back (the attacker's own input — no server internals are leaked).

### Verification

The helpers were unit-tested in isolation against:

* legitimate names — `uk_price_paid`, `default`, `system`,
  `uk_price_paid_pitr_20260422`, `_tmp`, `col$1`, branch suffixes `v2` /
  `20260422` — all **accepted**;
* injection attempts — backtick break-out (`` x`; DROP TABLE y; -- ``),
  quote break-out (`' OR '1'='1`), statement chaining (`a;b`), whitespace,
  parentheses — all **rejected**.

### Known follow-up surfaces (not covered by this pass)

These use *different* request parameters and feature paths and should be
hardened in a dedicated follow-up with the same method:

* **Backup** — `db_name` / `tables` flow into the `BACKUP …` / `RESTORE …`
  clause builder.
* **User & role management** — `name` / `role` / `user` in `CREATE USER`,
  `CREATE ROLE`, and `GRANT` statements. (These have different semantics: console
  usernames can legitimately contain characters that are not bare SQL
  identifiers, so they need quoting rather than the strict identifier allowlist.)

---

## Related hardening (existing)

* **`harden.sql`** — revokes `DELETE` / `UPDATE` / `TRUNCATE` on the Postgres
  `audit_events` table from the application role, so a compromised app
  credential still cannot rewrite the audit trail.
* **Credential vault** — per-user ClickHouse credentials are stored encrypted
  (Fernet) and decrypted only in memory at connection time.
* **Sessions** — server-side sessions in Redis; the session gate fails closed on
  a Redis outage.
* **Deployment** — see `INSTALLATION.md` §13 (run behind nginx + gunicorn, TLS
  termination, restricted bind address, non-root service user).

---

## Reporting a vulnerability

Please report suspected security issues privately to the BlancoByte security
contact rather than opening a public issue. Include reproduction steps and the
affected version (see `CHANGELOG-v4.md`).
