"""
Ingestion endpoints.

All endpoints require API key authentication.
File uploads use multipart/form-data.

Rate limiting: 10 uploads/minute per key (enforced in Redis middleware).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_api_key
from app.database import get_db
from app.schemas import IngestionResponse
from app.services.ingestion import ingest_bank_statements, ingest_transactions

router = APIRouter(dependencies=[Depends(verify_api_key)])

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB


@router.post(
    "/transactions",
    response_model=IngestionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest transaction CSV",
    description=(
        "Upload a CSV of internal transactions. "
        "Duplicate rows (same external_id + source) are silently skipped. "
        "Invalid rows are quarantined — they don't fail the batch. "
        "Re-uploading the same file is safe (idempotent via content hash)."
    ),
)
async def upload_transactions(
    file: UploadFile = File(..., description="CSV file of transactions"),
    source: str = Form(
        ...,
        min_length=1,
        max_length=100,
        description="Source system name, e.g. 'erp' or 'pos'",
    ),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read(MAX_UPLOAD_SIZE + 1)

    if len(content) > MAX_UPLOAD_SIZE:
        from fastapi import HTTPException

        raise HTTPException(status_code=413, detail="File exceeds 50MB limit.")

    return await ingest_transactions(db=db, file_content=content, source=source)


@router.post(
    "/bank-statements",
    response_model=IngestionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest bank statement CSV",
)
async def upload_bank_statements(
    file: UploadFile = File(..., description="Bank statement CSV"),
    bank_name: str = Form(
        ...,
        min_length=1,
        max_length=100,
        description="Bank identifier, e.g. 'chase' or 'wells_fargo'",
    ),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read(MAX_UPLOAD_SIZE + 1)

    if len(content) > MAX_UPLOAD_SIZE:
        from fastapi import HTTPException

        raise HTTPException(status_code=413, detail="File exceeds 50MB limit.")

    return await ingest_bank_statements(
        db=db, file_content=content, bank_name=bank_name
    )
