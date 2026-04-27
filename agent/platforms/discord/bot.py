import asyncio
from io import BytesIO
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from agent.core.config import discord_attachment_max_bytes, discord_bot_token, discord_command_prefix, discord_guild_id, discord_notify_user_id, public_base_url
from agent.features.e3.service import handle_e3_command, run_e3_async_command
from agent.features.e3.data.db import get_discord_delivery_target, get_user_id, init_db, upsert_discord_delivery_target
from agent.features.e3.reminder.api import build_test_reminder_payloads, refresh_all_saved_accounts, start_reminder_worker
from agent.features.e3.services.upload import E3UploadError, upload_assignment_submission
from agent.features.plot.service import (
    PlotPreviewError,
    build_plot_template_csv,
    default_plot_selection,
    is_supported_plot_file,
    parse_workbook_preview,
    selected_sheet,
)
from agent.features.weather.service import handle_city_weather
from agent.platforms.discord.command_helpers import autocomplete_course_files, autocomplete_course_homework, build_help_text
from agent.platforms.discord.file_delivery import download_discord_attachment
from agent.platforms.discord.message_utils import send_text_chunks as _send_text_chunks
from agent.platforms.discord.payload_sender import edit_message_from_payload, send_payload
from agent.platforms.discord.plot_views import PlotWorkbookConfigView
from agent.platforms.discord.views import DiscordViewCallbacks, E3LoginModal
from agent.core.system_status import build_system_report


logger = logging.getLogger(__name__)
E3_EXECUTION_SEMAPHORE = asyncio.Semaphore(2)
MAX_PLOT_UPLOAD_BYTES = 8 * 1024 * 1024


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

def _schedule_command_for_slots(slots: list[str]) -> str:
    normalized = list(slots)
    if normalized == ["09:00"]:
        return "e3 remind schedule morning"
    if normalized == ["21:00"]:
        return "e3 remind schedule evening"
    return "e3 remind schedule both"


def _format_refresh_all_summary(summary: dict[str, Any]) -> str:
    total = int(summary.get("total", 0) or 0)
    ok = int(summary.get("ok", 0) or 0)
    failed = int(summary.get("failed", 0) or 0)
    grade_changes = int(summary.get("grade_changes", 0) or 0)
    ok_users = [str(row.get("user_key")) for row in summary.get("results", []) if row.get("ok")]
    failed_users = [str(row.get("user_key")) for row in summary.get("results", []) if not row.get("ok")]
    lines = [
        "✅ `/e3 refresh` 已完成。",
        "",
        f"• 已掃描帳號：`{total}`",
        f"• 成功同步：`{ok}`",
        f"• 同步失敗：`{failed}`",
        f"• 成績異動筆數：`{grade_changes}`",
    ]
    if ok_users:
        lines.extend([
            "",
            "🟢 成功清單",
            "\n".join(f"• `{user}`" for user in ok_users),
        ])
    if failed_users:
        lines.extend([
            "",
            "🔴 失敗清單",
            "\n".join(f"• `{user}`" for user in failed_users),
        ])
    lines.extend(["", "這次是靜默刷新，不會另外把結果推給其他使用者。"])
    return "\n".join(lines)


def _plot_error_message(exc: Exception) -> str:
    if isinstance(exc, PlotPreviewError):
        return str(exc)
    return "XE3 目前讀不懂這份檔案，先試試 `.xlsx`、`.xlsm` 或 `.csv`。"


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


def _download_discord_attachment(user_id: int, action: dict[str, str], fallback_name: str) -> tuple[discord.File | None, str | None]:
    return download_discord_attachment(
        user_id,
        action,
        fallback_name,
        public_base_url_getter=public_base_url,
        attachment_max_bytes_getter=discord_attachment_max_bytes,
    )


async def _edit_message_from_payload(message: discord.Message, payload: Any, *, bot: commands.Bot, user_id: int) -> bool:
    return await edit_message_from_payload(
        message,
        payload,
        user_id=user_id,
        callbacks=_view_callbacks(bot),
        send_text_chunks=_send_text_chunks,
    )


