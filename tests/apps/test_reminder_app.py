from __future__ import annotations

import asyncio
import json

from nanobot.agent.tools.context import RequestContext, ToolContext, request_context
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.apps.loader import AppLoader
from nanobot.apps.reminder import legacy
from nanobot.apps.reminder.config import ReminderAppConfig
from nanobot.apps.reminder.scheduler import ReminderScheduler
from nanobot.config.schema import AppsConfig, Config, ToolsConfig
from nanobot.cron.types import CronJob, CronPayload, CronSchedule

REMINDER_TOOL_NAMES = {
    "reminder_app_status",
    "reminder_list_todos",
    "reminder_act",
    "reminder_schedule",
    "reminder_update_task",
}
REMINDER_MAINTENANCE_TOOL_NAMES = {
    "reminder_list_today_tasks",
    "reminder_refresh_today_todos",
    "reminder_start_task",
    "reminder_sync_current_task",
    "reminder_get_resumable_task",
    "reminder_defer_task",
    "reminder_complete_task",
    "reminder_start_ad_hoc_task",
    "reminder_schedule_one_off",
    "reminder_schedule_start_prompt",
    "schedule_task_wakeup",
    "cancel_task_wakeup",
    "list_task_wakeups",
}


def test_reminder_app_config_accepts_device_sn() -> None:
    cfg = Config.model_validate({
        "apps": {
            "reminder": {
                "enabled": True,
                "baseUrl": "http://127.0.0.1:8090",
                "deviceSn": "SN-001",
                "deviceSecret": "local-secret",
            },
        },
    })

    assert cfg.apps.reminder.enabled is True
    assert cfg.apps.reminder.base_url == "http://127.0.0.1:8090"
    assert cfg.apps.reminder.device_sn == "SN-001"
    assert cfg.apps.reminder.device_secret == "local-secret"


def test_reminder_tools_are_not_registered_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda: Config())
    ctx = ToolContext(config=ToolsConfig(), workspace=str(tmp_path))
    registry = ToolRegistry()

    ToolLoader().load(ctx, registry)

    assert REMINDER_TOOL_NAMES.isdisjoint(registry.tool_names)


def test_tool_loader_does_not_register_reminder_app_tools(tmp_path, monkeypatch) -> None:
    cfg = Config(
        apps=AppsConfig(
            reminder=ReminderAppConfig(
                enabled=True,
                baseUrl="http://127.0.0.1:8090",
                deviceSn="SN-001",
            ),
        ),
    )
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda: cfg)
    ctx = ToolContext(config=ToolsConfig(), workspace=str(tmp_path))
    registry = ToolRegistry()

    ToolLoader().load(ctx, registry)

    assert REMINDER_TOOL_NAMES.isdisjoint(registry.tool_names)


def test_app_loader_registers_reminder_tools_when_app_enabled(tmp_path) -> None:
    cfg = Config(
        apps=AppsConfig(
            reminder=ReminderAppConfig(
                enabled=True,
                baseUrl="http://127.0.0.1:8090",
                deviceSn="SN-001",
            ),
        ),
    )
    ctx = ToolContext(config=ToolsConfig(), workspace=str(tmp_path))
    registry = ToolRegistry()

    AppLoader(cfg.apps).load(ctx, registry)

    assert REMINDER_TOOL_NAMES.issubset(set(registry.tool_names))
    assert REMINDER_MAINTENANCE_TOOL_NAMES.isdisjoint(set(registry.tool_names))


def test_app_loader_registers_reminder_refresh_cron_when_service_enabled(tmp_path) -> None:
    cfg = Config(
        apps=AppsConfig(
            reminder=ReminderAppConfig(
                enabled=True,
                baseUrl="http://127.0.0.1:8090",
                deviceSn="SN-001",
                refreshIntervalSeconds=300,
            ),
        ),
    )

    class FakeCron:
        def __init__(self) -> None:
            self.jobs = []

        def register_system_job(self, job):
            self.jobs.append(job)
            return job

    cron = FakeCron()
    ctx = ToolContext(config=ToolsConfig(), workspace=str(tmp_path), cron_service=cron)
    registry = ToolRegistry()

    AppLoader(cfg.apps).load(ctx, registry)

    assert len(cron.jobs) == 1
    job = cron.jobs[0]
    assert job.id == "reminder_todos_refresh"
    assert job.payload.kind == "system_event"
    assert job.payload.origin_metadata["type"] == "reminder_todos_refresh"
    assert job.schedule.kind == "every"
    assert job.schedule.every_ms == 300_000

