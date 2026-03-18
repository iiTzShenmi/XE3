import asyncio
import logging
from typing import Any

MAX_SELECT_OPTIONS = 25

import discord
from discord import app_commands
from discord.ext import commands

from agent.config import discord_bot_token, discord_command_prefix, discord_guild_id
from agent.features.e3 import handle_e3_command, run_e3_async_command
from agent.features.weather import handle_city_weather
from agent.system_status import build_system_report


logger = logging.getLogger(__name__)


def _platform_user_key(user_id: int) -> str:
    return f"discord:{user_id}"


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


async def _send_text_chunks(target, text: str) -> None:
    chunks = _chunk_text(text)
    for idx, chunk in enumerate(chunks):
        if isinstance(target, discord.Interaction):
            if not target.response.is_done() and idx == 0:
                await target.response.send_message(chunk)
            else:
                await target.followup.send(chunk)
        else:
            await target.send(chunk)


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
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, self.command_text, self.user_id)


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


def _select_summary_title(entries: list[tuple[str, str, dict[str, str]]]) -> str:
    if entries and all(_is_file_entry(entry) for entry in entries):
        return "Choose a file"
    return "Choose an item"


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
        await interaction.response.defer(thinking=True)
        _, desc, action = self.entries[int(self.values[0])]
        if action.get("kind") == "message":
            await _execute_e3_payload(interaction, action.get("value") or "", self.user_id, bot=self.bot)
            return
        if action.get("kind") == "uri":
            embed = discord.Embed(title=(self.entries[int(self.values[0])][0] or action.get("label") or "Open file"), description=desc or "Open the selected file.", color=discord.Color.blurple())
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
        await interaction.response.defer(thinking=True)
        command = f"login {self.account.value.strip()} {self.password.value.strip()}"
        await _execute_e3_payload(interaction, command, interaction.user.id, bot=self.bot)


def _bubble_actions(bubble: dict[str, Any]) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    footer = bubble.get("footer") or {}
    for item in footer.get("contents") or []:
        if not isinstance(item, dict) or item.get("type") != "button":
            continue
        action = item.get("action") or {}
        action_type = action.get("type")
        if action_type == "message":
            actions.append({"kind": "message", "label": str(action.get("label") or "Open"), "value": str(action.get("text") or "")})
        elif action_type == "uri":
            actions.append({"kind": "uri", "label": str(action.get("label") or "Open"), "value": str(action.get("uri") or "")})
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


async def _send_payload(target, payload: Any, *, bot: commands.Bot, user_id: int) -> None:
    items = _extract_embeds_and_views(bot, payload, user_id)
    sent_any = False
    pending_embeds: list[discord.Embed] = []
    pending_actions: list[dict[str, str]] = []

    def _send_with(target_obj, *, embeds=None, view=None, content=None):
        if isinstance(target_obj, discord.Interaction):
            if not target_obj.response.is_done() and not sent_any:
                return target_obj.response.send_message(content=content, embeds=embeds, view=view)
            return target_obj.followup.send(content=content, embeds=embeds, view=view)
        return target_obj.send(content=content, embeds=embeds, view=view)

    async def flush_pending() -> None:
        nonlocal sent_any, pending_embeds, pending_actions
        if not pending_embeds:
            return
        view = CommandButtonView(bot, user_id, pending_actions[:5]) if pending_actions else None
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
    only_cards = True
    for embed, actions, text in items:
        if text:
            only_cards = False
            break
        if embed is None:
            continue
        selector_candidates.append((embed, actions))

    if only_cards and len(selector_candidates) > 2 and all(_primary_action(actions) for _, actions in selector_candidates):
        for start in range(0, len(selector_candidates), MAX_SELECT_OPTIONS):
            await send_select_chunk(selector_candidates[start:start + MAX_SELECT_OPTIONS])
        return

    for embed, actions, text in items:
        if text:
            await flush_pending()
            await _send_text_chunks(target, text)
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


