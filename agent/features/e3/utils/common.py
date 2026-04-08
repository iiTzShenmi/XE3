import re
from datetime import datetime, timedelta, timezone
from typing import Any


def matches_course_keyword(course_label: str, keyword: str) -> bool:
    if not keyword:
        return True
    left = re.sub(r"\s+", "", str(course_label or "")).lower()
    right = re.sub(r"\s+", "", str(keyword or "")).lower()
    return right in left


def normalize_title_token(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).casefold()


def assignment_items(payload: dict[str, Any] | None) -> list[Any]:
    assignments = (payload or {}).get("assignments") or {}
    if isinstance(assignments, dict):
        items = assignments.get("assignments") or []
        return items if isinstance(items, list) else []
    if isinstance(assignments, list):
        return assignments
    return []


def is_assignment_completed(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("is_completed") is True:
        return True
    category = str(item.get("category") or "").strip().lower()
    if category == "submitted":
        return True
    submitted_files = item.get("submitted_files") or []
    return bool(submitted_files)


def current_semester_tag(now: datetime | None = None) -> str:
    taipei_tz = timezone(timedelta(hours=8))
    now = now or datetime.now(taipei_tz)
    year = now.year
    month = now.month

    if month >= 9:
        roc_year = year - 1911
        term = "上"
    elif month == 1:
        roc_year = year - 1912
        term = "上"
    elif 2 <= month <= 6:
        roc_year = year - 1912
        term = "下"
    else:
        roc_year = year - 1911
        term = "上"

    return f"{roc_year}{term}"


def extract_semester_tag(display_name: str | None) -> str | None:
    match = re.match(r"^(\d{2,3}[上下])", (display_name or "").strip())
    return match.group(1) if match else None


def strip_semester_prefix(display_name: str | None) -> str:
    cleaned = re.sub(r"^\d{2,3}[上下]", "", (display_name or "").strip())
    cleaned = cleaned.replace("_", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def course_name_for_display(course_name: str | None) -> str:
    text = strip_semester_prefix(course_name) if course_name else "-"
    matches = list(re.finditer(r"[\u4e00-\u9fff]", text))
    if matches:
        end = matches[-1].end()
        while end < len(text) and text[end] in ")）】] ":
            end += 1
        text = text[:end]
    text = re.sub(r"\s+", " ", text).strip(" -_|,")
    return text or "-"


def shorten_course_name(course_name: str | None, max_len: int = 28) -> str:
    text = course_name_for_display(course_name)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def shorten_title(title: str | None, max_len: int = 32) -> str:
    text = re.sub(r"\s+", " ", (title or "").strip())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def is_meaningful_grade(score: Any) -> bool:
    text = str(score or "").strip()
    return bool(text) and text != "-"


def normalize_due_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None

    taipei_tz = timezone(timedelta(hours=8))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=taipei_tz)
    else:
        dt = dt.astimezone(taipei_tz)
    return dt


def parse_due_at_sort_key(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        return datetime.max.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


def is_discord_user_key(user_key: Any) -> bool:
    return str(user_key or "").startswith("discord:")


def discord_bold(text: Any, user_key: Any = None) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    return f"**{raw}**" if is_discord_user_key(user_key) else raw


def discord_relative_due_tag(value: Any, user_key: Any = None) -> str:
    if not is_discord_user_key(user_key):
        return ""
    dt = normalize_due_at(value)
    if dt is None:
        return ""
    return f" <t:{int(dt.timestamp())}:R>"


def discord_full_due_tag(value: Any, user_key: Any = None) -> str:
    if not is_discord_user_key(user_key):
        return ""
    dt = normalize_due_at(value)
    if dt is None:
        return ""
    return f"<t:{int(dt.timestamp())}:F>"


def format_due_at_for_display(value: Any, user_key: Any = None) -> str:
    if not value:
        return "N/A"

    dt = normalize_due_at(value)
    if dt is None:
        return str(value)

    if is_discord_user_key(user_key):
        return f"{discord_full_due_tag(value, user_key)} · {discord_relative_due_tag(value, user_key).strip()}"

    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    return dt.strftime("%m/%d") + f" ({weekdays[dt.weekday()]}) " + dt.strftime("%H:%M") + discord_relative_due_tag(value, user_key)


def format_due_at_full(value: Any, user_key: Any = None) -> str:
    if not value:
        return "N/A"
    dt = normalize_due_at(value)
    if dt is None:
        return str(value)
    if is_discord_user_key(user_key):
        return f"{discord_full_due_tag(value, user_key)} · {discord_relative_due_tag(value, user_key).strip()}"
    return dt.strftime("%Y/%m/%d %H:%M") + discord_relative_due_tag(value, user_key)


def format_event_type_label(event_type: str) -> str:
    mapping = {
        "calendar": "行事曆",
        "homework": "作業",
        "exam": "考試",
    }
    return mapping.get(event_type, event_type)
