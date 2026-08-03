"""Reminder scheduler adapter over nanobot CronService."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from nanobot.agent.tools.context import current_request_context
from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.session.keys import UNIFIED_SESSION_KEY

REMINDER_TODOS_REFRESH_JOB_ID = "reminder_todos_refresh"
REMINDER_TODOS_REFRESH_JOB_TYPE = "reminder_todos_refresh"


@dataclass(frozen=True)
class ReminderCronJobView:
    """Compatibility view for legacy reminder code."""

    job: CronJob

    @property
    def id(self) -> str:
        return self.job.id

    @property
    def name(self) -> str:
        return self.job.name

    @property
    def enabled(self) -> bool:
        return self.job.enabled

    @property
    def schedule(self) -> CronSchedule:
        return self.job.schedule

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self.job.payload.origin_metadata or {})

    @property
    def next_run_at_ms(self) -> int | None:
        return self.job.state.next_run_at_ms

    @property
    def last_status(self) -> str | None:
        return self.job.state.last_status

    @property
    def last_result_summary(self) -> str:
        return self.job.state.last_error or ""


class ReminderScheduler:
    """Adapter that gives legacy Reminder code a UserCron-like surface."""

    def __init__(self, cron_service: Any):
        self._cron = cron_service

    @staticmethod
    def _message(name: str, metadata: dict[str, Any], description: str = "") -> str:
        job_type = str(metadata.get("type") or "")
        if job_type == "reminder_one_off":
            marker = "[Reminder One-Off Wakeup]"
        elif job_type == "reminder_scheduled_start":
            marker = "[Reminder Scheduled Start Wakeup]"
        elif job_type == "task_wakeup":
            marker = "[Task Wakeup]"
        else:
            marker = "[Reminder Wakeup]"
        body = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        prefix = description.strip() or name.strip() or "Reminder wakeup"
        return f"{marker}\n{prefix}\nWakeup payload:\n{body}"

    @staticmethod
    def _request_route() -> tuple[str, str, str, dict[str, Any]]:
        ctx = current_request_context()
        if ctx is None:
            return "", "", "", {}
        raw_key = f"{ctx.channel}:{ctx.chat_id}" if ctx.channel and ctx.chat_id else ""
        session_key = (
            raw_key if ctx.session_key == UNIFIED_SESSION_KEY else (ctx.session_key or "")
        )
        return session_key, ctx.channel or "", ctx.chat_id or "", dict(ctx.metadata or {})

    def can_route_to_session(self) -> bool:
        session_key, origin_channel, origin_chat_id, _metadata = self._request_route()
        return bool(session_key and origin_channel and origin_chat_id)

    def add_job(
        self,
        *,
        name: str,
        schedule: CronSchedule,
        description: str = "",
        delete_after_run: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> ReminderCronJobView:
        metadata = metadata or {}
        session_key, origin_channel, origin_chat_id, origin_request_metadata = self._request_route()
        if not session_key or not origin_channel or not origin_chat_id:
            raise RuntimeError("Reminder cron jobs must be created from a live chat session.")
        job = self._cron.add_job(
            name=name,
            schedule=schedule,
            message=self._message(name, metadata, description),
            delete_after_run=delete_after_run,
            session_key=session_key or None,
            origin_channel=origin_channel or None,
            origin_chat_id=origin_chat_id or None,
            origin_metadata={**origin_request_metadata, **metadata},
        )
        return ReminderCronJobView(job)

    def ensure_todos_refresh_job(self, *, interval_seconds: int) -> ReminderCronJobView:
        interval_ms = max(60, int(interval_seconds)) * 1000
        job = CronJob(
            id=REMINDER_TODOS_REFRESH_JOB_ID,
            name="Reminder todos refresh",
            schedule=CronSchedule(kind="every", every_ms=interval_ms),
            payload=CronPayload(
                kind="system_event",
                message="Refresh Reminder App todos from Reminder Service.",
                origin_metadata={
                    "type": REMINDER_TODOS_REFRESH_JOB_TYPE,
                    "interval_seconds": interval_ms // 1000,
                },
            ),
        )
        return ReminderCronJobView(self._cron.register_system_job(job))

    def remove_job(self, job_id: str) -> bool:
        result = self._cron.remove_job(job_id)
        return result == "removed"

    def list_jobs(self, include_disabled: bool = False) -> list[ReminderCronJobView]:
        return [
            ReminderCronJobView(job)
            for job in self._cron.list_jobs(include_disabled=include_disabled)
        ]
