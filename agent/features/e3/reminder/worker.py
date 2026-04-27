from __future__ import annotations

import fcntl
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import requests

from agent.core.config import e3_reminder_poll_seconds, e3_sync_interval_minutes, reminder_worker_lock_file

from ..services.client import fetch_courses, login_and_sync, make_user_key
from ..data.db import (
    get_e3_account_by_user_id,
    get_events_due_between,
    get_grade_items,
    get_line_user_id_by_user_id,
    list_reminder_targets,
    list_sync_targets,
    log_notification,
    mark_missing_events_inactive,
    notification_sent,
    notification_succeeded,
    update_login_state,
    upsert_event,
    upsert_grade_item,
)
from ..services.events import extract_events_from_fetch_all
from ..utils.common import assignment_items, is_assignment_completed, normalize_title_token
from .payloads import (
    COUNTDOWN_HOURS,
    DEFAULT_LOOKAHEAD_HOURS,
    build_digest_payload,
    build_empty_digest_payload,
    extract_grade_items,
    format_countdown_payload,
    format_grade_payload,
    load_schedule,
    taipei_now,
)
from ..services.secrets import decrypt_secret

_STARTED = False
_LOCK = threading.Lock()
_WORKER_LOCK_HANDLE: Optional[Any] = None
PRE_REMINDER_SYNC_MINUTES = 10
TRANSIENT_SYNC_ERROR_MARKERS = (
    "temporary failure in name resolution",
    "nameresolutionerror",
    "failed to resolve",
    "nodename nor servname provided",
    "name or service not known",
    "no route to host",
    "network is unreachable",
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection timed out",
    "read timed out",
    "connect timeout",
    "max retries exceeded",
    "remote end closed connection",
)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        value = row.get(key, default)
    else:
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            return default
    return default if value is None else value


def _build_assignment_completion_lookup(user_key: str, logger) -> dict[tuple[str, str], bool]:
    try:
        courses = fetch_courses(make_user_key(user_key))
    except Exception:
        logger.exception("e3_assignment_completion_lookup_failed user=%s", user_key)
        return {}

    lookup: dict[tuple[str, str], bool] = {}
    for payload in (courses or {}).values():
        if not isinstance(payload, dict):
            continue
        course_id = str(payload.get("_course_id") or "").strip()
        if not course_id:
            continue
        for item in assignment_items(payload):
            if not isinstance(item, dict):
                continue
            title = normalize_title_token(item.get("title") or item.get("name"))
            if not title:
                continue
            lookup[(course_id, title)] = is_assignment_completed(item)
    return lookup


def _titles_similar(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if min(len(left), len(right)) < 6:
        return False
    return left in right or right in left


def _is_completed_homework_event(row: Any, completion_lookup: dict[tuple[str, str], bool]) -> bool:
    if str(_row_value(row, "event_type", "")).strip() != "homework":
        return False
    if not completion_lookup:
        return False

    course_id = str(_row_value(row, "course_id", "")).strip()
    title = normalize_title_token(_row_value(row, "title", ""))
    if not course_id or not title:
        return False

    if completion_lookup.get((course_id, title)) is True:
        return True

    for (lookup_course_id, lookup_title), is_completed in completion_lookup.items():
        if not is_completed or lookup_course_id != course_id:
            continue
        if _titles_similar(title, lookup_title):
            return True
    return False


def _filter_actionable_events(rows: list[Any], completion_lookup: dict[tuple[str, str], bool]) -> list[Any]:
    if not rows:
        return []
    return [row for row in rows if not _is_completed_homework_event(row, completion_lookup)]


def _parse_iso_timestamp(value: Any):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _looks_transient_error_text(value: Any) -> bool:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in TRANSIENT_SYNC_ERROR_MARKERS)


def _is_transient_sync_error(exc: BaseException) -> bool:
    for item in _exception_chain(exc):
        if isinstance(item, (requests.ConnectionError, requests.Timeout, TimeoutError, ConnectionError)):
            return True
        if _looks_transient_error_text(item):
            return True
    return False


