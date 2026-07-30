from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Auth in front of this proxy. REQUIRED. Generate a long random string.
    proxy_api_key: str = ""

    # Garmin credentials. Only needed for the one-time /garmin/login bootstrap.
    # After tokens are persisted you can leave these unset.
    garmin_email: str | None = None
    garmin_password: str | None = None

    # Persisted to a Railway Volume so login survives redeploys (hands-off).
    garmin_token_dir: str = "/data/garmin_tokens"
    db_path: str = "/data/garmin.db"

    # Server-side poll cadence.
    poll_interval_minutes: int = 30
    timezone: str = "America/New_York"

    # How many recent activities to pull each poll.
    activities_limit: int = 5

    # Per-metric retry. Garmin's slower endpoints (get_stats especially) sometimes
    # exceed garth's 10s read timeout; one retry a few seconds later almost always
    # lands. Worst case stays far inside the poll interval.
    metric_attempts: int = 2
    retry_delay_seconds: int = 5

    # How many days of overnight history to retain (rolling window).
    # Each metric keeps one row per day; older rows are pruned after each poll.
    retention_days: int = 3


settings = Settings()
