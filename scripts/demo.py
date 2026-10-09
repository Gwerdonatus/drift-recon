"""Assert an actual local CSV ingestion, reconciliation and drift demo. No bank connection."""

import csv
import io
import json
from datetime import date
from pathlib import Path
import urllib.request

root = Path(__file__).resolve().parents[1]
values = dict(
    line.split("=", 1)
    for line in (root / ".env").read_text().splitlines()
    if "=" in line
)
base = "http://localhost:" + values.get("API_PORT", "8200")
key = values["VALID_API_KEYS"].split(",")[0]
source = "demo-recruiter"


def request(path, body=None, content_type="application/json", method=None):
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    req = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={"X-API-Key": key, "Content-Type": content_type},
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def upload(kind, rows):
    content = io.StringIO()
    fieldnames = [
        "external_id",
        "transaction_date" if kind == "transactions" else "value_date",
        "amount",
        "currency",
        "reference",
        "description",
    ]
    writer = csv.DictWriter(content, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    boundary = "drift-recon-demo-csv"
    field = "source" if kind == "transactions" else "bank_name"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{source}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="synthetic-{kind}.csv"\r\n'
        f"Content-Type: text/csv\r\n\r\n{content.getvalue()}\r\n--{boundary}--\r\n"
    ).encode()
    return request(
        f"/api/v1/ingest/{kind}", body, f"multipart/form-data; boundary={boundary}"
    )


snapshots = request(f"/api/v1/reconciliation/snapshots?source_name={source}&limit=100")
if len(snapshots) >= 8:
    print("Existing eight-run demo preserved; no duplicate history created.")
else:
    assert not snapshots, "Partial demo history exists; inspect before resuming."
    runs = []
    for batch, matches in enumerate([18, 19, 18, 19, 17, 18, 19, 5]):
        day = date.today().isoformat()
        transactions = [
            {
                "external_id": f"DEMO-V1-T-{batch}-{i}",
                "transaction_date": day,
                "amount": str(1000 + i * 1000 + batch * 25000),
                "currency": "USD",
                "reference": f"DEMO-{batch}-{i}",
                "description": f"Synthetic order {batch}-{i}",
            }
            for i in range(20)
        ]
        bank = [
            {
                "external_id": f"DEMO-V1-B-{batch}-{i}",
                "value_date": day,
                "amount": transaction["amount"],
                "currency": "USD",
                "reference": transaction["reference"],
                "description": transaction["description"],
            }
            for i, transaction in enumerate(transactions[:matches])
        ]
        accepted = upload("transactions", transactions)
        assert accepted["accepted"] == 20
        replay = upload("transactions", transactions)
        assert replay["accepted"] == 0 and replay["duplicate_skipped"] == 20
        assert upload("bank-statements", bank)["accepted"] == matches
        result = request("/api/v1/reconciliation/run", {"source_name": source})
        assert result["matched"] == matches, result
        assert result["unmatched"] == 20 - matches
        runs.append(result)
    invalid = [
        {
            "external_id": "DEMO-INVALID",
            "transaction_date": day,
            "amount": "not-a-number",
            "currency": "USD",
            "reference": "INVALID",
            "description": "Deliberately invalid demo row",
        }
    ]
    assert upload("transactions", invalid)["quarantined"] == 1
    print(
        json.dumps({"synthetic": True, "bank_connected": False, "runs": runs}, indent=2)
    )
summary = request(f"/api/v1/drift/summary/{source}")
events = request(f"/api/v1/drift/events?source_name={source}")
assert summary["total_snapshots"] == 8
assert events and any(
    event["metric_name"] == "match_rate" and event["severity"] == "high"
    for event in events
)
assert request(f"/api/v1/drift/analyze/{source}", {})["events_detected"] == 0
print(
    json.dumps(
        {
            "synthetic": True,
            "source": source,
            "summary": summary,
            "events": len(events),
        },
        indent=2,
    )
)

snapshots = request(f"/api/v1/reconciliation/snapshots?source_name={source}&limit=100")
latest = max(snapshots, key=lambda snapshot: snapshot["run_date"])
results = request(f"/api/v1/reconciliation/results/{latest['run_id']}")
unmatched = sorted(
    (result for result in results if result["status"] == "unmatched"),
    key=lambda result: result["id"],
)
review = next(
    (result for result in unmatched if result["human_verdict"] == "escalated"),
    unmatched[0],
)
request(
    f"/api/v1/reconciliation/results/{review['id']}/review?verdict=escalated&reviewed_by=demo-analyst",
    {},
    method="PATCH",
)
results = request(f"/api/v1/reconciliation/results/{latest['run_id']}")
assert any(result["human_verdict"] == "escalated" for result in results)
print("Analyst escalation persisted alongside the original unmatched decision.")
