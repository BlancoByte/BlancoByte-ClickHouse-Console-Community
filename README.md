# BlancoByte ClickHouse Console — Community Edition

A multi-user, web-based operations console for ClickHouse: SQL workbench, live
monitoring, per-query and per-user cost analysis, cluster topology,
mutations/replication tooling, and role-based access control.

Hundreds of developers can connect concurrently. Every action and audit event
is written to a PostgreSQL metadata database (recommended on a separate
server), and session management is handled by Redis.

---
<img width="696" height="654" alt="Screenshot 2026-08-15 at 13 24 46" src="https://github.com/user-attachments/assets/dfef07ab-2b55-4ad7-b4b1-8bfbd15cc6b6" />

<img width="1324" height="829" alt="Screenshot 2026-08-15 at 13 25 23" src="https://github.com/user-attachments/assets/ba0e638f-44d7-41e1-93c2-2c0f581d9f7a" />


## Community vs Enterprise

The Community Edition includes the **full feature set** — SQL workbench,
monitoring, cost analysis, cluster tooling, RBAC, audit logging, LDAP/SSO,
SIEM forwarding, and the compliance pack are all present and functional.

The only limit is **3 console users**. Enterprise licensing lifts the user
limit for large teams and adds priority support and SLAs. For enterprise
licensing, contact **support@blancobyte.com**.

---

## Requirements

| Component | Minimum | Notes |
|---|---|---|
| OS | Linux x86_64 (Ubuntu 22.04+, RHEL 8+, Debian 12+) | macOS for local testing only |
| Python | 3.10 or newer | With `venv` module (`python3-venv` on Debian/Ubuntu) |
| PostgreSQL | 14 or newer | Metadata store: users, audit, connections, per-user query history / favorites / encrypted credentials. Separate server recommended. |
| Redis | 6 or newer | Session store. AOF persistence recommended. |
| ClickHouse | Any supported version | Must have system tables enabled (`query_log`, `processes`, etc.) |
| Network | Outbound TCP to ClickHouse (8123/9000), PostgreSQL (5432), Redis (6379) | Inbound TCP from end users |

---

## Installation

### 1. Install PostgreSQL and Redis

**PostgreSQL** (metadata store):

```bash
# Ubuntu/Debian
sudo apt update && sudo apt install -y postgresql

sudo -u postgres psql <<'SQL'
CREATE DATABASE chconsole;
CREATE USER chconsole WITH ENCRYPTED PASSWORD 'change-me-strong-password';
GRANT ALL PRIVILEGES ON DATABASE chconsole TO chconsole;
SQL
```

**Redis** (session store):

```bash
sudo apt install -y redis-server
sudo systemctl enable --now redis-server
# (Recommended) set "requirepass your-redis-password" in /etc/redis/redis.conf,
# then: sudo systemctl restart redis-server
```

### 2. Get the application

```bash
git clone https://github.com/BlancoByte/BlancoByte-ClickHouse-Console-Community.git
cd BlancoByte-ClickHouse-Console-Community
```

### 3. Run the installer

`install.sh` creates a Python virtualenv (`.venv/`), installs all dependencies,
and generates helper scripts (`run.sh`, `run-prod.sh`, `admin.sh`, `logtail.sh`):

```bash
chmod +x install.sh
./install.sh
```

### 4. Configure PostgreSQL, Redis, and the master key

Create a file named **`.env` in the project root directory** — the same folder
as `app.py` and `run-prod.sh`. This is the single file where all connection
parameters go; `run-prod.sh` loads it automatically on startup.

```bash
cp .env.example .env    # then edit .env
```

Put your PostgreSQL and Redis details into **`.env`**:

```bash
# ── PostgreSQL (metadata store) ──
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=chconsole
DB_USER=chconsole
DB_PASSWORD=change-me-strong-password

# ── Redis (session store) ──
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=          # set if you configured requirepass
REDIS_DB=0

# ── Master key — encrypts stored ClickHouse credentials ──
# Generate one with:  python3 -c "import secrets; print(secrets.token_urlsafe(48))"
MASTER_KEY=paste-a-long-random-value-here
```

> **Where does `.env` live?** In the project root, next to `app.py`. It holds
> secrets, is already in `.gitignore`, and must never be committed.
>
> `run-prod.sh` sources `.env` automatically. For a **dev** start with `./run.sh`,
> load it manually first: `set -a; source .env; set +a`. Under **systemd**, point
> `EnvironmentFile=` at this `.env` (see `deploy/` and `INSTALLATION.md`).

### 5. Create the first administrator

There is **no default account** — create the first admin explicitly with the
`admin.sh` helper:

```bash
./admin.sh create-user admin --role admin --password 'admin123'
```

This creates user `admin` with password `admin123`. **Log in and change this
password immediately** from **My Profile → account & password**. You can then
create up to 3 total users (Community limit) under **Security → Users**.

Other CLI helpers:

```bash
./admin.sh list-users                             # list console users
./admin.sh create-user alice --role developer     # add a user
./admin.sh reset-password admin                    # reset a password
```

