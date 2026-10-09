"""
Reconciliation endpoints.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_api_key
from app.core.exceptions import InsufficientDataError
from app.core.logging import get_logger
from app.database import get_db
from app.models import ReconciliationResult, ReconciliationSnapshot
from app.schemas import (
    ReconciliationResultOut,
    ReconciliationRunRequest,
    ReconciliationRunResponse,
    SnapshotOut,
)
from app.services.drift_analyzer import DriftAnalyzer
from app.services.matcher import ReconciliationOrchestrator

router = APIRouter(dependencies=[Depends(verify_api_key)])
log = get_logger(__name__)


@router.post(
    "/run",
    response_model=ReconciliationRunResponse,
    status_code=status.HTTP_200_OK,
    summary="Trigger a reconciliation run",
)
async def run_reconciliation(
    body: ReconciliationRunRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Trigger a reconciliation run for a source.
    Loads pending transactions and bank entries, scores all pairs,
    persists results, creates a snapshot, and runs drift analysis.
    """
    orchestrator = ReconciliationOrchestrator(db=db)
    result = await orchestrator.run(
        source_name=body.source_name,
        from_date=body.from_date,
        to_date=body.to_date,
        confidence_threshold=body.confidence_threshold,
        review_threshold=body.review_threshold,
    )

    # Run drift analysis on the new snapshot (non-blocking on failure)
    drift_events_raised = 0
    try:
        async with db.begin_nested():
            analyzer = DriftAnalyzer(db=db)
            events = await analyzer.analyze_latest(source_name=body.source_name)
            for event in events:
                db.add(event)
            await db.flush()
            drift_events_raised = len(events)
    except InsufficientDataError:
        pass  # A baseline is not available for the first few runs.
    except Exception:
        log.exception(
            "drift_analysis_failed", source=body.source_name, run_id=result.run_id
        )

    confidences = [float(c.confidence) for c in result.matched + result.review]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    return ReconciliationRunResponse(
        run_id=result.run_id,
        source_name=result.source_name,
        started_at=result.started_at,
        completed_at=result.completed_at,
        duration_seconds=result.duration_seconds,
        total_transactions=result.total_transactions,
        total_bank_entries=result.total_bank_entries,
        matched=len(result.matched),
        review=len(result.review),
        unmatched=len(result.unmatched_transactions),
        match_rate=result.match_rate,
        avg_confidence=avg_confidence,
        drift_events_raised=drift_events_raised,
    )


@router.get(
    "/results/{run_id}",
    response_model=list[ReconciliationResultOut],
    summary="Get results for a specific run",
)
async def get_run_results(
    run_id: str,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    query = select(ReconciliationResult).where(ReconciliationResult.run_id == run_id)
    if status_filter:
        query = query.where(ReconciliationResult.status == status_filter)
    query = query.limit(limit).offset(offset)

    result = await db.execute(query)
    rows = result.scalars().all()
    return [ReconciliationResultOut.from_orm_with_scores(r) for r in rows]


@router.get(
    "/snapshots",
    response_model=list[SnapshotOut],
    summary="List reconciliation snapshots (history)",
)
async def list_snapshots(
    source_name: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(ReconciliationSnapshot)
        .order_by(ReconciliationSnapshot.run_date.desc())
        .limit(limit)
    )
    if source_name:
        query = query.where(ReconciliationSnapshot.source_name == source_name)

    result = await db.execute(query)
    snapshots = result.scalars().all()
    return [
        SnapshotOut(
            id=s.id,
            run_id=s.run_id,
            run_date=s.run_date,
            source_name=s.source_name,
            total_transactions=s.total_transactions,
            total_bank_entries=s.total_bank_entries,
            matched_count=s.matched_count,
            review_count=s.review_count,
            p50_confidence=(
                float(s.p50_confidence) if s.p50_confidence is not None else None
            ),
            p10_confidence=(
                float(s.p10_confidence) if s.p10_confidence is not None else None
            ),
            unmatched_count=s.unmatched_count,
            match_rate=float(s.match_rate),
            avg_confidence=float(s.avg_confidence) if s.avg_confidence else None,
            run_duration_seconds=(
                float(s.run_duration_seconds) if s.run_duration_seconds else None
            ),
        )
        for s in snapshots
    ]


@router.patch(
    "/results/{result_id}/review",
    summary="Record an analyst verdict alongside an engine decision",
)
async def review_result(
    result_id: uuid.UUID,
    verdict: str = Query(..., pattern="^(matched|unmatched|escalated)$"),
    reviewed_by: str = Query(..., min_length=1, max_length=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Record a caller-supplied analyst label and verdict.
    The original engine result is retained. No model training is performed.
    """
    result = await db.get(ReconciliationResult, result_id)
    if not result:
        from app.core.exceptions import ResourceNotFoundError

        raise ResourceNotFoundError("ReconciliationResult", str(result_id))

    result.human_reviewed = True
    result.human_verdict = verdict
    result.reviewed_by = reviewed_by
    result.reviewed_at = datetime.now(timezone.utc)

    return {"id": result_id, "verdict": verdict, "reviewed_by": reviewed_by}
