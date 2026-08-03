"""Tool wrappers exposed by the Reminder App."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import RequestContext, ToolContext
from nanobot.apps.reminder import legacy
from nanobot.apps.reminder.app import ReminderApp
from nanobot.runtime_context import RuntimeContextBlock, wrap_runtime_context_lines


def _app(ctx: ToolContext) -> ReminderApp:
    return ReminderApp.from_context(ctx)


class _ReminderEnabled:
    config_key = "apps.reminder"

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        try:
            return _app(ctx).enabled
        except Exception:
            return False


class _ReminderServiceEnabled(_ReminderEnabled):
    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        try:
            return _app(ctx).service_enabled
        except Exception:
            return False


class ReminderAppStatusTool(_ReminderEnabled, Tool):
    """Expose Reminder App load status to tools and runtime context."""

    name = "reminder_app_status"
    description = (
        "Report whether the built-in Reminder App is loaded and configured. "
        "Use this when the user asks what apps are loaded or whether task/reminder management is available."
    )
    parameters = {"type": "object", "properties": {}}
    read_only = True

    def __init__(self, app: ReminderApp):
        self._app = app

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(_app(ctx))

    @staticmethod
    def _masked_device_sn(device_sn: str) -> str:
        value = device_sn.strip()
        if len(value) <= 4:
            return "*" * len(value) if value else ""
        return f"{'*' * (len(value) - 4)}{value[-4:]}"

    def runtime_context_provider(self):
        return self._provide_runtime_context

    async def _provide_runtime_context(
        self,
        request: RequestContext,
    ) -> RuntimeContextBlock:
        _ = request
        cfg = self._app.config
        now = datetime.now(ZoneInfo(self._app.timezone))
        today = now.date().isoformat()
        lines = [
            "Loaded App: Reminder App",
            "App id: reminder",
            "Purpose: task-like user requests, planned tasks, ad-hoc supervised tasks, one-off reminders, start prompts, wakeups, completion, deferral, and day-level task queries.",
            f"Current date: {today}",
            f"Current time: {now.isoformat()}",
            f"Timezone: {self._app.timezone}",
            f"Reminder query date for 'today': {today}",
            f"Reminder Service: {'configured' if self._app.service_enabled else 'local-only'}",
            f"Device SN configured: {'yes' if cfg.device_sn.strip() else 'no'}",
            "Preferred tools: reminder_list_todos for day facts; reminder_act for start/complete/defer/wait actions; reminder_schedule for one-off/start/current wakeups; reminder_update_task for active current changes. Lower-level sync/resumable/wakeup tools are maintenance-only and are not registered by default.",
        ]
        return RuntimeContextBlock(
            source="reminder_app",
            content=wrap_runtime_context_lines(lines),
        )

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        cfg = self._app.config
        return legacy._success({
            "id": "reminder",
            "name": "Reminder App",
            "loaded": True,
            "enabled": self._app.enabled,
            "service_enabled": self._app.service_enabled,
            "base_url": cfg.base_url,
            "device_sn_configured": bool(cfg.device_sn.strip()),
            "device_sn_masked": self._masked_device_sn(cfg.device_sn),
            "scheduler": "nanobot CronService / automation" if self._app.scheduler else "unavailable",
        })


class ReminderListTodayTasksTool(_ReminderServiceEnabled, legacy.ReminderListTodayTasksTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.tool_config(), app.workspace)


class ReminderRefreshTodayTodosTool(_ReminderServiceEnabled, legacy.ReminderRefreshTodayTodosTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.tool_config(), app.workspace, app.scheduler)


class ReminderListTodosTool(_ReminderEnabled, legacy.ReminderListTodosTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(
            app.workspace,
            app.tool_config() if app.service_enabled else None,
            app.scheduler,
        )


class ReminderStartTaskTool(_ReminderServiceEnabled, legacy.ReminderStartTaskTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.tool_config(), app.workspace, app.scheduler)


class ReminderCompleteTaskTool(_ReminderEnabled, legacy.ReminderCompleteTaskTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.tool_config(), app.workspace, app.scheduler)


class ReminderSyncCurrentTaskTool(_ReminderServiceEnabled, legacy.ReminderSyncCurrentTaskTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.tool_config(), app.workspace)


class ReminderGetResumableTaskTool(_ReminderServiceEnabled, legacy.ReminderGetResumableTaskTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.tool_config(), app.workspace)


class ReminderDeferTaskTool(_ReminderServiceEnabled, legacy.ReminderDeferTaskTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.tool_config(), app.workspace)


class ReminderActTool(_ReminderEnabled, legacy.ReminderActTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.tool_config(), app.workspace, app.scheduler)

class ReminderStartAdHocTaskTool(_ReminderEnabled, legacy.ReminderStartAdHocTaskTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.workspace, app.scheduler)


class ReminderScheduleOneOffTool(_ReminderEnabled, legacy.ReminderScheduleOneOffTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.workspace, app.scheduler)


class ReminderScheduleStartPromptTool(_ReminderEnabled, legacy.ReminderScheduleStartPromptTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.workspace, app.scheduler)


class ReminderScheduleTool(_ReminderEnabled, legacy.ReminderScheduleTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.workspace, app.scheduler)

class ReminderUpdateTaskTool(_ReminderEnabled, legacy.ReminderUpdateTaskTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.workspace, app.scheduler)


class ScheduleTaskWakeupTool(_ReminderEnabled, legacy.ScheduleTaskWakeupTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.scheduler, app.workspace)


class CancelTaskWakeupTool(_ReminderEnabled, legacy.CancelTaskWakeupTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        app = _app(ctx)
        return cls(app.scheduler, app.workspace)


class ListTaskWakeupsTool(_ReminderEnabled, legacy.ListTaskWakeupsTool):
    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(_app(ctx).scheduler)


def build_reminder_tools(ctx: ToolContext) -> list[Tool]:
    """Build Reminder App tools for tests and explicit registration."""
    return build_reminder_app_tools(_app(ctx))


def build_reminder_app_tools(app: ReminderApp) -> list[Tool]:
    """Build enabled Reminder App tools from an explicit app instance."""
    tools: list[Tool] = [ReminderAppStatusTool(app)]
    tools.extend([
        ReminderListTodosTool(
            app.workspace,
            app.tool_config() if app.service_enabled else None,
            app.scheduler,
        ),
        ReminderActTool(app.tool_config(), app.workspace, app.scheduler),
        ReminderScheduleTool(app.workspace, app.scheduler),
        ReminderUpdateTaskTool(app.workspace, app.scheduler),
    ])
    return tools
