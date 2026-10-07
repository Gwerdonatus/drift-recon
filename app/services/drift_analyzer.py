"""
Drift Analyzer.

Uses Z-score based anomaly detection on a rolling window of snapshots.

Algorithm:
  1. Load last N snapshots (lookback window)
  2. Compute mean and stddev for each tracked metric
  3. For the most recent snapshot, compute z-score per metric:
       z = (current - mean) / stddev
  4. Flag as drift if |z| > DRIFT_ZSCORE_THRESHOLD
  5. Assign severity: LOW (2.0-2.5σ), MEDIUM (2.5-3.0σ), HIGH (>3.0σ)
  6. Run diagnostics to generate root cause hypotheses

Why Z-score over simple threshold:
  Absolute thresholds break as your data changes seasonally.
  Z-score adapts to your baseline — if your normal match rate is 92%,
  a drop to 88% might be normal Monday behavior (not an alert).
  Z-score catches drops relative to your actual history.

Limitation: requires DRIFT_MIN_SNAPSHOTS before analysis is reliable.
With < 7 data points, stddev is unreliable. We skip analysis in that case.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections.abc import Sequence
from typing import cast

import numpy as np
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import InsufficientDataError
from app.core.logging import get_logger
from app.models import (
    DriftEvent,
    DriftEventType,
    DriftSeverity,
    ReconciliationSnapshot,
)

log = get_logger(__name__)

# Metrics to monitor, mapped to their column names and event types
MONITORED_METRICS = [
    {
        "name": "match_rate",
        "column": "match_rate",
        "event_type": DriftEventType.MATCH_RATE_DROP,
        "direction": "low",  # alert when current is LOW relative to baseline
    },
    {
        "name": "avg_confidence",
        "column": "avg_confidence",
        "event_type": DriftEventType.MATCH_RATE_DROP,
        "direction": "low",
    },
    {
        "name": "avg_date_delta_days",
        "column": "avg_date_delta_days",
        "event_type": DriftEventType.DATE_SKEW,
        "direction": "high",  # alert when current is HIGH (increasing delay)
    },
    {
        "name": "unmatched_count",
        "column": "unmatched_count",
        "event_type": DriftEventType.VOLUME_ANOMALY,
        "direction": "both",
    },
]


def _compute_z_score(current: float, values: list[float]) -> tuple[float, float, float]:
    """
    Returns (z_score, mean, stddev).
    Uses population stddev (not sample) since we're not estimating from a sample.
    """
    arr = np.array(values)
    mean = float(np.mean(arr))
    stddev = float(np.std(arr))

    if stddev < 1e-10:
        # No variance in baseline — can't compute meaningful z-score
        return 0.0, mean, stddev

    z = (current - mean) / stddev
    return z, mean, stddev


def _z_to_severity(z_score: float, threshold: float) -> DriftSeverity | None:
    abs_z = abs(z_score)
    if abs_z >= threshold + 0.5:
        return DriftSeverity.HIGH
    elif abs_z >= threshold:
        return DriftSeverity.MEDIUM
    elif abs_z >= threshold - 0.5:
        return DriftSeverity.LOW
    return None


class DriftAnalyzer:

    def __init__(self, db: AsyncSession):
        self.db = db
        self.settings = get_settings()

    async def analyze_latest(self, source_name: str) -> list[DriftEvent]:
        """
        Run drift analysis on the most recent snapshot for a source.
        Returns list of DriftEvent objects (not yet persisted — caller commits).
        """
        lookback_days = self.settings.DRIFT_LOOKBACK_DAYS
        min_snapshots = self.settings.DRIFT_MIN_SNAPSHOTS
        z_threshold = self.settings.DRIFT_ZSCORE_THRESHOLD

        # Load recent snapshots for this source
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        query = (
            select(ReconciliationSnapshot)
            .where(
                and_(
                    ReconciliationSnapshot.source_name == source_name,
                    ReconciliationSnapshot.run_date >= cutoff,
                )
            )
            .order_by(ReconciliationSnapshot.run_date.asc())
        )
        result = await self.db.execute(query)
        snapshots = result.scalars().all()

        if len(snapshots) < min_snapshots:
            raise InsufficientDataError(
                required=min_snapshots, available=len(snapshots)
            )

        # Most recent is the current; rest form the baseline
        current = snapshots[-1]
        baseline = snapshots[:-1]

        log.info(
            "drift_analysis_start",
            source=source_name,
            current_run=current.run_id,
            baseline_count=len(baseline),
        )

        events: list[DriftEvent] = []

        for metric_config in MONITORED_METRICS:
            metric_name = metric_config["name"]
            col_name = metric_config["column"]
            direction = metric_config["direction"]
            event_type = metric_config["event_type"]

            current_val = getattr(current, col_name, None)
            if current_val is None:
                continue

            baseline_vals = [
                float(getattr(s, col_name))
                for s in baseline
                if getattr(s, col_name) is not None
            ]

            if len(baseline_vals) < 3:
                continue

            current_float = float(current_val)
            z_score, mean, stddev = _compute_z_score(current_float, baseline_vals)

            # Direction filtering
            if direction == "low" and z_score >= 0:
                continue  # Only alert on drops
            if direction == "high" and z_score <= 0:
                continue  # Only alert on increases

            severity = _z_to_severity(z_score, z_threshold)
            if severity is None:
                continue

            # Determine specific event type based on direction
            if metric_name == "match_rate" and z_score < 0:
                resolved_event_type = DriftEventType.MATCH_RATE_DROP
            elif metric_name == "match_rate" and z_score > 0:
                resolved_event_type = DriftEventType.MATCH_RATE_SPIKE
            else:
                resolved_event_type = cast(DriftEventType, event_type)

            hypothesis = self._generate_hypothesis(
                metric_name=metric_name,
                z_score=z_score,
                current=current_float,
                mean=mean,
                stddev=stddev,
                snapshot=current,
            )

            evidence = self._collect_evidence(
                metric_name=metric_name,
                current_snapshot=current,
                baseline_snapshots=baseline,
            )

            event = DriftEvent(
                snapshot_id=current.id,
                event_type=resolved_event_type,
                severity=severity,
                metric_name=metric_name,
                current_value=Decimal(str(round(current_float, 6))),
                baseline_mean=Decimal(str(round(mean, 6))),
                baseline_stddev=Decimal(str(round(stddev, 6))),
                z_score=Decimal(str(round(z_score, 4))),
                hypothesis=hypothesis,
                supporting_evidence=evidence,
            )
            events.append(event)

            log.warning(
                "drift_event_detected",
                metric=metric_name,
                z_score=round(z_score, 3),
                current=round(current_float, 4),
                baseline_mean=round(mean, 4),
                severity=severity.value,
            )

        return events

    def _generate_hypothesis(
        self,
        metric_name: str,
        z_score: float,
        current: float,
        mean: float,
        stddev: float,
        snapshot: ReconciliationSnapshot,
    ) -> str:
        """
        Rule-based hypothesis generation.
        Not ML — deliberately simple and explainable.
        Add rules as you learn patterns in your data.
        """
        direction = "decreased" if z_score < 0 else "increased"
        pct_change = abs((current - mean) / mean * 100) if mean != 0 else 0

        hypotheses = {
            "match_rate": {
                "low": [
                    f"Match rate dropped {pct_change:.1f}% below 30-day baseline ({mean:.1%} → {current:.1%}).",
                    "Possible causes: (1) New bank statement format — check reference field alignment. "
                    "(2) Batch of transactions with missing references. "
                    "(3) Currency mismatch introduced in upstream system. "
                    "(4) New merchant/counterparty with atypical formatting.",
                ],
                "high": [
                    f"Match rate spiked {pct_change:.1f}% above baseline — possible duplicate data.",
                    "Possible causes: (1) Same bank file ingested twice (check batch_id). "
                    "(2) Transactions pre-populated from last run.",
                ],
            },
            "avg_date_delta_days": {
                "high": [
                    f"Average date gap {direction} to {current:.1f} days (baseline: {mean:.1f}d).",
                    "Possible causes: (1) Bank posting delay increased — contact bank ops. "
                    "(2) Transaction date field changed from posting_date to value_date. "
                    "(3) Weekend/holiday batch processing backlog.",
                ],
            },
            "avg_confidence": {
                "low": [
                    f"Average confidence {direction} to {current:.2%} (baseline: {mean:.2%}).",
                    "Possible causes: (1) Reference field format changed in source system. "
                    "(2) Description truncation introduced. "
                    "(3) Amount rounding change in upstream system.",
                ],
            },
            "unmatched_count": {
                "both": [
                    f"Unmatched transaction count {direction} significantly (z={z_score:.2f}σ).",
                    "Possible causes: (1) Missing bank statement file for this period. "
                    "(2) Upstream transaction volume spike. "
                    "(3) Bank account change not reflected in ingestion config.",
                ],
            },
        }

        direction_key = "low" if z_score < 0 else "high"
        metric_hypos = hypotheses.get(metric_name, {})
        parts = metric_hypos.get(direction_key) or metric_hypos.get(
            "both",
            [
                f"{metric_name} {direction} significantly (z={z_score:.2f}σ). Manual investigation required."
            ],
        )

        return " ".join(parts)

    def _collect_evidence(
        self,
        metric_name: str,
        current_snapshot: ReconciliationSnapshot,
        baseline_snapshots: Sequence[ReconciliationSnapshot],
    ) -> dict:
        """Collect supporting data points to include with the drift event."""
        baseline_values = [
            {
                "run_id": s.run_id,
                "run_date": s.run_date.isoformat(),
                "value": float(getattr(s, metric_name) or 0),
            }
            for s in baseline_snapshots[-7:]  # Last 7 baseline points
        ]

        return {
            "metric": metric_name,
            "current_run_id": current_snapshot.run_id,
            "current_run_date": current_snapshot.run_date.isoformat(),
            "baseline_trend": baseline_values,
            "current_snapshot_summary": {
                "match_rate": float(current_snapshot.match_rate),
                "matched_count": current_snapshot.matched_count,
                "unmatched_count": current_snapshot.unmatched_count,
                "avg_confidence": float(current_snapshot.avg_confidence or 0),
            },
        }

    async def get_drift_summary(self, source_name: str) -> dict:
        """Summary of drift activity over the lookback window."""
        from sqlalchemy import func
        from app.models import DriftEvent as DE

        lookback = datetime.now(timezone.utc) - timedelta(
            days=self.settings.DRIFT_LOOKBACK_DAYS
        )

        # Latest match rate
        latest_q = (
            select(ReconciliationSnapshot)
            .where(ReconciliationSnapshot.source_name == source_name)
            .order_by(ReconciliationSnapshot.run_date.desc())
            .limit(1)
        )
        latest_result = await self.db.execute(latest_q)
        latest = latest_result.scalar_one_or_none()

        # Baseline match rate (avg of lookback)
        baseline_q = select(func.avg(ReconciliationSnapshot.match_rate)).where(
            and_(
                ReconciliationSnapshot.source_name == source_name,
                ReconciliationSnapshot.run_date >= lookback,
            )
        )
        baseline_result = await self.db.execute(baseline_q)
        baseline_mean = baseline_result.scalar()

        # Count drift events
        events_q = select(ReconciliationSnapshot.id).where(
            and_(
                ReconciliationSnapshot.source_name == source_name,
                ReconciliationSnapshot.run_date >= lookback,
            )
        )
        events_result = await self.db.execute(events_q)
        snapshot_ids = [r[0] for r in events_result.all()]

        drift_count = 0
        open_drift = 0
        by_type: dict = {}
        by_severity: dict = {}

        if snapshot_ids:
            drift_q = select(DE).where(DE.snapshot_id.in_(snapshot_ids))
            drift_result = await self.db.execute(drift_q)
            drift_events = drift_result.scalars().all()
            drift_count = len(drift_events)
            open_drift = sum(1 for e in drift_events if e.resolved_at is None)

            for e in drift_events:
                by_type[e.event_type] = by_type.get(e.event_type, 0) + 1
                by_severity[e.severity] = by_severity.get(e.severity, 0) + 1

        # Trend: compare first vs last half of baseline
        if latest and baseline_mean:
            latest_rate = float(latest.match_rate)
            base = float(baseline_mean)
            if abs(latest_rate - base) < 0.01:
                trend = "stable"
            elif latest_rate < base - 0.02:
                trend = "degrading"
            elif latest_rate > base + 0.02:
                trend = "improving"
            else:
                trend = "stable"
        else:
            trend = "insufficient_data"

        return {
            "lookback_days": self.settings.DRIFT_LOOKBACK_DAYS,
            "total_snapshots": len(snapshot_ids),
            "total_drift_events": drift_count,
            "open_drift_events": open_drift,
            "latest_match_rate": float(latest.match_rate) if latest else None,
            "baseline_match_rate": float(baseline_mean) if baseline_mean else None,
            "match_rate_trend": trend,
            "events_by_type": by_type,
            "events_by_severity": by_severity,
        }
