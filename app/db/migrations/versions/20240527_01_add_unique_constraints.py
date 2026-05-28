"""Add explicit unique constraints to raw_events table.

This migration ensures that the database enforces uniqueness for the
`payload_hash` and `vendor_event_id` columns, protecting against race
conditions where two concurrent webhook deliveries could be inserted
before the ORM-level check runs.
"""

from alembic import op

# Revision identifiers, used by Alembic.
revision = "20240527_01"
down_revision = "e0f7e6f658c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add a unique constraint for payload_hash (already indexed uniquely, but explicitly
    # declaring a constraint aids clarity and works across DB back‑ends).
    op.create_unique_constraint("uq_raw_events_payload_hash", "raw_events", ["payload_hash"])
    # Add a unique constraint for vendor_event_id.
    op.create_unique_constraint("uq_raw_events_vendor_event_id", "raw_events", ["vendor_event_id"])


def downgrade() -> None:
    op.drop_constraint("uq_raw_events_payload_hash", "raw_events", type_="unique")
    op.drop_constraint("uq_raw_events_vendor_event_id", "raw_events", type_="unique")
