import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.webhooks import router as webhooks_router
from app.core.queue import close_redis, get_redis

# Configure structlog for JSON logging output
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifecycle events to manage resources like Redis connection pool.
    """
    # Startup: Initialize Redis pool
    await get_redis()
    yield
    # Shutdown: Clean up connections
    await close_redis()


app = FastAPI(
    title="Glacis Gateway",
    description="AI-powered webhook ingestion and normalization platform",
    version="1.0.0",
    lifespan=lifespan,
)

# Standard CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_timing_and_tracing(request: Request, call_next):
    """
    Middleware to measure response latency and check webhook SLA target (<200ms).
    """
    start_time = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start_time) * 1000

    # Log requests and flag those exceeding our 200ms target limit
    extra = {
        "method": request.method,
        "path": request.url.path,
        "duration_ms": round(duration_ms, 2),
        "status_code": response.status_code,
    }

    if duration_ms > 200:
        logger.warning("SLA Warning: Request exceeded 200ms target latency.", **extra)
    else:
        logger.info("Handled request", **extra)

    response.headers["X-Response-Time-Ms"] = str(round(duration_ms, 2))
    return response


# Register our webhooks endpoints
app.include_router(webhooks_router, tags=["Webhooks"])


@app.get("/health", tags=["System"])
async def health_check():
    """
    Health check endpoint for container environments.
    """
    return {"status": "healthy", "timestamp": time.time()}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