def _login_status_allows_cached_reminders(row: Any) -> bool:
    status = str(_row_value(row, "login_status", "")).strip()
    if status == "ok":
        return True
    return status == "error" and _looks_transient_error_text(_row_value(row, "last_error", ""))


def _scheduled_digest_succeeded(user_id: int, dedupe_key: str) -> bool:
    return notification_succeeded(user_id, "scheduled_digest", dedupe_key) or notification_succeeded(
        user_id,
        "scheduled_digest",
        f"{dedupe_key}|empty",
    )


def _has_homework_events(rows: list[Any]) -> bool:
    return any(str(_row_value(row, "event_type", "")).strip() == "homework" for row in rows or [])


def _recently_synced(row: Any, now, minutes: int = PRE_REMINDER_SYNC_MINUTES) -> bool:
    synced_at = _parse_iso_timestamp(_row_value(row, "account_updated_at"))
    if synced_at is None:
        return False
    if getattr(synced_at, "tzinfo", None) is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)
    else:
        synced_at = synced_at.astimezone(timezone.utc)
    current = now.astimezone(timezone.utc)
    return (current - synced_at) <= timedelta(minutes=minutes)


def sync_grade_items(user_id: int, courses: dict[str, Any]) -> list[dict[str, Any]]:
    existing = {(row["course_id"], row["item_name"]): row["score"] for row in get_grade_items(user_id)}
    changes = []
    for item in extract_grade_items(courses):
        key = (item["course_id"], item["item_name"])
        old_score = existing.get(key)
        if old_score != item["score"]:
            change = dict(item)
            change["old_score"] = old_score
            changes.append(change)
        upsert_grade_item(user_id, item["course_id"], item["course_name"], item["item_name"], item["score"])
    return changes


def sync_user_snapshot(row: Any, logger, persist_failure: bool = True) -> tuple[list[dict[str, Any]], bool]:
    account_row = get_e3_account_by_user_id(row["user_id"])
    if not account_row or not account_row["encrypted_password"]:
        return [], False

    try:
        password = decrypt_secret(account_row["encrypted_password"])
        result = login_and_sync(
            account_row["e3_account"],
            password,
            make_user_key(row["line_user_id"]),
            update_data=True,
            update_links=False,
        )
        courses = result["courses"]
        calendar_events = result.get("calendar_events") or []
        events = extract_events_from_fetch_all(courses, calendar_events=calendar_events)
        active_event_uids = []
        for event in events:
            active_event_uids.append(event["event_uid"])
            upsert_event(
                user_id=row["user_id"],
                event_uid=event["event_uid"],
                event_type=event["event_type"],
                course_id=event.get("course_id"),
                course_name=event.get("course_name"),
                title=event["title"],
                due_at=event["due_at"],
                payload_json=event["payload_json"],
            )
        mark_missing_events_inactive(row["user_id"], active_event_uids)
        grade_changes = sync_grade_items(row["user_id"], courses)
        update_login_state(row["user_id"], "ok", None)
        return grade_changes, True
    except Exception as exc:
        logger.exception("e3_periodic_sync_failed user=%s", row["line_user_id"])
        if persist_failure and not _is_transient_sync_error(exc):
            update_login_state(row["user_id"], "error", str(exc))
        return [], False


def maybe_periodic_sync(row: Any, now, push_fn, logger) -> None:
    interval_minutes = e3_sync_interval_minutes()
    if interval_minutes <= 0 or now.minute % interval_minutes != 0:
        return

    dedupe_key = now.strftime("%Y-%m-%d %H:%M")
    if notification_sent(row["user_id"], "periodic_sync", dedupe_key):
        return

    grade_changes, ok = sync_user_snapshot(row, logger)
    log_notification(row["user_id"], "periodic_sync", "sent" if ok else "failed", details=dedupe_key)
    if not ok:
        return

    for change in grade_changes:
        change_key = f"{change['course_id']}|{change['item_name']}|{change['score']}"
        if notification_succeeded(row["user_id"], "grade_posted", change_key):
            continue
        push_ok = push_fn(row["line_user_id"], format_grade_payload(change))
        log_notification(row["user_id"], "grade_posted", "sent" if push_ok else "failed", details=change_key)