async def _execute_e3_payload(target, command_text: str, user_id: int, *, bot: commands.Bot | None = None):
    bot = bot or target.client
    text = f"e3 {command_text.strip()}" if not command_text.strip().lower().startswith("e3") else command_text.strip()
    user_key = _platform_user_key(user_id)
    command = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
    if command.split(maxsplit=1)[0].lower() in {"login", "relogin", "refresh", "update"} or command in {"重新登入", "更新", "刷新"}:
        payload = await asyncio.to_thread(run_e3_async_command, text, logger, user_key)
    else:
        payload = await asyncio.to_thread(handle_e3_command, text, logger, user_key)
    await _send_payload(target, payload, bot=bot, user_id=user_id)


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

    @bot.command(name="homevault")
    async def homevault(ctx: commands.Context):
        await _send_text_chunks(ctx, _build_help_text(str(bot.command_prefix)))

    @bot.command(name="help")
    async def help_command(ctx: commands.Context):
        await _send_text_chunks(ctx, _build_help_text(str(bot.command_prefix)))

    @bot.command(name="weather")
    async def weather(ctx: commands.Context, *, city: str = ""):
        city = city.strip()
        if not city:
            await _send_text_chunks(ctx, f"Usage: {bot.command_prefix}weather <city>")
            return
        async with ctx.typing():
            payload = await asyncio.to_thread(handle_city_weather, city, logger)
        await _send_payload(ctx, payload, bot=bot, user_id=ctx.author.id)

    @bot.command(name="chksys")
    async def chksys(ctx: commands.Context):
        async with ctx.typing():
            payload = await asyncio.to_thread(build_system_report)
        await _send_text_chunks(ctx, payload)

    @bot.command(name="e3")
    async def e3(ctx: commands.Context, *, command: str = "help"):
        async with ctx.typing():
            await _execute_e3_payload(ctx, command.strip() or "help", ctx.author.id, bot=bot)

    e3_group = app_commands.Group(name="e3", description="XE3 course assistant")

    @e3_group.command(name="help", description="Show E3 help")
    async def e3_help(interaction: discord.Interaction):
        await interaction.response.send_message(_build_help_text(str(bot.command_prefix)))

    @e3_group.command(name="run", description="Run an arbitrary E3 command")
    @app_commands.describe(command="Example: course, timeline, files 韓文")
    async def e3_run(interaction: discord.Interaction, command: str):
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, command, interaction.user.id, bot=bot)

    @e3_group.command(name="login", description="Open a login form for E3")
    async def e3_login(interaction: discord.Interaction):
        await interaction.response.send_modal(E3LoginModal(bot))

    @e3_group.command(name="relogin", description="Refresh your E3 session")
    async def e3_relogin(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, "relogin", interaction.user.id, bot=bot)

    @e3_group.command(name="course", description="Show current courses")
    async def e3_course(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, "course", interaction.user.id, bot=bot)

    @e3_group.command(name="timeline", description="Show E3 timeline")
    async def e3_timeline(interaction: discord.Interaction, kind: str | None = None):
        await interaction.response.defer(thinking=True)
        command = "timeline" if not kind else f"timeline {kind}"
        await _execute_e3_payload(interaction, command, interaction.user.id, bot=bot)

    @e3_group.command(name="grades", description="Show grades")
    async def e3_grades(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, "grades", interaction.user.id, bot=bot)

    @e3_group.command(name="files", description="Search course files")
    async def e3_files(interaction: discord.Interaction, keyword: str):
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, f"files {keyword}", interaction.user.id, bot=bot)

    bot.tree.add_command(e3_group, guild=discord.Object(id=discord_guild_id()) if discord_guild_id() else None)

    @bot.tree.command(name="weather", description="Get weather by city")
    async def slash_weather(interaction: discord.Interaction, city: str):
        await interaction.response.defer(thinking=True)
        payload = await asyncio.to_thread(handle_city_weather, city, logger)
        await _send_payload(interaction, payload, bot=bot, user_id=interaction.user.id)

    @bot.tree.command(name="chksys", description="Show system status")
    async def slash_chksys(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        payload = await asyncio.to_thread(build_system_report)
        await _send_text_chunks(interaction, payload)

    return bot


def run_discord_bot() -> None:
    token = discord_bot_token()
    if not token:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = _create_bot()
    bot.run(token)
