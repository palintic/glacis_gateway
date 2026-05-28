"""
Integration test fixtures.

These tests require real PostgreSQL and Redis — run `docker compose up postgres redis -d` first.
They are skipped automatically if the database is unreachable.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.core.queue import get_redis
from app.db.base import Base
from app.db.session import get_db
from app.main import app


@pytest.fixture(scope="session")
async def integration_engine():
    """Create a real async engine. Skip the session if Postgres is unreachable."""
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable — skipping integration tests")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
def integration_session_factory(integration_engine):
    return async_sessionmaker(bind=integration_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def clean_tables(integration_session_factory):
    """Truncate all tables before each test to guarantee isolation."""
    async with integration_session_factory() as session:
        await session.execute(text("TRUNCATE raw_events, shipments, invoices, vendor_schemas RESTART IDENTITY CASCADE"))
        await session.commit()


@pytest.fixture
async def db_session(integration_session_factory) -> AsyncSession:
    async with integration_session_factory() as session:
        yield session


@pytest.fixture
async def redis():
    """Real arq Redis pool."""
    try:
        pool = await get_redis()
        yield pool
    except Exception:
        pytest.skip("Redis not reachable — skipping integration tests")


@pytest.fixture
async def client(db_session, redis):
    """HTTPX client wired to the real DB session and Redis pool."""
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_redis] = lambda: redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
