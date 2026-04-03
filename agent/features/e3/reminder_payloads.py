from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.features.weather.city_data import CITY_COORDINATES
from agent.features.weather.weather_api import get_weather

DEFAULT_LOOKAHEAD_HOURS = 36
DEFAULT_SCHEDULE = ["09:00", "21:00"]
COUNTDOWN_HOURS = [12, 2]
DEFAULT_BRIEFING_LOCATION = "新竹市東區"
DEFAULT_BRIEFING_COORD_KEY = "新竹市"


def taipei_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def row_value(row: Any, key: str, default: Any = None) -> Any:
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


def load_schedule(row: Any) -> list[str]:
    raw = row_value(row, "schedule_json")
    if not raw:
        return list(DEFAULT_SCHEDULE)
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return list(DEFAULT_SCHEDULE)
    if not isinstance(parsed, list):
        return list(DEFAULT_SCHEDULE)
    normalized = []
    for value in parsed:
        slot = str(value or "").strip()
        if slot and slot not in normalized:
            normalized.append(slot)
    return normalized or list(DEFAULT_SCHEDULE)


def format_due_label(value: Any) -> str:
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


def is_discord_target(user_key: str | None) -> bool:
    return str(user_key or "").startswith("discord:")


def discord_due_label(value: Any, user_key: str | None) -> str:
    if not is_discord_target(user_key):
        return format_due_label(value)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    taipei_tz = timezone(timedelta(hours=8))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=taipei_tz)
    else:
        dt = dt.astimezone(taipei_tz)
    ts = int(dt.timestamp())
    return f"<t:{ts}:F> · <t:{ts}:R>"


def course_name_for_display(text: Any) -> str:
    text = str(text or "").strip().replace("_", " ")
    matches = list(re.finditer(r"[\u4e00-\u9fff]", text))
    if matches:
        end = matches[-1].end()
        while end < len(text) and text[end] in ")）】] ":
            end += 1
        text = text[:end]
    return text.strip(" -_|,") or "-"


def count_event_types(rows: list[Any]) -> dict[str, int]:
    counts = {"homework": 0, "exam": 0, "calendar": 0}
    for row in rows:
        event_type = str(row_value(row, "event_type", "") or "").strip()
        if event_type in counts:
            counts[event_type] += 1
    return counts


def briefing_weather_line() -> str | None:
    coordinates = CITY_COORDINATES.get(DEFAULT_BRIEFING_COORD_KEY)
    if not coordinates:
        return None
    lat, lon = coordinates
    try:
        weather = get_weather(lat, lon)
    except Exception:
        return None
    return (
        f"🌤️ {DEFAULT_BRIEFING_LOCATION}｜"
        f"{weather['temperature']}°C"
        f"｜體感 {weather['apparent_temperature']}°C"
        f"｜降雨 {weather['precipitation_probability']}%"
    )


