from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import ReconciliationSnapshot
from app.schemas import ReconciliationRunRequest
from tests.conftest import make_bank_entry, make_transaction


def test_inverted_thresholds_are_rejected():
    with pytest.raises(ValueError, match="review_threshold"):
        ReconciliationRunRequest(
            source_name="demo", confidence_threshold=0.4, review_threshold=0.8
        )


@pytest.mark.asyncio
async def test_snapshot_exposes_current_run_and_actual_matched_amount(
    client, db_session
):
    txn = make_transaction()
    bank = make_bank_entry()
    bank.bank_name = txn.source
    db_session.add_all([txn, bank])
    await db_session.flush()
    response = await client.post(
        "/api/v1/reconciliation/run", json={"source_name": txn.source}
    )
    assert response.status_code == 200, response.text
    assert response.json()["matched"] == 1
    snapshot = (await db_session.execute(select(ReconciliationSnapshot))).scalar_one()
    assert snapshot.total_amount_matched == Decimal("1000")
    snapshots = await client.get("/api/v1/reconciliation/snapshots")
    assert snapshots.status_code == 200
    assert snapshots.json()[0]["run_id"] == response.json()["run_id"]
    assert snapshots.json()[0]["review_count"] == 0
    assert snapshots.json()[0]["p50_confidence"] > 0.75


@pytest.mark.asyncio
async def test_currency_is_preserved_between_database_and_matcher(client, db_session):
    txn = make_transaction()
    bank = make_bank_entry()
    bank.bank_name = txn.source
    bank.currency = "EUR"
    db_session.add_all([txn, bank])
    await db_session.flush()
    response = await client.post(
        "/api/v1/reconciliation/run", json={"source_name": txn.source}
    )
    assert response.status_code == 200, response.text
    assert response.json()["matched"] == 0
    assert response.json()["unmatched"] == 1


@pytest.mark.asyncio
async def test_drift_analysis_is_repeatable_and_events_serialize(client, db_session):
    now = datetime.now(timezone.utc)
    for index, rate in enumerate([0.90, 0.91, 0.92, 0.93, 0.91, 0.90, 0.92, 0.25]):
        db_session.add(
            ReconciliationSnapshot(
                run_id=f"baseline-{index}",
                run_date=now - timedelta(days=7 - index),
                source_name="drift-proof",
                match_rate=Decimal(str(rate)),
                total_transactions=100,
                matched_count=int(rate * 100),
                unmatched_count=100 - int(rate * 100),
            )
        )
    await db_session.flush()
    first = await client.post("/api/v1/drift/analyze/drift-proof")
    assert first.status_code == 200, first.text
    assert first.json()["events_detected"] > 0
    await db_session.flush()
    second = await client.post("/api/v1/drift/analyze/drift-proof")
    assert second.status_code == 200
    assert second.json()["events_detected"] == 0
    events = await client.get(
        "/api/v1/drift/events", params={"source_name": "drift-proof"}
    )
    assert events.status_code == 200, events.text
    assert len(events.json()) == first.json()["events_detected"]
    assert all(event["created_at"] for event in events.json())


@pytest.mark.asyncio
async def test_database_failure_produces_unhealthy_http_status(client):
    from unittest.mock import AsyncMock, patch

    with patch(
        "app.main.check_db_health", new=AsyncMock(return_value={"status": "unhealthy"})
    ):
        response = await client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
