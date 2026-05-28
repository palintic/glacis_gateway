# Glacis Gateway

Webhook ingestion and normalization service for logistics and financial vendors. Accepts arbitrary JSON payloads, classifies and normalizes them into canonical schemas using an LLM, and persists reliable entity state — handling duplicate deliveries and out-of-order events.

---

## How it works

Ingestion and processing are fully decoupled. The API commits the raw payload to the database and returns a `202` to the vendor in under 200ms, before any LLM work begins. The background worker then normalizes the event asynchronously.

On first encounter with a vendor's payload shape, the worker calls the LLM twice: once to normalize the event, once to generate a reusable extraction spec. That spec is stored and applied deterministically to all future payloads with the same schema — so the LLM cost is paid once per vendor, not once per event.

```mermaid
sequenceDiagram
    participant V as Vendor
    participant A as API
    participant DB as PostgreSQL
    participant R as Redis
    participant W as Worker
    participant L as OpenAI

    V->>A: POST /webhooks (JSON payload)
    A->>DB: check duplicate (payload_hash / vendor_event_id)

    alt duplicate
        DB-->>A: existing event
        A-->>V: 202 Accepted (existing request_id)
    else new event
        A->>DB: INSERT raw_events (status=pending)
        A->>DB: COMMIT
        A->>R: enqueue_job(event_id)
        A-->>V: 202 Accepted (request_id)

        R-->>W: dequeue job(event_id)
        W->>DB: UPDATE raw_events status=processing
        W->>DB: SELECT vendor_schemas WHERE fingerprint=?

        alt registry hit — known vendor schema
            DB-->>W: extraction spec
            W->>W: apply spec deterministically (no LLM call)
        else registry miss — new vendor schema
            W->>L: normalize_event(payload)
            L-->>W: NormalizationResult
            W->>L: generate_vendor_spec(payload, result)
            L-->>W: ExtractionSpec
            W->>DB: INSERT vendor_schemas (cached for next time)
        end

        W->>DB: INSERT shipments or invoices
        W->>DB: UPDATE raw_events status=completed
        W->>DB: COMMIT
    end
```

**Entity types:** `SHIPMENT` · `INVOICE` · `UNCLASSIFIED`

**Shipment states:** `PICKED_UP → IN_TRANSIT → OUT_FOR_DELIVERY → DELIVERED`

**Invoice states:** `ISSUED → PAID` · terminal: `VOIDED` · `REFUNDED`

---

## Data model

```
raw_events
├── id                UUID PK
├── payload           JSON          raw vendor payload
├── payload_hash      TEXT UNIQUE   SHA-256 for deduplication
├── vendor_event_id   TEXT          vendor-provided ID for deduplication
├── status            TEXT          pending → processing → completed | failed
└── received_at       TIMESTAMPTZ

shipments                           append-only, one row per event
├── id                UUID PK
├── external_id       TEXT          booking / BL number
├── vendor            TEXT
├── state             TEXT          PICKED_UP | IN_TRANSIT | OUT_FOR_DELIVERY | DELIVERED
├── event_time        TIMESTAMPTZ   vendor-reported time (used for ordering)
├── container_id      TEXT
├── raw_payload_id    UUID FK → raw_events
└── created_at        TIMESTAMPTZ

invoices                            append-only, one row per event
├── id                UUID PK
├── invoice_number    TEXT
├── vendor            TEXT
├── state             TEXT          ISSUED | PAID | VOIDED | REFUNDED
├── currency          TEXT
├── amount            NUMERIC
├── event_time        TIMESTAMPTZ   vendor-reported time (used for ordering)
├── raw_payload_id    UUID FK → raw_events
└── created_at        TIMESTAMPTZ

vendor_schemas                      one row per vendor schema shape
├── id                UUID PK
├── schema_fingerprint TEXT UNIQUE  16-char hash of sorted top-level payload keys
├── entity_type       TEXT          SHIPMENT | INVOICE | UNCLASSIFIED
├── extraction_spec   JSON          LLM-generated dot-notation extraction rules
└── sample_raw_payload_id UUID FK → raw_events
```

Current state of a shipment or invoice is derived at read time by querying the append-only table ordered by `event_time DESC` — out-of-order arrivals are stored but never surface as the latest state.

