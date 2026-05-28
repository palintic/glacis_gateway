import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.core.queue import get_redis
from app.db.base import Base
from app.db.session import get_db
from app.main import app

# Use real PostgreSQL — tests require `docker compose up postgres -d`
settings.ENVIRONMENT = "testing"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def engine():
    _engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL not reachable — run `docker compose up postgres -d`")

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield _engine
    await _engine.dispose()


@pytest.fixture(scope="session")
def session_factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def clean_tables(session_factory):
    """Truncate all tables before each test to guarantee isolation."""
    async with session_factory() as session:
        await session.execute(text("TRUNCATE raw_events, shipments, invoices, vendor_schemas RESTART IDENTITY CASCADE"))
        await session.commit()


@pytest.fixture
async def db_session(session_factory) -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.enqueue_job = AsyncMock(return_value=MagicMock())
    return redis


@pytest.fixture
async def client(db_session: AsyncSession, mock_redis: AsyncMock) -> AsyncGenerator[AsyncClient, None]:
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_redis] = lambda: mock_redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
