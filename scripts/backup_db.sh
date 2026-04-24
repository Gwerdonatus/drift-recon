#!/usr/bin/env bash
# =============================================================================
# PostgreSQL Backup Script
#
# Strategy: Daily dumps, kept for 7 days locally.
# Optionally upload to S3-compatible storage (set S3_BUCKET env var).
#
# Schedule with cron (as the recon user):
#   0 2 * * * /opt/drift-recon/scripts/backup_db.sh >> /var/log/recon_backup.log 2>&1
#
# Restore:
#   gunzip -c /opt/drift-recon/backups/recon_20240115_020000.sql.gz \
#     | docker compose exec -T postgres psql -U recon_user reconciliation
# =============================================================================

set -euo pipefail

BACKUP_DIR="/opt/drift-recon/backups"
RETENTION_DAYS=7
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/recon_${TIMESTAMP}.sql.gz"

# Load env vars from .env file
if [[ -f /opt/drift-recon/.env ]]; then
    set -a
    source /opt/drift-recon/.env
    set +a
fi

: "${POSTGRES_USER:?POSTGRES_USER not set}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD not set}"
: "${POSTGRES_DB:?POSTGRES_DB not set}"

mkdir -p "$BACKUP_DIR"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting backup of $POSTGRES_DB"

# Dump via Docker (no pg_dump installed on host needed)
docker compose -f /opt/drift-recon/docker-compose.yml \
    exec -T postgres \
    pg_dump \
    --username="$POSTGRES_USER" \
    --no-password \
    --format=plain \
    --clean \
    --if-exists \
    "$POSTGRES_DB" \
    | gzip -9 > "$BACKUP_FILE"

SIZE=$(du -sh "$BACKUP_FILE" | cut -f1)
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Backup complete: $BACKUP_FILE ($SIZE)"

# Optional S3 upload (uses AWS CLI or rclone)
if [[ -n "${S3_BUCKET:-}" ]]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Uploading to s3://${S3_BUCKET}/backups/"
    aws s3 cp "$BACKUP_FILE" "s3://${S3_BUCKET}/backups/$(basename $BACKUP_FILE)" \
        --storage-class STANDARD_IA
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] S3 upload complete"
fi

# Delete backups older than RETENTION_DAYS
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Pruning backups older than ${RETENTION_DAYS} days"
find "$BACKUP_DIR" -name "recon_*.sql.gz" -mtime "+${RETENTION_DAYS}" -delete

REMAINING=$(ls -1 "$BACKUP_DIR"/recon_*.sql.gz 2>/dev/null | wc -l)
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Backup rotation complete. ${REMAINING} backups retained."
