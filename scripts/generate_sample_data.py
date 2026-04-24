#!/usr/bin/env python3
"""
Generate realistic sample CSV data for development and demos.

Creates two files:
  - transactions.csv    (internal system records)
  - bank_statements.csv (bank side, with intentional variations)

Variations introduced to make matching interesting:
  - 5% of bank entries have 1-day date shift (posting delay)
  - 3% of entries have minor amount differences (rounding/fees)
  - 5% of entries have truncated/reformatted references
  - 7% of transactions have NO matching bank entry (unmatched)
  - 3% of bank entries have NO matching transaction (bank-only)

Usage:
    python scripts/generate_sample_data.py --rows 500 --output data/
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import uuid
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

random.seed(42)  # Reproducible data


COUNTERPARTIES = [
    "Amazon Web Services", "Stripe Inc", "GitHub Inc",
    "Cloudflare Inc", "Twilio Inc", "Datadog Inc",
    "Atlassian Corp", "Zoom Video", "Slack Technologies",
    "SendGrid Inc", "Heroku Inc", "New Relic Inc",
]

DESCRIPTIONS = [
    "Monthly subscription", "Annual license", "Usage charges",
    "Professional services", "Support contract", "API calls",
    "Infrastructure costs", "Platform fee", "SaaS subscription",
]


def rand_amount(min_val: float = 50.0, max_val: float = 10000.0) -> Decimal:
    """Generate a realistic transaction amount."""
    raw = random.uniform(min_val, max_val)
    return Decimal(str(raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def rand_date(start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, delta))


def generate_reference(prefix: str = "REF") -> str:
    return f"{prefix}-{random.randint(10000, 99999)}"


def introduce_bank_variation(
    txn: dict,
    date_shift_prob: float = 0.05,
    amount_variation_prob: float = 0.03,
    ref_variation_prob: float = 0.05,
) -> dict:
    """
    Apply realistic bank-side variations to a transaction.
    Banks reformat references, have posting delays, and sometimes
    charge fees that alter amounts slightly.
    """
    bank = txn.copy()

    # Date shift (bank posting delay)
    if random.random() < date_shift_prob:
        original_date = date.fromisoformat(bank["value_date"])
        bank["value_date"] = (original_date + timedelta(days=1)).isoformat()

    # Amount variation (bank fees, FX rounding)
    if random.random() < amount_variation_prob:
        original = Decimal(bank["amount"])
        variation = Decimal(str(random.uniform(-0.50, 0.50))).quantize(Decimal("0.01"))
        bank["amount"] = str((original + variation).quantize(Decimal("0.01")))

    # Reference reformatting (banks often add prefixes or truncate)
    if random.random() < ref_variation_prob and bank.get("reference"):
        variations = [
            lambda r: r.replace("-", ""),           # REF001234
            lambda r: f"WIRE/{r}",                  # WIRE/REF-01234
            lambda r: r[:8].upper(),                # Truncated
            lambda r: f"{r}/GBP",                   # Suffix added
        ]
        bank["reference"] = random.choice(variations)(bank["reference"])

    return bank


def generate_data(n_rows: int, output_dir: str) -> None:
    os.makedirs(output_dir, exist_ok=True)

    start_date = date(2024, 1, 1)
    end_date = date(2024, 3, 31)

    transactions = []
    bank_entries = []

    # Unmatched transaction IDs (no bank counterpart)
    unmatched_txn_count = max(1, int(n_rows * 0.07))
    unmatched_txn_indices = set(random.sample(range(n_rows), unmatched_txn_count))

    for i in range(n_rows):
        txn_id = f"TXN-{i:06d}"
        counterparty = random.choice(COUNTERPARTIES)
        description = f"{random.choice(DESCRIPTIONS)} - {counterparty}"
        amount = rand_amount()
        txn_date = rand_date(start_date, end_date)
        reference = generate_reference()

        txn = {
            "external_id": txn_id,
            "transaction_date": txn_date.isoformat(),
            "amount": str(amount),
            "currency": "USD",
            "reference": reference,
            "description": description,
            "counterparty": counterparty,
        }
        transactions.append(txn)

        # Create matching bank entry (unless this is an "unmatched" transaction)
        if i not in unmatched_txn_indices:
            bank = {
                "external_id": f"BANK-{i:06d}",
                "value_date": txn_date.isoformat(),
                "amount": str(amount),
                "currency": "USD",
                "reference": reference,
                "description": description,
                "counterparty": counterparty,
            }
            bank = introduce_bank_variation(bank)
            bank_entries.append(bank)

    # Add bank-only entries (bank received money we never recorded)
    n_bank_only = max(1, int(n_rows * 0.03))
    for i in range(n_bank_only):
        bank_only_date = rand_date(start_date, end_date)
        bank_entries.append({
            "external_id": f"BANK-ONLY-{i:06d}",
            "value_date": bank_only_date.isoformat(),
            "amount": str(rand_amount(200, 5000)),
            "currency": "USD",
            "reference": generate_reference("UNKN"),
            "description": "Unknown credit",
            "counterparty": "Unknown Sender",
        })

    # Shuffle bank entries to make ordering non-deterministic
    random.shuffle(bank_entries)

    # Write CSVs
    txn_path = Path(output_dir) / "transactions.csv"
    bank_path = Path(output_dir) / "bank_statements.csv"

    txn_fields = ["external_id", "transaction_date", "amount", "currency",
                  "reference", "description", "counterparty"]
    bank_fields = ["external_id", "value_date", "amount", "currency",
                   "reference", "description", "counterparty"]

    with open(txn_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=txn_fields)
        w.writeheader()
        w.writerows(transactions)

    with open(bank_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=bank_fields)
        w.writeheader()
        w.writerows(bank_entries)

    print(f"""
Sample data generated:
  Transactions:   {txn_path} ({len(transactions)} rows)
  Bank entries:   {bank_path} ({len(bank_entries)} rows)

Expected results:
  Matched:       ~{n_rows - unmatched_txn_count - n_bank_only} ({(n_rows - unmatched_txn_count) / n_rows:.0%})
  Unmatched txn: ~{unmatched_txn_count}
  Bank-only:     ~{n_bank_only}

Upload commands (with API running):
  curl -X POST http://localhost:8000/api/v1/ingest/transactions \\
    -H "X-API-Key: your_api_key" \\
    -F "file=@{txn_path}" \\
    -F "source=demo"

  curl -X POST http://localhost:8000/api/v1/ingest/bank-statements \\
    -H "X-API-Key: your_api_key" \\
    -F "file=@{bank_path}" \\
    -F "bank_name=demo"
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate sample reconciliation data")
    parser.add_argument("--rows", type=int, default=500, help="Number of transactions")
    parser.add_argument("--output", default="data", help="Output directory")
    args = parser.parse_args()
    generate_data(args.rows, args.output)
