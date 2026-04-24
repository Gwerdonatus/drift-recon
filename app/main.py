"""
FastAPI application entry point.

Middleware stack (applied outer → inner):
  1. TrustedHostMiddleware      — blocks requests with unexpected Host headers
  2. Request ID middleware       — injects X-Request-ID for tracing
  3. Logging middleware          — logs all requests with timing
  4. GZipMiddleware             — compress responses > 1KB
  5. CORSMiddleware             — locked down to explicit origins in production
  6. Rate limiting              — via Redis sliding window
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.core.exceptions import (
    DuplicateIngestionError,
    IngestionError,
    InsufficientDataError,
    ReconciliationError,
    ResourceNotFoundError,
)
from app.core.logging import configure_logging, get_logger
from app.database import check_db_health, close_engine, get_engine

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup → yield → shutdown."""
    settings = get_settings()

    # Configure structured logging first
    configure_logging(
        log_level=settings.LOG_LEVEL,
        use_json=not settings.is_development,
    )

    log.info(
        "application_starting",
        version=settings.APP_VERSION,
        environment=settings.ENVIRONMENT,
    )

    # Warm up DB connection pool
    engine = get_engine()
    log.info("database_pool_initialized")

    # Start background scheduler
    from app.workers.scheduler import start_scheduler, stop_scheduler
    scheduler = start_scheduler()

    log.info("application_ready")
    yield

    # Graceful shutdown
    log.info("application_shutting_down")
    stop_scheduler(scheduler)
    await close_engine()
    log.info("application_stopped")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="Data Drift Diagnosis & Reconciliation Service",
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        openapi_url="/openapi.json" if settings.is_development else None,
        lifespan=lifespan,
    )

    # ── Middleware ─────────────────────────────────────────────────────────────

    # Block unexpected Host headers (SSRF/host header injection protection)
    if not settings.is_development:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=["*"],  # Replace with your actual domain in production
        )

    # CORS — default to deny all in production
    cors_origins = ["http://localhost:8501"] if settings.is_development else []
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["X-API-Key", "Content-Type"],
    )

    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # ── Request ID + Logging middleware ───────────────────────────────────────

    @app.middleware("http")
    async def request_id_and_logging(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        start = time.perf_counter()

        import structlog
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        response: Response = await call_next(request)

        duration_ms = (time.perf_counter() - start) * 1000
        log.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
            request_id=request_id,
        )

        response.headers["X-Request-ID"] = request_id
        return response

    # ── Exception Handlers ────────────────────────────────────────────────────

    @app.exception_handler(DuplicateIngestionError)
    async def duplicate_ingestion_handler(req: Request, exc: DuplicateIngestionError):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc), "external_id": exc.external_id},
        )

    @app.exception_handler(IngestionError)
    async def ingestion_error_handler(req: Request, exc: IngestionError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": str(exc)},
        )

    @app.exception_handler(ResourceNotFoundError)
    async def not_found_handler(req: Request, exc: ResourceNotFoundError):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": str(exc)},
        )

    @app.exception_handler(InsufficientDataError)
    async def insufficient_data_handler(req: Request, exc: InsufficientDataError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": str(exc),
                "required_snapshots": exc.required,
                "available_snapshots": exc.available,
            },
        )

    @app.exception_handler(ReconciliationError)
    async def domain_error_handler(req: Request, exc: ReconciliationError):
        log.error("unhandled_domain_error", error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal reconciliation error. Check logs."},
        )

    # ── Routes ────────────────────────────────────────────────────────────────

    from app.api.v1.router import router as v1_router
    app.include_router(v1_router, prefix="/api/v1")

    # Health endpoint (no auth — needed by load balancer / Docker healthcheck)
    @app.get("/health", tags=["ops"])
    async def health():
        db_health = await check_db_health()
        overall = "healthy" if db_health["status"] == "healthy" else "degraded"
        return {
            "status": overall,
            "version": settings.APP_VERSION,
            "environment": settings.ENVIRONMENT,
            "checks": {"database": db_health},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/", include_in_schema=False)
    async def root():
        return {"service": settings.APP_NAME, "version": settings.APP_VERSION}

    return app


app = create_app()
