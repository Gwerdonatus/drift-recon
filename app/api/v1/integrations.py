"""Authenticated read-only provider imports into local reconciliation evidence."""

from datetime import date, timedelta
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.security import verify_api_key
from app.database import get_db
from app.services.stripe_sandbox import (
    StripeConnectionError,
    connection_status,
    sync_charges,
)

router = APIRouter(dependencies=[Depends(verify_api_key)])


class StripeSyncRequest(BaseModel):
    from_date: date | None = None
    to_date: date | None = None


@router.get("/stripe/status")
async def stripe_status():
    try:
        return await connection_status()
    except StripeConnectionError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.post("/stripe/sync")
async def stripe_sync(body: StripeSyncRequest, db: AsyncSession = Depends(get_db)):
    end = body.to_date or date.today()
    start = body.from_date or end - timedelta(days=30)
    try:
        return await sync_charges(db, start, end)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except StripeConnectionError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
