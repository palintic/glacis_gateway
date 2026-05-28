import json
from enum import Enum

import structlog
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.config import settings

logger = structlog.get_logger()


class EntityType(str, Enum):
    SHIPMENT = "SHIPMENT"
    INVOICE = "INVOICE"
    UNCLASSIFIED = "UNCLASSIFIED"


class ShipmentState(str, Enum):
    PICKED_UP = "PICKED_UP"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"


class InvoiceState(str, Enum):
    ISSUED = "ISSUED"
    PAID = "PAID"
    VOIDED = "VOIDED"
    REFUNDED = "REFUNDED"


class ShipmentExtracted(BaseModel):
    external_id: str = Field(
        ..., description="Unique reference ID of the shipment, e.g., Booking Number or Bill of Lading."
    )
    vendor: str = Field(..., description="The logistics vendor name normalized (e.g., MAERSK, ONE).")
    state: ShipmentState = Field(..., description="The canonical shipment state derived from the vendor update.")
    event_time: str = Field(..., description="ISO 8601 event date-time, preserving vendor offset if available.")
    container_id: str | None = Field(
        None, description="The specific container code (e.g., MSKU7748112) if available in payload."
    )


class InvoiceExtracted(BaseModel):
    invoice_number: str = Field(..., description="Unique invoice document identifier.")
    vendor: str = Field(..., description="The financial/billing vendor name normalized (e.g., GlobalFreightPay).")
    state: InvoiceState = Field(..., description="The canonical invoice state.")
    currency: str = Field(..., description="ISO 3-letter currency code, e.g. EUR, USD.")
    amount: float = Field(..., description="The document monetary amount.")
    event_time: str = Field(..., description="ISO 8601 event date-time, preserving vendor offset if available.")


class NormalizationResult(BaseModel):
    entity_type: EntityType = Field(..., description="The classification of the incoming payload.")
    confidence: float = Field(..., description="Confidence score of classification from 0.0 to 1.0.")
    shipment_data: ShipmentExtracted | None = Field(None, description="Populate only if entity_type is SHIPMENT.")
    invoice_data: InvoiceExtracted | None = Field(None, description="Populate only if entity_type is INVOICE.")
    reasoning: str = Field(..., description="Chain of thought reasoning for the mapping decision.")


class ExtractionSpec(BaseModel):
    """
    Reusable extraction spec generated once per vendor schema shape.
    Stored in vendor_schemas and applied deterministically to future payloads —
    no LLM call needed after the first encounter.
    """

    entity_type: str = Field(..., description="SHIPMENT, INVOICE, or UNCLASSIFIED")
    vendor_value: str | None = Field(None, description="Static vendor name (e.g. MAERSK, GlobalFreightPay)")
    vendor_path: str | None = Field(None, description="Dot-notation path to vendor name in payload, if not static")
    state_text_path: str = Field(
        ..., description="Dot-notation path to the field that contains the event state description"
    )
    state_map: dict = Field(
        ...,
        description="Lowercase keyword substrings mapped to canonical state values (e.g. 'loaded onboard' -> 'IN_TRANSIT')",
    )
    event_time_path: str = Field(..., description="Dot-notation path to the primary event timestamp field")
    event_time_alt_paths: list[str] = Field(
        default=[], description="Fallback paths if primary event_time_path is missing"
    )
    external_id_path: str | None = Field(None, description="SHIPMENT only: dot-notation path to booking/BL number")
    container_id_path: str | None = Field(None, description="SHIPMENT only: dot-notation path to container code")
    invoice_number_path: str | None = Field(None, description="INVOICE only: dot-notation path to invoice number")
    amount_path: str | None = Field(
        None, description="INVOICE only: dot-notation path to amount field (may include currency prefix)"
    )
    amount_format: str | None = Field(
        None,
        description="INVOICE only: 'european' if amount uses European format with dot as thousands separator and comma as decimal (e.g. 24.350,75)",
    )


class NormalizerService:
    def __init__(self, client: AsyncOpenAI | None = None):
        self.client = client or AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    async def normalize_event(self, payload: dict) -> NormalizationResult:
        prompt = f"""You are a logistics and financial event parser for Glacis Gateway.
Analyze the incoming raw vendor webhook JSON payload and extract structured details into the target schema.

## Guidelines:
1. **Classification:**
   - **SHIPMENT:** Payload contains cargo movement updates, container tracked statuses, or bill of lading statuses.
   - **INVOICE:** Payload contains financial invoices, payments, void notices, or credit memos.
   - **UNCLASSIFIED:** Payload has no relevant logistics or financial document context.

2. **Term Mappings & Canonical States:**
   - Map vendor text states to canonical states:
     - Shipment states: PICKED_UP, IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED
       (e.g., "Loaded onboard" -> IN_TRANSIT, "Cargo released" -> DELIVERED, "In gate" -> PICKED_UP)
     - Invoice states: ISSUED, PAID, VOIDED, REFUNDED
       (e.g., "settled in full" -> PAID, "cancelled" -> VOIDED)

3. **Vendor Normalization:** Normalize all vendor names to standard identifiers (e.g. MAERSK, GlobalFreightPay, ONE).
4. **Time parsing:** Extract the event occurrence time and format as ISO 8601.

Raw Payload:
{payload}
"""
        response = await self.client.beta.chat.completions.parse(
            model=settings.LLM_MODEL,
            messages=[
                {"role": "system", "content": "You are an expert logistics and finance data processing API."},
                {"role": "user", "content": prompt},
            ],
            response_format=NormalizationResult,
            temperature=0.0,
        )
        parsed_result = response.choices[0].message.parsed
        if not parsed_result:
            raise ValueError("LLM response did not contain parsed structured output.")
        return parsed_result

    async def generate_vendor_spec(self, payload: dict, norm_result: NormalizationResult) -> dict:
        prompt = f"""You are generating a reusable JSON extraction spec for a vendor webhook schema.

The payload below was already classified as: {norm_result.entity_type}
Correct normalization result: {norm_result.model_dump_json()}

Your task: produce an ExtractionSpec that can extract the same information from ANY future payload
with this exact schema shape, using only dot-notation field paths and keyword matching.

Rules:
- Use dot-notation paths (e.g. "transaction.kind", "transport_doc.number")
- state_map keys must be lowercase substrings that uniquely identify a state in the state text field
- Include ALL known state variants for this vendor, not just the one in this payload
- For amounts like "EUR 24.350,75", set amount_format to "european"
- For amounts like "USD 1,234.56", leave amount_format null

Payload:
{json.dumps(payload, indent=2)}
"""
        response = await self.client.beta.chat.completions.parse(
            model=settings.LLM_MODEL,
            messages=[
                {"role": "system", "content": "You are an expert data extraction schema generator."},
                {"role": "user", "content": prompt},
            ],
            response_format=ExtractionSpec,
            temperature=0.0,
        )
        spec = response.choices[0].message.parsed
        if not spec:
            raise ValueError("LLM did not return a parsed spec.")
        return spec.model_dump()
