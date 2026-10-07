"""
Integration tests for the ingestion API.
Tests the full stack: HTTP → FastAPI → service → database.
"""

from __future__ import annotations

import io

import pytest


def make_csv(rows: list[dict], extra_header: str = "") -> bytes:
    """Build a minimal CSV for testing."""
    if not rows:
        return b"external_id,transaction_date,amount\n"
    headers = list(rows[0].keys())
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(str(row[h]) for h in headers))
    return "\n".join(lines).encode()


VALID_TRANSACTIONS = [
    {
        "external_id": f"TXN-{i:04d}",
        "transaction_date": "2024-01-15",
        "amount": f"{100 * (i + 1)}.00",
        "currency": "USD",
        "reference": f"REF-{i:04d}",
        "description": f"Payment {i}",
        "counterparty": "Vendor Inc",
    }
    for i in range(10)
]

VALID_BANK_ENTRIES = [
    {
        "external_id": f"BANK-{i:04d}",
        "value_date": "2024-01-15",
        "amount": f"{100 * (i + 1)}.00",
        "currency": "USD",
        "reference": f"REF-{i:04d}",
        "description": f"Payment {i}",
        "counterparty": "Vendor Inc",
    }
    for i in range(10)
]


class TestTransactionIngestion:

    @pytest.mark.asyncio
    async def test_upload_valid_csv(self, client):
        csv_bytes = make_csv(VALID_TRANSACTIONS)
        resp = await client.post(
            "/api/v1/ingest/transactions",
            files={"file": ("transactions.csv", io.BytesIO(csv_bytes), "text/csv")},
            data={"source": "test_erp"},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["accepted"] == 10
        assert body["quarantined"] == 0
        assert body["duplicate_skipped"] == 0
        assert body["batch_id"].startswith("batch_")

    @pytest.mark.asyncio
    async def test_idempotent_reupload(self, client):
        """Uploading the same file twice should skip duplicates, not fail."""
        csv_bytes = make_csv(VALID_TRANSACTIONS)
        params = {
            "files": {"file": ("transactions.csv", io.BytesIO(csv_bytes), "text/csv")},
            "data": {"source": "test_erp"},
        }

        r1 = await client.post("/api/v1/ingest/transactions", **params)
        # Reset the BytesIO
        params["files"]["file"] = (
            "transactions.csv",
            io.BytesIO(csv_bytes),
            "text/csv",
        )
        r2 = await client.post("/api/v1/ingest/transactions", **params)

        assert r1.status_code == 202
        assert r2.status_code == 202
        # Second upload: all should be skipped as duplicates
        assert r2.json()["duplicate_skipped"] == 10
        assert r2.json()["accepted"] == 0

    @pytest.mark.asyncio
    async def test_quarantines_invalid_rows(self, client):
        """Rows with bad amounts should be quarantined, rest accepted."""
        rows = VALID_TRANSACTIONS[:3] + [
            {
                "external_id": "BAD-001",
                "transaction_date": "2024-01-15",
                "amount": "NOT_A_NUMBER",
                "currency": "USD",
                "reference": "REF-BAD",
                "description": "Bad row",
                "counterparty": "",
            }
        ]
        csv_bytes = make_csv(rows)
        resp = await client.post(
            "/api/v1/ingest/transactions",
            files={"file": ("mix.csv", io.BytesIO(csv_bytes), "text/csv")},
            data={"source": "test_erp_bad"},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["accepted"] == 3
        assert body["quarantined"] == 1
        assert len(body["errors"]) == 1

    @pytest.mark.asyncio
    async def test_requires_api_key(self, client):
        """Request without API key should return 401."""
        csv_bytes = make_csv(VALID_TRANSACTIONS[:1])
        resp = await client.post(
            "/api/v1/ingest/transactions",
            files={"file": ("t.csv", io.BytesIO(csv_bytes), "text/csv")},
            data={"source": "test"},
            headers={"X-API-Key": ""},  # Override fixture header
        )
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_missing_required_columns_quarantines_all(self, client):
        """CSV with no amount column should quarantine everything."""
        csv = b"external_id,transaction_date\nTXN-001,2024-01-15\n"
        resp = await client.post(
            "/api/v1/ingest/transactions",
            files={"file": ("bad_schema.csv", io.BytesIO(csv), "text/csv")},
            data={"source": "bad_source"},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["quarantined"] == 1
        assert body["accepted"] == 0

    @pytest.mark.asyncio
    async def test_zero_amount_quarantined(self, client):
        """Zero amount transactions are invalid (CheckConstraint)."""
        rows = [
            {
                "external_id": "TXN-ZERO",
                "transaction_date": "2024-01-15",
                "amount": "0.00",
                "currency": "USD",
                "reference": "REF-ZERO",
                "description": "Zero amount",
                "counterparty": "",
            }
        ]
        csv_bytes = make_csv(rows)
        resp = await client.post(
            "/api/v1/ingest/transactions",
            files={"file": ("zero.csv", io.BytesIO(csv_bytes), "text/csv")},
            data={"source": "zero_source"},
        )
        assert resp.status_code == 202
        assert resp.json()["quarantined"] == 1


class TestBankStatementIngestion:

    @pytest.mark.asyncio
    async def test_upload_valid_bank_csv(self, client):
        csv_bytes = make_csv(VALID_BANK_ENTRIES)
        resp = await client.post(
            "/api/v1/ingest/bank-statements",
            files={"file": ("bank.csv", io.BytesIO(csv_bytes), "text/csv")},
            data={"bank_name": "test_bank"},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["accepted"] == 10
        assert body["quarantined"] == 0

    @pytest.mark.asyncio
    async def test_bank_idempotent(self, client):
        csv_bytes = make_csv(VALID_BANK_ENTRIES)

        r1 = await client.post(
            "/api/v1/ingest/bank-statements",
            files={"file": ("bank.csv", io.BytesIO(csv_bytes), "text/csv")},
            data={"bank_name": "test_bank_idem"},
        )
        r2 = await client.post(
            "/api/v1/ingest/bank-statements",
            files={"file": ("bank.csv", io.BytesIO(csv_bytes), "text/csv")},
            data={"bank_name": "test_bank_idem"},
        )
        assert r1.json()["accepted"] == 10
        assert r2.json()["duplicate_skipped"] == 10
