# Stripe sandbox integration

Set `STRIPE_SECRET_KEY=sk_test_...` in the ignored local `.env`, then recreate the API container. Never commit or put the secret in the dashboard or a URL. The dashboard API key stays server-side as well.

Authenticated endpoints:
- `GET /api/v1/integrations/stripe/status`: checks the provider account connection.
- `POST /api/v1/integrations/stripe/sync`: imports captured gross charge evidence. Optional JSON `from_date` / `to_date` dates select up to 32 inclusive days. Default is today and the preceding 30 days.

Select the returned `stripe-sandbox:<account>:<currency>` source to view its reconciliation separately from synthetic data. Sync only imports provider evidence; internal records must be imported independently before running reconciliation.

## Import TxCore internal records

Export settled Stripe transactions from TxCore's Django ORM as a JSON array with `id`, `reference`, `amount`, `currency`, `description`, `provider_reference`, `created_at` and `settled_at`. Keep this export local and outside Git. `provider_reference` must identify its sandbox Checkout session.

Run `python3 scripts/import_txcore_sandbox.py /path/to/export.json` from the Drift Recon repository. It resolves Checkout payment identities using Stripe GET requests, verifies the sandbox session and original client reference, and imports amounts and dates from the internal export. Newly accepted rows trigger reconciliation. Repeating an unchanged import skips duplicates and preserves snapshot history.

This script is an explicit local bridge, not an automatic TxCore feed or webhook subscriber. The source label is a reconciliation namespace, not a tenant security boundary.

## Scope

The Stripe connector creates no payments or payouts. It accepts test credentials only. Gross charges are compared with internal payment totals; Stripe fee/net amounts and pending availability are retained in provider evidence, not treated as bank settlement. Refunds, disputes, payouts and FX conversion require additional accounting flows and are not reconciled here. Collection is bounded to 1,000 records per request and validates all fetched pages before inserting.
