import asyncio
import io
import logging
import re
from typing import Any
from urllib.parse import urlparse

import requests

import discord
from discord import app_commands
from discord.ext import commands

from agent.config import discord_attachment_max_bytes, discord_bot_token, discord_command_prefix, discord_guild_id, public_base_url
from agent.features.e3 import handle_e3_command, run_e3_async_command
from agent.features.e3.db import get_discord_delivery_target, get_user_id, init_db, upsert_discord_delivery_target
from agent.features.e3.reminders import build_test_reminder_payloads, start_reminder_worker
from agent.features.e3.file_proxy import FileProxyError, prepare_proxy_download, prepare_user_download
from agent.features.weather import handle_city_weather
from agent.platforms.discord.command_helpers import autocomplete_course_files, build_help_text
from agent.platforms.discord.message_utils import (
    extract_embed_items,
    send_text_chunks as _send_text_chunks,
)
from agent.platforms.discord.rendering import (
    all_file_entries,
    build_file_selector_summary,
    build_grouped_selector_summary,
    build_timeline_selector_summary,
    display_index_emoji,
    embed_option_description,
    is_file_entry,
    repeated_message_label,
    select_option_label,
    select_summary_title,
)
from agent.platforms.discord.views import CommandButtonView, CommandSelectView, DiscordViewCallbacks, E3LoginModal, ReminderToggleView
from agent.system_status import build_system_report


logger = logging.getLogger(__name__)
MAX_SELECT_OPTIONS = 25
E3_EXECUTION_SEMAPHORE = asyncio.Semaphore(2)


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
    mention = f"<@{discord_user_id}> 這是給你的提醒："
    if isinstance(payload, str):
        return f"{mention}\n{payload}"
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if isinstance(messages, list):
            wrapped = dict(payload)
            wrapped["messages"] = [{"type": "text", "text": mention}] + list(messages)
            return wrapped
    return f"{mention}\n{payload}"


def _is_reminder_actions(actions: list[dict[str, str]]) -> bool:
    commands = {str(action.get("value") or "").strip().lower() for action in actions if action.get("kind") == "message"}
    return "e3 remind on" in commands and "e3 remind off" in commands


def _reminder_enabled_from_embed(embed: discord.Embed | None) -> bool:
    title = getattr(embed, "title", "") or ""
    description = getattr(embed, "description", "") or ""
    text = f"{title}\n{description}".lower()
    return any(token in text for token in ["已開啟", "狀態｜已開啟", "狀態：開啟", "✅ 已開啟"])


def _reminder_schedule_from_embed(embed: discord.Embed | None) -> list[str]:
    text = f"{getattr(embed, 'title', '')}\n{getattr(embed, 'description', '')}"
    slots = re.findall(r"\b(?:[01]\d|2[0-3]):[0-5]\d\b", text)
    normalized: list[str] = []
    for slot in slots:
        if slot not in normalized:
            normalized.append(slot)
    return normalized or ["09:00", "21:00"]

def _schedule_command_for_slots(slots: list[str]) -> str:
    normalized = list(slots)
    if normalized == ["09:00"]:
        return "e3 remind schedule morning"
    if normalized == ["21:00"]:
        return "e3 remind schedule evening"
    return "e3 remind schedule both"


def _normalize_command_text(command_text: str) -> str:
    normalized = command_text.strip()
    if not normalized.lower().startswith("e3"):
        normalized = f"e3 {normalized}"
    return normalized


async def _run_command_from_view(interaction: discord.Interaction, bot: commands.Bot, user_id: int, command_text: str) -> None:
    payload = await asyncio.to_thread(handle_e3_command, _normalize_command_text(command_text), logger, _platform_user_key(user_id))
    edited = False
    message = getattr(interaction, "message", None)
    if message is not None:
        try:
            edited = await _edit_message_from_payload(message, payload, bot=bot, user_id=user_id)
        except discord.NotFound:
            logger.info("discord_interaction_message_missing user=%s command=%s", user_id, command_text)
        except discord.DiscordException:
            logger.exception("discord_interaction_command_edit_failed user=%s command=%s", user_id, command_text)
    if not interaction.response.is_done():
        if edited:
            await interaction.response.defer()
        else:
            await _send_payload(interaction, payload, bot=bot, user_id=user_id)
    elif not edited:
        await _send_payload(interaction, payload, bot=bot, user_id=user_id)


