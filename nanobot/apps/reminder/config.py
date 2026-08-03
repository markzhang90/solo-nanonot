"""Configuration for the built-in Reminder App."""

from __future__ import annotations

from pydantic import Field

from nanobot.config_base import Base


class ReminderAppConfig(Base):
    """Reminder App runtime settings."""

    enabled: bool = False
    base_url: str = Field(default="", validation_alias="baseUrl", serialization_alias="baseUrl")
    device_sn: str = Field(default="", validation_alias="deviceSn", serialization_alias="deviceSn")
    device_secret: str = Field(
        default="",
        validation_alias="deviceSecret",
        serialization_alias="deviceSecret",
    )
    bearer_token: str = Field(
        default="",
        repr=False,
        validation_alias="bearerToken",
        serialization_alias="bearerToken",
    )
    timeout_seconds: int = Field(
        default=10,
        ge=1,
        le=120,
        validation_alias="timeoutSeconds",
        serialization_alias="timeoutSeconds",
    )
    refresh_interval_seconds: int = Field(
        default=300,
        ge=60,
        validation_alias="refreshIntervalSeconds",
        serialization_alias="refreshIntervalSeconds",
    )
    verify_ssl: bool = Field(
        default=True,
        validation_alias="verifySsl",
        serialization_alias="verifySsl",
    )
