import asyncio
import logging
from typing import Any

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
            await interaction.response.send_message("This button belongs to another user's session.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, self.command_text, self.user_id)


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


def _extract_embeds_and_views(bot: commands.Bot, payload: Any, user_id: int) -> list[tuple[discord.Embed | None, discord.ui.View | None, str | None]]:
    if not isinstance(payload, dict):
        return [(None, None, _response_text(payload))]

    messages = payload.get("messages") or []
    items: list[tuple[discord.Embed | None, discord.ui.View | None, str | None]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("type") == "text":
            items.append((None, None, str(message.get("text") or "")))
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
            view = CommandButtonView(bot, user_id, actions) if actions else None
            items.append((embed, view, None))

    if not items:
        items.append((None, None, _response_text(payload)))
    return items


async def _send_payload(target, payload: Any, *, bot: commands.Bot, user_id: int, ephemeral: bool = False) -> None:
    items = _extract_embeds_and_views(bot, payload, user_id)
    sent_any = False
    for embed, view, text in items:
        if text:
            await _send_text_chunks(target, text, ephemeral=ephemeral)
            sent_any = True
            continue
        if isinstance(target, discord.Interaction):
            if not target.response.is_done() and not sent_any:
                await target.response.send_message(embed=embed, view=view, ephemeral=ephemeral)
            else:
                await target.followup.send(embed=embed, view=view, ephemeral=ephemeral)
        else:
            await target.send(embed=embed, view=view)
        sent_any = True


async def _execute_e3_payload(target, command_text: str, user_id: int, *, bot: commands.Bot | None = None, ephemeral: bool = False):
    bot = bot or target.client
    text = f"e3 {command_text.strip()}" if not command_text.strip().lower().startswith("e3") else command_text.strip()
    user_key = _platform_user_key(user_id)
    command = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
    if command.split(maxsplit=1)[0].lower() in {"login", "relogin", "refresh", "update"} or command in {"重新登入", "更新", "刷新"}:
        payload = await asyncio.to_thread(run_e3_async_command, text, logger, user_key)
    else:
        payload = await asyncio.to_thread(handle_e3_command, text, logger, user_key)
    await _send_payload(target, payload, bot=bot, user_id=user_id, ephemeral=ephemeral)


def _build_help_text(prefix: str) -> str:
    return (
        "XE3 Discord Bot\n"
        f"{prefix}weather <city>\n"
        f"{prefix}e3 help\n"
        f"{prefix}e3 login <account> <password>\n"
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
        await interaction.response.send_message(_build_help_text(str(bot.command_prefix)), ephemeral=True)

    @e3_group.command(name="run", description="Run an arbitrary E3 command")
    @app_commands.describe(command="Example: course, timeline, files 韓文")
    async def e3_run(interaction: discord.Interaction, command: str):
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, command, interaction.user.id, bot=bot)

    @e3_group.command(name="login", description="Login to E3")
    async def e3_login(interaction: discord.Interaction, account: str, password: str):
        await interaction.response.defer(thinking=True, ephemeral=True)
        await _execute_e3_payload(interaction, f"login {account} {password}", interaction.user.id, bot=bot, ephemeral=True)

    @e3_group.command(name="relogin", description="Refresh your E3 session")
    async def e3_relogin(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        await _execute_e3_payload(interaction, "relogin", interaction.user.id, bot=bot, ephemeral=True)

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
        await interaction.response.defer(thinking=True, ephemeral=True)
        payload = await asyncio.to_thread(build_system_report)
        await _send_text_chunks(interaction, payload, ephemeral=True)

    return bot


def run_discord_bot() -> None:
    token = discord_bot_token()
    if not token:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = _create_bot()
    bot.run(token)
