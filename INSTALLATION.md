# ClickHouse Console — Installation Guide

This guide walks you through installing ClickHouse Console in a customer environment, from prerequisites to first login. Two deployment paths are covered:

- **Path A — Docker (recommended)** for fast, isolated, easy-to-update installs.
- **Path B — Bare-metal Linux** when Docker is not allowed on the host.

Either way, the application stores all its data under a single `data/` directory and never modifies your ClickHouse cluster's data or configuration.

---

## 1. Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| OS | Linux x86_64 (Ubuntu 22.04+, RHEL 8+, Debian 12+) | macOS for testing only |
| CPU | 1 core | 2 cores recommended for 10+ concurrent users |
| RAM | 1 GB | 2 GB recommended |
| Disk | 5 GB free | Application is small; space goes to query/audit history |
| Python | 3.10 or newer (bare-metal only) | Pre-installed on most modern distros |
| **PostgreSQL** | **14 or newer, reachable from the app host** | **Stores users, the audit master, connections, and per-user query history / favorites / encrypted credentials. May run on the same VM as the app or on a separate, hardened DB VM.** |
| **Redis** | **6 or newer, reachable from the app host** | **Stores console sessions (Phase 2). AOF persistence recommended. May run on the same VM or a separate one.** |
| Docker | 20.10+ (Docker path only) | With Docker Compose v2 |
| Network | Outbound TCP to ClickHouse hosts on 8123/9000, to PostgreSQL on 5432, and to Redis on 6379 | Inbound TCP 443 from end users |

The installer needs `sudo` or root for the production setup (systemd unit, certificates, port 443). The application itself does **not** require root at runtime.

> **Why Postgres?** Earlier 3.x releases used per-process SQLite files. From v4 onward, all persistent state moved to PostgreSQL: one shared database for users, the audit master, connections, and per-user query history / favorites / credentials. This enables horizontal scaling (multiple gunicorn workers), point-in-time recovery via `pg_dump`/WAL, and consistent backups without stopping the service.
>
> **Why Redis?** From Phase 2 onward, **sessions** live in Redis rather than a Postgres table. Sessions are short-lived, write-heavy, and need automatic expiry — a natural fit for Redis key TTL. This keeps session churn off Postgres and lets the auth hot path resolve a cookie without a database round-trip. Redis is the only place sessions are stored; if Redis is unavailable the app fails closed (everyone is treated as signed out) rather than letting requests through.

---

## 2. Architecture overview

```
End users ─── HTTPS (443) ───▶ nginx ─── HTTP (5000) ───▶ ClickHouse Console
                                                                │
                                                                ├──▶ ClickHouse cluster
                                                                │    (your existing nodes,
                                                                │     port 8123/9000)
                                                                │
                                                                └──▶ PostgreSQL
                                                                     (console state,
                                                                      port 5432)
```

- ClickHouse Console runs as a single Python/Flask process on port 5000.
- nginx terminates TLS, adds security headers (HSTS, X-Frame-Options, etc.), and proxies to the app.
- The app talks to your ClickHouse cluster over the native HTTP interface (8123) or HTTPS (8443).
- Persistent console data (users, audit log, connections, query history, encrypted credentials) lives in **PostgreSQL**; **sessions** live in **Redis**. The local `data/` directory only holds install-level secrets (`master.key`, `instance.id`, `license.lic`) and append-only log files. **No tables, no rows, no settings are ever written to your ClickHouse cluster.**

---

## 3. File layout after installation

```
/opt/clickhouse-console/                  install root
├── app.py                                main application
├── db.py                                 Postgres connection pool + SQLite-compat shim
├── schema.sql                            DDL applied idempotently at startup
├── static/                               web assets
├── requirements.txt                      includes psycopg[binary] for Postgres
├── update.sh                             in-place upgrade tool
├── Dockerfile
├── docker-compose.yml
├── nginx/
│   ├── nginx.conf
│   └── certs/                            TLS certificates
├── public_key.pem                        license verification key (ships with build)
├── .env                                  environment overrides — including DB_* (you create this)
└── data/                                 install-level secrets + log files (small, no DB rows)
    ├── global/
    │   ├── master.key                    Fernet key for credential vault
    │   ├── instance.id                   per-install fingerprint (used for licensing)
    │   └── license.lic                   your license token (optional, see §10)
    └── <username>/
        └── (per-user activity log files, JSON-lines; rotated monthly)
```

All structured state (users, sessions, connections, audit events, query history, favorites, encrypted credentials) lives in **PostgreSQL** — see the schema in `schema.sql`. The per-user folders under `data/` only carry append-only activity logs for SIEM ingestion; no database files live there anymore.

