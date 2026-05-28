import uuid
from datetime import datetime

import pytest

from app.db.queries import get_current_invoice_state, get_current_shipment_state, insert_invoice, insert_shipment
from app.services.llm import InvoiceState, ShipmentState


@pytest.mark.asyncio
async def test_shipment_out_of_order_preservation(db_session):
    """Verifies that late-arriving stale updates are stored, but do not regress the active state projection."""
    vendor = "MAERSK"
    external_id = "SH-990011"
    raw_payload_id = uuid.uuid4()

    # 1. Chronologically LATEST event arrives FIRST: DELIVERED at 12:00
    await insert_shipment(
        db_session,
        external_id=external_id,
        vendor=vendor,
        state=ShipmentState.DELIVERED.value,
        event_time=datetime.fromisoformat("2026-05-27T12:00:00+00:00"),
        raw_payload_id=raw_payload_id,
        container_id="MSKU112233",
    )

    current = await get_current_shipment_state(db_session, vendor, external_id)
    assert current is not None
    assert current.state == "DELIVERED"

    # 2. Chronologically EARLIER event arrives SECOND (late delivery): IN_TRANSIT at 10:00
    await insert_shipment(
        db_session,
        external_id=external_id,
        vendor=vendor,
        state=ShipmentState.IN_TRANSIT.value,
        event_time=datetime.fromisoformat("2026-05-27T10:00:00+00:00"),
        raw_payload_id=raw_payload_id,
        container_id="MSKU112233",
    )

    # 3. Late arrival stored historically, but current state is STILL DELIVERED
    current_after = await get_current_shipment_state(db_session, vendor, external_id)
    assert current_after is not None
    assert current_after.state == "DELIVERED"  # Did not regress to IN_TRANSIT!


@pytest.mark.asyncio
async def test_invoice_out_of_order_preservation(db_session):
    """Verifies that late-arriving stale invoice updates do not regress current active status."""
    vendor = "GlobalFreightPay"
    invoice_number = "INV-776655"
    raw_payload_id = uuid.uuid4()

    # 1. Chronologically LATEST event arrives FIRST: PAID at 15:00
    await insert_invoice(
        db_session,
        invoice_number=invoice_number,
        vendor=vendor,
        state=InvoiceState.PAID.value,
        currency="USD",
        amount=5000.00,
        event_time=datetime.fromisoformat("2026-05-27T15:00:00+00:00"),
        raw_payload_id=raw_payload_id,
    )

    current = await get_current_invoice_state(db_session, vendor, invoice_number)
    assert current is not None
    assert current.state == "PAID"

    # 2. Chronologically EARLIER event arrives SECOND: ISSUED at 14:00
    await insert_invoice(
        db_session,
        invoice_number=invoice_number,
        vendor=vendor,
        state=InvoiceState.ISSUED.value,
        currency="USD",
        amount=5000.00,
        event_time=datetime.fromisoformat("2026-05-27T14:00:00+00:00"),
        raw_payload_id=raw_payload_id,
    )

    # 3. Current active state remains PAID
    current_after = await get_current_invoice_state(db_session, vendor, invoice_number)
    assert current_after is not None
    assert current_after.state == "PAID"  # Did not regress!