### 6. Run

**Development** (Flask, http://localhost:5000):

```bash
set -a; source .env; set +a
./run.sh
```

**Production** (gunicorn, loads `.env` automatically):

```bash
./run-prod.sh
```

Put nginx with TLS in front for production. For a hardened systemd + nginx
setup, see the `deploy/` directory and `INSTALLATION.md`.

### Updating to a newer version

`update.sh` performs a safe in-place upgrade from a release zip: it snapshots
the current install, replaces only code files, validates them, and restarts —
rolling back on failure. Your `data/`, keys, TLS certs, and `.env` are never
overwritten.

```bash
./update.sh /path/to/blancobyte-clickhouse-console-X.Y.zip [/path/to/install]
```

---

## Roles

Access is role-based and enforced on the server for every endpoint:

- **Admin** — full access to all panels and actions, including user management.
- **Developer** — run queries (read & write), kill jobs, monitor, browse schema.
- **Monitoring** — monitoring, cluster, dashboards, user cost, and storage (read).
- **Read-only** — view-only access to query, monitor, schema, dashboards, storage.

---

## Features

### Query
- **Query** — SQL editor with tabs, history, favorites, formatting, and result paging. One-click cost **Estimate** projects bytes-to-be-read before running, plus EXPLAIN Plan / Pipeline / Estimate.
- **Schema** — table explorer: browse databases, tables, columns, engines, and DDL.
- **Slow Queries** — completed queries ranked by duration over a selectable window, with the full cost picture per query.
- **Running Queries** — live view of active processes: elapsed time, rows/bytes read, memory, and threads; one-click kill for runaway queries.
- **Failed Queries** — every query that ended with an exception: error code, full message, failures over time, by user, and by error code.
- **Most Expensive** — top queries ranked by memory, duration, rows, data read, or CPU, with record-breaker cards.
- **Query Analyzer** — paste or click a Query ID for a full per-query deep dive: timing, resources, threads, and the exact SQL.

### Monitoring
- **Health Dashboard** — five cluster signals (replication, mutations, disk, errors, load) scored 0–100 into one overall health score, tracked over time.
- **Monitor** — real-time server vitals (memory, CPU, connections, merges, replication queue) with a live process list and threshold-based alerting.
- **Cluster** — cluster topology and per-node metrics (connections, uptime, parts, queries, memory, disk); works for single-node and replicated/sharded setups.
- **Dashboard** — user-built dashboards from custom widgets.
- **User Cost** — per-user scan/runtime breakdown with trend charts; on replicated/sharded clusters, shows per-node activity by user.
- **Mutations** — track ALTER/DELETE/UPDATE mutations: progress, parts, and failures.
- **Rep. Queue** — the replication queue: pending entries, types, and errors per replica.
- **Cluster Health** — replication status, ZooKeeper/Keeper health, and error signals in one place.
- **Table Health** — per-table health score to spot problem tables early.

### Storage & Schema
- **Part Inspector** — part distribution per table: sizes, rows, and part counts.
- **Storage** — disks and storage policies with capacity usage.
- **Disk Usage** — storage treemap to see where space is going at a glance.
- **Table Activity** — hot vs cold tables by read/write activity.
- **TTL Manager** — view and manage TTL expressions for tables.
- **Dictionaries** — manage ClickHouse dictionaries: status, source, and layout.
- **Mat. Views** — materialized views: definitions and dependencies.
- **Index Analyzer** — EXPLAIN and index analysis to improve query performance.
- **ZooKeeper** — browse the ZooKeeper/Keeper tree (replicated/sharded clusters).

### Operations
- **Backup** — backup and restore workflows for ClickHouse data.
- **Log Profiler** — workload profiling from `system.query_log` or an offline log file: call counts, timings, and failure mix (pgBadger-style).
- **Branching** — table branching for safe, isolated changes.

### Security
- **Users** — manage console users and their roles (Community limit: 3 users).
- **Connections** — server-side registry of saved ClickHouse connections.
- **DB Users** — manage ClickHouse (database) users and their grants.
- **Access Audit** — review user activity across the console.
- **User Activity** — per-user timeline of actions.
- **Grant Explorer** — who can access what, across databases and tables.
- **Schema Drift** — DDL change audit: track schema changes over time.
- **Activity Log** — full UI audit trail of every action in the console.
- **Alerts** — configure thresholds and notification channels.
- **SIEM** — forward audit events to an external SIEM.
- **LDAP** — authenticate against LDAP / Active Directory with group-to-role mapping.
- **Compliance Pack** — one-click ZIP of audit evidence mapped to SOC 2 / ISO 27001 / GDPR controls.

### Settings
- **My Profile** — account details and password change.
- **Settings** — license status and system configuration.
- **Console Log** — view the server log from the UI.

---

## License

Community Edition — see [LICENSE](LICENSE). The 3-user limit is a functional
characteristic of this edition.

For enterprise licensing (unlimited users, priority support, and SLAs), contact
**support@blancobyte.com**.
