import hashlib
import json
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.queue import get_redis
from app.db.queries import get_raw_event_by_hash, get_raw_event_by_vendor_event_id, insert_raw_event
from app.db.session import get_db

logger = structlog.get_logger()

router = APIRouter()


class IngestResponse(BaseModel):
    status: str = Field(..., description="The status of the ingestion request")
    request_id: uuid.UUID = Field(..., description="The unique identifier for the ingested event")


def generate_payload_hash(payload: dict[str, Any]) -> str:
    """
    Generate a deterministic SHA-256 hash of a dictionary by sorting keys.
    """
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def extract_vendor_event_id(payload: dict[str, Any]) -> str | None:
    """
    Attempt to extract standard vendor event identifiers from typical payload keys.
    """
    # Check top-level typical keys
    candidates = [
        "event_id",
        "eventId",
        "eventID",
        "id",
        "uuid",
        "guid",
        "request_id",
        "requestId",
        "message_id",
        "messageId",
    ]
    for key in candidates:
        val = payload.get(key)
        if val and isinstance(val, (str, int)):
            return str(val)

    # Check common nested structures, e.g., metadata or header
    for parent in ["metadata", "header", "headers", "context"]:
        sub = payload.get(parent)
        if isinstance(sub, dict):
            for key in candidates:
                val = sub.get(key)
                if val and isinstance(val, (str, int)):
                    return str(val)

    return None


@router.post(
    "/webhooks",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest vendor webhook event",
)
async def ingest_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """
    Accepts arbitrary JSON payloads from external vendors.

    Calculates payload hash and vendor event ID, performs immediate database-level
    deduplication, and enqueues a background normalization job.
    """
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload must be a JSON object",
        )

    payload_hash = generate_payload_hash(payload)
    vendor_event_id = extract_vendor_event_id(payload)

    existing_event = await get_raw_event_by_hash(db, payload_hash)

    if not existing_event and vendor_event_id:
        existing_event = await get_raw_event_by_vendor_event_id(db, vendor_event_id)

    if existing_event:
        logger.info(
            "Duplicate webhook payload detected.",
            event_id=str(existing_event.id),
            vendor_event_id=vendor_event_id,
            payload_hash=payload_hash,
        )
        return IngestResponse(status="accepted", request_id=existing_event.id)

    # 3. Store raw payload in the database
    new_event = await insert_raw_event(db, payload, payload_hash, vendor_event_id)
    await db.commit()  # commit before enqueue — worker must not race an uncommitted row

    # 4. Enqueue background task
    # arq enqueues job with string name matching task in worker settings
    await redis.enqueue_job("process_webhook_event", str(new_event.id))

    logger.info(
        "Ingested webhook event successfully.",
        event_id=str(new_event.id),
        vendor_event_id=vendor_event_id,
    )

    return IngestResponse(status="accepted", request_id=new_event.id)