> **Migrating from a 3.x SQLite install?** Stop the old service, take a final `tar.gz` of `data/` for the audit trail, then point the new build at a freshly created Postgres database. Startup applies `schema.sql` idempotently. There is no automatic SQLite-to-Postgres migration tool in this release; user accounts are recreated via `python app.py create-user` (one line per user). Contact your vendor if you need a bulk migration helper for a large install.

---

## 4. Path A — Docker installation (recommended)

### 4.1 Copy the release package to the target host

Move `clickhouse-console.zip` to the customer host using `scp`, USB stick, or your usual transfer method, then place it under `/opt`:

```bash
sudo mkdir -p /opt
sudo unzip clickhouse-console.zip -d /opt
cd /opt/clickhouse-console
```

### 4.2 Prepare PostgreSQL

PostgreSQL is **not** bundled in the docker-compose stack. You can:

- **Option A — run an external Postgres** (managed service, an existing internal cluster, or a separately-hardened DB VM). Recommended for production.
- **Option B — run Postgres on the same host** as a sidecar container or via your distro's `postgresql` package. Acceptable for small / single-tenant installs.

Create a dedicated database and role:

```bash
# Example: same-host Postgres (Debian/Ubuntu)
sudo -u postgres psql <<'SQL'
CREATE USER chconsole WITH PASSWORD 'CHANGE_THIS_STRONG_PASSWORD';
CREATE DATABASE chconsole OWNER chconsole
    ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8' TEMPLATE template0;
SQL
```

The console only needs the standard CRUD privileges its owner role grants by default — no superuser, no extensions. Schema is applied automatically on startup.

> **Encoding matters.** `schema.sql` includes UTF-8 comment markers. Creating the database with `ENCODING 'UTF8'` (not `SQL_ASCII`) avoids `UnicodeEncodeError` at first start.

### 4.3 Generate a master key

The master key encrypts ClickHouse passwords stored in the credential vault. Generate one:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy the output. Open `.env`:

```bash
cp .env.example .env
nano .env
```

A complete `.env` looks like:

```
# ── PostgreSQL connection ─────────────────────────────────
DB_HOST=127.0.0.1
DB_PORT=5432
DB_USER=chconsole
DB_PASSWORD=CHANGE_THIS_STRONG_PASSWORD
DB_NAME=chconsole
DB_POOL_MIN=2
DB_POOL_MAX=20

# ── Redis connection (Phase 2: session store) ─────────────
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=CHANGE_THIS_STRONG_PASSWORD
REDIS_DB=0

# ── Console secrets ───────────────────────────────────────
MASTER_KEY=<your generated Fernet key>
SESSION_TTL_DAYS=7
TZ=Europe/Istanbul
```

If you skip `MASTER_KEY` the application will generate one on first run and write it to `data/global/master.key`. Setting `MASTER_KEY` via env is more secure — the key never touches disk.

`REDIS_PASSWORD` may be left empty for a Redis instance with no auth, but a password is strongly recommended for any non-loopback Redis. `SESSION_TTL_DAYS` sets both the cookie lifetime and the Redis key TTL.

> **Pool sizing.** `DB_POOL_MIN`/`DB_POOL_MAX` set per-process pool size. Total connections at peak = `DB_POOL_MAX × gunicorn-workers`. Stay well below Postgres's `max_connections` (default 100).

### 4.4 Prepare TLS certificates

For internal deployments a self-signed certificate is sufficient:

```bash
mkdir -p nginx/certs
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout nginx/certs/server.key \
    -out nginx/certs/server.crt \
    -subj "/CN=clickhouse-console.yourcompany.local"
sudo chmod 600 nginx/certs/server.key
```

