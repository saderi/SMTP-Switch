"""Configuration models and loader.

Config is read from a YAML file (path via ``SMTP_SWITCH_CONFIG`` env var, default
``config.yaml``). Any leaf value may be overridden by an environment variable of
the form ``SMTP_SWITCH_<SECTION>__<KEY>`` (double underscore = nesting), which is
handy for injecting secrets in a container.
"""

from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from pydantic_settings.sources import InitSettingsSource

DEFAULT_CONFIG_PATH = "config.yaml"
ENV_PREFIX = "SMTP_SWITCH_"

PositiveInt = Annotated[int, Field(gt=0)]
NonNegInt = Annotated[int, Field(ge=0)]


class TLSConfig(BaseModel):
    cert_file: Path | None = None
    key_file: Path | None = None
    require_starttls: bool = False

    @model_validator(mode="after")
    def _check_pair(self) -> TLSConfig:
        if bool(self.cert_file) ^ bool(self.key_file):
            raise ValueError("tls.cert_file and tls.key_file must be set together")
        if self.require_starttls and not self.cert_file:
            raise ValueError("tls.require_starttls is true but no cert_file/key_file given")
        return self

    @property
    def enabled(self) -> bool:
        return self.cert_file is not None


class IngressConfig(BaseModel):
    host: str = "0.0.0.0"
    port: NonNegInt = 2525  # 0 => bind an ephemeral port (tests)
    hostname: str = "smtp-switch.local"
    tls: TLSConfig = Field(default_factory=TLSConfig)
    allowed_ips: list[str] = Field(default_factory=lambda: ["127.0.0.1/32", "::1/128"])
    max_message_bytes: PositiveInt = 26_214_400  # 25 MiB
    require_auth: bool = True

    @field_validator("allowed_ips")
    @classmethod
    def _validate_cidrs(cls, v: list[str]) -> list[str]:
        for entry in v:
            ipaddress.ip_network(entry, strict=False)
        return v

    @property
    def allowed_networks(self) -> list[ipaddress._BaseNetwork]:
        return [ipaddress.ip_network(e, strict=False) for e in self.allowed_ips]


class RetryConfig(BaseModel):
    base_delay_seconds: PositiveInt = 30
    max_delay_seconds: PositiveInt = 3600
    max_attempts: PositiveInt = 12
    max_age_hours: PositiveInt = 48
    jitter_ratio: float = Field(default=0.2, ge=0, le=1)

    @model_validator(mode="after")
    def _order(self) -> RetryConfig:
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("retry.max_delay_seconds must be >= base_delay_seconds")
        return self


class DispatchConfig(BaseModel):
    workers: PositiveInt = 4
    spool_dir: Path = Path("./data/spool")
    claim_batch_size: PositiveInt = 20
    poll_interval_seconds: float = Field(default=1.0, gt=0)
    failover_per_tick: PositiveInt = 3
    no_capacity_backoff_seconds: PositiveInt = 20
    retry: RetryConfig = Field(default_factory=RetryConfig)
    # Housekeeping: how long to keep terminal messages (and their spooled bodies)
    # before a background sweep deletes them. 0 disables the sweep.
    sent_retention_hours: NonNegInt = 168      # 7 days
    deadletter_retention_hours: NonNegInt = 720  # 30 days


class ProviderLimits(BaseModel):
    max_concurrent: PositiveInt | None = None
    per_second: PositiveInt | None = None
    per_minute: PositiveInt | None = None
    per_hour: PositiveInt | None = None
    per_day: PositiveInt | None = None
    per_month: PositiveInt | None = None
    month_reset_day: int = Field(default=1, ge=1, le=28)

    @property
    def sliding_windows(self) -> dict[str, tuple[int, int]]:
        """window name -> (span_seconds, limit) for limits enforced via send_log."""
        out: dict[str, tuple[int, int]] = {}
        if self.per_second:
            out["per_second"] = (1, self.per_second)
        if self.per_minute:
            out["per_minute"] = (60, self.per_minute)
        if self.per_hour:
            out["per_hour"] = (3600, self.per_hour)
        return out

    @property
    def max_window_span(self) -> int:
        spans = [span for span, _ in self.sliding_windows.values()]
        return max(spans) if spans else 0


class ProviderSMTP(BaseModel):
    host: str
    port: PositiveInt = 587
    starttls: bool = True
    tls: bool = False  # implicit TLS (port 465 style); mutually exclusive with starttls
    username: str | None = None
    password: str | None = None
    timeout_seconds: PositiveInt = 30
    verify_cert: bool = True

    @model_validator(mode="after")
    def _tls_mode(self) -> ProviderSMTP:
        if self.tls and self.starttls:
            raise ValueError("provider smtp: set only one of tls (implicit) or starttls")
        return self


class ProviderConfig(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    enabled: bool = True
    priority: int = 100
    smtp: ProviderSMTP
    limits: ProviderLimits = Field(default_factory=ProviderLimits)


class CircuitBreakerConfig(BaseModel):
    failure_threshold: PositiveInt = 5
    cooldown_seconds: PositiveInt = 120
    half_open_max_probes: PositiveInt = 1


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: NonNegInt = 8080  # 0 => bind an ephemeral port (tests)
    session_secret: str = Field(default="change-me-in-config", min_length=8)
    session_ttl_seconds: PositiveInt = 86_400


class DatabaseConfig(BaseModel):
    url: str = "sqlite+aiosqlite:///./data/smtp_switch.db"
    echo: bool = False
    # Create any missing tables on startup. Convenient for single-file SQLite;
    # set false when you manage the schema with Alembic migrations.
    auto_create: bool = True


class LoggingConfig(BaseModel):
    model_config = {"populate_by_name": True}

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_format: bool = Field(default=True, alias="json")


class MetricsConfig(BaseModel):
    enabled: bool = True


class _YamlSource(PydanticBaseSettingsSource):
    """Reads the whole settings tree from a YAML file."""

    def __init__(self, settings_cls: type[BaseSettings], data: dict):
        super().__init__(settings_cls)
        self._data = data

    def get_field_value(self, field, field_name):  # pragma: no cover - unused hook
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict:
        return self._data


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Populated by load_settings() before source resolution.
    _yaml_data: dict = {}

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_data: dict = {}
        if isinstance(init_settings, InitSettingsSource):
            yaml_data = dict(init_settings.init_kwargs)
        # Priority: env vars > .env > YAML file > file secrets.
        return (
            env_settings,
            dotenv_settings,
            _YamlSource(settings_cls, yaml_data),
            file_secret_settings,
        )

    ingress: IngressConfig = Field(default_factory=IngressConfig)
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    providers: list[ProviderConfig] = Field(default_factory=list)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)

    @model_validator(mode="after")
    def _validate_providers(self) -> Settings:
        names = [p.name for p in self.providers]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate provider names: {sorted(dupes)}")
        return self

    @property
    def enabled_providers(self) -> list[ProviderConfig]:
        return sorted(
            (p for p in self.providers if p.enabled),
            key=lambda p: (p.priority, p.name),
        )

    def provider(self, name: str) -> ProviderConfig | None:
        return next((p for p in self.providers if p.name == name), None)


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Load settings from a YAML file, then let env vars override."""
    cfg_path = Path(path or os.environ.get(f"{ENV_PREFIX}CONFIG", DEFAULT_CONFIG_PATH))
    file_data: dict = {}
    if cfg_path.is_file():
        loaded = yaml.safe_load(cfg_path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{cfg_path}: top level must be a mapping")
        file_data = loaded
    elif path is not None:
        raise FileNotFoundError(f"config file not found: {cfg_path}")
    return Settings(**file_data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
