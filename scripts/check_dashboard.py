"""Render the complete dashboard with synthetic contract data, including drift."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from streamlit.testing.v1 import AppTest

now = datetime.now(timezone.utc).isoformat()
snapshot = {
    "run_id": "synthetic-render-check",
    "run_date": now,
    "match_rate": 0.25,
    "matched_count": 5,
    "review_count": 0,
    "unmatched_count": 15,
    "avg_confidence": 1.0,
    "p10_confidence": 1.0,
    "run_duration_seconds": 0.01,
}
event = {
    "severity": "high",
    "metric_name": "match_rate",
    "z_score": -10.0,
    "created_at": now,
    "resolved_at": None,
    "resolution_notes": None,
    "event_type": "match_rate_drop",
    "current_value": 0.25,
    "baseline_mean": 0.9,
    "baseline_stddev": 0.065,
    "hypothesis": "Synthetic rendering fixture",
    "supporting_evidence": {"synthetic": True},
}
result = {
    "status": "unmatched",
    "confidence_score": 0.0,
    "match_reason": "No matching bank entry found",
    "amount_delta": None,
    "date_delta_days": None,
    "human_reviewed": False,
}


def response(url, **kwargs):
    if url.endswith("/integrations/stripe/status"):
        data = {"configured": False, "connected": False}
    elif url.endswith("/health"):
        data = {"status": "healthy", "version": "test", "environment": "test"}
    elif "/snapshots" in url:
        data = [snapshot]
    elif "/drift/summary/" in url:
        data = {
            "baseline_match_rate": 0.9,
            "open_drift_events": 1,
            "match_rate_trend": "degrading",
            "lookback_days": 30,
        }
    elif "/drift/events" in url:
        data = [event]
    elif "/reconciliation/results/" in url:
        data = [result]
    else:
        raise AssertionError("Unexpected dashboard request")
    return httpx.Response(200, json=data, request=httpx.Request("GET", url))


with patch("httpx.get", side_effect=response):
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "dashboard/app.py")
    ).run(timeout=30)
    assert not app.exception, [error.message for error in app.exception]
    assert len(app.dataframe) >= 2, "Run history and matching evidence must render"
    assert any("Drift Events" in heading.value for heading in app.subheader)
    assert len(app.json) >= 1, "Drift supporting evidence must render"
print("Complete dashboard rendering check passed, including tables and drift evidence.")
