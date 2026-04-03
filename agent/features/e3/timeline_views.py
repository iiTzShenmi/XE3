from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .common import (
    course_name_for_display,
    format_due_at_for_display,
    format_due_at_full,
    format_event_type_label,
    parse_due_at_sort_key,
    shorten_title,
)
from .file_proxy import build_proxy_url
from .payloads import attach_message_meta


def _row_value(row, key, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _event_payload(row):
    payload_json = _row_value(row, "payload_json", "") or ""
    if not payload_json:
        return {}
    try:
        return json.loads(payload_json)
    except json.JSONDecodeError:
        return {}


def _event_title_for_display(row, payload=None):
    title = str(_row_value(row, "title", "") or "").strip()
    if title.startswith("課綱考試｜"):
        title = title.split("｜", 1)[1].strip()
    title = re.sub(r"\s+", " ", title).strip()
    return title or "未命名事件"


def _event_type_label_for_display(row, payload=None):
    payload = payload or {}
    if str(payload.get("source") or "").strip() == "course_outline_syllabus" and str(_row_value(row, "event_type", "") or "") == "exam":
        return "課綱考試"
    return format_event_type_label(str(_row_value(row, "event_type", "") or ""))


def _timeline_heading(event_type):
    section_emoji = {
        "exam": "⚠️",
        "homework": "📝",
        "calendar": "🗓️",
    }.get(event_type, "📌")
    return f"{section_emoji} 【{format_event_type_label(event_type)}】"


def _timeline_rows_sorted(rows):
    return sorted(rows, key=lambda row: (parse_due_at_sort_key(_row_value(row, "due_at")), str(_row_value(row, "title", "") or "")))


def _filter_rows_within_days(rows, days: int):
    cutoff = datetime.now(timezone.utc) + timedelta(days=days)
    filtered = []
    for row in rows:
        due_dt = parse_due_at_sort_key(_row_value(row, "due_at"))
        if due_dt <= cutoff:
            filtered.append(row)
    return filtered


def _detail_file_buttons(payload, line_user_id):
    if not line_user_id:
        return []

    file_entries = []
    for item in payload.get("attachments") or []:
        if isinstance(item, dict):
            file_entries.append(("附件", item))
    for item in payload.get("submitted_files") or []:
        if isinstance(item, dict):
            file_entries.append(("已繳", item))

    buttons = []
    for idx, (label_prefix, item) in enumerate(file_entries[:3], start=1):
        source_url = str(item.get("url") or "").strip()
        title = str(item.get("name") or "").strip() or f"{label_prefix}{idx}"
        if not source_url:
            continue
        role_label = "老師附件" if label_prefix == "附件" else "你的提交"
        buttons.append(
            {
                "type": "button",
                "style": "link",
                "height": "sm",
                "action": {
                    "type": "uri",
                    "label": f"{label_prefix}{idx}",
                    "uri": build_proxy_url(line_user_id, source_url, filename=title),
                    "xe3_meta": {
                        "selector_kind": "file",
                        "entry_kind": "file",
                        "file_role_label": role_label,
                        "item_title": title,
                        "option_label": f"{'📎' if label_prefix == '附件' else '📤'} {role_label}｜{title}",
                        "option_description": role_label,
                    },
                },
            }
        )
    return buttons


def _timeline_homework_file_buttons(row, line_user_id):
    if not line_user_id or str(_row_value(row, "event_type", "") or "") != "homework":
        return []
    payload = _event_payload(row)
    return _detail_file_buttons(payload, line_user_id)[:2]


def _build_timeline_flex(rows, alt_text, hero_title, event_type=None, line_user_id=None):
    bubbles = []
    accent = {
        "exam": "#B22222",
        "homework": "#D97706",
        "calendar": "#2563EB",
    }.get(event_type, "#4B5563")
    for idx, row in rows[:10]:
        payload = _event_payload(row)
        due_at = format_due_at_for_display(_row_value(row, "due_at"), line_user_id)
        course_name = course_name_for_display(_row_value(row, "course_name") or _row_value(row, "course_id") or "-")
        title = shorten_title(_event_title_for_display(row, payload), max_len=44)
        type_label = _event_type_label_for_display(row, payload)
        footer_contents = [
            {
                "type": "button",
                "style": "primary",
                "height": "sm",
                "color": accent,
                "action": {
                    "type": "message",
                    "label": "查看詳情",
                    "text": f"e3 詳情 {idx}",
                    "xe3_meta": {
                        "selector_kind": "timeline_event",
                        "selector_section": {
                            "homework": "🟠 作業",
                            "exam": "🔴 考試",
                            "calendar": "🟢 行事曆",
                        }.get(str(_row_value(row, "event_type") or ""), "🗓️ 近期事件"),
                        "entry_kind": "timeline_event",
                        "event_type": str(_row_value(row, "event_type") or ""),
                        "item_title": title,
                        "course_name": course_name,
                        "due_label": due_at,
                        "option_label": title,
                        "option_description": f"{type_label}｜{course_name}｜{due_at}",
                    },
                },
            }
        ]
        footer_contents.extend(_timeline_homework_file_buttons(row, line_user_id))

        bubbles.append(
            {
                "type": "bubble",
                "size": "kilo",
                "xe3_meta": {
                    "selector_kind": "timeline_event",
                    "selector_section": {
                        "homework": "🟠 作業",
                        "exam": "🔴 考試",
                        "calendar": "🟢 行事曆",
                    }.get(str(_row_value(row, "event_type") or ""), "🗓️ 近期事件"),
                    "entry_kind": "timeline_event",
                    "event_type": str(_row_value(row, "event_type") or ""),
                    "item_title": title,
                    "course_name": course_name,
                    "due_label": due_at,
                    "option_label": title,
                    "option_description": f"{type_label}｜{course_name}｜{due_at}",
                    "selector_summary_title": "選擇近期事件",
                },
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": accent,
                    "paddingAll": "12px",
                    "contents": [
                        {
                            "type": "text",
                            "text": hero_title,
                            "color": "#FFFFFF",
                            "weight": "bold",
                            "size": "sm",
                        },
                        {
                            "type": "text",
                            "text": due_at,
                            "color": "#FFFFFF",
                            "size": "xs",
                            "margin": "sm",
                        },
                    ],
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "contents": [
                        {
                            "type": "text",
                            "text": course_name,
                            "weight": "bold",
                            "size": "md",
                            "wrap": True,
                        },
                        {
                            "type": "text",
                            "text": title,
                            "size": "sm",
                            "wrap": True,
                            "color": "#374151",
                        },
                        {
                            "type": "text",
                            "text": type_label,
                            "size": "xs",
                            "color": "#6B7280",
                        },
                        {
                            "type": "text",
                            "text": f"編號 #{idx}",
                            "size": "xs",
                            "color": "#6B7280",
                        },
                    ],
                },
                "footer": {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "contents": footer_contents,
                },
            }
        )

    if not bubbles:
        return None

    return attach_message_meta({
        "type": "flex",
        "altText": alt_text,
        "contents": {
            "type": "carousel",
            "contents": bubbles,
        },
    }, selector_kind="timeline_event", selector_summary_title="選擇近期事件")


