"""
Pydantic v2 schemas for API layer.

Separation from models: SQLAlchemy models own DB structure,
Pydantic schemas own API contracts. They should NOT be coupled.
This allows independent evolution of each layer.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ─── Shared base ───────────────────────────────────────────────────────────────


class APIModel(BaseModel):
    """Base for all API schemas: strict mode, no extra fields."""

    model_config = ConfigDict(
        from_attributes=True,  # Allow ORM model -> schema conversion
        str_strip_whitespace=True,
        populate_by_name=True,
    )


# ─── Ingestion ─────────────────────────────────────────────────────────────────


class TransactionIngestionRow(APIModel):
    """Single row from a transaction CSV upload."""

    external_id: str = Field(..., min_length=1, max_length=255)
    transaction_date: date
    amount: Decimal = Field(max_digits=18, decimal_places=4)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    reference: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    counterparty: str | None = Field(default=None, max_length=255)

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, v: str) -> str:
        return v.upper()

    @field_validator("amount", mode="before")
    @classmethod
    def parse_amount(cls, v: Any) -> Decimal:
        if isinstance(v, str):
            v = v.replace(",", "").strip()
        amount = Decimal(str(v))
        if amount == 0:
            raise ValueError("amount must not be zero")
        return amount


class BankStatementIngestionRow(APIModel):
    """Single row from a bank statement CSV upload."""

    external_id: str = Field(..., min_length=1, max_length=255)
    value_date: date
    posting_date: date | None = None
    amount: Decimal = Field(max_digits=18, decimal_places=4)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    reference: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    counterparty: str | None = Field(default=None, max_length=255)

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, v: str) -> str:
        return v.upper()

    @field_validator("amount", mode="before")
    @classmethod
    def parse_amount(cls, v: Any) -> Decimal:
        if isinstance(v, str):
            v = v.replace(",", "").strip()
        amount = Decimal(str(v))
        if amount == 0:
            raise ValueError("amount must not be zero")
        return amount


class IngestionResponse(APIModel):
    batch_id: str
    source: str
    total_rows: int
    accepted: int
    quarantined: int
    duplicate_skipped: int
    errors: list[dict] = Field(default_factory=list)


# ─── Reconciliation ────────────────────────────────────────────────────────────


class ReconciliationRunRequest(APIModel):
    source_name: str = Field(..., min_length=1, max_length=100)
    # Optional date range filter — default to last 30 days
    from_date: date | None = None
    to_date: date | None = None
    # Override config thresholds per-run (for testing)
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    review_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_date_range(self) -> "ReconciliationRunRequest":
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("from_date must be before to_date")
        from app.config import get_settings

        settings = get_settings()
        match = (
            self.confidence_threshold
            if self.confidence_threshold is not None
            else settings.MATCH_CONFIDENCE_THRESHOLD
        )
        review = (
            self.review_threshold
            if self.review_threshold is not None
            else settings.MATCH_REVIEW_THRESHOLD
        )
        if review > match:
            raise ValueError("review_threshold must not exceed confidence_threshold")
        return self


class MatchScoreBreakdown(APIModel):
    amount: float
    date: float
    reference: float
    description: float
    composite: float


class ReconciliationResultOut(APIModel):
    id: uuid.UUID
    run_id: str
    transaction_id: uuid.UUID | None
    bank_statement_id: uuid.UUID | None
    status: str
    confidence_score: float
    score_breakdown: MatchScoreBreakdown
    match_reason: str | None
    amount_delta: Decimal | None
    date_delta_days: int | None
    human_reviewed: bool
    human_verdict: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_orm_with_scores(cls, obj: Any) -> "ReconciliationResultOut":
        return cls(
            id=obj.id,
            run_id=obj.run_id,
            transaction_id=obj.transaction_id,
            bank_statement_id=obj.bank_statement_id,
            status=obj.status,
            confidence_score=float(obj.confidence_score),
            score_breakdown=MatchScoreBreakdown(
                amount=float(obj.score_amount),
                date=float(obj.score_date),
                reference=float(obj.score_reference),
                description=float(obj.score_description),
                composite=float(obj.confidence_score),
            ),
            match_reason=obj.match_reason,
            amount_delta=obj.amount_delta,
            date_delta_days=obj.date_delta_days,
            human_reviewed=obj.human_reviewed,
            human_verdict=obj.human_verdict,
            reviewed_by=obj.reviewed_by,
            reviewed_at=obj.reviewed_at,
            created_at=obj.created_at,
        )


class ReconciliationRunResponse(APIModel):
    run_id: str
    source_name: str
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    total_transactions: int
    total_bank_entries: int
    matched: int
    review: int
    unmatched: int
    match_rate: float
    avg_confidence: float
    drift_events_raised: int


# ─── Snapshots & Drift ─────────────────────────────────────────────────────────


class SnapshotOut(APIModel):
    id: uuid.UUID
    run_id: str
    run_date: datetime
    source_name: str
    total_transactions: int
    total_bank_entries: int
    matched_count: int
    review_count: int
    p50_confidence: float | None
    p10_confidence: float | None
    unmatched_count: int
    match_rate: float
    avg_confidence: float | None
    run_duration_seconds: float | None


class DriftEventOut(APIModel):
    id: uuid.UUID
    snapshot_id: uuid.UUID
    event_type: str
    severity: str
    metric_name: str
    current_value: float
    baseline_mean: float
    baseline_stddev: float
    z_score: float
    hypothesis: str | None
    supporting_evidence: dict | None
    resolved_at: datetime | None
    resolution_notes: str | None
    created_at: datetime


class DriftSummary(APIModel):
    lookback_days: int
    total_snapshots: int
    total_drift_events: int
    open_drift_events: int
    latest_match_rate: float | None
    baseline_match_rate: float | None
    match_rate_trend: str  # "stable" | "degrading" | "improving" | "insufficient_data"
    events_by_type: dict[str, int]
    events_by_severity: dict[str, int]


# ─── Health ────────────────────────────────────────────────────────────────────


class HealthCheck(APIModel):
    status: str  # "healthy" | "degraded" | "unhealthy"
    version: str
    environment: str
    checks: dict[str, dict]
    timestamp: datetime