For internet-facing installs use a real certificate (Let's Encrypt, your internal CA, etc.) and place the `.crt` / `.key` pair at the same path.

### 4.5 Bring up the stack

```bash
sudo docker compose up -d
sudo docker compose ps
```

You should see two containers running: `clickhouse-console` and `nginx`.

### 4.6 Create the first admin user

```bash
sudo docker compose exec clickhouse-console \
    python app.py create-user admin --role admin --password 'StrongPassHere!'
```

You can also omit `--password` to be prompted interactively.

### 4.7 First login

Open `https://<host>/` in a browser. Accept the certificate warning if self-signed. Log in with `admin` / `StrongPassHere!`.

---

## 5. Path B — Bare-metal installation

### 5.1 Install Python dependencies

```bash
sudo mkdir -p /opt
sudo unzip clickhouse-console.zip -d /opt
cd /opt/clickhouse-console

sudo python3 -m venv .venv
sudo .venv/bin/pip install --upgrade pip
sudo .venv/bin/pip install -r requirements.txt
```

> `requirements.txt` pulls in `psycopg[binary]` and `psycopg_pool`. The `[binary]` extra ships the libpq bindings prebuilt — no `libpq-dev` or `apt-get build-essential` required on the target. If your security policy forbids prebuilt binaries, switch to plain `psycopg>=3.1` and `apt-get install libpq-dev gcc python3-dev` before running pip.

### 5.2 Install and prepare PostgreSQL

If your deployment uses a managed Postgres (RDS, Cloud SQL, an existing internal cluster), skip the install step and only create the database/role. Otherwise:

```bash
# Same-host Postgres on Debian/Ubuntu:
sudo apt-get update
sudo apt-get install -y postgresql
sudo systemctl enable --now postgresql

# Create the database and role:
sudo -u postgres psql <<'SQL'
CREATE USER chconsole WITH PASSWORD 'CHANGE_THIS_STRONG_PASSWORD';
CREATE DATABASE chconsole OWNER chconsole
    ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8' TEMPLATE template0;
SQL
```

For a separate DB VM (recommended in production), bind Postgres to the internal interface only and restrict `pg_hba.conf` to the app VM's IP using `scram-sha-256` authentication:

```
# /etc/postgresql/<ver>/main/pg_hba.conf
host    chconsole    chconsole    10.0.0.0/24    scram-sha-256
```

Reload Postgres after editing: `sudo systemctl reload postgresql`. Verify reachability from the app host: `psql -h <db-host> -U chconsole -d chconsole -c 'SELECT 1'`.

### 5.3 Create a dedicated system user

Running the application as root is discouraged. Create an unprivileged user:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin clickhouse-console
sudo chown -R clickhouse-console:clickhouse-console /opt/clickhouse-console
```

### 5.4 Environment file

```bash
sudo cp .env.example .env
sudo nano .env
```

Set the Postgres connection variables (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`) and the Redis connection variables (`REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_DB`), then `MASTER_KEY` (see §4.3 above), `SESSION_TTL_DAYS`, and any other overrides. A complete example is shown in §4.3.

Restrict the file: `sudo chmod 600 /opt/clickhouse-console/.env && sudo chown clickhouse-console: /opt/clickhouse-console/.env` — it holds the DB password and master key.

### 5.5 Create the first admin user

```bash
sudo -u clickhouse-console env $(grep -v '^#' .env | xargs) \
    .venv/bin/python app.py \
    create-user admin --role admin --password 'StrongPassHere!'
```

The `env $(...)` form loads the Postgres variables from `.env` for this one command. The systemd unit below uses `EnvironmentFile=` so it doesn't need this prefix.

### 5.6 Production install — systemd + gunicorn + nginx (Phase 3)

From Phase 3 the release ships ready-made deployment assets and an
installer that wires them up. Instead of hand-writing a unit file, run:

```bash
# from the unzipped release directory, as root
sudo bash deploy/install.sh
```

The installer is idempotent and **does not** touch an existing `.env` or
`data/` directory. It:

- creates the unprivileged system user `chconsole`;
- syncs the release to `/opt/clickhouse-console` and builds a virtualenv;
- installs `deploy/clickhouse-console.service` — a hardened systemd unit
  that runs **gunicorn** (not the Flask dev server) via `run-prod.sh`,
  with `Restart=always`, `NoNewPrivileges`, `ProtectSystem=strict`, and
  write access limited to `data/` and the log directories;
- installs the nginx TLS site (`nginx/clickhouse-console.site`) and
  generates a self-signed certificate under `/etc/ssl/clickhouse-console/`.

It deliberately **does not start** the service — review `.env` first, then:

```bash
sudo systemctl start clickhouse-console
sudo systemctl status clickhouse-console
journalctl -u clickhouse-console -f          # live logs
sudo systemctl reload nginx                  # serve TLS
```

The service is enabled on boot. gunicorn worker/thread counts come from
`GUNICORN_WORKERS` / `GUNICORN_THREADS` in `.env` (defaults: 2 and 4 —
fine for a 1–2 GB VM; raise them on larger hosts).

> **TLS certificate.** The bundled certificate is self-signed, so browsers
> show a warning — expected for an internal console. To use an internal-CA
> or public certificate instead, replace `/etc/ssl/clickhouse-console/server.crt`
> and `server.key` and `sudo systemctl reload nginx`. If the console has a
> real DNS name, also set `server_name` in
> `/etc/nginx/sites-available/clickhouse-console`.

