# BlancoByte ClickHouse Console — Community Edition

A multi-user, web-based operations console for ClickHouse: SQL workbench,
monitoring dashboards, per-query & per-user cost analysis, cluster topology,
mutations/replication tooling, and role-based access control.

Hundreds of developers can connect concurrently; every action and audit event
is written to a PostgreSQL metadata database (on a separate server), and
session management is handled by Redis.

## Community vs Enterprise

The Community Edition is fully functional and free to self-host, limited to
**3 console users**. Enterprise adds unlimited users, LDAP/Active Directory
and SSO login, SIEM forwarding, the one-click compliance pack (SOC 2 / ISO
27001 / GDPR evidence), and priority support. See `LICENSE`.

## Features

- **Query** — SQL editor with tabs, history, favorites, formatting, result
  paging, one-click cost **Estimate** (bytes-to-be-read, no execution), and
  EXPLAIN Plan / Pipeline / Estimate.
- **Observability** — Running / Slow / Failed / Most Expensive queries
  (rank by memory, duration, rows, data read, CPU), Query Analyzer by ID.
- **Monitoring** — health score, live Monitor with alerting, per-user cost
  breakdown incl. per-node activity on replicated/sharded clusters.
- **Cluster** — topology, node metrics (connections, uptime, parts…),
  mutations, replication queue, ZooKeeper browser, table health.
- **Security** — 4 server-enforced roles, encrypted credential store,
  full audit trail.

## Requirements

- Python 3.11+
- PostgreSQL (metadata store, separate server recommended)
- Redis (session store)
- A reachable ClickHouse server with system tables enabled (`query_log`, etc.)

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # then edit DB_* and MASTER_KEY
python app.py               # serves on 0.0.0.0:5000
```

For production, run behind gunicorn + a reverse proxy (nginx). See `deploy/`.

## License

Community Edition — see [LICENSE](LICENSE). The 3-user limit is a functional
characteristic of this edition; enterprise licensing removes it.
