# Engineering decisions

## Matching rather than simple equality

The service generates eligible transaction/statement candidates, applies weighted scores, sorts by confidence and assigns pairs greedily. Currency is a hard boundary; equal numeric values in USD and EUR are not interchangeable. Same-sign amounts and a configured date window further restrict candidates. The score is explainable, but the greedy solution is not necessarily globally optimal and is not a probability of correctness.

Threshold overrides belong to one run. The orchestrator copies cached settings so a caller's threshold cannot change a later request. Effective review thresholds cannot exceed automatic-match thresholds. PostgreSQL transaction-level advisory locks serialize runs for the same source; different sources can proceed independently. This is PostgreSQL-specific, deliberate coordination.

Both sides currently share one reconciliation namespace: transaction `source` and statement `bank_name` must match. The source label is not a tenant or ownership boundary.

## Persistence and drift

Runs persist original results and an aggregate snapshot. A flush makes the latest snapshot visible to automatic drift analysis before commit. The matched-amount aggregate sums actual matched transaction amounts, rather than amount differences.

The latest snapshot is compared with earlier snapshots inside the configured window. The summary baseline also excludes the current snapshot. The population standard deviation defines a z-score; a zero-variance baseline produces no statistical signal. The default minimum is seven total snapshots, including current, rather than seven prior snapshots.

Analysis locks the current snapshot and skips metrics already recorded for it. Repeated requests preserve existing signals, including resolved ones, instead of duplicating them. Hypotheses are rule-based suggestions with evidence, not proven root causes. Drift failure is logged and isolated in a savepoint so a failed optional analysis cannot poison a successful reconciliation transaction.

## Operations and scheduling

APScheduler runs inside the API lifespan. Local runtime uses one Uvicorn process to avoid multiple scheduler owners using one job store. Scheduled reconciliation discovers sources with pending records; scheduled drift discovers sources with historical snapshots. There is no Redis task queue or Celery worker here. Redis is provisioned, but a Redis rate-limiting middleware is not implemented.

Health returns an unsuccessful HTTP status when the database is unavailable. CSV reads are bounded before decoding, while schema precision limits prevent oversized decimal values from reaching fixed-precision database columns. Quarantine keeps invalid-row evidence; valid records deduplicate by external ID and source. Repeated invalid submissions can add more quarantine evidence.

## Dashboard interpretation

Review, matched and unmatched counts come from distinct stored fields. Confidence percentiles and review counts are exposed by the snapshot API. The chart lookback filters dates, rather than treating a number of runs as a number of days. Drift summary uses the server's configured analysis window, separately labelled.

The evidence table displays stored decisions and score details. It is read-only. Human review and drift resolution require API calls; review labels are caller-supplied and do not establish authenticated personal identity. A review verdict remains alongside the engine result and does not train a model.

The dashboard pins compatible NumPy and PyArrow versions. Docker builds and CI import the dataframe dependencies before startup, because Streamlit health alone cannot verify that table rendering works.

## Honest deployment boundary

Local ports bind to loopback, and private credentials remain in ignored configuration. The dashboard has no login; exposing it publicly would expose financial information. TLS templates, VPS scripts and backup scripts are deployment references, not proof of tested production operations. Dependency security review, access roles, tenant boundaries, fair large-backlog scheduling, optimal matching and tested restore procedures remain separate work.