> **Manual alternative.** If you prefer not to use the installer, the unit
> file (`deploy/clickhouse-console.service`) and nginx site
> (`nginx/clickhouse-console.site`) are plain files you can copy into place
> yourself; both carry header comments with the exact steps.

### 5.8 First login

Open `https://<host>/` in a browser. Log in with `admin` / `StrongPassHere!`.

---

## 6. Connecting to your ClickHouse cluster

The console does **not** auto-discover clusters. Each cluster you want to manage must be added once, by an admin:

1. Log in as admin.
2. Top right → **Connections** → **New connection**.
3. Fill in:
   - **Name**: any friendly label, e.g. `prod`.
   - **Host**: ClickHouse node hostname or IP. For replicated clusters point at any member; the console queries `system.clusters` for the rest.
   - **Port**: typically `8123` (HTTP) or `8443` (HTTPS).
   - **Cluster name**: the value used in your `<remote_servers>` config (e.g. `prod`, `default_cluster`). Leave empty for single-node setups.
4. Save. The cluster appears in the sidebar.
5. Click the cluster → **Set credentials**. Enter the ClickHouse username/password the **current console user** should use against this cluster. Credentials are encrypted with the Fernet master key before being stored.
6. Click **Connect** → **Test**. If it returns green, you are ready to run queries.

Repeat for any other clusters (staging, analytics, etc.). Cluster definitions are shared; per-user credentials are private.

---

## 7. Adding more users

### 7.1 From the UI (admin only)

**Settings → Users → New user**. Provide username, role, and an initial password. Usernames must match `[A-Za-z0-9._-]{1,32}` (used as a folder name on disk).

### 7.2 From the CLI

```bash
# Docker:
sudo docker compose exec clickhouse-console \
    python app.py create-user developer1 --role developer --password 'pw'

# Bare-metal:
sudo -u clickhouse-console /opt/clickhouse-console/.venv/bin/python \
    /opt/clickhouse-console/app.py create-user developer1 --role developer --password 'pw'
```

Roles available:

| Role | Capabilities |
|---|---|
| `admin` | Everything: users, license, audit, all queries, all panels |
| `developer` | Run queries, branching, profiler, schema browser, part inspector |
| `monitoring` | All monitoring panels including part inspector and storage; no destructive actions |
| `readonly` | Read-only views — no INSERT/ALTER/DROP, no destructive panels |

Creating a user automatically creates `data/<username>/<username>.db` with their private tables.

### 7.3 Resetting a password

```bash
sudo docker compose exec clickhouse-console \
    python app.py reset-password developer1 --password 'newpw'
```

This also invalidates that user's existing sessions, forcing a fresh login.

### 7.4 Deleting a user

**Settings → Users → ✕** next to the user, or via API. **The user's `data/<username>/` folder is never physically deleted** — it is renamed to `data/<username>.deleted/` (or `.deleted.2`, `.deleted.3` on repeat) so query history and audit trails remain available for future investigation.

---

## 8. Updating to a new version

The console ships a self-contained update script that preserves your data and rolls back automatically on failure.

```bash
cd /opt
./clickhouse-console/update.sh ~/clickhouse-console-3.3.zip
```

What it does:

1. Takes a full snapshot of the current install.
2. Stops the running process (`systemctl stop` or container restart).
3. Extracts the new release over the install root.
4. Preserves: `data/`, `public_key.pem`, optional `private_key.pem`, `.env`, `nginx/certs/`, `console.log`.
5. Validates the new code (Python parse + import probe).
6. Starts the service back up.
7. Performs a health check on `/health`.
8. On any failure, restores the previous snapshot and starts the old version.

Schema migrations run automatically on the first start with the new code.

> **Upgrading to the Phase 2 (Redis sessions) build.** This release moves
> sessions from a Postgres table to Redis. Before or right after the
> update:
>
> 1. Provision a Redis instance (6+) and add `REDIS_HOST`, `REDIS_PORT`,
>    `REDIS_PASSWORD`, `REDIS_DB` to `.env` — see §4.3.
> 2. There is **no session migration**: all users are signed out by the
>    upgrade and simply log in again. This is expected, not an error.
> 3. The old Postgres `sessions` table is no longer created or used. Once
>    the new build is confirmed working, drop the leftover table by hand:
>    ```sql
>    DROP TABLE IF EXISTS sessions;
>    ```
>    Nothing in the application references it after the upgrade; this is
>    cleanup only.

