import asyncio
import io
import logging
from typing import Any
from urllib.parse import urlparse

import requests

MAX_SELECT_OPTIONS = 25
E3_EXECUTION_SEMAPHORE = asyncio.Semaphore(2)

import discord
from discord import app_commands
from discord.ext import commands

from agent.config import discord_attachment_max_bytes, discord_bot_token, discord_command_prefix, discord_guild_id, public_base_url
from agent.features.e3 import handle_e3_command, run_e3_async_command
from agent.features.e3.client import fetch_courses
from agent.features.e3.db import get_discord_delivery_target, get_user_id, init_db, upsert_discord_delivery_target
from agent.features.e3.reminders import build_test_reminder_payload, start_reminder_worker
from agent.features.e3.file_proxy import FileProxyError, prepare_proxy_download, prepare_user_download
from agent.features.weather import handle_city_weather
from agent.system_status import build_system_report


logger = logging.getLogger(__name__)


def _platform_user_key(user_id: int) -> str:
    return f"discord:{user_id}"


def _remember_delivery_target(discord_user_id: int, channel_id: int | None, guild_id: int | None = None) -> None:
    if not channel_id or not guild_id:
        return
    init_db()
    upsert_discord_delivery_target(
        _platform_user_key(discord_user_id),
        str(channel_id),
        str(guild_id) if guild_id else None,
    )


async def _remember_interaction_target(interaction: discord.Interaction) -> None:
    await asyncio.to_thread(
        _remember_delivery_target,
        interaction.user.id,
        interaction.channel_id,
        interaction.guild_id,
    )


async def _remember_context_target(ctx: commands.Context) -> None:
    channel = getattr(ctx, "channel", None)
    guild = getattr(ctx, "guild", None)
    channel_id = getattr(channel, "id", None)
    guild_id = getattr(guild, "id", None)
    await asyncio.to_thread(_remember_delivery_target, ctx.author.id, channel_id, guild_id)


def _reminder_channel_payload(payload: Any, discord_user_id: int) -> Any:
    mention = f"<@{discord_user_id}> Reminder for you:"
    if isinstance(payload, str):
        return f"{mention}\n{payload}"
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if isinstance(messages, list):
            wrapped = dict(payload)
            wrapped["messages"] = [{"type": "text", "text": mention}] + list(messages)
            return wrapped
    return f"{mention}\n{payload}"


def _response_text(payload: Any) -> str:
    if isinstance(payload, dict):
        text = str(payload.get("text") or "").strip()
        if text:
            return text
        messages = payload.get("messages") or []
        parts = []
        for item in messages:
            if isinstance(item, dict) and item.get("type") == "text":
                chunk = str(item.get("text") or "").strip()
                if chunk:
                    parts.append(chunk)
        if parts:
            return "\n\n".join(parts)
        return str(payload)
    return str(payload)


def _chunk_text(text: str, limit: int = 1900) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return ["(empty response)"]
    if len(raw) <= limit:
        return [raw]

    chunks = []
    remaining = raw
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _flatten_bubble_text(node: Any) -> list[str]:
    lines: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "text":
            text = str(node.get("text") or "").strip()
            if text:
                lines.append(text)
        for key in ("contents", "header", "body", "footer", "hero"):
            if key in node:
                lines.extend(_flatten_bubble_text(node[key]))
    elif isinstance(node, list):
        for item in node:
            lines.extend(_flatten_bubble_text(item))
    return lines


def _bubble_title(bubble: dict[str, Any]) -> str:
    header = bubble.get("header") or {}
    texts = [line for line in _flatten_bubble_text(header) if line]
    if texts:
        if len(texts) >= 2:
            return texts[1]
        return texts[0]
    body_texts = [line for line in _flatten_bubble_text(bubble.get("body") or {}) if line]
    return body_texts[0] if body_texts else "XE3"


