from __future__ import annotations

from typing import Any, Awaitable, Callable

import discord

from agent.platforms.discord.message_utils import extract_embed_items
from agent.platforms.discord.rendering import (
    action_meta,
    all_file_entries,
    build_file_selector_summary,
    build_grouped_selector_summary,
    build_news_selector_summary,
    build_timeline_selector_summary,
    display_index_emoji,
    embed_option_description,
    is_file_entry,
    repeated_message_label,
    select_option_label,
    select_summary_title,
)
from agent.platforms.discord.views import CommandButtonView, CommandSelectView, DiscordViewCallbacks, ReminderToggleView

MAX_SELECT_OPTIONS = 25
SendTextChunksFn = Callable[[Any, str], Awaitable[None]]


def primary_action(actions: list[dict[str, str]]) -> dict[str, str] | None:
    for preferred_kind in ("message", "uri"):
        for action in actions:
            if action.get("kind") == preferred_kind and action.get("value"):
                return {
                    "kind": str(action.get("kind") or ""),
                    "label": str(action.get("label") or "開啟"),
                    "value": str(action.get("value") or ""),
                    **({"xe3_meta": action["xe3_meta"]} if isinstance(action.get("xe3_meta"), dict) else {}),
                }
    return None


def is_reminder_actions(actions: list[dict[str, str]]) -> bool:
    commands = {str(action.get("value") or "").strip().lower() for action in actions if action.get("kind") == "message"}
    return "e3 remind on" in commands and "e3 remind off" in commands


def reminder_enabled_from_embed(embed: discord.Embed | None) -> bool:
    title = getattr(embed, "title", "") or ""
    description = getattr(embed, "description", "") or ""
    text = f"{title}\n{description}".lower()
    return any(token in text for token in ["已開啟", "狀態｜已開啟", "狀態：開啟", "✅ 已開啟"])


def reminder_schedule_from_embed(embed: discord.Embed | None) -> list[str]:
    import re

    text = f"{getattr(embed, 'title', '')}\n{getattr(embed, 'description', '')}"
    slots = re.findall(r"\b(?:[01]\d|2[0-3]):[0-5]\d\b", text)
    normalized: list[str] = []
    for slot in slots:
        if slot not in normalized:
            normalized.append(slot)
    return normalized or ["09:00", "21:00"]


def extract_embeds_and_views(payload: Any) -> list[tuple[discord.Embed | None, list[dict[str, str]], str | None]]:
    items: list[tuple[discord.Embed | None, list[dict[str, str]], str | None]] = []
    for item in extract_embed_items(payload):
        items.append((item.get("embed"), list(item.get("actions") or []), item.get("text")))
    return items


def selector_back_command(entries: list[tuple[str, str, dict[str, str]]]) -> str | None:
    if not entries:
        return None
    metas = [action_meta(action) for _, _, action in entries if isinstance(action, dict)]
    first_meta = metas[0] if metas else {}
    explicit = str(first_meta.get("selector_back_command") or "").strip()
    if explicit:
        return explicit

    selector_kind = str(first_meta.get("selector_kind") or "").strip()
    course_id = str(first_meta.get("course_id") or "").strip()
    course_name = str(first_meta.get("course_name") or "").strip()
    course_target = course_id or course_name

    if selector_kind == "course_summary":
        return "e3 course"
    if selector_kind == "grade_course":
        return "e3 grades"
    if selector_kind == "timeline_event":
        return "e3 timeline"
    if selector_kind == "news_item":
        return "e3 news"
    if selector_kind == "file_folder" and course_target:
        return f"e3 files {course_target}"
    if selector_kind == "course_homework_detail" and course_target:
        return f"e3 課程作業 {course_target}"
    if selector_kind == "file":
        explicit_parent = str(first_meta.get("parent_command") or "").strip()
        if explicit_parent:
            return explicit_parent
        if course_target:
            return f"e3 files {course_target}"
    return None


def with_back_entry(entries: list[tuple[str, str, dict[str, str]]]) -> list[tuple[str, str, dict[str, str]]]:
    trimmed = list(entries[: MAX_SELECT_OPTIONS - 1])
    back_command = selector_back_command(trimmed)
    if not back_command:
        return trimmed[:MAX_SELECT_OPTIONS]
    trimmed.append(
        (
            "↩️ 上一頁",
            "回到上一個結果頁面",
            {
                "kind": "message",
                "label": "上一頁",
                "value": back_command,
                "xe3_meta": {
                    "entry_kind": "navigation",
                    "group_label": "上一頁",
                    "option_label": "↩️ 上一頁",
                    "option_description": "回到上一個結果頁面",
                },
            },
        )
    )
    return trimmed

