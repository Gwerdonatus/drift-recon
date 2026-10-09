from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.config import get_settings
from app.models import BankStatement
from app.services import stripe_sandbox as stripe


def sample(currency="usd"):
    gross = 300
    balance = {
        "id": "txn_example",
        "type": "charge",
        "source": "ch_example",
        "currency": currency,
        "amount": gross,
        "fee": 39,
        "net": 261,
        "status": "pending",
        "available_on": 1791590400,
    }
    charge = {
        "id": "ch_example",
        "livemode": False,
        "paid": True,
        "captured": True,
        "currency": currency,
        "amount": gross,
        "payment_intent": "pi_example",
        "created": int(datetime.now(timezone.utc).timestamp()),
    }
    return balance, charge


def provider(monkeypatch, handler):
    def client():
        return httpx.AsyncClient(
            base_url="https://api.stripe.com/v1/",
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(stripe, "stripe_client", client)


def test_currency_precision_and_gross_not_net():
    balance, charge = sample()
    row = stripe.charge_row(balance, charge, "acct_test")
    assert row["amount"] == Decimal("3")
    assert row["raw_data"]["fee_minor"] == 39
    assert row["raw_data"]["net_minor"] == 261
    assert row["reference"] == "pi_example"
    balance, charge = sample("jpy")
    assert stripe.charge_row(balance, charge, "acct_test")["amount"] == Decimal("300")


@pytest.mark.parametrize("change", ["live", "invalid_net", "invalid_id"])
def test_unsafe_evidence_is_rejected(change):
    balance, charge = sample()
    if change == "live":
        charge["livemode"] = True
    if change == "invalid_net":
        balance["net"] = 300
    if change == "invalid_id":
        balance["id"] = "../../other"
    with pytest.raises(stripe.StripeConnectionError):
        stripe.charge_row(balance, charge, "acct_test")


def test_unsupported_currency_and_fx_are_not_converted():
    balance, charge = sample("isk")
    assert stripe.charge_row(balance, charge, "acct_test") is None
    balance, charge = sample()
    charge["currency"] = "eur"
    assert stripe.charge_row(balance, charge, "acct_test") is None
    charge["currency"] = "usd"
    charge["amount"] = 400
    assert stripe.charge_row(balance, charge, "acct_test") is None


def test_live_keys_rejected_and_secret_masked():
    config = get_settings().model_dump()
    config["STRIPE_SECRET_KEY"] = "sk_live_notallowed"
    with pytest.raises(ValueError, match="sandbox secret") as error:
        type(get_settings())(**config)
    assert "sk_live_notallowed" not in str(error.value)
    with patch.object(
        stripe,
        "get_settings",
        return_value=SimpleNamespace(STRIPE_SECRET_KEY=SecretStr("sk_live_bad")),
    ):
        with pytest.raises(stripe.StripeConnectionError):
            stripe.sandbox_key()


@pytest.mark.asyncio
async def test_unconfigured_connection():
    with patch.object(
        stripe,
        "get_settings",
        return_value=SimpleNamespace(STRIPE_SECRET_KEY=SecretStr("")),
    ):
        assert (await stripe.connection_status())["configured"] is False


@pytest.mark.asyncio
async def test_provider_import_is_paginated_idempotent_and_read_only(
    client, db_session, monkeypatch
):
    balance, charge = sample()
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/account"):
            data = {"id": "acct_test"}
        elif request.url.path.endswith("/charges/ch_example"):
            data = charge
        elif request.url.params.get("starting_after"):
            data = {"data": [], "has_more": False}
        else:
            data = {"data": [balance], "has_more": True}
        return httpx.Response(200, json=data)

    provider(monkeypatch, handler)
    response = await client.post("/api/v1/integrations/stripe/sync", json={})
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 1
    assert response.json()["sources"] == ["stripe-sandbox:acct_test:USD"]
    response = await client.post("/api/v1/integrations/stripe/sync", json={})
    assert response.json()["accepted"] == 0
    assert response.json()["duplicate_skipped"] == 1
    rows = (await db_session.execute(select(BankStatement))).scalars().all()
    assert len(rows) == 1 and rows[0].amount == Decimal("3")
    assert any(r.url.params.get("starting_after") == "txn_example" for r in calls)
    assert (await client.get("/api/v1/integrations/stripe/status")).status_code == 200


@pytest.mark.asyncio
async def test_provider_failure_is_sanitized_and_atomic(
    client, db_session, monkeypatch
):
    provider(
        monkeypatch,
        lambda request: httpx.Response(401, json={"error": "private-provider-body"}),
    )
    response = await client.post("/api/v1/integrations/stripe/sync", json={})
    assert response.status_code == 503
    assert "private-provider-body" not in response.text
    assert not (await db_session.execute(select(BankStatement))).scalars().all()


@pytest.mark.asyncio
async def test_invalid_window_does_not_contact_stripe(client, monkeypatch):
    provider(
        monkeypatch,
        lambda request: pytest.fail("No provider request for invalid window"),
    )
    response = await client.post(
        "/api/v1/integrations/stripe/sync",
        json={"from_date": "2026-01-01", "to_date": "2026-03-01"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_repeat_page_rejected_before_any_insert(client, db_session, monkeypatch):
    balance, charge = sample()

    def handler(request):
        if request.url.path.endswith("/account"):
            data = {"id": "acct_test"}
        elif "/charges/" in request.url.path:
            data = charge
        else:
            data = {"data": [balance], "has_more": True}
        return httpx.Response(200, json=data)

    provider(monkeypatch, handler)
    response = await client.post("/api/v1/integrations/stripe/sync", json={})
    assert response.status_code == 503
    assert not (await db_session.execute(select(BankStatement))).scalars().all()
