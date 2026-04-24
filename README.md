# Data Drift Diagnosis & Reconciliation Service

A production-grade system that continuously reconciles financial transactions against bank statements, detects data drift, and diagnoses root causes of mismatches.

> **"Something is wrong"** → most tools stop here.
> **"What broke, when it broke, and why it broke"** → what this system answers.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          Nginx (TLS)                            │
│              api.yourdomain.com  |  dashboard.yourdomain.com    │
└────────────────────┬────────────────────────┬───────────────────┘
                     │                        │
           ┌─────────▼──────────┐   ┌─────────▼──────────┐
           │   FastAPI (8000)   │   │ Streamlit (8501)    │
           │                    │   │                      │
           │  Ingestion Layer   │   │  Live Dashboard      │
           │  Matching Engine   │   │  Drift Visualizer    │
           │  Drift Analyzer    │   │  Run History         │
           │  REST API          │   │                      │
           └─────────┬──────────┘   └────────────────────-─┘
                     │
           ┌─────────▼──────────┐   ┌────────────────────┐
           │  PostgreSQL (5432) │   │   Redis (6379)     │
           │                    │   │                    │
           │  transactions      │   │  Rate limiting     │
           │  bank_statements   │   │  Job store         │
           │  recon_results     │   │                    │
           │  snapshots         │   └────────────────────┘
           │  drift_events      │
           │  quarantined_recs  │
           └────────────────────┘
```

## Core Features

| Feature | Implementation |
|---------|---------------|
| **Idempotent ingestion** | SHA-256 content hash → deterministic batch ID; `ON CONFLICT DO NOTHING` |
| **Confidence-based matching** | Multi-factor weighted scoring (amount, date, reference, description) |
| **Dead-letter quarantine** | Invalid rows parked in `quarantined_records`, never dropped |
| **Statistical drift detection** | Z-score on 30-day rolling window of match rate, confidence, date delta |
| **Root cause hypotheses** | Rule-based diagnosis per drift event type |
| **Human review workflow** | Analysts can override engine decisions; stored for model improvement |
| **Scheduled runs** | APScheduler with PostgreSQL job store (survives restarts) |
| **Structured logging** | JSON in production, colored in dev; every request gets a trace ID |

## Quick Start (Development)

### Prerequisites
- Docker & Docker Compose
- Python 3.12+ (for local dev without Docker)

### 1. Clone and configure
```bash
git clone https://github.com/Gwerdonatus/drift-recon
cd drift-recon
cp .env.example .env
# Edit .env — at minimum set SECRET_KEY, API_KEY_SALT, VALID_API_KEYS
```

### 2. Generate secure values
```bash
# SECRET_KEY
openssl rand -hex 32

# API_KEY_SALT
openssl rand -hex 16

# API key for clients
openssl rand -hex 24
```

### 3. Start the stack
```bash
docker compose up -d
docker compose logs -f api   # Watch startup
```

### 4. Run migrations
```bash
docker compose exec api alembic upgrade head
```

### 5. Generate and upload sample data
```bash
python scripts/generate_sample_data.py --rows 500 --output data/

API_KEY="your_key_from_env"

curl -X POST http://localhost:8000/api/v1/ingest/transactions \
  -H "X-API-Key: $API_KEY" \
  -F "file=@data/transactions.csv" \
  -F "source=demo"

curl -X POST http://localhost:8000/api/v1/ingest/bank-statements \
  -H "X-API-Key: $API_KEY" \
  -F "file=@data/bank_statements.csv" \
  -F "bank_name=demo"
```

### 6. Run reconciliation
```bash
curl -X POST http://localhost:8000/api/v1/reconciliation/run \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"source_name": "demo"}'
```

### 7. Open dashboard
Visit http://localhost:8501 (Streamlit) or http://localhost:8000/docs (FastAPI, dev only)

## API Reference

### Authentication
All endpoints require `X-API-Key` header.

### Ingestion
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/ingest/transactions` | Upload transaction CSV |
| `POST` | `/api/v1/ingest/bank-statements` | Upload bank statement CSV |

**CSV column names** are flexible — the ingestion service maps common variants automatically. Minimum required: `external_id`, `transaction_date`/`value_date`, `amount`.