def _bubble_description(bubble: dict[str, Any]) -> str:
    parts: list[str] = []
    body = bubble.get("body") or {}
    footer = bubble.get("footer") or {}
    parts.extend(_flatten_bubble_text(body))
    footer_lines = [line for line in _flatten_bubble_text(footer) if line]
    if footer_lines:
        parts.append("")
        parts.extend(footer_lines)
    cleaned = [line for line in parts if line is not None]
    text = "\n".join(cleaned).strip()
    return text[:4000] if text else "No details provided."


def _hex_to_color(value: str | None) -> discord.Color | None:
    raw = str(value or "").strip().lstrip("#")
    if len(raw) != 6:
        return None
    try:
        return discord.Color(int(raw, 16))
    except ValueError:
        return None


async def _send_text_chunks(target, text: str, *, ephemeral: bool = False) -> None:
    chunks = _chunk_text(text)
    for idx, chunk in enumerate(chunks):
        if isinstance(target, discord.Interaction):
            if not target.response.is_done() and idx == 0:
                await target.response.send_message(chunk, ephemeral=ephemeral)
            else:
                await target.followup.send(chunk, ephemeral=ephemeral)
        else:
            await target.send(chunk)


def _is_reminder_actions(actions: list[dict[str, str]]) -> bool:
    commands = {str(action.get("value") or "").strip().lower() for action in actions if action.get("kind") == "message"}
    return "e3 remind on" in commands and "e3 remind off" in commands


def _reminder_enabled_from_embed(embed: discord.Embed | None) -> bool:
    title = getattr(embed, "title", "") or ""
    description = getattr(embed, "description", "") or ""
    text = f"{title}\n{description}".lower()
    return any(token in text for token in ["已開啟", "狀態｜已開啟", "狀態：開啟", "✅ 已開啟"])


class ReminderToggleButton(discord.ui.Button):
    def __init__(self, bot: commands.Bot, user_id: int, enabled: bool):
        self.bot = bot
        self.user_id = user_id
        self.enabled = enabled
        command_text = "e3 remind off" if enabled else "e3 remind on"
        label = "Turn Reminder Off" if enabled else "Turn Reminder On"
        style = discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success
        super().__init__(label=label, style=style)
        self.command_text = command_text

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This toggle belongs to another user's session.")
            return
        payload = await asyncio.to_thread(handle_e3_command, self.command_text, logger, _platform_user_key(self.user_id))
        edited = False
        try:
            edited = await _edit_message_from_payload(interaction.message, payload, bot=self.bot, user_id=self.user_id)
        except discord.DiscordException:
            logger.exception("discord_reminder_toggle_edit_failed user=%s", self.user_id)
        if not interaction.response.is_done():
            if edited:
                await interaction.response.defer()
            else:
                await _send_payload(interaction, payload, bot=self.bot, user_id=self.user_id)
        elif not edited:
            await _send_payload(interaction, payload, bot=self.bot, user_id=self.user_id)


