-- harden.sql — Phase 3 audit hardening (defense in depth).
--
-- WHAT THIS DOES
--   Revokes DELETE / UPDATE / TRUNCATE on the audit_events table from the
--   application's Postgres role. The application only ever INSERTs and
--   SELECTs audit_events — there is no UPDATE or DELETE code path anywhere
--   (verified in Phase 2). Removing those rights means that even if the
--   app role's credentials are compromised, the audit trail cannot be
--   rewritten or erased: it becomes append-only at the database level.
--
-- WHEN TO RUN
--   Once, AFTER the first successful startup — the schema (and so the
--   audit_events table) must already exist. Re-running is harmless.
--
-- WHO RUNS IT
--   A Postgres superuser or the audit_events table owner — NOT the
--   chconsole app role (it cannot revoke its own grants meaningfully).
--
--   psql -h <db-host> -U postgres -d chconsole -f harden.sql
--
-- ROLE NAME
--   Replace `chconsole` below if your application role has a different
--   name (matches DB_USER in your .env).

REVOKE DELETE, UPDATE, TRUNCATE ON audit_events FROM chconsole;

-- Verify (optional): this should list only INSERT and SELECT for chconsole.
--   \dp audit_events