async def test_reminder_list_todos_refreshes_before_reading(tmp_path, monkeypatch) -> None:
    calls = []

    async def fake_refresh(workspace, config, *, date=None, client=None, user_cron=None, reconcile=True, mode="interactive"):
        calls.append((workspace, config, date, client, user_cron, reconcile, mode))
        legacy.ReminderStateStore(workspace).upsert_todo(date, {
            "local_id": "task_1",
            "source": "scheduled",
            "category": "study",
            "title": "完成语文作业",
            "status": "scheduled",
        })
        return {"ok": True, "data": {"merged_count": 1, "tasks_count": 1}, "error": None}

    monkeypatch.setattr(legacy, "refresh_today_todos_from_service", fake_refresh)
    tool = legacy.ReminderListTodosTool(
        tmp_path,
        legacy.ReminderToolConfigData(
            enabled=True,
            base_url="http://127.0.0.1:8090",
            device_sn="SN-001",
        ),
    )

    result = json.loads(await tool.execute("2026-07-31"))

    assert calls
    assert result["ok"] is True
    assert result["data"]["summary"]["open_count"] == 1
    assert result["data"]["summary"]["open_items"][0]["title"] == "完成语文作业"

async def test_reminder_list_todos_keeps_local_cache_with_sync_error(tmp_path, monkeypatch) -> None:
    async def fake_refresh(workspace, config, *, date=None, client=None, user_cron=None, reconcile=True, mode="interactive"):
        return {
            "ok": False,
            "data": None,
            "error": {"code": "SERVICE_UNAVAILABLE", "message": "Reminder Service is unavailable."},
        }

    monkeypatch.setattr(legacy, "refresh_today_todos_from_service", fake_refresh)
    tool = legacy.ReminderListTodosTool(
        tmp_path,
        legacy.ReminderToolConfigData(
            enabled=True,
            base_url="http://127.0.0.1:8090",
            device_sn="SN-001",
        ),
    )

    result = json.loads(await tool.execute("2026-07-31"))

    assert result["ok"] is True
    assert result["data"]["summary"]["open_count"] == 0
    assert result["data"]["sync_error"]["code"] == "SERVICE_UNAVAILABLE"

async def test_reminder_app_status_provides_loaded_app_context(tmp_path) -> None:
    cfg = Config(
        apps=AppsConfig(
            reminder=ReminderAppConfig(
                enabled=True,
                baseUrl="http://127.0.0.1:8090",
                deviceSn="SN-001",
            ),
        ),
    )
    ctx = ToolContext(config=ToolsConfig(), workspace=str(tmp_path), timezone="Asia/Shanghai")
    registry = ToolRegistry()

    AppLoader(cfg.apps).load(ctx, registry)

    providers = registry.get_runtime_context_providers()
    assert providers
    blocks = [await provider(RequestContext(channel="test", chat_id="chat")) for provider in providers]
    content = "\n".join(str(block.content) for block in blocks if block is not None)
    assert "Loaded App: Reminder App" in content
    assert "App id: reminder" in content
    assert "Current date:" in content
    assert "Current time:" in content
    assert "Timezone: Asia/Shanghai" in content
    assert "Reminder query date for 'today':" in content