class ReminderTestButton(discord.ui.Button):
    def __init__(self, bot: commands.Bot, user_id: int):
        self.bot = bot
        self.user_id = user_id
        super().__init__(label="Test Reminder", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This test button belongs to another user's session.")
            return
        internal_user_id = get_user_id(_platform_user_key(self.user_id))
        if not internal_user_id:
            await interaction.response.send_message("Please login to E3 first before testing reminders.")
            return
        await interaction.response.defer()
        payload = await asyncio.to_thread(_build_reminder_test_payload, internal_user_id)
        await _send_payload(interaction, payload, bot=self.bot, user_id=self.user_id)


class ReminderToggleView(discord.ui.View):
    def __init__(self, bot: commands.Bot, user_id: int, enabled: bool, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.add_item(ReminderToggleButton(bot, user_id, enabled))
        self.add_item(ReminderTestButton(bot, user_id))


class CommandButtonView(discord.ui.View):
    def __init__(self, bot: commands.Bot, user_id: int, actions: list[dict[str, str]], timeout: float = 600):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.user_id = user_id
        for action in actions[:5]:
            kind = action.get("kind")
            label = action.get("label") or "Open"
            if kind == "uri":
                self.add_item(discord.ui.Button(label=label[:80], url=action.get("value") or "https://discord.com"))
            elif kind == "message":
                self.add_item(_MessageCommandButton(bot, user_id, label[:80], action.get("value") or ""))


class _MessageCommandButton(discord.ui.Button):
    def __init__(self, bot: commands.Bot, user_id: int, label: str, command_text: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.bot = bot
        self.user_id = user_id
        self.command_text = command_text

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This button belongs to another user's session.")
            return
        payload = await asyncio.to_thread(handle_e3_command, f"e3 {self.command_text.strip()}" if not self.command_text.strip().lower().startswith("e3") else self.command_text.strip(), logger, _platform_user_key(self.user_id))
        edited = False
        try:
            edited = await _edit_message_from_payload(interaction.message, payload, bot=self.bot, user_id=self.user_id)
        except discord.DiscordException:
            logger.exception("discord_message_button_edit_failed user=%s command=%s", self.user_id, self.command_text)
        if not interaction.response.is_done():
            if edited:
                await interaction.response.defer()
            else:
                await _send_payload(interaction, payload, bot=self.bot, user_id=self.user_id)
        elif not edited:
            await _send_payload(interaction, payload, bot=self.bot, user_id=self.user_id)


def _primary_action(actions: list[dict[str, str]]) -> dict[str, str] | None:
    for preferred_kind in ("message", "uri"):
        for action in actions:
            if action.get("kind") == preferred_kind and action.get("value"):
                return {"kind": str(action.get("kind") or ""), "label": str(action.get("label") or "Open"), "value": str(action.get("value") or "")}
    return None


def _embed_option_description(embed: discord.Embed) -> str:
    text = str(embed.description or "").replace("\n", " ").strip()
    return text[:100] if text else "Select to open details"


def _select_option_label(embed: discord.Embed, action: dict[str, str]) -> str:
    if action.get("kind") == "uri":
        lines = [line.strip() for line in str(embed.description or "").splitlines() if line.strip()]
        if lines:
            return lines[0][:100]
    return str(embed.title or action.get("label") or "Item")[:100]


def _is_file_entry(entry: tuple[str, str, dict[str, str]]) -> bool:
    return bool(entry and (entry[2] or {}).get("kind") == "uri")


def _repeated_message_label(entries: list[tuple[str, str, dict[str, str]]]) -> str | None:
    if not entries:
        return None
    actions = [entry[2] or {} for entry in entries]
    if not all(str(action.get("kind") or "") == "message" for action in actions):
        return None
    labels = {str(action.get("label") or "").strip() for action in actions}
    labels.discard("")
    if len(labels) == 1:
        return next(iter(labels))
    return None


def _select_summary_title(entries: list[tuple[str, str, dict[str, str]]]) -> str:
    if entries and all(_is_file_entry(entry) for entry in entries):
        return "Choose a file"
    repeated_label = _repeated_message_label(entries)
    if repeated_label and "詳情" in repeated_label:
        return "Choose homework details"
    if repeated_label == "查看檔案":
        return "Choose materials"
    if repeated_label:
        return f"Choose {repeated_label}"
    return "Choose an item"


def _extract_proxy_token(url: str) -> str | None:
    base = public_base_url()
    parsed = urlparse(str(url or ""))
    if base and str(url).startswith(base + "/e3/file/"):
        return str(url).split('/e3/file/', 1)[1]
    if parsed.path.startswith('/e3/file/'):
        return parsed.path.split('/e3/file/', 1)[1]
    return None


def _download_discord_attachment(user_id: int, action: dict[str, str], fallback_name: str) -> tuple[discord.File | None, str | None]:
    source = str(action.get('value') or '').strip()
    if not source:
        return None, None

    try:
        token = _extract_proxy_token(source)
        if token:
            payload = prepare_proxy_download(token)
        else:
            payload = prepare_user_download(f"discord:{user_id}", source, filename=fallback_name, max_bytes=discord_attachment_max_bytes())
    except FileProxyError as exc:
        return None, exc.message
    except requests.RequestException:
        return None, 'Unable to download the file from E3 right now.'

    response = payload['response']
    filename = payload.get('filename') or fallback_name or 'download'
    try:
        data = response.content
    finally:
        response.close()

    if len(data) > discord_attachment_max_bytes():
        return None, 'This file is too large to upload directly to Discord.'

    return discord.File(io.BytesIO(data), filename=filename), None


class _CommandSelect(discord.ui.Select):
    def __init__(self, bot: commands.Bot, user_id: int, entries: list[tuple[str, str, dict[str, str]]]):
        self.entries = entries[:MAX_SELECT_OPTIONS]
        options = [
            discord.SelectOption(label=label[:100], description=(None if _is_file_entry(self.entries[idx]) else desc[:100]), value=str(idx))
            for idx, (label, desc, _) in enumerate(self.entries)
        ]
        super().__init__(placeholder="Choose an item", min_values=1, max_values=1, options=options)
        self.bot = bot
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This selector belongs to another user's session.")
            return
        _, desc, action = self.entries[int(self.values[0])]
        if action.get("kind") == "message":
            payload = await asyncio.to_thread(handle_e3_command, action.get("value") or "", logger, _platform_user_key(self.user_id))
            edited = False
            try:
                edited = await _edit_message_from_payload(interaction.message, payload, bot=self.bot, user_id=self.user_id)
            except discord.NotFound:
                logger.info("discord_selector_message_missing user=%s command=%s", self.user_id, action.get("value") or "")
            except discord.DiscordException:
                logger.exception("discord_selector_edit_failed user=%s command=%s", self.user_id, action.get("value") or "")
            if not interaction.response.is_done():
                if edited:
                    await interaction.response.defer()
                else:
                    await _send_payload(interaction, payload, bot=self.bot, user_id=self.user_id)
            elif not edited:
                await _send_payload(interaction, payload, bot=self.bot, user_id=self.user_id)
            return
        await interaction.response.defer(thinking=True)
        if action.get("kind") == "uri":
            selected_label = self.entries[int(self.values[0])][0] or action.get("label") or "Open file"
            file_obj, error_text = await asyncio.to_thread(_download_discord_attachment, self.user_id, action, selected_label)
            if file_obj is not None:
                embed = discord.Embed(title=selected_label, description='Sent directly from E3.', color=discord.Color.blurple())
                await interaction.followup.send(embed=embed, file=file_obj)
                return
            embed = discord.Embed(title=selected_label, description=error_text or desc or 'Open the selected file.', color=discord.Color.blurple())
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label=(action.get("label") or "Open")[:80], url=action.get("value") or "https://discord.com"))
            await interaction.followup.send(embed=embed, view=view)


class CommandSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot, user_id: int, entries: list[tuple[str, str, dict[str, str]]], timeout: float = 600):
        super().__init__(timeout=timeout)
        self.add_item(_CommandSelect(bot, user_id, entries))


