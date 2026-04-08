from __future__ import annotations

import html
import re
from typing import Any

from ..common import (
    assignment_items,
    course_name_for_display,
    format_due_at_for_display,
    is_assignment_completed,
    is_meaningful_grade,
    parse_due_at_sort_key,
    shorten_title,
)
from .file_catalog import collect_file_entries, count_file_items


def count_active_assignments(payload: dict[str, Any] | None) -> int:
    count = 0
    for item in assignment_items(payload):
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip().lower()
        if category and category not in {"in_progress", "upcoming"}:
            continue
        if is_assignment_completed(item):
            continue
        count += 1
    return count


def count_completed_assignments(payload: dict[str, Any] | None) -> int:
    count = 0
    for item in assignment_items(payload):
        if is_assignment_completed(item):
            count += 1
    return count


def count_grade_items(payload: dict[str, Any] | None) -> int:
    grades_payload = (payload or {}).get("grades") or {}
    if not isinstance(grades_payload, dict):
        return 0
    if isinstance(grades_payload.get("grade_items"), list):
        return sum(
            1
            for row in (grades_payload.get("grade_items") or [])
            if isinstance(row, dict) and is_meaningful_grade(row.get("score"))
        )
    return sum(1 for score in grades_payload.values() if is_meaningful_grade(score))


def build_course_summary(index: int, display_name: str, payload: dict[str, Any] | None, link_payload: dict[str, Any] | None) -> dict[str, Any]:
    course_id = str((payload or {}).get("_course_id") or "").strip()
    course_name = course_name_for_display(display_name)
    course_label = f"{course_id} {course_name}".strip()
    return {
        "index": index,
        "course_id": course_id,
        "course_name": course_name,
        "course_label": course_label,
        "homework_count": count_active_assignments(payload),
        "grade_count": count_grade_items(payload),
        "file_count": count_file_items(link_payload or {}),
    }


