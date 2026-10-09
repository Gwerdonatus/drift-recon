# ─── Stage 1: Builder ──────────────────────────────────────────────────────────
# Separate build stage keeps the final image lean.
# Build dependencies (gcc, headers) never reach production.
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies into a prefix directory
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --prefix=/install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt


# ─── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Security: run as non-root user
RUN groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --no-create-home appuser

WORKDIR /app

# Runtime dependencies only (libpq for psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY --chown=appuser:appgroup app/ ./app/
COPY --chown=appuser:appgroup alembic/ ./alembic/
COPY --chown=appuser:appgroup alembic.ini .

# Drop to non-root
USER appuser

# Expose API port
EXPOSE 8000

# Health check — Docker will mark container unhealthy if this fails 3x
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Entrypoint: run migrations then start uvicorn
# Using exec form (not shell form) so signals are passed correctly to uvicorn
CMD ["sh", "-c", \
    "alembic upgrade head && \
     exec uvicorn app.main:app \
     --host 0.0.0.0 \
     --port 8000 \
     --workers 1 \
     --loop uvloop \
     --access-log \
     --proxy-headers \
     --forwarded-allow-ips='*'"]
