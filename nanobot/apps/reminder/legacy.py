"""Reminder tools for Agent ↔ Reminder Service integration."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.cron.types import CronSchedule

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TODO_VERSION = "1.0"
_TODO_SOURCE_SCHEDULED = "scheduled"
_TODO_SOURCE_AD_HOC = "ad_hoc"
_TODO_SOURCE_ONE_OFF = "one_off"
_TODO_SOURCE_PENDING_START = "pending_start"
_TODO_STATUS_SCHEDULED = "scheduled"
_TODO_STATUS_PENDING_START = "pending_start"
_TODO_STATUS_ACTIVE = "active"
_TODO_STATUS_COMPLETED = "completed"
_TODO_STATUS_INTERRUPTED = "interrupted"
_TODO_STATUS_DEFERRED = "deferred"
_TODO_STATUS_CLOSED = "closed"
_CRON_TYPE_TASK_WAKEUP = "task_wakeup"
_CRON_TYPE_ONE_OFF = "reminder_one_off"
_CRON_TYPE_SCHEDULED_START = "reminder_scheduled_start"
_CRON_TYPE_TODOS_REFRESH = "reminder_todos_refresh"
_REFRESH_MODE_SYNC_ONLY = "sync_only"
_REFRESH_MODE_INTERACTIVE = "interactive"
_OVERDUE_PROMPT_COOLDOWN_SECONDS = 15 * 60
_CURRENT_ACTIVE_STATUSES = {"active", "paused"}
_TODO_FINAL_STATUSES = {
    _TODO_STATUS_COMPLETED,
    _TODO_STATUS_INTERRUPTED,
    _TODO_STATUS_DEFERRED,
    _TODO_STATUS_CLOSED,
}
_REMINDER_CATEGORIES = {"study", "reminder", "habit", "other"}


@dataclass(frozen=True)
class ReminderToolConfigData:
    enabled: bool = False
    base_url: str = ""
    timeout_seconds: int = 10
    bearer_token: str = ""
    device_sn: str = ""
    device_secret: str = ""
    verify_ssl: bool = True


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def _success(data: Any) -> str:
    return _json({"ok": True, "data": data, "error": None})


def _failure(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> str:
    return _json({
        "ok": False,
        "data": None,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details or {},
        },
    })


def _extract_uid_from_workspace(workspace: Path) -> str:
    parts = workspace.expanduser().resolve().parts
    for i, part in enumerate(parts):
        if part == "users" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _validate_date(date: str) -> str | None:
    if not _DATE_RE.match(date.strip()):
        return "date must use YYYY-MM-DD format"
    return None


def _parse_iso_ms(value: str) -> int:
    dt = datetime.fromisoformat(value)
    return int(dt.timestamp() * 1000)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _iso_after_seconds(seconds: int) -> str:
    return (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat()


def _planner_dir(workspace: Path) -> Path:
    return workspace / "task-planner"


def _current_task_path(workspace: Path) -> Path:
    return _planner_dir(workspace) / "current_task.json"


def _memory_path(workspace: Path) -> Path:
    return workspace / "memory" / "MEMORY.md"


def _reminder_todos_dir(workspace: Path) -> Path:
    return _planner_dir(workspace) / "reminder_todos"


def _reminder_todos_path(workspace: Path, date: str) -> Path:
    return _reminder_todos_dir(workspace) / f"{date}.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _date_from_iso(value: str, fallback: str = "") -> str:
    value = (value or "").strip()
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().date().isoformat()
        except Exception:
            if _DATE_RE.match(value[:10]):
                return value[:10]
    if fallback and _DATE_RE.match(fallback):
        return fallback
    return datetime.now().astimezone().date().isoformat()


def _normalize_reminder_category(category: str, *, title: str = "", content: str = "") -> str:
    value = category.strip().lower()
    if value in _REMINDER_CATEGORIES:
        return value
    return _infer_reminder_category(title=title, content=content)


def _infer_reminder_category(*, title: str = "", content: str = "") -> str:
    text = f"{title} {content}".lower()
    if any(keyword in text for keyword in (
        "学习", "作业", "功课", "课程", "复习", "预习", "阅读", "读书", "看书",
        "写字", "数学", "英语", "语文", "物理", "化学", "生物", "历史", "地理",
        "政治", "考试", "试卷", "练习", "背单词", "音乐学习",
    )):
        return "study"
    if any(keyword in text for keyword in (
        "习惯", "打卡", "运动", "锻炼", "跑步", "拉伸", "冥想", "早睡", "睡觉",
        "起床", "刷牙", "洗澡", "休息", "喝水",
    )):
        return "habit"
    if any(keyword in text for keyword in (
        "提醒", "叫我", "吃药", "药", "吃饭", "吃个", "饼干", "喝药", "打电话",
        "回消息", "倒垃圾", "取快递", "拿快递",
    )):
        return "reminder"
    return "other"


def _empty_todos(date: str) -> dict[str, Any]:
    return {
        "version": _TODO_VERSION,
        "date": date,
        "updated_at": _now_iso(),
        "items": [],
    }


def _merge_unique(left: list[Any], right: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for item in [*left, *right]:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, dict) else str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _todo_sync(status: str, *, last_sync_at: str = "", error: str = "") -> dict[str, Any]:
    return {
        "status": status,
        "last_sync_at": last_sync_at,
        "error": error,
    }


def _normalize_todo_item(item: dict[str, Any], *, date: str) -> dict[str, Any]:
    now = _now_iso()
    local_id = str(item.get("local_id") or "").strip()
    source = str(item.get("source") or "").strip() or _TODO_SOURCE_AD_HOC
    title = str(item.get("title") or "提醒").strip()
    content = str(item.get("content") or title).strip()
    sync = item.get("sync") if isinstance(item.get("sync"), dict) else {}
    return {
        "local_id": local_id,
        "source": source,
        "remote_task_id": str(item.get("remote_task_id") or "").strip(),
        "remote_session_id": str(item.get("remote_session_id") or "").strip(),
        "category": _normalize_reminder_category(str(item.get("category") or ""), title=title, content=content),
        "title": title,
        "content": content,
        "status": str(item.get("status") or _TODO_STATUS_SCHEDULED).strip(),
        "planned_start_at": str(item.get("planned_start_at") or "").strip(),
        "scheduled_for": str(item.get("scheduled_for") or "").strip(),
        "expected_until": str(item.get("expected_until") or "").strip(),
        "started_at": str(item.get("started_at") or "").strip(),
        "ended_at": str(item.get("ended_at") or "").strip(),
        "completed_at": str(item.get("completed_at") or "").strip(),
        "fired_at": str(item.get("fired_at") or "").strip(),
        "current_task_path": str(item.get("current_task_path") or "").strip(),
        "cron_job_ids": [str(job_id) for job_id in item.get("cron_job_ids") or [] if str(job_id).strip()],
        "events": [event for event in item.get("events") or [] if isinstance(event, dict)],
        "sync": _todo_sync(
            str(sync.get("status") or "local_only"),
            last_sync_at=str(sync.get("last_sync_at") or ""),
            error=str(sync.get("error") or sync.get("sync_error") or ""),
        ),
        "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
        "created_at": str(item.get("created_at") or now),
        "updated_at": str(item.get("updated_at") or now),
        "date": str(item.get("date") or date),
    }


class ReminderStateStore:
    """Deterministic local state store for reminder todos and current state."""

    def __init__(self, workspace: Path):
        self.workspace = workspace

    def todos_path(self, date: str) -> Path:
        return _reminder_todos_path(self.workspace, date)

    def load_todos(self, date: str) -> dict[str, Any]:
        payload = _read_json(self.todos_path(date))
        if not payload:
            return _empty_todos(date)
        items = payload.get("items")
        if not isinstance(items, list):
            items = []
        return {
            "version": str(payload.get("version") or _TODO_VERSION),
            "date": str(payload.get("date") or date),
            "updated_at": str(payload.get("updated_at") or _now_iso()),
            "items": [
                _normalize_todo_item(item, date=date)
                for item in items
                if isinstance(item, dict)
            ],
        }

    def save_todos(self, date: str, payload: dict[str, Any]) -> None:
        payload["version"] = str(payload.get("version") or _TODO_VERSION)
        payload["date"] = date
        payload["updated_at"] = _now_iso()
        _write_json(self.todos_path(date), payload)

    @staticmethod
    def _matches(
        item: dict[str, Any],
        *,
        local_id: str = "",
        remote_task_id: str = "",
        title: str = "",
        source: str = "",
    ) -> bool:
        if source and str(item.get("source") or "") != source:
            return False
        if local_id and str(item.get("local_id") or "") == local_id:
            return True
        if remote_task_id and str(item.get("remote_task_id") or "") == remote_task_id:
            return True
        if title and source and str(item.get("source") or "") == source and _title_matches(title, str(item.get("title") or "")):
            return True
        return False

    def find_todo(
        self,
        date: str,
        *,
        local_id: str = "",
        remote_task_id: str = "",
        title: str = "",
        source: str = "",
    ) -> dict[str, Any] | None:
        payload = self.load_todos(date)
        for item in payload["items"]:
            if self._matches(item, local_id=local_id, remote_task_id=remote_task_id, title=title, source=source):
                return item
        return None

    def upsert_todo(self, date: str, item: dict[str, Any]) -> dict[str, Any]:
        payload = self.load_todos(date)
        normalized = _normalize_todo_item(item, date=date)
        now = _now_iso()
        for idx, existing in enumerate(payload["items"]):
            if not self._matches(
                existing,
                local_id=str(normalized.get("local_id") or ""),
                remote_task_id=str(normalized.get("remote_task_id") or ""),
                title=str(normalized.get("title") or ""),
                source=str(normalized.get("source") or ""),
            ):
                continue
            merged = {**existing, **{k: v for k, v in normalized.items() if v not in ("", [], None)}}
            merged["cron_job_ids"] = _merge_unique(existing.get("cron_job_ids") or [], normalized.get("cron_job_ids") or [])
            merged["events"] = _merge_unique(existing.get("events") or [], normalized.get("events") or [])
            merged["created_at"] = existing.get("created_at") or normalized.get("created_at") or now
            merged["updated_at"] = now
            payload["items"][idx] = _normalize_todo_item(merged, date=date)
            self.save_todos(date, payload)
            return payload["items"][idx]

        normalized["updated_at"] = now
        payload["items"].append(normalized)
        self.save_todos(date, payload)
        return normalized

    def update_todo(
        self,
        date: str,
        *,
        local_id: str = "",
        remote_task_id: str = "",
        title: str = "",
        source: str = "",
        updates: dict[str, Any] | None = None,
        event: dict[str, Any] | None = None,
        create_item: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        payload = self.load_todos(date)
        for idx, existing in enumerate(payload["items"]):
            if not self._matches(existing, local_id=local_id, remote_task_id=remote_task_id, title=title, source=source):
                continue
            merged = {**existing, **(updates or {})}
            if event:
                merged["events"] = _merge_unique(existing.get("events") or [], [event])
            merged["updated_at"] = _now_iso()
            payload["items"][idx] = _normalize_todo_item(merged, date=date)
            self.save_todos(date, payload)
            return payload["items"][idx]

        if create_item:
            item = {**create_item, **(updates or {})}
            if event:
                item["events"] = [*(item.get("events") or []), event]
            return self.upsert_todo(date, item)
        return None


def _current_todo_local_id(payload: dict[str, Any]) -> str:
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    snapshot = payload.get("task_snapshot") if isinstance(payload.get("task_snapshot"), dict) else {}
    task_id, _session_id = _current_ids(payload)
    return str(state.get("todo_local_id") or snapshot.get("local_id") or task_id or "").strip()


def _current_todo_date(payload: dict[str, Any], fallback_iso: str = "") -> str:
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    snapshot = payload.get("task_snapshot") if isinstance(payload.get("task_snapshot"), dict) else {}
    for value in (
        str(state.get("started_at") or ""),
        str(session.get("started_at") or ""),
        str(snapshot.get("planned_date") or ""),
        fallback_iso,
    ):
        if value.strip():
            return _date_from_iso(value)
    return _date_from_iso("")


def _current_todo_source(payload: dict[str, Any]) -> str:
    snapshot = payload.get("task_snapshot") if isinstance(payload.get("task_snapshot"), dict) else {}
    source_type = str(snapshot.get("source_type") or "").strip().lower()
    if source_type in {"scheduled", "plan", "planned"}:
        return _TODO_SOURCE_SCHEDULED
    if source_type == _TODO_SOURCE_PENDING_START:
        return _TODO_SOURCE_PENDING_START
    return _TODO_SOURCE_AD_HOC


def _current_todo_create_item(payload: dict[str, Any], *, ended_at: str = "") -> dict[str, Any]:
    task_id, session_id = _current_ids(payload)
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    snapshot = payload.get("task_snapshot") if isinstance(payload.get("task_snapshot"), dict) else {}
    local_id = _current_todo_local_id(payload) or task_id
    source = _current_todo_source(payload)
    return {
        "local_id": local_id,
        "source": source,
        "remote_task_id": task_id if source == _TODO_SOURCE_SCHEDULED else "",
        "remote_session_id": session_id if source == _TODO_SOURCE_SCHEDULED else "",
        "category": _normalize_reminder_category(
            str(snapshot.get("category") or ""),
            title=str(snapshot.get("title") or ""),
            content=str(session.get("task_ctx") or snapshot.get("content") or ""),
        ),
        "title": str(snapshot.get("title") or "当前提醒"),
        "content": str(session.get("task_ctx") or snapshot.get("content") or snapshot.get("title") or "当前提醒"),
        "status": _current_status(payload) or _TODO_STATUS_ACTIVE,
        "planned_start_at": str(snapshot.get("planned_start_at") or session.get("started_at") or ""),
        "expected_until": str(state.get("expected_until") or snapshot.get("planned_end_at") or ""),
        "started_at": str(state.get("started_at") or session.get("started_at") or ""),
        "ended_at": ended_at,
        "current_task_path": str(_current_task_path(Path("."))),
        "cron_job_ids": [],
        "sync": payload.get("sync") if isinstance(payload.get("sync"), dict) else _todo_sync("local_only"),
    }


def _update_current_todo(
    workspace: Path,
    payload: dict[str, Any],
    *,
    status: str,
    occurred_at: str,
    result: str,
    sync_status: str,
    sync_error: str = "",
    remote_task_id: str = "",
    remote_session_id: str = "",
    cron_job_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    store = ReminderStateStore(workspace)
    date = _current_todo_date(payload, occurred_at)
    task_id, session_id = _current_ids(payload)
    local_id = _current_todo_local_id(payload) or task_id
    source = _current_todo_source(payload)
    title = _current_title(payload)
    create_item = _current_todo_create_item(payload, ended_at=occurred_at)
    create_item["current_task_path"] = str(_current_task_path(workspace))
    updates: dict[str, Any] = {
        "status": status,
        "ended_at": occurred_at,
        "current_task_path": str(_current_task_path(workspace)),
        "sync": _todo_sync(sync_status, last_sync_at=_now_iso() if sync_status == "synced" else "", error=sync_error),
    }
    if status == _TODO_STATUS_COMPLETED:
        updates["completed_at"] = occurred_at
    if remote_task_id:
        updates["remote_task_id"] = remote_task_id
    elif source == _TODO_SOURCE_SCHEDULED and task_id:
        updates["remote_task_id"] = task_id
    if remote_session_id:
        updates["remote_session_id"] = remote_session_id
    elif source == _TODO_SOURCE_SCHEDULED and session_id:
        updates["remote_session_id"] = session_id
    if cron_job_ids is not None:
        updates["cron_job_ids"] = cron_job_ids
    event_type = "completed" if status == _TODO_STATUS_COMPLETED else "ended"
    return store.update_todo(
        date,
        local_id=local_id,
        remote_task_id=remote_task_id or (task_id if source == _TODO_SOURCE_SCHEDULED else ""),
        title=title,
        source=source,
        updates=updates,
        event={"type": event_type, "at": occurred_at, "reason": result},
        create_item=create_item,
    )


def _is_current_task_payload(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("task_snapshot"), dict)
        and isinstance(payload.get("session"), dict)
        and isinstance(payload.get("events"), list)
    )


def _task_id_from_payload(payload: dict[str, Any]) -> str:
    snapshot = payload.get("task_snapshot") if isinstance(payload, dict) else None
    if isinstance(snapshot, dict) and snapshot.get("id"):
        return str(snapshot["id"]).strip()
    session = payload.get("session") if isinstance(payload, dict) else None
    if isinstance(session, dict) and session.get("task_id"):
        return str(session["task_id"]).strip()
    return ""


def _load_current_task_payload(workspace: Path, task_id: str, payload: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Return a full local current_task payload, falling back to disk when needed."""
    candidate = payload if _is_current_task_payload(payload) else None
    current_path = _current_task_path(workspace)
    disk_payload = _read_json(current_path)

    if candidate is None and _is_current_task_payload(disk_payload):
        candidate = disk_payload

    if candidate is None:
        return None, f"payload must be the full current_task.json object; expected path: {current_path}"

    payload_task_id = _task_id_from_payload(candidate)
    if task_id and payload_task_id and payload_task_id != task_id:
        return None, f"payload task id {payload_task_id} does not match requested task id {task_id}"
    if task_id and not payload_task_id:
        return None, "payload.task_snapshot.id or payload.session.task_id is required"
    return candidate, None


def _event_occurred_at(event: dict[str, Any]) -> str:
    value = event.get("occurred_at") or event.get("at") or event.get("timestamp")
    return str(value).strip() if value else _now_iso()


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "type": str(event.get("type") or "").strip(),
        "occurred_at": _event_occurred_at(event),
    }
    reason = event.get("reason") or event.get("notes") or event.get("note")
    if reason:
        normalized["reason"] = str(reason)
    if event.get("message_id"):
        normalized["message_id"] = str(event["message_id"])
    if event.get("note") or event.get("notes"):
        normalized["note"] = str(event.get("note") or event.get("notes"))
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        normalized["metadata"] = metadata
    return normalized


def _state_end_result(payload: dict[str, Any]) -> str:
    state = payload.get("state")
    if not isinstance(state, dict):
        return ""
    status = str(state.get("status") or "").strip().lower()
    if status in {"completed", "complete", "done"}:
        return "completed"
    if status in {"deferred", "deferred_to_tomorrow"}:
        return "deferred"
    if status in {"closed", "cancelled", "canceled"}:
        return "closed"
    return ""


