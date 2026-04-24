"""Initial schema

Revision ID: 001_initial
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── transactions ──────────────────────────────────────────────────────
    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reference", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("counterparty", sa.String(255), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("raw_data", postgresql.JSONB(), nullable=True),
        sa.Column("ingestion_batch_id", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id", "source", name="uq_transaction_external_source"),
        sa.CheckConstraint("amount != 0", name="chk_transaction_nonzero_amount"),
    )
    op.create_index("ix_transactions_date_amount", "transactions", ["transaction_date", "amount"])
    op.create_index("ix_transactions_status", "transactions", ["status"])
    op.create_index("ix_transactions_reference", "transactions", ["reference"])

    # ── bank_statements ───────────────────────────────────────────────────
    op.create_table(
        "bank_statements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("bank_name", sa.String(100), nullable=False),
        sa.Column("value_date", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reference", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("counterparty", sa.String(255), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("raw_data", postgresql.JSONB(), nullable=True),
        sa.Column("ingestion_batch_id", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id", "bank_name", name="uq_bank_external_bank"),
    )
    op.create_index("ix_bank_date_amount", "bank_statements", ["value_date", "amount"])
    op.create_index("ix_bank_reference", "bank_statements", ["reference"])
    op.create_index("ix_bank_status", "bank_statements", ["status"])

    # ── reconciliation_results ────────────────────────────────────────────
    op.create_table(
        "reconciliation_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", sa.String(100), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("bank_statement_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("confidence_score", sa.Numeric(5, 4), nullable=False),
        sa.Column("score_amount", sa.Numeric(5, 4), nullable=False),
        sa.Column("score_date", sa.Numeric(5, 4), nullable=False),
        sa.Column("score_reference", sa.Numeric(5, 4), nullable=False),
        sa.Column("score_description", sa.Numeric(5, 4), nullable=False),
        sa.Column("match_reason", sa.Text(), nullable=True),
        sa.Column("amount_delta", sa.Numeric(18, 4), nullable=True),
        sa.Column("date_delta_days", sa.Integer(), nullable=True),
        sa.Column("human_reviewed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("human_verdict", sa.String(20), nullable=True),
        sa.Column("reviewed_by", sa.String(100), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
        sa.ForeignKeyConstraint(["bank_statement_id"], ["bank_statements.id"]),
        sa.UniqueConstraint("transaction_id", "run_id", name="uq_recon_transaction_run"),
    )
    op.create_index("ix_recon_run_id", "reconciliation_results", ["run_id"])
    op.create_index("ix_recon_status", "reconciliation_results", ["status"])
    op.create_index("ix_recon_confidence", "reconciliation_results", ["confidence_score"])

    # ── reconciliation_snapshots ──────────────────────────────────────────
    op.create_table(
        "reconciliation_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", sa.String(100), nullable=False),
        sa.Column("run_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_name", sa.String(100), nullable=False),
        sa.Column("total_transactions", sa.Integer(), nullable=False),
        sa.Column("total_bank_entries", sa.Integer(), nullable=False),
        sa.Column("matched_count", sa.Integer(), nullable=False),
        sa.Column("review_count", sa.Integer(), nullable=False),
        sa.Column("unmatched_count", sa.Integer(), nullable=False),
        sa.Column("match_rate", sa.Numeric(5, 4), nullable=False),
        sa.Column("review_rate", sa.Numeric(5, 4), nullable=False),
        sa.Column("avg_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("p50_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("p10_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("total_amount_matched", sa.Numeric(20, 4), nullable=True),
        sa.Column("total_amount_unmatched", sa.Numeric(20, 4), nullable=True),
        sa.Column("avg_amount_delta", sa.Numeric(18, 4), nullable=True),
        sa.Column("avg_date_delta_days", sa.Numeric(6, 2), nullable=True),
        sa.Column("max_date_delta_days", sa.Integer(), nullable=True),
        sa.Column("run_duration_seconds", sa.Numeric(10, 3), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_snapshot_run_date", "reconciliation_snapshots", ["run_date"])
    op.create_index("ix_snapshot_source", "reconciliation_snapshots", ["source_name"])

    # ── drift_events ──────────────────────────────────────────────────────
    op.create_table(
        "drift_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("metric_name", sa.String(100), nullable=False),
        sa.Column("current_value", sa.Numeric(18, 6), nullable=False),
        sa.Column("baseline_mean", sa.Numeric(18, 6), nullable=False),
        sa.Column("baseline_stddev", sa.Numeric(18, 6), nullable=False),
        sa.Column("z_score", sa.Numeric(8, 4), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=True),
        sa.Column("supporting_evidence", postgresql.JSONB(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["reconciliation_snapshots.id"]),
    )
    op.create_index("ix_drift_snapshot_id", "drift_events", ["snapshot_id"])
    op.create_index("ix_drift_event_type", "drift_events", ["event_type"])
    op.create_index("ix_drift_severity", "drift_events", ["severity"])
    op.create_index("ix_drift_resolved", "drift_events", ["resolved_at"])

    # ── quarantined_records ───────────────────────────────────────────────
    op.create_table(
        "quarantined_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ingestion_batch_id", sa.String(100), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=True),
        sa.Column("raw_data", postgresql.JSONB(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_quarantine_batch", "quarantined_records", ["ingestion_batch_id"])
    op.create_index("ix_quarantine_source", "quarantined_records", ["source_type"])
    op.create_index("ix_quarantine_resolved", "quarantined_records", ["resolved_at"])


def downgrade() -> None:
    op.drop_table("quarantined_records")
    op.drop_table("drift_events")
    op.drop_table("reconciliation_snapshots")
    op.drop_table("reconciliation_results")
    op.drop_table("bank_statements")
    op.drop_table("transactions")
