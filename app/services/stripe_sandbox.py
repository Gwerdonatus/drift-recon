"""Read-only Stripe sandbox charge evidence. No payments or payouts are created."""

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import re
import uuid

import httpx
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import BankStatement, TransactionStatus
from app.schemas import BankStatementIngestionRow

SUPPORTED_CURRENCIES = {"USD": 100, "EUR": 100, "GBP": 100, "NGN": 100, "JPY": 1}
MAX_RECORDS = 1000


class StripeConnectionError(Exception):
    """Sanitized provider failures, without response bodies or credentials."""


def sandbox_key() -> str:
    key = get_settings().STRIPE_SECRET_KEY.get_secret_value()
    if not key.startswith("sk_test_"):
        raise StripeConnectionError("Stripe sandbox is not configured.")
    return key


async def stripe_get(client: httpx.AsyncClient, path: str, params=None) -> dict:
    try:
        response = await client.get(path, params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid payload")
        return data
    except (httpx.HTTPError, ValueError) as error:
        raise StripeConnectionError(
            "Stripe sandbox request failed. Check the credential and connection."
        ) from error


def stripe_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://api.stripe.com/v1/",
        auth=httpx.BasicAuth(sandbox_key(), ""),
        timeout=httpx.Timeout(15),
        follow_redirects=False,
    )


async def account_id(client: httpx.AsyncClient) -> str:
    account = await stripe_get(client, "account")
    identifier = account.get("id", "")
    if not isinstance(identifier, str) or not re.fullmatch(
        r"acct_[A-Za-z0-9]+", identifier
    ):
        raise StripeConnectionError("Stripe returned an invalid account identifier.")
    return identifier


async def connection_status() -> dict:
    configured = bool(get_settings().STRIPE_SECRET_KEY.get_secret_value())
    if not configured:
        return {"configured": False, "connected": False, "mode": "sandbox"}
    async with stripe_client() as client:
        account = await account_id(client)
    return {
        "configured": True,
        "connected": True,
        "mode": "sandbox",
        "account_id": account,
        "read_only": True,
    }


def charge_row(balance: dict, charge: dict, account: str) -> dict | None:
    # Gross charges only: fees/net are evidence, never silently substituted for sales.
    currency = str(balance.get("currency", "")).upper()
    if balance.get("type") != "charge" or currency not in SUPPORTED_CURRENCIES:
        return None
    if charge.get("livemode") is not False:
        raise StripeConnectionError("Live or unverified Stripe charge rejected.")
    if (
        not charge.get("paid")
        or not charge.get("captured")
        or charge.get("currency", "").upper() != currency
    ):
        return None
    gross, fee, net = (balance.get(field) for field in ("amount", "fee", "net"))
    if (
        any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in (gross, fee, net)
        )
        or gross <= 0
        or gross - fee != net
    ):
        raise StripeConnectionError("Stripe returned inconsistent monetary evidence.")
    if charge.get("amount") != gross:
        return None  # Foreign exchange / differing presentment amount requires separate handling.
    identifier, charge_id = balance.get("id", ""), charge.get("id", "")
    if (
        not isinstance(identifier, str)
        or not isinstance(charge_id, str)
        or not re.fullmatch(r"txn_[A-Za-z0-9]+", identifier)
        or not re.fullmatch(r"ch_[A-Za-z0-9]+", charge_id)
    ):
        raise StripeConnectionError("Stripe returned an invalid record identifier.")
    reference = charge.get("payment_intent") or charge_id
    if not isinstance(reference, str):
        raise StripeConnectionError("Stripe returned an invalid payment reference.")
    try:
        value_date = datetime.fromtimestamp(charge["created"], timezone.utc).date()
        amount = Decimal(gross) / SUPPORTED_CURRENCIES[currency]
        row = BankStatementIngestionRow(
            external_id=identifier,
            value_date=value_date,
            amount=amount,
            currency=currency,
            reference=reference,
            description="Stripe sandbox gross charge",
        )
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        raise StripeConnectionError("Stripe returned invalid charge data.") from error
    return {
        "id": uuid.uuid4(),
        **row.model_dump(),
        "bank_name": f"stripe-sandbox:{account}:{currency}",
        "status": TransactionStatus.PENDING,
        "ingestion_batch_id": "stripe-sandbox-v1",
        "raw_data": {
            "provider": "stripe",
            "sandbox": True,
            "account_id": account,
            "charge_id": charge_id,
            "payment_intent": reference,
            "gross_minor": gross,
            "fee_minor": fee,
            "net_minor": net,
            "balance_status": balance.get("status"),
            "available_on": balance.get("available_on"),
        },
    }


async def sync_charges(db: AsyncSession, from_date: date, to_date: date) -> dict:
    if from_date > to_date or (to_date - from_date).days > 31:
        raise ValueError("Choose an ordered window of at most 32 inclusive days.")
    params = {
        "limit": 100,
        "type": "charge",
        "created[gte]": int(
            datetime.combine(from_date, time.min, timezone.utc).timestamp()
        ),
        "created[lt]": int(
            datetime.combine(
                to_date + timedelta(days=1), time.min, timezone.utc
            ).timestamp()
        ),
    }
    rows, fetched, skipped = [], 0, 0
    seen = set()
    async with stripe_client() as client:
        account = await account_id(client)
        for _ in range(MAX_RECORDS // 100):
            page = await stripe_get(client, "balance_transactions", params)
            entries = page.get("data")
            if not isinstance(entries, list):
                raise StripeConnectionError(
                    "Stripe returned an invalid transaction page."
                )
            for balance in entries:
                if not isinstance(balance, dict):
                    raise StripeConnectionError(
                        "Stripe returned an invalid balance record."
                    )
                fetched += 1
                if fetched > MAX_RECORDS or balance.get("id") in seen:
                    raise StripeConnectionError(
                        "Stripe pagination exceeded its safe limit; use a smaller window."
                    )
                seen.add(balance.get("id"))
                source = balance.get("source", "")
                if not isinstance(source, str) or not re.fullmatch(
                    r"ch_[A-Za-z0-9]+", source
                ):
                    skipped += 1
                    continue
                charge = await stripe_get(client, f"charges/{source}")
                row = charge_row(balance, charge, account)
                if row is None:
                    skipped += 1
                else:
                    rows.append(row)
            if not page.get("has_more"):
                break
            if not entries or fetched >= MAX_RECORDS:
                raise StripeConnectionError(
                    "Stripe import limit reached; use a smaller window."
                )
            params["starting_after"] = entries[-1]["id"]
        else:
            raise StripeConnectionError(
                "Stripe import limit reached; use a smaller window."
            )
    # Fetch and validate all provider evidence before mutating local persistence.
    accepted = 0
    if rows:
        result = await db.execute(
            insert(BankStatement)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_bank_external_bank")
        )
        accepted = result.rowcount
    totals = Counter()
    for row in rows:
        totals[row["currency"]] += row["amount"]
    return {
        "mode": "sandbox",
        "read_only": True,
        "account_id": account,
        "fetched": fetched,
        "accepted": accepted,
        "duplicate_skipped": len(rows) - accepted,
        "unsupported_skipped": skipped,
        "sources": sorted({row["bank_name"] for row in rows}),
        "gross_observed": {
            currency: str(amount) for currency, amount in totals.items()
        },
        "amount_basis": "gross charge; fees and net retained as evidence",
        "from_date": from_date,
        "to_date": to_date,
    }
