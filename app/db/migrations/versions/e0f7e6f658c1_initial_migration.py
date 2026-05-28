"""initial migration

Revision ID: e0f7e6f658c1
Revises:
Create Date: 2026-05-27 22:15:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e0f7e6f658c1"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Create raw_events table
    op.create_table(
        "raw_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("vendor_event_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), server_default="pending", nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_raw_events_id"), "raw_events", ["id"], unique=False)
    op.create_index(op.f("ix_raw_events_payload_hash"), "raw_events", ["payload_hash"], unique=True)
    op.create_index(op.f("ix_raw_events_vendor_event_id"), "raw_events", ["vendor_event_id"], unique=True)

    # 2. Create shipments table
    op.create_table(
        "shipments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("vendor", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=50), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("container_id", sa.String(length=255), nullable=True),
        sa.Column("raw_payload_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["raw_payload_id"], ["raw_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_shipments_id"), "shipments", ["id"], unique=False)
    op.create_index(op.f("ix_shipments_external_id"), "shipments", ["external_id"], unique=False)
    op.create_index(op.f("ix_shipments_vendor"), "shipments", ["vendor"], unique=False)
    op.create_index(op.f("ix_shipments_event_time"), "shipments", ["event_time"], unique=False)

    # 3. Create invoices table
    op.create_table(
        "invoices",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("invoice_number", sa.String(length=255), nullable=False),
        sa.Column("vendor", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=50), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["raw_payload_id"], ["raw_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_invoices_id"), "invoices", ["id"], unique=False)
    op.create_index(op.f("ix_invoices_invoice_number"), "invoices", ["invoice_number"], unique=False)
    op.create_index(op.f("ix_invoices_vendor"), "invoices", ["vendor"], unique=False)
    op.create_index(op.f("ix_invoices_event_time"), "invoices", ["event_time"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_invoices_event_time"), table_name="invoices")
    op.drop_index(op.f("ix_invoices_vendor"), table_name="invoices")
    op.drop_index(op.f("ix_invoices_invoice_number"), table_name="invoices")
    op.drop_index(op.f("ix_invoices_id"), table_name="invoices")
    op.drop_table("invoices")

    op.drop_index(op.f("ix_shipments_event_time"), table_name="shipments")
    op.drop_index(op.f("ix_shipments_vendor"), table_name="shipments")
    op.drop_index(op.f("ix_shipments_external_id"), table_name="shipments")
    op.drop_index(op.f("ix_shipments_id"), table_name="shipments")
    op.drop_table("shipments")

    op.drop_index(op.f("ix_raw_events_vendor_event_id"), table_name="raw_events")
    op.drop_index(op.f("ix_raw_events_payload_hash"), table_name="raw_events")
    op.drop_index(op.f("ix_raw_events_id"), table_name="raw_events")
    op.drop_table("raw_events")
