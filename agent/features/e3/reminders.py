import re
import threading
import time
from datetime import datetime, timedelta, timezone
import fcntl
from typing import Any, Callable, Optional

from agent.config import e3_reminder_poll_seconds, e3_sync_interval_minutes, reminder_worker_lock_file
from .client import login_and_sync, make_user_key
from .db import (
    get_e3_account_by_user_id,
    get_events_due_between,
    list_reminder_targets,
    log_notification,
    mark_missing_events_inactive,
    notification_sent,
    update_login_state,
    upsert_event,
)
from .events import extract_events_from_fetch_all
from .secrets import decrypt_secret


DEFAULT_LOOKAHEAD_HOURS = 36
DEFAULT_SCHEDULE = ["09:00", "21:00"]
COUNTDOWN_HOURS = [12, 2]
_STARTED = False
_LOCK = threading.Lock()
_WORKER_LOCK_HANDLE: Optional[Any] = None


def _taipei_now():
    return datetime.now(timezone(timedelta(hours=8)))


def _load_schedule(row):
    return list(DEFAULT_SCHEDULE)


def _format_due_label(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    taipei_tz = timezone(timedelta(hours=8))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=taipei_tz)
    else:
        dt = dt.astimezone(taipei_tz)
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    return dt.strftime("%m/%d") + f" ({weekdays[dt.weekday()]}) " + dt.strftime("%H:%M")


def _course_name_for_display(text):
    text = str(text or "").strip()
    text = text.replace("_", " ")
    matches = list(re.finditer(r"[\u4e00-\u9fff]", text))
    if matches:
        end = matches[-1].end()
        while end < len(text) and text[end] in ")）】] ":
            end += 1
        text = text[:end]
    text = text.strip(" -_|,")
    return text or "-"


def _count_event_types(rows):
    counts = {"homework": 0, "exam": 0, "calendar": 0}
    for row in rows:
        event_type = str(row.get("event_type") or "").strip()
        if event_type in counts:
            counts[event_type] += 1
    return counts