def process_periodic_syncs(now, push_fn, logger, target_predicate=None) -> None:
    for row in list_sync_targets():
        if target_predicate and not target_predicate(str(row["line_user_id"])):
            continue
        if not _login_status_allows_cached_reminders(row):
            continue
        maybe_periodic_sync(row, now, push_fn, logger)


def refresh_all_saved_accounts(logger) -> dict[str, Any]:
    rows = list(list_sync_targets())
    summary: dict[str, Any] = {
        "total": len(rows),
        "ok": 0,
        "failed": 0,
        "grade_changes": 0,
        "results": [],
    }
    for row in rows:
        grade_changes, ok = sync_user_snapshot(row, logger)
        result = {
            "user_key": str(row["line_user_id"]),
            "ok": bool(ok),
            "grade_changes": len(grade_changes),
        }
        summary["results"].append(result)
        if ok:
            summary["ok"] += 1
            summary["grade_changes"] += len(grade_changes)
        else:
            summary["failed"] += 1
    return summary


def process_due_reminders(push_fn, logger, target_predicate=None) -> None:
    now = taipei_now()
    current_slot = now.strftime("%H:%M")
    start_iso = now.astimezone(timezone.utc).isoformat()
    end_iso = (now + timedelta(hours=DEFAULT_LOOKAHEAD_HOURS)).astimezone(timezone.utc).isoformat()
    interval_seconds = e3_reminder_poll_seconds()
    tolerance = max(interval_seconds * 2, 300)

    process_periodic_syncs(now, push_fn, logger, target_predicate=target_predicate)

    for row in list_reminder_targets():
        if target_predicate and not target_predicate(str(row["line_user_id"])):
            continue
        if not _login_status_allows_cached_reminders(row):
            continue

        countdown_windows: dict[int, list[Any]] = {}
        for hours_left in COUNTDOWN_HOURS:
            window_start = (now + timedelta(hours=hours_left)).astimezone(timezone.utc)
            window_end = (now + timedelta(hours=hours_left, seconds=tolerance)).astimezone(timezone.utc)
            countdown_windows[hours_left] = list(
                get_events_due_between(
                    row["user_id"],
                    window_start.isoformat(),
                    window_end.isoformat(),
                    limit=10,
                )
            )

        schedule = load_schedule(row)
        digest_key = f"{now.date().isoformat()} {current_slot}"
        digest_enabled = current_slot in schedule and not notification_sent(row["user_id"], "scheduled_digest", digest_key)
        digest_events = list(get_events_due_between(row["user_id"], start_iso, end_iso, limit=8)) if digest_enabled else []

        needs_homework_guard = _has_homework_events(digest_events) or any(
            _has_homework_events(rows) for rows in countdown_windows.values()
        )
        completion_lookup: dict[tuple[str, str], bool] = {}
        if needs_homework_guard:
            if not _recently_synced(row, now):
                _, sync_ok = sync_user_snapshot(row, logger, persist_failure=False)
                log_notification(
                    row["user_id"],
                    "pre_reminder_sync",
                    "sent" if sync_ok else "failed",
                    details=now.strftime("%Y-%m-%d %H:%M"),
                )
                if sync_ok:
                    countdown_windows = {}
                    for hours_left in COUNTDOWN_HOURS:
                        window_start = (now + timedelta(hours=hours_left)).astimezone(timezone.utc)
                        window_end = (now + timedelta(hours=hours_left, seconds=tolerance)).astimezone(timezone.utc)
                        countdown_windows[hours_left] = list(
                            get_events_due_between(
                                row["user_id"],
                                window_start.isoformat(),
                                window_end.isoformat(),
                                limit=10,
                            )
                        )
                    if digest_enabled:
                        digest_events = list(get_events_due_between(row["user_id"], start_iso, end_iso, limit=8))
            completion_lookup = _build_assignment_completion_lookup(str(row["line_user_id"]), logger)
            countdown_windows = {
                hours_left: _filter_actionable_events(rows, completion_lookup)
                for hours_left, rows in countdown_windows.items()
            }
            digest_events = _filter_actionable_events(digest_events, completion_lookup)

        for hours_left in COUNTDOWN_HOURS:
            countdown_rows = countdown_windows.get(hours_left) or []
            for event_row in countdown_rows:
                countdown_key = f"{event_row['event_uid']}|{hours_left}h"
                if notification_succeeded(row["user_id"], "countdown_alert", countdown_key):
                    continue
                ok = push_fn(row["line_user_id"], format_countdown_payload(event_row, hours_left, row["line_user_id"]))
                log_notification(
                    row["user_id"],
                    "countdown_alert",
                    "sent" if ok else "failed",
                    details=countdown_key,
                    event_uid=event_row["event_uid"],
                )

        if current_slot not in schedule:
            continue

        dedupe_key = f"{now.date().isoformat()} {current_slot}"
        if _scheduled_digest_succeeded(row["user_id"], dedupe_key):
            continue

        events = digest_events
        if not events:
            payload = build_empty_digest_payload(current_slot, row["line_user_id"])
            ok = push_fn(row["line_user_id"], payload)
            log_notification(
                row["user_id"],
                "scheduled_digest",
                "sent" if ok else "failed",
                details=f"{dedupe_key}|empty",
            )
            if not ok:
                logger.error("e3_reminder_push_failed user=%s slot=%s empty_digest=1", row["line_user_id"], current_slot)
            continue

        payload = build_digest_payload(events, current_slot, row["line_user_id"])
        ok = push_fn(row["line_user_id"], payload)
        log_notification(
            row["user_id"],
            "scheduled_digest",
            "sent" if ok else "failed",
            details=dedupe_key,
        )
        if not ok:
            logger.error("e3_reminder_push_failed user=%s slot=%s", row["line_user_id"], current_slot)


