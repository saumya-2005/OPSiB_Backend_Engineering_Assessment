"""Central configuration. Everything environment-specific lives here."""
from datetime import datetime
from decimal import Decimal
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://opsib:opsib@localhost:5432/opsib"
    test_database_url: str = "postgresql+psycopg://opsib:opsib@localhost:5432/opsib_test"
    tenant_id: str = "tenant-demo"

    # Frozen scenario time (timezone-aware). Brief: do NOT use the machine clock.
    evaluation_time: datetime = datetime.fromisoformat("2026-10-16T15:00:00+05:30")

    # Versions stamped on every record / finding for provenance.
    mapping_version: str = "map-v1"
    rule_version: str = "receipt-shortfall-v1"

    # Shortfall tolerance in BASE units (absolute). 0 = zero tolerance, as the brief asks.
    shortfall_tolerance_base: Decimal = Decimal("0")

    # Task due time = creation (frozen) time + this many hours.
    task_due_hours: int = 24

    # Optional AI investigation summary. 'none' (default) = fully deterministic, no network.
    ai_provider: str = "none"            # none | claude
    ai_timeout_seconds: float = 5.0
    ai_model: str = "claude-sonnet-5-5"

    # Workforce snapshot older than this (vs evaluation_time) is "stale".
    availability_max_age_minutes: int = 60

    @field_validator("evaluation_time")
    @classmethod
    def _must_be_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("EVALUATION_TIME must include a UTC offset, e.g. +05:30")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()