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
│   ├── registry.py          # vendor schema registry (spec lookup + apply)
│   └── state_manager.py     # canonical entity persistence
├── workers/tasks.py         # arq worker — registry-first, LLM fallback
└── config.py                # Pydantic settings
migrations/versions/         # Alembic migration history
tests/                       # pytest — no infrastructure required
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

Unit tests mock Redis. Integration tests need both:

```bash
docker compose up postgres redis -d
pytest tests/integration/
```

Tests use in-memory SQLite and a mock Redis — no infrastructure needed.

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

**Vendor schema registry:** The LLM generates a reusable extraction spec on the first event from each vendor schema. All subsequent events from that vendor are processed deterministically — zero LLM calls. The LLM cost is a one-time onboarding cost per vendor, not a per-event cost.

**Commit before enqueue:** The raw event is committed to the DB before the job is pushed to Redis. This prevents the worker from racing an uncommitted row. Events stuck in `pending` (Redis down at enqueue time) are recoverable by re-enqueueing from the DB.

**Append-only canonical tables:** Every normalized event is a new row. Current entity state is derived by ordering records by `event_time DESC` — out-of-order arrivals are handled automatically without any special logic.

**Raw payload persistence:** Every webhook is stored exactly as received. This enables replay for debugging, prompt iteration, and backfills when normalization logic changes.

**PostgreSQL over Kafka:** Lower operational complexity for this scope. The architecture supports swapping in Kafka later — the worker only depends on the `raw_events` table, not the queue transport.

---

## License

MIT
