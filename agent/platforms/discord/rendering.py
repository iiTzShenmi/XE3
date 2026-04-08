import re
from typing import Any

import discord

from agent.features.e3.views.payloads import META_KEY


_EMOJI_INDEX = {
    1: "1️⃣",
    2: "2️⃣",
    3: "3️⃣",
    4: "4️⃣",
    5: "5️⃣",
    6: "6️⃣",
    7: "7️⃣",
    8: "8️⃣",
    9: "9️⃣",
    10: "🔟",
}

_SECTION_HEADINGS = {
    "🟠 作業",
    "🔴 考試提醒",
    "🔴 考試",
    "🟢 行事曆",
    "📎 檔案",
    "📎 教材",
    "📎 教材 / 檔案",
    "📤 已繳檔案",
    "📎 老師附件",
    "課綱重點",
    "成績摘要",
    "課綱考試提醒",
    "⚙️ 快速操作",
    "🧭 提醒節奏",
    "你會收到什麼",
    "作業",
    "行事曆",
    "檔案",
    "教材",
}


def _normalize_heading(text: str) -> str:
    cleaned = re.sub(r"[*`_]+", "", str(text or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def is_section_heading(text: str) -> bool:
    return _normalize_heading(text) in _SECTION_HEADINGS


def section_divider(label: str) -> str:
    clean = _normalize_heading(label)
    return f"── {clean} ──"


def strong_section_divider(label: str) -> str:
    clean = _normalize_heading(label)
    return f"━━━━━━━━━━━━\n{clean}\n━━━━━━━━━━━━"


def format_discord_text(text: str) -> str:
    raw_lines = [line.rstrip() for line in str(text or "").splitlines()]
    if not raw_lines:
        return ""

    formatted: list[str] = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            if formatted and formatted[-1] != "":
                formatted.append("")
            continue
        if is_section_heading(stripped):
            while formatted and formatted[-1] == "":
                formatted.pop()
            if formatted:
                formatted.append("")
            formatted.append(section_divider(stripped))
            formatted.append("")
            continue
        formatted.append(stripped)

    while formatted and formatted[-1] == "":
        formatted.pop()

    compacted: list[str] = []
    previous_blank = False
    for line in formatted:
        if line == "":
            if not previous_blank:
                compacted.append("")
            previous_blank = True
            continue
        compacted.append(line)
        previous_blank = False
    return "\n".join(compacted).strip()


def display_index_emoji(idx: int) -> str:
    return _EMOJI_INDEX.get(idx, f"{idx}.")


def flatten_bubble_text(node: Any) -> list[str]:
    lines: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "text":
            text = str(node.get("text") or "").strip()
            if text:
                lines.append(text)
        for key in ("contents", "header", "body", "footer", "hero"):
            if key in node:
                lines.extend(flatten_bubble_text(node[key]))
    elif isinstance(node, list):
        for item in node:
            lines.extend(flatten_bubble_text(item))
    return lines


def flatten_bubble_description(node: Any) -> list[str]:
    lines: list[str] = []
    if isinstance(node, dict):
        node_type = str(node.get("type") or "")
        if node_type == "text":
            text = str(node.get("text") or "").strip()
            if text:
                lines.append(text)
        elif node_type == "separator":
            lines.append("")
        for key in ("contents", "header", "body", "footer", "hero"):
            if key in node:
                lines.extend(flatten_bubble_description(node[key]))
    elif isinstance(node, list):
        for item in node:
            lines.extend(flatten_bubble_description(item))
    return lines


def bubble_title(bubble: dict[str, Any]) -> str:
    header = bubble.get("header") or {}
    texts = [line for line in flatten_bubble_text(header) if line]
    if texts:
        if len(texts) >= 2:
            return texts[1]
        return texts[0]
    body_texts = [line for line in flatten_bubble_text(bubble.get("body") or {}) if line]
    return body_texts[0] if body_texts else "XE3"


def bubble_header_lines(bubble: dict[str, Any]) -> list[str]:
    header = bubble.get("header") or {}
    return [line for line in flatten_bubble_text(header) if line]


def bubble_description(bubble: dict[str, Any]) -> str:
    parts: list[str] = []
    body = bubble.get("body") or {}
    footer = bubble.get("footer") or {}
    parts.extend(flatten_bubble_description(body))
    footer_lines = flatten_bubble_description(footer)
    if footer_lines:
        parts.append("")
        parts.extend(footer_lines)
    cleaned: list[str] = []
    previous_blank = False
    for line in parts:
        if line is None:
            continue
        if line == "":
            if not previous_blank:
                cleaned.append("")
            previous_blank = True
            continue
        cleaned.append(line)
        previous_blank = False
    text = "\n".join(cleaned).strip()
    text = format_discord_text(text)
    return text[:4000] if text else "沒有更多內容。"


def hex_to_color(value: str | None) -> discord.Color | None:
    raw = str(value or "").strip().lstrip("#")
    if len(raw) != 6:
        return None
    try:
        return discord.Color(int(raw, 16))
    except ValueError:
        return None


def action_meta(action: dict[str, Any] | None) -> dict[str, Any]:
    raw = (action or {}).get(META_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def embed_option_description(embed: discord.Embed, action: dict[str, str] | None = None) -> str:
    meta = action_meta(action)
    option_description = str(meta.get("option_description") or "").strip()
    if option_description:
        return option_description[:100]
    if str(meta.get("entry_kind") or "").strip() == "timeline_event":
        parts = [
            str(meta.get("course_name") or "").strip(),
            str(meta.get("due_label") or "").strip(),
            str(meta.get("event_type_label") or "").strip(),
        ]
        parts = [part for part in parts if part]
        if parts:
            return "｜".join(parts)[:100]
    file_role_label = str(meta.get("file_role_label") or "").strip()
    if file_role_label:
        return file_role_label[:100]
    action_value = str((action or {}).get("value") or "").strip()
    desc_lines = [line.strip() for line in str(embed.description or "").splitlines() if line.strip()]
    if action_value.startswith("e3 詳情"):
        course = desc_lines[0] if len(desc_lines) >= 1 else ""
        due = str(embed.title or "").strip()
        type_hint = str(getattr(getattr(embed, "footer", None), "text", "") or "").strip()
        parts = [part for part in (course, due, type_hint) if part]
        if parts:
            return "｜".join(parts)[:100]
    footer_text = str(getattr(getattr(embed, "footer", None), "text", "") or "").strip()
    if footer_text:
        if footer_text == "作業附件":
            return "老師附件"
        if footer_text == "已繳檔案":
            return "你的提交"
        return footer_text[:100]
    text = str(embed.description or "").replace("\n", " ").strip()
    return text[:100] if text else "點選後查看詳細內容"


def select_option_label(embed: discord.Embed, action: dict[str, str]) -> str:
    meta = action_meta(action)
    option_label = str(meta.get("option_label") or meta.get("item_title") or "").strip()
    if option_label:
        return option_label[:100]
    action_value = str(action.get("value") or "").strip()
    desc_lines = [line.strip() for line in str(embed.description or "").splitlines() if line.strip()]
    if action_value.startswith("e3 詳情"):
        if len(desc_lines) >= 2:
            return desc_lines[1][:100]
        if desc_lines:
            return desc_lines[0][:100]
    if action.get("kind") == "uri":
        footer_text = str(getattr(getattr(embed, "footer", None), "text", "") or "").strip()
        prefix = ""
        if footer_text == "作業附件":
            prefix = "📎 老師附件｜"
        elif footer_text == "已繳檔案":
            prefix = "📤 你的提交｜"
        if desc_lines:
            return f"{prefix}{desc_lines[0][:97]}".strip()[:100]
    return str(embed.title or action.get("label") or "項目")[:100]


def is_file_entry(entry: tuple[str, str, dict[str, str]]) -> bool:
    if not entry:
        return False
    meta = action_meta(entry[2] or {})
    if str(meta.get("entry_kind") or "").strip() == "file":
        return True
    return bool((entry[2] or {}).get("kind") == "uri")


def repeated_message_label(entries: list[tuple[str, str, dict[str, str]]]) -> str | None:
    if not entries:
        return None
    actions = [entry[2] or {} for entry in entries]
    if not all(str(action.get("kind") or "") == "message" for action in actions):
        return None
    meta_labels = {str(action_meta(action).get("group_label") or "").strip() for action in actions}
    meta_labels.discard("")
    if len(meta_labels) == 1:
        return next(iter(meta_labels))
    labels = {str(action.get("label") or "").strip() for action in actions}
    labels.discard("")
    if len(labels) == 1:
        return next(iter(labels))
    return None


def all_file_entries(entries: list[tuple[str, str, dict[str, str]]]) -> bool:
    return bool(entries) and all(is_file_entry(entry) for entry in entries)


def select_summary_title(entries: list[tuple[str, str, dict[str, str]]]) -> str:
    if entries:
        meta = action_meta(entries[0][2] or {})
        explicit = str(meta.get("selector_summary_title") or "").strip()
        if explicit:
            return explicit
        selector_kind = str(meta.get("selector_kind") or "").strip()
        if selector_kind == "timeline_event":
            return "選擇近期事件"
        if selector_kind == "file":
            return "選擇檔案"
        if selector_kind == "course_homework_detail":
            return "選擇作業詳情"
    if entries and all(is_file_entry(entry) for entry in entries):
        return "選擇檔案"
    repeated_label = repeated_message_label(entries)
    if repeated_label and "詳情" in repeated_label:
        return "選擇作業詳情"
    if repeated_label == "查看檔案":
        return "選擇教材"
    if repeated_label:
        return f"選擇要{repeated_label}的項目"
    return "選擇項目"


def _parse_timeline_selector_candidate(embed: discord.Embed, action: dict[str, str]) -> dict[str, str] | None:
    meta = action_meta(action)
    if str(meta.get("entry_kind") or "").strip() == "timeline_event":
        return {
            "event_type": str(meta.get("event_type") or ""),
            "course": str(meta.get("course_name") or ""),
            "title": str(meta.get("item_title") or ""),
            "due_full": str(meta.get("due_full") or meta.get("due_label") or ""),
            "due_relative": str(meta.get("due_relative") or ""),
        }
    action_value = str(action.get("value") or "").strip()
    if not action_value.startswith("e3 詳情"):
        return None
    desc_lines = [line.strip() for line in str(embed.description or "").splitlines() if line.strip()]
    if len(desc_lines) < 3:
        return None
    type_hint = desc_lines[2]
    if "作業" in type_hint:
        event_type = "homework"
    elif "考試" in type_hint:
        event_type = "exam"
    elif "行事曆" in type_hint:
        event_type = "calendar"
    else:
        return None
    due_text = str(embed.title or "").strip()
    due_full = due_text
    due_relative = ""
    if "·" in due_text:
        parts = [part.strip() for part in due_text.split("·", 1)]
        if len(parts) == 2:
            due_full, due_relative = parts
    return {
        "event_type": event_type,
        "course": desc_lines[0],
        "title": desc_lines[1],
        "due_full": due_full,
        "due_relative": due_relative,
    }


def build_timeline_selector_summary(
    candidates: list[tuple[discord.Embed, list[dict[str, str]]]],
    entries: list[tuple[str, str, dict[str, str]]],
) -> discord.Embed | None:
    parsed_rows: list[dict[str, str]] = []
    for embed, actions in candidates:
        action = next((action for action in actions if action.get("kind") in {"message", "uri"} and action.get("value")), None)
        if not action:
            return None
        parsed = _parse_timeline_selector_candidate(embed, action)
        if not parsed:
            return None
        parsed_rows.append(parsed)

    if not parsed_rows:
        return None

    sections: dict[str, list[str]] = {"homework": [], "exam": [], "calendar": []}
    for parsed in parsed_rows:
        if parsed["event_type"] == "homework":
            prefix = "📝 作業"
        elif parsed["event_type"] == "exam":
            prefix = "⚠️ 考試"
        else:
            prefix = "🗓️ 行事曆"
        relative = f" **{parsed['due_relative']}**" if parsed["due_relative"] else ""
        sections[parsed["event_type"]].append(
            "\n".join(
                [
                    f"__INDEX__ **{parsed['title']}**",
                    f"{prefix}｜{parsed['course']}{relative}",
                    f"🗓️ **{parsed['due_full']}**",
                ]
            )
        )

    ordered_section_keys = ["homework", "exam", "calendar"]
    display_counter = 1

    def _renumber(lines: list[str]) -> list[str]:
        nonlocal display_counter
        numbered: list[str] = []
        for block in lines:
            numbered.append(block.replace("__INDEX__", display_index_emoji(display_counter), 1))
            display_counter += 1
        return numbered

    body_lines = ["請從下方下拉選單挑一個，我會直接幫你打開，不洗版。", ""]

    body_lines.extend(
        [
            strong_section_divider("🟠 作業"),
            "",
            "\n\n".join(_renumber(sections["homework"])) if sections["homework"] else "🎉 目前沒有未完成作業",
            "",
            strong_section_divider("🔴 考試"),
            "",
            "\n\n".join(_renumber(sections["exam"])) if sections["exam"] else "🎉 目前沒有近期考試",
        ]
    )
    if sections["calendar"]:
        body_lines.extend(
            [
                "",
                strong_section_divider("🟢 行事曆"),
                "",
                "\n\n".join(_renumber(sections["calendar"])),
            ]
        )

    return discord.Embed(
        title="選擇作業詳情",
        description="\n".join(body_lines).strip(),
        color=discord.Color.blurple(),
    )


def build_file_selector_summary(
    candidates: list[tuple[discord.Embed, list[dict[str, str]]]],
    entries: list[tuple[str, str, dict[str, str]]],
) -> discord.Embed | None:
    if not entries or not all(is_file_entry(entry) for entry in entries):
        return None

    teacher_lines: list[str] = []
    submitted_lines: list[str] = []
    other_lines: list[str] = []

    for (embed, _actions), (_, _, action) in zip(candidates, entries):
        meta = action_meta(action)
        desc_lines = [line.strip() for line in str(embed.description or "").splitlines() if line.strip()]
        filename = str(meta.get("item_title") or "").strip() or (desc_lines[0] if desc_lines else str(embed.title or "未命名檔案").strip())
        footer_text = str(meta.get("file_role_label") or "").strip() or str(getattr(getattr(embed, "footer", None), "text", "") or "").strip()
        line = f"▶️ {filename}"
        if footer_text == "作業附件":
            teacher_lines.append(line)
        elif footer_text == "已繳檔案":
            submitted_lines.append(line)
        else:
            other_lines.append(line)

    sections: list[str] = ["請從下方下拉選單挑一個，我會直接幫你打開，不洗版。"]
    if teacher_lines:
        sections.extend(["", strong_section_divider("📎 老師附件"), "", "\n".join(teacher_lines)])
    if submitted_lines:
        sections.extend(["", strong_section_divider("📤 你的提交"), "", "\n".join(submitted_lines)])
    if other_lines:
        sections.extend(["", strong_section_divider("📎 檔案"), "", "\n".join(other_lines)])
    if len(sections) == 1:
        return None
    return discord.Embed(
        title="選擇檔案",
        description="\n".join(sections).strip(),
        color=discord.Color.blurple(),
    )


def build_news_selector_summary(
    entries: list[tuple[str, str, dict[str, str]]],
) -> discord.Embed | None:
    if not entries:
        return None
    metas = [action_meta(action) for _, _, action in entries]
    if not metas or not all(str(meta.get("entry_kind") or "").strip() == "news_item" for meta in metas):
        return None

    grouped: dict[str, list[tuple[str, str]]] = {}
    for label, desc, action in entries:
        meta = action_meta(action)
        course_name = str(meta.get("course_name") or "").strip() or "未分類課程"
        group = grouped.setdefault(course_name, [])
        clean_label = str(label or "未命名公告").strip()
        clean_desc = str(desc or "").strip()
        time_only = clean_desc.split("｜")[-1].strip() if "｜" in clean_desc else clean_desc
        group.append((clean_label, time_only))

    sections = ["請從下方下拉選單挑一則，我會直接幫你打開，不洗版。"]
    display_counter = 1
    for course_name, lines in grouped.items():
        rendered_lines: list[str] = []
        for clean_label, clean_desc in lines:
            prefix = display_index_emoji(display_counter)
            display_counter += 1
            if clean_desc:
                rendered_lines.append(f"{prefix} **{clean_label}**\n　🕒 {clean_desc}")
            else:
                rendered_lines.append(f"{prefix} **{clean_label}**")
        sections.extend(
            [
                "",
                strong_section_divider(f"📚 {course_name}"),
                "",
                "\n\n".join(rendered_lines),
            ]
        )

    return discord.Embed(
        title="選擇公告",
        description="\n".join(sections).strip(),
        color=discord.Color.blurple(),
    )


def build_grouped_selector_summary(
    entries: list[tuple[str, str, dict[str, str]]],
) -> discord.Embed | None:
    if not entries or any(is_file_entry(entry) for entry in entries):
        return None

    first_meta = action_meta(entries[0][2] or {}) if entries else {}
    repeated_label = repeated_message_label(entries)
    explicit_section = str(first_meta.get("selector_section") or "").strip()
    explicit_title = str(first_meta.get("selector_summary_title") or "").strip()
    if explicit_section:
        section_name = explicit_section
        title = explicit_title or select_summary_title(entries)
    elif repeated_label == "查看檔案":
        section_name = "📎 教材"
        title = "選擇教材"
    elif repeated_label and "詳情" in repeated_label:
        section_name = "📘 項目"
        title = "選擇詳情"
    elif repeated_label and "課程" in repeated_label:
        section_name = "📚 課程"
        title = "選擇課程"
    else:
        section_name = "📚 項目"
        title = select_summary_title(entries)

    lines: list[str] = []
    for label, desc, _action in entries:
        clean_label = str(label or "").strip()
        clean_desc = str(desc or "").strip()
        if clean_desc:
            lines.append(f"▶️ {clean_label}\n　{clean_desc}")
        else:
            lines.append(f"▶️ {clean_label}")

    return discord.Embed(
        title=title,
        description="\n".join(
            [
                "請從下方下拉選單挑一個，我會直接幫你打開，不洗版。",
                "",
                strong_section_divider(section_name),
                "",
                "\n\n".join(lines),
            ]
        ).strip(),
        color=discord.Color.blurple(),
    )