async def _send_payload(target, payload: Any, *, bot: commands.Bot, user_id: int, ephemeral: bool = False) -> None:
    await send_payload(
        target,
        payload,
        user_id=user_id,
        callbacks=_view_callbacks(bot),
        send_text_chunks=_send_text_chunks,
        ephemeral=ephemeral,
    )


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


async def _autocomplete_course_homework(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await autocomplete_course_homework(
        interaction,
        current,
        user_key_builder=_platform_user_key,
        logger=logger,
    )


async def _is_e3_upload_user(interaction: discord.Interaction) -> bool:
    client = interaction.client
    if hasattr(client, "is_owner") and await client.is_owner(interaction.user):
        return True
    allowed_user_id = discord_notify_user_id()
    if allowed_user_id is not None:
        return interaction.user.id == allowed_user_id
    return False


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

    @e3_group.command(name="refresh", description="重新整理所有已儲存的 E3 帳號")
    @_is_owner_check()
    async def e3_refresh(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        summary = await asyncio.to_thread(refresh_all_saved_accounts, logger)
        await _send_text_chunks(interaction, _format_refresh_all_summary(summary), ephemeral=True)

    @e3_group.command(name="course", description="顯示目前課程")
    async def e3_course(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, "course", interaction.user.id, bot=bot)

    @e3_group.command(name="today", description="顯示今天的作業、考試與課程事件")
    async def e3_today(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, "today", interaction.user.id, bot=bot)

    @e3_group.command(name="week", description="顯示未來 7 天的作業、考試與課程事件")
    async def e3_week(interaction: discord.Interaction):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, "week", interaction.user.id, bot=bot)

    @e3_group.command(name="news", description="顯示近期公告與 forum 討論")
    @app_commands.describe(course="只看某一門課", recent_days="只看最近幾天")
    @app_commands.choices(recent_days=[
        app_commands.Choice(name="最近 3 天", value=3),
        app_commands.Choice(name="最近 7 天", value=7),
        app_commands.Choice(name="最近 14 天", value=14),
    ])
    @app_commands.autocomplete(course=_autocomplete_course_files)
    async def e3_news(
        interaction: discord.Interaction,
        course: str = "",
        recent_days: app_commands.Choice[int] | None = None,
    ):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        parts = ["news"]
        if recent_days is not None:
            parts.extend(["recent", str(recent_days.value)])
        if course.strip():
            parts.extend(["course", course.strip()])
        await _execute_e3_payload(interaction, " ".join(parts), interaction.user.id, bot=bot)

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

    @e3_group.command(name="passcalc", description="試算這門課還需要拿幾分")
    @app_commands.describe(course="要試算的課程", target="目標總成績（0-100）")
    @app_commands.autocomplete(course=_autocomplete_course_files)
    async def e3_passcalc(interaction: discord.Interaction, course: str, target: float):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, f"成績試算 {course} {target}", interaction.user.id, bot=bot)

    @e3_group.command(name="files", description="瀏覽某一門課的檔案")
    @app_commands.autocomplete(keyword=_autocomplete_course_files)
    async def e3_files(interaction: discord.Interaction, keyword: str):
        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True)
        await _execute_e3_payload(interaction, f"files {keyword}", interaction.user.id, bot=bot)

    @e3_group.command(name="upload", description="上傳檔案到指定 E3 作業")
    @app_commands.describe(
        course="作業所屬課程，請從選單挑課號",
        homework="要繳交的作業，請先選 course 再從選單挑作業",
        file="要上傳到 E3 的檔案",
        replace_existing="已有繳交檔案時，是否先刪除舊提交再上傳",
    )
    @app_commands.autocomplete(course=_autocomplete_course_files, homework=_autocomplete_course_homework)
    async def e3_upload(
        interaction: discord.Interaction,
        course: str,
        homework: str,
        file: discord.Attachment,
        replace_existing: bool = False,
    ):
        if not await _is_e3_upload_user(interaction):
            await interaction.response.send_message("⚠️ 這個 E3 上傳功能目前只開放給管理者測試。", ephemeral=True)
            return

        await _remember_interaction_target(interaction)
        await interaction.response.defer(thinking=True, ephemeral=True)

        filename = str(file.filename or "").strip()
        if not filename:
            await interaction.followup.send("⚠️ Discord 附件沒有檔名，已取消上傳。", ephemeral=True)
            return

        max_bytes = discord_attachment_max_bytes()
        if int(getattr(file, "size", 0) or 0) > max_bytes:
            await interaction.followup.send(
                f"⚠️ 這個檔案超過目前 Discord 上傳代理限制 `{max_bytes // (1024 * 1024)} MB`，已取消。",
                ephemeral=True,
            )
            return

        try:
            blob = await file.read()
            result = await asyncio.to_thread(
                upload_assignment_submission,
                _platform_user_key(interaction.user.id),
                course,
                homework,
                filename,
                blob,
                content_type=getattr(file, "content_type", None),
                replace_existing=replace_existing,
            )
        except E3UploadError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        except discord.DiscordException:
            logger.exception("discord_e3_upload_attachment_read_failed user=%s file=%s", interaction.user.id, filename)
            await interaction.followup.send("⚠️ Discord 附件讀取失敗，請重新上傳一次。", ephemeral=True)
            return
        except Exception:
            logger.exception("discord_e3_upload_failed user=%s course=%s homework=%s file=%s", interaction.user.id, course, homework, filename)
            await interaction.followup.send("⚠️ E3 上傳流程失敗，請先回 E3 網頁確認目前作業狀態。", ephemeral=True)
            return

        replaced_text = "（已先刪除舊提交）" if result.replaced_existing else ""
        await interaction.followup.send(
            "\n".join(
                [
                    "✅ E3 作業檔案已上傳並送出。",
                    f"課程：`{result.course_id}` {result.course_name}",
                    f"作業：{result.assignment_title}",
                    f"檔案：`{result.filename}` {replaced_text}".strip(),
                    f"目前頁面上可見已繳檔案：`{result.submitted_file_count}`",
                ]
            ),
            ephemeral=True,
        )

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

    plot_group = app_commands.Group(name="plot", description="實驗資料圖表工具")

    @plot_group.command(name="excel", description="上傳 Excel 或 CSV，先設定圖表欄位")
    @app_commands.describe(file="Excel 或 CSV 檔案")
    async def plot_excel(interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer(thinking=True, ephemeral=True)

        filename = str(file.filename or "").strip()
        if not is_supported_plot_file(filename):
            await interaction.followup.send(
                "⚠️ 目前先支援 `.xlsx`、`.xlsm`、`.csv`。你先用這三種格式測流程就好。",
                ephemeral=True,
            )
            return
        if int(getattr(file, "size", 0) or 0) > MAX_PLOT_UPLOAD_BYTES:
            await interaction.followup.send(
                "⚠️ 這一版先讓 XE3 讀 `8 MB` 以內的資料檔，避免實驗資料太大把互動拖慢。",
                ephemeral=True,
            )
            return

        try:
            blob = await file.read()
            preview = await asyncio.to_thread(parse_workbook_preview, filename, blob)
            state = default_plot_selection(preview)
        except Exception as exc:
            logger.exception("discord_plot_excel_parse_failed user=%s file=%s", interaction.user.id, filename)
            await interaction.followup.send(f"⚠️ {_plot_error_message(exc)}", ephemeral=True)
            return

        from agent.features.plot.views import build_plot_setup_embed

        embed = build_plot_setup_embed(preview, state)
        view = PlotWorkbookConfigView(user_id=interaction.user.id, preview=preview, state=state)
        sheet = selected_sheet(preview, state)
        if not sheet.has_header:
            template_file = discord.File(BytesIO(build_plot_template_csv()), filename="plot_template.csv")
            await interaction.followup.send(
                content="📎 我另外附了一份 `plot_template.csv`。如果你想整理成比較穩定的格式，可以照這份 template 重傳；如果現在只是先測流程，也可以直接繼續。",
                embed=embed,
                view=view,
                file=template_file,
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    bot.tree.add_command(plot_group, guild=discord.Object(id=discord_guild_id()) if discord_guild_id() else None)


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
