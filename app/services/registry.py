import hashlib
import json

import structlog

from app.services.llm import (
    EntityType,
    InvoiceExtracted,
    InvoiceState,
    NormalizationResult,
    ShipmentExtracted,
    ShipmentState,
)

logger = structlog.get_logger()


def _resolve_path(payload: dict, path: str) -> str | None:
    """Resolve a dot-notation path in a nested dict. Returns string value or None."""
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return str(current) if current is not None else None


def _match_state(text: str, state_map: dict) -> str | None:
    """Case-insensitive substring match against state map keys."""
    lower = text.lower()
    for keyword, state in state_map.items():
        if keyword.lower() in lower:
            return state
    return None


def _parse_amount(value: str, fmt: str | None) -> float:
    """Parse a monetary amount string, handling European format (24.350,75)."""
    parts = value.strip().split()
    # Take last token — handles "EUR 24.350,75" or just "24.350,75"
    num_str = parts[-1] if len(parts) > 1 else parts[0]
    if fmt == "european":
        num_str = num_str.replace(".", "").replace(",", ".")
    else:
        num_str = num_str.replace(",", "")
    try:
        return float(num_str)
    except ValueError:
        return 0.0


def _parse_currency(value: str) -> str:
    """Extract ISO currency code from a string like 'EUR 24.350,75'."""
    parts = value.strip().split()
    if len(parts) >= 2 and len(parts[0]) == 3 and parts[0].isalpha():
        return parts[0].upper()
    return "USD"


def compute_fingerprint(payload: dict) -> str:
    """Stable 16-char fingerprint from the sorted set of top-level payload keys."""
    keys = sorted(payload.keys())
    return hashlib.sha256(json.dumps(keys).encode()).hexdigest()[:16]


def apply_spec(payload: dict, spec: dict) -> NormalizationResult | None:
    """
    Apply a stored extraction spec to a payload deterministically.
    Returns None on any failure — caller should fall back to LLM.
    """
    try:
        entity_type = spec.get("entity_type")

        if entity_type == "UNCLASSIFIED":
            return NormalizationResult(
                entity_type=EntityType.UNCLASSIFIED,
                confidence=1.0,
                reasoning="Matched stored UNCLASSIFIED vendor schema.",
            )

        vendor = spec.get("vendor_value") or _resolve_path(payload, spec.get("vendor_path") or "")
        if not vendor:
            logger.warning("registry.apply_spec.no_vendor")
            return None

        state_text = _resolve_path(payload, spec.get("state_text_path") or "")
        if not state_text:
            logger.warning("registry.apply_spec.no_state_text", path=spec.get("state_text_path"))
            return None

        state_value = _match_state(state_text, spec.get("state_map", {}))
        if not state_value:
            # New event type from a known vendor — let LLM handle it
            logger.info("registry.apply_spec.state_map_miss", state_text=state_text)
            return None

        event_time = _resolve_path(payload, spec.get("event_time_path") or "")
        if not event_time:
            for alt in spec.get("event_time_alt_paths", []):
                event_time = _resolve_path(payload, alt)
                if event_time:
                    break

        if not event_time:
            logger.warning("registry.apply_spec.no_event_time")
            return None

        if entity_type == "SHIPMENT":
            external_id = _resolve_path(payload, spec.get("external_id_path") or "")
            if not external_id:
                return None
            container_id = (
                _resolve_path(payload, spec["container_id_path"]) if spec.get("container_id_path") else None
            )
            return NormalizationResult(
                entity_type=EntityType.SHIPMENT,
                confidence=0.95,
                reasoning="Applied stored vendor schema spec (no LLM call).",
                shipment_data=ShipmentExtracted(
                    external_id=external_id,
                    vendor=vendor,
                    state=ShipmentState(state_value),
                    event_time=event_time,
                    container_id=container_id,
                ),
            )

        if entity_type == "INVOICE":
            invoice_number = _resolve_path(payload, spec.get("invoice_number_path") or "")
            if not invoice_number:
                return None
            amount_raw = _resolve_path(payload, spec.get("amount_path") or "")
            currency = _parse_currency(amount_raw) if amount_raw else "USD"
            amount = _parse_amount(amount_raw, spec.get("amount_format")) if amount_raw else 0.0
            return NormalizationResult(
                entity_type=EntityType.INVOICE,
                confidence=0.95,
                reasoning="Applied stored vendor schema spec (no LLM call).",
                invoice_data=InvoiceExtracted(
                    invoice_number=invoice_number,
                    vendor=vendor,
                    state=InvoiceState(state_value),
                    currency=currency,
                    amount=amount,
                    event_time=event_time,
                ),
            )

    except Exception as e:
        logger.warning("registry.apply_spec.error", error=str(e))

    return None
