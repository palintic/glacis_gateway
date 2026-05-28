import uuid

import pytest
from sqlalchemy import select

from app.db.models.raw_event import RawEvent


@pytest.mark.asyncio
async def test_webhook_ingestion_success(client, db_session, mock_redis):
    """Verifies that an incoming webhook is successfully stored and enqueued."""
    payload = {
        "event_id": "evt_maersk_99182",
        "vendor": "MAERSK",
        "container_id": "MSKU7748112",
        "status": "In Gate",
        "event_time": "2026-04-21T22:47:00Z",
    }

    # 1. Trigger POST /webhooks
    response = await client.post("/webhooks", json=payload)
    assert response.status_code == 202

    data = response.json()
    assert data["status"] == "accepted"
    assert "request_id" in data

    request_id = data["request_id"]

    # 2. Verify raw event exists in database with pending status
    query = select(RawEvent).where(RawEvent.id == uuid.UUID(request_id))
    result = await db_session.execute(query)
    raw_event = result.scalar_one_or_none()

    assert raw_event is not None
    assert raw_event.status == "pending"
    assert raw_event.vendor_event_id == "evt_maersk_99182"
    assert raw_event.payload == payload

    # 3. Verify task enqueued in mock redis
    mock_redis.enqueue_job.assert_called_once_with("process_webhook_event", str(request_id))


@pytest.mark.asyncio
async def test_webhook_ingestion_deduplication(client, mock_redis):
    """Verifies that duplicate webhook payloads or vendor event IDs are rejected/deduplicated."""
    payload = {
        "event_id": "evt_gfp_881231",
        "vendor": "GlobalFreightPay",
        "invoice_number": "GFP-INV-2026-088",
        "amount": 250.50,
        "currency": "USD",
    }

    # First call - creates new raw event and queues job
    resp1 = await client.post("/webhooks", json=payload)
    assert resp1.status_code == 202
    id1 = resp1.json()["request_id"]

    mock_redis.enqueue_job.assert_called_once()
    mock_redis.reset_mock()

    # Second call (exact duplicate payload) - should return same ID and NOT queue job again
    resp2 = await client.post("/webhooks", json=payload)
    assert resp2.status_code == 202
    id2 = resp2.json()["request_id"]

    assert id1 == id2
    mock_redis.enqueue_job.assert_not_called()

    # Third call (different payload but same vendor event ID) - should also deduplicate
    payload_variant = payload.copy()
    payload_variant["amount"] = 300.00  # Changed content, same event_id

    resp3 = await client.post("/webhooks", json=payload_variant)
    assert resp3.status_code == 202
    id3 = resp3.json()["request_id"]

    assert id1 == id3
    mock_redis.enqueue_job.assert_not_called()
