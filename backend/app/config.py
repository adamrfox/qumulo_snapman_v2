import os
import secrets
import sys
from pathlib import Path


class Settings:
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://snapman:snapman@db:5432/snapman"
    )
    jwt_secret: str = os.environ.get("JWT_SECRET") or secrets.token_hex(32)
    jwt_algorithm: str = "HS256"
    jwt_expire_seconds: int = 60 * 60 * 8

    _token_key: str = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
    admin_password: str = os.environ.get("ADMIN_PASSWORD", "")
    cache_path: str = os.environ.get("CACHE_PATH", "/data/snapman-cache.db")

    # Backend's own tee'd stdout/stderr log (app/main.py) -- same volume as
    # cache_path, just a subdirectory, so no extra volume mount is needed.
    log_dir: str = os.environ.get("LOG_DIR", str(Path(cache_path).parent / "logs"))
    # Shared volume nginx writes access/error logs into (see docker-compose.yml).
    nginx_log_dir: str = os.environ.get("NGINX_LOG_DIR", "/var/log/snapman")

    snapshot_listing_ttl: float = 300.0
    file_workers: int = int(os.environ.get("FILE_WORKERS", "16"))
    pair_workers: int = int(os.environ.get("PAIR_WORKERS", "4"))
    pair_batch_size: int = int(os.environ.get("PAIR_BATCH_SIZE", "50"))
    api_timeout: float = float(os.environ.get("API_TIMEOUT", "300.0"))

    @property
    def fernet(self):
        from cryptography.fernet import Fernet

        key = self._token_key
        if not key:
            print(
                "[snapman] TOKEN_ENCRYPTION_KEY is not set — Qumulo tokens cannot be stored.",
                file=sys.stderr,
            )
            raise RuntimeError("TOKEN_ENCRYPTION_KEY must be set in environment")
        return Fernet(key.encode() if isinstance(key, str) else key)


settings = Settings()