def _normalize_current_task_payload(
    payload: dict[str, Any],
    *,
    task_id: str = "",
    end_result: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Convert local current_task.json shape to Reminder Service payload shape."""
    snapshot = payload.get("task_snapshot")
    session = payload.get("session")
    events = payload.get("events")
    if not isinstance(snapshot, dict) or not isinstance(session, dict) or not isinstance(events, list):
        raise ValueError("payload must include task_snapshot, session, and events")

    effective_task_id = task_id or _task_id_from_payload(payload)
    status = str(session.get("session_status") or session.get("status") or "").strip()
    session_status = status if status in {"running", "ended", "interrupted"} else ""
    inferred_end = end_result or _state_end_result(payload)
    if inferred_end:
        session_status = "ended"

    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    started_at = str(session.get("started_at") or state.get("started_at") or "").strip()
    ended_at = str(session.get("ended_at") or "").strip()
    if inferred_end and not ended_at:
        ended_at = _now_iso()
    session_date = str(session.get("session_date") or snapshot.get("planned_date") or started_at[:10] or "").strip()

    normalized_events = [
        _normalize_event(event)
        for event in events
        if isinstance(event, dict) and str(event.get("type") or "").strip()
    ]
    if inferred_end and not any(event.get("type") == "task_ended" for event in normalized_events):
        normalized_events.append({
            "type": "task_ended",
            "occurred_at": ended_at or _now_iso(),
            "reason": reason or inferred_end,
            "metadata": {"result": inferred_end},
        })

    normalized_messages: list[dict[str, Any]] = []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        normalized_messages.append({
            key: message[key]
            for key in ("id", "role", "content", "intent", "confidence", "triggered_event_type", "created_at", "metadata")
            if key in message
        })

    return {
        "version": str(payload.get("version") or "1.0"),
        "task_snapshot": snapshot,
        "session": {
            "id": str(session.get("id") or "").strip(),
            "task_id": str(session.get("task_id") or effective_task_id).strip(),
            "uid": str(session.get("uid") or "").strip(),
            "session_date": session_date,
            "session_status": session_status or "running",
            "end_result": inferred_end,
            "resumable": bool(session.get("resumable", not inferred_end)),
            "task_ctx": str(session.get("task_ctx") or "").strip(),
            "started_at": started_at,
            "ended_at": ended_at,
        },
        "events": normalized_events,
        "messages": normalized_messages,
        "sync": payload.get("sync") if isinstance(payload.get("sync"), dict) else {},
    }


def _replace_memory_section(workspace: Path, lines: list[str]) -> None:
    path = _memory_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    block = "\n".join(["## User Reminder State", *lines]).rstrip() + "\n"

    marker = "## User Reminder State"
    start = text.find(marker)
    if start < 0:
        prefix = text.rstrip()
        new_text = (prefix + "\n\n" if prefix else "") + block
        path.write_text(new_text, encoding="utf-8")
        return

    next_start = text.find("\n## ", start + len(marker))
    if next_start < 0:
        new_text = text[:start].rstrip() + "\n\n" + block
    else:
        new_text = text[:start].rstrip() + "\n\n" + block + text[next_start:]
    path.write_text(new_text.lstrip(), encoding="utf-8")


def _write_memory_active(
    workspace: Path,
    *,
    status: str,
    reminder_id: str,
    session_id: str,
    title: str,
    started_at: str = "",
    expected_until: str = "",
    wakeups: list[dict[str, Any]] | None = None,
    guidance: str = "",
) -> None:
    lines = [
        f"- status: {status}",
        f"- reminder_id: {reminder_id}",
        f"- reminder_session_id: {session_id}",
        f"- reminder_title: {title}",
    ]
    if started_at:
        lines.append(f"- started_at: {started_at}")
    if expected_until:
        lines.append(f"- expected_until: {expected_until}")
    lines.append("- wakeups:")
    for wakeup in wakeups or []:
        lines.append(
            f"  - {wakeup.get('wakeup_id', '')} {wakeup.get('kind', '')} at {wakeup.get('scheduled_for', '')}"
        )
    if guidance:
        lines.append(f"- guidance: {guidance}")
    _replace_memory_section(workspace, lines)


def _current_reminder(workspace: Path) -> dict[str, Any] | None:
    return _read_json(_current_task_path(workspace))


def _current_status(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    state = payload.get("state")
    return state.get("status", "") if isinstance(state, dict) else ""


def _current_ids(payload: dict[str, Any]) -> tuple[str, str]:
    snapshot = payload.get("task_snapshot") if isinstance(payload.get("task_snapshot"), dict) else {}
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    return str(snapshot.get("id") or ""), str(session.get("id") or "")


def _current_title(payload: dict[str, Any]) -> str:
    snapshot = payload.get("task_snapshot") if isinstance(payload.get("task_snapshot"), dict) else {}
    return str(snapshot.get("title") or "当前提醒").strip()


def _current_expected_until(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    expected_until = str(state.get("expected_until") or "").strip()
    if expected_until:
        return expected_until
    snapshot = payload.get("task_snapshot") if isinstance(payload.get("task_snapshot"), dict) else {}
    return str(snapshot.get("planned_end_at") or "").strip()


def _is_current_overdue(payload: dict[str, Any] | None, now_iso: str) -> bool:
    expected_until = _current_expected_until(payload)
    if not expected_until:
        return False
    try:
        return _parse_iso_ms(expected_until) <= _parse_iso_ms(now_iso)
    except Exception:
        return False


def _title_matches(left: str, right: str) -> bool:
    left = left.strip().lower()
    right = right.strip().lower()
    return bool(left and right and (left in right or right in left))


def _reminder_title_key(value: str) -> str:
    value = value.strip().lower()
    for token in (
        "提醒",
        "计时",
        "开始",
        "继续",
        "休息结束",
        "该",
        "做",
        "了",
        "的",
        "任务",
    ):
        value = value.replace(token, "")
    return re.sub(r"[\s\W_]+", "", value)


def _reminder_titles_related(left: str, right: str) -> bool:
    if _title_matches(left, right):
        return True
    left_key = _reminder_title_key(left)
    right_key = _reminder_title_key(right)
    if not left_key or not right_key:
        return False
    return left_key in right_key or right_key in left_key


def _close_related_pending_todos(
    workspace: Path,
    user_cron: Any,
    *,
    title: str,
    occurred_at: str,
    reason: str,
) -> list[str]:
    """Close unused local helper reminders related to a completed reminder."""
    date = _date_from_iso(occurred_at)
    store = ReminderStateStore(workspace)
    payload = store.load_todos(date)
    removed: list[str] = []
    closed_ids: list[str] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "")
        status = str(item.get("status") or "")
        if source not in {_TODO_SOURCE_ONE_OFF, _TODO_SOURCE_PENDING_START}:
            continue
        if status not in {_TODO_STATUS_SCHEDULED, _TODO_STATUS_PENDING_START, _TODO_STATUS_ACTIVE}:
            continue
        item_title = str(item.get("title") or "")
        item_content = str(item.get("content") or "")
        if not (
            _reminder_titles_related(title, item_title)
            or _reminder_titles_related(title, item_content)
        ):
            continue
        for job_id in [str(job_id) for job_id in item.get("cron_job_ids") or [] if str(job_id).strip()]:
            try:
                if user_cron and user_cron.remove_job(job_id):
                    removed.append(job_id)
            except Exception:
                logger.debug("Failed to remove related reminder cron job {}", job_id)
        item["status"] = _TODO_STATUS_CLOSED
        item["ended_at"] = occurred_at
        item["cron_job_ids"] = []
        item["updated_at"] = _now_iso()
        events = item.setdefault("events", [])
        if isinstance(events, list):
            events.append({
                "type": "closed",
                "at": occurred_at,
                "reason": reason or "related reminder already completed",
                "metadata": {"closed_by": "related_completion", "completed_title": title},
            })
        closed_ids.append(str(item.get("local_id") or ""))
    if closed_ids:
        store.save_todos(date, payload)
    return removed


def _close_todo_by_cron_job_id(
    workspace: Path,
    *,
    cron_job_id: str,
    occurred_at: str,
    reason: str,
) -> dict[str, Any] | None:
    cron_job_id = cron_job_id.strip()
    if not cron_job_id:
        return None
    todos_dir = _reminder_todos_dir(workspace)
    if not todos_dir.exists():
        return None
    store = ReminderStateStore(workspace)
    for path in sorted(todos_dir.glob("*.json")):
        date = path.stem
        if _validate_date(date):
            continue
        payload = store.load_todos(date)
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            cron_ids = [str(job_id) for job_id in item.get("cron_job_ids") or [] if str(job_id).strip()]
            if cron_job_id not in cron_ids:
                continue
            status = str(item.get("status") or "")
            source = str(item.get("source") or "")
            if source in {_TODO_SOURCE_ONE_OFF, _TODO_SOURCE_PENDING_START} and status in {
                _TODO_STATUS_SCHEDULED,
                _TODO_STATUS_PENDING_START,
                _TODO_STATUS_ACTIVE,
            }:
                item["status"] = _TODO_STATUS_CLOSED
                item["ended_at"] = occurred_at
                events = item.setdefault("events", [])
                if isinstance(events, list):
                    events.append({
                        "type": "closed",
                        "at": occurred_at,
                        "reason": reason,
                        "metadata": {"closed_by": "cron_cancel", "cron_job_id": cron_job_id},
                    })
            _sync_item_cron_job_ids(item, remove_ids=[cron_job_id])
            store.save_todos(date, payload)
            return {
                "date": date,
                "local_id": str(item.get("local_id") or ""),
                "source": source,
                "status": str(item.get("status") or ""),
            }
    return None


def _is_ad_hoc_current_task(payload: dict[str, Any]) -> bool:
    task_id, _ = _current_ids(payload)
    snapshot = payload.get("task_snapshot") if isinstance(payload.get("task_snapshot"), dict) else {}
    source_type = str(snapshot.get("source_type") or "").strip().lower()
    return task_id.startswith("adhoc_") or source_type in {"ad_hoc", "adhoc"}


def _find_pending_scheduled_todo_by_title(
    workspace: Path,
    *,
    title: str,
    now_iso: str,
) -> dict[str, Any] | None:
    """Find a same-day planned reminder that is waiting for start confirmation."""
    conflict = _find_scheduled_todo_conflict_by_title(
        workspace,
        title=title,
        now_iso=now_iso,
        include_recent_final=False,
    )
    if not conflict:
        return None
    status = str(conflict.get("status") or "")
    return conflict if status in {_TODO_STATUS_PENDING_START, _TODO_STATUS_SCHEDULED} else None

def _find_scheduled_todo_conflict_by_title(
    workspace: Path,
    *,
    title: str,
    now_iso: str,
    include_recent_final: bool = True,
) -> dict[str, Any] | None:
    """Find a same-day planned reminder that should block ad-hoc duplication."""
    if not title.strip():
        return None
    date = _date_from_iso(now_iso)
    now_ms = _parse_iso_ms(now_iso)
    payload = ReminderStateStore(workspace).load_todos(date)
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("source") or "") != _TODO_SOURCE_SCHEDULED:
            continue
        if not _title_matches(title, str(item.get("title") or "")):
            continue
        status = str(item.get("status") or "")
        if status not in _TODO_FINAL_STATUSES:
            return item
        if not include_recent_final:
            continue
        planned_start_at = str(item.get("planned_start_at") or item.get("scheduled_for") or "").strip()
        completed_at = str(item.get("completed_at") or item.get("ended_at") or "").strip()
        reference_at = completed_at or planned_start_at
        if not reference_at:
            return item
        try:
            reference_ms = _parse_iso_ms(reference_at)
        except Exception:
            continue
        if abs(now_ms - reference_ms) <= 2 * 60 * 60 * 1000:
            return item
    return None


def _prepare_ad_hoc_sync_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sync_payload = json.loads(json.dumps(payload, ensure_ascii=False))
    snapshot = sync_payload.setdefault("task_snapshot", {})
    session = sync_payload.setdefault("session", {})
    state = sync_payload.get("state") if isinstance(sync_payload.get("state"), dict) else {}

    title = str(snapshot.get("title") or "临时提醒").strip()
    notes = str(session.get("task_ctx") or "").strip()
    started_at = str(session.get("started_at") or state.get("started_at") or "").strip()
    expected_until = str(state.get("expected_until") or "").strip()
    reminder_config = snapshot.get("reminder_config") if isinstance(snapshot.get("reminder_config"), dict) else {}
    expected_minutes = reminder_config.get("expected_minutes")

    snapshot["id"] = ""
    snapshot.setdefault("title", title)
    snapshot.setdefault("content", notes or title)
    snapshot.setdefault("source_type", "ad_hoc")
    snapshot["category"] = _normalize_reminder_category(
        str(snapshot.get("category") or ""),
        title=title,
        content=notes or title,
    )
    snapshot.setdefault("created_by", "agent")
    if started_at:
        snapshot.setdefault("planned_date", started_at[:10])
        snapshot.setdefault("planned_start_at", started_at)
    if expected_until:
        snapshot.setdefault("planned_end_at", expected_until)
    if expected_minutes and not snapshot.get("estimated_duration_minutes"):
        snapshot["estimated_duration_minutes"] = expected_minutes

    session["task_id"] = ""
    return sync_payload


def _append_task_ended(
    payload: dict[str, Any],
    *,
    result: str,
    occurred_at: str,
    reason: str,
) -> dict[str, Any]:
    events = payload.setdefault("events", [])
    if not isinstance(events, list):
        payload["events"] = events = []
    if not any(isinstance(event, dict) and event.get("type") == "task_ended" for event in events):
        events.append({
            "type": "task_ended",
            "at": occurred_at,
            "reason": reason or result,
            "metadata": {"result": result},
        })

    state = payload.setdefault("state", {})
    if isinstance(state, dict):
        state["status"] = "completed" if result == "completed" else result
        if result == "completed":
            state["completed_at"] = occurred_at
            state.pop("ended_at", None)
        else:
            state["ended_at"] = occurred_at
            state.pop("completed_at", None)
        state["last_activity_at"] = occurred_at

    session = payload.setdefault("session", {})
    if isinstance(session, dict):
        session["status"] = "ended"
        session["session_status"] = "ended"
        session["end_result"] = result
        session["resumable"] = False
        session["ended_at"] = occurred_at

    sync = payload.setdefault("sync", {})
    if isinstance(sync, dict):
        sync["last_sync_at"] = sync.get("last_sync_at")
        sync["sync_error"] = None
    return payload


def _write_memory_none(
    workspace: Path,
    *,
    reminder_id: str,
    session_id: str,
    result: str,
    updated_at: str,
) -> None:
    _replace_memory_section(
        workspace,
        [
            "- status: none",
            f"- last_reminder_id: {reminder_id}",
            f"- last_reminder_session_id: {session_id}",
            f"- last_result: {result}",
            f"- updated_at: {updated_at}",
        ],
    )


def _write_memory_pending_sync(
    workspace: Path,
    *,
    reminder_id: str,
    session_id: str,
    title: str,
    sync_error: str,
) -> None:
    _replace_memory_section(
        workspace,
        [
            "- status: pending_sync",
            f"- reminder_id: {reminder_id}",
            f"- reminder_session_id: {session_id}",
            f"- reminder_title: {title}",
            "- pending_reason: sync_failed",
            f"- sync_error: {sync_error}",
            "- guidance: 不要丢失 current_task.json；后续应优先重试同步或确认用户是否继续。",
        ],
    )


def _cancel_current_wakeups(user_cron: Any, payload: dict[str, Any]) -> list[str]:
    if not user_cron:
        return []
    task_id, session_id = _current_ids(payload)
    wakeup_ids = {
        str(wakeup.get("wakeup_id") or "")
        for wakeup in payload.get("wakeups") or []
        if isinstance(wakeup, dict) and wakeup.get("wakeup_id")
    }
    removed: list[str] = []
    try:
        jobs = user_cron.list_jobs(include_disabled=True)
    except Exception:
        return removed
    for job in jobs:
        metadata = getattr(job, "metadata", {}) or {}
        if metadata.get("type") != _CRON_TYPE_TASK_WAKEUP:
            continue
        if task_id and metadata.get("task_id") != task_id:
            continue
        if session_id and metadata.get("session_id") != session_id:
            continue
        if wakeup_ids and metadata.get("wakeup_id") not in wakeup_ids:
            continue
        try:
            if user_cron.remove_job(job.id):
                removed.append(job.id)
        except Exception:
            logger.debug("Failed to cancel reminder wakeup {}", getattr(job, "id", ""))
    return removed


def _finish_current_reminder_locally(
    workspace: Path,
    user_cron: Any,
    payload: dict[str, Any],
    *,
    ended_at: str,
    reason: str,
    result: str = "closed",
) -> dict[str, Any]:
    task_id, session_id = _current_ids(payload)
    removed_wakeups = _cancel_current_wakeups(user_cron, payload)
    _append_task_ended(
        payload,
        result=result,
        occurred_at=ended_at,
        reason=reason,
    )
    sync = payload.setdefault("sync", {})
    if isinstance(sync, dict):
        sync["status"] = "local_ended"
        sync["sync_error"] = None
    _write_json(_current_task_path(workspace), payload)
    todo_status = {
        "completed": _TODO_STATUS_COMPLETED,
        "interrupted": _TODO_STATUS_INTERRUPTED,
        "deferred": _TODO_STATUS_DEFERRED,
    }.get(result, _TODO_STATUS_CLOSED)
    _update_current_todo(
        workspace,
        payload,
        status=todo_status,
        occurred_at=ended_at,
        result=result,
        sync_status="local_ended",
        cron_job_ids=[],
    )
    _write_memory_none(
        workspace,
        reminder_id=task_id,
        session_id=session_id,
        result=result,
        updated_at=ended_at,
    )
    return {
        "reminder_id": task_id,
        "reminder_session_id": session_id,
        "ended_at": ended_at,
        "reason": reason,
        "result": result,
        "cancelled_cron_job_ids": removed_wakeups,
    }


async def _complete_current_reminder(
    *,
    client: ReminderClient,
    workspace: Path,
    user_cron: Any = None,
    completed_at: str,
    reason: str,
    sync_ad_hoc: bool = True,
    service_enabled: bool = False,
) -> dict[str, Any]:
    current = _current_reminder(workspace)
    if not _is_current_task_payload(current):
        return json.loads(_failure(
            "NO_ACTIVE_REMINDER",
            f"No current reminder state found at {_current_task_path(workspace)}.",
        ))
    status = _current_status(current)
    if status not in {"active", "paused", "pending_sync"}:
        return json.loads(_failure(
            "NO_ACTIVE_REMINDER",
            "No active reminder to complete.",
            details={"status": status or "none"},
        ))

    completed_at = completed_at.strip() or _now_iso()
    try:
        _parse_iso_ms(completed_at)
    except ValueError as exc:
        return json.loads(_failure("INVALID_ARGUMENT", f"completed_at must be ISO datetime with timezone: {exc}"))

    task_id, session_id = _current_ids(current)
    title = _current_title(current)
    removed_wakeups = _cancel_current_wakeups(user_cron, current)
    related_closed_wakeups = _close_related_pending_todos(
        workspace,
        user_cron,
        title=title,
        occurred_at=completed_at,
        reason="related reminder already completed",
    )
    all_removed_wakeups = list(dict.fromkeys([*removed_wakeups, *related_closed_wakeups]))
    _append_task_ended(
        current,
        result="completed",
        occurred_at=completed_at,
        reason=reason or "用户确认完成",
    )

    if _is_ad_hoc_current_task(current):
        if sync_ad_hoc and service_enabled:
            sync_payload = _prepare_ad_hoc_sync_payload(current)
            try:
                normalized = _normalize_current_task_payload(
                    sync_payload,
                    end_result="completed",
                    reason=reason or "用户确认完成",
                )
            except ValueError as exc:
                return json.loads(_failure("INVALID_ARGUMENT", str(exc)))

            result = await client.request(
                "POST",
                "/api/v1/reminder/tasks/sync",
                body={"payload": normalized},
            )
            try:
                parsed = json.loads(result)
            except Exception:
                parsed = {}

            if parsed.get("ok") is True:
                data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
                server_task = data.get("task") if isinstance(data.get("task"), dict) else {}
                server_task_id = str(data.get("task_id") or server_task.get("id") or "").strip()
                server_session_id = str(data.get("session_id") or session_id).strip()
                if server_task:
                    current["task_snapshot"] = server_task
                elif server_task_id:
                    current.setdefault("task_snapshot", {})["id"] = server_task_id
                if server_task_id:
                    current.setdefault("session", {})["task_id"] = server_task_id
                if server_session_id:
                    current.setdefault("session", {})["id"] = server_session_id
                sync = current.setdefault("sync", {})
                if isinstance(sync, dict):
                    sync["status"] = "synced"
                    sync["last_sync_at"] = str(data.get("synced_at") or _now_iso())
                    sync["sync_error"] = None
                    sync["local_reminder_id"] = task_id
                _update_current_todo(
                    workspace,
                    current,
                    status=_TODO_STATUS_COMPLETED,
                    occurred_at=completed_at,
                    result="completed",
                    sync_status="synced",
                    remote_task_id=server_task_id,
                    remote_session_id=server_session_id,
                    cron_job_ids=[],
                )
                _write_json(_current_task_path(workspace), current)
                _write_memory_none(
                    workspace,
                    reminder_id=server_task_id or task_id,
                    session_id=server_session_id or session_id,
                    result="completed",
                    updated_at=completed_at,
                )
                waiting_start_prompt_job_ids = _schedule_waiting_start_prompts_after_current_done(
                    workspace,
                    user_cron,
                    current_task_id=task_id,
                    occurred_at=completed_at,
                )
                data.update({
                    "reminder_id": server_task_id or task_id,
                    "reminder_session_id": server_session_id or session_id,
                    "local_reminder_id": task_id,
                    "status": "completed",
                    "source": "reminder_service_ad_hoc",
                    "current_task_path": str(_current_task_path(workspace)),
                    "memory_path": str(_memory_path(workspace)),
                    "cancelled_cron_job_ids": all_removed_wakeups,
                    "waiting_start_prompt_job_ids": waiting_start_prompt_job_ids,
                })
                return {"ok": True, "data": data, "error": None}

            error = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
            message = str(error.get("message") or result)
            sync = current.setdefault("sync", {})
            if isinstance(sync, dict):
                sync["status"] = "pending_sync"
                sync["sync_error"] = message
            state = current.setdefault("state", {})
            if isinstance(state, dict):
                state["status"] = "pending_sync"
            _update_current_todo(
                workspace,
                current,
                status=_TODO_STATUS_COMPLETED,
                occurred_at=completed_at,
                result="completed",
                sync_status="pending_sync",
                sync_error=message[:300],
                cron_job_ids=[],
            )
            _write_json(_current_task_path(workspace), current)
            _write_memory_pending_sync(
                workspace,
                reminder_id=task_id,
                session_id=session_id,
                title=title,
                sync_error=message[:300],
            )
            return parsed if isinstance(parsed, dict) else json.loads(_failure("INVALID_RESPONSE", result))

        sync = current.setdefault("sync", {})
        if isinstance(sync, dict):
            sync["status"] = "local_only_completed"
            sync["sync_error"] = None
        _update_current_todo(
            workspace,
            current,
            status=_TODO_STATUS_COMPLETED,
            occurred_at=completed_at,
            result="completed",
            sync_status="local_only_completed",
            cron_job_ids=[],
        )
        _write_json(_current_task_path(workspace), current)
        _write_memory_none(
            workspace,
            reminder_id=task_id,
            session_id=session_id,
            result="completed",
            updated_at=completed_at,
        )
        waiting_start_prompt_job_ids = _schedule_waiting_start_prompts_after_current_done(
            workspace,
            user_cron,
            current_task_id=task_id,
            occurred_at=completed_at,
        )
        return {
            "ok": True,
            "data": {
                "reminder_id": task_id,
                "reminder_session_id": session_id,
                "status": "completed",
                "source": "local_ad_hoc",
                "current_task_path": str(_current_task_path(workspace)),
                "memory_path": str(_memory_path(workspace)),
                "cancelled_cron_job_ids": all_removed_wakeups,
                "waiting_start_prompt_job_ids": waiting_start_prompt_job_ids,
            },
            "error": None,
        }

    try:
        normalized = _normalize_current_task_payload(
            current,
            task_id=task_id,
            end_result="completed",
            reason=reason or "用户确认完成",
        )
    except ValueError as exc:
        return json.loads(_failure("INVALID_ARGUMENT", str(exc)))

    result = await client.request(
        "POST",
        "/api/v1/reminder/tasks/sync",
        body={"payload": normalized},
    )
    try:
        parsed = json.loads(result)
    except Exception:
        parsed = {}

    if parsed.get("ok") is True:
        sync = current.setdefault("sync", {})
        if isinstance(sync, dict):
            sync["status"] = "synced"
            sync["last_sync_at"] = _now_iso()
            sync["sync_error"] = None
        _update_current_todo(
            workspace,
            current,
            status=_TODO_STATUS_COMPLETED,
            occurred_at=completed_at,
            result="completed",
            sync_status="synced",
            remote_task_id=task_id,
            remote_session_id=session_id,
            cron_job_ids=[],
        )
        _write_json(_current_task_path(workspace), current)
        _write_memory_none(
            workspace,
            reminder_id=task_id,
            session_id=session_id,
            result="completed",
            updated_at=completed_at,
        )
        waiting_start_prompt_job_ids = _schedule_waiting_start_prompts_after_current_done(
            workspace,
            user_cron,
            current_task_id=task_id,
            occurred_at=completed_at,
        )
        data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
        data.update({
            "reminder_id": task_id,
            "reminder_session_id": session_id,
            "status": "completed",
            "source": "reminder_service",
            "current_task_path": str(_current_task_path(workspace)),
            "memory_path": str(_memory_path(workspace)),
            "cancelled_cron_job_ids": all_removed_wakeups,
            "waiting_start_prompt_job_ids": waiting_start_prompt_job_ids,
        })
        return {"ok": True, "data": data, "error": None}

    error = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
    message = str(error.get("message") or result)
    sync = current.setdefault("sync", {})
    if isinstance(sync, dict):
        sync["status"] = "pending_sync"
        sync["sync_error"] = message
    state = current.setdefault("state", {})
    if isinstance(state, dict):
        state["status"] = "pending_sync"
    _update_current_todo(
        workspace,
        current,
        status=_TODO_STATUS_COMPLETED,
        occurred_at=completed_at,
        result="completed",
        sync_status="pending_sync",
        sync_error=message[:300],
        remote_task_id=task_id,
        remote_session_id=session_id,
        cron_job_ids=[],
    )
    _write_json(_current_task_path(workspace), current)
    _write_memory_pending_sync(
        workspace,
        reminder_id=task_id,
        session_id=session_id,
        title=title,
        sync_error=message[:300],
    )
    return parsed if isinstance(parsed, dict) else json.loads(_failure("INVALID_RESPONSE", result))


def _build_local_payload(
    *,
    reminder_id: str,
    session_id: str,
    title: str,
    status: str,
    started_at: str = "",
    expected_until: str = "",
    expected_minutes: int | None = None,
    notes: str = "",
    source_type: str = "ad_hoc",
    category: str = "",
) -> dict[str, Any]:
    now = _now_iso()
    category = _normalize_reminder_category(category, title=title, content=notes or title)
    return {
        "version": "1.0",
        "task_snapshot": {
            "id": reminder_id,
            "title": title,
            "source_type": source_type,
            "category": category,
            "created_at": now,
            "reminder_config": {
                "expected_minutes": expected_minutes,
            },
        },
        "session": {
            "id": session_id,
            "task_id": reminder_id,
            "status": "active" if status == "active" else status,
            "started_at": started_at,
            "task_ctx": notes,
        },
        "state": {
            "status": status,
            "started_at": started_at,
            "expected_until": expected_until,
            "last_activity_at": now,
        },
        "events": [
            {
                "type": "task_started" if status == "active" else "start_prompt_scheduled",
                "at": started_at or now,
                "notes": notes,
            }
        ],
        "messages": [],
        "wakeups": [],
        "sync": {
            "status": "local_only",
            "last_sync_at": None,
            "sync_error": None,
        },
    }


def _build_planned_completion_payload(
    workspace: Path,
    *,
    snapshot: dict[str, Any],
    completed_at: str,
    reason: str,
) -> dict[str, Any]:
    task_id = str(snapshot.get("id") or snapshot.get("ID") or "").strip()
    title = str(snapshot.get("title") or snapshot.get("Title") or "当前提醒").strip()
    uid = str(snapshot.get("uid") or snapshot.get("UID") or _extract_uid_from_workspace(workspace)).strip()
    planned_date = str(snapshot.get("planned_date") or snapshot.get("PlannedDate") or completed_at[:10]).strip()
    planned_start = str(snapshot.get("planned_start_at") or snapshot.get("PlannedStartAt") or "").strip()
    content = str(snapshot.get("content") or snapshot.get("Content") or title).strip()
    started_at = planned_start if planned_start and not planned_start.startswith("0001-01-01") else completed_at
    session_id = f"sess_{uuid.uuid4().hex[:16]}"
    return {
        "version": "1.0",
        "task_snapshot": snapshot,
        "session": {
            "id": session_id,
            "task_id": task_id,
            "uid": uid,
            "session_date": planned_date,
            "status": "ended",
            "session_status": "ended",
            "end_result": "completed",
            "resumable": False,
            "task_ctx": content,
            "started_at": started_at,
            "ended_at": completed_at,
        },
        "state": {
            "status": "completed",
            "started_at": started_at,
            "completed_at": completed_at,
            "last_activity_at": completed_at,
        },
        "events": [
            {
                "type": "task_started",
                "at": started_at,
                "notes": reason or "用户确认完成",
            },
            {
                "type": "task_ended",
                "at": completed_at,
                "reason": reason or "用户确认完成",
                "metadata": {"result": "completed"},
            },
        ],
        "messages": [],
        "wakeups": [],
        "sync": {
            "status": "pending_sync",
            "last_sync_at": None,
            "sync_error": None,
        },
    }


def _schedule_structured_wakeup(
    user_cron: Any,
    *,
    kind: str,
    message: str,
    task_id: str,
    session_id: str,
    at: str = "",
    after_seconds: int | None = None,
    wakeup_id: str = "",
    diagnostic: bool = False,
) -> dict[str, Any]:
    if not user_cron:
        raise RuntimeError("UserCron service is not available for task wakeups.")
    if not at and after_seconds is None:
        raise ValueError("Either at or after_seconds is required.")
    if after_seconds is not None and after_seconds <= 0:
        raise ValueError("after_seconds must be greater than 0.")

    if after_seconds is not None:
        at_ms = int(time.time() * 1000) + after_seconds * 1000
        scheduled_for = datetime.fromtimestamp(at_ms / 1000).astimezone().isoformat()
    else:
        at_ms = _parse_iso_ms(at)
        scheduled_for = at

    wakeup_id = wakeup_id or f"wake_{uuid.uuid4().hex[:8]}"
    payload = {
        "type": _CRON_TYPE_TASK_WAKEUP,
        "kind": kind,
        "task_id": task_id,
        "session_id": session_id,
        "wakeup_id": wakeup_id,
        "scheduled_for": scheduled_for,
        "message": message,
    }
    if diagnostic:
        payload["diagnostic"] = True

    job = user_cron.add_job(
        name=f"task_wakeup:{kind}"[:30],
        description=json.dumps(payload, ensure_ascii=False),
        schedule=CronSchedule(kind="at", at_ms=at_ms),
        delete_after_run=True,
        metadata=payload,
    )
    return {
        "wakeup_id": wakeup_id,
        "cron_job_id": job.id,
        "kind": kind,
        "task_id": task_id,
        "session_id": session_id,
        "scheduled_for": scheduled_for,
        "metadata": payload,
    }


def _schedule_one_off_wakeup(
    user_cron: Any,
    *,
    local_id: str,
    date: str,
    title: str,
    message: str,
    category: str,
    at: str = "",
    after_seconds: int | None = None,
) -> dict[str, Any]:
    if not user_cron:
        raise RuntimeError("UserCron service is not available for one-off reminders.")
    if not at and after_seconds is None:
        raise ValueError("Either at or after_seconds is required.")
    if after_seconds is not None and after_seconds <= 0:
        raise ValueError("after_seconds must be greater than 0.")

    if after_seconds is not None:
        at_ms = int(time.time() * 1000) + after_seconds * 1000
        scheduled_for = datetime.fromtimestamp(at_ms / 1000).astimezone().isoformat()
    else:
        at_ms = _parse_iso_ms(at)
        scheduled_for = at

    payload = {
        "type": _CRON_TYPE_ONE_OFF,
        "local_id": local_id,
        "date": date,
        "title": title,
        "message": message or title,
        "category": category,
        "scheduled_for": scheduled_for,
        "source": _TODO_SOURCE_ONE_OFF,
    }
    job = user_cron.add_job(
        name=f"reminder:{title}"[:30],
        description=json.dumps(payload, ensure_ascii=False),
        schedule=CronSchedule(kind="at", at_ms=at_ms),
        delete_after_run=True,
        metadata=payload,
    )
    return {
        "local_id": local_id,
        "cron_job_id": job.id,
        "scheduled_for": scheduled_for,
        "metadata": payload,
    }


def _find_existing_structured_wakeup(
    user_cron: Any,
    *,
    kind: str,
    task_id: str,
    session_id: str,
    wakeup_id: str = "",
) -> dict[str, Any] | None:
    if not user_cron:
        return None
    try:
        jobs = user_cron.list_jobs(include_disabled=True)
    except Exception:
        return None
    for job in jobs:
        if not _is_enabled_job(job):
            continue
        metadata = _job_metadata(job)
        if metadata.get("type") != _CRON_TYPE_TASK_WAKEUP:
            continue
        if str(metadata.get("kind") or "") != kind:
            continue
        if wakeup_id and str(metadata.get("wakeup_id") or "") != wakeup_id:
            continue
        if task_id and str(metadata.get("task_id") or "") != task_id:
            continue
        if session_id and str(metadata.get("session_id") or "") != session_id:
            continue
        return {
            "wakeup_id": str(metadata.get("wakeup_id") or ""),
            "cron_job_id": str(getattr(job, "id", "")),
            "kind": kind,
            "task_id": str(metadata.get("task_id") or ""),
            "session_id": str(metadata.get("session_id") or ""),
            "scheduled_for": str(metadata.get("scheduled_for") or ""),
            "metadata": metadata,
        }
    return None


def _append_wakeup_once(current: dict[str, Any], metadata: dict[str, Any]) -> bool:
    wakeups = current.setdefault("wakeups", [])
    if not isinstance(wakeups, list):
        current["wakeups"] = wakeups = []
    wakeup_id = str(metadata.get("wakeup_id") or "").strip()
    for item in wakeups:
        if not isinstance(item, dict):
            continue
        if wakeup_id and str(item.get("wakeup_id") or "") == wakeup_id:
            return False
        if (
            item.get("type") == metadata.get("type")
            and item.get("kind") == metadata.get("kind")
            and item.get("task_id") == metadata.get("task_id")
            and item.get("session_id") == metadata.get("session_id")
            and item.get("scheduled_for") == metadata.get("scheduled_for")
        ):
            return False
    wakeups.append(metadata)
    return True


def _job_metadata(job: Any) -> dict[str, Any]:
    metadata = getattr(job, "metadata", {}) or {}
    return metadata if isinstance(metadata, dict) else {}


def _job_schedule_at_ms(job: Any) -> int | None:
    schedule = getattr(job, "schedule", None)
    if getattr(schedule, "kind", "") != "at":
        return None
    at_ms = getattr(schedule, "at_ms", None)
    return at_ms if isinstance(at_ms, int) else None


def _is_enabled_job(job: Any) -> bool:
    return bool(getattr(job, "enabled", False))


def _user_cron_can_route_to_session(user_cron: Any) -> bool:
    if not user_cron:
        return False
    checker = getattr(user_cron, "can_route_to_session", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return True


def _todo_has_event(item: dict[str, Any], event_type: str, planned_start_at: str = "") -> bool:
    for event in item.get("events") or []:
        if not isinstance(event, dict) or event.get("type") != event_type:
            continue
        if not planned_start_at:
            return True
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        if str(metadata.get("planned_start_at") or "") == planned_start_at:
            return True
    return False


def _event_at_ms(event: dict[str, Any]) -> int | None:
    value = str(event.get("at") or "").strip()
    if not value:
        return None
    try:
        return _parse_iso_ms(value)
    except Exception:
        return None


def _has_recent_event(
    events: list[Any],
    event_types: set[str],
    *,
    now_ms: int,
    cooldown_seconds: int,
) -> bool:
    cooldown_ms = max(0, cooldown_seconds) * 1000
    for event in events:
        if not isinstance(event, dict) or str(event.get("type") or "") not in event_types:
            continue
        event_ms = _event_at_ms(event)
        if event_ms is not None and 0 <= now_ms - event_ms <= cooldown_ms:
            return True
    return False


def _append_event_once(events: list[Any], event: dict[str, Any]) -> bool:
    for existing in events:
        if not isinstance(existing, dict):
            continue
        if (
            existing.get("type") == event.get("type")
            and existing.get("task_id") == event.get("task_id")
            and existing.get("session_id") == event.get("session_id")
            and existing.get("reason") == event.get("reason")
        ):
            return False
    events.append(event)
    return True


def _todo_has_local_update_override(item: dict[str, Any] | None) -> bool:
    if not isinstance(item, dict):
        return False
    for event in item.get("events") or []:
        if not isinstance(event, dict):
            continue
        if event.get("type") not in {"task_updated", "task_time_extended"}:
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        if metadata.get("local_override") is True:
            return True
    return False


def _sync_item_cron_job_ids(
    item: dict[str, Any],
    *,
    remove_ids: list[str] | set[str] | None = None,
    add_ids: list[str] | set[str] | None = None,
) -> bool:
    remove_set = {str(job_id) for job_id in (remove_ids or []) if str(job_id).strip()}
    existing = [str(job_id) for job_id in item.get("cron_job_ids") or [] if str(job_id).strip()]
    updated: list[str] = []
    for job_id in existing:
        if job_id in remove_set or job_id in updated:
            continue
        updated.append(job_id)
    for job_id in [str(job_id) for job_id in (add_ids or []) if str(job_id).strip()]:
        if job_id not in updated:
            updated.append(job_id)
    if updated == existing:
        return False
    item["cron_job_ids"] = updated
    item["updated_at"] = _now_iso()
    return True


def _update_matching_todo_cron_ids(
    workspace: Path,
    *,
    date: str,
    local_id: str = "",
    remote_task_id: str = "",
    title: str = "",
    source: str = "",
    remove_ids: list[str] | set[str] | None = None,
    add_ids: list[str] | set[str] | None = None,
    existing_job_ids: set[str] | None = None,
) -> bool:
    if not date or _validate_date(date):
        return False
    store = ReminderStateStore(workspace)
    payload = store.load_todos(date)
    changed = False
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        if not ReminderStateStore._matches(
            item,
            local_id=local_id,
            remote_task_id=remote_task_id,
            title=title,
            source=source,
        ):
            continue
        effective_remove_ids = set(remove_ids or set())
        if existing_job_ids is not None:
            effective_remove_ids.update(
                str(job_id)
                for job_id in item.get("cron_job_ids") or []
                if str(job_id).strip() and str(job_id) not in existing_job_ids
            )
        changed = _sync_item_cron_job_ids(
            item,
            remove_ids=effective_remove_ids,
            add_ids=add_ids,
        ) or changed
        break
    if changed:
        store.save_todos(date, payload)
    return changed


def _scheduled_start_job_matches(job: Any, item: dict[str, Any], date: str) -> bool:
    metadata = _job_metadata(job)
    if metadata.get("type") != _CRON_TYPE_SCHEDULED_START:
        return False
    if str(metadata.get("date") or "") != date:
        return False
    local_id = str(item.get("local_id") or "").strip()
    remote_task_id = str(item.get("remote_task_id") or "").strip()
    if local_id and str(metadata.get("local_id") or "") == local_id:
        return True
    if remote_task_id and str(metadata.get("remote_task_id") or "") == remote_task_id:
        return True
    return False


def _task_completion_job_matches(job: Any, task_id: str, session_id: str) -> bool:
    metadata = _job_metadata(job)
    return (
        metadata.get("type") == _CRON_TYPE_TASK_WAKEUP
        and metadata.get("kind") == "completion_check"
        and (not task_id or str(metadata.get("task_id") or "") == task_id)
        and (not session_id or str(metadata.get("session_id") or "") == session_id)
    )


def _remove_matching_completion_jobs(
    user_cron: Any,
    *,
    task_id: str,
    session_id: str,
) -> list[str]:
    if not user_cron:
        return []
    try:
        jobs = user_cron.list_jobs(include_disabled=True)
    except Exception:
        return []
    removed: list[str] = []
    for job in jobs:
        job_id = str(getattr(job, "id", "") or "")
        if not job_id or not _task_completion_job_matches(job, task_id, session_id):
            continue
        try:
            if user_cron.remove_job(job_id):
                removed.append(job_id)
        except Exception:
            continue
    return removed


def _schedule_scheduled_start_wakeup(
    user_cron: Any,
    *,
    item: dict[str, Any],
    date: str,
    planned_start_at: str,
) -> Any:
    at_ms = _parse_iso_ms(planned_start_at)
    title = str(item.get("title") or "计划提醒").strip()
    payload = {
        "type": _CRON_TYPE_SCHEDULED_START,
        "local_id": str(item.get("local_id") or "").strip(),
        "remote_task_id": str(item.get("remote_task_id") or "").strip(),
        "remote_session_id": str(item.get("remote_session_id") or "").strip(),
        "date": date,
        "title": title,
        "content": str(item.get("content") or title).strip(),
        "planned_start_at": planned_start_at,
        "scheduled_for": planned_start_at,
        "expected_until": str(item.get("expected_until") or "").strip(),
        "source": _TODO_SOURCE_SCHEDULED,
        "message": f"{title}到时间了，要现在开始吗？",
    }
    return user_cron.add_job(
        name=f"reminder_start:{title}"[:30],
        description=json.dumps(payload, ensure_ascii=False),
        schedule=CronSchedule(kind="at", at_ms=at_ms),
        delete_after_run=True,
        metadata=payload,
    )


def _reconcile_scheduled_start_jobs(
    workspace: Path,
    user_cron: Any,
    *,
    date: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        jobs = user_cron.list_jobs(include_disabled=True)
    except Exception as exc:
        return {"created": [], "removed": [], "kept": [], "error": str(exc)}

    now_ms = int(time.time() * 1000)
    created: list[str] = []
    removed: list[str] = []
    kept: list[str] = []
    changed = False
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        matching_jobs = [job for job in jobs if _scheduled_start_job_matches(job, item, date)]
        matching_ids = [str(getattr(job, "id", "")) for job in matching_jobs if getattr(job, "id", "")]
        remove_ids: list[str] = []
        add_ids: list[str] = []

        planned_start_at = str(item.get("planned_start_at") or item.get("scheduled_for") or "").strip()
        status = str(item.get("status") or "").strip()
        source = str(item.get("source") or "").strip()
        should_have = (
            source == _TODO_SOURCE_SCHEDULED
            and status == _TODO_STATUS_SCHEDULED
            and planned_start_at
            and not _todo_has_event(item, "scheduled_start_prompted", planned_start_at)
        )
        try:
            planned_start_ms = _parse_iso_ms(planned_start_at) if should_have else None
        except Exception:
            planned_start_ms = None
            should_have = False

        if should_have and planned_start_ms is not None and planned_start_ms < now_ms - 60_000:
            should_have = False

        if should_have and planned_start_ms is not None:
            valid = [
                job for job in matching_jobs
                if _is_enabled_job(job) and _job_schedule_at_ms(job) == planned_start_ms
            ]
            keep_job = valid[0] if valid else None
            for job in matching_jobs:
                if keep_job and getattr(job, "id", "") == getattr(keep_job, "id", ""):
                    continue
                if user_cron.remove_job(job.id):
                    removed.append(job.id)
                    remove_ids.append(job.id)
            if keep_job:
                kept.append(keep_job.id)
                add_ids.append(keep_job.id)
            else:
                job = _schedule_scheduled_start_wakeup(
                    user_cron,
                    item=item,
                    date=date,
                    planned_start_at=planned_start_at,
                )
                created.append(job.id)
                add_ids.append(job.id)
        else:
            for job in matching_jobs:
                if user_cron.remove_job(job.id):
                    removed.append(job.id)
                    remove_ids.append(job.id)
            remove_ids.extend(matching_ids)

        if _sync_item_cron_job_ids(item, remove_ids=remove_ids, add_ids=add_ids):
            changed = True

    if changed:
        ReminderStateStore(workspace).save_todos(date, payload)
    return {"created": created, "removed": removed, "kept": kept}


def _overdue_intent_event(
    *,
    task_id: str,
    session_id: str = "",
    reason: str,
    scheduled_for: str,
    expected_until: str,
    intent: str,
) -> dict[str, Any]:
    return {
        "type": "automation_intent_evaluated",
        "at": _now_iso(),
        "actor": "system",
        "task_id": task_id,
        "session_id": session_id,
        "intent": intent,
        "reason": reason,
        "metadata": {
            "scheduled_for": scheduled_for,
            "expected_until": expected_until,
        },
    }


def _overdue_action_from_todo(
    item: dict[str, Any],
    *,
    reason: str,
    scheduled_for: str,
    expected_until: str,
) -> dict[str, Any]:
    return {
        "type": "overdue_intent",
        "intent": "llm_decide",
        "reason": reason,
        "task": {
            "local_id": item.get("local_id"),
            "remote_task_id": item.get("remote_task_id"),
            "remote_session_id": item.get("remote_session_id"),
            "title": item.get("title"),
            "content": item.get("content"),
            "status": item.get("status"),
            "planned_start_at": item.get("planned_start_at"),
            "expected_until": item.get("expected_until"),
        },
        "events": [event for event in item.get("events") or [] if isinstance(event, dict)][-10:],
        "instruction": "Use reminder-agent policy and events to decide whether to send a short prompt or stay silent.",
    }


def _reconcile_overdue_todo_intents(
    workspace: Path,
    *,
    date: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    actions: list[dict[str, Any]] = []
    silenced: list[dict[str, Any]] = []
    changed = False
    cooldown_types = {
        "automation_intent_evaluated",
        "overdue_prompt_sent",
        "overdue_prompt_silenced",
        "overdue_prompt_scheduled",
        "prompt_snoozed",
    }

    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("source") or "") != _TODO_SOURCE_SCHEDULED:
            continue
        if str(item.get("status") or "") != _TODO_STATUS_SCHEDULED:
            continue

        planned_start_at = str(item.get("planned_start_at") or item.get("scheduled_for") or "").strip()
        expected_until = str(item.get("expected_until") or "").strip()
        due_at = expected_until or planned_start_at
        try:
            due_ms = _parse_iso_ms(due_at) if due_at else None
        except Exception:
            due_ms = None
        if due_ms is None or due_ms > now_ms:
            continue

        task_id = str(item.get("remote_task_id") or item.get("local_id") or "").strip()
        events = item.setdefault("events", [])
        if not isinstance(events, list):
            events = []
            item["events"] = events

        recent = _has_recent_event(
            events,
            cooldown_types,
            now_ms=now_ms,
            cooldown_seconds=_OVERDUE_PROMPT_COOLDOWN_SECONDS,
        )
        reason = "recent_overdue_interaction" if recent else "scheduled_task_overdue"

        action = _overdue_action_from_todo(
            item,
            reason=reason,
            scheduled_for=planned_start_at,
            expected_until=expected_until,
        )
        if recent:
            silenced.append(action)
        else:
            event = _overdue_intent_event(
                task_id=task_id,
                reason=reason,
                scheduled_for=planned_start_at,
                expected_until=expected_until,
                intent="llm_decide",
            )
            events.append(event)
            item["updated_at"] = _now_iso()
            changed = True
            actions.append(action)

    if changed:
        ReminderStateStore(workspace).save_todos(date, payload)
    return {"actions": actions, "silenced": silenced}


def _set_current_completion_wakeup(
    current: dict[str, Any],
    completion_metadata: dict[str, Any] | None,
) -> bool:
    wakeups = current.get("wakeups") if isinstance(current.get("wakeups"), list) else []
    updated = [
        wakeup for wakeup in wakeups
        if not (
            isinstance(wakeup, dict)
            and wakeup.get("type") == _CRON_TYPE_TASK_WAKEUP
            and wakeup.get("kind") == "completion_check"
        )
    ]
    if completion_metadata:
        updated.append(completion_metadata)
    if updated == wakeups:
        return False
    current["wakeups"] = updated
    return True


def _reconcile_current_completion_wakeup(
    workspace: Path,
    user_cron: Any,
    *,
    target_date: str = "",
) -> dict[str, Any]:
    try:
        jobs = user_cron.list_jobs(include_disabled=True)
    except Exception as exc:
        return {"created": [], "removed": [], "kept": [], "error": str(exc)}

    completion_jobs = [
        job for job in jobs
        if _job_metadata(job).get("type") == _CRON_TYPE_TASK_WAKEUP
        and _job_metadata(job).get("kind") == "completion_check"
    ]
    current = _current_reminder(workspace)
    status = _current_status(current)
    active = _is_current_task_payload(current) and status in _CURRENT_ACTIVE_STATUSES
    task_id, session_id = _current_ids(current or {}) if isinstance(current, dict) else ("", "")
    expected_until = _current_expected_until(current) if active else ""
    current_date = _current_todo_date(current, expected_until or "") if _is_current_task_payload(current) else ""
    stale_current = bool(active and target_date and current_date and current_date != target_date)
    active_for_reconcile = active and not stale_current
    now_ms = int(time.time() * 1000)
    removed: list[str] = []
    created: list[str] = []
    kept: list[str] = []
    actions: list[dict[str, Any]] = []
    silenced: list[dict[str, Any]] = []
    keep_job: Any | None = None
    completion_metadata: dict[str, Any] | None = None
    desired_ms: int | None = None

    if active_for_reconcile and task_id and session_id and expected_until:
        try:
            desired_ms = _parse_iso_ms(expected_until)
        except Exception:
            desired_ms = None
        if desired_ms is not None and desired_ms > now_ms:
            valid = [
                job for job in completion_jobs
                if _task_completion_job_matches(job, task_id, session_id)
                and _is_enabled_job(job)
                and _job_schedule_at_ms(job) == desired_ms
            ]
            keep_job = valid[0] if valid else None

    for job in completion_jobs:
        if keep_job and getattr(job, "id", "") == getattr(keep_job, "id", ""):
            continue
        if user_cron.remove_job(job.id):
            removed.append(job.id)

    overdue_active = bool(active_for_reconcile and task_id and session_id and expected_until and desired_ms is not None and desired_ms <= now_ms)

    if active_for_reconcile and task_id and session_id and expected_until and not overdue_active:
        if keep_job:
            kept.append(keep_job.id)
            completion_metadata = _job_metadata(keep_job)
        else:
            try:
                wakeup = _schedule_structured_wakeup(
                    user_cron,
                    kind="completion_check",
                    message=f"{_current_title(current)}的预计时间到了，完成了吗？",
                    task_id=task_id,
                    session_id=session_id,
                    at=expected_until,
                )
                created.append(wakeup["cron_job_id"])
                completion_metadata = wakeup["metadata"]
            except Exception as exc:
                logger.warning("Failed to reconcile reminder completion wakeup: {}", exc)

    if _is_current_task_payload(current):
        if overdue_active:
            events = current.setdefault("events", [])
            if not isinstance(events, list):
                events = []
                current["events"] = events
            recent = _has_recent_event(
                events,
                {
                    "automation_intent_evaluated",
                    "overdue_prompt_sent",
                    "overdue_prompt_silenced",
                    "overdue_prompt_scheduled",
                    "prompt_snoozed",
                },
                now_ms=now_ms,
                cooldown_seconds=_OVERDUE_PROMPT_COOLDOWN_SECONDS,
            )
            reason = "recent_overdue_interaction" if recent else "active_task_overdue"
            action = {
                "type": "overdue_intent",
                "intent": "llm_decide",
                "reason": reason,
                "task": {
                    "local_id": _current_todo_local_id(current) or task_id,
                    "remote_task_id": task_id,
                    "remote_session_id": session_id,
                    "title": _current_title(current),
                    "status": _current_status(current),
                    "expected_until": expected_until,
                },
                "events": [event for event in events if isinstance(event, dict)][-10:],
                "instruction": "Use reminder-agent policy and events to decide whether to send a short prompt or stay silent.",
            }
            if recent:
                silenced.append(action)
            else:
                event = _overdue_intent_event(
                    task_id=task_id,
                    session_id=session_id,
                    reason=reason,
                    scheduled_for=str((current.get("state") if isinstance(current.get("state"), dict) else {}).get("started_at") or ""),
                    expected_until=expected_until,
                    intent="llm_decide",
                )
                events.append(event)
                actions.append(action)
        if _set_current_completion_wakeup(current, completion_metadata):
            _write_json(_current_task_path(workspace), current)
        elif overdue_active:
            _write_json(_current_task_path(workspace), current)
        current_date = _current_todo_date(current, expected_until or _now_iso())
        local_id = _current_todo_local_id(current) or task_id
        source = _current_todo_source(current)
        try:
            existing_job_ids = {
                str(getattr(job, "id", ""))
                for job in user_cron.list_jobs(include_disabled=True)
                if getattr(job, "id", "")
            }
        except Exception:
            existing_job_ids = None
        _update_matching_todo_cron_ids(
            workspace,
            date=current_date,
            local_id=local_id,
            remote_task_id=task_id if source == _TODO_SOURCE_SCHEDULED else "",
            title=_current_title(current),
            source=source,
            remove_ids=removed,
            add_ids=kept + created,
            existing_job_ids=existing_job_ids,
        )
    return {
        "created": created,
        "removed": removed,
        "kept": kept,
        "actions": actions,
        "silenced": silenced,
        "skipped": "stale_current_task" if stale_current else "",
    }


def reconcile_reminder_todos_jobs(
    workspace: Path,
    user_cron: Any,
    *,
    date: str | None = None,
    mode: str = _REFRESH_MODE_INTERACTIVE,
) -> dict[str, Any]:
    """Reconcile local reminder todos/current_task into per-user cron jobs."""
    if not user_cron:
        return {"ok": False, "error": "UserCron service is not available."}
    target_date = (date or datetime.now().astimezone().date().isoformat()).strip()
    if err := _validate_date(target_date):
        return {"ok": False, "error": err}
    if mode not in {_REFRESH_MODE_SYNC_ONLY, _REFRESH_MODE_INTERACTIVE}:
        return {"ok": False, "error": f"Unsupported reminder reconcile mode: {mode}"}

    can_route = _user_cron_can_route_to_session(user_cron)
    if mode == _REFRESH_MODE_SYNC_ONLY or not can_route:
        return {
            "ok": True,
            "date": target_date,
            "mode": _REFRESH_MODE_SYNC_ONLY,
            "interactive": False,
            "scheduled_start": {"created": [], "removed": [], "kept": [], "skipped": "no_live_session"},
            "completion_check": {"created": [], "removed": [], "kept": [], "skipped": "no_live_session"},
            "overdue_intents": {"actions": [], "silenced": []},
        }

    store = ReminderStateStore(workspace)
    payload = store.load_todos(target_date)
    scheduled_start = _reconcile_scheduled_start_jobs(
        workspace,
        user_cron,
        date=target_date,
        payload=payload,
    )
    payload = store.load_todos(target_date)
    overdue_intents = _reconcile_overdue_todo_intents(
        workspace,
        date=target_date,
        payload=payload,
    )
    completion_check = _reconcile_current_completion_wakeup(
        workspace,
        user_cron,
        target_date=target_date,
    )
    return {
        "ok": True,
        "date": target_date,
        "mode": _REFRESH_MODE_INTERACTIVE,
        "interactive": True,
        "scheduled_start": scheduled_start,
        "completion_check": completion_check,
        "overdue_intents": overdue_intents,
    }


def _find_scheduled_start_todo(
    workspace: Path,
    metadata: dict[str, Any],
) -> tuple[str, dict[str, Any], int, dict[str, Any] | None]:
    date = str(metadata.get("date") or _date_from_iso(str(metadata.get("scheduled_for") or ""))).strip()
    store = ReminderStateStore(workspace)
    payload = store.load_todos(date)
    local_id = str(metadata.get("local_id") or "").strip()
    remote_task_id = str(metadata.get("remote_task_id") or "").strip()
    for idx, item in enumerate(payload.get("items") or []):
        if not isinstance(item, dict):
            continue
        if ReminderStateStore._matches(
            item,
            local_id=local_id,
            remote_task_id=remote_task_id,
            source=_TODO_SOURCE_SCHEDULED,
        ):
            return date, payload, idx, item
    return date, payload, -1, None


def prepare_scheduled_start_wakeup_from_cron(
    workspace: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Return a structured scheduled-start wakeup payload, or a silent skip."""
    date, payload, idx, item = _find_scheduled_start_todo(workspace, metadata)
    if item is None:
        return {
            "ok": True,
            "data": {"action": "skip", "reason": "todo_not_found", "summary": "scheduled start todo not found"},
            "error": None,
        }

    planned_start_at = str(item.get("planned_start_at") or metadata.get("planned_start_at") or "").strip()
    status = str(item.get("status") or "").strip()
    if status in _TODO_FINAL_STATUSES:
        return {
            "ok": True,
            "data": {"action": "skip", "reason": f"todo_{status}", "summary": f"scheduled start skipped: {status}"},
            "error": None,
        }
    if status == _TODO_STATUS_ACTIVE:
        return {
            "ok": True,
            "data": {"action": "skip", "reason": "todo_active", "summary": "scheduled start skipped: already active"},
            "error": None,
        }
    if _todo_has_event(item, "scheduled_start_prompted", planned_start_at):
        return {
            "ok": True,
            "data": {
                "action": "skip",
                "reason": "already_prompted",
                "summary": "scheduled start skipped: already prompted",
            },
            "error": None,
        }

    current = _current_reminder(workspace)
    current_status = _current_status(current)
    current_task_id, current_session_id = _current_ids(current or {}) if isinstance(current, dict) else ("", "")
    remote_task_id = str(item.get("remote_task_id") or metadata.get("remote_task_id") or "").strip()
    local_id = str(item.get("local_id") or metadata.get("local_id") or "").strip()
    current_local_id = _current_todo_local_id(current or {}) if isinstance(current, dict) else ""
    same_current = bool(
        current_status
        and (
            (remote_task_id and remote_task_id == current_task_id)
            or (local_id and local_id == current_local_id)
        )
    )
    cron_job_id = str(metadata.get("_cron_job_id") or metadata.get("cron_job_id") or "").strip()
    if same_current and current_status in _CURRENT_ACTIVE_STATUSES | {"pending_start"}:
        item["status"] = _TODO_STATUS_PENDING_START if current_status == "pending_start" else _TODO_STATUS_ACTIVE
        item["updated_at"] = _now_iso()
        _sync_item_cron_job_ids(item, remove_ids=[cron_job_id] if cron_job_id else [])
        ReminderStateStore(workspace).save_todos(date, payload)
        return {
            "ok": True,
            "data": {
                "action": "skip",
                "reason": "same_current_reminder",
                "summary": "scheduled start skipped: same reminder is current",
            },
            "error": None,
        }

    now = _now_iso()
    events = item.setdefault("events", [])
    if not isinstance(events, list):
        item["events"] = events = []
    events.append({
        "type": "scheduled_start_prompted",
        "at": now,
        "reason": "scheduled start cron fired",
        "metadata": {
            "cron_job_id": cron_job_id,
            "planned_start_at": planned_start_at,
        },
    })
    item["status"] = _TODO_STATUS_PENDING_START
    item["updated_at"] = now
    _sync_item_cron_job_ids(item, remove_ids=[cron_job_id] if cron_job_id else [])
    ReminderStateStore(workspace).save_todos(date, payload)

    has_blocking_current = _is_current_task_payload(current) and current_status in (_CURRENT_ACTIVE_STATUSES | {"pending_start"})
    wakeup_payload = {
        **metadata,
        "date": date,
        "local_id": local_id,
        "remote_task_id": remote_task_id,
        "title": str(item.get("title") or metadata.get("title") or "计划提醒").strip(),
        "content": str(item.get("content") or metadata.get("content") or "").strip(),
        "planned_start_at": planned_start_at,
        "expected_until": str(item.get("expected_until") or metadata.get("expected_until") or "").strip(),
        "current_task_id": current_task_id,
        "current_session_id": current_session_id,
        "current_status": current_status,
        "conflict": has_blocking_current,
    }
    if has_blocking_current:
        content = (
            "[Reminder Scheduled Start Conflict]\n"
            "Use the reminder-agent skill. A scheduled reminder is due, but another reminder is current. "
            "Read task-planner/current_task.json and task-planner/reminder_todos/"
            f"{date}.json before replying. Reply in short spoken Chinese, ask whether to switch, "
            "and do not start or complete anything automatically.\n\n"
            f"Wakeup payload:\n{json.dumps(wakeup_payload, ensure_ascii=False)}"
        )
        summary = "scheduled start conflict wakeup published"
    else:
        content = (
            "[Reminder Scheduled Start Wakeup]\n"
            "Use the reminder-agent skill. This scheduled reminder is due to start. "
            "Read task-planner/reminder_todos/"
            f"{date}.json and task-planner/current_task.json before replying. "
            "Reply in short spoken Chinese and ask whether to start now. Do not start automatically.\n\n"
            f"Wakeup payload:\n{json.dumps(wakeup_payload, ensure_ascii=False)}"
        )
        summary = "scheduled start wakeup published"
    return {
        "ok": True,
        "data": {
            "action": "publish",
            "content": content,
            "metadata": wakeup_payload,
            "summary": summary,
        },
        "error": None,
    }


def prepare_task_wakeup_from_cron(
    workspace: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Prepare a structured task wakeup and record prompt events before Agent delivery."""
    kind = str(metadata.get("kind") or "").strip()
    if kind != "completion_check":
        return {
            "ok": True,
            "data": {"action": "publish", "content": "", "metadata": metadata, "summary": "task wakeup published"},
            "error": None,
        }

    current = _current_reminder(workspace)
    if not _is_current_task_payload(current):
        return {
            "ok": True,
            "data": {"action": "skip", "reason": "no_current_reminder", "summary": "completion wakeup skipped: no current reminder"},
            "error": None,
        }
    current_status = _current_status(current)
    if current_status not in _CURRENT_ACTIVE_STATUSES:
        return {
            "ok": True,
            "data": {"action": "skip", "reason": f"current_{current_status or 'none'}", "summary": "completion wakeup skipped: current reminder is not active"},
            "error": None,
        }

    task_id, session_id = _current_ids(current)
    expected_task_id = str(metadata.get("task_id") or "").strip()
    expected_session_id = str(metadata.get("session_id") or "").strip()
    if (expected_task_id and expected_task_id != task_id) or (expected_session_id and expected_session_id != session_id):
        return {
            "ok": True,
            "data": {"action": "skip", "reason": "current_mismatch", "summary": "completion wakeup skipped: current reminder mismatch"},
            "error": None,
        }

    now = _now_iso()
    events = current.setdefault("events", [])
    if not isinstance(events, list):
        events = []
        current["events"] = events
    event = _overdue_intent_event(
        task_id=task_id,
        session_id=session_id,
        reason="completion_check_prompted",
        scheduled_for=str(metadata.get("scheduled_for") or ""),
        expected_until=_current_expected_until(current),
        intent="ask_completion",
    )
    event["type"] = "overdue_prompt_sent"
    event["at"] = now
    event["metadata"]["wakeup_id"] = str(metadata.get("wakeup_id") or "")
    event["metadata"]["cron_job_id"] = str(metadata.get("_cron_job_id") or metadata.get("cron_job_id") or "")
    _append_event_once(events, event)
    _set_current_completion_wakeup(current, None)
    _write_json(_current_task_path(workspace), current)

    current_date = _current_todo_date(current, _current_expected_until(current) or now)
    local_id = _current_todo_local_id(current) or task_id
    source = _current_todo_source(current)
    ReminderStateStore(workspace).update_todo(
        current_date,
        local_id=local_id,
        remote_task_id=task_id if source == _TODO_SOURCE_SCHEDULED else "",
        source=source,
        updates={"current_task_path": str(_current_task_path(workspace))},
        event=event,
    )
    cron_job_id = str(metadata.get("_cron_job_id") or metadata.get("cron_job_id") or "").strip()
    if cron_job_id:
        _update_matching_todo_cron_ids(
            workspace,
            date=current_date,
            local_id=local_id,
            remote_task_id=task_id if source == _TODO_SOURCE_SCHEDULED else "",
            source=source,
            remove_ids=[cron_job_id],
        )

    wakeup_payload = {
        **metadata,
        "task_id": task_id,
        "session_id": session_id,
        "title": _current_title(current),
        "current_status": current_status,
        "expected_until": _current_expected_until(current),
        "events": [item for item in events if isinstance(item, dict)][-10:],
    }
    content = (
        "[Reminder Completion Wakeup]\n"
        "Use the reminder-agent skill. A current reminder reached its expected time. "
        "Use events to decide whether to ask or stay silent. Do not start, complete, close, or defer automatically.\n\n"
        f"Wakeup payload:\n{json.dumps(wakeup_payload, ensure_ascii=False)}"
    )
    return {
        "ok": True,
        "data": {
            "action": "publish",
            "content": content,
            "metadata": wakeup_payload,
            "summary": "completion wakeup published",
        },
        "error": None,
    }

def _build_one_off_sync_payload(
    workspace: Path,
    *,
    item: dict[str, Any],
    fired_at: str,
) -> dict[str, Any]:
    local_id = str(item.get("local_id") or "").strip()
    title = str(item.get("title") or "提醒").strip()
    message = str(item.get("content") or title).strip()
    category = _normalize_reminder_category(str(item.get("category") or ""), title=title, content=message)
    scheduled_for = str(item.get("scheduled_for") or item.get("expected_until") or fired_at).strip()
    session_id = f"sess_{uuid.uuid5(uuid.NAMESPACE_URL, local_id or title).hex[:16]}"
    date = _date_from_iso(scheduled_for or fired_at)
    return {
        "version": _TODO_VERSION,
        "task_snapshot": {
            "id": "",
            "title": title,
            "content": message,
            "source_type": "ad_hoc",
            "category": category,
            "created_by": "agent",
            "planned_date": date,
            "planned_start_at": scheduled_for,
            "planned_end_at": fired_at,
            "local_id": local_id,
        },
        "session": {
            "id": session_id,
            "task_id": "",
            "uid": _extract_uid_from_workspace(workspace),
            "session_date": date,
            "status": "ended",
            "session_status": "ended",
            "end_result": "completed",
            "resumable": False,
            "task_ctx": message,
            "started_at": scheduled_for,
            "ended_at": fired_at,
        },
        "state": {
            "status": "completed",
            "started_at": scheduled_for,
            "completed_at": fired_at,
            "last_activity_at": fired_at,
            "todo_local_id": local_id,
        },
        "events": [
            {
                "type": "task_started",
                "at": scheduled_for,
                "notes": "one-off reminder scheduled",
            },
            {
                "type": "task_ended",
                "at": fired_at,
                "reason": "one-off reminder fired",
                "metadata": {"result": "completed", "source": _TODO_SOURCE_ONE_OFF},
            },
        ],
        "messages": [],
        "wakeups": [],
        "sync": {
            "status": "pending_sync",
            "last_sync_at": None,
            "sync_error": None,
        },
    }


async def fire_one_off_from_cron(
    workspace: Path,
    metadata: dict[str, Any],
    config: ReminderToolConfigData | None = None,
) -> dict[str, Any]:
    """Mark an independent one-off reminder as fired and silently sync when configured."""
    local_id = str(metadata.get("local_id") or "").strip()
    title = str(metadata.get("title") or "提醒").strip()
    message = str(metadata.get("message") or title).strip()
    scheduled_for = str(metadata.get("scheduled_for") or "").strip()
    date = str(metadata.get("date") or _date_from_iso(scheduled_for)).strip()
    fired_at = _now_iso()
    if not local_id:
        return json.loads(_failure("INVALID_ARGUMENT", "one-off reminder metadata.local_id is required."))

    store = ReminderStateStore(workspace)
    item = store.update_todo(
        date,
        local_id=local_id,
        source=_TODO_SOURCE_ONE_OFF,
        updates={
            "status": _TODO_STATUS_COMPLETED,
            "fired_at": fired_at,
            "completed_at": fired_at,
            "ended_at": fired_at,
            "sync": _todo_sync("pending_sync", error="sync not attempted"),
        },
        event={"type": "fired", "at": fired_at, "reason": message},
        create_item={
            "local_id": local_id,
            "source": _TODO_SOURCE_ONE_OFF,
            "title": title,
            "content": message,
            "status": _TODO_STATUS_COMPLETED,
            "scheduled_for": scheduled_for,
            "expected_until": scheduled_for,
            "sync": _todo_sync("pending_sync", error="sync not attempted"),
        },
    )
    if not item:
        return json.loads(_failure("INTERNAL", "failed to update one-off reminder state."))

    if not config or not config.base_url.strip():
        item = store.update_todo(
            date,
            local_id=local_id,
            source=_TODO_SOURCE_ONE_OFF,
            updates={"sync": _todo_sync("pending_sync", error="reminder service not configured")},
        ) or item
        return {
            "ok": True,
            "data": {
                "local_id": local_id,
                "status": _TODO_STATUS_COMPLETED,
                "sync_status": item.get("sync", {}).get("status", "pending_sync"),
                "todos_path": str(_reminder_todos_path(workspace, date)),
            },
            "error": None,
        }

    client = ReminderClient(config, workspace)
    try:
        normalized = _normalize_current_task_payload(
            _build_one_off_sync_payload(workspace, item=item, fired_at=fired_at),
            end_result="completed",
            reason="one-off reminder fired",
        )
    except ValueError as exc:
        store.update_todo(
            date,
            local_id=local_id,
            source=_TODO_SOURCE_ONE_OFF,
            updates={"sync": _todo_sync("pending_sync", error=str(exc))},
        )
        return json.loads(_failure("INVALID_ARGUMENT", str(exc)))

    result = await client.request("POST", "/api/v1/reminder/tasks/sync", body={"payload": normalized})
    try:
        parsed = json.loads(result)
    except Exception:
        parsed = {}

    if parsed.get("ok") is True:
        data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
        server_task = data.get("task") if isinstance(data.get("task"), dict) else {}
        remote_task_id = str(data.get("task_id") or server_task.get("id") or "").strip()
        remote_session_id = str(data.get("session_id") or "").strip()
        store.update_todo(
            date,
            local_id=local_id,
            source=_TODO_SOURCE_ONE_OFF,
            updates={
                "remote_task_id": remote_task_id,
                "remote_session_id": remote_session_id,
                "sync": _todo_sync("synced", last_sync_at=str(data.get("synced_at") or _now_iso())),
            },
        )
        return {
            "ok": True,
            "data": {
                "local_id": local_id,
                "remote_task_id": remote_task_id,
                "remote_session_id": remote_session_id,
                "status": _TODO_STATUS_COMPLETED,
                "sync_status": "synced",
                "todos_path": str(_reminder_todos_path(workspace, date)),
            },
            "error": None,
        }

    error = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
    message = str(error.get("message") or result)
    store.update_todo(
        date,
        local_id=local_id,
        source=_TODO_SOURCE_ONE_OFF,
        updates={"sync": _todo_sync("pending_sync", error=message[:300])},
    )
    return {
        "ok": True,
        "data": {
            "local_id": local_id,
            "status": _TODO_STATUS_COMPLETED,
            "sync_status": "pending_sync",
            "sync_error": message[:300],
            "todos_path": str(_reminder_todos_path(workspace, date)),
        },
        "error": None,
    }


def _coerce_task_snapshot(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _expected_until_for_snapshot(snapshot: dict[str, Any], started_at: str) -> str:
    planned_end = str(snapshot.get("planned_end_at") or "").strip()
    if planned_end and not planned_end.startswith("0001-01-01"):
        try:
            if not started_at or _parse_iso_ms(planned_end) > _parse_iso_ms(started_at):
                return planned_end
        except Exception:
            return planned_end
    minutes = snapshot.get("estimated_duration_minutes")
    if minutes and started_at:
        try:
            return (datetime.fromisoformat(started_at) + timedelta(minutes=int(minutes))).isoformat()
        except Exception:
            return ""
    return ""


def _snapshot_text(snapshot: dict[str, Any], snake: str, camel: str = "") -> str:
    return str(snapshot.get(snake) or (snapshot.get(camel) if camel else "") or "").strip()


def _scheduled_status_from_remote(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in {"done", "completed", "complete"}:
        return _TODO_STATUS_COMPLETED
    if normalized in {"running", "active"}:
        return _TODO_STATUS_ACTIVE
    if normalized in {"deferred"}:
        return _TODO_STATUS_DEFERRED
    if normalized in {"closed", "cancelled", "canceled"}:
        return _TODO_STATUS_CLOSED
    return _TODO_STATUS_SCHEDULED


def _upsert_scheduled_todo_from_snapshot(
    workspace: Path,
    snapshot: dict[str, Any],
    *,
    status: str = "",
    session_id: str = "",
    started_at: str = "",
    ended_at: str = "",
    sync_status: str = "remote",
    sync_error: str = "",
) -> dict[str, Any] | None:
    task_id = _snapshot_text(snapshot, "id", "ID")
    if not task_id:
        return None
    source_type = _snapshot_text(snapshot, "source_type", "SourceType").lower()
    source = _TODO_SOURCE_AD_HOC if source_type in {"ad_hoc", "adhoc"} else _TODO_SOURCE_SCHEDULED
    title = _snapshot_text(snapshot, "title", "Title") or "计划提醒"
    content = _snapshot_text(snapshot, "content", "Content") or title
    category = _normalize_reminder_category(
        _snapshot_text(snapshot, "category", "Category"),
        title=title,
        content=content,
    )
    planned_date = _snapshot_text(snapshot, "planned_date", "PlannedDate")
    planned_start = _snapshot_text(snapshot, "planned_start_at", "PlannedStartAt")
    planned_end = _snapshot_text(snapshot, "planned_end_at", "PlannedEndAt")
    expected_until = planned_end if planned_end and not planned_end.startswith("0001-01-01") else ""
    if not expected_until and started_at:
        expected_until = _expected_until_for_snapshot(snapshot, started_at)
    date = _date_from_iso(planned_date or planned_start or started_at or ended_at)

    store = ReminderStateStore(workspace)
    existing = store.find_todo(date, remote_task_id=task_id, source=source)
    effective_status = status or _scheduled_status_from_remote(_snapshot_text(snapshot, "status", "Status"))
    if (
        existing
        and existing.get("status") in {_TODO_STATUS_ACTIVE, _TODO_STATUS_PENDING_START}
        and effective_status == _TODO_STATUS_SCHEDULED
    ):
        effective_status = _TODO_STATUS_ACTIVE
        if existing.get("status") == _TODO_STATUS_PENDING_START:
            effective_status = _TODO_STATUS_PENDING_START
    preserve_local_update = (
        existing
        and existing.get("status") in {_TODO_STATUS_ACTIVE, _TODO_STATUS_PENDING_START}
        and effective_status in {_TODO_STATUS_ACTIVE, _TODO_STATUS_PENDING_START}
        and _todo_has_local_update_override(existing)
    )
    if preserve_local_update:
        title = str(existing.get("title") or title).strip()
        content = str(existing.get("content") or content).strip()
        expected_until = str(existing.get("expected_until") or expected_until).strip()
        category = str(existing.get("category") or category).strip()

    item = {
        "local_id": str((existing or {}).get("local_id") or task_id),
        "source": source,
        "remote_task_id": task_id,
        "remote_session_id": session_id or str((existing or {}).get("remote_session_id") or ""),
        "category": category,
        "title": title,
        "content": content,
        "status": effective_status,
        "planned_start_at": planned_start or started_at,
        "expected_until": expected_until,
        "started_at": started_at or str((existing or {}).get("started_at") or ""),
        "ended_at": ended_at,
        "completed_at": ended_at if effective_status == _TODO_STATUS_COMPLETED else "",
        "current_task_path": str(_current_task_path(workspace)) if effective_status == _TODO_STATUS_ACTIVE else "",
        "events": [
            {
                "type": "started" if effective_status == _TODO_STATUS_ACTIVE else "synced",
                "at": started_at or ended_at or _now_iso(),
            }
        ],
        "sync": _todo_sync(sync_status, last_sync_at=_now_iso() if sync_status in {"remote", "synced"} else "", error=sync_error),
        "metadata": {"snapshot": snapshot},
    }
    return store.upsert_todo(date, item)


def _write_started_planned_task(
    workspace: Path,
    *,
    snapshot: dict[str, Any],
    session_id: str,
    started_at: str,
) -> dict[str, Any]:
    snapshot = dict(snapshot)
    task_id = str(snapshot.get("id") or "").strip()
    title = str(snapshot.get("title") or "当前提醒").strip()
    snapshot["category"] = _normalize_reminder_category(
        str(snapshot.get("category") or ""),
        title=title,
        content=str(snapshot.get("content") or title),
    )
    expected_until = _expected_until_for_snapshot(snapshot, started_at)
    payload = _build_local_payload(
        reminder_id=task_id,
        session_id=session_id,
        title=title,
        status="active",
        started_at=started_at,
        expected_until=expected_until,
        expected_minutes=snapshot.get("estimated_duration_minutes"),
        notes=str(snapshot.get("content") or title),
        source_type=str(snapshot.get("source_type") or "scheduled"),
        category=str(snapshot.get("category") or ""),
    )
    payload.setdefault("state", {})["todo_local_id"] = task_id
    payload.setdefault("task_snapshot", {})["local_id"] = task_id
    payload["task_snapshot"] = snapshot
    payload.setdefault("task_snapshot", {})["local_id"] = task_id
    _write_json(_current_task_path(workspace), payload)
    _upsert_scheduled_todo_from_snapshot(
        workspace,
        snapshot,
        status=_TODO_STATUS_ACTIVE,
        session_id=session_id,
        started_at=started_at,
        sync_status="remote",
    )
    _write_memory_active(
        workspace,
        status="active",
        reminder_id=task_id,
        session_id=session_id,
        title=title,
        started_at=started_at,
        expected_until=expected_until,
        guidance="用户正在执行该 reminder。若超过预计完成时间仍未结束，heartbeat 会主动检查并提醒。",
    )
    return payload


def _cleanup_invalid_legacy_minute_jobs(user_cron: Any) -> list[str]:
    """Remove legacy every-minute jobs created by the old cron fallback bug."""
    if not user_cron:
        return []
    removed: list[str] = []
    try:
        jobs = user_cron.list_jobs(include_disabled=True)
    except Exception:
        return removed
    for job in jobs:
        metadata = getattr(job, "metadata", {}) or {}
        schedule = getattr(job, "schedule", None)
        if (
            not metadata
            and getattr(schedule, "kind", "") == "cron"
            and getattr(schedule, "expr", "") == "* * * * *"
        ):
            try:
                if user_cron.remove_job(job.id):
                    removed.append(job.id)
            except Exception:
                logger.debug("Failed to remove invalid legacy minute job {}", getattr(job, "id", ""))
    return removed


class ReminderClient:
    """Small HTTP client for Reminder Service."""

    def __init__(self, config: ReminderToolConfigData, workspace: Path):
        self.config = config
        self.workspace = workspace

    def auth_headers(self) -> tuple[dict[str, str] | None, str | None]:
        headers = {"Content-Type": "application/json"}
        token = self.config.bearer_token.strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
            return headers, None

        device_sn = self.config.device_sn.strip()
        if not device_sn:
            return None, _failure(
                "AUTH_CONFIG_MISSING",
                "Reminder App requires apps.reminder.deviceSn or apps.reminder.bearerToken.",
            )
        headers["X-Device-SN"] = device_sn
        if secret := self.config.device_secret.strip():
            headers["X-Device-Secret"] = secret
        return headers, None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> str:
        if not self.config.base_url.strip():
            return _failure("INVALID_ARGUMENT", "tools.reminder.baseUrl is not configured.")

        headers, auth_error = self.auth_headers()
        if auth_error:
            return auth_error

        url = f"{self.config.base_url.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds,
                verify=self.config.verify_ssl,
            ) as client:
                response = await client.request(
                    method,
                    url,
                    params=params or None,
                    json=body if body is not None else None,
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            return _failure("TIMEOUT", "Reminder Service request timed out.", retryable=True, details={"error": str(exc)})
        except httpx.TransportError as exc:
            return _failure("SERVICE_UNAVAILABLE", "Reminder Service is unavailable.", retryable=True, details={"error": str(exc)})

        try:
            data = response.json()
        except ValueError:
            return _failure(
                "INVALID_RESPONSE",
                "Reminder Service returned a non-JSON response.",
                details={"status_code": response.status_code, "body": response.text[:500]},
            )

        if response.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else None
            code = err.get("code", "INTERNAL") if isinstance(err, dict) else "INTERNAL"
            message = err.get("message", response.text[:500]) if isinstance(err, dict) else response.text[:500]
            return _failure(code, message, retryable=code == "INTERNAL", details={"status_code": response.status_code})

        return _success(data)


def _unwrap_tasks_payload(parsed: dict[str, Any]) -> list[Any]:
    """Return tasks from either wrapped client results or direct service results."""
    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
    tasks = data.get("tasks")
    if isinstance(tasks, list):
        return tasks

    nested_data = data.get("data") if isinstance(data.get("data"), dict) else {}
    tasks = nested_data.get("tasks")
    return tasks if isinstance(tasks, list) else []


async def refresh_today_todos_from_service(
    workspace: Path,
    config: ReminderToolConfigData,
    *,
    date: str | None = None,
    client: ReminderClient | None = None,
    user_cron: Any = None,
    reconcile: bool = True,
    mode: str = _REFRESH_MODE_INTERACTIVE,
) -> dict[str, Any]:
    """Merge scheduled Reminder Service tasks into the local daily todos file."""
    target_date = (date or datetime.now().astimezone().date().isoformat()).strip()
    if err := _validate_date(target_date):
        return json.loads(_failure("INVALID_ARGUMENT", err))

    reminder_client = client or ReminderClient(config, workspace)
    result = await reminder_client.request("GET", "/api/v1/reminder/tasks", params={"date": target_date})
    try:
        parsed = json.loads(result)
    except Exception:
        return json.loads(_failure("INVALID_RESPONSE", "Reminder Service returned invalid JSON.", details={"body": result[:500]}))
    if parsed.get("ok") is not True:
        return parsed

    tasks = _unwrap_tasks_payload(parsed)
    merged = 0
    for snapshot in tasks:
        if not isinstance(snapshot, dict):
            continue
        if _upsert_scheduled_todo_from_snapshot(workspace, snapshot, sync_status="remote"):
            merged += 1
    reconcile_result = None
    if reconcile and user_cron:
        reconcile_result = reconcile_reminder_todos_jobs(
            workspace,
            user_cron,
            date=target_date,
            mode=mode,
        )
    todos = ReminderStateStore(workspace).load_todos(target_date)
    return {
        "ok": True,
        "data": {
            "date": target_date,
            "merged_count": merged,
            "tasks_count": len([task for task in tasks if isinstance(task, dict)]),
            "todos_path": str(_reminder_todos_path(workspace, target_date)),
            "todos": todos,
            "reconcile": reconcile_result,
        },
        "error": None,
    }


class ReminderToolBase(Tool):
    """Base class shared by Reminder Service tools."""

    def __init__(self, config: ReminderToolConfigData, workspace: Path):
        self.config = config
        self.workspace = workspace
        self.client = ReminderClient(config, workspace)

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters


class ReminderListTodayTasksTool(ReminderToolBase):
    name = "reminder_list_today_tasks"
    _description = "Canonical reminder.list_today_tasks. List today's Reminder Service tasks for the current authenticated uid."
    _parameters = {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Date in YYYY-MM-DD format."},
        },
        "required": ["date"],
    }

    async def execute(self, date: str, **kwargs: Any) -> str:
        if err := _validate_date(date):
            return _failure("INVALID_ARGUMENT", err)
        return await self.client.request("GET", "/api/v1/reminder/tasks", params={"date": date})


class ReminderListTodosTool(Tool):
    name = "reminder_list_todos"
    description = (
        "List reminder todos for one day. When Reminder Service is configured, this refreshes from the service first. "
        "Use this before answering whether reminders are done today. "
        "After using it, answer the user in one or two short spoken Chinese sentences; do not render tables, "
        "numbered lists, markdown, emoji, or technical state details. If sync_error is present, mention that the latest "
        "service sync failed instead of claiming there are no tasks."
    )
    parameters = {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Date in YYYY-MM-DD format."},
        },
        "required": ["date"],
    }

    def __init__(
        self,
        workspace: Path,
        config: ReminderToolConfigData | None = None,
        user_cron_service: Any = None,
    ):
        self.workspace = workspace
        self.config = config
        self._user_cron = user_cron_service
        self.client = ReminderClient(config, workspace) if config and config.base_url.strip() else None

    async def execute(self, date: str, **kwargs: Any) -> str:
        if err := _validate_date(date):
            return _failure("INVALID_ARGUMENT", err)
        sync_error: dict[str, Any] | None = None
        reconcile_result: dict[str, Any] | None = None
        if self.config and self.config.base_url.strip():
            refreshed = await refresh_today_todos_from_service(
                self.workspace,
                self.config,
                date=date,
                client=self.client,
                user_cron=self._user_cron,
            )
            if refreshed.get("ok") is not True:
                sync_error = refreshed.get("error") if isinstance(refreshed.get("error"), dict) else {
                    "code": "SYNC_FAILED",
                    "message": "Reminder Service sync failed.",
                }
            else:
                refreshed_data = refreshed.get("data") if isinstance(refreshed.get("data"), dict) else {}
                reconcile = refreshed_data.get("reconcile")
                if isinstance(reconcile, dict):
                    reconcile_result = reconcile
        payload = ReminderStateStore(self.workspace).load_todos(date)
        open_items: list[dict[str, Any]] = []
        completed_items: list[dict[str, Any]] = []
        closed_items: list[dict[str, Any]] = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            compact = {
                "local_id": item.get("local_id"),
                "source": item.get("source"),
                "category": item.get("category"),
                "title": item.get("title"),
                "status": item.get("status"),
                "planned_start_at": item.get("planned_start_at"),
                "scheduled_for": item.get("scheduled_for"),
                "started_at": item.get("started_at"),
                "completed_at": item.get("completed_at"),
                "ended_at": item.get("ended_at"),
            }
            status = str(item.get("status") or "")
            if status == _TODO_STATUS_COMPLETED:
                completed_items.append(compact)
            elif status in _TODO_FINAL_STATUSES:
                closed_items.append(compact)
            else:
                open_items.append(compact)
        data = {
            "date": date,
            "todos_path": str(_reminder_todos_path(self.workspace, date)),
            "todos": payload,
            "summary": {
                "open_count": len(open_items),
                "completed_count": len(completed_items),
                "closed_count": len(closed_items),
                "open_items": open_items,
                "completed_items": completed_items,
                "closed_items": closed_items,
            },
        }
        if sync_error:
            data["sync_error"] = sync_error
        if reconcile_result:
            data["reconcile"] = reconcile_result
        return _success(data)


class ReminderRefreshTodayTodosTool(ReminderToolBase):
    name = "reminder_refresh_today_todos"
    _description = "Merge today's scheduled Reminder Service tasks into local reminder todos."
    _parameters = {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Date in YYYY-MM-DD format."},
        },
        "required": ["date"],
    }

    def __init__(
        self,
        config: ReminderToolConfigData,
        workspace: Path,
        user_cron_service: Any = None,
    ):
        super().__init__(config, workspace)
        self._user_cron = user_cron_service

    async def execute(self, date: str, **kwargs: Any) -> str:
        return _json(await refresh_today_todos_from_service(
            self.workspace,
            self.config,
            date=date,
            client=self.client,
            user_cron=self._user_cron,
        ))


class ReminderStartTaskTool(ReminderToolBase):
    name = "reminder_start_task"
    _description = "Canonical reminder.start_task. Start an existing planned reminder task."
    _parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Reminder task id to start."},
            "started_at": {"type": "string", "description": "Optional ISO datetime when the task started."},
        },
        "required": ["task_id"],
    }

    def __init__(
        self,
        config: ReminderToolConfigData,
        workspace: Path,
        user_cron_service: Any = None,
    ):
        super().__init__(config, workspace)
        self._user_cron = user_cron_service

    async def execute(self, task_id: str, started_at: str = "", **kwargs: Any) -> str:
        body: dict[str, Any] = {"task_id": task_id}
        if started_at:
            body["started_at"] = started_at
        result = await self.client.request("POST", "/api/v1/reminder/tasks/start", body=body)
        try:
            parsed = json.loads(result)
            if parsed.get("ok") is True and isinstance(parsed.get("data"), dict):
                data = parsed["data"]
                snapshot = _coerce_task_snapshot(data.get("task_snapshot"))
                session_id = str(data.get("session_id") or "").strip()
                if snapshot and session_id:
                    effective_started_at = started_at.strip() or _now_iso()
                    payload = _write_started_planned_task(
                        self.workspace,
                        snapshot=snapshot,
                        session_id=session_id,
                        started_at=effective_started_at,
                    )
                    reconcile_result = None
                    if self._user_cron:
                        reconcile_result = reconcile_reminder_todos_jobs(
                            self.workspace,
                            self._user_cron,
                            date=_date_from_iso(effective_started_at),
                        )
                    data["current_task_path"] = str(_current_task_path(self.workspace))
                    data["expected_until"] = payload.get("state", {}).get("expected_until", "")
                    data["reconcile"] = reconcile_result
                    return _success(data)
        except Exception:
            logger.exception("Failed to persist local current_task after reminder_start_task")
        return result


class ReminderSyncCurrentTaskTool(ReminderToolBase):
    name = "reminder_sync_current_task"
    _description = "Canonical reminder.sync_current_task. Sync current_task.json payload to Reminder Service."
    _parameters = {
        "type": "object",
        "properties": {
            "payload": {"type": "object", "description": "Full current_task.json payload."},
        },
        "required": ["payload"],
    }

    async def execute(self, payload: dict[str, Any], **kwargs: Any) -> str:
        payload, error = _load_current_task_payload(self.workspace, "", payload)
        if error or payload is None:
            return _failure(
                "INVALID_ARGUMENT",
                error or "payload must be the full current_task.json object.",
            )
        try:
            normalized = _normalize_current_task_payload(payload)
        except ValueError as exc:
            return _failure("INVALID_ARGUMENT", str(exc))
        return await self.client.request("POST", "/api/v1/reminder/tasks/sync", body={"payload": normalized})


class ReminderGetResumableTaskTool(ReminderToolBase):
    name = "reminder_get_resumable_task"
    _description = "Canonical reminder.get_resumable_task. Find a same-day resumable reminder task."
    _parameters = {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Date in YYYY-MM-DD format."},
            "query": {"type": "string", "description": "Optional keyword to match reminder title/content."},
        },
        "required": ["date"],
    }

    async def execute(self, date: str, query: str = "", **kwargs: Any) -> str:
        if err := _validate_date(date):
            return _failure("INVALID_ARGUMENT", err)
        return await self.client.request(
            "GET",
            "/api/v1/reminder/tasks/resumable",
            params={"date": date, "query": query},
        )


class ReminderDeferTaskTool(ReminderToolBase):
    name = "reminder_defer_task"
    _description = "Canonical reminder.defer_task. Defer an unfinished reminder task after user confirmation."
    _parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Reminder task id to defer."},
            "payload": {"type": "object", "description": "Full current_task.json payload."},
            "next_planned_at": {"type": "string", "description": "Optional ISO datetime for the deferred task."},
            "reason": {"type": "string", "description": "User-confirmed defer reason."},
        },
        "required": ["task_id", "payload"],
    }

    async def execute(
        self,
        task_id: str,
        payload: dict[str, Any],
        next_planned_at: str = "",
        reason: str = "",
        **kwargs: Any,
    ) -> str:
        payload, error = _load_current_task_payload(self.workspace, task_id, payload)
        if error or payload is None:
            return _failure("INVALID_ARGUMENT", error or "payload must be the full current_task.json object.")
        try:
            normalized = _normalize_current_task_payload(
                payload,
                task_id=task_id,
                end_result="deferred",
                reason=reason,
            )
        except ValueError as exc:
            return _failure("INVALID_ARGUMENT", str(exc))
        body: dict[str, Any] = {"task_id": task_id, "payload": normalized, "reason": reason}
        if next_planned_at:
            body["next_planned_at"] = next_planned_at
        return await self.client.request("POST", "/api/v1/reminder/tasks/defer", body=body)


class ReminderCompleteTaskTool(ReminderToolBase):
    name = "reminder_complete_task"
    _description = (
        "Complete a reminder business item by current state, task_id, or title. "
        "Use this for '做完了', '完成了', '已经好了'. It reconciles local ad-hoc state "
        "and Reminder Service planned tasks; do not compose multiple state-changing tools manually."
    )
    _parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Optional Reminder Service task id to complete."},
            "title": {"type": "string", "description": "Optional reminder title/query, e.g. 数学作业."},
            "completed_at": {"type": "string", "description": "Optional ISO datetime when completed."},
            "date": {"type": "string", "description": "Optional YYYY-MM-DD date for planned task lookup."},
            "reason": {"type": "string", "description": "Completion reason or user confirmation."},
        },
    }

    def __init__(
        self,
        config: ReminderToolConfigData,
        workspace: Path,
        user_cron_service: Any = None,
    ):
        super().__init__(config, workspace)
        self._user_cron = user_cron_service

    async def _complete_current(self, completed_at: str, reason: str, *, sync_ad_hoc: bool = True) -> dict[str, Any]:
        return await _complete_current_reminder(
            client=self.client,
            workspace=self.workspace,
            user_cron=self._user_cron,
            completed_at=completed_at,
            reason=reason,
            sync_ad_hoc=sync_ad_hoc,
            service_enabled=bool(self.config.base_url.strip()),
        )

    async def _find_planned_snapshot(
        self,
        *,
        task_id: str,
        title: str,
        date: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        result = await self.client.request("GET", "/api/v1/reminder/tasks", params={"date": date})
        try:
            parsed = json.loads(result)
        except Exception:
            return None, result
        if parsed.get("ok") is not True:
            error = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
            return None, str(error.get("message") or result)
        data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
        tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
        for item in tasks:
            if isinstance(item, dict) and task_id and str(item.get("id") or item.get("ID") or "") == task_id:
                return item, None
        matches: list[dict[str, Any]] = []
        for item in tasks:
            if not isinstance(item, dict):
                continue
            item_title = str(item.get("title") or item.get("Title") or "").strip()
            if title and _title_matches(title, item_title):
                matches.append(item)
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, "ambiguous planned reminder task"
        return None, "planned reminder task not found"

    async def execute(
        self,
        task_id: str = "",
        title: str = "",
        completed_at: str = "",
        date: str = "",
        reason: str = "",
        **kwargs: Any,
    ) -> str:
        task_id = task_id.strip()
        title = title.strip()
        reason = reason.strip() or "用户确认完成"
        completed_at = completed_at.strip() or _now_iso()
        try:
            _parse_iso_ms(completed_at)
        except ValueError as exc:
            return _failure("INVALID_ARGUMENT", f"completed_at must be ISO datetime with timezone: {exc}")
        lookup_date = date.strip() or completed_at[:10]
        if err := _validate_date(lookup_date):
            return _failure("INVALID_ARGUMENT", err)

        current = _current_reminder(self.workspace)
        if _is_current_task_payload(current) and _current_status(current) in {"active", "paused", "pending_sync"}:
            current_task_id, current_session_id = _current_ids(current)
            current_title = _current_title(current)
            should_complete_current = (
                not task_id and not title
                or bool(task_id and task_id == current_task_id)
                or bool(title and _title_matches(title, current_title))
            )
            if should_complete_current:
                return _json(await self._complete_current(
                    completed_at,
                    reason,
                    sync_ad_hoc=True,
                ))
            return _failure(
                "CURRENT_REMINDER_ACTIVE",
                "A different reminder is active. Complete or update the current reminder before completing another task.",
                details={
                    "current_task_id": current_task_id,
                    "current_session_id": current_session_id,
                    "current_title": current_title,
                    "requested_task_id": task_id,
                    "requested_title": title,
                },
            )

        if not task_id and not title:
            return _failure(
                "NO_ACTIVE_REMINDER",
                "No active reminder matched, and no task_id/title was provided.",
            )

        snapshot, lookup_error = await self._find_planned_snapshot(
            task_id=task_id,
            title=title,
            date=lookup_date,
        )
        if snapshot is None:
            if lookup_error == "ambiguous planned reminder task":
                return _failure(
                    "AMBIGUOUS_REMINDER",
                    "Multiple reminders match the requested title. Ask which one to complete before changing state.",
                    details={"title": title, "date": lookup_date},
                )
            return _failure(
                "TASK_NOT_FOUND",
                lookup_error or "planned reminder task not found",
                details={"task_id": task_id, "title": title, "date": lookup_date},
            )

        planned_payload = _build_planned_completion_payload(
            self.workspace,
            snapshot=snapshot,
            completed_at=completed_at,
            reason=reason,
        )
        planned_task_id = _task_id_from_payload(planned_payload)
        try:
            normalized = _normalize_current_task_payload(
                planned_payload,
                task_id=planned_task_id,
                end_result="completed",
                reason=reason,
            )
        except ValueError as exc:
            return _failure("INVALID_ARGUMENT", str(exc))

        result = await self.client.request(
            "POST",
            "/api/v1/reminder/tasks/sync",
            body={"payload": normalized},
        )
        try:
            parsed = json.loads(result)
        except Exception:
            parsed = {}

        session_id = str(planned_payload.get("session", {}).get("id") or "")
        title_for_memory = str(snapshot.get("title") or snapshot.get("Title") or title or "当前提醒")
        if parsed.get("ok") is True:
            sync = planned_payload.setdefault("sync", {})
            if isinstance(sync, dict):
                sync["status"] = "synced"
                sync["last_sync_at"] = _now_iso()
                sync["sync_error"] = None
            _upsert_scheduled_todo_from_snapshot(
                self.workspace,
                snapshot,
                status=_TODO_STATUS_COMPLETED,
                session_id=session_id,
                started_at=str(planned_payload.get("session", {}).get("started_at") or ""),
                ended_at=completed_at,
                sync_status="synced",
            )
            related_closed_wakeups = _close_related_pending_todos(
                self.workspace,
                self._user_cron,
                title=title_for_memory,
                occurred_at=completed_at,
                reason="related reminder already completed",
            )
            _write_json(_current_task_path(self.workspace), planned_payload)
            _write_memory_none(
                self.workspace,
                reminder_id=planned_task_id,
                session_id=session_id,
                result="completed",
                updated_at=completed_at,
            )
            data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
            data.update({
                "reminder_id": planned_task_id,
                "reminder_session_id": session_id,
                "status": "completed",
                "source": "reminder_service_planned",
                "current_task_path": str(_current_task_path(self.workspace)),
                "memory_path": str(_memory_path(self.workspace)),
                "cancelled_cron_job_ids": related_closed_wakeups,
            })
            return _success(data)

        error = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
        message = str(error.get("message") or result)
        sync = planned_payload.setdefault("sync", {})
        if isinstance(sync, dict):
            sync["status"] = "pending_sync"
            sync["sync_error"] = message
        state = planned_payload.setdefault("state", {})
        if isinstance(state, dict):
            state["status"] = "pending_sync"
        _upsert_scheduled_todo_from_snapshot(
            self.workspace,
            snapshot,
            status=_TODO_STATUS_COMPLETED,
            session_id=session_id,
            started_at=str(planned_payload.get("session", {}).get("started_at") or ""),
            ended_at=completed_at,
            sync_status="pending_sync",
            sync_error=message[:300],
        )
        _write_json(_current_task_path(self.workspace), planned_payload)
        _write_memory_pending_sync(
            self.workspace,
            reminder_id=planned_task_id,
            session_id=session_id,
            title=title_for_memory,
            sync_error=message[:300],
        )
        return result


class ReminderStartAdHocTaskTool(Tool):
    """Create a local ad-hoc reminder session without backend changes."""

    name = "reminder_start_ad_hoc_task"
    description = (
        "Start a local ad-hoc reminder session. Use this for unplanned reminders such as "
        "'我要写字了，15分钟做完'. Do not use reminder_start_task for ad-hoc reminders."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short reminder title."},
            "category": {
                "type": "string",
                "description": "Reminder category. Use one of: study, reminder, habit, other. Infer it from the user's intent when absent.",
            },
            "started_at": {"type": "string", "description": "Optional ISO datetime with timezone."},
            "expected_minutes": {"type": "integer", "description": "Expected duration in minutes."},
            "notes": {"type": "string", "description": "Original user context or guidance."},
            "completion_message": {"type": "string", "description": "Optional completion-check wakeup message."},
            "replace_current": {
                "type": "boolean",
                "description": "Set true only after the user confirms switching away from an unexpired active ad-hoc reminder.",
            },
            "allow_new_when_planned_exists": {
                "type": "boolean",
                "description": "Set true only when the user explicitly says this is a separate new reminder, not the matching planned reminder.",
            },
        },
        "required": ["title"],
    }

    def __init__(self, workspace: Path, user_cron_service: Any = None):
        self.workspace = workspace
        self._user_cron = user_cron_service

    async def execute(
        self,
        title: str,
        category: str = "",
        started_at: str = "",
        expected_minutes: int | None = None,
        notes: str = "",
        completion_message: str = "",
        replace_current: bool = False,
        allow_new_when_planned_exists: bool = False,
        **kwargs: Any,
    ) -> str:
        title = title.strip()
        if not title:
            return _failure("INVALID_ARGUMENT", "title is required.")
        category = _normalize_reminder_category(category, title=title, content=notes or title)
        if expected_minutes is not None and expected_minutes <= 0:
            return _failure("INVALID_ARGUMENT", "expected_minutes must be greater than 0.")

        started_at = started_at.strip() or _now_iso()
        try:
            _parse_iso_ms(started_at)
        except ValueError as exc:
            return _failure("INVALID_ARGUMENT", f"started_at must be ISO datetime with timezone: {exc}")

        cleaned_jobs = _cleanup_invalid_legacy_minute_jobs(self._user_cron)
        existing = _current_reminder(self.workspace)
        existing_status = _current_status(existing)
        ended_previous: dict[str, Any] | None = None
        if existing_status == "active":
            if not _is_ad_hoc_current_task(existing):
                return _failure(
                    "CURRENT_REMINDER_ACTIVE",
                    "A planned reminder is active. Complete or defer it before starting a new ad-hoc reminder.",
                    details={"current": existing, "cleaned_legacy_cron_job_ids": cleaned_jobs},
                )
            if _is_current_overdue(existing, started_at):
                ended_previous = _finish_current_reminder_locally(
                    self.workspace,
                    self._user_cron,
                    existing,
                    ended_at=_current_expected_until(existing) or started_at,
                    reason="expected_until elapsed before starting a new reminder",
                    result="closed",
                )
            elif replace_current:
                ended_previous = _finish_current_reminder_locally(
                    self.workspace,
                    self._user_cron,
                    existing,
                    ended_at=started_at,
                    reason="user confirmed switching to a new reminder",
                    result="interrupted",
                )
            else:
                return _failure(
                    "CURRENT_REMINDER_ACTIVE",
                    "An active reminder already exists. Confirm interruption or completion before starting a new ad-hoc reminder.",
                    details={"current": existing, "cleaned_legacy_cron_job_ids": cleaned_jobs},
                )
        else:
            planned_todo = _find_scheduled_todo_conflict_by_title(
                self.workspace,
                title=title,
                now_iso=started_at,
                include_recent_final=True,
            )
            if planned_todo and planned_todo.get("remote_task_id") and not allow_new_when_planned_exists:
                return _failure(
                    "PLANNED_REMINDER_CONFLICT",
                    "A matching planned reminder exists. Use reminder_start_task with remote_task_id instead of creating an ad-hoc reminder, unless the user explicitly wants a separate new reminder.",
                    details={
                        "remote_task_id": planned_todo.get("remote_task_id"),
                        "local_id": planned_todo.get("local_id"),
                        "title": planned_todo.get("title"),
                        "planned_start_at": planned_todo.get("planned_start_at"),
                        "status": planned_todo.get("status"),
                    },
                )

        reminder_id = f"adhoc_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        session_id = f"sess_{uuid.uuid4().hex[:10]}"
        expected_until = ""
        if expected_minutes:
            expected_until = (datetime.fromisoformat(started_at) + timedelta(minutes=expected_minutes)).isoformat()

        payload = _build_local_payload(
            reminder_id=reminder_id,
            session_id=session_id,
            title=title,
            status="active",
            started_at=started_at,
            expected_until=expected_until,
            expected_minutes=expected_minutes,
            notes=notes,
            category=category,
        )
        payload.setdefault("state", {})["todo_local_id"] = reminder_id
        payload.setdefault("task_snapshot", {})["local_id"] = reminder_id

        wakeups: list[dict[str, Any]] = []
        if expected_minutes:
            try:
                wakeup = _schedule_structured_wakeup(
                    self._user_cron,
                    kind="completion_check",
                    message=completion_message or f"{title}的预计时间到了，完成了吗？",
                    task_id=reminder_id,
                    session_id=session_id,
                    after_seconds=expected_minutes * 60,
                )
                wakeups.append(wakeup)
                payload["wakeups"].append(wakeup["metadata"])
            except Exception as exc:
                payload["sync"]["sync_error"] = f"wakeup_schedule_failed: {exc}"

        todo = ReminderStateStore(self.workspace).upsert_todo(
            _date_from_iso(started_at),
            {
                "local_id": reminder_id,
                "source": _TODO_SOURCE_AD_HOC,
                "category": category,
                "title": title,
                "content": notes or title,
                "status": _TODO_STATUS_ACTIVE,
                "planned_start_at": started_at,
                "expected_until": expected_until,
                "started_at": started_at,
                "current_task_path": str(_current_task_path(self.workspace)),
                "cron_job_ids": [wakeup["cron_job_id"] for wakeup in wakeups if wakeup.get("cron_job_id")],
                "events": [{"type": "started", "at": started_at, "reason": notes or "用户开始"}],
                "sync": payload.get("sync") if isinstance(payload.get("sync"), dict) else _todo_sync("local_only"),
            },
        )
        _write_json(_current_task_path(self.workspace), payload)
        _write_memory_active(
            self.workspace,
            status="active",
            reminder_id=reminder_id,
            session_id=session_id,
            title=title,
            started_at=started_at,
            expected_until=expected_until,
            wakeups=wakeups,
            guidance="用户正在执行该 reminder。处理新输入时先判断是否与当前 reminder 相关；若明显无关，应温和提醒当前 reminder 仍在进行，并询问是否需要暂停或切换。",
        )

        return _success({
            "reminder_id": reminder_id,
            "reminder_session_id": session_id,
            "status": "active",
            "current_task_path": str(_current_task_path(self.workspace)),
            "memory_path": str(_memory_path(self.workspace)),
            "todos_path": str(_reminder_todos_path(self.workspace, _date_from_iso(started_at))),
            "todo_local_id": todo["local_id"],
            "expected_until": expected_until,
            "wakeups": wakeups,
            "cleaned_legacy_cron_job_ids": cleaned_jobs,
            "ended_previous_reminder": ended_previous,
        })


class ReminderScheduleOneOffTool(Tool):
    """Schedule an independent one-off reminder without touching current_task."""

    name = "reminder_schedule_one_off"
    description = (
        "Schedule an independent one-off reminder, such as '1分钟后提醒我吃药'. "
        "Use this instead of schedule_task_wakeup when the reminder is not part of the active current reminder."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short reminder title."},
            "category": {
                "type": "string",
                "description": "Reminder category. Use one of: study, reminder, habit, other. Infer it from the user's intent when absent.",
            },
            "message": {"type": "string", "description": "Short spoken reminder message."},
            "at": {"type": "string", "description": "ISO datetime for one-time reminder."},
            "after_seconds": {"type": "integer", "description": "Relative seconds until reminder."},
            "notes": {"type": "string", "description": "Original user context."},
        },
        "required": ["title"],
    }

    def __init__(self, workspace: Path, user_cron_service: Any = None):
        self.workspace = workspace
        self._user_cron = user_cron_service

    async def execute(
        self,
        title: str,
        category: str = "",
        message: str = "",
        at: str = "",
        after_seconds: int | None = None,
        notes: str = "",
        **kwargs: Any,
    ) -> str:
        title = title.strip()
        message = message.strip() or title
        if not title:
            return _failure("INVALID_ARGUMENT", "title is required.")
        category = _normalize_reminder_category(category, title=title, content=message or notes or title)
        if not self._user_cron:
            return _failure("SERVICE_UNAVAILABLE", "UserCron service is not available for one-off reminders.")
        if not at and after_seconds is None:
            return _failure("INVALID_ARGUMENT", "Either at or after_seconds is required.")
        if after_seconds is not None and after_seconds <= 0:
            return _failure("INVALID_ARGUMENT", "after_seconds must be greater than 0.")

        scheduled_for = at or (_iso_after_seconds(after_seconds) if after_seconds is not None else "")
        try:
            _parse_iso_ms(scheduled_for)
        except ValueError as exc:
            return _failure("INVALID_ARGUMENT", f"at must be ISO datetime with timezone: {exc}")

        local_id = f"oneoff_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        date = _date_from_iso(scheduled_for)
        try:
            wakeup = _schedule_one_off_wakeup(
                self._user_cron,
                local_id=local_id,
                date=date,
                title=title,
                message=message,
                category=category,
                at=at,
                after_seconds=after_seconds,
            )
        except ValueError as exc:
            return _failure("INVALID_ARGUMENT", str(exc))
        except Exception as exc:
            return _failure("INTERNAL", str(exc), retryable=True)

        todo = ReminderStateStore(self.workspace).upsert_todo(
            date,
            {
                "local_id": local_id,
                "source": _TODO_SOURCE_ONE_OFF,
                "category": category,
                "title": title,
                "content": message or notes or title,
                "status": _TODO_STATUS_SCHEDULED,
                "scheduled_for": wakeup["scheduled_for"],
                "expected_until": wakeup["scheduled_for"],
                "cron_job_ids": [wakeup["cron_job_id"]],
                "events": [{"type": "scheduled", "at": _now_iso(), "reason": notes or message}],
                "sync": _todo_sync("local_only"),
            },
        )
        return _success({
            "local_id": local_id,
            "status": _TODO_STATUS_SCHEDULED,
            "scheduled_for": wakeup["scheduled_for"],
            "cron_job_id": wakeup["cron_job_id"],
            "todos_path": str(_reminder_todos_path(self.workspace, date)),
            "todo": todo,
        })


class ReminderScheduleStartPromptTool(Tool):
    """Schedule a future prompt asking whether the user is ready to start."""

    name = "reminder_schedule_start_prompt"
    description = (
        "Schedule a pending-start reminder. Use this for requests like "
        "'3分钟以后我要开始写作业'; it should ask the user to confirm starting later."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short reminder title."},
            "category": {
                "type": "string",
                "description": "Reminder category. Use one of: study, reminder, habit, other. Infer it from the user's intent when absent.",
            },
            "message": {"type": "string", "description": "Prompt to send when it is time to start."},
            "at": {"type": "string", "description": "ISO datetime for the start prompt."},
            "after_seconds": {"type": "integer", "description": "Relative seconds until the start prompt."},
            "expected_minutes": {"type": "integer", "description": "Optional duration after the user confirms start."},
            "notes": {"type": "string", "description": "Original user context or guidance."},
        },
        "required": ["title"],
    }

    def __init__(self, workspace: Path, user_cron_service: Any = None):
        self.workspace = workspace
        self._user_cron = user_cron_service

    async def execute(
        self,
        title: str,
        category: str = "",
        message: str = "",
        at: str = "",
        after_seconds: int | None = None,
        expected_minutes: int | None = None,
        notes: str = "",
        **kwargs: Any,
    ) -> str:
        title = title.strip()
        if not title:
            return _failure("INVALID_ARGUMENT", "title is required.")
        category = _normalize_reminder_category(category, title=title, content=notes or message or title)
        if expected_minutes is not None and expected_minutes <= 0:
            return _failure("INVALID_ARGUMENT", "expected_minutes must be greater than 0.")

        cleaned_jobs = _cleanup_invalid_legacy_minute_jobs(self._user_cron)
        existing = _current_reminder(self.workspace)
        if _current_status(existing) == "active":
            return _failure(
                "CURRENT_REMINDER_ACTIVE",
                "An active reminder already exists. Confirm interruption or completion before scheduling a new start prompt.",
                details={"current": existing, "cleaned_legacy_cron_job_ids": cleaned_jobs},
            )

        reminder_id = f"pending_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        session_id = f"sess_{uuid.uuid4().hex[:10]}"
        scheduled_for = at or (_iso_after_seconds(after_seconds) if after_seconds is not None else "")
        if not scheduled_for:
            return _failure("INVALID_ARGUMENT", "Either at or after_seconds is required.")

        payload = _build_local_payload(
            reminder_id=reminder_id,
            session_id=session_id,
            title=title,
            status="pending_start",
            expected_minutes=expected_minutes,
            notes=notes,
            source_type="pending_start",
            category=category,
        )
        payload["state"]["start_prompt_at"] = scheduled_for
        payload["state"]["todo_local_id"] = reminder_id
        payload.setdefault("task_snapshot", {})["local_id"] = reminder_id

        try:
            wakeup = _schedule_structured_wakeup(
                self._user_cron,
                kind="start_prompt",
                message=message or f"现在要开始{title}了吗？",
                task_id=reminder_id,
                session_id=session_id,
                at=at,
                after_seconds=after_seconds,
            )
        except ValueError as exc:
            return _failure("INVALID_ARGUMENT", str(exc))
        except Exception as exc:
            return _failure("INTERNAL", str(exc), retryable=True)

        payload["wakeups"].append(wakeup["metadata"])
        todo = ReminderStateStore(self.workspace).upsert_todo(
            _date_from_iso(scheduled_for),
            {
                "local_id": reminder_id,
                "source": _TODO_SOURCE_PENDING_START,
                "category": category,
                "title": title,
                "content": notes or title,
                "status": _TODO_STATUS_PENDING_START,
                "scheduled_for": scheduled_for,
                "expected_until": scheduled_for,
                "current_task_path": str(_current_task_path(self.workspace)),
                "cron_job_ids": [wakeup["cron_job_id"]],
                "events": [{"type": "start_prompt_scheduled", "at": _now_iso(), "reason": notes or "用户安排稍后开始"}],
                "sync": _todo_sync("local_only"),
            },
        )
        _write_json(_current_task_path(self.workspace), payload)
        _write_memory_active(
            self.workspace,
            status="pending_start",
            reminder_id=reminder_id,
            session_id=session_id,
            title=title,
            expected_until=scheduled_for,
            wakeups=[wakeup],
            guidance="用户有一个待开始的 reminder。到点后应询问用户是否现在开始；用户确认后调用 reminder_start_ad_hoc_task，而不是直接启动倒计时。",
        )

        return _success({
            "reminder_id": reminder_id,
            "reminder_session_id": session_id,
            "status": "pending_start",
            "start_prompt_at": wakeup["scheduled_for"],
            "current_task_path": str(_current_task_path(self.workspace)),
            "memory_path": str(_memory_path(self.workspace)),
            "todos_path": str(_reminder_todos_path(self.workspace, _date_from_iso(scheduled_for))),
            "todo_local_id": todo["local_id"],
            "wakeup": wakeup,
            "cleaned_legacy_cron_job_ids": cleaned_jobs,
        })


class ReminderUpdateTaskTool(Tool):
    """Update the local execution plan for the current active reminder."""

    name = "reminder_update_task"
    description = (
        "Update the current active reminder locally, including title/content and expected finish time. "
        "Use this for extending or changing the active task plan; it replaces the previous local completion_check wakeup."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Optional current Reminder Service task id."},
            "title": {"type": "string", "description": "Optional updated task title."},
            "content": {"type": "string", "description": "Optional updated task content or notes."},
            "expected_until": {"type": "string", "description": "Optional ISO datetime for the updated local expected finish time."},
            "extend_seconds": {"type": "integer", "description": "Optional seconds from now to set the next completion check."},
            "expected_minutes": {"type": "integer", "description": "Optional total minutes from task start to expected finish."},
            "message": {"type": "string", "description": "Optional completion check message."},
            "reason": {"type": "string", "description": "Reason for the local task update."},
        },
        "required": [],
    }

    def __init__(self, workspace: Path, user_cron_service: Any = None):
        self.workspace = workspace
        self._user_cron = user_cron_service

    @staticmethod
    def _iso_from_minutes(started_at: str, minutes: int) -> str:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        return (started + timedelta(minutes=minutes)).astimezone().isoformat()

    async def execute(
        self,
        task_id: str = "",
        title: str = "",
        content: str = "",
        expected_until: str = "",
        extend_seconds: int | None = None,
        expected_minutes: int | None = None,
        message: str = "",
        reason: str = "",
        **kwargs: Any,
    ) -> str:
        _ = kwargs
        current = _current_reminder(self.workspace)
        if not _is_current_task_payload(current):
            return _failure("NO_ACTIVE_REMINDER", "No active reminder to update.")
        status = _current_status(current)
        if status not in _CURRENT_ACTIVE_STATUSES:
            return _failure("NO_ACTIVE_REMINDER", "No active reminder to update.", details={"status": status or "none"})

        current_task_id, session_id = _current_ids(current)
        task_id = task_id.strip()
        if task_id and current_task_id and task_id != current_task_id:
            return _failure("REMINDER_MISMATCH", "task_id does not match current reminder.")
        if not current_task_id or not session_id:
            return _failure("INVALID_ARGUMENT", "current reminder must include task and session ids.")

        state = current.setdefault("state", {})
        session = current.setdefault("session", {})
        snapshot = current.setdefault("task_snapshot", {})
        if not isinstance(state, dict) or not isinstance(session, dict) or not isinstance(snapshot, dict):
            return _failure("INVALID_ARGUMENT", "current reminder payload is invalid.")

        now = datetime.now().astimezone()
        now_iso = now.isoformat()
        old_expected_until = _current_expected_until(current)
        new_expected_until = expected_until.strip()
        if extend_seconds is not None:
            if extend_seconds <= 0:
                return _failure("INVALID_ARGUMENT", "extend_seconds must be greater than 0.")
            new_expected_until = (now + timedelta(seconds=extend_seconds)).isoformat()
        elif expected_minutes is not None:
            if expected_minutes <= 0:
                return _failure("INVALID_ARGUMENT", "expected_minutes must be greater than 0.")
            started_at = str(state.get("started_at") or session.get("started_at") or "").strip()
            if not started_at:
                return _failure("INVALID_ARGUMENT", "current reminder must include started_at for expected_minutes.")
            try:
                new_expected_until = self._iso_from_minutes(started_at, expected_minutes)
            except Exception as exc:
                return _failure("INVALID_ARGUMENT", f"started_at must be ISO datetime: {exc}")

        if new_expected_until:
            try:
                _parse_iso_ms(new_expected_until)
            except ValueError as exc:
                return _failure("INVALID_ARGUMENT", f"expected_until must be ISO datetime with timezone: {exc}")
        else:
            new_expected_until = old_expected_until

        new_title = title.strip()
        new_content = content.strip()
        if new_title:
            snapshot["title"] = new_title
        if new_content:
            snapshot["content"] = new_content
            session["task_ctx"] = new_content
        if new_expected_until:
            state["expected_until"] = new_expected_until
            snapshot["planned_end_at"] = new_expected_until
            started_at = str(state.get("started_at") or session.get("started_at") or "").strip()
            if started_at:
                try:
                    seconds = max(0, (_parse_iso_ms(new_expected_until) - _parse_iso_ms(started_at)) // 1000)
                    snapshot["estimated_duration_minutes"] = max(1, (seconds + 59) // 60)
                except Exception:
                    pass
        state["last_activity_at"] = now_iso
        current["sync"] = _todo_sync("pending_sync")

        removed_ids: list[str] = []
        wakeup: dict[str, Any] | None = None
        if new_expected_until:
            if not self._user_cron:
                return _failure("SERVICE_UNAVAILABLE", "UserCron service is not available for task wakeups.")
            removed_ids = _remove_matching_completion_jobs(
                self._user_cron,
                task_id=current_task_id,
                session_id=session_id,
            )
            if _parse_iso_ms(new_expected_until) > int(time.time() * 1000):
                wakeup = _schedule_structured_wakeup(
                    self._user_cron,
                    kind="completion_check",
                    message=message.strip() or f"{_current_title(current)}的预计时间到了，完成了吗？",
                    task_id=current_task_id,
                    session_id=session_id,
                    at=new_expected_until,
                )
            _set_current_completion_wakeup(current, wakeup["metadata"] if wakeup else None)

        event_type = "task_time_extended" if new_expected_until and new_expected_until != old_expected_until else "task_updated"
        event = {
            "type": event_type,
            "at": now_iso,
            "reason": reason.strip() or "用户更新任务",
            "metadata": {
                "local_override": True,
                "old_expected_until": old_expected_until,
                "new_expected_until": new_expected_until,
                "removed_cron_job_ids": removed_ids,
                "created_cron_job_id": wakeup["cron_job_id"] if wakeup else "",
            },
        }
        if new_title:
            event["metadata"]["title"] = new_title
        if new_content:
            event["metadata"]["content"] = new_content
        current.setdefault("events", [])
        if isinstance(current["events"], list):
            current["events"].append(event)
        _write_json(_current_task_path(self.workspace), current)

        date = _current_todo_date(current, new_expected_until or now_iso)
        local_id = _current_todo_local_id(current) or current_task_id
        todo_updates = {
            "status": _TODO_STATUS_ACTIVE,
            "title": str(snapshot.get("title") or _current_title(current)),
            "content": str(session.get("task_ctx") or snapshot.get("content") or _current_title(current)),
            "expected_until": new_expected_until,
            "current_task_path": str(_current_task_path(self.workspace)),
            "cron_job_ids": [wakeup["cron_job_id"]] if wakeup else [],
            "sync": _todo_sync("pending_sync"),
        }
        todo = ReminderStateStore(self.workspace).update_todo(
            date,
            local_id=local_id,
            remote_task_id=current_task_id,
            title=str(snapshot.get("title") or ""),
            source=_current_todo_source(current),
            updates=todo_updates,
            event=event,
            create_item=_current_todo_create_item(current),
        )
        return _success({
            "task_id": current_task_id,
            "session_id": session_id,
            "expected_until": new_expected_until,
            "event": event,
            "removed_cron_job_ids": removed_ids,
            "cron_job_id": wakeup["cron_job_id"] if wakeup else "",
            "scheduled_for": wakeup["scheduled_for"] if wakeup else "",
            "current_task_path": str(_current_task_path(self.workspace)),
            "todo": todo,
        })


class ScheduleTaskWakeupTool(Tool):
    """Schedule a structured wakeup routed back into the Agent."""

    name = "schedule_task_wakeup"
    description = "Schedule a structured task_wakeup for an active reminder session."
    parameters = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "Wakeup kind, e.g. break_reminder."},
            "message": {"type": "string", "description": "Message the Agent should send if the reminder is still active."},
            "task_id": {"type": "string", "description": "Reminder Service task id."},
            "session_id": {"type": "string", "description": "Reminder session id."},
            "at": {"type": "string", "description": "ISO datetime for one-time wakeup."},
            "after_seconds": {"type": "integer", "description": "Schedule relative wakeup in seconds."},
            "wakeup_id": {"type": "string", "description": "Optional stable wakeup id."},
            "diagnostic": {"type": "boolean", "description": "Set true only for tool connectivity tests without an active reminder."},
        },
        "required": ["kind", "message"],
    }

    def __init__(self, user_cron_service: Any = None, workspace: Path | None = None):
        self._user_cron = user_cron_service
        self.workspace = workspace

    async def execute(
        self,
        kind: str,
        message: str,
        task_id: str = "",
        session_id: str = "",
        at: str = "",
        after_seconds: int | None = None,
        wakeup_id: str = "",
        diagnostic: bool = False,
        **kwargs: Any,
    ) -> str:
        if not self._user_cron:
            return _failure("SERVICE_UNAVAILABLE", "UserCron service is not available for task wakeups.")

        current: dict[str, Any] | None = None
        if not diagnostic:
            if not self.workspace:
                return _failure("INVALID_ARGUMENT", "workspace is required for non-diagnostic task wakeups.")
            current = _current_reminder(self.workspace)
            status = _current_status(current)
            if status not in ("active", "paused", "pending_start"):
                return _failure(
                    "NO_ACTIVE_REMINDER",
                    "No active reminder. Start an ad-hoc or planned reminder before scheduling a business wakeup.",
                    details={"status": status or "none"},
                )
            current_task_id, current_session_id = _current_ids(current or {})
            if task_id and current_task_id and task_id != current_task_id:
                return _failure("REMINDER_MISMATCH", "task_id does not match current reminder.")
            if session_id and current_session_id and session_id != current_session_id:
                return _failure("REMINDER_MISMATCH", "session_id does not match current reminder session.")
            task_id = task_id or current_task_id
            session_id = session_id or current_session_id

        existing_wakeup = None
        if kind == "completion_check" or wakeup_id:
            existing_wakeup = _find_existing_structured_wakeup(
                self._user_cron,
                kind=kind,
                task_id=task_id,
                session_id=session_id,
                wakeup_id=wakeup_id,
            )
        if existing_wakeup:
            if current and self.workspace:
                if _append_wakeup_once(current, existing_wakeup["metadata"]):
                    _write_json(_current_task_path(self.workspace), current)
            return _success({
                "wakeup_id": existing_wakeup["wakeup_id"],
                "cron_job_id": existing_wakeup["cron_job_id"],
                "kind": existing_wakeup["kind"],
                "task_id": existing_wakeup["task_id"],
                "session_id": existing_wakeup["session_id"],
                "scheduled_for": existing_wakeup["scheduled_for"],
                "diagnostic": diagnostic,
                "deduped": True,
            })

        try:
            wakeup = _schedule_structured_wakeup(
                self._user_cron,
                kind=kind,
                message=message,
                task_id=task_id,
                session_id=session_id,
                at=at,
                after_seconds=after_seconds,
                wakeup_id=wakeup_id,
                diagnostic=diagnostic,
            )
            if current and self.workspace:
                if _append_wakeup_once(current, wakeup["metadata"]):
                    _write_json(_current_task_path(self.workspace), current)
        except Exception as exc:
            logger.warning("Failed to schedule task wakeup: {}", exc)
            code = "INVALID_ARGUMENT" if isinstance(exc, ValueError) else "INTERNAL"
            return _failure(code, str(exc), retryable=code == "INTERNAL")

        return _success({
            "wakeup_id": wakeup["wakeup_id"],
            "cron_job_id": wakeup["cron_job_id"],
            "kind": wakeup["kind"],
            "task_id": wakeup["task_id"],
            "session_id": wakeup["session_id"],
            "scheduled_for": wakeup["scheduled_for"],
            "diagnostic": diagnostic,
        })


class CancelTaskWakeupTool(Tool):
    name = "cancel_task_wakeup"
    description = "Cancel a scheduled structured reminder wakeup by cron_job_id."
    parameters = {
        "type": "object",
        "properties": {
            "cron_job_id": {"type": "string", "description": "Cron job id returned by schedule_task_wakeup."},
        },
        "required": ["cron_job_id"],
    }

    def __init__(self, user_cron_service: Any = None, workspace: Path | None = None):
        self._user_cron = user_cron_service
        self.workspace = workspace

    async def execute(self, cron_job_id: str, **kwargs: Any) -> str:
        if not self._user_cron:
            return _failure("SERVICE_UNAVAILABLE", "UserCron service is not available for task wakeups.")
        removed = self._user_cron.remove_job(cron_job_id)
        todo_update = None
        if self.workspace:
            todo_update = _close_todo_by_cron_job_id(
                self.workspace,
                cron_job_id=cron_job_id,
                occurred_at=_now_iso(),
                reason="wakeup cancelled",
            )
        return _success({"cron_job_id": cron_job_id, "cancelled": removed, "todo": todo_update})


class ListTaskWakeupsTool(Tool):
    name = "list_task_wakeups"
    description = "List scheduled structured reminder wakeups for the current user."
    parameters = {"type": "object", "properties": {}}

    def __init__(self, user_cron_service: Any = None):
        self._user_cron = user_cron_service

    async def execute(self, **kwargs: Any) -> str:
        if not self._user_cron:
            return _failure("SERVICE_UNAVAILABLE", "UserCron service is not available for task wakeups.")
        jobs = []
        for job in self._user_cron.list_jobs(include_disabled=True):
            metadata = getattr(job, "metadata", {}) or {}
            if metadata.get("type") == _CRON_TYPE_TASK_WAKEUP:
                jobs.append({
                    "cron_job_id": job.id,
                    "enabled": job.enabled,
                    "next_run_at_ms": job.next_run_at_ms,
                    "last_status": job.last_status,
                    "metadata": metadata,
                })
        return _success({"wakeups": jobs})


def _find_scheduled_todo_for_action(
    workspace: Path,
    *,
    date: str,
    task_id: str = "",
    title: str = "",
) -> tuple[dict[str, Any] | None, str]:
    payload = ReminderStateStore(workspace).load_todos(date)
    matches: list[dict[str, Any]] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("source") or "") != _TODO_SOURCE_SCHEDULED:
            continue
        if task_id and str(item.get("remote_task_id") or item.get("local_id") or "") == task_id:
            return item, ""
        if title and _title_matches(title, str(item.get("title") or "")):
            matches.append(item)
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, "ambiguous"
    return None, "not_found"


def _record_wait_until_current_done_intent(
    workspace: Path,
    *,
    date: str,
    task_id: str = "",
    title: str = "",
    reason: str = "",
) -> dict[str, Any]:
    current = _current_reminder(workspace)
    if not _is_current_task_payload(current) or _current_status(current) not in _CURRENT_ACTIVE_STATUSES:
        return json.loads(_failure("NO_ACTIVE_REMINDER", "No active reminder to wait for."))
    current_task_id, current_session_id = _current_ids(current)
    target, error = _find_scheduled_todo_for_action(
        workspace,
        date=date,
        task_id=task_id,
        title=title,
    )
    if error == "ambiguous":
        return json.loads(_failure(
            "AMBIGUOUS_REMINDER",
            "Multiple planned reminders match. Ask which one should wait until current is done.",
            details={"title": title, "date": date},
        ))
    if target is None:
        return json.loads(_failure(
            "TASK_NOT_FOUND",
            "planned reminder task not found",
            details={"task_id": task_id, "title": title, "date": date},
        ))
    target_task_id = str(target.get("remote_task_id") or target.get("local_id") or "").strip()
    event = {
        "type": "intent_deferred",
        "at": _now_iso(),
        "actor": "user",
        "task_id": target_task_id,
        "session_id": str(target.get("remote_session_id") or ""),
        "intent": "wait_until_current_done",
        "reason": reason or "user asked to wait until current reminder is done",
        "metadata": {
            "current_task_id": current_task_id,
            "current_session_id": current_session_id,
            "target_task_id": target_task_id,
            "target_source": _TODO_SOURCE_SCHEDULED,
        },
    }
    updated = ReminderStateStore(workspace).update_todo(
        date,
        local_id=str(target.get("local_id") or ""),
        remote_task_id=target_task_id,
        source=_TODO_SOURCE_SCHEDULED,
        event=event,
    )
    return {
        "ok": True,
        "data": {
            "action": "wait_until_current_done",
            "target_task_id": target_task_id,
            "current_task_id": current_task_id,
            "todo": updated,
        },
        "error": None,
    }


def _schedule_waiting_start_prompts_after_current_done(
    workspace: Path,
    user_cron: Any,
    *,
    current_task_id: str,
    occurred_at: str,
) -> list[str]:
    if not user_cron or not current_task_id:
        return []
    date = _date_from_iso(occurred_at)
    store = ReminderStateStore(workspace)
    payload = store.load_todos(date)
    created: list[str] = []
    changed = False
    prompt_at = _iso_after_seconds(1)
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("source") or "") != _TODO_SOURCE_SCHEDULED:
            continue
        if str(item.get("status") or "") in _TODO_FINAL_STATUSES | {_TODO_STATUS_ACTIVE}:
            continue
        events = item.setdefault("events", [])
        if not isinstance(events, list):
            events = []
            item["events"] = events
        target_task_id = str(item.get("remote_task_id") or item.get("local_id") or "").strip()
        pending = False
        consumed = False
        for event in events:
            if not isinstance(event, dict):
                continue
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            same = (
                str(metadata.get("current_task_id") or "") == current_task_id
                and str(metadata.get("target_task_id") or target_task_id) == target_task_id
            )
            if event.get("type") == "intent_deferred" and event.get("intent") == "wait_until_current_done" and same:
                pending = True
            if event.get("type") == "intent_consumed" and event.get("intent") == "wait_until_current_done" and same:
                consumed = True
        if not pending or consumed:
            continue
        try:
            job = _schedule_scheduled_start_wakeup(
                user_cron,
                item=item,
                date=date,
                planned_start_at=prompt_at,
            )
        except Exception:
            continue
        cron_job_id = str(getattr(job, "id", "") or "")
        if cron_job_id:
            created.append(cron_job_id)
            _sync_item_cron_job_ids(item, add_ids=[cron_job_id])
        events.append({
            "type": "intent_consumed",
            "at": _now_iso(),
            "actor": "system",
            "task_id": target_task_id,
            "intent": "wait_until_current_done",
            "reason": "current reminder completed; scheduled start prompt",
            "metadata": {
                "current_task_id": current_task_id,
                "target_task_id": target_task_id,
                "cron_job_id": cron_job_id,
                "scheduled_for": prompt_at,
            },
        })
        item["status"] = _TODO_STATUS_PENDING_START
        item["updated_at"] = _now_iso()
        changed = True
    if changed:
        store.save_todos(date, payload)
    return created


class ReminderActTool(Tool):
    """High-level Reminder action tool with one business transition per call."""

    name = "reminder_act"
    description = (
        "Run one high-level reminder action after resolving the target. "
        "Use for start, complete, defer, or wait_until_current_done. "
        "Do not call lower-level reminder_start_task, reminder_start_ad_hoc_task, "
        "or reminder_complete_task directly unless doing maintenance."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "One of: start, complete, defer, wait_until_current_done."},
            "task_id": {"type": "string", "description": "Reminder Service task id when known."},
            "title": {"type": "string", "description": "Reminder title/query when task_id is unknown."},
            "date": {"type": "string", "description": "YYYY-MM-DD date for lookup; defaults from occurred_at/today."},
            "occurred_at": {"type": "string", "description": "ISO datetime for action time."},
            "reason": {"type": "string", "description": "Short user-facing reason."},
            "category": {"type": "string", "description": "For unplanned start: study, reminder, habit, or other."},
            "expected_minutes": {"type": "integer", "description": "For unplanned start: expected duration in minutes."},
            "notes": {"type": "string", "description": "For unplanned start: original user context."},
            "next_planned_at": {"type": "string", "description": "For defer: optional ISO datetime for next planned time."},
        },
        "required": ["action"],
    }

    def __init__(
        self,
        config: ReminderToolConfigData,
        workspace: Path,
        user_cron_service: Any = None,
    ):
        self.config = config
        self.workspace = workspace
        self._user_cron = user_cron_service

    async def execute(
        self,
        action: str,
        task_id: str = "",
        title: str = "",
        date: str = "",
        occurred_at: str = "",
        reason: str = "",
        category: str = "",
        expected_minutes: int | None = None,
        notes: str = "",
        next_planned_at: str = "",
        **kwargs: Any,
    ) -> str:
        action = action.strip().lower()
        occurred_at = occurred_at.strip() or _now_iso()
        try:
            _parse_iso_ms(occurred_at)
        except ValueError as exc:
            return _failure("INVALID_ARGUMENT", f"occurred_at must be ISO datetime with timezone: {exc}")
        lookup_date = date.strip() or _date_from_iso(occurred_at)
        if err := _validate_date(lookup_date):
            return _failure("INVALID_ARGUMENT", err)

        if action == "start":
            if task_id.strip():
                return await ReminderStartTaskTool(
                    self.config,
                    self.workspace,
                    self._user_cron,
                ).execute(task_id=task_id, started_at=occurred_at)
            return await ReminderStartAdHocTaskTool(
                self.workspace,
                self._user_cron,
            ).execute(
                title=title,
                category=category,
                started_at=occurred_at,
                expected_minutes=expected_minutes,
                notes=notes or reason,
            )

        if action == "complete":
            return await ReminderCompleteTaskTool(
                self.config,
                self.workspace,
                self._user_cron,
            ).execute(
                task_id=task_id,
                title=title,
                completed_at=occurred_at,
                date=lookup_date,
                reason=reason,
            )

        if action == "defer":
            current = _current_reminder(self.workspace)
            current_task_id, _session_id = _current_ids(current or {}) if isinstance(current, dict) else ("", "")
            effective_task_id = task_id.strip() or current_task_id
            if not effective_task_id:
                return _failure("NO_ACTIVE_REMINDER", "No active reminder to defer.")
            return await ReminderDeferTaskTool(
                self.config,
                self.workspace,
            ).execute(
                task_id=effective_task_id,
                payload=current if isinstance(current, dict) else {},
                next_planned_at=next_planned_at,
                reason=reason,
            )

        if action == "wait_until_current_done":
            return _json(_record_wait_until_current_done_intent(
                self.workspace,
                date=lookup_date,
                task_id=task_id,
                title=title,
                reason=reason,
            ))

        return _failure("INVALID_ARGUMENT", "action must be one of: start, complete, defer, wait_until_current_done.")


class ReminderScheduleTool(Tool):
    """High-level Reminder scheduling tool."""

    name = "reminder_schedule"
    description = (
        "Schedule one reminder-related wakeup. Use kind=one_off for independent reminders, "
        "kind=start_prompt for asking whether to start later, and kind=completion_check/progress_check/break_reminder "
        "for the current active reminder."
    )
    parameters = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "one_off, start_prompt, completion_check, progress_check, or break_reminder."},
            "scope": {"type": "string", "description": "independent, current, or pending_start."},
            "title": {"type": "string", "description": "Short reminder title."},
            "category": {"type": "string", "description": "study, reminder, habit, or other."},
            "message": {"type": "string", "description": "Short spoken message."},
            "at": {"type": "string", "description": "ISO datetime for wakeup."},
            "after_seconds": {"type": "integer", "description": "Relative seconds until wakeup."},
            "expected_minutes": {"type": "integer", "description": "For start_prompt: expected duration after user starts."},
            "notes": {"type": "string", "description": "Original user context."},
        },
        "required": ["kind", "title"],
    }

    def __init__(self, workspace: Path, user_cron_service: Any = None):
        self.workspace = workspace
        self._user_cron = user_cron_service

    async def execute(
        self,
        kind: str,
        title: str,
        scope: str = "",
        category: str = "",
        message: str = "",
        at: str = "",
        after_seconds: int | None = None,
        expected_minutes: int | None = None,
        notes: str = "",
        **kwargs: Any,
    ) -> str:
        kind = kind.strip().lower()
        scope = scope.strip().lower()
        if kind == "one_off" or scope == "independent":
            return await ReminderScheduleOneOffTool(
                self.workspace,
                self._user_cron,
            ).execute(
                title=title,
                category=category,
                message=message,
                at=at,
                after_seconds=after_seconds,
                notes=notes,
            )
        if kind == "start_prompt" or scope == "pending_start":
            return await ReminderScheduleStartPromptTool(
                self.workspace,
                self._user_cron,
            ).execute(
                title=title,
                category=category,
                message=message,
                at=at,
                after_seconds=after_seconds,
                expected_minutes=expected_minutes,
                notes=notes,
            )
        if kind in {"completion_check", "progress_check", "break_reminder"}:
            return await ScheduleTaskWakeupTool(
                self._user_cron,
                self.workspace,
            ).execute(
                kind=kind,
                message=message or title,
                at=at,
                after_seconds=after_seconds,
            )
        return _failure("INVALID_ARGUMENT", "kind must be one of: one_off, start_prompt, completion_check, progress_check, break_reminder.")


def build_reminder_service_tools(
    config: ReminderToolConfigData,
    workspace: Path,
    user_cron_service: Any = None,
) -> list[Tool]:
    return [
        ReminderListTodayTasksTool(config, workspace),
        ReminderRefreshTodayTodosTool(config, workspace, user_cron_service),
        ReminderListTodosTool(workspace),
        ReminderStartTaskTool(config, workspace, user_cron_service),
        ReminderCompleteTaskTool(config, workspace, user_cron_service),
        ReminderSyncCurrentTaskTool(config, workspace),
        ReminderGetResumableTaskTool(config, workspace),
        ReminderDeferTaskTool(config, workspace),
    ]


def build_wakeup_tools(
    user_cron_service: Any = None,
    workspace: Path | None = None,
    reminder_config: ReminderToolConfigData | None = None,
) -> list[Tool]:
    local_workspace = workspace or Path.cwd()
    config = reminder_config or ReminderToolConfigData()
    return [
        ReminderListTodosTool(local_workspace),
        ReminderCompleteTaskTool(config, local_workspace, user_cron_service),
        ReminderScheduleStartPromptTool(local_workspace, user_cron_service),
        ReminderStartAdHocTaskTool(local_workspace, user_cron_service),
        ReminderScheduleOneOffTool(local_workspace, user_cron_service),
        ScheduleTaskWakeupTool(user_cron_service, local_workspace),
        CancelTaskWakeupTool(user_cron_service, local_workspace),
        ListTaskWakeupsTool(user_cron_service),
    ]