def test_reminder_scheduler_binds_jobs_to_current_chat_session() -> None:
    class FakeCron:
        def __init__(self) -> None:
            self.kwargs = {}

        def add_job(self, **kwargs):
            self.kwargs = kwargs
            return CronJob(
                id="job-1",
                name=kwargs["name"],
                schedule=kwargs["schedule"],
                payload=CronPayload(
                    message=kwargs["message"],
                    session_key=kwargs.get("session_key"),
                    origin_channel=kwargs.get("origin_channel"),
                    origin_chat_id=kwargs.get("origin_chat_id"),
                    origin_metadata=kwargs.get("origin_metadata") or {},
                ),
            )

    cron = FakeCron()
    scheduler = ReminderScheduler(cron)
    ctx = RequestContext(
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
        metadata={"request_id": "req-1"},
    )

    with request_context(ctx):
        view = scheduler.add_job(
            name="reminder:吃夜宵",
            schedule=CronSchedule(kind="at", at_ms=1),
            metadata={"type": "reminder_one_off", "title": "吃夜宵"},
        )

    assert cron.kwargs["session_key"] == "websocket:chat-1"
    assert cron.kwargs["origin_channel"] == "websocket"
    assert cron.kwargs["origin_chat_id"] == "chat-1"
    assert cron.kwargs["origin_metadata"]["request_id"] == "req-1"
    assert cron.kwargs["origin_metadata"]["type"] == "reminder_one_off"
    assert view.metadata["title"] == "吃夜宵"

def test_reminder_scheduler_rejects_unbound_jobs() -> None:
    class FakeCron:
        def add_job(self, **kwargs):
            raise AssertionError("unbound add_job must not reach cron service")

    scheduler = ReminderScheduler(FakeCron())

    try:
        scheduler.add_job(
            name="reminder:无会话",
            schedule=CronSchedule(kind="at", at_ms=1),
            metadata={"type": "reminder_one_off", "title": "无会话"},
        )
    except RuntimeError as exc:
        assert "live chat session" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

class FakeCronService:
    def __init__(self, jobs=None) -> None:
        self.jobs = list(jobs or [])
        self.removed = []
        self.added = []

    def add_job(self, **kwargs):
        job = CronJob(
            id=f"new-{len(self.added) + 1}",
            name=kwargs["name"],
            schedule=kwargs["schedule"],
            payload=CronPayload(
                message=kwargs["message"],
                session_key=kwargs.get("session_key"),
                origin_channel=kwargs.get("origin_channel"),
                origin_chat_id=kwargs.get("origin_chat_id"),
                origin_metadata=kwargs.get("origin_metadata") or {},
            ),
            delete_after_run=bool(kwargs.get("delete_after_run")),
        )
        self.jobs.append(job)
        self.added.append(job)
        return job

    def remove_job(self, job_id):
        self.removed.append(job_id)
        self.jobs = [job for job in self.jobs if job.id != job_id]
        return "removed"

    def list_jobs(self, include_disabled=False):
        if include_disabled:
            return list(self.jobs)
        return [job for job in self.jobs if job.enabled]

def test_reminder_reconcile_sync_only_does_not_create_cron(tmp_path) -> None:
    cron = FakeCronService()
    scheduler = ReminderScheduler(cron)
    legacy.ReminderStateStore(tmp_path).upsert_todo("2099-07-31", {
        "local_id": "task_future",
        "source": "scheduled",
        "remote_task_id": "task_future",
        "category": "study",
        "title": "未来任务",
        "content": "未来任务",
        "status": "scheduled",
        "planned_start_at": "2099-07-31T23:00:00+08:00",
        "expected_until": "2099-07-31T23:03:00+08:00",
    })

    result = legacy.reconcile_reminder_todos_jobs(
        tmp_path,
        scheduler,
        date="2099-07-31",
        mode="sync_only",
    )

    assert result["ok"] is True
    assert result["interactive"] is False
    assert cron.added == []

def test_reminder_reconcile_interactive_creates_future_start_cron(tmp_path) -> None:
    cron = FakeCronService()
    scheduler = ReminderScheduler(cron)
    legacy.ReminderStateStore(tmp_path).upsert_todo("2099-07-31", {
        "local_id": "task_future",
        "source": "scheduled",
        "remote_task_id": "task_future",
        "category": "study",
        "title": "未来任务",
        "content": "未来任务",
        "status": "scheduled",
        "planned_start_at": "2099-07-31T23:00:00+08:00",
        "expected_until": "2099-07-31T23:03:00+08:00",
    })
    ctx = RequestContext(
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
    )

    with request_context(ctx):
        result = legacy.reconcile_reminder_todos_jobs(
            tmp_path,
            scheduler,
            date="2099-07-31",
            mode="interactive",
        )

    assert result["ok"] is True
    assert result["interactive"] is True
    assert [job.name for job in cron.added] == ["reminder_start:未来任务"]
    assert cron.added[0].payload.session_key == "websocket:chat-1"

