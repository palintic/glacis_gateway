import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Shipment(Base):
    """
    Represents a normalized shipment event/update.

    Preserves full history. The current state is derived by selecting
    the latest event for a given (vendor, external_id) ordered by event_time.
    """

    __tablename__ = "shipments"

    external_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    vendor: Mapped[str] = mapped_column(String(255), index=True, nullable=False)

    # State values: PICKED_UP, IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED
    state: Mapped[str] = mapped_column(String(50), nullable=False)

    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
    )

    container_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    raw_payload_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("raw_events.id", ondelete="CASCADE"),
        nullable=False,
    )
