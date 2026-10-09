"""Import independent TxCore settled payments; resolve identity using Stripe GETs only."""

import csv
import io
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    root = Path(__file__).resolve().parents[1]
    values = dict(
        line.split("=", 1)
        for line in (root / ".env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    secret = values.get("STRIPE_SECRET_KEY", "")
    if not secret.startswith("sk_test_"):
        raise ValueError("Stripe sandbox credential required")
    base = "http://localhost:" + values.get("API_PORT", "8200")

    def stripe(path):
        req = Request(
            "https://api.stripe.com/v1/" + path,
            headers={"Authorization": "Bearer " + secret},
        )
        with urlopen(req, timeout=30) as response:
            return json.load(response)

    def local(path, body, content_type="application/json"):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        req = Request(
            base + path,
            data=body,
            headers={
                "X-API-Key": values["VALID_API_KEYS"].split(",")[0],
                "Content-Type": content_type,
            },
        )
        with urlopen(req, timeout=60) as response:
            return json.load(response)

    account = stripe("account")["id"]
    groups = defaultdict(list)
    for row in json.loads(Path(sys.argv[1]).read_text()):
        session_id = row["provider_reference"]
        if (
            not session_id.startswith("cs_test_")
            or not session_id.isalnum()
            and any(
                c
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
                for c in session_id
            )
        ):
            raise ValueError("Invalid sandbox session")
        session = stripe("checkout/sessions/" + session_id)
        if (
            session.get("livemode") is not False
            or session.get("payment_status") != "paid"
            or session.get("client_reference_id") != row["reference"]
        ):
            raise ValueError("Checkout identity or sandbox status did not agree")
        payment = session.get("payment_intent")
        if not isinstance(payment, str) or not payment.startswith("pi_"):
            raise ValueError("Missing payment identity")
        source = f"stripe-sandbox:{account}:{row['currency']}"
        groups[source].append(
            {
                "external_id": "txcore:" + row["id"],
                "transaction_date": datetime.fromisoformat(
                    row["settled_at"].replace("Z", "+00:00")
                )
                .date()
                .isoformat(),
                "amount": row["amount"],
                "currency": row["currency"],
                "reference": payment,
                "description": row["description"],
            }
        )
    for source, rows in groups.items():
        content = io.StringIO()
        writer = csv.DictWriter(content, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        boundary = "txcore-sandbox-import"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="source"\r\n\r\n{source}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="txcore.csv"\r\nContent-Type: text/csv\r\n\r\n{content.getvalue()}\r\n--{boundary}--\r\n'
        ).encode()
        result = local(
            "/api/v1/ingest/transactions",
            body,
            "multipart/form-data; boundary=" + boundary,
        )
        print(json.dumps({"source": source, "ingestion": result}))
        if result.get("accepted", result.get("rows_accepted", 0)):
            print(
                json.dumps(local("/api/v1/reconciliation/run", {"source_name": source}))
            )


if __name__ == "__main__":
    main()