def test_reminder_reconcile_interactive_marks_overdue_without_cron(tmp_path) -> None:
    cron = FakeCronService()
    scheduler = ReminderScheduler(cron)
    legacy.ReminderStateStore(tmp_path).upsert_todo("2026-07-31", {
        "local_id": "task_overdue",
        "source": "scheduled",
        "remote_task_id": "task_overdue",
        "category": "study",
        "title": "过期任务",
        "content": "过期任务",
        "status": "scheduled",
        "planned_start_at": "2000-07-31T23:00:00+08:00",
        "expected_until": "2000-07-31T23:03:00+08:00",
    })
    ctx = RequestContext(
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
    )

    with request_context(ctx):
        result = legacy.reconcile_reminder_todos_jobs(
            tmp_path,
            scheduler,
            date="2026-07-31",
            mode="interactive",
        )

    assert result["ok"] is True
    assert cron.added == []
    actions = result["overdue_intents"]["actions"]
    assert len(actions) == 1
    assert actions[0]["task"]["remote_task_id"] == "task_overdue"
    todo = legacy.ReminderStateStore(tmp_path).find_todo("2026-07-31", remote_task_id="task_overdue")
    assert todo["events"][-1]["type"] == "automation_intent_evaluated"
    assert todo["events"][-1]["intent"] == "llm_decide"

def test_reminder_reconcile_skips_stale_cross_day_current_task(tmp_path) -> None:
    old_job = CronJob(
        id="old-completion",
        name="task_wakeup:completion_check",
        schedule=CronSchedule(kind="at", at_ms=946684800000),
        payload=CronPayload(
            origin_metadata={
                "type": "task_wakeup",
                "kind": "completion_check",
                "task_id": "task_old",
                "session_id": "sess_old",
                "wakeup_id": "wake_old",
                "scheduled_for": "2000-07-31T23:03:00+08:00",
            },
        ),
    )
    cron = FakeCronService([old_job])
    scheduler = ReminderScheduler(cron)
    current = {
        "version": "1.0",
        "task_snapshot": {
            "id": "task_old",
            "uid": "kid_1",
            "source_type": "scheduled",
            "category": "study",
            "title": "旧任务",
            "content": "旧任务",
            "planned_date": "2000-07-31",
            "planned_start_at": "2000-07-31T23:00:00+08:00",
            "planned_end_at": "2000-07-31T23:03:00+08:00",
            "estimated_duration_minutes": 3,
            "status": "active",
        },
        "session": {
            "id": "sess_old",
            "task_id": "task_old",
            "status": "active",
            "started_at": "2000-07-31T23:00:00+08:00",
            "task_ctx": "旧任务",
        },
        "state": {
            "status": "active",
            "started_at": "2000-07-31T23:00:00+08:00",
            "expected_until": "2000-07-31T23:03:00+08:00",
            "todo_local_id": "task_old",
        },
        "events": [],
        "wakeups": [old_job.payload.origin_metadata],
    }
    legacy._write_json(tmp_path / "task-planner" / "current_task.json", current)
    legacy.ReminderStateStore(tmp_path).upsert_todo("2000-07-31", {
        "local_id": "task_old",
        "source": "scheduled",
        "remote_task_id": "task_old",
        "remote_session_id": "sess_old",
        "category": "study",
        "title": "旧任务",
        "content": "旧任务",
        "status": "active",
        "planned_start_at": "2000-07-31T23:00:00+08:00",
        "expected_until": "2000-07-31T23:03:00+08:00",
        "cron_job_ids": ["old-completion"],
    })
    ctx = RequestContext(
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
    )

    with request_context(ctx):
        result = legacy.reconcile_reminder_todos_jobs(
            tmp_path,
            scheduler,
            date="2000-08-01",
            mode="interactive",
        )

    completion = result["completion_check"]
    assert completion["skipped"] == "stale_current_task"
    assert completion["removed"] == ["old-completion"]
    assert completion["created"] == []
    assert completion["actions"] == []
    assert cron.added == []
    updated_current = legacy._read_json(tmp_path / "task-planner" / "current_task.json")
    assert updated_current["wakeups"] == []
    old_todo = legacy.ReminderStateStore(tmp_path).find_todo("2000-07-31", remote_task_id="task_old")
    assert old_todo["cron_job_ids"] == []