def collect_course_calendar_events(snapshot: dict[str, Any] | None, course_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for event in (snapshot or {}).get("calendar_events") or []:
        if str(event.get("course_id") or "").strip() != course_id:
            continue
        due_at = event.get("due_at")
        if not due_at:
            continue
        items.append(event)
    items.sort(key=lambda item: item.get("due_at") or "")
    return items[:3]


def normalize_title_token(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).casefold()


def assignment_completion_map(payload: dict[str, Any] | None) -> dict[str, bool]:
    mapping: dict[str, bool] = {}
    for item in assignment_items(payload):
        if not isinstance(item, dict):
            continue
        title = normalize_title_token(item.get("title") or item.get("name"))
        if not title:
            continue
        mapping[title] = is_assignment_completed(item)
    return mapping


def clean_outline_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def short_exam_topic(topic: str) -> str:
    clean = html.unescape(clean_outline_text(topic))
    if not clean:
        return ""
    direct = re.search(r"((?:Exam|Midterm|Final Exam|Quiz)\s*[\w\-()\/:. ]*)", clean, flags=re.IGNORECASE)
    if direct:
        return clean_outline_text(direct.group(1))
    zh_direct = re.search(r"((?:期中考|期末考|小考|測驗)[^｜,，;；]*)", clean)
    if zh_direct:
        return clean_outline_text(zh_direct.group(1))
    return shorten_title(clean, 36)


def short_exam_date(class_date: str) -> str:
    clean = html.unescape(clean_outline_text(class_date))
    if not clean:
        return ""
    matches = re.findall(r"(\d{4}-\d{2}-\d{2}(?:\([^)]+\))?)", clean)
    if matches:
        return matches[-1]
    return clean


def course_outline_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    timetable = payload.get("timetable") or {}
    if not isinstance(timetable, dict):
        timetable = {}

    outline = timetable.get("course_outline_data") or {}
    if not isinstance(outline, dict):
        outline = {}

    base = outline.get("base_normalized") or outline.get("base") or {}
    desc = outline.get("description_normalized") or outline.get("description") or {}
    syllabus = outline.get("syllabus_normalized") or outline.get("syllabus") or []
    if not isinstance(base, dict):
        base = {}
    if not isinstance(desc, dict):
        desc = {}
    if not isinstance(syllabus, list):
        syllabus = []

    exam_lines = []
    for row in syllabus:
        if not isinstance(row, dict):
            continue
        topic = clean_outline_text(row.get("class_data"))
        class_date = clean_outline_text(row.get("class_date"))
        if not topic or not class_date:
            continue
        if not any(keyword in topic.lower() for keyword in ("exam", "midterm", "final", "quiz", "期中", "期末", "考試", "測驗")):
            continue
        short_date = short_exam_date(class_date)
        short_topic = short_exam_topic(topic)
        if short_date and short_topic:
            exam_lines.append(f"{short_date}｜{short_topic}")

    return {
        "teacher": clean_outline_text(base.get("tea_name") or base.get("Instructors") or base.get("teacher_id")),
        "credits": clean_outline_text(base.get("cos_credit")),
        "schedule": clean_outline_text(base.get("cos_time") or desc.get("crs_meeting_time")),
        "textbook": clean_outline_text(desc.get("crs_textbook")),
        "prerequisite": clean_outline_text(desc.get("crs_prerequisite")),
        "grading": clean_outline_text(desc.get("crs_exam_score")),
        "outline": clean_outline_text(desc.get("crs_outline")),
        "meeting_place": clean_outline_text(desc.get("crs_meeting_place")),
        "contact": clean_outline_text(desc.get("crs_contact")),
        "exam_lines": exam_lines[:3],
        "syllabus_count": len(syllabus),
    }


def course_grade_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    grades = (payload or {}).get("grades") or {}
    if not isinstance(grades, dict):
        return {}

    summary = grades.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    items = grades.get("grade_items") or []
    if not isinstance(items, list):
        items = []

    graded = []
    feedback_count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        score = str(item.get("score") or "").strip()
        if score and score != "-":
            graded.append(item)
        if str(item.get("feedback") or "").strip():
            feedback_count += 1

    latest_lines = []
    for item in graded[:3]:
        title = shorten_title(str(item.get("item_name") or "評分項目"), 28)
        score = str(item.get("score") or "-").strip() or "-"
        score_range = str(item.get("range") or "").strip()
        latest_lines.append(f"{title}｜{score}" + (f" / {score_range}" if score_range and score_range != "-" else ""))

    return {
        "total_items": int(summary.get("total_items") or len(items) or 0),
        "scored_items": int(summary.get("scored_items") or len(graded) or 0),
        "feedback_count": feedback_count,
        "latest_lines": latest_lines,
    }


def collect_course_homework_items(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    items = []
    for item in assignment_items(payload):
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip().lower()
        if category and category not in {"in_progress", "upcoming"}:
            continue
        if is_assignment_completed(item):
            continue
        due_raw = item.get("due") or item.get("due_time") or item.get("due_date") or item.get("deadline") or item.get("截止")
        items.append({"title": str(item.get("title") or item.get("name") or "未命名作業").strip(), "due_at": due_raw})
    return items[:3]


def collect_course_homework_entries(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    items = []
    for item in assignment_items(payload):
        if not isinstance(item, dict):
            continue
        due_raw = item.get("due") or item.get("due_time") or item.get("due_date") or item.get("deadline") or item.get("截止")
        completed = is_assignment_completed(item)
        attachments = [entry for entry in (item.get("attachments") or []) if isinstance(entry, dict)]
        submitted_files = [entry for entry in (item.get("submitted_files") or []) if isinstance(entry, dict)]
        category = str(item.get("category") or "").strip().lower()
        if not completed and category and category not in {"in_progress", "upcoming"}:
            continue
        if completed and not (attachments or submitted_files):
            continue
        items.append({
            "title": str(item.get("title") or item.get("name") or "未命名作業").strip(),
            "due_at": due_raw,
            "completed": completed,
            "_raw": item,
        })
    items.sort(key=lambda item: (1 if item.get("completed") else 0, parse_due_at_sort_key(item.get("due_at")), item.get("title") or ""))
    return items


def build_course_homework_flex(course_name: str, course_id: str, items: list[dict[str, Any]], alt_text: str, line_user_id: str | None = None) -> dict[str, Any] | None:
    bubbles = []
    for idx, item in enumerate(items[:10], start=1):
        title = str(item.get("title") or "未命名作業").strip()
        due_at = format_due_at_for_display(item.get("due_at"), line_user_id)
        payload = item.get("_raw") or {}
        status_text = "已完成" if item.get("completed") else "未完成"
        attachment_count = len(payload.get("attachments") or [])
        submitted_count = len(payload.get("submitted_files") or [])
        footer_contents = [
            {
                "type": "button",
                "style": "primary",
                "height": "sm",
                "color": "#D97706",
                "action": {
                    "type": "message",
                    "label": "查看詳情",
                    "text": f"e3 作業詳情 {course_id or course_name} i{idx}",
                    "xe3_meta": {
                        "selector_kind": "course_homework_detail",
                        "selector_section": "🟠 作業",
                        "entry_kind": "course_homework",
                        "item_title": title,
                        "course_name": course_name,
                        "status_label": status_text,
                        "due_label": due_at,
                    },
                },
            }
        ]
        bubbles.append(
            {
                "type": "bubble",
                "size": "kilo",
                "xe3_meta": {
                    "selector_kind": "course_homework_detail",
                    "selector_section": "🟠 作業",
                    "entry_kind": "course_homework",
                    "item_title": title,
                    "course_name": course_name,
                    "status_label": status_text,
                    "due_label": due_at,
                },
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#D97706",
                    "paddingAll": "12px",
                    "contents": [
                        {"type": "text", "text": "作業", "color": "#FFFBEB", "size": "xs"},
                        {"type": "text", "text": title, "color": "#FFFFFF", "weight": "bold", "wrap": True},
                    ],
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": course_name, "wrap": True, "size": "sm"},
                        {"type": "text", "text": status_text, "size": "sm", "color": "#475569", "wrap": True},
                        {"type": "text", "text": due_at, "size": "sm", "color": "#475569", "wrap": True},
                        {"type": "text", "text": f"附件 {attachment_count}｜已繳 {submitted_count}", "size": "xs", "color": "#6B7280", "wrap": True},
                        {"type": "text", "text": f"編號 #{idx}", "size": "xs", "color": "#6B7280"},
                    ],
                },
                "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": footer_contents},
            }
        )

    if not bubbles:
        return None

    return {"type": "flex", "altText": alt_text, "xe3_meta": {"selector_kind": "course_homework_detail"}, "contents": {"type": "carousel", "contents": bubbles}}


