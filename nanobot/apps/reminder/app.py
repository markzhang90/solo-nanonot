"""Reminder App runtime facade."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from nanobot.apps.reminder.config import ReminderAppConfig
from nanobot.apps.reminder.legacy import ReminderToolConfigData
from nanobot.apps.reminder.scheduler import ReminderScheduler

if TYPE_CHECKING:
    from nanobot.agent.tools.context import ToolContext
    from nanobot.config.schema import Config


@dataclass(frozen=True)
class ReminderApp:
    """Thin facade that owns Reminder App config, workspace, and scheduler access."""

    config: ReminderAppConfig
    workspace: Path
    cron_service: object | None = None
    timezone: str = "UTC"

    @classmethod
    def from_context(cls, ctx: "ToolContext") -> "ReminderApp":
        from nanobot.config.loader import load_config

        root_config = load_config()
        return cls.from_config(root_config, Path(ctx.workspace), ctx.cron_service)

    @classmethod
    def from_config(
        cls,
        root_config: "Config",
        workspace: Path,
        cron_service: object | None = None,
    ) -> "ReminderApp":
        return cls(
            config=root_config.apps.reminder,
            workspace=workspace.expanduser(),
            cron_service=cron_service,
            timezone=root_config.agents.defaults.timezone,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    @property
    def service_enabled(self) -> bool:
        return self.enabled and bool(self.config.base_url.strip())

    @property
    def scheduler(self) -> ReminderScheduler | None:
        if self.cron_service is None:
            return None
        return ReminderScheduler(self.cron_service)

    def tool_config(self) -> ReminderToolConfigData:
        return ReminderToolConfigData(
            enabled=self.config.enabled,
            base_url=self.config.base_url,
            timeout_seconds=self.config.timeout_seconds,
            bearer_token=self.config.bearer_token,
            device_sn=self.config.device_sn,
            device_secret=self.config.device_secret,
            verify_ssl=self.config.verify_ssl,
        )
