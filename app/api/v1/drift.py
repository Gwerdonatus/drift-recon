"""
Drift detection endpoints.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_api_key
from app.database import get_db
from app.models import DriftEvent, ReconciliationSnapshot
from app.schemas import DriftEventOut, DriftSummary
from app.services.drift_analyzer import DriftAnalyzer

router = APIRouter(dependencies=[Depends(verify_api_key)])


@router.post(
    "/analyze/{source_name}",
    summary="Run drift analysis for a source",
    description=(
        "Analyzes the latest snapshot against historical baseline. "
        "Requires DRIFT_MIN_SNAPSHOTS total snapshots (default: 7, including current)."
    ),
)
async def analyze_drift(
    source_name: str,
    db: AsyncSession = Depends(get_db),
):
    analyzer = DriftAnalyzer(db=db)
    events = await analyzer.analyze_latest(source_name=source_name)
    for event in events:
        db.add(event)
    return {
        "source_name": source_name,
        "events_detected": len(events),
        "events": [
            {
                "type": e.event_type,
                "severity": e.severity,
                "metric": e.metric_name,
                "z_score": float(e.z_score),
                "hypothesis": e.hypothesis,
            }
            for e in events
        ],
    }


@router.get(
    "/summary/{source_name}",
    response_model=DriftSummary,
    summary="Drift summary for a source",
)
async def drift_summary(
    source_name: str,
    db: AsyncSession = Depends(get_db),
):
    analyzer = DriftAnalyzer(db=db)
    summary = await analyzer.get_drift_summary(source_name=source_name)
    return DriftSummary(**summary)


@router.get(
    "/events",
    response_model=list[DriftEventOut],
    summary="List drift events",
)
async def list_drift_events(
    source_name: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    unresolved_only: bool = Query(default=False),
    limit: int = Query(default=50, le=500),
    db: AsyncSession = Depends(get_db),
):
    # Join through snapshot to filter by source_name
    query = select(DriftEvent).join(
        ReconciliationSnapshot,
        DriftEvent.snapshot_id == ReconciliationSnapshot.id,
    )

    if source_name:
        query = query.where(ReconciliationSnapshot.source_name == source_name)
    if severity:
        query = query.where(DriftEvent.severity == severity)
    if unresolved_only:
        query = query.where(DriftEvent.resolved_at.is_(None))

    query = query.order_by(DriftEvent.created_at.desc()).limit(limit)
    result = await db.execute(query)
    events = result.scalars().all()

    return [
        DriftEventOut(
            id=e.id,
            snapshot_id=e.snapshot_id,
            event_type=e.event_type,
            severity=e.severity,
            metric_name=e.metric_name,
            current_value=float(e.current_value),
            baseline_mean=float(e.baseline_mean),
            baseline_stddev=float(e.baseline_stddev),
            z_score=float(e.z_score),
            hypothesis=e.hypothesis,
            supporting_evidence=e.supporting_evidence,
            resolved_at=e.resolved_at,
            resolution_notes=e.resolution_notes,
            created_at=e.created_at,
        )
        for e in events
    ]


@router.patch(
    "/events/{event_id}/resolve",
    summary="Mark a drift event as resolved",
)
async def resolve_drift_event(
    event_id: uuid.UUID,
    notes: str = Query(..., min_length=5, max_length=1000),
    db: AsyncSession = Depends(get_db),
):
    event = await db.get(DriftEvent, event_id)
    if not event:
        from app.core.exceptions import ResourceNotFoundError

        raise ResourceNotFoundError("DriftEvent", str(event_id))

    event.resolved_at = datetime.now(timezone.utc)
    event.resolution_notes = notes

    return {"id": event_id, "resolved_at": event.resolved_at, "notes": notes}
