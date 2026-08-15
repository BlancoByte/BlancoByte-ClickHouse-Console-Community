"""audit hash chain columns

Revision ID: 0002_audit_hash_chain
Revises: 0001_baseline
Create Date: 2026-06-28

Adds the tamper-evident hash-chain columns to audit_events:

  * prev_hash  — the entry_hash of the previous audit row (chain link)
  * entry_hash — sha256(prev_hash + canonical(this row)), computed at insert

Idempotent (ADD COLUMN IF NOT EXISTS): a fresh database created from the
current schema.sql already has these columns, in which case this migration is
a no-op that only advances the recorded revision; an existing database created
before the chain existed gets the columns added here. Existing rows keep NULL
hashes and are reported as 'legacy (unhashed)' by the verifier — the chain
becomes authoritative from the first row written after this migration.
"""
from alembic import op

revision = "0002_audit_hash_chain"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS prev_hash TEXT")
    op.execute("ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS entry_hash TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE audit_events DROP COLUMN IF EXISTS entry_hash")
    op.execute("ALTER TABLE audit_events DROP COLUMN IF EXISTS prev_hash")