def _build_detail_flex(row, index, alt_text, line_user_id=None):
    payload = _event_payload(row)
    title = _event_title_for_display(row, payload)
    type_label = _event_type_label_for_display(row, payload)

    action_buttons = [
        {
            "type": "button",
            "style": "primary",
            "height": "sm",
            "color": "#2563EB",
            "action": {
                "type": "message",
                "label": "回到時間軸",
                "text": "e3 timeline",
            },
        }
    ]
    if payload.get("url"):
        action_buttons.append(
            {
                "type": "button",
                "style": "link",
                "height": "sm",
                "action": {
                    "type": "uri",
                    "label": "開啟 E3",
                    "uri": payload["url"],
                },
            }
        )
    action_buttons.extend(_detail_file_buttons(payload, line_user_id))

    return {
        "type": "flex",
        "altText": alt_text,
        "contents": {
            "type": "bubble",
            "xe3_meta": {
                "selector_kind": "timeline_event_detail",
                "entry_kind": "timeline_event_detail",
                "item_title": title,
                "course_name": course_name_for_display(_row_value(row, "course_name") or _row_value(row, "course_id") or "-"),
                "event_type": str(_row_value(row, "event_type") or ""),
            },
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#2563EB",
                "paddingAll": "12px",
                "contents": [
                    {
                        "type": "text",
                        "text": f"事件詳情 #{index}",
                        "color": "#FFFFFF",
                        "weight": "bold",
                        "size": "md",
                    }
                ],
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {"type": "text", "text": type_label, "size": "sm", "color": "#6B7280"},
                    {"type": "text", "text": course_name_for_display(_row_value(row, "course_name") or _row_value(row, "course_id") or "-"), "weight": "bold", "wrap": True},
                    {"type": "text", "text": title, "wrap": True, "size": "sm"},
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": f"截止：{format_due_at_full(_row_value(row, 'due_at'), line_user_id)}", "size": "sm", "wrap": True},
                    {"type": "text", "text": f"顯示日期：{payload.get('date_label') or '-'}", "size": "sm", "wrap": True},
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": action_buttons,
            },
        },
    }
