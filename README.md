# BlancoByte ClickHouse Console — Community Edition

A multi-user, web-based operations console for ClickHouse: SQL workbench, live
monitoring, per-query and per-user cost analysis, cluster topology,
mutations/replication tooling, and role-based access control.

Hundreds of developers can connect concurrently. Every action and audit event
is written to a PostgreSQL metadata database (recommended on a separate
server), and session management is handled by Redis.

---
<img width="696" height="654" alt="Screenshot 2026-08-15 at 13 24 46" src="https://github.com/user-attachments/assets/46b76bb3-64a1-4e0d-8099-fdd27da28bcd" />

<img width="1324" height="829" alt="Screenshot 2026-08-15 at 13 25 23" src="https://github.com/user-attachments/assets/25950329-6f69-4056-b02d-a0249f1ac1dd" />


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
| Python | 3.10 or newer | With the `venv` module (`python3-venv` on Debian/Ubuntu) |
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

### 3. Configure PostgreSQL, Redis, and the master key

The repository ships a template named **`.env.example`**. Copy it to **`.env`**
in the project root (the same folder as `app.py`) and fill in your values. This
`.env` file is where all connection parameters go.

> `.env` and `.env.example` are **dotfiles** (names starting with a `.`), so
> they are hidden by default in Finder — press `Cmd+Shift+.` to show them, or
> use `ls -la` in a terminal. `.env` is not committed to git (it holds secrets).

```bash
cp .env.example .env
```

Edit **`.env`**:

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

### 4. Run the installer

After the parameters are saved in `.env`, run the installer (creates the
virtualenv and installs dependencies):

```bash
chmod +x install.sh
./install.sh
```

### 5. Start the server

`run-prod.sh` loads `.env` automatically and starts the server on port 5000:

```bash
./run-prod.sh
```

### 6. Log in

Open the console in a browser and log in with the default administrator:
username: admin
password: admin123

**Change this password immediately after first login** from
**My Profile → account & password**. You can then create up to 3 total users
(Community limit) under **Security → Users**.

For production, run behind nginx with TLS (a systemd unit reading
`EnvironmentFile=.../.env` is provided). See `deploy/` and `INSTALLATION.md`.

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
