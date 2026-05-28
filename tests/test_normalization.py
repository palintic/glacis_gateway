from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.db.models.raw_event import RawEvent
from app.db.models.shipment import Shipment
from app.services.llm import (
    EntityType,
    InvoiceExtracted,
    InvoiceState,
    NormalizationResult,
    NormalizerService,
    ShipmentExtracted,
    ShipmentState,
)
from app.workers.tasks import process_webhook_event


def mock_normalizer(result: NormalizationResult) -> NormalizerService:
    """Returns a NormalizerService backed by a mock OpenAI client."""
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(parsed=result))]

    mock_client = MagicMock()
    mock_client.beta.chat.completions.parse = AsyncMock(return_value=response)

    return NormalizerService(client=mock_client)


SHIPMENT_RESULT = NormalizationResult(
    entity_type=EntityType.SHIPMENT,
    confidence=0.99,
    reasoning="Shipment update detected.",
    shipment_data=ShipmentExtracted(
        external_id="SH-12345",
        vendor="ONE",
        state=ShipmentState.DELIVERED,
        event_time="2026-04-21T10:00:00Z",
        container_id="MSKU9988112",
    ),
)

INVOICE_RESULT = NormalizationResult(
    entity_type=EntityType.INVOICE,
    confidence=0.99,
    reasoning="Invoice payment detected.",
    invoice_data=InvoiceExtracted(
        invoice_number="INV-009988",
        vendor="GlobalFreightPay",
        state=InvoiceState.PAID,
        currency="EUR",
        amount=1500.75,
        event_time="2026-04-22T18:00:00Z",
    ),
)


@pytest.mark.asyncio
async def test_normalize_shipment():
    service = mock_normalizer(SHIPMENT_RESULT)
    result = await service.normalize_event({"shipment_id": "SH-12345"})

    assert result.entity_type == EntityType.SHIPMENT
    assert result.shipment_data is not None
    assert result.shipment_data.external_id == "SH-12345"
    assert result.shipment_data.vendor == "ONE"
    assert result.shipment_data.state == ShipmentState.DELIVERED
    assert result.shipment_data.container_id == "MSKU9988112"


@pytest.mark.asyncio
async def test_normalize_invoice():
    service = mock_normalizer(INVOICE_RESULT)
    result = await service.normalize_event({"invoice_number": "INV-009988"})

    assert result.entity_type == EntityType.INVOICE
    assert result.invoice_data is not None
    assert result.invoice_data.invoice_number == "INV-009988"
    assert result.invoice_data.vendor == "GlobalFreightPay"
    assert result.invoice_data.state == InvoiceState.PAID
    assert result.invoice_data.currency == "EUR"
    assert result.invoice_data.amount == 1500.75


@pytest.mark.asyncio
async def test_worker_processing_pipeline(db_session):
    """Verifies complete worker loop: pending event -> task -> completed + shipment saved."""
    payload = {
        "shipment_id": "SH-554433",
        "container_id": "MSKU4433221",
        "status": "Cargo picked up",
        "vendor": "MAERSK",
        "event_time": "2026-05-27T12:00:00Z",
    }
    raw_event = RawEvent(
        payload=payload,
        payload_hash="test_hash_pipeline",
        vendor_event_id="evt_sh_554433",
        status="pending",
    )
    db_session.add(raw_event)
    await db_session.commit()

    norm_result = NormalizationResult(
        entity_type=EntityType.SHIPMENT,
        confidence=0.99,
        reasoning="test",
        shipment_data=ShipmentExtracted(
            external_id="SH-554433",
            vendor="MAERSK",
            state=ShipmentState.PICKED_UP,
            event_time="2026-05-27T12:00:00Z",
            container_id="MSKU4433221",
        ),
    )
    normalizer = mock_normalizer(norm_result)
    normalizer.generate_vendor_spec = AsyncMock(
        return_value={
            "entity_type": "SHIPMENT",
            "vendor_value": "MAERSK",
            "state_text_path": "status",
            "state_map": {"picked up": "PICKED_UP"},
            "event_time_path": "event_time",
            "external_id_path": "shipment_id",
            "container_id_path": "container_id",
        }
    )

    from app.workers import tasks

    original_sessionmaker = tasks.AsyncSessionLocal

    class MockSessionMaker:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        def __call__(self):
            return self

    tasks.AsyncSessionLocal = MockSessionMaker()

    try:
        result = await process_webhook_event({"normalizer": normalizer}, str(raw_event.id))
        assert "Processed event" in result

        await db_session.refresh(raw_event)
        assert raw_event.status == "completed"

        query = select(Shipment).where(Shipment.raw_payload_id == raw_event.id)
        shipment = (await db_session.execute(query)).scalar_one_or_none()

        assert shipment is not None
        assert shipment.external_id == "SH-554433"
        assert shipment.vendor == "MAERSK"
        assert shipment.state == "PICKED_UP"
        assert shipment.container_id == "MSKU4433221"
    finally:
        tasks.AsyncSessionLocal = original_sessionmaker
