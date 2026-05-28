import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Invoice(Base):
    """
    Represents a normalized invoice event/update.

    Preserves full history. The current state is derived by selecting
    the latest event for a given (vendor, invoice_number) ordered by event_time.
    """

    __tablename__ = "invoices"

    invoice_number: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    vendor: Mapped[str] = mapped_column(String(255), index=True, nullable=False)

    # State values: ISSUED, PAID, VOIDED, REFUNDED
    state: Mapped[str] = mapped_column(String(50), nullable=False)

    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)

    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
    )

    raw_payload_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("raw_events.id", ondelete="CASCADE"),
        nullable=False,
    )
