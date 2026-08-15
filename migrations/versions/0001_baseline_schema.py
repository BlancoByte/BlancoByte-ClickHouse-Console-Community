"""baseline schema (current schema.sql)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-27

This baseline records the schema produced by schema.sql. That file is written to
be idempotent (every CREATE uses IF NOT EXISTS, and the ALTERs live inside
guarded DO $$ blocks), so:

  * on a FRESH database, this migration creates the full schema, and
  * on an EXISTING database (one previously created by db.apply_schema), it is a
    no-op that simply stamps the alembic_version row.

Either way, after `alembic upgrade head` the database is recorded at this
revision and subsequent changes are applied as ordinary, hand-written Alembic
migrations. (The project uses raw SQL, not ORM models, so autogenerate is not
used — new migrations call op.add_column / op.execute directly.)
"""
from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def _schema_sql() -> str:
    # migrations/versions/0001_*.py  →  parents[2] is the project root.
    root = Path(__file__).resolve().parents[2]
    return (root / "schema.sql").read_text(encoding="utf-8")


def upgrade() -> None:
    # psycopg (v3) accepts multiple statements — including the DO $$ ... $$
    # blocks — in a single driver call. This is the same mechanism
    # db.executescript() relies on, so the whole idempotent script runs as one
    # unit inside Alembic's migration transaction.
    op.get_bind().exec_driver_sql(_schema_sql())


def downgrade() -> None:
    # The baseline is intentionally NOT reversible: a downgrade would have to
    # DROP every table and would destroy all application state (users, audit
    # trail, saved connections). Refuse rather than silently wipe data.
    raise RuntimeError(
        "Refusing to downgrade past the baseline schema — this would drop every "
        "table and destroy all application data. Restore from a backup instead."
    )