def worker_loop(push_fn: Callable[[str, Any], bool], logger, interval_seconds: int, target_predicate=None) -> None:
    while True:
        try:
            process_due_reminders(push_fn, logger, target_predicate=target_predicate)
        except Exception:
            logger.exception("e3_reminder_loop_failed")
        time.sleep(interval_seconds)


def acquire_worker_lock() -> bool:
    global _WORKER_LOCK_HANDLE
    lock_path = reminder_worker_lock_file()
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    _WORKER_LOCK_HANDLE = handle
    return True


def start_reminder_worker(push_fn: Callable[[str, Any], bool], logger, target_predicate=None) -> bool:
    global _STARTED
    with _LOCK:
        if _STARTED:
            return False
        if not acquire_worker_lock():
            logger.info("e3_reminder_worker_skipped reason=lock_held")
            return False
        _STARTED = True

    interval_seconds = e3_reminder_poll_seconds()
    worker = threading.Thread(
        target=worker_loop,
        args=(push_fn, logger, interval_seconds, target_predicate),
        daemon=True,
        name="e3-reminder-worker",
    )
    worker.start()
    return True


def build_test_reminder_payloads(user_id: int) -> list[str]:
    user_key = get_line_user_id_by_user_id(user_id) or "discord:test"

    class _NullLogger:
        def exception(self, *args, **kwargs):
            return None

    if user_key:
        sync_user_snapshot({"user_id": user_id, "line_user_id": user_key}, _NullLogger(), persist_failure=False)

    now = taipei_now()
    start_iso = now.astimezone(timezone.utc).isoformat()
    end_iso = (now + timedelta(hours=DEFAULT_LOOKAHEAD_HOURS)).astimezone(timezone.utc).isoformat()
    completion_lookup = _build_assignment_completion_lookup(user_key, _NullLogger())
    events = list(get_events_due_between(user_id, start_iso, end_iso, limit=5))
    events = _filter_actionable_events(events, completion_lookup)
    if not events:
        return [
            build_empty_digest_payload("09:00", user_key=user_key),
            build_empty_digest_payload("21:00", user_key=user_key),
        ]
    return [
        build_digest_payload(events, "09:00", user_key=user_key) or "",
        build_digest_payload(events, "21:00", user_key=user_key) or "",
    ]