def _morning_brief_lines(rows):
    now = _taipei_now()
    today = now.date()
    counts = _count_event_types(rows)
    today_rows = []
    for row in rows:
        try:
            dt = datetime.fromisoformat(str(row["due_at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        if dt.astimezone(timezone(timedelta(hours=8))).date() == today:
            today_rows.append(row)

    lines = ["Good morning. Here's your E3 briefing for today."]
    summary_bits = []
    if counts["homework"]:
        summary_bits.append(f"{counts['homework']} assignment(s)")
    if counts["exam"]:
        summary_bits.append(f"{counts['exam']} exam(s)")
    if counts["calendar"]:
        summary_bits.append(f"{counts['calendar']} calendar item(s)")
    if summary_bits:
        lines.append("In the next 36 hours: " + ", \".join(summary_bits) + \".")
    if today_rows:
        lines.append(f"Due today: {len(today_rows)} item(s).")
        first_row = min(today_rows, key=lambda row: str(row.get("due_at") or ""))
        course_name = _course_name_for_display(first_row["course_name"] or first_row["course_id"] or "-")
        lines.append(
            f"Next up: {_format_due_label(first_row['due_at'])} {course_name} - {first_row['title']}"
        )
    else:
        lines.append("Nothing is due today so far.")
    return lines


def _format_digest(rows, slot_text):
    lines = [f"⏰ E3 提醒 {slot_text}"]
    if slot_text == "09:00":
        lines.extend(_morning_brief_lines(rows))
        lines.append("")
    lines.append("未來 36 小時內的重點事件：")
    for idx, row in enumerate(rows, start=1):
        course_name = _course_name_for_display(row["course_name"] or row["course_id"] or "-")
        label = {"exam": "🧪", "homework": "📝", "calendar": "🗓️"}.get(row["event_type"], "📌")
        lines.append(f"{idx}. {_format_due_label(row['due_at'])} {label} {course_name}")
        lines.append(f"   {row['title']}")
    return "\n".join(lines)


def _build_digest_payload(rows, slot_text):
    if not rows:
        return None
    return _format_digest(rows, slot_text)


def _format_countdown_payload(row, hours_left):
    course_name = _course_name_for_display(row["course_name"] or row["course_id"] or "-")
    label = {"exam": "🧪", "homework": "📝", "calendar": "🗓️"}.get(row["event_type"], "📌")
    return (
        f"⏰ E3 倒數提醒（{hours_left} 小時）\n"
        f"{_format_due_label(row['due_at'])} {label} {course_name}\n"
        f"{row['title']}"
    )


def _extract_grade_items(courses):
    items = []
    if not isinstance(courses, dict):
        return items
    for display_name, payload in courses.items():
        if not isinstance(payload, dict):
            continue
        grades = payload.get("grades") or {}
        if not isinstance(grades, dict):
            continue
        course_id = str(payload.get("_course_id") or "").strip()
        course_name = _course_name_for_display(display_name)
        for item_name, score in grades.items():
            score_text = str(score or "").strip()
            if not score_text or score_text == "-":
                continue
            items.append(
                {
                    "course_id": course_id,
                    "course_name": course_name,
                    "item_name": str(item_name or "").replace("\u000b", " ").strip(),
                    "score": score_text,
                }
            )
    return items


def _sync_grade_items(user_id, courses, get_existing_rows, upsert_grade_item):
    existing = {
        (row["course_id"], row["item_name"]): row["score"]
        for row in get_existing_rows(user_id)
    }
    changes = []
    for item in _extract_grade_items(courses):
        key = (item["course_id"], item["item_name"])
        old_score = existing.get(key)
        if old_score != item["score"]:
            change = dict(item)
            change["old_score"] = old_score
            changes.append(change)
        upsert_grade_item(
            user_id,
            item["course_id"],
            item["course_name"],
            item["item_name"],
            item["score"],
        )
    return changes


def _format_grade_payload(change):
    course_name = _course_name_for_display(change["course_name"])
    if change.get("old_score"):
        score_line = f"{change['old_score']} -> {change['score']}"
    else:
        score_line = change["score"]
    return (
        "🎉 新成績已公布\n"
        f"{course_name}\n"
        f"{change['item_name']}：{score_line}"
    )


def _sync_user_snapshot(row, logger):
    account_row = get_e3_account_by_user_id(row["user_id"])
    if not account_row or not account_row["encrypted_password"]:
        return [], False

    from .db import get_grade_items, upsert_grade_item  # local import to avoid clutter

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
        grade_changes = _sync_grade_items(row["user_id"], courses, get_grade_items, upsert_grade_item)
        update_login_state(row["user_id"], "ok", None)
        return grade_changes, True
    except Exception as exc:
        logger.error("e3_periodic_sync_failed user=%s error=%s", row["line_user_id"], exc)
        update_login_state(row["user_id"], "error", str(exc))
        return [], False


def _maybe_periodic_sync(row, now, push_fn, logger):
    interval_minutes = e3_sync_interval_minutes()
    if now.minute % interval_minutes != 0:
        return

    dedupe_key = now.strftime("%Y-%m-%d %H:%M")
    if notification_sent(row["user_id"], "periodic_sync", dedupe_key):
        return

    grade_changes, ok = _sync_user_snapshot(row, logger)
    log_notification(row["user_id"], "periodic_sync", "sent" if ok else "failed", details=dedupe_key)
    if not ok:
        return

    for change in grade_changes:
        change_key = f"{change['course_id']}|{change['item_name']}|{change['score']}"
        if notification_sent(row["user_id"], "grade_posted", change_key):
            continue
        push_ok = push_fn(row["line_user_id"], _format_grade_payload(change))
        log_notification(
            row["user_id"],
            "grade_posted",
            "sent" if push_ok else "failed",
            details=change_key,
        )


def process_due_reminders(push_fn, logger, target_predicate=None):
    now = _taipei_now()
    current_slot = now.strftime("%H:%M")
    start_iso = now.astimezone(timezone.utc).isoformat()
    end_iso = (now + timedelta(hours=DEFAULT_LOOKAHEAD_HOURS)).astimezone(timezone.utc).isoformat()
    interval_seconds = e3_reminder_poll_seconds()
    tolerance = max(interval_seconds * 2, 300)

    for row in list_reminder_targets():
        if target_predicate and not target_predicate(str(row["line_user_id"])):
            continue
        if row["login_status"] != "ok":
            continue
        _maybe_periodic_sync(row, now, push_fn, logger)

        for hours_left in COUNTDOWN_HOURS:
            window_start = (now + timedelta(hours=hours_left)).astimezone(timezone.utc)
            window_end = (now + timedelta(hours=hours_left, seconds=tolerance)).astimezone(timezone.utc)
            countdown_rows = get_events_due_between(
                row["user_id"],
                window_start.isoformat(),
                window_end.isoformat(),
                limit=10,
            )
            for event_row in countdown_rows:
                countdown_key = f"{event_row['event_uid']}|{hours_left}h"
                if notification_sent(row["user_id"], "countdown_alert", countdown_key):
                    continue
                ok = push_fn(row["line_user_id"], _format_countdown_payload(event_row, hours_left))
                log_notification(
                    row["user_id"],
                    "countdown_alert",
                    "sent" if ok else "failed",
                    details=countdown_key,
                    event_uid=event_row["event_uid"],
                )

        schedule = _load_schedule(row)
        if current_slot not in schedule:
            continue

        dedupe_key = f"{now.date().isoformat()} {current_slot}"
        if notification_sent(row["user_id"], "scheduled_digest", dedupe_key):
            continue

        events = get_events_due_between(row["user_id"], start_iso, end_iso, limit=8)
        if not events:
            log_notification(row["user_id"], "scheduled_digest", "empty", details=dedupe_key)
            continue

        payload = _build_digest_payload(events, current_slot)
        ok = push_fn(row["line_user_id"], payload)
        log_notification(
            row["user_id"],
            "scheduled_digest",
            "sent" if ok else "failed",
            details=dedupe_key,
        )
        if not ok:
            logger.error("e3_reminder_push_failed user=%s slot=%s", row["line_user_id"], current_slot)


def _worker_loop(push_fn: Callable[[str, Any], bool], logger, interval_seconds: int, target_predicate=None) -> None:
    while True:
        try:
            process_due_reminders(push_fn, logger, target_predicate=target_predicate)
        except Exception:
            logger.exception("e3_reminder_loop_failed")
        time.sleep(interval_seconds)


def _acquire_worker_lock() -> bool:
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
        if not _acquire_worker_lock():
            logger.info("e3_reminder_worker_skipped reason=lock_held")
            return False
        _STARTED = True

    interval_seconds = e3_reminder_poll_seconds()
    worker = threading.Thread(
        target=_worker_loop,
        args=(push_fn, logger, interval_seconds, target_predicate),
        daemon=True,
        name="e3-reminder-worker",
    )
    worker.start()
    return True


def build_test_reminder_payload(user_id: int) -> str:
    now = _taipei_now()
    start_iso = now.astimezone(timezone.utc).isoformat()
    end_iso = (now + timedelta(hours=DEFAULT_LOOKAHEAD_HOURS)).astimezone(timezone.utc).isoformat()
    events = get_events_due_between(user_id, start_iso, end_iso, limit=5)
    if not events:
        return "⏰ Reminder test\nNo upcoming events in the next 36 hours."
    return _format_digest(events, "test")
