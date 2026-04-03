from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import discord

MAX_SELECT_OPTIONS = 25

ScheduleCommandFn = Callable[[list[str]], str]
RunCommandFn = Callable[[discord.Interaction, int, str], Awaitable[None]]
RunUriActionFn = Callable[[discord.Interaction, int, dict[str, str], str, str], Awaitable[None]]
RunTestReminderFn = Callable[[discord.Interaction, int], Awaitable[None]]
RunLoginModalFn = Callable[[discord.Interaction, str, str], Awaitable[None]]


@dataclass(frozen=True)
class DiscordViewCallbacks:
    run_command: RunCommandFn
    run_uri_action: RunUriActionFn
    run_test_reminder: RunTestReminderFn
    run_login_modal: RunLoginModalFn
    schedule_command_for_slots: ScheduleCommandFn


class ReminderToggleButton(discord.ui.Button):
    def __init__(self, callbacks: DiscordViewCallbacks, user_id: int, enabled: bool):
        self.callbacks = callbacks
        self.user_id = user_id
        command_text = "e3 remind off" if enabled else "e3 remind on"
        label = "關閉提醒" if enabled else "開啟提醒"
        style = discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success
        super().__init__(label=label, style=style)
        self.command_text = command_text

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("這個提醒開關不是你的操作介面。")
            return
        await self.callbacks.run_command(interaction, self.user_id, self.command_text)


class ReminderTestButton(discord.ui.Button):
    def __init__(self, callbacks: DiscordViewCallbacks, user_id: int):
        self.callbacks = callbacks
        self.user_id = user_id
        super().__init__(label="測試提醒", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("這個測試按鈕不是你的操作介面。")
            return
        await self.callbacks.run_test_reminder(interaction, self.user_id)


class ReminderScheduleSelect(discord.ui.Select):
    _SCHEDULE_PRESETS: list[tuple[str, str, list[str]]] = [
        ("09:00 + 21:00", "早晚各提醒一次", ["09:00", "21:00"]),
        ("僅 09:00", "只接收早安摘要", ["09:00"]),
        ("僅 21:00", "只接收晚間整理", ["21:00"]),
    ]

    def __init__(self, callbacks: DiscordViewCallbacks, user_id: int, schedule: list[str]):
        self.callbacks = callbacks
        self.user_id = user_id
        normalized = list(schedule or [])
        options = []
        for label, description, slots in self._SCHEDULE_PRESETS:
            options.append(
                discord.SelectOption(
                    label=label,
                    description=description,
                    value="|".join(slots),
                    default=normalized == slots,
                )
            )
        super().__init__(placeholder="調整提醒時段", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("這個提醒設定不是你的操作介面。")
            return
        slots = [slot for slot in str(self.values[0]).split("|") if slot]
        await self.callbacks.run_command(
            interaction,
            self.user_id,
            self.callbacks.schedule_command_for_slots(slots),
        )


class ReminderToggleView(discord.ui.View):
    def __init__(self, callbacks: DiscordViewCallbacks, user_id: int, enabled: bool, schedule: list[str] | None = None, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.add_item(ReminderToggleButton(callbacks, user_id, enabled))
        self.add_item(ReminderTestButton(callbacks, user_id))
        self.add_item(ReminderScheduleSelect(callbacks, user_id, schedule or ["09:00", "21:00"]))


class _MessageCommandButton(discord.ui.Button):
    def __init__(self, callbacks: DiscordViewCallbacks, user_id: int, label: str, command_text: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.callbacks = callbacks
        self.user_id = user_id
        self.command_text = command_text

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("這個按鈕不是你的操作介面。")
            return
        await self.callbacks.run_command(interaction, self.user_id, self.command_text)


class CommandButtonView(discord.ui.View):
    def __init__(self, callbacks: DiscordViewCallbacks, user_id: int, actions: list[dict[str, str]], timeout: float = 600):
        super().__init__(timeout=timeout)
        for action in actions[:5]:
            kind = action.get("kind")
            label = action.get("label") or "開啟"
            if kind == "uri":
                self.add_item(discord.ui.Button(label=label[:80], url=action.get("value") or "https://discord.com"))
            elif kind == "message":
                self.add_item(_MessageCommandButton(callbacks, user_id, label[:80], action.get("value") or ""))


class _CommandSelect(discord.ui.Select):
    def __init__(self, callbacks: DiscordViewCallbacks, user_id: int, entries: list[tuple[str, str, dict[str, str]]]):
        self.entries = entries[:MAX_SELECT_OPTIONS]
        self.callbacks = callbacks
        self.user_id = user_id
        options = [
            discord.SelectOption(label=label[:100], description=(desc[:100] if desc else None), value=str(idx))
            for idx, (label, desc, _) in enumerate(self.entries)
        ]
        super().__init__(placeholder="選擇一個項目", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("這個下拉選單不是你的操作介面。")
            return
        selected_label, desc, action = self.entries[int(self.values[0])]
        if action.get("kind") == "message":
            await self.callbacks.run_command(interaction, self.user_id, action.get("value") or "")
            return
        await self.callbacks.run_uri_action(
            interaction,
            self.user_id,
            action,
            desc,
            selected_label or action.get("label") or "開啟檔案",
        )


class CommandSelectView(discord.ui.View):
    def __init__(self, callbacks: DiscordViewCallbacks, user_id: int, entries: list[tuple[str, str, dict[str, str]]], timeout: float = 600):
        super().__init__(timeout=timeout)
        self.add_item(_CommandSelect(callbacks, user_id, entries))


class E3LoginModal(discord.ui.Modal, title="E3 登入"):
    account = discord.ui.TextInput(label="帳號", placeholder="請輸入 E3 帳號", max_length=128)
    password = discord.ui.TextInput(
        label="密碼",
        placeholder="請輸入 E3 密碼",
        style=discord.TextStyle.short,
        max_length=128,
    )

    def __init__(self, callbacks: DiscordViewCallbacks):
        super().__init__()
        self.callbacks = callbacks

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.callbacks.run_login_modal(interaction, self.account.value.strip(), self.password.value.strip())
