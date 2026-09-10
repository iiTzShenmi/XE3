from __future__ import annotations

from typing import Any, Awaitable, Callable

import discord

from agent.platforms.discord.components_v2 import (
    build_embed_layout,
    build_reminder_layout,
    build_select_layout,
    build_text_layout,
    validate_layout,
)
from agent.platforms.discord.message_utils import chunk_text, extract_embed_items
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
from agent.platforms.discord.views import DiscordViewCallbacks

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
    trimmed = list(entries[:MAX_SELECT_OPTIONS])
    back_command = selector_back_command(trimmed)
    if not back_command:
        return trimmed
    # Never discard a real option just to add navigation. Full selectors keep all
    # 25 entries; smaller selectors can still include the requested back item.
    if len(trimmed) >= MAX_SELECT_OPTIONS:
        return trimmed
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
    return trimmed[:MAX_SELECT_OPTIONS]


def _selector_summary(
    selector_candidates: list[tuple[discord.Embed, list[dict[str, str]]]],
    entries: list[tuple[str, str, dict[str, str]]],
) -> discord.Embed:
    summary_entries = entries[:MAX_SELECT_OPTIONS]
    summary = build_file_selector_summary(selector_candidates[:MAX_SELECT_OPTIONS], summary_entries)
    if summary is None:
        summary = build_news_selector_summary(summary_entries)
    if summary is None:
        summary = build_timeline_selector_summary(selector_candidates[:MAX_SELECT_OPTIONS], summary_entries)
    if summary is None:
        summary = build_grouped_selector_summary(summary_entries)
    if summary is not None:
        summary.colour = _selector_accent(summary_entries)
        return summary

    summary = discord.Embed(
        title=select_summary_title(summary_entries),
        description="請從下方下拉選單挑一個，我會直接幫你打開，不洗版。",
        color=discord.Color.blurple(),
    )
    for idx, (label, desc, action) in enumerate(summary_entries, start=1):
        value = (desc[:1024] or "點選後開啟檔案") if is_file_entry((label, desc, action)) else (desc[:1024] or "點選後查看詳情")
        summary.add_field(name=f"{display_index_emoji(idx)} {label[:100]}", value=value, inline=False)
    return summary


def _selector_accent(entries: list[tuple[str, str, dict[str, str]]]) -> discord.Colour:
    meta = action_meta(entries[0][2]) if entries else {}
    selector_kind = str(meta.get("selector_kind") or "")
    if selector_kind.startswith("grade"):
        return discord.Colour.from_rgb(124, 58, 237)
    if selector_kind in {"file", "file_folder"}:
        return discord.Colour.from_rgb(37, 99, 235)
    if selector_kind == "news_item":
        return discord.Colour.from_rgb(2, 132, 199)
    return discord.Colour.from_rgb(15, 118, 110)


async def _edit_with_layout(message: discord.Message, layout: discord.ui.LayoutView) -> None:
    validate_layout(layout)
    # Discord requires every legacy field to be explicitly cleared when a message
    # is first converted to Components v2. The v2 flag is irreversible afterward.
    await message.edit(content=None, embeds=[], attachments=[], view=layout)


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
        view_entries = with_back_entry(selector_entries)
        summary = _selector_summary(selector_candidates, selector_entries)
        layout = build_select_layout(summary, callbacks=callbacks, user_id=user_id, entries=view_entries)
        await _edit_with_layout(message, layout)
        return True

    if embeds:
        if content:
            embeds[0].description = f"{content}\n\n{embeds[0].description or ''}".strip()
        if is_reminder_actions(actions):
            layout = build_reminder_layout(
                embeds[0],
                callbacks=callbacks,
                user_id=user_id,
                enabled=reminder_enabled_from_embed(embeds[0]),
                schedule=reminder_schedule_from_embed(embeds[0]),
            )
        else:
            layout = build_embed_layout(embeds[:6], callbacks=callbacks, user_id=user_id, actions=actions)
        await _edit_with_layout(message, layout)
        return True

    if content:
        await _edit_with_layout(message, build_text_layout(content))
        return True
    return False


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

    def _send_layout(target_obj, layout: discord.ui.LayoutView):
        validate_layout(layout)
        if isinstance(target_obj, discord.Interaction):
            if not target_obj.response.is_done() and not sent_any:
                return target_obj.response.send_message(view=layout, ephemeral=ephemeral)
            return target_obj.followup.send(view=layout, ephemeral=ephemeral)
        return target_obj.send(view=layout)

    async def flush_pending() -> None:
        nonlocal sent_any, pending_embeds, pending_actions
        if not pending_embeds:
            return
        first_embed = pending_embeds[0] if pending_embeds else None
        if is_reminder_actions(pending_actions):
            layout = build_reminder_layout(
                first_embed,
                callbacks=callbacks,
                user_id=user_id,
                enabled=reminder_enabled_from_embed(first_embed),
                schedule=reminder_schedule_from_embed(first_embed),
            )
            await _send_layout(target, layout)
            sent_any = True
        else:
            remaining = list(pending_embeds)
            first_layout = True
            while remaining:
                layout = build_embed_layout(
                    remaining,
                    callbacks=callbacks,
                    user_id=user_id,
                    actions=pending_actions if first_layout else None,
                )
                consumed = max(1, len(layout.children))
                await _send_layout(target, layout)
                sent_any = True
                remaining = remaining[consumed:]
                first_layout = False
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
        view_entries = with_back_entry(entries)
        summary = _selector_summary(chunk, entries)
        layout = build_select_layout(summary, callbacks=callbacks, user_id=user_id, entries=view_entries)
        await _send_layout(target, layout)
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
            for chunk in chunk_text(text):
                await _send_layout(target, build_text_layout(chunk))
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
