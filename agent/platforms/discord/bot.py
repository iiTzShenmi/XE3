import asyncio
import logging
from typing import Any

import discord
from discord.ext import commands

from agent.config import discord_bot_token, discord_command_prefix, discord_guild_id
from agent.features.e3 import handle_e3_command, run_e3_async_command
from agent.features.weather import handle_city_weather
from agent.system_status import build_system_report


logger = logging.getLogger(__name__)


def _discord_user_key(user_id: int) -> str:
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


async def _send_text(target, payload: Any) -> None:
    text = _response_text(payload)
    for chunk in _chunk_text(text):
        await target.send(chunk)


def _build_help_text(prefix: str) -> str:
    return (
        "HomeVault Discord Bot\n"
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
        if guild_id:
            try:
                synced = await bot.tree.sync(guild=discord.Object(id=guild_id))
                logger.info("discord_app_commands_synced guild=%s count=%s", guild_id, len(synced))
            except Exception:
                logger.exception("discord_app_commands_sync_failed guild=%s", guild_id)

    @bot.command(name="homevault")
    async def homevault(ctx: commands.Context):
        await _send_text(ctx, _build_help_text(bot.command_prefix))

    @bot.command(name="help")
    async def help_command(ctx: commands.Context):
        await _send_text(ctx, _build_help_text(bot.command_prefix))

    @bot.command(name="weather")
    async def weather(ctx: commands.Context, *, city: str = ""):
        city = city.strip()
        if not city:
            await _send_text(ctx, f"Usage: {bot.command_prefix}weather <city>")
            return
        async with ctx.typing():
            payload = await asyncio.to_thread(handle_city_weather, city, logger)
        await _send_text(ctx, payload)

    @bot.command(name="chksys")
    async def chksys(ctx: commands.Context):
        async with ctx.typing():
            payload = await asyncio.to_thread(build_system_report)
        await _send_text(ctx, payload)

    @bot.command(name="e3")
    async def e3(ctx: commands.Context, *, command: str = "help"):
        command = command.strip() or "help"
        text = f"e3 {command}"
        user_key = _discord_user_key(ctx.author.id)
        async with ctx.typing():
            if command.split(maxsplit=1)[0].lower() in {"login", "relogin", "refresh", "update"} or command in {"重新登入", "更新", "刷新"}:
                payload = await asyncio.to_thread(run_e3_async_command, text, logger, user_key)
            else:
                payload = await asyncio.to_thread(handle_e3_command, text, logger, user_key)
        await _send_text(ctx, payload)

    return bot


def run_discord_bot() -> None:
    token = discord_bot_token()
    if not token:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = _create_bot()
    bot.run(token)