### Reconciliation
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/reconciliation/run` | Trigger a reconciliation run |
| `GET`  | `/api/v1/reconciliation/results/{run_id}` | Get results for a run |
| `GET`  | `/api/v1/reconciliation/snapshots` | Historical snapshot list |
| `PATCH`| `/api/v1/reconciliation/results/{id}/review` | Human review override |

### Drift
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/drift/analyze/{source}` | Run drift analysis |
| `GET`  | `/api/v1/drift/summary/{source}` | 30-day drift summary |
| `GET`  | `/api/v1/drift/events` | List drift events |
| `PATCH`| `/api/v1/drift/events/{id}/resolve` | Resolve a drift event |

## Matching Algorithm

Confidence score = weighted sum of four factors:

```
confidence = 0.50 × amount_score
           + 0.25 × date_score
           + 0.15 × reference_score
           + 0.10 × description_score
```

| Score | 1.0 | 0.9 | 0.7 | 0.5 | 0.0 |
|-------|-----|-----|-----|-----|-----|
| **Amount** | Exact | ≤1% diff | — | ≤3% diff | >3% diff |
| **Date** | Same day | ±1d | ±2d | ±3d | >3d |
| **Reference** | Exact | Token sort ≥90% | Partial ≥70% | — | No match |
| **Description** | 95%+ similarity | Linear scale | | | No overlap |

Threshold defaults:
- `≥0.75` → **MATCHED** (auto)
- `0.50–0.75` → **REVIEW** (human queue)
- `<0.50` → **UNMATCHED**

## Drift Detection

Z-score based anomaly detection on a 30-day rolling window:

```
z = (current_metric - baseline_mean) / baseline_stddev
```

Monitored metrics: `match_rate`, `avg_confidence`, `avg_date_delta_days`, `unmatched_count`

| |z| | Severity |
|------|---------|
| 2.0–2.5σ | LOW |
| 2.5–3.0σ | MEDIUM |
| >3.0σ | HIGH |

Requires minimum 7 snapshots before drift analysis activates.

## VPS Deployment

See the [RUNBOOK](RUNBOOK.md) for full operational procedures.

### Initial setup
```bash
# On your VPS as root
wget https://raw.githubusercontent.com/your-repo/main/scripts/setup_vps.sh
sudo bash setup_vps.sh
```

### SSL certificates
```bash
# Using Let's Encrypt (certbot)
sudo certbot certonly --standalone -d api.yourdomain.com -d dashboard.yourdomain.com
sudo cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem /opt/drift-recon/nginx/ssl/
sudo cp /etc/letsencrypt/live/yourdomain.com/privkey.pem /opt/drift-recon/nginx/ssl/
```

### Update nginx config
Edit `nginx/conf.d/default.conf` — replace `yourdomain.com` with your actual domain.

## Running Tests
```bash
# Unit tests only (no DB required)
pytest tests/test_matcher.py -m unit -v

# Full test suite (requires test DB)
export TEST_DATABASE_URL="postgresql+asyncpg://recon_user:pass@localhost:5432/recon_test"
pytest tests/ -v

# With coverage
pytest tests/ --cov=app --cov-report=html
```

## Project Structure
```
drift-recon/
├── app/
│   ├── api/v1/          # FastAPI routers (ingestion, reconciliation, drift)
│   ├── core/            # Logging, security, exceptions
│   ├── models/          # SQLAlchemy models
│   ├── schemas/         # Pydantic v2 schemas
│   ├── services/        # Business logic (ingestion, matcher, drift_analyzer)
│   ├── workers/         # Background scheduler
│   ├── config.py        # Settings with validation
│   ├── database.py      # Engine, session, health check
│   └── main.py          # FastAPI app factory
├── alembic/             # Database migrations
├── dashboard/           # Streamlit monitoring dashboard
├── nginx/               # Reverse proxy config
├── postgres/            # DB config and init scripts
├── scripts/             # VPS setup, backup, credential rotation, data gen
├── tests/               # Pytest unit + integration tests
├── .github/workflows/   # CI/CD pipeline
├── docker-compose.yml
├── Dockerfile
└── RUNBOOK.md
```

## Security Model
- API key auth with HMAC-SHA256 hashing (raw keys never stored post-startup)
- Constant-time key comparison (timing attack resistant)
- No Postgres or Redis ports exposed on host in production
- All traffic terminates at Nginx with TLS 1.2/1.3
- Non-root Docker containers
- Pre-commit hook blocks secret detection and direct main pushes

## License
MIT
