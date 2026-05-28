from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RawEvent(Base):
    __tablename__ = "raw_events"

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )

    # SHA-256 hash of the payload to detect exact duplicates
    payload_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=False,
    )

    # Optional vendor event identifier if extracted from the payload
    vendor_event_id: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        default="pending",
        server_default="pending",
        nullable=False,
    )

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