def test_reminder_update_task_replaces_completion_check_and_local_plan(tmp_path) -> None:
    old_job = CronJob(
        id="old-1",
        name="task_wakeup:completion_check",
        schedule=CronSchedule(kind="at", at_ms=1785512370000),
        payload=CronPayload(
            origin_metadata={
                "type": "task_wakeup",
                "kind": "completion_check",
                "task_id": "task_1",
                "session_id": "sess_1",
                "wakeup_id": "wake_old",
                "scheduled_for": "2026-07-31T23:39:30+08:00",
            },
        ),
    )
    cron = FakeCronService([old_job])
    scheduler = ReminderScheduler(cron)
    current = {
        "version": "1.0",
        "task_snapshot": {
            "id": "task_1",
            "uid": "kid_1",
            "source_type": "scheduled",
            "category": "study",
            "title": "检查数学练习",
            "content": "检查两道题",
            "planned_date": "2026-07-31",
            "planned_start_at": "2026-07-31T23:37:30+08:00",
            "planned_end_at": "2026-07-31T23:39:30+08:00",
            "estimated_duration_minutes": 2,
            "status": "active",
        },
        "session": {
            "id": "sess_1",
            "task_id": "task_1",
            "status": "active",
            "started_at": "2026-07-31T23:37:30+08:00",
            "task_ctx": "检查两道题",
        },
        "state": {
            "status": "active",
            "started_at": "2026-07-31T23:37:30+08:00",
            "expected_until": "2026-07-31T23:39:30+08:00",
            "todo_local_id": "task_1",
        },
        "events": [],
        "wakeups": [old_job.payload.origin_metadata],
    }
    legacy._write_json(tmp_path / "task-planner" / "current_task.json", current)
    legacy.ReminderStateStore(tmp_path).upsert_todo("2026-07-31", {
        "local_id": "task_1",
        "source": "scheduled",
        "remote_task_id": "task_1",
        "remote_session_id": "sess_1",
        "category": "study",
        "title": "检查数学练习",
        "content": "检查两道题",
        "status": "active",
        "planned_start_at": "2026-07-31T23:37:30+08:00",
        "expected_until": "2026-07-31T23:39:30+08:00",
        "cron_job_ids": ["old-1"],
    })
    tool = legacy.ReminderUpdateTaskTool(tmp_path, scheduler)
    ctx = RequestContext(
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
    )

    with request_context(ctx):
        result = json.loads(asyncio.run(tool.execute(
            task_id="task_1",
            expected_until="2099-07-31T23:42:53+08:00",
            reason="再延长 3 分钟",
        )))

    assert result["ok"] is True
    assert cron.removed == ["old-1"]
    assert result["data"]["cron_job_id"] == "new-1"
    updated_current = legacy._read_json(tmp_path / "task-planner" / "current_task.json")
    assert updated_current["state"]["expected_until"] == "2099-07-31T23:42:53+08:00"
    assert updated_current["wakeups"][0]["wakeup_id"] != "wake_old"
    assert updated_current["events"][0]["type"] == "task_time_extended"
    todo = legacy.ReminderStateStore(tmp_path).find_todo("2026-07-31", remote_task_id="task_1")
    assert todo["expected_until"] == "2099-07-31T23:42:53+08:00"
    assert todo["cron_job_ids"] == ["new-1"]