async def edit_message_from_payload(
    message: discord.Message,
    payload: Any,
    *,
    user_id: int,
    callbacks: DiscordViewCallbacks,
    send_text_chunks: SendTextChunksFn,
) -> bool:
    items = extract_embeds_and_views(payload)
    text_chunks: list[str] = []
    selector_candidates: list[tuple[discord.Embed, list[dict[str, str]]]] = []
    embeds: list[discord.Embed] = []
    actions: list[dict[str, str]] = []

    for embed, item_actions, text in items:
        if text:
            cleaned = str(text).strip()
            if cleaned:
                text_chunks.append(cleaned)
            continue
        if embed is None:
            continue
        embeds.append(embed)
        actions.extend(item_actions)
        selector_candidates.append((embed, item_actions))

    if not embeds:
        return False

    selector_entries: list[tuple[str, str, dict[str, str]]] = []
    if selector_candidates:
        for embed, item_actions in selector_candidates:
            action = primary_action(item_actions)
            if not action:
                selector_entries = []
                break
            selector_entries.append((select_option_label(embed, action), embed_option_description(embed, action), action))

    repeated_label_cards = bool(selector_entries and repeated_message_label(selector_entries))
    file_selector_cards = bool(selector_entries and all_file_entries(selector_entries) and len(selector_entries) > 1)
    should_use_selector = (
        selector_entries
        and len(selector_entries) <= MAX_SELECT_OPTIONS
        and (
            file_selector_cards
            or (len(selector_candidates) > 2 and all(primary_action(item_actions) for _, item_actions in selector_candidates))
            or (repeated_label_cards and len(selector_candidates) > 1)
        )
    )

    content = "\n\n".join(chunk for chunk in text_chunks if chunk) or None
    if should_use_selector:
        summary_entries = selector_entries[:MAX_SELECT_OPTIONS]
        view_entries = with_back_entry(selector_entries)
        summary = build_file_selector_summary(selector_candidates[:MAX_SELECT_OPTIONS], summary_entries[:MAX_SELECT_OPTIONS])
        if summary is None:
            summary = build_news_selector_summary(summary_entries[:MAX_SELECT_OPTIONS])
        if summary is None:
            summary = build_timeline_selector_summary(selector_candidates[:MAX_SELECT_OPTIONS], summary_entries[:MAX_SELECT_OPTIONS])
        if summary is None:
            summary = build_grouped_selector_summary(summary_entries[:MAX_SELECT_OPTIONS])
        if summary is None:
            summary = discord.Embed(
                title=select_summary_title(summary_entries),
                description="請從下方下拉選單挑一個，我會直接幫你打開，不洗版。",
                color=discord.Color.blurple(),
            )
            for idx, (label, desc, action) in enumerate(summary_entries[:MAX_SELECT_OPTIONS], start=1):
                value = (desc[:1024] or "點選後開啟檔案") if is_file_entry((label, desc, action)) else (desc[:1024] or "點選後查看詳情")
                summary.add_field(name=f"{display_index_emoji(idx)} {label[:100]}", value=value, inline=False)
        await message.edit(content=content, embeds=[summary], view=CommandSelectView(callbacks, user_id, view_entries))
        return True

    view = build_view(callbacks, user_id, embeds[0], actions)
    kwargs = {"content": content, "embeds": embeds[:10]}
    if view is not None:
        kwargs["view"] = view
    await message.edit(**kwargs)
    return True


def build_view(callbacks: DiscordViewCallbacks, user_id: int, embed: discord.Embed | None, actions: list[dict[str, str]]) -> discord.ui.View | None:
    if is_reminder_actions(actions):
        return ReminderToggleView(callbacks, user_id, reminder_enabled_from_embed(embed), reminder_schedule_from_embed(embed))
    return CommandButtonView(callbacks, user_id, actions[:5]) if actions else None


