# Disaster Recovery Runbook

Operational procedures for backing up and restoring a ClickHouse Console
deployment. This covers the **console's own state**, not the customer's
ClickHouse clusters (those have their own backup/PITR features inside the
product).

> Audience: the operators who run the console in production. Keep a copy of this
> runbook somewhere reachable when the application host is down.

---

## 1. What state lives where

| Store | Holds | Authoritative? | Loss impact |
| --- | --- | --- | --- |
| **PostgreSQL** | users, roles, saved connections (encrypted), audit trail, saved queries, dashboards, alert rules, schedules | **Yes — the system of record** | Catastrophic without a backup. Everything below the app is here. |
| **Master key** (`.env` / KMS) | the key that decrypts saved ClickHouse credentials | **Yes** | Postgres rows remain but every saved ClickHouse credential becomes undecryptable. **Back this up separately from Postgres.** |
| **`.env`** | DB credentials, master key, SSO/LDAP secrets, license path | Yes | Service cannot start; secrets must be re-provisioned. |
| **Redis** | sessions, in-flight query jobs/results | **No — ephemeral** | Everyone is signed out and running queries are lost. No data loss. Safe to start empty. |
| **nginx `certs/`** | TLS certificate and key | Reproducible | TLS down until re-issued; no data loss. |
| **License file** | offline license | Reproducible (re-issue) | Product runs in its unlicensed state until restored. |

**Key principle:** PostgreSQL and the master key together are the only things
whose loss is unrecoverable. Redis can always be thrown away and recreated
empty.

---

## 2. What to back up, and how often

| Item | Method | Suggested cadence |
| --- | --- | --- |
| PostgreSQL | `pg_dump` (logical) and/or base backups / PITR (physical) | Logical daily; physical/WAL continuous for low RPO |
| Master key + `.env` | copy to your secrets manager / sealed store | On every change; verify quarterly |
| TLS certs | with your certificate management | On renewal |
| License file | keep the original issuance | Once |

### PostgreSQL logical backup

```bash
# On a host that can reach the database, with the same DB_* values as .env:
pg_dump \
  --host="$DB_HOST" --port="$DB_PORT" \
  --username="$DB_USER" --dbname="$DB_NAME" \
  --format=custom --no-owner --no-privileges \
  --file="chconsole-$(date +%Y%m%d-%H%M%S).dump"
```

Store the dump encrypted, off the application host. Test that it restores at
least quarterly — an untested backup is not a backup.

### Master key

The master key is the single most important secret. Store it in your KMS / Vault
/ sealed secrets store, **not** only in `.env` on the application host. If the
host is lost and the key was only in `.env`, every saved ClickHouse credential
is gone even though the Postgres rows survive.

---

## 3. Restore procedure

Order matters: provision secrets, restore the database, migrate, then start.

1. **Stand up the hosts.** A patched application host, a PostgreSQL instance, and
   a Redis instance on the private network (see `INSTALLATION.md`).

2. **Restore secrets.** Recreate `.env` from your secrets store. Critically,
   restore the **same master key** that was in use when the credentials were
   encrypted — a different key cannot decrypt existing saved connections.

3. **Restore PostgreSQL.**

   ```bash
   # Create an empty database first if needed, then:
   pg_restore \
     --host="$DB_HOST" --port="$DB_PORT" \
     --username="$DB_USER" --dbname="$DB_NAME" \
     --no-owner --no-privileges \
     chconsole-YYYYMMDD-HHMMSS.dump
   ```

4. **Bring the schema to head.** The restored dump is at whatever revision it was
   taken at. Apply any newer migrations:

   ```bash
   cd /opt/clickhouse-console
   set -a; . ./.env; set +a
   .venv/bin/alembic current     # show the restored revision
   .venv/bin/alembic upgrade head
   ```

5. **Redis needs nothing.** Start it empty. Sessions and jobs rebuild on use;
   users simply sign in again.

6. **Start the service and verify** (Section 5).

---

## 4. Schema migrations and rollback

Schema changes are managed by Alembic (`alembic.ini`, `migrations/`). The deploy
script (`deploy/update.sh`) runs `alembic upgrade head` automatically before
restarting, and **aborts the deploy without restarting if a migration fails**,
so the old code is never left against a half-migrated schema.

```bash
alembic current      # the DB's current revision
alembic history      # all migrations, newest last
alembic heads        # the target revision(s)
alembic upgrade head # apply everything pending
alembic downgrade -1 # roll back one step (where the migration defines a downgrade)
```

Notes:

* The **baseline** migration (`0001_baseline`) is deliberately **not**
  reversible — downgrading past it would drop every table. Recover by restoring
  a backup, never by downgrading the baseline.
* Before a risky migration on production, take a fresh `pg_dump` first. That dump
  *is* your rollback for destructive changes.
* `alembic upgrade head --sql` prints the SQL without touching the database if
  you want a review/approval step.

---

## 5. Post-restore verification

```bash
# Service up?
systemctl is-active clickhouse-console
curl -sk https://127.0.0.1/health

# Schema at head?
cd /opt/clickhouse-console && set -a; . ./.env; set +a
.venv/bin/alembic current
```

Then, in the UI:

* sign in (confirms Postgres + sessions),
* open a **saved connection** and run a trivial query (confirms the master key
  decrypts credentials correctly — the strongest signal that the key matches),
* open the audit trail and confirm recent events are present.

If saved connections fail to connect with a credential/decrypt error, the
restored master key does **not** match the one used to encrypt them. Stop and
locate the correct key before proceeding.

---

## 6. RPO / RTO guidance

* **RPO (how much data you can lose):** equal to your PostgreSQL backup interval.
  Daily `pg_dump` ⇒ up to 24 h. Continuous WAL archiving / PITR ⇒ near zero.
  Redis is ephemeral and does not factor in.
* **RTO (how long to recover):** dominated by provisioning hosts and the
  `pg_restore` time. Keep `.env` and the master key immediately retrievable so
  recovery is never blocked waiting on a secret.

The console performs no telemetry and has no cloud dependency, so recovery can be
completed entirely within an air-gapped environment.
