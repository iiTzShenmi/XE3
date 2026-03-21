import hashlib
import json
import re
from datetime import datetime


DATETIME_PATTERNS = [
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
]


def _parse_dt(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass

    zh = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2})[:：](\d{2}))?", text)
    if zh:
        y, m, d, hh, mm = zh.groups()
        hh = hh or "00"
        mm = mm or "00"
        try:
            return datetime(int(y), int(m), int(d), int(hh), int(mm))
        except ValueError:
            return None

    for pattern in DATETIME_PATTERNS:
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue

    fallback = re.search(r"(\d{4}[/-]\d{1,2}[/-]\d{1,2})(?:\s+(\d{1,2}:\d{2}))?", text)
    if fallback:
        date_part, time_part = fallback.groups()
        merged = f"{date_part} {time_part or '00:00'}"
        for pattern in ["%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"]:
            try:
                return datetime.strptime(merged, pattern)
            except ValueError:
                continue

    return None


def _infer_event_type(title, fallback="calendar"):
    text = (title or "").strip().lower()
    if not text:
        return fallback

    exam_keywords = ["exam", "midterm", "final", "quiz", "期中", "期末", "考試", "測驗"]
    homework_keywords = ["homework", "assignment", "作業", "hw"]

    if any(keyword in text for keyword in exam_keywords):
        return "exam"
    if any(keyword in text for keyword in homework_keywords):
        return "homework"
    return fallback


def _make_event_uid(*parts):
    raw = ":".join(str(part or "").strip() for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _assignment_is_completed(item):
    if not isinstance(item, dict):
        return False
    if item.get("is_completed") is True:
        return True
    category = str(item.get("category") or "").strip().lower()
    if category == "submitted":
        return True
    submitted_files = item.get("submitted_files") or []
    return bool(submitted_files)


def extract_events_from_fetch_all(data, calendar_events=None):
    if not isinstance(data, dict):
        data = {}

    events = []
    seen = set()
    for course_name, payload in data.items():
        if not isinstance(payload, dict):
            continue

        course_id = payload.get("_course_id")

        assignments = payload.get("assignments") or {}
        if isinstance(assignments, dict):
            items = assignments.get("assignments")
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    category = str(item.get("category") or "").strip().lower()
                    if category and category not in {"in_progress", "upcoming"}:
                        continue
                    if _assignment_is_completed(item):
                        continue
                    title = str(item.get("title") or item.get("name") or "未命名作業").strip()
                    due_raw = (
                        item.get("due")
                        or item.get("due_time")
                        or item.get("due_date")
                        or item.get("deadline")
                        or item.get("截止")
                    )
                    due_dt = _parse_dt(due_raw)
                    if not due_dt:
                        continue
                    event_uid = _make_event_uid("assignment", course_id, title, due_dt.isoformat())
                    if event_uid in seen:
                        continue
                    seen.add(event_uid)
                    events.append(
                        {
                            "event_uid": event_uid,
                            "event_type": _infer_event_type(title, fallback="homework"),
                            "course_id": course_id,
                            "course_name": course_name,
                            "title": title,
                            "due_at": due_dt.isoformat(),
                            "payload_json": json.dumps(item, ensure_ascii=False),
                        }
                    )

    if isinstance(calendar_events, list):
        for item in calendar_events:
            if not isinstance(item, dict):
                continue

            title = str(item.get("title") or "").strip()
            due_raw = item.get("due_at")
            due_dt = _parse_dt(due_raw)
            if not title or not due_dt:
                continue

            course_id = str(item.get("course_id") or "").strip() or None
            course_name = str(item.get("course_name") or "").strip()
            event_uid = _make_event_uid("calendar", item.get("event_id"), course_id, title, due_dt.isoformat())
            if event_uid in seen:
                continue
            seen.add(event_uid)
            events.append(
                {
                    "event_uid": event_uid,
                    "event_type": _infer_event_type(title, fallback="calendar"),
                    "course_id": course_id,
                    "course_name": course_name,
                    "title": title,
                    "due_at": due_dt.isoformat(),
                    "payload_json": json.dumps(item, ensure_ascii=False),
                }
            )

    return events
