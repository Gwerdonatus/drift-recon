# Local development runbook

Generate private configuration with `python3 scripts/configure_local.py`, then start with `docker compose -p drift-recon up -d --build --wait --wait-timeout 300`. Migrations run before the API starts. Preserve `.env` and all named volumes when restarting.

Use `docker compose -p drift-recon ps` to inspect all five services: postgres, redis, api, dashboard and nginx. Wait for startup health checks before diagnosing failure. Inspect only the affected service logs; do not publish configuration or database credentials.

`docker compose -p drift-recon config --quiet` validates configuration without printing resolved secrets. The plain configuration command can expose credentials and should not be used in shared transcripts.

Run `python3 scripts/demo.py` to create the staged eight-run scenario. Repeat it after completion to verify that history is preserved. If it reports partial history, inspect snapshots and imported records before resuming; do not reset the database to hide failures.

Dashboard: http://localhost:8502; proxy: http://localhost:8082; API contract: http://localhost:8200/docs. Select `demo-recruiter`. The local dashboard is read-only and has no login. Keep ports bound to loopback.

A failed CSV row is quarantined with a reason. Review decisions and drift resolutions are available through API endpoints and require the configured key. Never show that key in screenshots or source. No external alert destination is configured for the demo.

The runtime uses one API worker because APScheduler runs inside its lifespan. Increasing workers starts more schedulers against a shared job store; separate scheduling ownership is required before scaling.

The `nginx/conf.d` TLS template is retained as a deployment reference; local Compose uses `nginx/local.conf`. Before public deployment, configure actual certificates, restricted host/origin policy, dashboard authentication, deployment secrets, access logging controls, tested backups and dependency review. The existing VPS scripts have not been run or validated as part of this local walkthrough.

The dashboard build accepts an optional local wheel cache in `dashboard/.wheel-cache`. The checked-in directory is empty; normal builds use PyPI. Cached wheels are ignored by Git and mounted only while installing, never copied into the runtime image. A complete compatible cache can be used offline with `docker compose -p drift-recon build --build-arg PIP_NO_INDEX=1 dashboard`; the default remains online installation. Local cache files used in this verification were checked against official PyPI SHA-256 hashes after working around stalled large downloads.