---

## 9. Backup & restore

Two things need backing up, and they live in different places now:

> **What about Redis?** Sessions in Redis are deliberately **not** part of
> the backup bundle. They are short-lived and reconstructible — if Redis is
> lost, every user simply logs in again, exactly like the Phase 2 upgrade
> itself. Enable AOF persistence on Redis so a restart doesn't sign
> everyone out, but there is nothing here to `pg_dump` or `tar`. The audit
> trail of logins/logouts/revocations is in Postgres `audit_events`, which
> *is* backed up below.

1. **PostgreSQL** — all structured state (users, sessions, audit events, query history, favorites, encrypted credentials). Backed up with `pg_dump`.
2. **`data/` directory plus `.env`** — install-level secrets (`master.key`, `instance.id`, `license.lic`) and append-only activity log files. Backed up with `tar`.

You need **both**. Losing `master.key` makes every stored ClickHouse password unrecoverable, even with a fresh Postgres restore. Losing the database loses every user, audit row, and credential. Treat them as one bundle.

### 9.1 Recommended backup

```bash
DATE=$(date +%F)
BACKUP_DIR=/var/backups/clickhouse-console
sudo mkdir -p "$BACKUP_DIR"

# 1. Dump Postgres (works hot — no downtime needed)
sudo -u postgres pg_dump --format=custom --file="$BACKUP_DIR/chconsole-$DATE.dump" chconsole

# 2. Snapshot the install-level secrets + activity logs
sudo tar czf "$BACKUP_DIR/chconsole-files-$DATE.tar.gz" \
    -C /opt/clickhouse-console data .env

# 3. Set tight permissions — both files contain secrets
sudo chmod 600 "$BACKUP_DIR/chconsole-$DATE.dump" "$BACKUP_DIR/chconsole-files-$DATE.tar.gz"
```

Move both files off-host. The dump is self-contained: it embeds schema + data, and the custom format supports parallel restore.

### 9.2 Restore

```bash
# 1. Restore secrets + logs first (so master.key matches the encrypted creds)
sudo tar xzf chconsole-files-<DATE>.tar.gz -C /opt/clickhouse-console

# 2. Recreate an empty database (drop the old one only if you're sure)
sudo -u postgres psql -c "DROP DATABASE IF EXISTS chconsole;"
sudo -u postgres psql -c "CREATE DATABASE chconsole OWNER chconsole \
    ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8' TEMPLATE template0;"

# 3. Restore the dump
sudo -u postgres pg_restore --dbname=chconsole --no-owner --role=chconsole \
    chconsole-<DATE>.dump

# 4. Start the service
sudo systemctl start clickhouse-console
```

### 9.3 Continuous / point-in-time recovery

If your operations team already runs Postgres backups (base backup + WAL archiving, or a managed service with PITR), the console database is just another database — no special handling needed. PITR lets you roll the audit log back to any second within the retention window.

For lighter setups, a nightly `pg_dump` cron job plus a weekly secrets-tarball rotation is a reasonable baseline:

```cron
# /etc/cron.d/clickhouse-console-backup
15 2 * * *  postgres  pg_dump --format=custom --file=/var/backups/clickhouse-console/chconsole-$(date +\%F).dump chconsole
30 2 * * 0  root      tar czf /var/backups/clickhouse-console/chconsole-files-$(date +\%F).tar.gz -C /opt/clickhouse-console data .env
0  3 * * *  root      find /var/backups/clickhouse-console -name 'chconsole-*' -mtime +30 -delete
```

> **Do not** `rsync` Postgres data files (`/var/lib/postgresql/`) as a backup — that produces inconsistent copies. Always use `pg_dump` (logical) or `pg_basebackup` + WAL archiving (physical).

---

## 10. Licensing

The console runs in **Community mode** out of the box, with a hard limit of 3 users. To enable more users (or to remove the trial banner), apply a license token.

### 10.1 Provide your instance fingerprint to the vendor

```bash
cat /opt/clickhouse-console/data/global/instance.id
```

Send this 64-character string to the vendor along with your license request.

### 10.2 Apply the license token

The vendor will return a signed token (a long base64 string). Apply it through the UI:

**Settings → License → Paste token → Apply**

Or by file:

```bash
echo "<token>" | sudo tee /opt/clickhouse-console/data/global/license.lic
sudo systemctl restart clickhouse-console
```

Verify under **Settings → License** — you should see the customer name, expiry, and user limit.

---

## 11. Audit logs and operator CLI

