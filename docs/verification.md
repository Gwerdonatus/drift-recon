# Verified local walkthrough

Verified on an Apple Silicon Mac on 9 October 2026, using PostgreSQL 16 and the actual HTTP API.

## Scenario

Select **demo-recruiter** in the dashboard at http://localhost:8082 or http://localhost:8502.

1. Overview: latest run matched 5 of 20 records (25%); 15 remain unmatched. The seven-run baseline is 91.43%.
2. Matching evidence: select a run and inspect status, confidence and factor evidence. Exact pairs score 1.00. The latest run includes an unmatched result escalated by the synthetic analyst label `demo-analyst`.
3. Drift: expand the high-severity match-rate and unmatched-volume signals. The recorded evidence compares the latest run with earlier snapshots; hypotheses guide investigation and do not establish a cause.
4. API contract: http://localhost:8200/docs describes ingestion, runs, review and drift endpoints. Business requests require an API key.

All financial rows are synthetic and statements are simulated. Eight batches are created close together; these are not thirty days of production observations.

## Measured checks

- 87 backend tests passed against an isolated PostgreSQL database; 77.01% coverage, above the configured 70% gate.
- Black formatting and Ruff lint passed for `app/` and `tests/`.
- Compose configuration validation passed; all five services became healthy: PostgreSQL, Redis, API, dashboard and Nginx.
- Startup migrations completed. PostgreSQL contains both persisted scheduler jobs.
- The HTTP demo created 160 transactions, 133 statement rows, eight runs, two high-severity drift events and one quarantined invalid row.
- Valid CSV retries were rejected as duplicates. A second full demo invocation preserved eight runs and two drift events.
- Repeating analysis created zero new drift signals.
- The review API persisted an escalation while retaining the original engine decision.
- A business endpoint without a key returned HTTP 401; proxied health returned HTTP 200 with a healthy database.
- Browser validation exposed a NumPy/PyArrow incompatibility and unsupported nested expanders. Compatible pins, a flat evidence layout and a complete Streamlit AppTest rendering check address both. The rendering check is included in CI.

The test suite includes mocks. The separate live HTTP scenario verifies actual persistence and the browser screenshots document actual rendering. No load benchmark or production deployment is claimed. Mypy remains advisory in CI. The pull request is intentionally unmerged; reviewers should use its branch to see the walkthrough changes.

## Stripe sandbox verification

- Actual provider GET requests returned three captured USD charges totaling $33.
- First sync accepted 3 rows; second accepted 0 and skipped 3 duplicates.
- TxCore independently supplied internal amounts and settlement dates; Stripe Checkout supplied the corresponding payment identities.
- Reconciliation matched all 3 records, with no review or unmatched records.
- Live credentials and records, inconsistent fee/net values, repeated pagination and unsupported FX pairs are rejected or skipped in regression tests.
- No payments, payouts or live bank actions were performed.
