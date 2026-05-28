"""
End-to-end integration tests for the full webhook ingestion and normalization pipeline.

Uses real PostgreSQL and Redis. LLM calls use the heuristic mock fallback (OPENAI_API_KEY=None).
Sample payloads match the vendor appendix in the assignment spec.
"""

import pytest
from sqlalchemy import select

from app.db.models.invoice import Invoice
from app.db.models.raw_event import RawEvent
from app.db.models.shipment import Shipment
from app.db.models.vendor_schema import VendorSchema
from app.db.session import AsyncSessionLocal
from app.services.llm import NormalizerService
from app.services.registry import VendorSchemaRegistry
from app.services.state_manager import StateManagerService
from app.workers.tasks import process_webhook_event

# ---------------------------------------------------------------------------
# Sample payloads from the vendor appendix
# ---------------------------------------------------------------------------

MAERSK_IN_TRANSIT = {
    "carrier_scac": "MAEU",
    "event_msg_id": "MAEU-EVT-2026-04-22-0001",
    "transport_doc": {"type": "MBL", "number": "MAEU240498712"},
    "container": "MSKU7748112",
    "vessel": {"name": "MAERSK GUATEMALA", "imo": "9778120", "voyage": "424W"},
    "milestone": "Loaded onboard and sailed",
    "milestone_at": "2026-04-21T22:47:00+08:00",
    "port": {"code": "CNSHA", "name": "Shanghai"},
}

MAERSK_PICKED_UP = {
    "carrier_scac": "MAEU",
    "event_msg_id": "MAEU-EVT-2026-04-19-0042",
    "transport_doc": {"type": "MBL", "number": "MAEU240498712"},
    "container": "MSKU7748112",
    "milestone": "Empty container released to shipper; full container received at origin terminal",
    "milestone_at": "2026-04-19T11:15:00+08:00",
    "port": {"code": "CNSHA", "name": "Shanghai"},
    "shipper_ref": "ACME-IND-PO-2026-9921",
}

ONE_DELIVERED = {
    "carrier": "Ocean Network Express",
    "carrier_scac": "ONEY",
    "event_id": "ONE-2026-04-28-114",
    "house_bl": "ONEYJKTHKG2604113",
    "master_bl": "ONEYMBLHKG260499",
    "container_no": "TLLU2890442",
    "consignee": "ACME Manufacturing PT.",
    "milestone_text": "Cargo released to consignee at consignee facility — empty container returned to depot",
    "milestone_local_time": "28/04/2026 09:42 WIB",
    "port_of_discharge": "IDJKT",
    "delivery_order_no": "DO-IDJKT-26044881",
}

GFP_INVOICE_PAID = {
    "source": "globalfreightpay.api",
    "channel": "carrier_billing",
    "doc_ref": "GFP-INV-2026-Q2-08821",
    "carrier": "Hapag-Lloyd AG",
    "linked_bl": "HLCU2604OCEAN221",
    "transaction": {
        "kind": "settled in full",
        "settled_at": "2026-04-22 18:47:11+02:00",
        "amount": "EUR 24.350,75",
        "remitter": "ACME Logistics GmbH",
        "memo": "Ocean freight + THC + BAF, Shanghai → Hamburg, container HLBU4490221",
    },
}

GFP_INVOICE_ISSUED = {
    "source": "globalfreightpay.api",
    "channel": "carrier_billing",
    "doc_ref": "GFP-INV-2026-Q2-08821",
    "carrier": "Hapag-Lloyd AG",
    "linked_bl": "HLCU2604OCEAN221",
    "transaction": {
        "kind": "freight invoice raised",
        "issued_at": "2026-04-15T09:00:00+02:00",
        "amount": "EUR 24.350,75",
        "due_at": "2026-05-15T00:00:00+02:00",
        "line_items": [
            {"desc": "Ocean freight Shanghai → Hamburg", "amt": "EUR 21.000,00"},
            {"desc": "Terminal handling charges (THC)", "amt": "EUR 1.850,75"},
            {"desc": "Bunker adjustment factor (BAF)", "amt": "EUR 1.500,00"},
        ],
    },
}

