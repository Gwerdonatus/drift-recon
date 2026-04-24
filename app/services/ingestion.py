"""
Ingestion service.

Responsibilities:
1. Parse and validate CSV uploads
2. Enforce idempotency via external_id + source uniqueness
3. Quarantine invalid rows (dead-letter pattern) instead of failing the batch
4. Return a structured batch report

Design decisions:
- Chunked inserts (not row-by-row) for performance
- All-or-quarantine per row (bad rows never block good rows)
- batch_id is deterministic hash of file content — re-uploading same
  file produces same batch_id, enabling safe retries
"""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pandas as pd
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import IngestionError
from app.core.logging import get_logger
from app.models import (
    BankStatement,
    QuarantinedRecord,
    Transaction,
    TransactionStatus,
)
from app.schemas import BankStatementIngestionRow, IngestionResponse, TransactionIngestionRow

log = get_logger(__name__)

# ── CSV column mappings ────────────────────────────────────────────────────────
# Maps expected canonical names to common bank/internal naming variants.
# Add variants as you encounter new data sources.

TRANSACTION_COLUMN_MAP = {
    "external_id": ["external_id", "id", "txn_id", "transaction_id", "ref"],
    "transaction_date": ["transaction_date", "date", "txn_date", "trans_date"],
    "amount": ["amount", "value", "debit_credit", "net_amount"],
    "currency": ["currency", "ccy"],
    "reference": ["reference", "ref", "payment_ref", "memo"],
    "description": ["description", "narration", "details", "narrative"],
    "counterparty": ["counterparty", "payee", "beneficiary", "merchant"],
}

BANK_COLUMN_MAP = {
    "external_id": ["external_id", "id", "bank_ref", "statement_id", "transaction_ref"],
    "value_date": ["value_date", "date", "transaction_date", "posting_date"],
    "posting_date": ["posting_date", "booking_date"],
    "amount": ["amount", "debit_credit", "credit_debit", "net_amount"],
    "currency": ["currency", "ccy"],
    "reference": ["reference", "ref", "bank_ref", "memo"],
    "description": ["description", "narration", "details"],
    "counterparty": ["counterparty", "payer", "payee"],
}


def _normalize_columns(df: pd.DataFrame, column_map: dict[str, list[str]]) -> pd.DataFrame:
    """Rename dataframe columns to canonical names using the mapping."""
    df.columns = [c.lower().strip().replace(" ", "_") for c in df.columns]
    rename = {}
    for canonical, variants in column_map.items():
        for v in variants:
            if v in df.columns and canonical not in rename.values():
                rename[v] = canonical
                break
    return df.rename(columns=rename)


def _compute_batch_id(content: bytes, source: str) -> str:
    """
    Deterministic batch ID from file content hash.
    Re-uploading the exact same file will produce the same batch_id,
    which allows idempotent retries at the batch level.
    """
    h = hashlib.sha256(content + source.encode()).hexdigest()[:16]
    return f"batch_{h}"


