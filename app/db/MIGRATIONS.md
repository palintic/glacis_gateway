# Database Migrations

Glacis Gateway uses [Alembic](https://alembic.sqlalchemy.org/) for database schema migrations with async SQLAlchemy support.

---

## Prerequisites

- Python 3.12+ with dependencies installed (`pip install -r requirements.txt`)
- A running PostgreSQL instance
- `DATABASE_URL` set in your environment

```bash
export DATABASE_URL=postgresql+asyncpg://glacis:glacispassword@localhost:5432/glacis_gateway
```

If you are using Docker Compose to run PostgreSQL:

```bash
docker compose up postgres
```

---

## Common Commands

### Apply all pending migrations

```bash
alembic upgrade head
```

### Apply the next pending migration

```bash
alembic upgrade +1
```

### Rollback the last migration

```bash
alembic downgrade -1
```

### Rollback all migrations

```bash
alembic downgrade base
```

### Check the current migration revision

```bash
alembic current
```

### View migration history

```bash
alembic history --verbose
```

---

## Creating a New Migration

### Auto-generate from model changes

After modifying a SQLAlchemy model in `app/models/`, generate a migration:

```bash
alembic revision --autogenerate -m "describe your change here"
```

This compares your current models against the database schema and generates the diff.

### Create a blank migration

```bash
alembic revision -m "describe your change here"
```

Use this for data migrations or anything Alembic cannot auto-detect (e.g. custom indexes, functions, triggers).

New migration files are placed in `migrations/versions/`.

---

## Existing Migrations

| Revision | Description |
|---|---|
| `e0f7e6f658c1` | Initial schema: `raw_events`, `shipments`, `invoices` tables |
| `20240527_01` | Explicit unique constraints on `payload_hash` and `vendor_event_id` |

---

## Running Migrations in Docker

To run migrations against the Dockerized PostgreSQL instance:

```bash
# Start only the database
docker compose up postgres -d

# Run migrations from your local machine
export DATABASE_URL=postgresql+asyncpg://glacis:glacispassword@localhost:5432/glacis_gateway
alembic upgrade head
```

Or run migrations inside a one-off container:

```bash
docker compose run --rm api alembic upgrade head
```

---

## Configuration

Alembic is configured via `alembic.ini`. The database URL is loaded dynamically from `app/config.py` (which reads from the `DATABASE_URL` environment variable), so you do not need to set it in `alembic.ini` directly.

Migration scripts are located in `app/db/migrations/versions/`.

---

## Notes

- **All tables include audit columns** (`id`, `created_at`, `updated_at`) inherited from the declarative base in `app/models/base.py`.
- **Async engine:** The migration environment (`migrations/env.py`) uses `async_engine_from_config` to support the async SQLAlchemy setup.
- **Test database:** Tests use an in-memory SQLite database managed directly by SQLAlchemy — Alembic is not involved in test runs.
