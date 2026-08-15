-- schema.sql — ClickHouse Console, Postgres schema.
-- Applied idempotently at startup via db.apply_schema().
-- See SCHEMA_MAPPING.md for the SQLite → Postgres mapping rationale.

-- ── Users ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id              BIGSERIAL PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    email           TEXT,
    first_name      TEXT,
    last_name       TEXT,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at   TIMESTAMPTZ,
    sso_subject     TEXT,
    sso_provider    TEXT
);

-- Phase 4o: backfill first_name / last_name on installs that pre-date them.
-- ADD COLUMN IF NOT EXISTS makes this safe to re-run on every boot.
ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name  TEXT;
CREATE INDEX IF NOT EXISTS idx_users_sso ON users(sso_provider, sso_subject);

-- ── Sessions ────────────────────────────────────────────────────────────
-- Phase 2: sessions moved OUT of Postgres into Redis (see session_store.py).
-- The `sessions` table and its indexes are intentionally no longer created
-- here. On an existing install upgraded from Phase 1, drop the leftover
-- table manually once — it is no longer read or written:
--     DROP TABLE IF EXISTS sessions;
-- See INSTALLATION.md. No data migration: all users re-login after upgrade.

-- ── Connections (ClickHouse cluster endpoints registered in the app) ───
CREATE TABLE IF NOT EXISTS connections (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL,
    host          TEXT NOT NULL,
    port          INTEGER NOT NULL DEFAULT 8123,
    cluster_name  TEXT,
    notes         TEXT,
    created_by    BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Audit events (single master; per-user mirror dropped) ──────────────
CREATE TABLE IF NOT EXISTS audit_events (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id     BIGINT REFERENCES users(id) ON DELETE SET NULL,
    username    TEXT,
    role        TEXT,
    action      TEXT NOT NULL,
    panel       TEXT,
    detail      TEXT,
    conn_host   TEXT,
    conn_port   TEXT,
    conn_user   TEXT,
    ip          TEXT,
    result      TEXT NOT NULL DEFAULT 'ok',
    -- Tamper-evident hash chain (see 0002 migration). prev_hash links to the
    -- previous row's entry_hash; entry_hash = sha256(prev_hash + canonical row).
    prev_hash   TEXT,
    entry_hash  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_user_ts   ON audit_events(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action_ts ON audit_events(action, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_ts        ON audit_events(ts DESC);

-- ── Per-user data (formerly each in <username>.db; now user_id-scoped) ─

CREATE TABLE IF NOT EXISTS query_history (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    conn_host      TEXT,
    conn_port      TEXT,
    conn_user      TEXT,
    sql            TEXT NOT NULL,
    duration_ms    INTEGER,
    rows_returned  INTEGER,
    error          TEXT,
    job_id         TEXT
);
CREATE INDEX IF NOT EXISTS idx_qh_user_ts ON query_history(user_id, ts DESC);
-- Phase 4z-g: batch_id groups Run-All statements into a single logical
-- entry in the history dropdown. NULL for a regular single-statement run.
-- Added via ALTER ... IF NOT EXISTS so older installs migrate automatically.
ALTER TABLE query_history ADD COLUMN IF NOT EXISTS batch_id TEXT;

-- ─── SIEM forwarding ─────────────────────────────────────────────────────
-- Destinations: external systems (Splunk, Datadog, Elastic, Slack, generic
-- webhooks) that receive a JSON-formatted copy of each audit_events row
-- shortly after it's written. Forwarding is at-least-once: last_forwarded_id
-- only advances after a successful POST, so a destination that has been
-- offline catches up without dropping events when it comes back.
CREATE TABLE IF NOT EXISTS siem_destinations (
    id                    BIGSERIAL PRIMARY KEY,
    name                  TEXT NOT NULL,
    url                   TEXT NOT NULL,
    -- json | ecs | splunk_hec | slack
    format                TEXT NOT NULL DEFAULT 'json',
    -- A single HTTP header sent with each request, e.g.
    --   "Authorization: Bearer abc..."   (Datadog uses "DD-API-KEY: ...")
    --   "Authorization: Splunk <token>"
    -- Stored as a single line "Header-Name: value". NULL = no auth.
    auth_header           TEXT,
    enabled               BOOLEAN NOT NULL DEFAULT TRUE,
    -- Optional comma-separated allowlist of action names. NULL = forward all.
    filter_actions        TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_forwarded_id     BIGINT NOT NULL DEFAULT 0,
    last_attempt_at       TIMESTAMPTZ,
    last_status           TEXT,    -- 'ok' | 'error: ...'
    last_error            TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0
);
-- Per-destination forwarding attempt log. Keep recent N for the admin UI;
-- a startup-side trim keeps this table small (one INSERT per batch, not per
-- event, so volume is low).
CREATE TABLE IF NOT EXISTS siem_forward_log (
    id              BIGSERIAL PRIMARY KEY,
    destination_id  BIGINT NOT NULL REFERENCES siem_destinations(id) ON DELETE CASCADE,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL,     -- 'ok' | 'failed'
    http_status     INTEGER,
    batch_size      INTEGER,
    first_event_id  BIGINT,
    last_event_id   BIGINT,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_siem_log_dest_ts
    ON siem_forward_log(destination_id, ts DESC);

-- ─── LDAP / Active Directory ─────────────────────────────────────────────
-- Phase 4z-k: hybrid local + LDAP authentication. Each user record carries
-- an auth_source distinguishing where the user comes from and (for LDAP
-- users) the LDAP DN that authoritatively identifies them.
ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_source TEXT NOT NULL DEFAULT 'local';
ALTER TABLE users ADD COLUMN IF NOT EXISTS ldap_dn TEXT;
-- LDAP users carry no password hash — relax the legacy NOT NULL on the
-- column. Existing local users keep their hashes; new local users still
-- get one assigned at creation.
DO $$ BEGIN
    EXECUTE 'ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL';
EXCEPTION WHEN others THEN NULL;
END $$;

-- Singleton config row holding LDAP server connection parameters. We use
-- a singleton (id=1) rather than env vars so that an operator can change
-- the config through the admin UI without restarting the application.
CREATE TABLE IF NOT EXISTS ldap_config (
    id                  INTEGER PRIMARY KEY DEFAULT 1
                        CHECK (id = 1),         -- enforce single row
    enabled             BOOLEAN NOT NULL DEFAULT FALSE,
    server_url          TEXT,                   -- ldap://host:389 or ldaps://host:636
    use_starttls        BOOLEAN NOT NULL DEFAULT FALSE,
    bind_dn             TEXT,                   -- service account for searches
    bind_password       TEXT,                   -- fernet-encrypted at rest (master key); legacy rows may be plaintext until re-saved
    user_search_base    TEXT,                   -- ou=users,dc=...
    user_filter         TEXT NOT NULL DEFAULT '(uid={username})',
    group_search_base   TEXT,                   -- ou=groups,dc=...
    group_filter        TEXT NOT NULL DEFAULT '(member={user_dn})',
    default_role        TEXT NOT NULL DEFAULT 'readonly',
    nested_groups       BOOLEAN NOT NULL DEFAULT FALSE,  -- AD transitive (1.2.840.113556.1.4.1941)
    timeout_seconds     INTEGER NOT NULL DEFAULT 10,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- LDAP group → console role mapping. Multiple groups can map to the same
-- role. When a user is in zero mapped groups they receive the
-- ldap_config.default_role. When in multiple groups, the highest-privilege
-- role wins (admin > developer > monitoring > readonly).
CREATE TABLE IF NOT EXISTS ldap_group_mappings (
    id           BIGSERIAL PRIMARY KEY,
    group_name   TEXT NOT NULL,         -- the cn= value, not the full DN
    role         TEXT NOT NULL,         -- admin | developer | monitoring | readonly
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(group_name)
);

-- ─── Backup schedules ────────────────────────────────────────────────────
-- Phase 4z-n: cron-based backup schedules. Each row describes one
-- recurring backup job; a background thread polls this table once a
-- minute and fires due jobs. Schedules are independent — a typical
-- setup is one weekly 'full' schedule plus one daily 'differential'
-- schedule, both writing to the same storage path.
CREATE TABLE IF NOT EXISTS backup_schedules (
    id                BIGSERIAL PRIMARY KEY,
    name              TEXT NOT NULL,
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    cron              TEXT NOT NULL,         -- 5-field cron expression
    -- Target: 'database' | 'tables' | 'all'
    target            TEXT NOT NULL DEFAULT 'database',
    db_name           TEXT,
    tables            TEXT,                  -- space-separated db.table list
    -- Backup semantics: 'full' | 'differential' | 'incremental'
    --   full         : BACKUP TO File(...)  -- no base
    --   differential : BACKUP TO File(...) SETTINGS base_backup=<last full from this schedule's chain>
    --   incremental  : BACKUP TO File(...) SETTINGS base_backup=<last backup of any kind from this schedule>
    backup_type       TEXT NOT NULL DEFAULT 'full',
    -- Storage location
    storage_path      TEXT NOT NULL,
    -- Filename template; {db}, {type}, {date}, {time}, {datetime}, {ts}
    -- are substituted at run time. Default produces files like
    -- 'mydb_full_20260522_1430.zip'.
    name_template     TEXT NOT NULL DEFAULT '{db}_{type}_{datetime}.zip',
    -- For differential/incremental: which schedule's chain to walk
    -- back through to find the base backup. NULL = this schedule's own
    -- chain (default and recommended).
    base_chain_schedule_id  BIGINT REFERENCES backup_schedules(id) ON DELETE SET NULL,
    -- Connection profile to use (optional; NULL = system default)
    connection_id     BIGINT,
    -- Runtime state
    last_full_name    TEXT,    -- filename only, no path; base for next diff
    last_run_name     TEXT,    -- last produced filename (full/diff/incr)
    last_run_at       TIMESTAMPTZ,
    last_status       TEXT,    -- 'ok' | 'error: ...'
    last_error        TEXT,
    last_backup_id    TEXT,    -- ClickHouse system.backups id (UUID)
    next_run_at       TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_backup_schedules_due
    ON backup_schedules(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS query_favorites (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conn_label  TEXT NOT NULL,
    name        TEXT NOT NULL,
    sql         TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, conn_label, name)
);
CREATE INDEX IF NOT EXISTS idx_qfav_user_conn ON query_favorites(user_id, conn_label);

-- Phase 4s: query tab persistence so users resume where they left off after
-- logout/reconnect. One row per (user, host:port) holding the JSONB array of
-- tabs (id, name, sql) plus the id of the tab that was active. Transient
-- state (result, error, running, jobId) is never persisted.
-- Phase 4y: per-user dashboard persistence. Single row per user holds the
-- full list of boards (with their widgets) as JSONB so a re-login from any
-- browser restores the same dashboards. localStorage stays the in-browser
-- source of truth for speed; this table is the cross-browser / cross-device
-- backup.
CREATE TABLE IF NOT EXISTS user_dashboards (
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE PRIMARY KEY,
    boards_json JSONB NOT NULL,
    active_id   TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS query_tabs (
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conn_host   TEXT NOT NULL,
    conn_port   TEXT NOT NULL,
    tabs_json   JSONB NOT NULL,
    active_id   TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, conn_host, conn_port)
);

CREATE TABLE IF NOT EXISTS user_credentials (
    user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    connection_id    BIGINT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    ch_username      TEXT NOT NULL,
    ch_password_enc  TEXT NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, connection_id)
);

-- Phase 4z-v: per-user saved connections list. Replaces the
-- localStorage-backed savedConns / clusterList pair from earlier phases.
-- The user explicitly clicks "Save" after a successful connect to add
-- the entry; on next login the list is loaded back from this table so
-- the dropdown and Connections panel both restore.
--
-- Password is intentionally NOT stored here. For password persistence
-- across logins, users either:
--   1) re-type the password on connect (default — most private), or
--   2) use a connection from the admin-managed Connection Registry
--      (`connections` + `user_credentials`), which encrypts the password
--      at rest with the master key.
-- This table holds only the locator + display fields.
CREATE TABLE IF NOT EXISTS user_saved_connections (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    host        TEXT NOT NULL,
    port        INTEGER NOT NULL DEFAULT 8123,
    username    TEXT NOT NULL DEFAULT 'default',
    db          TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    last_used_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Uniqueness key: same user can't have two entries pointing at the
    -- same host:port:username triple. Saving the same target twice
    -- updates the name and bumps last_used_at instead.
    UNIQUE (user_id, host, port, username)
);

CREATE INDEX IF NOT EXISTS idx_user_saved_conns_user
    ON user_saved_connections(user_id, sort_order);

-- Phase 4z-aj: folder grouping for saved connections. Users can
-- group their saved entries under labels like "Production", "Test",
-- "Staging" so the sidebar can be organised by environment.
-- NULL / empty folder means "Ungrouped" — the UI renders those
-- entries in a default section.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_name = 'user_saved_connections'
       AND column_name = 'folder'
  ) THEN
    ALTER TABLE user_saved_connections ADD COLUMN folder TEXT;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_user_saved_conns_user_folder
    ON user_saved_connections(user_id, folder);

-- Phase 4z-at: per-user folder display settings. Lets the user pick a
-- colour for each folder (e.g. red for Production, green for Test) so
-- the Connections sidebar and Saved dropdown render the headers in
-- that colour. Per-user, like the saved-connections list itself —
-- two operators can pick different colours for the same folder name
-- and never see each other's choices.
CREATE TABLE IF NOT EXISTS user_folder_settings (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    folder       TEXT NOT NULL,
    color        TEXT,
    created_at   TIMESTAMPTZ DEFAULT now(),
    updated_at   TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, folder)
);
CREATE INDEX IF NOT EXISTS idx_user_folder_settings_user
    ON user_folder_settings(user_id);

-- ── Query annotations ────────────────────────────────────────────
-- A console user can attach text notes to a ClickHouse query_id —
-- collaborative postmortem context that survives across analyzer
-- opens. Multiple notes per query_id are allowed (thread-like).
-- A note cannot orphan: deleting a console user deletes their notes.
CREATE TABLE IF NOT EXISTS query_annotations (
    id          BIGSERIAL PRIMARY KEY,
    query_id    TEXT NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    username    TEXT NOT NULL,
    note        TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_query_annotations_qid
    ON query_annotations(query_id);
CREATE INDEX IF NOT EXISTS idx_query_annotations_ts
    ON query_annotations(ts);

-- ── Health score history ─────────────────────────────────────────
-- A row per Health Dashboard fetch — captures the composite score
-- and the five sub-scores so we can show a trend sparkline and
-- detect proactive degradation. Append-only; an external job can
-- prune very old rows if storage becomes a concern.
CREATE TABLE IF NOT EXISTS health_score_history (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    overall         INTEGER NOT NULL,
    band            TEXT NOT NULL,
    replication     INTEGER,
    mutations       INTEGER,
    disk            INTEGER,
    errors          INTEGER,
    queries         INTEGER,
    recorded_by_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    cluster_label   TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_score_history_ts
    ON health_score_history(ts);
