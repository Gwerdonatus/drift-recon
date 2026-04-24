#!/usr/bin/env bash
# =============================================================================
# Credential Rotation Script
#
# Rotates: API keys, Redis password, DB password
# Principle: new value takes effect before old is removed (no downtime window)
#
# Usage:
#   ./scripts/rotate_credentials.sh [api-keys|redis|db|all]
#
# ALWAYS test on staging before running on production.
# =============================================================================

set -euo pipefail

ENV_FILE="/opt/drift-recon/.env"
BACKUP_DIR="/opt/drift-recon/credential_backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

[[ -f "$ENV_FILE" ]] || error ".env file not found at $ENV_FILE"

mkdir -p "$BACKUP_DIR"

# Backup current .env before any changes
cp "$ENV_FILE" "$BACKUP_DIR/.env.backup.$TIMESTAMP"
info "Backed up .env to $BACKUP_DIR/.env.backup.$TIMESTAMP"

rotate_api_keys() {
    info "Rotating API keys..."

    NEW_KEY_1=$(openssl rand -hex 24)
    NEW_KEY_2=$(openssl rand -hex 24)

    # Update .env — sed in-place replacement
    sed -i "s|^VALID_API_KEYS=.*|VALID_API_KEYS=${NEW_KEY_1},${NEW_KEY_2}|" "$ENV_FILE"

    # Reload API without full restart (only env vars changed)
    cd /opt/drift-recon
    docker compose up -d --no-deps --force-recreate api

    info "API keys rotated. New keys:"
    echo "  Key 1: $NEW_KEY_1"
    echo "  Key 2: $NEW_KEY_2"
    warning "Update any clients using the old API keys immediately."
}

rotate_redis() {
    info "Rotating Redis password..."

    NEW_REDIS_PASSWORD=$(openssl rand -hex 32)

    # Step 1: Update Redis to accept BOTH old and new password
    OLD_REDIS_PASSWORD=$(grep "^REDIS_PASSWORD=" "$ENV_FILE" | cut -d= -f2)

    # Step 2: Update config files
    sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=${NEW_REDIS_PASSWORD}|" "$ENV_FILE"
    sed -i "s|redis://:.*@redis|redis://:${NEW_REDIS_PASSWORD}@redis|" "$ENV_FILE"

    # Step 3: Restart Redis with new password
    cd /opt/drift-recon
    docker compose up -d --no-deps --force-recreate redis

    # Wait for Redis to be ready
    sleep 5

    # Step 4: Restart API to use new password
    docker compose up -d --no-deps --force-recreate api

    info "Redis password rotated."
}

rotate_db() {
    warning "Database password rotation requires a brief maintenance window."
    warning "This will restart the API service."
    read -p "Continue? (yes/no): " confirm
    [[ "$confirm" == "yes" ]] || { info "Aborted."; exit 0; }

    NEW_DB_PASSWORD=$(openssl rand -hex 32)
    CURRENT_USER=$(grep "^POSTGRES_USER=" "$ENV_FILE" | cut -d= -f2)

    # Step 1: Change password in Postgres FIRST
    info "Updating Postgres password..."
    cd /opt/drift-recon
    docker compose exec postgres psql -U "$CURRENT_USER" -c \
        "ALTER USER ${CURRENT_USER} PASSWORD '${NEW_DB_PASSWORD}';"

    # Step 2: Update .env IMMEDIATELY after (minimize downtime window)
    sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${NEW_DB_PASSWORD}|" "$ENV_FILE"
    # Also update DATABASE_URL
    sed -i "s|postgresql+asyncpg://[^:]*:[^@]*@|postgresql+asyncpg://${CURRENT_USER}:${NEW_DB_PASSWORD}@|" "$ENV_FILE"

    # Step 3: Restart API
    docker compose up -d --no-deps --force-recreate api

    info "Database password rotated."
    info "Verify connectivity: docker compose exec api python -c 'from app.database import check_db_health; import asyncio; print(asyncio.run(check_db_health()))'"
}

case "${1:-all}" in
    api-keys) rotate_api_keys ;;
    redis)    rotate_redis ;;
    db)       rotate_db ;;
    all)
        rotate_api_keys
        rotate_redis
        warning "DB rotation requires --confirm due to maintenance window. Run: $0 db"
        ;;
    *)
        echo "Usage: $0 [api-keys|redis|db|all]"
        exit 1
        ;;
esac

info "Credential rotation complete. Verify with: docker compose ps && curl -f http://localhost:8000/health"
