"""Built-in app loading for agent runtimes."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from nanobot.apps.reminder import ReminderApp
from nanobot.apps.reminder.tools import build_reminder_app_tools

if TYPE_CHECKING:
    from nanobot.agent.tools.context import ToolContext
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.config.schema import AppsConfig


class AppLoader:
    """Register enabled built-in apps into an agent tool registry."""

    def __init__(self, apps_config: "AppsConfig"):
        self._apps_config = apps_config

    def load(self, ctx: "ToolContext", registry: "ToolRegistry") -> list[str]:
        registered: list[str] = []
        reminder_app = ReminderApp(
            config=self._apps_config.reminder,
            workspace=Path(ctx.workspace).expanduser(),
            cron_service=ctx.cron_service,
            timezone=ctx.timezone,
        )
        if reminder_app.enabled:
            if reminder_app.service_enabled and reminder_app.scheduler:
                reminder_app.scheduler.ensure_todos_refresh_job(
                    interval_seconds=reminder_app.config.refresh_interval_seconds,
                )
            for tool in build_reminder_app_tools(reminder_app):
                registry.register(tool)
                registered.append(tool.name)
        return registered