---

## Tech stack

| Component | Technology |
|---|---|
| Language | Python 3.12 |
| API | FastAPI + Uvicorn |
| Database | PostgreSQL 16 + SQLAlchemy 2.0 (async) |
| Migrations | Alembic |
| Queue | Redis + arq |
| LLM | OpenAI structured outputs (`gpt-4o-mini`) |
| Logging | structlog (JSON) |

---

## Project structure

```
app/
├── api/webhooks.py          # POST /webhooks ingestion endpoint
├── db/
│   ├── base.py              # declarative base with audit columns
│   ├── session.py           # async engine, session factory, get_db
│   ├── queries.py           # all database queries
│   ├── models/
│   │   ├── raw_event.py     # raw payload + status tracking
│   │   ├── shipment.py      # canonical shipment events (append-only)
│   │   ├── invoice.py       # canonical invoice events (append-only)
│   │   └── vendor_schema.py # per-vendor extraction specs
│   └── migrations/          # Alembic migration scripts
├── core/
│   └── queue.py             # Redis connection pool
├── services/
│   ├── llm.py               # OpenAI normalization + spec generation
│   └── registry.py          # vendor schema fingerprinting + spec application
├── workers/tasks.py         # arq worker — registry-first, LLM fallback
├── utils.py                 # shared utilities (timestamp parsing etc.)
└── config.py                # Pydantic settings
tests/                       # pytest — requires PostgreSQL
```

---

## Running with Docker

The fastest way to get everything running.

```bash
git clone https://github.com/<your-username>/glacis-gateway.git
cd glacis-gateway

export OPENAI_API_KEY=sk-...

docker compose up
```

This starts PostgreSQL, Redis, the API (`localhost:8000`), and the background worker. Migrations run automatically before the API starts.

**Verify:**

```bash
curl http://localhost:8000/health

curl -X POST http://localhost:8000/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "carrier_scac": "MAEU",
    "event_msg_id": "MAEU-EVT-2026-04-22-0001",
    "transport_doc": {"type": "MBL", "number": "MAEU240498712"},
    "container": "MSKU7748112",
    "milestone": "Loaded onboard and sailed",
    "milestone_at": "2026-04-21T22:47:00+08:00"
  }'
```

**Tail logs by service:**

```bash
docker compose logs -f api
docker compose logs -f worker
```

**Rebuild after code changes:**

```bash
docker compose up --build
```

---

## Debugging locally

Run infrastructure in Docker, application on your machine — with hot reload and direct log output.

### 1. Start infrastructure

```bash
docker compose up postgres redis -d
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set environment variables

```bash
export DATABASE_URL=postgresql+asyncpg://glacis:glacispassword@localhost:5432/glacis_gateway
export REDIS_URL=redis://localhost:6379/0
export OPENAI_API_KEY=sk-...
```

### 4. Run migrations

```bash
alembic upgrade head
```

### 5. Start the API (hot reload)

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 6. Start the worker (separate terminal)

```bash
arq app.workers.tasks.WorkerSettings
```

---

### Inspecting state

**Check what's in the DB:**

```bash
docker compose exec postgres psql -U glacis -d glacis_gateway
```

```sql
-- See all raw events and their processing status
SELECT id, status, vendor_event_id, created_at FROM raw_events ORDER BY created_at DESC LIMIT 20;

-- See normalized shipments
SELECT external_id, vendor, state, event_time FROM shipments ORDER BY event_time DESC LIMIT 20;

-- See stored vendor extraction specs
SELECT schema_fingerprint, entity_type, extraction_spec FROM vendor_schemas;

-- Find stuck events (pending > 60s)
SELECT id, status, created_at FROM raw_events
WHERE status = 'pending' AND created_at < now() - interval '60 seconds';
```

**Check the Redis queue:**

```bash
docker compose exec redis redis-cli
```

```
> KEYS *          # list all arq keys
> LLEN arq:queue  # pending job count
```

**Re-enqueue a stuck event manually:**

```python
# run in a Python shell with env vars set
import asyncio
from arq import create_pool
from arq.connections import RedisSettings
from app.config import settings

async def requeue(event_id: str):
    redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    await redis.enqueue_job("process_webhook_event", event_id)
    await redis.aclose()