def test_remote_refresh_preserves_active_local_task_override(tmp_path) -> None:
    store = legacy.ReminderStateStore(tmp_path)
    store.upsert_todo("2026-07-31", {
        "local_id": "task_1",
        "source": "scheduled",
        "remote_task_id": "task_1",
        "category": "study",
        "title": "本地数学检查",
        "content": "本地改过的内容",
        "status": "active",
        "expected_until": "2026-07-31T23:42:53+08:00",
        "events": [{
            "type": "task_time_extended",
            "at": "2026-07-31T23:39:53+08:00",
            "metadata": {"local_override": True},
        }],
    })

    legacy._upsert_scheduled_todo_from_snapshot(tmp_path, {
        "id": "task_1",
        "uid": "kid_1",
        "source_type": "scheduled",
        "category": "study",
        "title": "云端数学检查",
        "content": "云端旧内容",
        "planned_date": "2026-07-31",
        "planned_start_at": "2026-07-31T23:37:30+08:00",
        "planned_end_at": "2026-07-31T23:39:30+08:00",
        "estimated_duration_minutes": 2,
        "status": "active",
    }, sync_status="remote")

    todo = store.find_todo("2026-07-31", remote_task_id="task_1")
    assert todo["title"] == "本地数学检查"
    assert todo["content"] == "本地改过的内容"
    assert todo["expected_until"] == "2026-07-31T23:42:53+08:00"

def test_todo_upsert_does_not_merge_across_sources_with_same_remote_id(tmp_path) -> None:
    store = legacy.ReminderStateStore(tmp_path)
    store.upsert_todo("2026-07-31", {
        "local_id": "task_1",
        "source": "scheduled",
        "remote_task_id": "task_remote",
        "title": "同名任务",
        "status": "scheduled",
    })
    store.upsert_todo("2026-07-31", {
        "local_id": "adhoc_1",
        "source": "ad_hoc",
        "remote_task_id": "task_remote",
        "title": "同名任务",
        "status": "completed",
    })

    payload = store.load_todos("2026-07-31")
    assert [(item["source"], item["local_id"]) for item in payload["items"]] == [
        ("scheduled", "task_1"),
        ("ad_hoc", "adhoc_1"),
    ]

def test_remote_ad_hoc_snapshot_preserves_ad_hoc_source(tmp_path) -> None:
    legacy._upsert_scheduled_todo_from_snapshot(tmp_path, {
        "id": "task_remote_adhoc",
        "uid": "kid_1",
        "source_type": "ad_hoc",
        "category": "study",
        "title": "临时任务",
        "content": "临时任务",
        "planned_date": "2026-07-31",
        "planned_start_at": "2026-07-31T23:00:00+08:00",
        "planned_end_at": "2026-07-31T23:03:00+08:00",
        "status": "done",
    }, status="completed", sync_status="remote")

    todo = legacy.ReminderStateStore(tmp_path).find_todo(
        "2026-07-31",
        remote_task_id="task_remote_adhoc",
        source="ad_hoc",
    )
    assert todo is not None
    assert todo["source"] == "ad_hoc"
    assert legacy.ReminderStateStore(tmp_path).find_todo(
        "2026-07-31",
        remote_task_id="task_remote_adhoc",
        source="scheduled",
    ) is None

async def test_complete_current_is_single_target_and_does_not_continue_to_planned_lookup(tmp_path) -> None:
    current = {
        "version": "1.0",
        "task_snapshot": {
            "id": "task_current",
            "source_type": "scheduled",
            "title": "当前任务",
            "planned_date": "2026-07-31",
        },
        "session": {"id": "sess_current", "task_id": "task_current", "status": "active"},
        "state": {"status": "active", "todo_local_id": "task_current"},
        "events": [],
        "wakeups": [],
    }
    legacy._write_json(tmp_path / "task-planner" / "current_task.json", current)
    tool = legacy.ReminderCompleteTaskTool(legacy.ReminderToolConfigData(), tmp_path)

    async def fake_complete_current(completed_at, reason, *, sync_ad_hoc=True):
        return {"ok": True, "data": {"reminder_id": "task_current", "status": "completed"}, "error": None}

    tool._complete_current = fake_complete_current

    class FailingClient:
        async def request(self, *args, **kwargs):
            raise AssertionError("planned lookup must not run after current completion")

    tool.client = FailingClient()
    result = json.loads(await tool.execute(task_id="task_current", completed_at="2026-07-31T23:03:00+08:00"))

    assert result["ok"] is True
    assert result["data"]["reminder_id"] == "task_current"

