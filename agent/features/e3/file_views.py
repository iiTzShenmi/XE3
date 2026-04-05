from __future__ import annotations

from .file_proxy import build_proxy_url
from .payloads import attach_message_meta


def _file_role_label(kind: str) -> str:
    return "老師附件" if kind in {"講義", "作業附件", "老師附件"} else "你的提交"


def _file_role_emoji(kind: str) -> str:
    return "📎" if kind in {"講義", "作業附件", "老師附件"} else "📤"


def _build_file_course_bubble(course_id, course_name, preview_lines):
    return {
        "type": "bubble",
        "size": "kilo",
        "xe3_meta": {
            "selector_kind": "file_folder",
            "entry_kind": "file_folder",
            "item_title": course_name,
            "course_name": course_name,
            "course_id": course_id,
            "selector_summary_title": "選擇教材",
            "selector_section": "📎 教材",
            "option_label": course_name,
            "option_description": "｜".join(preview_lines[:2]) or "點選後查看教材",
        },
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1D4ED8",
            "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": "檔案", "color": "#DBEAFE", "size": "xs"},
                {"type": "text", "text": course_name, "color": "#FFFFFF", "weight": "bold", "wrap": True},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {"type": "text", "text": course_id or "未提供課號", "size": "sm", "color": "#475569"},
                *[
                    {"type": "text", "text": line, "size": "sm", "wrap": True, "color": "#334155"}
                    for line in preview_lines[:4]
                ],
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "color": "#1D4ED8",
                    "action": {
                        "type": "message",
                        "label": "查看資料夾",
                        "text": f"e3 檔案資料夾 {course_id or course_name}",
                        "xe3_meta": {
                            "selector_kind": "file_folder",
                            "entry_kind": "file_folder",
                            "item_title": course_name,
                            "course_name": course_name,
                            "course_id": course_id,
                            "option_label": course_name,
                            "option_description": "｜".join(preview_lines[:2]) or "點選後查看教材",
                        },
                    },
                }
            ],
        },
    }


def _payload_file_entries(payload, line_user_id, title_fallback):
    if not line_user_id or not isinstance(payload, dict):
        return []

    entries = []
    for kind, items_list, accent in (
        ("作業附件", payload.get("attachments") or [], "#D97706"),
        ("已繳檔案", payload.get("submitted_files") or [], "#475569"),
    ):
        for item in items_list:
            if not isinstance(item, dict):
                continue
            source_url = str(item.get("url") or "").strip()
            title = str(item.get("name") or "").strip() or kind
            if not source_url:
                continue
            entries.append(
                {
                    "kind": kind,
                    "course_name": title_fallback,
                    "title": title,
                    "url": build_proxy_url(line_user_id, source_url, filename=title),
                    "accent": accent,
                }
            )
    return entries


def _build_file_download_flex(entries, alt_text, course_name):
    bubbles = []
    for entry in entries:
        if entry.get("_nav"):
            bubbles.append(entry["_nav"])
            continue
        if not entry.get("url"):
            continue
        role_label = _file_role_label(entry["kind"])
        role_emoji = _file_role_emoji(entry["kind"])
        parent_command = str(entry.get("parent_command") or "").strip()
        bubbles.append(
            {
                "type": "bubble",
                "size": "kilo",
                "xe3_meta": {
                    "selector_kind": "file",
                    "entry_kind": "file",
                    "item_title": entry["title"],
                    "course_name": course_name,
                    "file_role_label": role_label,
                    "option_label": f"{role_emoji} {role_label}｜{entry['title']}",
                    "option_description": role_label,
                    "selector_summary_title": "選擇檔案",
                    **({"parent_command": parent_command} if parent_command else {}),
                },
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": entry["accent"],
                    "paddingAll": "12px",
                    "contents": [
                        {"type": "text", "text": entry["kind"], "color": "#FFFFFF", "weight": "bold", "size": "sm"},
                        {"type": "text", "text": course_name, "color": "#FFFFFF", "size": "xs", "wrap": True, "margin": "sm"},
                    ],
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": entry["title"], "wrap": True, "size": "sm"},
                    ],
                },
                "footer": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "height": "sm",
                            "color": entry["accent"],
                            "action": {
                                "type": "uri",
                                "label": "開啟檔案",
                                "uri": entry["url"],
                                "xe3_meta": {
                                    "selector_kind": "file",
                                    "entry_kind": "file",
                                    "item_title": entry["title"],
                                    "course_name": course_name,
                                    "file_role_label": role_label,
                                    "option_label": f"{role_emoji} {role_label}｜{entry['title']}",
                                    "option_description": role_label,
                                    **({"parent_command": parent_command} if parent_command else {}),
                                },
                            },
                        }
                    ],
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
    }, selector_kind="file", selector_summary_title="選擇檔案")


def _build_file_folder_bubble(course_key, folder_name, file_count, index):
    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1D4ED8",
            "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": "資料夾", "color": "#DBEAFE", "size": "xs"},
                {"type": "text", "text": folder_name, "color": "#FFFFFF", "weight": "bold", "wrap": True},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": f"{file_count} 個檔案", "size": "sm", "color": "#334155"},
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "color": "#1D4ED8",
                    "action": {
                        "type": "message",
                        "label": "查看檔案",
                        "text": f"e3 檔案詳情 {course_key} f{index}",
                    },
                }
            ],
        },
    }


def _build_file_nav_bubble(course_key, page, total_pages):
    contents = []
    if page > 1:
        contents.append(
            {
                "type": "button",
                "style": "secondary",
                "height": "sm",
                "action": {
                    "type": "message",
                    "label": "上一頁",
                    "text": f"e3 檔案詳情 {course_key} p{page - 1}",
                },
            }
        )
    if page < total_pages:
        contents.append(
            {
                "type": "button",
                "style": "primary",
                "height": "sm",
                "color": "#1D4ED8",
                "action": {
                    "type": "message",
                    "label": "下一頁",
                    "text": f"e3 檔案詳情 {course_key} p{page + 1}",
                },
            }
        )
    if not contents:
        return None
    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0F172A",
            "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": f"第 {page}/{total_pages} 頁", "color": "#FFFFFF", "weight": "bold", "size": "sm"},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "檔案太多時，請用分頁查看。", "size": "sm", "wrap": True},
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": contents,
        },
    }


def _file_page_size(user_id):
    user_key = str(user_id or "")
    if user_key.startswith("discord:"):
        return 25
    return 8
