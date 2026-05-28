import uuid
from datetime import datetime
from typing import Any

import structlog
from arq.connections import RedisSettings

from app.config import settings
from app.db.queries import get_raw_event_by_id, get_vendor_schema, insert_invoice, insert_shipment, insert_vendor_schema
from app.db.session import AsyncSessionLocal, engine
from app.services.llm import EntityType, NormalizerService
from app.services.registry import apply_spec, compute_fingerprint

logger = structlog.get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    ctx["normalizer"] = NormalizerService()
    logger.info("background_worker.started")


async def shutdown(ctx: dict[str, Any]) -> None:
    await engine.dispose()
    logger.info("background_worker.stopped")


async def process_webhook_event(ctx: dict[str, Any], event_id_str: str) -> str:
    """
    arq task to process a single webhook event.

    Fetches the raw payload, executes LLM classification/normalization,
    and commits canonical updates inside a transaction.
    """
    event_id = uuid.UUID(event_id_str)
    normalizer: NormalizerService = ctx["normalizer"]

    async with AsyncSessionLocal() as db:
        event = await get_raw_event_by_id(db, event_id)

        if not event:
            logger.error("webhook_event.not_found", event_id=event_id_str)
            return f"Event {event_id_str} not found"

        if event.status == "completed":
            logger.info("webhook_event.already_completed", event_id=event_id_str)
            return f"Event {event_id_str} already completed"

        logger.info("webhook_event.processing", event_id=event_id_str)
        event.status = "processing"
        await db.commit()

        try:
            # 1. Normalize: try registry first, fall back to LLM
            fingerprint = compute_fingerprint(event.payload)
            spec = await get_vendor_schema(db, fingerprint)
            norm_result = None

            if spec:
                norm_result = apply_spec(event.payload, spec)
                if norm_result:
                    logger.info("webhook_event.registry_hit", event_id=event_id_str, fingerprint=fingerprint)
                else:
                    logger.info("webhook_event.registry_miss", event_id=event_id_str, reason="state_map_miss")

            if norm_result is None:
                norm_result = await normalizer.normalize_event(event.payload)
                if spec is None and norm_result.entity_type != EntityType.UNCLASSIFIED:
                    vendor_spec = await normalizer.generate_vendor_spec(event.payload, norm_result)
                    await insert_vendor_schema(db, fingerprint, vendor_spec, event.id)
                    logger.info("webhook_event.spec_generated", event_id=event_id_str, fingerprint=fingerprint)

            # 2. Persist normalized state
            if norm_result.entity_type == EntityType.SHIPMENT and norm_result.shipment_data:
                d = norm_result.shipment_data
                await insert_shipment(
                    db,
                    external_id=d.external_id,
                    vendor=d.vendor,
                    state=d.state.value if hasattr(d.state, "value") else d.state,
                    event_time=datetime.fromisoformat(d.event_time.replace("Z", "+00:00")),
                    raw_payload_id=event.id,
                    container_id=d.container_id,
                )
            elif norm_result.entity_type == EntityType.INVOICE and norm_result.invoice_data:
                d = norm_result.invoice_data
                await insert_invoice(
                    db,
                    invoice_number=d.invoice_number,
                    vendor=d.vendor,
                    state=d.state.value if hasattr(d.state, "value") else d.state,
                    currency=d.currency,
                    amount=d.amount,
                    event_time=datetime.fromisoformat(d.event_time.replace("Z", "+00:00")),
                    raw_payload_id=event.id,
                )
            elif norm_result.entity_type == EntityType.UNCLASSIFIED:
                logger.warning("webhook_event.unclassified", event_id=event_id_str, reasoning=norm_result.reasoning)
            else:
                logger.error(
                    "webhook_event.unexpected_classification",
                    event_id=event_id_str,
                    entity_type=norm_result.entity_type,
                )

            event.status = "completed"
            await db.commit()
            logger.info("webhook_event.processed", event_id=event_id_str)
            return f"Processed event {event_id_str} successfully"

        except Exception as e:
            await db.rollback()
            event.status = "failed"
            await db.commit()
            logger.exception("webhook_event.failed", event_id=event_id_str, error=str(e))
            raise


class WorkerSettings:
    functions = [process_webhook_event]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    max_jobs = 10
    job_timeout = 60
    keep_result = 3600


if __name__ == "__main__":
    import asyncio
    from arq import run_worker

    asyncio.run(run_worker(WorkerSettings))
