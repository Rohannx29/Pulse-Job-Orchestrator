import os
import sys


_INSECURE_DEFAULT_KEY = "dev-secret-change-me"
_IS_TESTING = (
    "pytest" in sys.modules
    or os.getenv("PYTEST_CURRENT_TEST") is not None
    or any("pytest" in arg for arg in sys.argv)
)


class Settings:
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql+psycopg2://pulse:pulse@postgres:5432/pulse"
    )
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
    SECRET_KEY: str = os.getenv("SECRET_KEY", _INSECURE_DEFAULT_KEY)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
    REFRESH_TOKEN_EXPIRE_DAYS: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))
    HEARTBEAT_INTERVAL_SECONDS: int = 15

    # Structured logging
    LOG_FORMAT: str = os.getenv("LOG_FORMAT", "json")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    SERVICE_NAME: str = os.getenv("SERVICE_NAME", "pulse-api")

    # Data retention & table pruning
    JOB_LOG_RETENTION_DAYS: int = int(os.getenv("JOB_LOG_RETENTION_DAYS", "30"))
    EXECUTION_RETENTION_DAYS: int = int(os.getenv("EXECUTION_RETENTION_DAYS", "30"))
    COMPLETED_JOB_RETENTION_DAYS: int = int(os.getenv("COMPLETED_JOB_RETENTION_DAYS", "60"))
    HEARTBEAT_RETENTION_DAYS: int = int(os.getenv("HEARTBEAT_RETENTION_DAYS", "7"))
    AUDIT_LOG_RETENTION_DAYS: int = int(os.getenv("AUDIT_LOG_RETENTION_DAYS", "365"))
    PRUNE_BATCH_SIZE: int = int(os.getenv("PRUNE_BATCH_SIZE", "1000"))
    PRUNE_INTERVAL_SECONDS: float = float(os.getenv("PRUNE_INTERVAL_SECONDS", "3600.0"))

    # Set IS_DEV=true only in local development. In any other environment the
    # application will refuse to start if SECRET_KEY is still the insecure default.
    IS_DEV: bool = os.getenv("IS_DEV", "false").lower() == "true" or _IS_TESTING


    # The dashboard is served from a separate origin during development, so
    # credentialed requests need an explicit origin instead of a wildcard.
    CORS_ORIGINS: list[str] = [
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
        if origin.strip()
    ]
    ACCESS_TOKEN_COOKIE_NAME: str = "pulse_access_token"
    REFRESH_TOKEN_COOKIE_NAME: str = "pulse_refresh_token"
    COOKIE_SECURE: bool = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    COOKIE_SAMESITE: str = os.getenv("COOKIE_SAMESITE", "lax")


settings = Settings()

# Fail fast if running with the insecure default key outside of dev.
# This prevents accidentally deploying without setting a real secret.
if settings.SECRET_KEY == _INSECURE_DEFAULT_KEY and not settings.IS_DEV:
    raise RuntimeError(
        "SECRET_KEY is set to the insecure default value. "
        "Set a cryptographically random SECRET_KEY environment variable before starting the server. "
        "To suppress this check in local development, set IS_DEV=true."
    )