ClickHouse-Console maintains a complete, append-only audit trail of every user action — login, query execution, dashboard change, branch creation, license update, alert config, and so on. All of this material lives in two places: text log files on disk (for SIEM ingestion and grep-style review) and PostgreSQL tables (for structured queries and exports).

### 11.1 On-disk log layout

Logs live under `logs/` and rotate at calendar-month boundaries. Past months are gzipped automatically — **nothing is ever deleted**.

```
logs/
├── global/
│   ├── console-2026-05.log              ← server log, current month (active)
│   ├── console-2026-04.log.gz           ← server log, past month (gzipped)
│   ├── activity-2026-05.log             ← UI activity, ALL users, current month
│   └── activity-2026-04.log.gz          ← past month, gzipped
└── users/
    ├── admin/
    │   ├── activity-2026-05.log         ← only admin's actions, current month
    │   └── activity-2026-04.log.gz      ← past month, gzipped
    └── cansayin/
        ├── activity-2026-05.log         ← only cansayin's actions
        └── activity-2026-04.log.gz
```

Format of every activity log entry — **JSON-lines** (one JSON object per line):

```json
{"ts": "2026-05-12 11:46:26", "console_user": "admin", "console_role": "admin", "panel": "query", "action": "Run Query", "detail": "SELECT *\nFROM events\nWHERE created_at >= '2026-05-01'\nLIMIT 100", "conn_host": "localhost:8123", "conn_user": "default", "ip": "10.0.0.5", "result": "ok"}
```

This format is compact, machine-parseable by any standard tool (jq, Python, awk), and preserves multi-line SQL details via `\n` escapes. For human review, the `read-log` CLI parses these lines and renders them as a block-formatted view:

```
============================================================
[2026-05-12 11:46:26] admin (admin) @ localhost:8123/default  · ip=10.0.0.5
Panel:  query
Action: Run Query
Detail:
    SELECT *
    FROM events
    WHERE created_at >= '2026-05-01'
    LIMIT 100
```

Pass `--raw` to keep the JSON-lines for downstream tooling (jq, SIEM forwarders, etc.).

### 11.2 List every retained log file

```bash
# All logs grouped by global / per-user
python3 app.py list-logs

# Only one user's logs
python3 app.py list-logs --user cansayin

# Only the global logs
python3 app.py list-logs --global
```

Output shows each file with its size, last-modified time, and a flag indicating whether it is the currently active month or a gzipped archive:

```
GLOBAL LOGS  (/opt/clickhouse-console/logs/global)
  activity-2026-05.log               779 B  2026-05-12 11:51 ← ACTIVE
  console-2026-05.log               4228 B  2026-05-12 11:51 ← ACTIVE
  activity-2026-04.log.gz             12K   2026-04-30 23:59 (gzipped)

USER LOGS — admin
  activity-2026-05.log               579 B  2026-05-12 11:51 ← ACTIVE

USER LOGS — cansayin
  activity-2026-05.log               200 B  2026-05-12 11:51 ← ACTIVE
```

### 11.3 Read a specific monthly log

```bash
# Current month for a user (rendered as readable blocks)
python3 app.py read-log --user cansayin

# A past month — the .gz file is decompressed on the fly
python3 app.py read-log --global --month 2026-04

# Grep within a log (case-insensitive, matches every field)
python3 app.py read-log --user admin --grep "Run Query"

# Last 50 events of the current global log
python3 app.py read-log --global --last 50

# Raw JSON-lines (for jq, scripts, SIEM ingestion)
python3 app.py read-log --user cansayin --raw | jq '.action'
```

Flags:
- `--user <name>` or `--global` (pick one)
- `--month YYYY-MM` (default: current month)
- `--grep <substring>` (case-insensitive, matches every field of every event)
- `--last N` (show only the last N matching events)
- `--raw` (emit raw JSON-lines instead of rendered blocks)

The parser auto-detects file format. New writes are JSON-lines, but if you have older log files in the previous pretty-block format, `read-log` understands those too — no migration required for reads.

### 11.4 Migrate legacy pretty-block files to JSON-lines (optional)

If you have log files created by an earlier release of the console that used the pretty-block format, you can convert them in place to JSON-lines for cleaner SIEM ingestion and tool compatibility:

```bash
# Preview what would be converted
python3 app.py migrate-logs --dry-run

# Actually convert
python3 app.py migrate-logs
```

The command walks every `activity-*.log` under `logs/global/` and `logs/users/<username>/`. Already-JSON files are detected and skipped — the operation is idempotent and safe to run repeatedly. Each file is rewritten atomically (`.new` then rename), so an interrupted run never leaves a corrupted file. Gzipped archives (`.log.gz`) are left as-is; `read-log` handles both formats transparently on read, so there is no need to rewrite history.

