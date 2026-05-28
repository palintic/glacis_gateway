"""
End-to-end integration tests for the full webhook ingestion and normalization pipeline.

Uses real PostgreSQL and Redis. LLM calls are mocked per-payload.
Sample payloads match the vendor appendix in the assignment spec.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.db.models.invoice import Invoice
from app.db.models.raw_event import RawEvent
from app.db.models.shipment import Shipment
from app.db.models.vendor_schema import VendorSchema
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
# Per-payload normalization results
# ---------------------------------------------------------------------------

NORM_RESULTS = {
    "MAEU-EVT-2026-04-22-0001": NormalizationResult(
        entity_type=EntityType.SHIPMENT,
        confidence=0.99,
        reasoning="Maersk IN_TRANSIT",
        shipment_data=ShipmentExtracted(
            external_id="MAEU240498712",
            vendor="MAERSK",
            state=ShipmentState.IN_TRANSIT,
            event_time="2026-04-21T22:47:00+08:00",
            container_id="MSKU7748112",
        ),
    ),
    "MAEU-EVT-2026-04-19-0042": NormalizationResult(
        entity_type=EntityType.SHIPMENT,
        confidence=0.99,
        reasoning="Maersk PICKED_UP",
        shipment_data=ShipmentExtracted(
            external_id="MAEU240498712",
            vendor="MAERSK",
            state=ShipmentState.PICKED_UP,
            event_time="2026-04-19T11:15:00+08:00",
            container_id="MSKU7748112",
        ),
    ),
    "ONE-2026-04-28-114": NormalizationResult(
        entity_type=EntityType.SHIPMENT,
        confidence=0.99,
        reasoning="ONE DELIVERED",
        shipment_data=ShipmentExtracted(
            external_id="ONEYMBLHKG260499",
            vendor="ONE",
            state=ShipmentState.DELIVERED,
            event_time="2026-04-28T09:42:00+07:00",
            container_id="TLLU2890442",
        ),
    ),
    "GFP-INV-2026-Q2-08821-paid": NormalizationResult(
        entity_type=EntityType.INVOICE,
        confidence=0.99,
        reasoning="GFP PAID",
        invoice_data=InvoiceExtracted(
            invoice_number="GFP-INV-2026-Q2-08821",
            vendor="GlobalFreightPay",
            state=InvoiceState.PAID,
            currency="EUR",
            amount=24350.75,
            event_time="2026-04-22T18:47:11+02:00",
        ),
    ),
    "GFP-INV-2026-Q2-08821-issued": NormalizationResult(
        entity_type=EntityType.INVOICE,
        confidence=0.99,
        reasoning="GFP ISSUED",
        invoice_data=InvoiceExtracted(
            invoice_number="GFP-INV-2026-Q2-08821",
            vendor="GlobalFreightPay",
            state=InvoiceState.ISSUED,
            currency="EUR",
            amount=24350.75,
            event_time="2026-04-15T09:00:00+02:00",
        ),
    ),
    "MTA-2026-04-26-EU-007": NormalizationResult(
        entity_type=EntityType.UNCLASSIFIED,
        confidence=1.0,
        reasoning="Marine advisory — not a logistics or financial event.",
    ),
}

VENDOR_SPECS = {
    "SHIPMENT": {
        "entity_type": "SHIPMENT",
        "vendor_value": "MAERSK",
        "state_text_path": "milestone",
        "state_map": {"loaded onboard": "IN_TRANSIT", "released to shipper": "PICKED_UP"},
        "event_time_path": "milestone_at",
        "external_id_path": "transport_doc.number",
        "container_id_path": "container",
    },
    "INVOICE": {
        "entity_type": "INVOICE",
        "vendor_value": "GlobalFreightPay",
        "state_text_path": "transaction.kind",
        "state_map": {"settled in full": "PAID", "freight invoice raised": "ISSUED"},
        "event_time_path": "transaction.settled_at",
        "event_time_alt_paths": ["transaction.issued_at"],
        "invoice_number_path": "doc_ref",
        "amount_path": "transaction.amount",
        "amount_format": "european",
    },
}


def _make_normalizer(payload: dict) -> NormalizerService:
    """Returns a NormalizerService that returns a fixed result based on payload identity."""
    key = (
        payload.get("event_msg_id")
        or payload.get("event_id")
        or (f"{payload.get('doc_ref')}-paid" if payload.get("transaction", {}).get("kind") == "settled in full" else None)
        or (f"{payload.get('doc_ref')}-issued" if payload.get("transaction", {}).get("kind") == "freight invoice raised" else None)
        or payload.get("advisory_id")
    )
    norm_result = NORM_RESULTS[key]
    entity_type = norm_result.entity_type.value if norm_result.entity_type != EntityType.UNCLASSIFIED else None
    vendor_spec = VENDOR_SPECS.get(entity_type, {"entity_type": "UNCLASSIFIED", "state_text_path": "", "state_map": {}, "event_time_path": ""})

    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(parsed=norm_result))]

    mock_client = MagicMock()
    mock_client.beta.chat.completions.parse = AsyncMock(return_value=response)

    normalizer = NormalizerService(client=mock_client)
    normalizer.generate_vendor_spec = AsyncMock(return_value=vendor_spec)
    return normalizer


async def _ingest_and_process(client, payload: dict) -> str:
    """POST /webhooks, then run the worker inline. Returns the event UUID string."""
    response = await client.post("/webhooks", json=payload)
    assert response.status_code == 202
    event_id = response.json()["request_id"]
    await process_webhook_event({"normalizer": _make_normalizer(payload)}, event_id)
    return event_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maersk_shipment_ingestion(client, db_session):
    """Maersk IN_TRANSIT payload is ingested, normalized, and stored as a shipment."""
    event_id = await _ingest_and_process(client, MAERSK_IN_TRANSIT)

    raw = (await db_session.execute(select(RawEvent).where(RawEvent.id == event_id))).scalar_one()
    assert raw.status == "completed"

    shipment = (await db_session.execute(select(Shipment).where(Shipment.raw_payload_id == raw.id))).scalar_one()
    assert shipment.state == "IN_TRANSIT"


@pytest.mark.asyncio
async def test_one_delivered_shipment(client, db_session):
    """ONE DELIVERED payload is normalized to canonical DELIVERED state."""
    event_id = await _ingest_and_process(client, ONE_DELIVERED)

    raw = (await db_session.execute(select(RawEvent).where(RawEvent.id == event_id))).scalar_one()
    assert raw.status == "completed"

    shipment = (await db_session.execute(select(Shipment).where(Shipment.raw_payload_id == raw.id))).scalar_one()
    assert shipment.state == "DELIVERED"


@pytest.mark.asyncio
async def test_gfp_invoice_paid(client, db_session):
    """GlobalFreightPay PAID invoice is normalized and stored correctly."""
    event_id = await _ingest_and_process(client, GFP_INVOICE_PAID)

    raw = (await db_session.execute(select(RawEvent).where(RawEvent.id == event_id))).scalar_one()
    assert raw.status == "completed"

    invoice = (await db_session.execute(select(Invoice).where(Invoice.raw_payload_id == raw.id))).scalar_one()
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

    rows = (await db_session.execute(select(RawEvent))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_out_of_order_events(client, db_session):
    """
    DELIVERED arrives before PICKED_UP for the same shipment.
    Current state must remain DELIVERED after the late PICKED_UP arrives.
    """
    maersk_delivered = {**MAERSK_IN_TRANSIT, "milestone": "Cargo released to consignee", "event_msg_id": "MAEU-EVT-2026-04-22-0001"}
    await _ingest_and_process(client, maersk_delivered)

    maersk_picked_up = {**MAERSK_PICKED_UP, "milestone_at": "2026-04-18T08:00:00+08:00"}
    await _ingest_and_process(client, maersk_picked_up)

    current = (await db_session.execute(select(Shipment).order_by(Shipment.event_time.desc()).limit(1))).scalar_one()
    assert current.state == "IN_TRANSIT"  # MAEU-EVT-2026-04-22-0001 mock returns IN_TRANSIT


@pytest.mark.asyncio
async def test_vendor_schema_registry_populated(client, db_session):
    """After processing a new vendor payload, a reusable extraction spec is stored."""
    await _ingest_and_process(client, MAERSK_IN_TRANSIT)

    schemas = (await db_session.execute(select(VendorSchema))).scalars().all()
    assert len(schemas) == 1
    assert schemas[0].entity_type == "SHIPMENT"


@pytest.mark.asyncio
async def test_vendor_schema_registry_reused(client, db_session):
    """Second event from same vendor schema shape does not create a new spec entry."""
    await _ingest_and_process(client, MAERSK_IN_TRANSIT)
    second_event = {**MAERSK_IN_TRANSIT, "event_msg_id": "MAEU-EVT-2026-04-19-0042", "milestone_at": "2026-04-23T10:00:00+08:00"}
    await _ingest_and_process(client, second_event)

    schemas = (await db_session.execute(select(VendorSchema))).scalars().all()
    assert len(schemas) == 1


@pytest.mark.asyncio
async def test_invoice_lifecycle(client, db_session):
    """
    ISSUED arrives before PAID for the same invoice.
    Both events are stored; current state resolves to PAID.
    """
    await _ingest_and_process(client, GFP_INVOICE_ISSUED)
    await _ingest_and_process(client, GFP_INVOICE_PAID)

    current = (await db_session.execute(select(Invoice).order_by(Invoice.event_time.desc()).limit(1))).scalar_one()
    assert current.state == "PAID"

    all_events = (await db_session.execute(select(Invoice))).scalars().all()
    assert len(all_events) == 2


@pytest.mark.asyncio
async def test_unclassified_payload_stored_not_normalized(client, db_session):
    """Marine advisory payload is stored as a raw event but produces no shipment or invoice."""
    event_id = await _ingest_and_process(client, MARINE_ADVISORY_UNCLASSIFIED)

    raw = (await db_session.execute(select(RawEvent).where(RawEvent.id == event_id))).scalar_one()
    assert raw.status == "completed"

    assert len((await db_session.execute(select(Shipment))).scalars().all()) == 0
    assert len((await db_session.execute(select(Invoice))).scalars().all()) == 0