class E3LoginModal(discord.ui.Modal, title="E3 Login"):
    account = discord.ui.TextInput(label="Account", placeholder="Enter your E3 account", max_length=128)
    password = discord.ui.TextInput(
        label="Password",
        placeholder="Enter your E3 password",
        style=discord.TextStyle.short,
        max_length=128,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True, ephemeral=True)
        command = f"login {self.account.value.strip()} {self.password.value.strip()}"
        await _execute_e3_payload(interaction, command, interaction.user.id, bot=self.bot, ephemeral=True)


def _bubble_actions(bubble: dict[str, Any]) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "button":
                action = node.get("action") or {}
                action_type = action.get("type")
                if action_type == "message":
                    actions.append({"kind": "message", "label": str(action.get("label") or "Open"), "value": str(action.get("text") or "")})
                elif action_type == "uri":
                    actions.append({"kind": "uri", "label": str(action.get("label") or "Open"), "value": str(action.get("uri") or "")})
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(bubble)
    return actions


def _extract_embeds_and_views(bot: commands.Bot, payload: Any, user_id: int) -> list[tuple[discord.Embed | None, list[dict[str, str]], str | None]]:
    if not isinstance(payload, dict):
        return [(None, [], _response_text(payload))]

    messages = payload.get("messages") or []
    items: list[tuple[discord.Embed | None, list[dict[str, str]], str | None]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("type") == "text":
            items.append((None, [], str(message.get("text") or "")))
            continue
        if message.get("type") != "flex":
            continue
        contents = message.get("contents") or {}
        bubbles = []
        if contents.get("type") == "bubble":
            bubbles = [contents]
        elif contents.get("type") == "carousel":
            bubbles = [item for item in (contents.get("contents") or []) if isinstance(item, dict)]
        for bubble in bubbles:
            embed = discord.Embed(
                title=_bubble_title(bubble),
                description=_bubble_description(bubble),
                color=_hex_to_color(((bubble.get("header") or {}).get("backgroundColor"))) or discord.Color.blurple(),
            )
            actions = _bubble_actions(bubble)
            items.append((embed, actions, None))

    if not items:
        items.append((None, [], _response_text(payload)))
    return items


async def _edit_message_from_payload(message: discord.Message, payload: Any, *, bot: commands.Bot, user_id: int) -> bool:
    items = _extract_embeds_and_views(bot, payload, user_id)
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
            action = _primary_action(item_actions)
            if not action:
                selector_entries = []
                break
            selector_entries.append((_select_option_label(embed, action), _embed_option_description(embed), action))

    repeated_label_cards = bool(selector_entries and _repeated_message_label(selector_entries))
    should_use_selector = (
        selector_entries
        and len(selector_entries) <= MAX_SELECT_OPTIONS
        and ((len(selector_candidates) > 2 and all(_primary_action(item_actions) for _, item_actions in selector_candidates)) or (repeated_label_cards and len(selector_candidates) > 1))
    )

    content = "\n\n".join(chunk for chunk in text_chunks if chunk) or None
    if should_use_selector:
        summary = discord.Embed(
            title=_select_summary_title(selector_entries),
            description='Use the selector below to open details without flooding the channel.',
            color=discord.Color.blurple(),
        )
        for idx, (label, desc, action) in enumerate(selector_entries[:MAX_SELECT_OPTIONS], start=1):
            value = "Open file" if _is_file_entry((label, desc, action)) else (desc[:1024] or "Open details")
            summary.add_field(name=f'{idx}. {label[:100]}', value=value, inline=True)
        await message.edit(content=content, embeds=[summary], view=CommandSelectView(bot, user_id, selector_entries))
        return True

    view = _build_preferred_view(bot, user_id, embeds[0], actions)
    kwargs = {"content": content, "embeds": embeds[:10]}
    if view is not None:
        kwargs["view"] = view
    await message.edit(**kwargs)
    return True


def _build_preferred_view(bot: commands.Bot, user_id: int, embed: discord.Embed | None, actions: list[dict[str, str]]) -> discord.ui.View | None:
    if _is_reminder_actions(actions):
        return ReminderToggleView(bot, user_id, _reminder_enabled_from_embed(embed))
    return CommandButtonView(bot, user_id, actions[:5]) if actions else None


async def _send_payload(target, payload: Any, *, bot: commands.Bot, user_id: int, ephemeral: bool = False) -> None:
    items = _extract_embeds_and_views(bot, payload, user_id)
    sent_any = False
    pending_embeds: list[discord.Embed] = []
    pending_actions: list[dict[str, str]] = []
    text_chunks_all: list[str] = []

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
        view = _build_preferred_view(bot, user_id, first_embed, pending_actions)
        await _send_with(target, embeds=pending_embeds, view=view)
        sent_any = True
        pending_embeds = []
        pending_actions = []

    async def send_select_chunk(chunk: list[tuple[discord.Embed, list[dict[str, str]]]]) -> None:
        nonlocal sent_any
        entries: list[tuple[str, str, dict[str, str]]] = []
        for embed, actions in chunk:
            action = _primary_action(actions)
            if not action:
                continue
            entries.append((_select_option_label(embed, action), _embed_option_description(embed), action))
        if not entries:
            return
        summary = discord.Embed(
            title=_select_summary_title(entries),
            description='Use the selector below to open details without flooding the channel.',
            color=discord.Color.blurple(),
        )
        preview_entries = entries[:25]
        for idx, (label, desc, action) in enumerate(preview_entries, start=1):
            value = "Open file" if _is_file_entry((label, desc, action)) else (desc[:1024] or "Open details")
            summary.add_field(name=f'{idx}. {label[:100]}', value=value, inline=True)
        await _send_with(target, embeds=[summary], view=CommandSelectView(bot, user_id, entries))
        sent_any = True

    selector_candidates: list[tuple[discord.Embed, list[dict[str, str]]]] = []
    for embed, actions, text in items:
        if text:
            cleaned = str(text).strip()
            if cleaned:
                text_chunks_all.append(cleaned)
            continue
        if embed is None:
            continue
        selector_candidates.append((embed, actions))

    repeated_label_cards = False
    if selector_candidates:
        selector_entries = []
        for embed, actions in selector_candidates:
            action = _primary_action(actions)
            if not action:
                selector_entries = []
                break
            selector_entries.append((_select_option_label(embed, action), _embed_option_description(embed), action))
        repeated_label_cards = bool(selector_entries and _repeated_message_label(selector_entries))

    if selector_candidates and ((len(selector_candidates) > 2 and all(_primary_action(actions) for _, actions in selector_candidates)) or (repeated_label_cards and len(selector_candidates) > 1)):
        for start in range(0, len(selector_candidates), MAX_SELECT_OPTIONS):
            await send_select_chunk(selector_candidates[start:start + MAX_SELECT_OPTIONS])
        return

    for embed, actions, text in items:
        if text:
            await flush_pending()
            await _send_text_chunks(target, text, ephemeral=ephemeral)
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


async def _execute_e3_payload(target, command_text: str, user_id: int, *, bot: commands.Bot | None = None, ephemeral: bool = False):
    bot = bot or target.client
    text = f"e3 {command_text.strip()}" if not command_text.strip().lower().startswith("e3") else command_text.strip()
    user_key = _platform_user_key(user_id)
    command = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
    async with E3_EXECUTION_SEMAPHORE:
        if command.split(maxsplit=1)[0].lower() in {"login", "relogin", "refresh", "update"} or command in {"重新登入", "更新", "刷新"}:
            payload = await asyncio.to_thread(run_e3_async_command, text, logger, user_key)
        else:
            payload = await asyncio.to_thread(handle_e3_command, text, logger, user_key)
    await _send_payload(target, payload, bot=bot, user_id=user_id, ephemeral=ephemeral)


async def _deliver_discord_dm(bot: commands.Bot, user_key: str, payload: Any) -> bool:
    key = str(user_key or "")
    if not key.startswith("discord:"):
        return False
    try:
        discord_user_id = int(key.split(":", 1)[1])
    except (IndexError, ValueError):
        return False

    user = bot.get_user(discord_user_id)
    if user is None:
        try:
            user = await bot.fetch_user(discord_user_id)
        except discord.DiscordException:
            return False
    try:
        await _send_payload(user, payload, bot=bot, user_id=discord_user_id)
        return True
    except discord.DiscordException:
        logger.exception("discord_reminder_delivery_failed user=%s", user_key)
    target_row = get_discord_delivery_target(user_key)
    channel_id = str(target_row["channel_id"] or "").strip() if target_row else ""
    if not channel_id:
        return False
    try:
        numeric_channel_id = int(channel_id)
    except ValueError:
        return False

    channel = bot.get_channel(numeric_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(numeric_channel_id)
        except discord.DiscordException:
            logger.exception("discord_reminder_channel_fetch_failed user=%s channel=%s", user_key, channel_id)
            return False
    try:
        await _send_payload(channel, _reminder_channel_payload(payload, discord_user_id), bot=bot, user_id=discord_user_id)
        return True
    except discord.DiscordException:
        logger.exception("discord_reminder_channel_delivery_failed user=%s channel=%s", user_key, channel_id)
        return False


def _start_discord_reminder_worker(bot: commands.Bot) -> None:
    def push_fn(user_key: str, payload: Any) -> bool:
        future = asyncio.run_coroutine_threadsafe(_deliver_discord_dm(bot, user_key, payload), bot.loop)
        try:
            return bool(future.result(timeout=60))
        except Exception:
            logger.exception("discord_reminder_future_failed user=%s", user_key)
            return False

    started = start_reminder_worker(
        push_fn,
        logger,
        target_predicate=lambda user_key: str(user_key or "").startswith("discord:"),
    )
    logger.info("discord_reminder_worker_started=%s", started)


def _build_reminder_test_payload(user_id: int) -> str:
    return build_test_reminder_payload(user_id)


def _is_owner_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        client = interaction.client
        if hasattr(client, "is_owner"):
            return await client.is_owner(interaction.user)
        return False

    return app_commands.check(predicate)


def _cached_course_choices(discord_user_id: int) -> list[tuple[str, str]]:
    user_key = _platform_user_key(discord_user_id)
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


async def _autocomplete_course_files(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    current_lower = str(current or "").strip().lower()
    choices = []
    for label, value in _cached_course_choices(interaction.user.id):
        haystack = f"{label} {value}".lower()
        if current_lower and current_lower not in haystack:
            continue
        choices.append(app_commands.Choice(name=label, value=value))
    return choices[:25]


def _build_help_text(prefix: str) -> str:
    return (
        "XE3 Discord Bot\n"
        f"{prefix}weather <city>\n"
        f"{prefix}e3 help\n"
        f"{prefix}e3 login <account> <password>  # prefix fallback\n"
        f"/e3 login  # opens secure modal\n"
        f"{prefix}e3 relogin\n"
        f"{prefix}e3 course\n"
        f"{prefix}e3 近期 作業\n"
        f"{prefix}e3 timeline\n"
        f"{prefix}e3 grades\n"
        f"{prefix}e3 files <keyword>\n"
        f"{prefix}e3 remind show|on|off|test\n"
        f"{prefix}chksys"
    )


def _create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix=discord_command_prefix(), intents=intents, help_command=None)

    @bot.event
    async def on_ready():
        logger.info("discord_bot_ready user=%s guilds=%s", bot.user, len(bot.guilds))
        guild_id = discord_guild_id()
        try:
            if guild_id:
                synced = await bot.tree.sync(guild=discord.Object(id=guild_id))
                logger.info("discord_app_commands_synced guild=%s count=%s", guild_id, len(synced))
            else:
                synced = await bot.tree.sync()
                logger.info("discord_app_commands_synced_global count=%s", len(synced))
        except Exception:
            logger.exception("discord_app_commands_sync_failed")
        _start_discord_reminder_worker(bot)

    @bot.command(name="homevault")
    async def homevault(ctx: commands.Context):
        await _send_text_chunks(ctx, _build_help_text(str(bot.command_prefix)))

    @bot.command(name="help")
    async def help_command(ctx: commands.Context):
        await _send_text_chunks(ctx, _build_help_text(str(bot.command_prefix)))

    @bot.command(name="weather")
    async def weather(ctx: commands.Context, *, city: str = ""):
        await _remember_context_target(ctx)
        city = city.strip()
        if not city:
            await _send_text_chunks(ctx, f"Usage: {bot.command_prefix}weather <city>")
            return
        async with ctx.typing():
            payload = await asyncio.to_thread(handle_city_weather, city, logger)
        await _send_payload(ctx, payload, bot=bot, user_id=ctx.author.id)

    @bot.command(name="chksys")
    async def chksys(ctx: commands.Context):
        await _remember_context_target(ctx)
        async with ctx.typing():
            payload = await asyncio.to_thread(build_system_report)
        await _send_text_chunks(ctx, payload)

    @bot.command(name="e3")
    async def e3(ctx: commands.Context, *, command: str = "help"):
        await _remember_context_target(ctx)
        async with ctx.typing():
            await _execute_e3_payload(ctx, command.strip() or "help", ctx.author.id, bot=bot)

    @bot.event
    async def on_command_error(ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            return
        logger.exception("discord_prefix_command_failed", exc_info=error)
        await _send_text_chunks(ctx, "⚠️ Something went wrong while running that command.")

    e3_group = app_commands.Group(name="e3", description="XE3 course assistant")

    @e3_group.command(name="help", description="Show E3 help")
    async def e3_help(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.send_message(_build_help_text(str(bot.command_prefix)))

    @e3_group.command(name="login", description="Open a login form for E3")
    async def e3_login(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.send_modal(E3LoginModal(bot))

    @e3_group.command(name="relogin", description="Refresh your E3 session")
    async def e3_relogin(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, "relogin", interaction.user.id, bot=bot, ephemeral=True)

    @e3_group.command(name="course", description="Show current courses")
    async def e3_course(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True, ephemeral=True)
        await _execute_e3_payload(interaction, "course", interaction.user.id, bot=bot, ephemeral=True)

    @e3_group.command(name="timeline", description="Show upcoming homework and exams")
    @app_commands.describe(kind="Optional filter: homework or exam")
    @app_commands.choices(kind=[
        app_commands.Choice(name="homework", value="homework"),
        app_commands.Choice(name="exam", value="exam"),
    ])
    async def e3_timeline(interaction: discord.Interaction, kind: app_commands.Choice[str] | None = None):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True, ephemeral=True)
        command = "timeline academic" if not kind else f"timeline {kind.value}"
        await _execute_e3_payload(interaction, command, interaction.user.id, bot=bot, ephemeral=True)

    @e3_group.command(name="grades", description="Show grades")
    async def e3_grades(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True, ephemeral=True)
        await _execute_e3_payload(interaction, "grades", interaction.user.id, bot=bot, ephemeral=True)

    @e3_group.command(name="files", description="Browse files from one of your courses")
    @app_commands.autocomplete(keyword=_autocomplete_course_files)
    async def e3_files(interaction: discord.Interaction, keyword: str):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, f"files {keyword}", interaction.user.id, bot=bot)

    @e3_group.command(name="remind", description="Reminder settings")
    @app_commands.describe(action="show, on, off, or test")
    async def e3_remind(interaction: discord.Interaction, action: str = "show"):
        await _remember_interaction_target(interaction)
        normalized = (action or "show").strip().lower()
        if normalized == "test":
            await interaction.response.defer(thinking=True)
            user_key = _platform_user_key(interaction.user.id)
            internal_user_id = await asyncio.to_thread(get_user_id, user_key)
            if not internal_user_id:
                await _send_text_chunks(interaction, "Please login to E3 first before testing reminders.")
                return
            payload = await asyncio.to_thread(_build_reminder_test_payload, internal_user_id)
            await _send_text_chunks(interaction, payload)
            return
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, f"remind {normalized}", interaction.user.id, bot=bot)

    bot.tree.add_command(e3_group, guild=discord.Object(id=discord_guild_id()) if discord_guild_id() else None)

    @bot.tree.command(name="weather", description="Get weather by city")
    async def slash_weather(interaction: discord.Interaction, city: str):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        payload = await asyncio.to_thread(handle_city_weather, city, logger)
        await _send_payload(interaction, payload, bot=bot, user_id=interaction.user.id)

    @bot.tree.command(name="chksys", description="Show system status")
    @_is_owner_check()
    async def slash_chksys(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True, ephemeral=True)
        payload = await asyncio.to_thread(build_system_report)
        await _send_text_chunks(interaction, payload, ephemeral=True)


    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.errors.CheckFailure):
            message = "⚠️ You do not have permission to run this command."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return
        logger.exception("discord_app_command_failed", exc_info=error)
        message = "⚠️ Something went wrong while running this command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    return bot


def run_discord_bot() -> None:
    token = discord_bot_token()
    if not token:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = _create_bot()
    bot.run(token)
