import hashlib
import html
import json
import re
from datetime import datetime


DATETIME_PATTERNS = [
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
]

EXAM_KEYWORDS = ["期中", "期末", "考試", "測驗"]
ENGLISH_EXAM_PATTERNS = [
    r"\bmidterm\b",
    r"\bmid-term\b",
    r"\bexam\b",
    r"\bquiz\b",
    r"\bsmall test\b",
    r"\bfinal exam\b",
    r"\bfinal evaluation\b",
    r"\bmidterm exam\b",
    r"\bmid-term exam\b",
]


def _event_payload_source(event):
    try:
        payload = json.loads(event.get("payload_json") or "{}")
    except Exception:
        payload = {}
    return str(payload.get("source") or "").strip().lower()


def _event_due_date_key(event):
    due_dt = _parse_dt(event.get("due_at"))
    if not due_dt:
        return ""
    return due_dt.date().isoformat()


def _exam_category(title):
    text = _clean_event_text(title).lower()
    if not text:
        return ""
    if "期中" in text or re.search(r"\bmid-?term\b", text):
        return "midterm"
    if "期末" in text or re.search(r"\bfinal(?: exam| evaluation)?\b", text):
        return "final"
    if "測驗" in text or re.search(r"\bquiz\b|\bsmall test\b", text):
        return "quiz"
    if "考" in text or re.search(r"\bexam\b", text):
        return "exam"
    return ""


def _dedupe_cross_source_exam_events(events):
    if not isinstance(events, list):
        return events

    grouped = {}
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        if str(event.get("event_type") or "") != "exam":
            continue
        source = _event_payload_source(event)
        if source not in {"course_outline_syllabus", "calendar_upcoming"}:
            continue
        course_id = str(event.get("course_id") or "").strip()
        if not course_id:
            continue
        due_key = _event_due_date_key(event)
        category = _exam_category(event.get("title") or "")
        if not due_key or not category:
            continue
        grouped.setdefault((course_id, due_key, category), []).append((index, source, event))

    drop_indexes = set()
    for _, matches in grouped.items():
        has_calendar = any(source == "calendar_upcoming" for _, source, _ in matches)
        has_outline = any(source == "course_outline_syllabus" for _, source, _ in matches)
        if not (has_calendar and has_outline):
            continue
        for index, source, _ in matches:
            if source == "course_outline_syllabus":
                drop_indexes.add(index)

    if not drop_indexes:
        return events
    return [event for index, event in enumerate(events) if index not in drop_indexes]


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


def _contains_exam_keyword(text):
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    if any(keyword in lowered for keyword in EXAM_KEYWORDS):
        return True
    return any(re.search(pattern, lowered) for pattern in ENGLISH_EXAM_PATTERNS)


def _clean_event_text(text):
    cleaned = html.unescape(str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _infer_exam_date_from_topic(topic, class_date):
    topic_text = _clean_event_text(topic)
    class_date_text = str(class_date or "").strip()

    try:
        base_dt = _parse_dt(class_date_text)
    except Exception:
        base_dt = None

    full_dates = re.findall(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", topic_text)
    if full_dates:
        y, m, d = full_dates[-1]
        try:
            return datetime(int(y), int(m), int(d))
        except ValueError:
            pass

    md_dates = re.findall(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", topic_text)
    if md_dates and base_dt:
        month, day = md_dates[-1]
        try:
            return datetime(base_dt.year, int(month), int(day))
        except ValueError:
            pass

    class_date_matches = re.findall(r"(\d{4})-(\d{1,2})-(\d{1,2})", class_date_text)
    if len(class_date_matches) > 1:
        try:
            y, m, d = class_date_matches[-1]
            return datetime(int(y), int(m), int(d))
        except ValueError:
            pass

    return _parse_dt(class_date_text)


def _syllabus_exam_events(course_id, course_name, timetable_payload):
    if not isinstance(timetable_payload, dict):
        return []

    outline_data = timetable_payload.get("course_outline_data") or {}
    if not isinstance(outline_data, dict):
        outline_data = {}

    syllabus_rows = (
        outline_data.get("syllabus_normalized")
        or outline_data.get("syllabus")
        or []
    )
    if not isinstance(syllabus_rows, list):
        syllabus_rows = []

    events = []
    seen = set()
    for row in syllabus_rows:
        if not isinstance(row, dict):
            continue
        topic = _clean_event_text(row.get("class_data") or row.get("topic") or "")
        class_date = row.get("class_date") or row.get("date")
        if not _contains_exam_keyword(topic):
            continue
        due_dt = _infer_exam_date_from_topic(topic, class_date)
        if not due_dt:
            continue
        title = f"課綱考試｜{topic}"
        event_uid = _make_event_uid("syllabus_exam", course_id, topic, due_dt.isoformat())
        if event_uid in seen:
            continue
        seen.add(event_uid)
        payload = {
            "source": "course_outline_syllabus",
            "class_date": class_date,
            "topic": topic,
            "week_id": row.get("week_id"),
            "course_id": course_id,
            "course_name": course_name,
        }
        events.append(
            {
                "event_uid": event_uid,
                "event_type": "exam",
                "course_id": course_id,
                "course_name": course_name,
                "title": title,
                "due_at": due_dt.isoformat(),
                "payload_json": json.dumps(payload, ensure_ascii=False),
            }
        )

    return events


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

        timetable_payload = payload.get("timetable") or {}
        if isinstance(timetable_payload, dict):
            for event in _syllabus_exam_events(course_id, course_name, timetable_payload):
                if event["event_uid"] in seen:
                    continue
                seen.add(event["event_uid"])
                events.append(event)

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

    return _dedupe_cross_source_exam_events(events)