asyncio.run(requeue("<event-uuid>"))
```

---

## Tests

All tests use real PostgreSQL. Start it before running:

```bash
docker compose up postgres -d
```

```bash
pytest                           # all tests
pytest -v                        # verbose
pytest tests/test_ingestion.py   # single file
pytest tests/integration/        # integration tests only (also needs Redis)
```

Unit tests mock Redis and the LLM. Integration tests require both PostgreSQL and Redis:

```bash
docker compose up postgres redis -d
pytest tests/integration/
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://glacis:glacispassword@localhost:5432/glacis_gateway` | PostgreSQL DSN |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis DSN |
| `OPENAI_API_KEY` | _(required)_ | OpenAI key for normalization and spec generation |
| `LLM_MODEL` | `gpt-4o-mini` | Model used for normalization and spec generation |
| `ENVIRONMENT` | `development` | `development` or `testing` |
| `INGESTION_TIMEOUT_LIMIT_MS` | `200` | Logs a warning when ingestion exceeds this threshold |

---

## Design decisions

**Async ingestion:** LLM calls take 1-3s. Decoupling ingestion from processing keeps webhook ACKs under 200ms and makes the two independently scalable and fault-tolerant.

**Vendor schema registry:** Every vendor sends payloads in a fixed structure — the same field names, the same nesting, event after event. The first time a new payload shape is seen, the worker calls the LLM twice: once to normalize the event, once to generate a reusable `ExtractionSpec`. The spec is a JSON document that encodes exactly how to extract every field from that vendor's payload using dot-notation paths and a keyword-to-state map:

```json
{
  "entity_type": "SHIPMENT",
  "vendor_value": "MAERSK",
  "state_text_path": "milestone",
  "state_map": {
    "loaded onboard": "IN_TRANSIT",
    "released to shipper": "PICKED_UP",
    "cargo released": "DELIVERED"
  },
  "event_time_path": "milestone_at",
  "external_id_path": "transport_doc.number",
  "container_id_path": "container"
}
```

The spec is keyed by a **schema fingerprint** — a 16-character hash of the sorted top-level payload keys. On every subsequent event, the worker computes the fingerprint, looks up the spec in `vendor_schemas`, and applies it deterministically with no LLM call. Field extraction is a dot-notation path walk; state mapping is a case-insensitive substring match.

If the spec lookup misses (new vendor) or the state keyword isn't in the map (new event type from a known vendor), the worker falls back to the LLM and the spec is updated. The LLM cost is a one-time onboarding cost per vendor schema shape, not a per-event cost.

**Example — Maersk, first event ever:**

```json
{
  "carrier_scac": "MAEU",
  "event_msg_id": "MAEU-EVT-2026-04-22-0001",
  "transport_doc": { "type": "MBL", "number": "MAEU240498712" },
  "container": "MSKU7748112",
  "milestone": "Loaded onboard and sailed",
  "milestone_at": "2026-04-21T22:47:00+08:00"
}
```

1. Fingerprint computed from sorted keys: `carrier_scac, container, event_msg_id, milestone, milestone_at, transport_doc` → `"a3f1c9e2b4d07f12"`
2. `vendor_schemas` lookup: **miss** — no spec stored yet
3. LLM call 1 — `normalize_event`: classifies as `SHIPMENT`, extracts `external_id=MAEU240498712`, `state=IN_TRANSIT`, `event_time=2026-04-21T22:47:00+08:00`
4. LLM call 2 — `generate_vendor_spec`: produces and stores the spec above
5. Shipment row inserted, `raw_events.status` → `completed`
6. **2 LLM calls used**

**Same vendor, next event (different milestone, same schema shape):**

```json
{
  "carrier_scac": "MAEU",
  "event_msg_id": "MAEU-EVT-2026-04-28-0099",
  "transport_doc": { "type": "MBL", "number": "MAEU240498712" },
  "container": "MSKU7748112",
  "milestone": "Cargo released to consignee",
  "milestone_at": "2026-04-28T09:15:00+08:00"
}
```

