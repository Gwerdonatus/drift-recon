"""
Database models.

Design notes:
- All PKs are UUIDs (no auto-increment leaking row counts to clients)
- external_id on ingestion tables enables idempotency checks
- Indexes are explicit and motivated (not "add index to everything")
- Soft deletes via deleted_at (audit trail)
- Amount stored as NUMERIC(18,4) — never FLOAT for money
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ─── Enums ─────────────────────────────────────────────────────────────────────

class TransactionStatus(str, Enum):
    PENDING = "pending"
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    REVIEW = "review"           # Confidence between review and match threshold
    QUARANTINED = "quarantined" # Failed validation during ingestion


class MatchStatus(str, Enum):
    MATCHED = "matched"         # Confidence >= MATCH_CONFIDENCE_THRESHOLD
    REVIEW = "review"           # Between REVIEW and MATCH thresholds
    UNMATCHED = "unmatched"     # Below REVIEW threshold
    DUPLICATE = "duplicate"     # Same transaction matched twice (data quality issue)


class DriftEventType(str, Enum):
    MATCH_RATE_DROP = "match_rate_drop"
    MATCH_RATE_SPIKE = "match_rate_spike"
    AMOUNT_SKEW = "amount_skew"
    DATE_SKEW = "date_skew"
    VOLUME_ANOMALY = "volume_anomaly"
    REFERENCE_PATTERN_CHANGE = "reference_pattern_change"


class DriftSeverity(str, Enum):
    LOW = "low"         # 2–2.5 sigma
    MEDIUM = "medium"   # 2.5–3 sigma
    HIGH = "high"       # > 3 sigma


# ─── Mixins ────────────────────────────────────────────────────────────────────

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


# ─── Core Tables ───────────────────────────────────────────────────────────────

class Transaction(Base, TimestampMixin, SoftDeleteMixin):
    """
    Internal transaction records (from your system of record).
    These are what you're trying to reconcile against bank statements.
    """
    __tablename__ = "transactions"
    __table_args__ = (
        # Idempotency: external_id must be unique per source
        UniqueConstraint("external_id", "source", name="uq_transaction_external_source"),
        # Covering index for the most common reconciliation lookup
        Index("ix_transactions_date_amount", "transaction_date", "amount"),
        Index("ix_transactions_status", "status"),
        Index("ix_transactions_reference", "reference"),
        CheckConstraint("amount != 0", name="chk_transaction_nonzero_amount"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Idempotency key: the ID from your source system
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g., "erp", "pos"

    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    counterparty: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[TransactionStatus] = mapped_column(
        String(20), nullable=False, default=TransactionStatus.PENDING
    )

    # Raw ingestion metadata (for debugging)
    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ingestion_batch_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Relationships
    reconciliation_results: Mapped[list["ReconciliationResult"]] = relationship(
        "ReconciliationResult",
        foreign_keys="ReconciliationResult.transaction_id",
        back_populates="transaction",
    )


class BankStatement(Base, TimestampMixin, SoftDeleteMixin):
    """
    Bank statement entries (from bank CSV or API feed).
    The external reference against which transactions are matched.
    """
    __tablename__ = "bank_statements"
    __table_args__ = (
        UniqueConstraint("external_id", "bank_name", name="uq_bank_external_bank"),
        Index("ix_bank_date_amount", "value_date", "amount"),
        Index("ix_bank_reference", "reference"),
        Index("ix_bank_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(100), nullable=False)

    value_date: Mapped[date] = mapped_column(Date, nullable=False)
    posting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    counterparty: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[TransactionStatus] = mapped_column(
        String(20), nullable=False, default=TransactionStatus.PENDING
    )

    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ingestion_batch_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    reconciliation_results: Mapped[list["ReconciliationResult"]] = relationship(
        "ReconciliationResult",
        foreign_keys="ReconciliationResult.bank_statement_id",
        back_populates="bank_statement",
    )


class ReconciliationResult(Base, TimestampMixin):
    """
    Output of the matching engine for a single transaction–statement pair.
    
    Stores confidence scores broken down by component so we can later
    diagnose which factor caused a low-confidence match or miss.
    """
    __tablename__ = "reconciliation_results"
    __table_args__ = (
        Index("ix_recon_run_id", "run_id"),
        Index("ix_recon_status", "status"),
        Index("ix_recon_confidence", "confidence_score"),
        # One transaction can only be matched once (prevent duplicate matches)
        UniqueConstraint(
            "transaction_id", "run_id", name="uq_recon_transaction_run"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[str] = mapped_column(String(100), nullable=False)

    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=True
    )
    bank_statement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_statements.id"), nullable=True
    )

    status: Mapped[MatchStatus] = mapped_column(String(20), nullable=False)

    # Composite confidence score and per-factor breakdown
    confidence_score: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, default=0
    )
    score_amount: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, default=0)
    score_date: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, default=0)
    score_reference: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, default=0)
    score_description: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, default=0)

    # Human-readable explanation for the match decision
    match_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Amount delta (positive = bank > internal, negative = bank < internal)
    amount_delta: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    date_delta_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Was this match reviewed/overridden by a human?
    human_reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    human_verdict: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    transaction: Mapped["Transaction | None"] = relationship(
        "Transaction", back_populates="reconciliation_results"
    )
    bank_statement: Mapped["BankStatement | None"] = relationship(
        "BankStatement", back_populates="reconciliation_results"
    )


class ReconciliationSnapshot(Base, TimestampMixin):
    """
    Aggregate statistics for each reconciliation run.
    
    This is the historical record that drift detection analyzes.
    Every run appends one row here — never update, only insert.
    Think of it as an append-only event log.
    """
    __tablename__ = "reconciliation_snapshots"
    __table_args__ = (
        Index("ix_snapshot_run_date", "run_date"),
        Index("ix_snapshot_source", "source_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    run_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Volume metrics
    total_transactions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_bank_entries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    review_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unmatched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Rate metrics (stored for fast access — derived from counts above)
    match_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, default=0)
    review_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, default=0)

    # Distribution metrics
    avg_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    p50_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    p10_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)

    # Amount metrics
    total_amount_matched: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    total_amount_unmatched: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    avg_amount_delta: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)

    # Timing metrics
    avg_date_delta_days: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    max_date_delta_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Run performance
    run_duration_seconds: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)

    drift_events: Mapped[list["DriftEvent"]] = relationship(
        "DriftEvent", back_populates="snapshot"
    )


class DriftEvent(Base, TimestampMixin):
    """
    A detected anomaly in reconciliation performance.
    
    Created by the DriftAnalyzer when a snapshot's metrics deviate
    significantly from historical baseline (z-score based detection).
    Each event includes a root cause hypothesis and supporting evidence.
    """
    __tablename__ = "drift_events"
    __table_args__ = (
        Index("ix_drift_snapshot_id", "snapshot_id"),
        Index("ix_drift_event_type", "event_type"),
        Index("ix_drift_severity", "severity"),
        Index("ix_drift_resolved", "resolved_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_snapshots.id"), nullable=False
    )

    event_type: Mapped[DriftEventType] = mapped_column(String(50), nullable=False)
    severity: Mapped[DriftSeverity] = mapped_column(String(10), nullable=False)

    # Statistical evidence
    metric_name: Mapped[str] = mapped_column(String(100), nullable=False)
    current_value: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    baseline_mean: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    baseline_stddev: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    z_score: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)

    # Diagnosis
    hypothesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    supporting_evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Resolution tracking
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    snapshot: Mapped["ReconciliationSnapshot"] = relationship(
        "ReconciliationSnapshot", back_populates="drift_events"
    )


class QuarantinedRecord(Base, TimestampMixin):
    """
    Records that failed validation during ingestion.
    Dead-letter pattern: don't discard bad data, park it for investigation.
    """
    __tablename__ = "quarantined_records"
    __table_args__ = (
        Index("ix_quarantine_batch", "ingestion_batch_id"),
        Index("ix_quarantine_source", "source_type"),
        Index("ix_quarantine_resolved", "resolved_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ingestion_batch_id: Mapped[str] = mapped_column(String(100), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)  # "transaction" | "bank"
    row_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    failure_reason: Mapped[str] = mapped_column(Text, nullable=False)

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
