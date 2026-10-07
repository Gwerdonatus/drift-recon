"""
Database engine, session factory, and dependency injection.

Using async SQLAlchemy 2.0 with asyncpg driver.
Connection pooling is configured conservatively — tune pool_size
based on your Postgres max_connections (default 100 on most VPS).

Rule of thumb: pool_size * num_workers < max_connections * 0.8
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.DATABASE_URL,
            pool_size=settings.DATABASE_POOL_SIZE,
            max_overflow=settings.DATABASE_MAX_OVERFLOW,
            pool_timeout=settings.DATABASE_POOL_TIMEOUT,
            pool_pre_ping=True,  # Detect stale connections before use
            pool_recycle=3600,  # Recycle connections after 1 hour
            echo=settings.is_development,
        )
        log.info(
            "database_engine_created",
            pool_size=settings.DATABASE_POOL_SIZE,
            max_overflow=settings.DATABASE_MAX_OVERFLOW,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,  # Prevents lazy-load after commit
            autoflush=False,
            autocommit=False,
        )
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency. Yields a database session and handles
    commit/rollback automatically.

    Usage:
        @router.get("/")
        async def handler(db: AsyncSession = Depends(get_db)):
            ...
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager version for use outside of FastAPI
    (e.g., background workers, scripts).

    Usage:
        async with get_db_context() as db:
            result = await db.execute(select(Transaction))
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_db_health() -> dict:
    """Health check: verify database is reachable and responsive."""
    try:
        from sqlalchemy import text

        async with get_db_context() as db:
            result = await db.execute(text("SELECT 1"))
            result.scalar()
        return {"status": "healthy", "backend": "postgresql"}
    except Exception as e:
        log.error("database_health_check_failed", error=str(e))
        return {"status": "unhealthy", "error": str(e)}


async def close_engine() -> None:
    """Gracefully dispose the connection pool on shutdown."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        log.info("database_engine_disposed")
        _engine = None