async def _run_uri_action_from_view(interaction: discord.Interaction, user_id: int, action: dict[str, str], desc: str, selected_label: str) -> None:
    await interaction.response.defer(thinking=True)
    file_obj, error_text = await asyncio.to_thread(_download_discord_attachment, user_id, action, selected_label)
    if file_obj is not None:
        embed = discord.Embed(title=selected_label, description="已直接從 E3 傳送到 Discord。", color=discord.Color.blurple())
        await interaction.followup.send(embed=embed, file=file_obj)
        return
    embed = discord.Embed(
        title=selected_label,
        description=error_text or desc or "請用下方按鈕開啟選取的檔案。",
        color=discord.Color.blurple(),
    )
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label=(action.get("label") or "開啟")[:80], url=action.get("value") or "https://discord.com"))
    await interaction.followup.send(embed=embed, view=view)


async def _run_test_reminder_from_view(interaction: discord.Interaction, bot: commands.Bot, user_id: int) -> None:
    internal_user_id = get_user_id(_platform_user_key(user_id))
    if not internal_user_id:
        await interaction.response.send_message("請先登入 E3，再測試提醒功能。")
        return
    await interaction.response.defer()
    payloads = await asyncio.to_thread(build_test_reminder_payloads, internal_user_id)
    for idx, payload in enumerate(payloads):
        if idx == 0:
            await _send_payload(interaction, payload, bot=bot, user_id=user_id)
        else:
            await _send_payload(interaction.followup, payload, bot=bot, user_id=user_id)


async def _run_login_modal_submit(interaction: discord.Interaction, bot: commands.Bot, account: str, password: str) -> None:
    await _remember_interaction_target(interaction)
    await interaction.response.defer(thinking=True)
    await _execute_e3_payload(interaction, f"login {account} {password}", interaction.user.id, bot=bot)


def _view_callbacks(bot: commands.Bot) -> DiscordViewCallbacks:
    return DiscordViewCallbacks(
        run_command=lambda interaction, user_id, command_text: _run_command_from_view(interaction, bot, user_id, command_text),
        run_uri_action=lambda interaction, user_id, action, desc, selected_label: _run_uri_action_from_view(interaction, user_id, action, desc, selected_label),
        run_test_reminder=lambda interaction, user_id: _run_test_reminder_from_view(interaction, bot, user_id),
        run_login_modal=lambda interaction, account, password: _run_login_modal_submit(interaction, bot, account, password),
        schedule_command_for_slots=_schedule_command_for_slots,
    )


def _primary_action(actions: list[dict[str, str]]) -> dict[str, str] | None:
    for preferred_kind in ("message", "uri"):
        for action in actions:
            if action.get("kind") == preferred_kind and action.get("value"):
                return {"kind": str(action.get("kind") or ""), "label": str(action.get("label") or "開啟"), "value": str(action.get("value") or "")}
    return None




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
        return None, '目前無法從 E3 下載這個檔案。'

    response = payload['response']
    filename = payload.get('filename') or fallback_name or 'download'
    try:
        data = response.content
    finally:
        response.close()

    if len(data) > discord_attachment_max_bytes():
        return None, '這個檔案太大，無法直接上傳到 Discord。'

    return discord.File(io.BytesIO(data), filename=filename), None




