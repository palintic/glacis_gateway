import structlog
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.config import settings

logger = structlog.get_logger()

# Redis pool instance holder
redis_pool: ArqRedis | None = None


async def get_redis() -> ArqRedis:
    """
    Get or initialize the arq Redis connection pool.

    This ensures reuse of the pool across API requests.
    """
    global redis_pool
    if redis_pool is None:
        arq_settings = RedisSettings.from_dsn(settings.REDIS_URL)
        redis_pool = await create_pool(arq_settings)
        logger.info("Initialized arq Redis pool successfully.")
    return redis_pool


async def close_redis() -> None:
    """
    Close the arq Redis connection pool.

    Called during application shutdown.
    """
    global redis_pool
    if redis_pool is not None:
        await redis_pool.close()
        redis_pool = None
        logger.info("Closed arq Redis pool successfully.")
