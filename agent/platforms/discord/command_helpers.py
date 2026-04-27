from __future__ import annotations

import logging
from typing import Callable

from discord import app_commands

from agent.features.e3.services.client import fetch_courses, make_user_key
from agent.features.e3.services.upload import E3UploadError, list_assignment_targets


def cached_course_choices(discord_user_id: int, user_key_builder: Callable[[int], str], logger: logging.Logger) -> list[tuple[str, str]]:
    user_key = make_user_key(user_key_builder(discord_user_id))
    try:
        courses = fetch_courses(user_key)
    except Exception:
        logger.exception("discord_course_autocomplete_failed user=%s", discord_user_id)
        return []

    rows: list[tuple[str, str]] = []
    for display_name, payload in (courses or {}).items():
        if not isinstance(payload, dict):
            continue
        course_name = str(display_name or "").strip()
        course_id = str(payload.get("_course_id") or "").strip()
        if not course_name and not course_id:
            continue
        label = f"{course_id} {course_name}".strip()[:100]
        value = course_id or course_name
        if value:
            rows.append((label, value[:100]))

    rows.sort(key=lambda row: row[0].lower())
    deduped: list[tuple[str, str]] = []
    seen_values: set[str] = set()
    for label, value in rows:
        if value in seen_values:
            continue
        seen_values.add(value)
        deduped.append((label, value))
    return deduped[:25]


async def autocomplete_course_files(
    interaction,
    current: str,
    *,
    user_key_builder: Callable[[int], str],
    logger: logging.Logger,
) -> list[app_commands.Choice[str]]:
    current_lower = str(current or "").strip().lower()
    choices = []
    for label, value in cached_course_choices(interaction.user.id, user_key_builder, logger):
        haystack = f"{label} {value}".lower()
        if current_lower and current_lower not in haystack:
            continue
        choices.append(app_commands.Choice(name=label, value=value))
    return choices[:25]


async def autocomplete_course_homework(
    interaction,
    current: str,
    *,
    user_key_builder: Callable[[int], str],
    logger: logging.Logger,
) -> list[app_commands.Choice[str]]:
    current_lower = str(current or "").strip().lower()
    try:
        course = str(getattr(interaction.namespace, "course", "") or "").strip()
    except Exception:
        course = ""
    if not course:
        return []

    try:
        targets = list_assignment_targets(user_key_builder(interaction.user.id), course)
    except E3UploadError:
        return []
    except Exception:
        logger.exception("discord_homework_autocomplete_failed user=%s course=%s", interaction.user.id, course)
        return []

    choices: list[app_commands.Choice[str]] = []
    seen_values: set[str] = set()
    for target in targets:
        haystack = f"{target.course_id} {target.course_name} {target.title} {target.cmid}".lower()
        if current_lower and current_lower not in haystack:
            continue
        status = "已繳" if target.completed else "未繳"
        due_text = format_due_for_choice(target.due_at)
        name = f"{status}｜{target.title}"
        if due_text:
            name = f"{name}｜{due_text}"
        value = target.value[:100]
        if value in seen_values:
            continue
        seen_values.add(value)
        choices.append(app_commands.Choice(name=name[:100], value=value))
    return choices[:25]


def format_due_for_choice(due_at: str) -> str:
    text = str(due_at or "").strip()
    if not text:
        return ""
    # Keep autocomplete labels compact; full due timestamps are shown in the normal E3 views.
    return text.replace("\n", " ")[:36]


def build_help_text(prefix: str) -> str:
    return (
        "🤖 XE3 Discord 助手\n"
        "──────────\n"
        "📚 E3\n"
        "• /e3 login\n"
        "• /e3 relogin\n"
        "• /e3 course\n"
        "• /e3 today\n"
        "• /e3 week\n"
        "• /e3 news\n"
        "• /e3 timeline\n"
        "• /e3 grades\n"
        "• /e3 passcalc\n"
        "• /e3 files\n"
        "• /e3 remind\n"
        "• /plot excel\n"
        "──────────\n"
        "🌦️ 工具\n"
        f"• {prefix}weather <城市>\n"
        f"• {prefix}chksys\n"
        "──────────\n"
        "🧰 備用前綴指令\n"
        "• /e3 help\n"
        f"• {prefix}e3 help\n"
        f"• {prefix}e3 login <帳號> <密碼>"
    )