MARINE_ADVISORY_UNCLASSIFIED = {
    "issuer": "marine-traffic-advisory",
    "advisory_id": "MTA-2026-04-26-EU-007",
    "severity": "AMBER",
    "issued_at": "2026-04-26T06:00:00Z",
    "subject": "Ongoing congestion at Port of Antwerp-Bruges",
    "body": "Vessel waiting times at Antwerp-Bruges berths have increased to 4-6 days.",
    "affected_services": ["AE7", "FAL3", "Mediterranean Bridge"],
    "expires_at": "2026-05-03T00:00:00Z",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _worker_ctx():
    return {
        "state_manager": StateManagerService(),
        "normalizer": NormalizerService(),
        "registry": VendorSchemaRegistry(),
    }


async def _ingest_and_process(client, payload: dict) -> str:
    """POST /webhooks, then run the worker inline. Returns the event UUID string."""
    response = await client.post("/webhooks", json=payload)
    assert response.status_code == 202
    event_id = response.json()["request_id"]
    await process_webhook_event(_worker_ctx(), event_id)
    return event_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maersk_shipment_ingestion(client, db_session):
    """Maersk IN_TRANSIT payload is ingested, normalized, and stored as a shipment."""
    event_id = await _ingest_and_process(client, MAERSK_IN_TRANSIT)

    result = await db_session.execute(select(RawEvent).where(RawEvent.id == event_id))
    raw = result.scalar_one()
    assert raw.status == "completed"

    result = await db_session.execute(select(Shipment).where(Shipment.raw_payload_id == raw.id))
    shipment = result.scalar_one()
    assert shipment.state == "IN_TRANSIT"


@pytest.mark.asyncio
async def test_one_delivered_shipment(client, db_session):
    """ONE DELIVERED payload is normalized to canonical DELIVERED state."""
    event_id = await _ingest_and_process(client, ONE_DELIVERED)

    result = await db_session.execute(select(RawEvent).where(RawEvent.id == event_id))
    raw = result.scalar_one()
    assert raw.status == "completed"

    result = await db_session.execute(select(Shipment).where(Shipment.raw_payload_id == raw.id))
    shipment = result.scalar_one()
    assert shipment.state == "DELIVERED"


@pytest.mark.asyncio
async def test_gfp_invoice_paid(client, db_session):
    """GlobalFreightPay PAID invoice is normalized and stored correctly."""
    event_id = await _ingest_and_process(client, GFP_INVOICE_PAID)

    result = await db_session.execute(select(RawEvent).where(RawEvent.id == event_id))
    raw = result.scalar_one()
    assert raw.status == "completed"

    result = await db_session.execute(select(Invoice).where(Invoice.raw_payload_id == raw.id))
    invoice = result.scalar_one()
    assert invoice.state == "PAID"
    assert invoice.currency == "EUR"


@pytest.mark.asyncio
async def test_deduplication(client, db_session):
    """Sending the same payload twice returns the same request_id and stores only one raw event."""
    r1 = await client.post("/webhooks", json=MAERSK_IN_TRANSIT)
    r2 = await client.post("/webhooks", json=MAERSK_IN_TRANSIT)

    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["request_id"] == r2.json()["request_id"]

    result = await db_session.execute(select(RawEvent))
    rows = result.scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_out_of_order_events(client, db_session):
    """
    DELIVERED arrives before PICKED_UP for the same shipment.
    Current state must remain DELIVERED after the late PICKED_UP arrives.
    """
    # DELIVERED arrives first (later event_time but arrives first)
    maersk_delivered = {**MAERSK_IN_TRANSIT, "milestone": "Cargo released to consignee", "event_msg_id": "MAEU-EVT-DELIVERED"}
    await _ingest_and_process(client, maersk_delivered)

    # PICKED_UP arrives second (earlier event_time but arrives late)
    maersk_picked_up = {**MAERSK_PICKED_UP, "milestone_at": "2026-04-18T08:00:00+08:00"}
    await _ingest_and_process(client, maersk_picked_up)

    result = await db_session.execute(
        select(Shipment).order_by(Shipment.event_time.desc()).limit(1)
    )
    current = result.scalar_one()
    assert current.state == "DELIVERED"


@pytest.mark.asyncio
async def test_vendor_schema_registry_populated(client, db_session):
    """After processing a new vendor payload, a reusable extraction spec is stored."""
    await _ingest_and_process(client, MAERSK_IN_TRANSIT)

    result = await db_session.execute(select(VendorSchema))
    schemas = result.scalars().all()
    assert len(schemas) == 1
    assert schemas[0].entity_type == "SHIPMENT"


@pytest.mark.asyncio
async def test_vendor_schema_registry_reused(client, db_session):
    """Second event from same vendor schema shape does not create a new spec entry."""
    await _ingest_and_process(client, MAERSK_IN_TRANSIT)
    # Second Maersk event — same schema shape, different event
    second_event = {**MAERSK_IN_TRANSIT, "event_msg_id": "MAEU-EVT-2026-04-23-9999", "milestone_at": "2026-04-23T10:00:00+08:00"}
    await _ingest_and_process(client, second_event)

    result = await db_session.execute(select(VendorSchema))
    schemas = result.scalars().all()
    assert len(schemas) == 1  # still only one spec, not two


@pytest.mark.asyncio
async def test_invoice_lifecycle(client, db_session):
    """
    ISSUED arrives before PAID for the same invoice.
    Both events are stored; current state resolves to PAID.
    """
    await _ingest_and_process(client, GFP_INVOICE_ISSUED)
    await _ingest_and_process(client, GFP_INVOICE_PAID)

    result = await db_session.execute(select(Invoice).order_by(Invoice.event_time.desc()).limit(1))
    current = result.scalar_one()
    assert current.state == "PAID"

    result = await db_session.execute(select(Invoice))
    all_events = result.scalars().all()
    assert len(all_events) == 2  # full history preserved


@pytest.mark.asyncio
async def test_unclassified_payload_stored_not_normalized(client, db_session):
    """Marine advisory payload is stored as a raw event but produces no shipment or invoice."""
    event_id = await _ingest_and_process(client, MARINE_ADVISORY_UNCLASSIFIED)

    result = await db_session.execute(select(RawEvent).where(RawEvent.id == event_id))
    raw = result.scalar_one()
    assert raw.status == "completed"

    shipments = (await db_session.execute(select(Shipment))).scalars().all()
    invoices = (await db_session.execute(select(Invoice))).scalars().all()
    assert len(shipments) == 0
    assert len(invoices) == 0