async def send_payload(
    target: Any,
    payload: Any,
    *,
    user_id: int,
    callbacks: DiscordViewCallbacks,
    send_text_chunks: SendTextChunksFn,
    ephemeral: bool = False,
) -> None:
    items = extract_embeds_and_views(payload)
    sent_any = False
    pending_embeds: list[discord.Embed] = []
    pending_actions: list[dict[str, str]] = []

    def _send_with(target_obj, *, embeds=None, view=None, content=None):
        kwargs = {"content": content, "embeds": embeds}
        if view is not None:
            kwargs["view"] = view
        if isinstance(target_obj, discord.Interaction):
            if not target_obj.response.is_done() and not sent_any:
                return target_obj.response.send_message(ephemeral=ephemeral, **kwargs)
            return target_obj.followup.send(ephemeral=ephemeral, **kwargs)
        return target_obj.send(**kwargs)

    async def flush_pending() -> None:
        nonlocal sent_any, pending_embeds, pending_actions
        if not pending_embeds:
            return
        first_embed = pending_embeds[0] if pending_embeds else None
        view = build_view(callbacks, user_id, first_embed, pending_actions)
        await _send_with(target, embeds=pending_embeds, view=view)
        sent_any = True
        pending_embeds = []
        pending_actions = []

    async def send_select_chunk(chunk: list[tuple[discord.Embed, list[dict[str, str]]]]) -> None:
        nonlocal sent_any
        entries: list[tuple[str, str, dict[str, str]]] = []
        for embed, actions in chunk:
            action = primary_action(actions)
            if not action:
                continue
            entries.append((select_option_label(embed, action), embed_option_description(embed, action), action))
        if not entries:
            return
        summary_entries = entries[:MAX_SELECT_OPTIONS]
        view_entries = with_back_entry(entries)
        summary = build_file_selector_summary(chunk[:MAX_SELECT_OPTIONS], summary_entries[:MAX_SELECT_OPTIONS])
        if summary is None:
            summary = build_news_selector_summary(summary_entries[:MAX_SELECT_OPTIONS])
        if summary is None:
            summary = build_timeline_selector_summary(chunk[:MAX_SELECT_OPTIONS], summary_entries[:MAX_SELECT_OPTIONS])
        if summary is None:
            summary = build_grouped_selector_summary(summary_entries[:MAX_SELECT_OPTIONS])
        if summary is None:
            summary = discord.Embed(
                title=select_summary_title(summary_entries),
                description="請從下方下拉選單挑一個，我會直接幫你打開，不洗版。",
                color=discord.Color.blurple(),
            )
            preview_entries = summary_entries[:25]
            for idx, (label, desc, action) in enumerate(preview_entries, start=1):
                value = (desc[:1024] or "點選後開啟檔案") if is_file_entry((label, desc, action)) else (desc[:1024] or "點選後查看詳情")
                summary.add_field(name=f"{display_index_emoji(idx)} {label[:100]}", value=value, inline=False)
        await _send_with(target, embeds=[summary], view=CommandSelectView(callbacks, user_id, view_entries))
        sent_any = True

    selector_candidates: list[tuple[discord.Embed, list[dict[str, str]]]] = []
    for embed, actions, text in items:
        if text:
            continue
        if embed is None:
            continue
        selector_candidates.append((embed, actions))

    repeated_label_cards = False
    file_selector_cards = False
    if selector_candidates:
        selector_entries = []
        for embed, actions in selector_candidates:
            action = primary_action(actions)
            if not action:
                selector_entries = []
                break
            selector_entries.append((select_option_label(embed, action), embed_option_description(embed, action), action))
        repeated_label_cards = bool(selector_entries and repeated_message_label(selector_entries))
        file_selector_cards = bool(selector_entries and all_file_entries(selector_entries) and len(selector_entries) > 1)

    if selector_candidates and (
        file_selector_cards
        or (len(selector_candidates) > 2 and all(primary_action(actions) for _, actions in selector_candidates))
        or (repeated_label_cards and len(selector_candidates) > 1)
    ):
        for start in range(0, len(selector_candidates), MAX_SELECT_OPTIONS):
            await send_select_chunk(selector_candidates[start : start + MAX_SELECT_OPTIONS])
        return

    for embed, actions, text in items:
        if text:
            await flush_pending()
            await send_text_chunks(target, text, ephemeral=ephemeral)
            sent_any = True
            continue
        if embed is None:
            continue
        would_exceed_embed_limit = len(pending_embeds) >= 10
        would_exceed_action_limit = len(pending_actions) + len(actions) > 5 and pending_actions
        if would_exceed_embed_limit or would_exceed_action_limit:
            await flush_pending()
        pending_embeds.append(embed)
        pending_actions.extend(actions)

    await flush_pending()