### 11.5 Export the full audit trail to CSV / JSON / TSV / pretty

```bash
# Whole month, CSV for Excel / spreadsheet
python3 app.py export-audit --month 2026-05 --format csv --out /tmp/audit-may.csv

# One user, JSON for programmatic processing
python3 app.py export-audit --user cansayin --format json --out /tmp/cansayin.json

# Specific action across a date range, pretty block format
python3 app.py export-audit --from 2026-04-01 --to 2026-04-30 \
    --action "Run Query" --format pretty --out /tmp/april-queries.txt

# Single panel
python3 app.py export-audit --panel branch --format tsv
```

Flags:
- `--month YYYY-MM` **or** `--from YYYY-MM-DD --to YYYY-MM-DD` (time range)
- `--user <substring>` (actor)
- `--action <substring>`
- `--panel <substring>`
- `--format csv | tsv | json | pretty`
- `--out <file>` (default: stdout)

`export-audit` reads directly from the `audit_events` table in PostgreSQL and returns the **full retained history** — it is not bounded by the UI's 1,000-event display cap.

### 11.6 Tail the live activity log

For real-time monitoring:

```bash
tail -F /opt/clickhouse-console/logs/global/activity-$(date +%Y-%m).log
```

For per-user tailing:

```bash
tail -F /opt/clickhouse-console/logs/users/cansayin/activity-$(date +%Y-%m).log
```

### 11.7 Ship logs to a SIEM

Because the on-disk activity log is **JSON-lines** (one well-formed JSON object per line), every major SIEM (Splunk, ELK, Datadog, Wazuh, Graylog, etc.) ingests it natively — no multi-line pattern configuration needed. Point your forwarder at `/opt/clickhouse-console/logs/` and configure source type `json`. Each forwarded event already has the full field set: `ts`, `console_user`, `panel`, `action`, `detail`, `conn_host`, `ip`, `result`. Gzipped archives are picked up automatically for back-fill.

### 11.8 Retention and immutability

- The activity log is **append-only**. There is no UI control to clear it; the API endpoint that previously did so now returns HTTP 403 and records the rejected attempt as an audit event of its own.
- The `audit_events` table in Postgres has no `DELETE` code path in the application — only `INSERT` and `SELECT`. To prevent operator tampering, grant `chconsole` only `INSERT, SELECT` on `audit_events` (the rest of the schema needs `UPDATE, DELETE` for normal app operation):
  ```sql
  REVOKE DELETE, UPDATE, TRUNCATE ON audit_events FROM chconsole;
  ```
  Apply this after the first startup (so the schema exists). The application never issues `DELETE`/`UPDATE` on `audit_events`; the revoke is defense in depth against a compromised app role. From Phase 3 this is shipped as `harden.sql` and is a **required** post-install step, not an optional one — run it once as a Postgres superuser:
  ```bash
  psql -h <db-host> -U postgres -d chconsole -f harden.sql
  ```
- Install-level secret files (`data/global/master.key`, `data/global/instance.id`) keep `0600` mode. The activity log files under `data/<username>/` are `0640` for SIEM forwarders.
- The per-user folder under `logs/users/` survives even when the user is soft-deleted; in Postgres the corresponding rows are removed via `ON DELETE CASCADE` (sessions, query history, favorites, encrypted credentials), but the on-disk log directory is preserved for forensic continuity.

---

## 12. Troubleshooting

### Service won't start

```bash
# systemd:
sudo journalctl -u clickhouse-console -n 200 --no-pager

# Docker:
sudo docker compose logs --tail=200 clickhouse-console
```

Look for the first `ERROR` line. Common causes:
- Port 5000 already in use → change `PORT` in `.env`.
- `data/` not writable → check ownership.
- Master key mismatch (rotated key without re-encrypting) → restore previous `master.key`.
- `psycopg.OperationalError: could not connect to server` → Postgres unreachable. Verify with `psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -c 'SELECT 1'` using the same `.env` values. Check `pg_hba.conf` allows the app host and that the DB password matches.
- `psycopg.errors.UndefinedTable` on startup → the schema wasn't applied. Confirm `schema.sql` is present in the install root and the DB role owns the database. Manual reapply: `sudo -u postgres psql -d chconsole -f /opt/clickhouse-console/schema.sql`.
- `UnicodeEncodeError` on first start → the database was created with `SQL_ASCII` encoding instead of `UTF8`. Drop and recreate with `ENCODING 'UTF8' TEMPLATE template0`.

