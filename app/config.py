from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    deployment_mode: Literal["full", "core", "hybrid"] = "full"
    app_port: int = 7860
    mongodb_uri: SecretStr = SecretStr("mongodb://localhost:27017")
    app_database_name: str = "genieacs_manager"
    genieacs_nbi_url: str = "http://127.0.0.1:7557"
    genieacs_nbi_auth_token: SecretStr = SecretStr("")
    core_api_url: str = "http://127.0.0.1:7860"
    api_token: SecretStr = SecretStr("")
    bot_api_token: SecretStr = SecretStr("")
    telegram_api_id: int = 0
    telegram_api_hash: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_owner_id: int = 0
    telegram_admin_ids: str = ""
    bot_enabled: bool = True
    device_discovery_interval: int = Field(10, ge=5)
    device_status_interval: int = Field(30, ge=10)
    metrics_collection_interval: int = Field(300, ge=60)
    device_active_threshold: int = Field(600, ge=30)
    device_offline_threshold: int = Field(1800, ge=60)
    require_device_approval: bool = True
    allow_high_risk_actions: bool = False
    notify_new_devices: bool = True
    notify_offline: bool = True
    notify_reconnected: bool = True
    notify_task_failures: bool = True
    retention_days: int = Field(30, ge=1)
    audit_retention_days: int = Field(365, ge=30)
    worker_concurrency: int = Field(8, ge=1, le=32)
    max_bulk_devices: int = Field(100, ge=1, le=500)
    task_timeout: int = Field(3600, ge=60)
    monitor_interval: int = Field(30, ge=15)
    monitor_ttl: int = Field(900, ge=60, le=3600)
    max_monitor_sessions: int = Field(50, ge=1, le=200)
    log_level: str = "INFO"
    tz: str = "Asia/Kolkata"
    # Exact paths only; secret-bearing parameters are never accepted.
    parameter_allowlist: list[str] = []
    # RPCs must be verified per model by an administrator before enabling.
    model_capabilities: dict[str, list[str]] = {}
    firmware_catalog: dict[str, dict] = {}
    metric_profiles: dict[str, dict] = {}
    quiet_hours_start: int = Field(0, ge=0, le=23)
    quiet_hours_end: int = Field(0, ge=0, le=23)
    report_interval_hours: int = Field(24, ge=1)
    enable_scheduled_reports: bool = False
    report_schedule: Literal["interval", "daily", "weekly", "monthly"] = "daily"
    alert_cooldown: int = Field(300, ge=0)

    @property
    def admin_ids(self):
        return {int(x.strip()) for x in self.telegram_admin_ids.split(",") if x.strip()}

    @model_validator(mode="after")
    def validate_settings(self):
        if self.device_offline_threshold <= self.device_active_threshold:
            raise ValueError("Offline threshold must exceed active threshold")
        for name in ("api_token", "bot_api_token"):
            if len(getattr(self, name).get_secret_value()) < 32:
                raise ValueError(f"{name} must contain at least 32 characters")
        if self.api_token == self.bot_api_token:
            raise ValueError("Use distinct API_TOKEN and BOT_API_TOKEN")
        if self.telegram_owner_id <= 0:
            raise ValueError("Set TELEGRAM_OWNER_ID")
        if self.bot_enabled and self.deployment_mode != "core":
            if not (
                self.telegram_api_id
                and self.telegram_api_hash.get_secret_value()
                and self.telegram_bot_token.get_secret_value()
            ):
                raise ValueError("Telegram bot credentials are required")
        for name in ("genieacs_nbi_url", "core_api_url"):
            u = urlsplit(getattr(self, name))
            if u.username or u.password or u.query or u.fragment:
                raise ValueError(f"{name} must not contain credentials, query or fragment")
            local = u.hostname in {"localhost", "127.0.0.1", "nbi", "core", "manager"}
            if u.scheme != "https" and not (local and u.scheme == "http"):
                raise ValueError(f"{name} requires HTTPS outside the private Compose network")
        if self.deployment_mode == "hybrid" and urlsplit(self.core_api_url).scheme != "https":
            raise ValueError("Hybrid mode requires an external HTTPS CORE_API_URL")
        return self
