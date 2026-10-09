"""
Reconciliation Monitoring Dashboard.

Connects to the API service to display:
- Live match rate trend (30-day rolling)
- Confidence score distribution
- Open drift events with hypotheses
- Recent reconciliation run summary
- Unmatched transactions table

Design: read-only, API-key authenticated. The API key is passed
via environment variable — never hardcoded or shown in UI.
"""

from __future__ import annotations

import os
from datetime import datetime

import httpx
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ── Config ─────────────────────────────────────────────────────────────────────
API_BASE = os.environ.get("API_BASE_URL", "http://api:8000")
API_KEY = os.environ.get("API_KEY", "")
HEADERS = {"X-API-Key": API_KEY}

st.set_page_config(
    page_title="Reconciliation Monitor",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ── API Client ─────────────────────────────────────────────────────────────────


@st.cache_data(ttl=60)
def fetch_snapshots(source_name: str, limit: int = 30) -> pd.DataFrame:
    try:
        r = httpx.get(
            f"{API_BASE}/api/v1/reconciliation/snapshots",
            params={"source_name": source_name, "limit": limit},
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df["run_date"] = pd.to_datetime(df["run_date"])
        df = df.sort_values("run_date")
        return df
    except Exception as e:
        st.error(f"Failed to fetch snapshots: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=60)
def fetch_drift_events(source_name: str) -> pd.DataFrame:
    try:
        r = httpx.get(
            f"{API_BASE}/api/v1/drift/events",
            params={"source_name": source_name, "limit": 100},
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df["created_at"] = pd.to_datetime(df["created_at"])
        return df
    except Exception as e:
        st.error(f"Failed to fetch drift events: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=60)
def fetch_drift_summary(source_name: str) -> dict | None:
    try:
        r = httpx.get(
            f"{API_BASE}/api/v1/drift/summary/{source_name}",
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


@st.cache_data(ttl=60)
def fetch_results(run_id: str) -> list[dict]:
    try:
        response = httpx.get(
            f"{API_BASE}/api/v1/reconciliation/results/{run_id}",
            headers=HEADERS,
            params={"limit": 1000},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except Exception:
        st.error("Could not load the selected run's reconciliation evidence.")
        return []


@st.cache_data(ttl=30)
def fetch_health() -> dict:
    try:
        r = httpx.get(f"{API_BASE}/health", timeout=5)
        return r.json()
    except Exception as e:
        return {"status": "unreachable", "error": str(e)}


# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚙️ Controls")

    source_name = st.text_input(
        "Source Name",
        value="default",
        help="The source_name used during reconciliation runs",
    )

    lookback = st.slider("Chart lookback (days)", min_value=7, max_value=90, value=30)

    if st.button("🔄 Refresh Data"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.caption("API Status")
    health = fetch_health()
    status_color = "🟢" if health.get("status") == "healthy" else "🔴"
    st.caption(f"{status_color} {health.get('status', 'unknown').upper()}")
    if "version" in health:
        st.caption(f"v{health['version']} · {health.get('environment', '')}")

    st.divider()
    st.caption(f"Last refresh: {datetime.now().strftime('%H:%M:%S')}")


# ── Main Content ───────────────────────────────────────────────────────────────

st.title("Reconciliation Monitor")
st.caption(
    "Local engineering demo · synthetic records and simulated bank statements. No bank connection."
)

snapshots_df = fetch_snapshots(source_name, limit=365)
if not snapshots_df.empty:
    snapshots_df = snapshots_df[
        snapshots_df["run_date"]
        >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=lookback)
    ]
drift_summary = fetch_drift_summary(source_name)
drift_df = fetch_drift_events(source_name)


# ── KPI Row ────────────────────────────────────────────────────────────────────

col1, col2, col3, col4, col5 = st.columns(5)

if not snapshots_df.empty:
    latest = snapshots_df.iloc[-1]
    prev = snapshots_df.iloc[-2] if len(snapshots_df) > 1 else None

    match_rate = latest.get("match_rate", 0)
    prev_rate = prev["match_rate"] if prev is not None else None
    delta_rate = f"{(match_rate - prev_rate):.1%}" if prev_rate is not None else None

    with col1:
        st.metric(
            "Match Rate",
            f"{match_rate:.1%}",
            delta=delta_rate,
            delta_color="normal",
        )

    with col2:
        st.metric("Matched", int(latest.get("matched_count", 0)))

    with col3:
        st.metric(
            "Unmatched",
            int(latest.get("unmatched_count", 0)),
            delta_color="inverse",
        )

    with col4:
        avg_conf = latest.get("avg_confidence")
        st.metric("Avg Confidence", f"{avg_conf:.1%}" if avg_conf else "—")

    with col5:
        open_drift = drift_summary.get("open_drift_events", 0) if drift_summary else 0
        color = "🔴" if open_drift > 0 else "🟢"
        st.metric("Open Drift Events", f"{color} {open_drift}")
else:
    st.info(
        "No reconciliation data found for this source. Run a reconciliation to see metrics."
    )
    st.stop()


st.divider()

# ── Charts Row ─────────────────────────────────────────────────────────────────

left_col, right_col = st.columns(2)

with left_col:
    st.subheader("📈 Match Rate Trend")
    if not snapshots_df.empty:
        fig = go.Figure()

        fig.add_trace(
            go.Scatter(
                x=snapshots_df["run_date"],
                y=snapshots_df["match_rate"],
                mode="lines+markers",
                name="Match Rate",
                line=dict(color="#2563eb", width=2),
                marker=dict(size=6),
            )
        )

        # Baseline band
        if drift_summary and drift_summary.get("baseline_match_rate"):
            baseline = drift_summary["baseline_match_rate"]
            fig.add_hline(
                y=baseline,
                line_dash="dot",
                line_color="gray",
                annotation_text=f"Baseline {baseline:.1%}",
            )

        fig.update_layout(
            yaxis_tickformat=".0%",
            yaxis_range=[0, 1.05],
            height=300,
            margin=dict(l=0, r=0, t=20, b=0),
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)

with right_col:
    st.subheader("🎯 Match Distribution")
    if not snapshots_df.empty:
        latest = snapshots_df.iloc[-1]
        matched = int(latest.get("matched_count", 0))
        review = int(latest.get("review_count", 0))
        unmatched = int(latest.get("unmatched_count", 0))

        fig = px.pie(
            values=[matched, max(review, 0), max(unmatched, 0)],
            names=["Matched", "Review", "Unmatched"],
            color=["Matched", "Review", "Unmatched"],
            color_discrete_map={
                "Matched": "#16a34a",
                "Review": "#d97706",
                "Unmatched": "#dc2626",
            },
            hole=0.4,
        )
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=20, b=0))
        st.plotly_chart(fig, use_container_width=True)


# ── Run History Table ───────────────────────────────────────────────────────────

st.subheader("📋 Recent Runs")
if not snapshots_df.empty:
    display_cols = [
        "run_date",
        "run_id",
        "match_rate",
        "matched_count",
        "unmatched_count",
        "avg_confidence",
        "run_duration_seconds",
    ]
    display_df = snapshots_df[
        [c for c in display_cols if c in snapshots_df.columns]
    ].copy()

    display_df = display_df.sort_values("run_date", ascending=False)
    display_df["run_date"] = display_df["run_date"].dt.strftime("%Y-%m-%d %H:%M")
    if "match_rate" in display_df:
        display_df["match_rate"] = display_df["match_rate"].apply(lambda x: f"{x:.1%}")
    if "avg_confidence" in display_df:
        display_df["avg_confidence"] = display_df["avg_confidence"].apply(
            lambda x: f"{x:.1%}" if x else "—"
        )

    st.dataframe(display_df, use_container_width=True, hide_index=True)


# Inspect actual engine evidence for a selected run.
st.subheader("Matching evidence")
selected_run = st.selectbox(
    "Reconciliation run", snapshots_df["run_id"].iloc[::-1].tolist()
)
results = fetch_results(selected_run)
if results:
    evidence = pd.DataFrame(results)
    st.dataframe(
        evidence[
            [
                "status",
                "confidence_score",
                "match_reason",
                "amount_delta",
                "date_delta_days",
                "human_reviewed",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )
    with st.expander("Score breakdown and record identifiers"):
        st.json(results[:5])
        st.caption(
            "First five records. API results support pagination; the table loads up to 1,000."
        )
else:
    st.info("No result rows for this run.")


# ── Drift Events ───────────────────────────────────────────────────────────────

st.divider()
st.subheader("⚠️ Drift Events")

if drift_summary:
    trend = drift_summary.get("match_rate_trend", "unknown")
    trend_icon = {
        "stable": "🟢 Stable",
        "degrading": "🔴 Degrading",
        "improving": "🟢 Improving",
        "insufficient_data": "⚪ Insufficient Data",
    }.get(trend, trend)
    st.caption(
        f"{drift_summary.get('lookback_days', 30)}-day analysis window: **{trend_icon}**"
    )

if not drift_df.empty:
    open_events = drift_df[drift_df["resolved_at"].isna()]

    if not open_events.empty:
        for _, event in open_events.iterrows():
            severity = event.get("severity", "low")
            icon = {"high": "🔴", "medium": "🟡", "low": "🔵"}.get(severity, "⚪")
            with st.expander(
                f"{icon} [{severity.upper()}] {event['metric_name']} — "
                f"z={float(event['z_score']):.2f}σ  "
                f"({pd.to_datetime(event['created_at']).strftime('%Y-%m-%d %H:%M')})"
            ):
                st.write(f"**Type:** {event['event_type']}")
                st.write(f"**Current Value:** {float(event['current_value']):.4f}")
                st.write(
                    f"**Baseline Mean:** {float(event['baseline_mean']):.4f} "
                    f"± {float(event['baseline_stddev']):.4f}"
                )

                if event.get("hypothesis"):
                    st.info(f"💡 **Hypothesis:** {event['hypothesis']}")

                if event.get("supporting_evidence"):
                    with st.expander("📎 Supporting Evidence"):
                        st.json(event["supporting_evidence"])
    else:
        st.success("✅ No open drift events.")

    with st.expander("Resolved Events"):
        resolved = drift_df[~drift_df["resolved_at"].isna()]
        if not resolved.empty:
            st.dataframe(
                resolved[
                    [
                        "created_at",
                        "event_type",
                        "severity",
                        "metric_name",
                        "z_score",
                        "resolution_notes",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("No resolved events yet.")
else:
    st.info(
        "No drift events recorded yet. Drift detection activates after 7+ reconciliation runs."
    )


# ── Confidence Distribution ────────────────────────────────────────────────────

if not snapshots_df.empty and "avg_confidence" in snapshots_df.columns:
    st.divider()
    st.subheader("📉 Confidence Score Over Time")

    fig = go.Figure()
    if "p10_confidence" in snapshots_df.columns:
        fig.add_trace(
            go.Scatter(
                x=snapshots_df["run_date"],
                y=snapshots_df["p10_confidence"],
                fill=None,
                mode="lines",
                line_color="rgba(37,99,235,0.2)",
                name="P10",
            )
        )
    fig.add_trace(
        go.Scatter(
            x=snapshots_df["run_date"],
            y=snapshots_df["avg_confidence"],
            fill="tonexty" if "p10_confidence" in snapshots_df.columns else None,
            mode="lines+markers",
            line=dict(color="#2563eb"),
            name="Avg Confidence",
        )
    )

    fig.update_layout(
        yaxis_tickformat=".0%",
        yaxis_range=[0, 1.05],
        height=250,
        margin=dict(l=0, r=0, t=10, b=0),
    )
    st.plotly_chart(fig, use_container_width=True)
