"""Generate private local demo credentials once, without printing them."""

from pathlib import Path
import secrets

path = Path(__file__).resolve().parents[1] / ".env"
if path.exists():
    print("Existing .env preserved.")
else:
    password, redis = secrets.token_hex(24), secrets.token_hex(24)
    values = {
        "ENVIRONMENT": "development",
        "LOG_LEVEL": "INFO",
        "POSTGRES_USER": "recon_user",
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": "reconciliation",
        "DATABASE_URL": f"postgresql+asyncpg://recon_user:{password}@postgres:5432/reconciliation",
        "SECRET_KEY": secrets.token_hex(32),
        "API_KEY_SALT": secrets.token_hex(16),
        "VALID_API_KEYS": secrets.token_hex(24),
        "REDIS_PASSWORD": redis,
        "REDIS_URL": f"redis://:{redis}@redis:6379/0",
        "DASHBOARD_PORT": "8502",
        "API_PORT": "8200",
        "PROXY_PORT": "8082",
    }
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    path.chmod(0o600)
    print("Private local .env created. No credentials printed.")
