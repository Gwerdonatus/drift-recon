"""
API v1 router — aggregates all sub-routers.
"""

from fastapi import APIRouter

from app.api.v1 import ingestion, reconciliation, drift

router = APIRouter()
router.include_router(ingestion.router, prefix="/ingest", tags=["ingestion"])
router.include_router(
    reconciliation.router, prefix="/reconciliation", tags=["reconciliation"]
)
router.include_router(drift.router, prefix="/drift", tags=["drift"])