async def ingest_transactions(
    db: AsyncSession,
    file_content: bytes,
    source: str,
) -> IngestionResponse:
    """
    Parse and ingest a transaction CSV file.
    Returns a batch report — never raises on partial failures.
    """
    batch_id = _compute_batch_id(file_content, source)
    log.info("ingestion_start", batch_id=batch_id, source=source)

    try:
        df = pd.read_csv(io.BytesIO(file_content), dtype=str)
    except Exception as e:
        raise IngestionError(f"Failed to parse CSV: {e}")

    df = _normalize_columns(df, TRANSACTION_COLUMN_MAP)

    accepted = 0
    quarantined = 0
    duplicate_skipped = 0
    errors: list[dict] = []

    rows_to_insert: list[dict] = []
    quarantine_rows: list[dict] = []

    for idx, row in df.iterrows():
        row_dict = row.where(pd.notna(row), None).to_dict()

        try:
            validated = TransactionIngestionRow(**row_dict)
        except (ValidationError, Exception) as e:
            quarantined += 1
            quarantine_rows.append({
                "ingestion_batch_id": batch_id,
                "source_type": "transaction",
                "row_index": int(str(idx)),
                "raw_data": row_dict,
                "failure_reason": str(e),
            })
            errors.append({"row": int(str(idx)), "error": str(e)})
            continue

        rows_to_insert.append({
            "id": uuid.uuid4(),
            "external_id": validated.external_id,
            "source": source,
            "transaction_date": validated.transaction_date,
            "amount": validated.amount,
            "currency": validated.currency,
            "reference": validated.reference,
            "description": validated.description,
            "counterparty": validated.counterparty,
            "status": TransactionStatus.PENDING,
            "raw_data": row_dict,
            "ingestion_batch_id": batch_id,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        })

    # Bulk upsert with conflict skip (idempotency)
    # ON CONFLICT DO NOTHING = skip duplicates without erroring
    if rows_to_insert:
        stmt = pg_insert(Transaction).values(rows_to_insert)
        stmt = stmt.on_conflict_do_nothing(
            constraint="uq_transaction_external_source"
        )
        result = await db.execute(stmt)
        # rowcount = rows actually inserted (excludes skipped duplicates)
        inserted = result.rowcount if result.rowcount >= 0 else len(rows_to_insert)
        duplicate_skipped = len(rows_to_insert) - inserted
        accepted = inserted

    if quarantine_rows:
        await db.execute(
            pg_insert(QuarantinedRecord).values(quarantine_rows)
        )

    log.info(
        "ingestion_complete",
        batch_id=batch_id,
        source=source,
        accepted=accepted,
        quarantined=quarantined,
        duplicate_skipped=duplicate_skipped,
    )

    return IngestionResponse(
        batch_id=batch_id,
        source=source,
        total_rows=len(df),
        accepted=accepted,
        quarantined=quarantined,
        duplicate_skipped=duplicate_skipped,
        errors=errors[:50],  # Cap error list in response
    )


async def ingest_bank_statements(
    db: AsyncSession,
    file_content: bytes,
    bank_name: str,
) -> IngestionResponse:
    """
    Parse and ingest a bank statement CSV file.
    Same idempotency and quarantine logic as transactions.
    """
    batch_id = _compute_batch_id(file_content, bank_name)
    log.info("bank_ingestion_start", batch_id=batch_id, bank_name=bank_name)

    try:
        df = pd.read_csv(io.BytesIO(file_content), dtype=str)
    except Exception as e:
        raise IngestionError(f"Failed to parse bank CSV: {e}")

    df = _normalize_columns(df, BANK_COLUMN_MAP)

    accepted = 0
    quarantined = 0
    duplicate_skipped = 0
    errors: list[dict] = []
    rows_to_insert: list[dict] = []
    quarantine_rows: list[dict] = []

    for idx, row in df.iterrows():
        row_dict = row.where(pd.notna(row), None).to_dict()

        try:
            validated = BankStatementIngestionRow(**row_dict)
        except (ValidationError, Exception) as e:
            quarantined += 1
            quarantine_rows.append({
                "ingestion_batch_id": batch_id,
                "source_type": "bank",
                "row_index": int(str(idx)),
                "raw_data": row_dict,
                "failure_reason": str(e),
            })
            errors.append({"row": int(str(idx)), "error": str(e)})
            continue

        rows_to_insert.append({
            "id": uuid.uuid4(),
            "external_id": validated.external_id,
            "bank_name": bank_name,
            "value_date": validated.value_date,
            "posting_date": validated.posting_date,
            "amount": validated.amount,
            "currency": validated.currency,
            "reference": validated.reference,
            "description": validated.description,
            "counterparty": validated.counterparty,
            "status": TransactionStatus.PENDING,
            "raw_data": row_dict,
            "ingestion_batch_id": batch_id,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        })

    if rows_to_insert:
        stmt = pg_insert(BankStatement).values(rows_to_insert)
        stmt = stmt.on_conflict_do_nothing(
            constraint="uq_bank_external_bank"
        )
        result = await db.execute(stmt)
        inserted = result.rowcount if result.rowcount >= 0 else len(rows_to_insert)
        duplicate_skipped = len(rows_to_insert) - inserted
        accepted = inserted

    if quarantine_rows:
        await db.execute(
            pg_insert(QuarantinedRecord).values(quarantine_rows)
        )

    return IngestionResponse(
        batch_id=batch_id,
        source=bank_name,
        total_rows=len(df),
        accepted=accepted,
        quarantined=quarantined,
        duplicate_skipped=duplicate_skipped,
        errors=errors[:50],
    )