async def test_complete_other_task_rejected_when_current_active(tmp_path) -> None:
    current = {
        "version": "1.0",
        "task_snapshot": {"id": "task_current", "source_type": "scheduled", "title": "当前任务"},
        "session": {"id": "sess_current", "task_id": "task_current", "status": "active"},
        "state": {"status": "active", "todo_local_id": "task_current"},
        "events": [],
        "wakeups": [],
    }
    legacy._write_json(tmp_path / "task-planner" / "current_task.json", current)
    tool = legacy.ReminderCompleteTaskTool(legacy.ReminderToolConfigData(), tmp_path)

    result = json.loads(await tool.execute(task_id="task_other", completed_at="2026-07-31T23:03:00+08:00"))

    assert result["ok"] is False
    assert result["error"]["code"] == "CURRENT_REMINDER_ACTIVE"

async def test_reminder_act_complete_uses_single_target_policy(tmp_path) -> None:
    current = {
        "version": "1.0",
        "task_snapshot": {"id": "task_current", "source_type": "scheduled", "title": "当前任务"},
        "session": {"id": "sess_current", "task_id": "task_current", "status": "active"},
        "state": {"status": "active", "todo_local_id": "task_current"},
        "events": [],
        "wakeups": [],
    }
    legacy._write_json(tmp_path / "task-planner" / "current_task.json", current)
    tool = legacy.ReminderActTool(legacy.ReminderToolConfigData(), tmp_path)

    result = json.loads(await tool.execute(
        action="complete",
        task_id="task_other",
        occurred_at="2026-07-31T23:03:00+08:00",
    ))

    assert result["ok"] is False
    assert result["error"]["code"] == "CURRENT_REMINDER_ACTIVE"

async def test_start_ad_hoc_rejects_matching_planned_conflict(tmp_path) -> None:
    legacy.ReminderStateStore(tmp_path).upsert_todo("2026-07-31", {
        "local_id": "task_1",
        "source": "scheduled",
        "remote_task_id": "task_1",
        "title": "同名任务",
        "status": "completed",
        "planned_start_at": "2026-07-31T23:00:00+08:00",
        "completed_at": "2026-07-31T23:02:00+08:00",
    })
    tool = legacy.ReminderStartAdHocTaskTool(tmp_path)

    result = json.loads(await tool.execute(
        title="同名任务",
        started_at="2026-07-31T23:05:00+08:00",
        expected_minutes=3,
    ))

    assert result["ok"] is False
    assert result["error"]["code"] == "PLANNED_REMINDER_CONFLICT"

async def test_reminder_schedule_one_off_wraps_independent_reminder(tmp_path) -> None:
    cron = FakeCronService()
    scheduler = ReminderScheduler(cron)
    tool = legacy.ReminderScheduleTool(tmp_path, scheduler)
    ctx = RequestContext(channel="websocket", chat_id="chat-1", session_key="websocket:chat-1")

    with request_context(ctx):
        result = json.loads(await tool.execute(
            kind="one_off",
            scope="independent",
            title="喝水",
            category="habit",
            after_seconds=60,
        ))

    assert result["ok"] is True
    assert result["data"]["status"] == "scheduled"
    assert cron.added[0].payload.origin_metadata["type"] == "reminder_one_off"

def test_reminder_act_records_wait_until_current_done_intent(tmp_path) -> None:
    current = {
        "version": "1.0",
        "task_snapshot": {"id": "task_current", "source_type": "scheduled", "title": "当前任务"},
        "session": {"id": "sess_current", "task_id": "task_current", "status": "active"},
        "state": {"status": "active", "todo_local_id": "task_current"},
        "events": [],
        "wakeups": [],
    }
    legacy._write_json(tmp_path / "task-planner" / "current_task.json", current)
    legacy.ReminderStateStore(tmp_path).upsert_todo("2026-07-31", {
        "local_id": "task_next",
        "source": "scheduled",
        "remote_task_id": "task_next",
        "title": "下一个任务",
        "status": "pending_start",
    })

    result = legacy._record_wait_until_current_done_intent(
        tmp_path,
        date="2026-07-31",
        task_id="task_next",
    )

    assert result["ok"] is True
    todo = legacy.ReminderStateStore(tmp_path).find_todo("2026-07-31", remote_task_id="task_next", source="scheduled")
    assert todo["events"][-1]["type"] == "intent_deferred"
    assert todo["events"][-1]["intent"] == "wait_until_current_done"