def _extract_embeds_and_views(bot: commands.Bot, payload: Any, user_id: int) -> list[tuple[discord.Embed | None, list[dict[str, str]], str | None]]:
    del bot, user_id
    items: list[tuple[discord.Embed | None, list[dict[str, str]], str | None]] = []
    for item in extract_embed_items(payload):
        items.append((item.get("embed"), list(item.get("actions") or []), item.get("text")))
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
            selector_entries.append((select_option_label(embed, action), embed_option_description(embed, action), action))

    repeated_label_cards = bool(selector_entries and repeated_message_label(selector_entries))
    file_selector_cards = bool(selector_entries and all_file_entries(selector_entries) and len(selector_entries) > 1)
    should_use_selector = (
        selector_entries
        and len(selector_entries) <= MAX_SELECT_OPTIONS
        and (
            file_selector_cards
            or (len(selector_candidates) > 2 and all(_primary_action(item_actions) for _, item_actions in selector_candidates))
            or (repeated_label_cards and len(selector_candidates) > 1)
        )
    )

    content = "\n\n".join(chunk for chunk in text_chunks if chunk) or None
    if should_use_selector:
        summary = build_file_selector_summary(selector_candidates[:MAX_SELECT_OPTIONS], selector_entries[:MAX_SELECT_OPTIONS])
        if summary is None:
            summary = build_timeline_selector_summary(selector_candidates[:MAX_SELECT_OPTIONS], selector_entries[:MAX_SELECT_OPTIONS])
        if summary is None:
            summary = build_grouped_selector_summary(selector_entries[:MAX_SELECT_OPTIONS])
        if summary is None:
            summary = discord.Embed(
                title=select_summary_title(selector_entries),
                description='請從下方下拉選單挑一個，我會直接幫你打開，不洗版。',
                color=discord.Color.blurple(),
            )
            for idx, (label, desc, action) in enumerate(selector_entries[:MAX_SELECT_OPTIONS], start=1):
                value = (desc[:1024] or "點選後開啟檔案") if is_file_entry((label, desc, action)) else (desc[:1024] or "點選後查看詳情")
                summary.add_field(name=f'{display_index_emoji(idx)} {label[:100]}', value=value, inline=False)
        await message.edit(content=content, embeds=[summary], view=CommandSelectView(_view_callbacks(bot), user_id, selector_entries))
        return True

    view = _build_preferred_view(bot, user_id, embeds[0], actions)
    kwargs = {"content": content, "embeds": embeds[:10]}
    if view is not None:
        kwargs["view"] = view
    await message.edit(**kwargs)
    return True


def _build_preferred_view(bot: commands.Bot, user_id: int, embed: discord.Embed | None, actions: list[dict[str, str]]) -> discord.ui.View | None:
    if _is_reminder_actions(actions):
        return ReminderToggleView(_view_callbacks(bot), user_id, _reminder_enabled_from_embed(embed), _reminder_schedule_from_embed(embed))
    return CommandButtonView(_view_callbacks(bot), user_id, actions[:5]) if actions else None


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
            entries.append((select_option_label(embed, action), embed_option_description(embed, action), action))
        if not entries:
            return
        summary = build_file_selector_summary(chunk[:MAX_SELECT_OPTIONS], entries[:MAX_SELECT_OPTIONS])
        if summary is None:
            summary = build_timeline_selector_summary(chunk[:MAX_SELECT_OPTIONS], entries[:MAX_SELECT_OPTIONS])
        if summary is None:
            summary = build_grouped_selector_summary(entries[:MAX_SELECT_OPTIONS])
        if summary is None:
            summary = discord.Embed(
                title=select_summary_title(entries),
                description='請從下方下拉選單挑一個，我會直接幫你打開，不洗版。',
                color=discord.Color.blurple(),
            )
            preview_entries = entries[:25]
            for idx, (label, desc, action) in enumerate(preview_entries, start=1):
                value = (desc[:1024] or "點選後開啟檔案") if is_file_entry((label, desc, action)) else (desc[:1024] or "點選後查看詳情")
                summary.add_field(name=f'{display_index_emoji(idx)} {label[:100]}', value=value, inline=False)
        await _send_with(target, embeds=[summary], view=CommandSelectView(_view_callbacks(bot), user_id, entries))
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
    file_selector_cards = False
    if selector_candidates:
        selector_entries = []
        for embed, actions in selector_candidates:
            action = _primary_action(actions)
            if not action:
                selector_entries = []
                break
            selector_entries.append((select_option_label(embed, action), embed_option_description(embed, action), action))
        repeated_label_cards = bool(selector_entries and repeated_message_label(selector_entries))
        file_selector_cards = bool(selector_entries and all_file_entries(selector_entries) and len(selector_entries) > 1)

    if selector_candidates and (
        file_selector_cards
        or (len(selector_candidates) > 2 and all(_primary_action(actions) for _, actions in selector_candidates))
        or (repeated_label_cards and len(selector_candidates) > 1)
    ):
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


def _is_owner_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        client = interaction.client
        if hasattr(client, "is_owner"):
            return await client.is_owner(interaction.user)
        return False

    return app_commands.check(predicate)