def build_course_detail_payload(display_name: str, payload: dict[str, Any] | None, timeline_snapshot: dict[str, Any] | None, file_snapshot: dict[str, Any] | None, line_user_id: str | None) -> dict[str, Any]:
    course_id = str((payload or {}).get("_course_id") or "").strip()
    course_name = course_name_for_display(display_name)
    links = ((file_snapshot or {}).get("file_links") or {}).get(course_id) or {}
    homework_items = collect_course_homework_items(payload)
    calendar_items = collect_course_calendar_events(timeline_snapshot, course_id)
    completion_map = assignment_completion_map(payload)
    all_file_entries = collect_file_entries(course_id, course_name, links)
    file_lines = [f"{entry['kind']}｜{entry['title']}" for entry in all_file_entries[:3]]
    remaining_files = len(all_file_entries) - len(file_lines)
    if remaining_files > 0:
        file_lines.append(f"還有 {remaining_files} 個檔案，點「查看教材」查看。")

    detail = {
        "index": 0,
        "course_id": course_id,
        "course_name": course_name,
        "homework_count": count_active_assignments(payload),
        "completed_homework_count": count_completed_assignments(payload),
        "calendar_count": len(calendar_items),
        "file_count": count_file_items(links),
        "homework_lines": [
            f"{shorten_title(item['title'], 26)}｜{format_due_at_for_display(item['due_at'], line_user_id)}" for item in homework_items
        ] or ["🎉 目前沒有未完成作業"],
        "calendar_lines": [
            (
                ("✅ ~~已完成｜" if completion_map.get(normalize_title_token(item.get("title"))) else "⚠️ 未完成｜")
                + f"{shorten_title(item['title'], 26)}｜{format_due_at_for_display(item['due_at'], line_user_id)}"
                + ("~~" if completion_map.get(normalize_title_token(item.get("title"))) else "")
            )
            if completion_map.get(normalize_title_token(item.get("title"))) is not None
            else f"{shorten_title(item['title'], 26)}｜{format_due_at_for_display(item['due_at'], line_user_id)}"
            for item in calendar_items
        ] or ["🎉 目前沒有近期行事曆"],
        "file_lines": file_lines or ["目前沒有可用檔案"],
    }
    outline_info = course_outline_summary(payload)
    grade_info = course_grade_summary(payload)
    detail["course_info_lines"] = [
        f"👨‍🏫 教師｜{outline_info['teacher']}",
        f"🎓 學分｜{outline_info['credits']}",
        f"🕒 時段｜{outline_info['schedule']}",
        f"📍 地點｜{outline_info['meeting_place']}",
        f"📚 教材｜{shorten_title(outline_info['textbook'], 36)}",
        f"🧭 先修｜{shorten_title(outline_info['prerequisite'], 36)}",
        f"📊 評分｜{shorten_title(outline_info['grading'], 36)}",
    ]
    detail["course_info_lines"] = [line for line in detail["course_info_lines"] if not line.endswith("｜")]
    detail["grade_summary_lines"] = [
        f"📊 已登錄成績｜{grade_info['scored_items']} / {grade_info['total_items']}",
        f"💬 回饋項目｜{grade_info['feedback_count']}",
        *[f"• {line}" for line in grade_info["latest_lines"]],
    ]
    detail["grade_summary_lines"] = [
        line for line in detail["grade_summary_lines"] if not line.endswith("｜0 / 0") or grade_info["latest_lines"]
    ]
    if grade_info["total_items"] == 0 and not grade_info["latest_lines"]:
        detail["grade_summary_lines"] = ["📊 目前還沒有可用的成績摘要"]
    detail["exam_lines"] = [f"⚠️ {line}" for line in outline_info["exam_lines"]] or ["🎉 目前沒有辨識到近期考試提醒"]
    return detail