def morning_brief_lines(rows: list[Any]) -> list[str]:
    now = taipei_now()
    today = now.date()
    counts = count_event_types(rows)
    today_rows = []
    for row in rows:
        try:
            dt = datetime.fromisoformat(str(row_value(row, "due_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        if dt.astimezone(timezone(timedelta(hours=8))).date() == today:
            today_rows.append(row)

    lines = ["Good morning. Here's your E3 briefing for today."]
    weather_line = briefing_weather_line()
    if weather_line:
        lines.append(weather_line)
    summary_bits = []
    if counts["homework"]:
        summary_bits.append(f"{counts['homework']} assignment(s)")
    if counts["exam"]:
        summary_bits.append(f"{counts['exam']} exam(s)")
    if counts["calendar"]:
        summary_bits.append(f"{counts['calendar']} calendar item(s)")
    if summary_bits:
        lines.append("In the next 36 hours: " + ", ".join(summary_bits) + ".")
    if today_rows:
        lines.append(f"Due today: {len(today_rows)} item(s).")
        first_row = min(today_rows, key=lambda row: str(row_value(row, "due_at", "") or ""))
        course_name = course_name_for_display(row_value(first_row, "course_name") or row_value(first_row, "course_id") or "-")
        lines.append(f"Next up: {format_due_label(row_value(first_row, 'due_at'))} {course_name} - {row_value(first_row, 'title', '-')}")
    else:
        lines.append("Nothing is due today so far.")
    return lines


def format_digest(rows: list[Any], slot_text: str, user_key: str | None = None) -> str:
    lines = [f"⏰ **E3 提醒 {slot_text}**" if is_discord_target(user_key) else f"⏰ E3 提醒 {slot_text}"]
    if slot_text == "09:00":
        lines.extend(morning_brief_lines(rows))
        lines.append("")
    lines.append("──────────" if is_discord_target(user_key) else "")
    lines.append("🚨 **接下來 36 小時內的重點事件**" if is_discord_target(user_key) else "未來 36 小時內的重點事件：")
    for idx, row in enumerate(rows, start=1):
        course_name = course_name_for_display(row_value(row, "course_name") or row_value(row, "course_id") or "-")
        label = {"exam": "🧪", "homework": "📝", "calendar": "🗓️"}.get(row_value(row, "event_type"), "📌")
        due_label = discord_due_label(row_value(row, "due_at"), user_key)
        if is_discord_target(user_key):
            lines.append(f"{label} **{course_name}**")
            lines.append(f"• {row_value(row, 'title', '-')}")
            lines.append(f"• Due {due_label}")
        else:
            lines.append(f"{idx}. {due_label} {label} {course_name}")
            lines.append(f"   {row_value(row, 'title', '-')}")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def build_digest_payload(rows: list[Any], slot_text: str, user_key: str | None = None) -> str | None:
    if not rows:
        return None
    return format_digest(rows, slot_text, user_key=user_key)


def build_empty_digest_payload(slot_text: str, user_key: str | None = None) -> str:
    if is_discord_target(user_key):
        lines = [f"⏰ **E3 提醒 {slot_text}**"]
        if slot_text == "09:00":
            weather_line = briefing_weather_line()
            lines.append("Good morning. XE3 先幫你看過了，接下來 36 小時內沒有新的截止事件。")
            if weather_line:
                lines.append(weather_line)
            lines.append("")
            lines.append("🎉 **今天暫時沒有作業或考試壓線，先安心過你的早上。**")
        else:
            lines.append("🎉 **今晚暫時沒有新的作業或考試壓線，可以安心休息。**")
        return "\n".join(lines)
    if slot_text == "09:00":
        return "⏰ E3 提醒 09:00\n未來 36 小時內沒有新的截止事件，今天先安心。"
    return "⏰ E3 提醒 21:00\n未來 36 小時內沒有新的截止事件，今晚可以安心休息。"


def format_countdown_payload(row: Any, hours_left: int, user_key: str | None = None) -> str:
    course_name = course_name_for_display(row_value(row, "course_name") or row_value(row, "course_id") or "-")
    label = {"exam": "🧪", "homework": "📝", "calendar": "🗓️"}.get(row_value(row, "event_type"), "📌")
    due_label = discord_due_label(row_value(row, "due_at"), user_key)
    if is_discord_target(user_key):
        return (
            f"⚠️ **Deadline creeping up: {hours_left}h left**\n"
            f"{label} **{course_name}**\n"
            f"• {row_value(row, 'title', '-')}\n"
            f"• Due {due_label}"
        )
    return f"⏰ E3 倒數提醒（{hours_left} 小時）\n{due_label} {label} {course_name}\n{row_value(row, 'title', '-')}"


def extract_grade_items(courses: Any) -> list[dict[str, str]]:
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
        course_name = course_name_for_display(display_name)
        if isinstance(grades.get("grade_items"), list):
            for row in grades.get("grade_items") or []:
                if not isinstance(row, dict):
                    continue
                if row.get("is_category") or row.get("is_calculated"):
                    continue
                score_text = str(row.get("score") or "").strip()
                item_name = re.sub(r"\s+", " ", str(row.get("item_name") or "").replace("\u000b", " ")).strip()
                if not score_text or score_text == "-" or not item_name:
                    continue
                items.append({"course_id": course_id, "course_name": course_name, "item_name": item_name, "score": score_text})
            continue
        for item_name, score in grades.items():
            score_text = str(score or "").strip()
            if not score_text or score_text == "-":
                continue
            items.append({
                "course_id": course_id,
                "course_name": course_name,
                "item_name": str(item_name or "").replace("\u000b", " ").strip(),
                "score": score_text,
            })
    return items


def format_grade_payload(change: dict[str, str]) -> str:
    course_name = course_name_for_display(change["course_name"])
    score_line = f"{change['old_score']} → {change['score']}" if change.get("old_score") else change["score"]
    return "📊 **成績更新**\n" + f"**{course_name}**\n" + f"• {change['item_name']}\n" + f"• 分數：**{score_line}**"