async def _autocomplete_course_files(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await autocomplete_course_files(
        interaction,
        current,
        user_key_builder=_platform_user_key,
        logger=logger,
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
        await _send_text_chunks(ctx, build_help_text(str(bot.command_prefix)))

    @bot.command(name="help")
    async def help_command(ctx: commands.Context):
        await _send_text_chunks(ctx, build_help_text(str(bot.command_prefix)))

    @bot.command(name="weather")
    async def weather(ctx: commands.Context, *, city: str = ""):
        await _remember_context_target(ctx)
        city = city.strip()
        if not city:
            await _send_text_chunks(ctx, f"用法：{bot.command_prefix}weather <城市>")
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
        await _send_text_chunks(ctx, "⚠️ 執行指令時發生問題，請稍後再試。")

    e3_group = app_commands.Group(name="e3", description="XE3 課程助理")

    @e3_group.command(name="help", description="顯示 E3 說明")
    async def e3_help(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.send_message(build_help_text(str(bot.command_prefix)))

    @e3_group.command(name="login", description="開啟 E3 登入視窗")
    async def e3_login(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.send_modal(E3LoginModal(_view_callbacks(bot)))

    @e3_group.command(name="relogin", description="重新整理你的 E3 工作階段")
    async def e3_relogin(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, "relogin", interaction.user.id, bot=bot)

    @e3_group.command(name="course", description="顯示目前課程")
    async def e3_course(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, "course", interaction.user.id, bot=bot)

    @e3_group.command(name="timeline", description="顯示近期作業與考試")
    @app_commands.describe(kind="可選：只看作業或只看考試")
    @app_commands.choices(kind=[
        app_commands.Choice(name="作業", value="homework"),
        app_commands.Choice(name="考試", value="exam"),
    ])
    async def e3_timeline(interaction: discord.Interaction, kind: app_commands.Choice[str] | None = None):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        command = "timeline academic" if not kind else f"timeline {kind.value}"
        await _execute_e3_payload(interaction, command, interaction.user.id, bot=bot)

    @e3_group.command(name="grades", description="顯示成績")
    async def e3_grades(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, "grades", interaction.user.id, bot=bot)

    @e3_group.command(name="files", description="瀏覽某一門課的檔案")
    @app_commands.autocomplete(keyword=_autocomplete_course_files)
    async def e3_files(interaction: discord.Interaction, keyword: str):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, f"files {keyword}", interaction.user.id, bot=bot)

    @e3_group.command(name="remind", description="提醒設定")
    @app_commands.describe(action="show、on、off 或 test")
    async def e3_remind(interaction: discord.Interaction, action: str = "show"):
        await _remember_interaction_target(interaction)
        normalized = (action or "show").strip().lower()
        if normalized == "test":
            await interaction.response.defer(thinking=True)
            user_key = _platform_user_key(interaction.user.id)
            internal_user_id = await asyncio.to_thread(get_user_id, user_key)
            if not internal_user_id:
                await _send_text_chunks(interaction, "請先登入 E3，再測試提醒功能。")
                return
            payloads = await asyncio.to_thread(build_test_reminder_payloads, internal_user_id)
            for idx, payload in enumerate(payloads):
                if idx == 0:
                    await _send_text_chunks(interaction, payload)
                else:
                    await _send_text_chunks(interaction.followup, payload)
            return
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, f"remind {normalized}", interaction.user.id, bot=bot)

    bot.tree.add_command(e3_group, guild=discord.Object(id=discord_guild_id()) if discord_guild_id() else None)

    @bot.tree.command(name="weather", description="查詢城市天氣")
    async def slash_weather(interaction: discord.Interaction, city: str):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        payload = await asyncio.to_thread(handle_city_weather, city, logger)
        await _send_payload(interaction, payload, bot=bot, user_id=interaction.user.id)

    @bot.tree.command(name="chksys", description="查看系統狀態")
    @_is_owner_check()
    async def slash_chksys(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True, ephemeral=True)
        payload = await asyncio.to_thread(build_system_report)
        await _send_text_chunks(interaction, payload, ephemeral=True)


    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.errors.CheckFailure):
            message = "⚠️ 你沒有權限執行這個指令。"
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return
        logger.exception("discord_app_command_failed", exc_info=error)
        message = "⚠️ 執行這個指令時發生問題，請稍後再試。"
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
