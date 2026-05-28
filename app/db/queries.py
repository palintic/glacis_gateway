import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.invoice import Invoice
from app.db.models.raw_event import RawEvent
from app.db.models.shipment import Shipment
from app.db.models.vendor_schema import VendorSchema


# ---------------------------------------------------------------------------
# RawEvent
# ---------------------------------------------------------------------------


async def get_raw_event_by_id(db: AsyncSession, event_id: uuid.UUID) -> RawEvent | None:
    result = await db.execute(select(RawEvent).where(RawEvent.id == event_id))
    return result.scalar_one_or_none()


async def get_raw_event_by_hash(db: AsyncSession, payload_hash: str) -> RawEvent | None:
    result = await db.execute(select(RawEvent).where(RawEvent.payload_hash == payload_hash))
    return result.scalar_one_or_none()


async def get_raw_event_by_vendor_event_id(db: AsyncSession, vendor_event_id: str) -> RawEvent | None:
    result = await db.execute(select(RawEvent).where(RawEvent.vendor_event_id == vendor_event_id))
    return result.scalar_one_or_none()


async def insert_raw_event(
    db: AsyncSession,
    payload: dict[str, Any],
    payload_hash: str,
    vendor_event_id: str | None,
) -> RawEvent:
    event = RawEvent(payload=payload, payload_hash=payload_hash, vendor_event_id=vendor_event_id, status="pending")
    db.add(event)
    await db.flush()
    return event


# ---------------------------------------------------------------------------
# Shipment
# ---------------------------------------------------------------------------


async def insert_shipment(
    db: AsyncSession,
    external_id: str,
    vendor: str,
    state: str,
    event_time: datetime,
    raw_payload_id: uuid.UUID,
    container_id: str | None = None,
) -> Shipment:
    shipment = Shipment(
        external_id=external_id,
        vendor=vendor,
        state=state,
        event_time=event_time,
        container_id=container_id,
        raw_payload_id=raw_payload_id,
    )
    db.add(shipment)
    await db.flush()
    return shipment


async def get_current_shipment_state(db: AsyncSession, vendor: str, external_id: str) -> Shipment | None:
    result = await db.execute(
        select(Shipment)
        .where(Shipment.vendor == vendor, Shipment.external_id == external_id)
        .order_by(Shipment.event_time.desc(), Shipment.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------


async def insert_invoice(
    db: AsyncSession,
    invoice_number: str,
    vendor: str,
    state: str,
    currency: str,
    amount: float,
    event_time: datetime,
    raw_payload_id: uuid.UUID,
) -> Invoice:
    invoice = Invoice(
        invoice_number=invoice_number,
        vendor=vendor,
        state=state,
        currency=currency,
        amount=amount,
        event_time=event_time,
        raw_payload_id=raw_payload_id,
    )
    db.add(invoice)
    await db.flush()
    return invoice


async def get_current_invoice_state(db: AsyncSession, vendor: str, invoice_number: str) -> Invoice | None:
    result = await db.execute(
        select(Invoice)
        .where(Invoice.vendor == vendor, Invoice.invoice_number == invoice_number)
        .order_by(Invoice.event_time.desc(), Invoice.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# VendorSchema
# ---------------------------------------------------------------------------


async def get_vendor_schema(db: AsyncSession, fingerprint: str) -> dict | None:
    result = await db.execute(select(VendorSchema).where(VendorSchema.schema_fingerprint == fingerprint))
    row = result.scalar_one_or_none()
    return row.extraction_spec if row else None


async def insert_vendor_schema(
    db: AsyncSession,
    fingerprint: str,
    spec: dict,
    sample_raw_payload_id: uuid.UUID | None = None,
) -> None:
    schema = VendorSchema(
        schema_fingerprint=fingerprint,
        entity_type=spec["entity_type"],
        extraction_spec=spec,
        sample_raw_payload_id=sample_raw_payload_id,
    )
    db.add(schema)
    await db.flush()