1. Same top-level keys → same fingerprint `"a3f1c9e2b4d07f12"`
2. `vendor_schemas` lookup: **hit** — spec returned
3. `apply_spec`: resolves `milestone` → `"Cargo released to consignee"`, matches `"cargo released"` in `state_map` → `DELIVERED`; resolves `transport_doc.number` → `MAEU240498712`; resolves `milestone_at` → timestamp
4. Shipment row inserted, `raw_events.status` → `completed`
5. **0 LLM calls used**

**Commit before enqueue:** The raw event is committed to the DB before the job is pushed to Redis. This prevents the worker from racing an uncommitted row. Events stuck in `pending` (Redis down at enqueue time) are recoverable by re-enqueueing from the DB.

**Append-only canonical tables and out-of-order handling:** Vendors frequently deliver events out of chronological order — a `DELIVERED` update can arrive before the `PICKED_UP` event that preceded it. Rather than trying to detect and reorder events on write, every normalized event is appended as a new row. Current entity state is derived at read time by ordering the full event history by `event_time DESC` and taking the top row — the vendor-reported timestamp, not our ingestion time.

Example — Maersk shipment `MAEU240498712`:

| Arrival order | `event_time` (vendor) | `state` | `created_at` (us) |
|---|---|---|---|
| 1st to arrive | 2026-04-21T22:47:00+08:00 | IN_TRANSIT | 2026-04-22T10:01:00Z |
| 2nd to arrive | 2026-04-19T11:15:00+08:00 | PICKED_UP | 2026-04-22T10:05:00Z |

Both rows are stored. `ORDER BY event_time DESC LIMIT 1` returns `IN_TRANSIT` (April 21) — the late-arriving `PICKED_UP` (April 19) is preserved in history but never becomes the current state. No special reordering logic, no state machine, no conflict resolution — the data model handles it naturally.

**Tolerant timestamp parsing:** Vendor timestamps arrive in inconsistent formats (`ISO 8601`, local time strings like `"28/04/2026 09:42 WIB"`, etc.). A `parse_event_time` utility tries `fromisoformat` first and falls back to `dateutil.parse` to handle non-standard formats gracefully.

**Raw payload persistence:** Every webhook is stored exactly as received. This enables replay for debugging, prompt iteration, and backfills when normalization logic changes.

**PostgreSQL over Kafka:** Lower operational complexity for this scope. The architecture supports swapping in Kafka later — the worker only depends on the `raw_events` table, not the queue transport.

---

## Production roadmap

What would need to happen before this runs in production at scale.

**Reliability**
- Retry with exponential backoff on LLM failures — currently a failed LLM call marks the event `failed` immediately; arq supports configurable retry counts and backoff
- Dead-letter queue for events that exhaust retries, with alerting
- Re-enqueue job for events stuck in `pending` beyond a threshold (worker crashed before dequeuing)
- Idempotency on the worker: guard against double-processing if arq re-runs a job that already completed

**Vendor schema registry**
- Upsert spec on state map miss — when a known vendor sends a new event type, the spec should be updated rather than falling back to the LLM forever
- Spec versioning — if the LLM regenerates a spec (e.g. vendor schema changed), keep history so old events can be replayed correctly
- Manual spec override — allow ops to correct a bad LLM-generated spec without touching the database directly

**Observability**
- Structured metrics: LLM call rate, registry hit/miss ratio, event processing latency, failure rate per vendor
- Alerting on sustained `failed` event rate or registry miss rate spikes (may indicate a vendor schema change)
- Distributed tracing across API → Redis → worker for end-to-end latency visibility

**Security**
- Webhook signature verification per vendor (HMAC, RSA) — currently any caller can POST to `/webhooks`
- Rate limiting per vendor to prevent abuse
- Secrets management (OpenAI key, DB credentials) via Vault or cloud secret manager rather than env vars

**Scalability**
- Horizontal worker scaling — arq supports multiple worker processes; `max_jobs` per worker is already configurable
- Read replicas for query-heavy workloads once shipment/invoice read APIs are added
- Partitioning `shipments` and `invoices` by vendor or time range at high event volumes

**Operations**
- Alembic migrations run in CI before deployment, not at container startup
- Blue/green or rolling deploys — the current `entrypoint.sh` migration approach blocks all instances during deploy
- Backfill tooling — replay raw events through updated normalization logic when prompt or spec changes