### Cannot reach the UI

```bash
curl -k https://localhost/health
# Expect: {"status":"ok","version":"..."}
```

If this fails:
1. Is the console process listening? `ss -ltnp | grep 5000`.
2. Is nginx listening? `ss -ltnp | grep ':443\|:80'`.
3. Is the firewall open? `sudo ufw status` or `sudo firewall-cmd --list-all`.

### Login fails with correct password

Check the audit log directly in Postgres:

```bash
# From any host that can reach the DB (use the .env credentials):
psql "postgres://chconsole:$DB_PASSWORD@$DB_HOST:$DB_PORT/$DB_NAME" \
    -c "SELECT ts, action, detail, result FROM audit_events ORDER BY id DESC LIMIT 20"

# Or via the export-audit CLI from the install root:
sudo -u clickhouse-console env $(grep -v '^#' .env | xargs) \
    .venv/bin/python app.py export-audit --action Login --format pretty | tail -40
```

Failed logins appear as `Login` events with `result='fail'`. Most often the cause is a typo, a locked-out account (`is_active=0`), or an expired session cookie cached in the browser.

### Cluster connection fails

From the host running the console:

```bash
curl http://<clickhouse-host>:8123/ping
# Expect: Ok.

curl -u user:pass "http://<clickhouse-host>:8123/?query=SELECT+1"
# Expect: 1
```

If those work but the console says "connection refused", restart the console once — DNS or routing changes are picked up on the next ClickHouse client construction.

### Lost admin password

```bash
sudo docker compose exec clickhouse-console \
    python app.py reset-password admin --password 'NewPassword!'
```

---

## 13. Security notes for the installation

- **Bind the app to 127.0.0.1 only.** All external access goes through nginx with TLS. Never expose port 5000 to the internet.
- **Set `MASTER_KEY` via environment, not via the on-disk fallback.** A leaked `master.key` lets an attacker decrypt every stored ClickHouse password.
- **Rotate the admin password after first login.** The setup password should not be reused.
- **Keep `data/global/instance.id` confidential.** Disclosing it makes targeted license forgery slightly easier.
- **Audit the audit log.** `Settings → Audit` lists every privileged action. Schedule periodic review or ship the events to your SIEM (the `audit_events` table in Postgres is queryable with any standard SQL client; the on-disk activity log under `logs/` is JSON-lines and ingestible by any forwarder).
- **Harden the Postgres role.** The `chconsole` role should not be a superuser. Restrict `pg_hba.conf` to the app host's IP with `scram-sha-256` authentication. Revoke `DELETE`/`UPDATE` on `audit_events` (the app never writes those — see §11.8). For separate-VM deployments, place the DB on a private network reachable only from the app VM.
- **Back up the bundle, not the parts.** `master.key` plus the Postgres dump form one secret; storing them separately defeats encrypted-credentials-at-rest. Treat backup transport and storage as a single high-sensitivity flow.
- **Run the console host inside the same trust boundary as your ClickHouse cluster.** Don't put the console in a DMZ that has elevated credentials to your production database.

---

## 14. Uninstall

```bash
# Docker:
sudo docker compose down
sudo rm -rf /opt/clickhouse-console

# Bare-metal:
sudo systemctl disable --now clickhouse-console
sudo rm /etc/systemd/system/clickhouse-console.service
sudo systemctl daemon-reload
sudo rm /etc/nginx/sites-enabled/clickhouse-console
sudo systemctl reload nginx
sudo rm -rf /opt/clickhouse-console
sudo userdel clickhouse-console

# Postgres (BOTH paths — only if you're sure; this drops every audit row):
sudo -u postgres psql <<'SQL'
DROP DATABASE IF EXISTS chconsole;
DROP ROLE IF EXISTS chconsole;
SQL
```

> Take a final `pg_dump` before dropping the database if the install holds audit history you may need later. Once dropped, the audit trail is gone.

The uninstall touches only the console host (and the Postgres database it owns). Your ClickHouse cluster is unaffected — the console never persists state on the cluster side.

---

## 15. Support

For installation help, licensing, or upgrades, contact your vendor with the following information:

- Console version (visible in the bottom-left sidebar).
- Instance fingerprint (`data/global/instance.id`).
- The last 200 lines of `console.log` or `journalctl -u clickhouse-console`.
- A short description of the symptom and the steps that produced it.

---

**Document version**: 2.0 (Postgres backend)
**Applies to**: ClickHouse Console 4.x