def test_wait_until_current_done_intent_consumes_once_after_completion(tmp_path) -> None:
    cron = FakeCronService()
    scheduler = ReminderScheduler(cron)
    legacy.ReminderStateStore(tmp_path).upsert_todo("2026-07-31", {
        "local_id": "task_next",
        "source": "scheduled",
        "remote_task_id": "task_next",
        "title": "下一个任务",
        "status": "pending_start",
        "events": [{
            "type": "intent_deferred",
            "at": "2026-07-31T23:00:00+08:00",
            "actor": "user",
            "task_id": "task_next",
            "intent": "wait_until_current_done",
            "metadata": {
                "current_task_id": "task_current",
                "target_task_id": "task_next",
            },
        }],
    })
    ctx = RequestContext(channel="websocket", chat_id="chat-1", session_key="websocket:chat-1")

    with request_context(ctx):
        first = legacy._schedule_waiting_start_prompts_after_current_done(
            tmp_path,
            scheduler,
            current_task_id="task_current",
            occurred_at="2026-07-31T23:03:00+08:00",
        )
        second = legacy._schedule_waiting_start_prompts_after_current_done(
            tmp_path,
            scheduler,
            current_task_id="task_current",
            occurred_at="2026-07-31T23:03:30+08:00",
        )

    assert first == ["new-1"]
    assert second == []
    todo = legacy.ReminderStateStore(tmp_path).find_todo("2026-07-31", remote_task_id="task_next", source="scheduled")
    assert todo["events"][-1]["type"] == "intent_consumed"
    assert todo["cron_job_ids"] == ["new-1"]

def test_completion_wakeup_records_prompt_event_and_reconcile_silences_repeat(tmp_path) -> None:
    current = {
        "version": "1.0",
        "task_snapshot": {
            "id": "task_1",
            "source_type": "scheduled",
            "title": "当前任务",
            "planned_date": "2026-07-31",
        },
        "session": {"id": "sess_1", "task_id": "task_1", "status": "active"},
        "state": {
            "status": "active",
            "expected_until": "2026-07-31T23:03:00+08:00",
            "todo_local_id": "task_1",
        },
        "events": [],
        "wakeups": [{
            "type": "task_wakeup",
            "kind": "completion_check",
            "task_id": "task_1",
            "session_id": "sess_1",
            "wakeup_id": "wake_1",
            "scheduled_for": "2026-07-31T23:03:00+08:00",
        }],
    }
    legacy._write_json(tmp_path / "task-planner" / "current_task.json", current)
    legacy.ReminderStateStore(tmp_path).upsert_todo("2026-07-31", {
        "local_id": "task_1",
        "source": "scheduled",
        "remote_task_id": "task_1",
        "remote_session_id": "sess_1",
        "title": "当前任务",
        "status": "active",
        "expected_until": "2026-07-31T23:03:00+08:00",
        "cron_job_ids": ["cron_1"],
    })

    prepared = legacy.prepare_task_wakeup_from_cron(tmp_path, {
        "type": "task_wakeup",
        "kind": "completion_check",
        "task_id": "task_1",
        "session_id": "sess_1",
        "wakeup_id": "wake_1",
        "scheduled_for": "2026-07-31T23:03:00+08:00",
        "_cron_job_id": "cron_1",
    })

    assert prepared["data"]["action"] == "publish"
    updated_current = legacy._read_json(tmp_path / "task-planner" / "current_task.json")
    assert updated_current["wakeups"] == []
    assert updated_current["events"][-1]["type"] == "overdue_prompt_sent"
    todo = legacy.ReminderStateStore(tmp_path).find_todo("2026-07-31", remote_task_id="task_1", source="scheduled")
    assert todo["cron_job_ids"] == []
    assert todo["events"][-1]["type"] == "overdue_prompt_sent"

    cron = FakeCronService()
    scheduler = ReminderScheduler(cron)
    ctx = RequestContext(channel="websocket", chat_id="chat-1", session_key="websocket:chat-1")
    with request_context(ctx):
        result = legacy.reconcile_reminder_todos_jobs(
            tmp_path,
            scheduler,
            date="2026-07-31",
            mode="interactive",
        )
    assert result["completion_check"]["actions"] == []
    assert len(result["completion_check"]["silenced"]) == 1
