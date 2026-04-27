from __future__ import annotations

import asyncio
from dataclasses import replace
from io import BytesIO
import logging

import discord

from agent.features.plot.service import (
    CHART_TYPE_LABELS,
    MAX_PLOT_SELECT_OPTIONS,
    MAX_PLOT_Y_SERIES,
    PlotRenderError,
    PlotSelectionState,
    PlotWorkbookPreview,
    apply_axis_label_mapping_hint,
    clamp_plot_selection,
    chart_type_label,
    render_plot_png,
    resolved_plot_title,
    resolved_x_axis_label,
    resolved_y_axis_label,
    selection_summary_text,
    selected_sheet,
)
from agent.features.plot.views import build_plot_setup_embed

logger = logging.getLogger(__name__)


def _column_option_description(column) -> str:
    parts: list[str] = []
    inferred = str(getattr(column, "inferred_type", "") or "").strip()
    if inferred:
        parts.append(inferred)
    sample_values = [str(value).strip() for value in getattr(column, "sample_values", ())[:3] if str(value).strip()]
    if sample_values:
        parts.append("例：" + " / ".join(sample_values))
    text = "｜".join(parts).strip()
    return text[:100] if text else "查看這欄前幾筆資料"


def _column_option_label(sheet, idx: int, column, *, role: str) -> str:
    raw_name = str(getattr(column, "name", "") or "").strip()
    if getattr(sheet, "has_header", True):
        return raw_name[:100]

    if role == "x":
        default_tag = "預設X軸"
    elif idx == 1:
        default_tag = "預設Y軸"
    else:
        default_tag = f"預設Y軸{idx}"

    fallback_name = raw_name or f"第 {idx + 1} 欄"
    if raw_name.startswith("預設"):
        fallback_name = f"第 {idx + 1} 欄"
    return f"{fallback_name}（{default_tag}）"[:100]


def _sheet_option_description(sheet) -> str:
    parts = [f"{sheet.row_count} 列", f"{len(sheet.columns)} 欄"]
    preview_cols = [column.name for column in sheet.columns[:2] if str(column.name).strip()]
    if preview_cols:
        parts.append("欄位：" + " / ".join(preview_cols))
    return "｜".join(parts)[:100]


class PlotWorkbookConfigView(discord.ui.View):
    def __init__(
        self,
        *,
        user_id: int,
        preview: PlotWorkbookPreview,
        state: PlotSelectionState,
        timeout: float = 900,
    ) -> None:
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.preview = preview
        self.state = clamp_plot_selection(preview, state)
        self._build_items()

    def _build_items(self) -> None:
        sheet = selected_sheet(self.preview, self.state)
        if len(self.preview.sheets) > 1:
            self.add_item(_PlotSheetSelect(self.user_id, self.preview, self.state))
        if sheet.columns:
            self.add_item(_PlotXSelect(self.user_id, self.preview, self.state))
        y_option_count = sum(1 for idx, _ in enumerate(sheet.columns[:MAX_PLOT_SELECT_OPTIONS]) if idx != self.state.x_index)
        if y_option_count:
            self.add_item(_PlotYSelect(self.user_id, self.preview, self.state))
        self.add_item(_PlotChartTypeSelect(self.user_id, self.preview, self.state))
        self.add_item(_PlotLabelButton(self.user_id, self.preview, self.state))
        self.add_item(_PlotDoneButton(self.user_id, self.preview, self.state))
        self.add_item(_PlotGenerateButton(self.user_id, self.preview, self.state))


class _BaseOwnedComponent:
    user_id: int

    async def _guard_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("這張圖表設定卡不是你的操作介面。", ephemeral=True)
            return False
        return True


class _PlotSheetSelect(discord.ui.Select, _BaseOwnedComponent):
    def __init__(self, user_id: int, preview: PlotWorkbookPreview, state: PlotSelectionState):
        self.user_id = user_id
        self.preview = preview
        self.state = state
        options = [
            discord.SelectOption(
                label=sheet.name[:100],
                description=_sheet_option_description(sheet),
                value=str(idx),
                default=idx == state.sheet_index,
            )
            for idx, sheet in enumerate(preview.sheets[:MAX_PLOT_SELECT_OPTIONS])
        ]
        super().__init__(placeholder="選擇工作表", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self._guard_owner(interaction):
            return
        new_state = replace(self.state, sheet_index=int(self.values[0]))
        clamp_plot_selection(self.preview, new_state)
        await interaction.response.edit_message(
            embed=build_plot_setup_embed(self.preview, new_state),
            view=PlotWorkbookConfigView(user_id=self.user_id, preview=self.preview, state=new_state),
        )


class _PlotXSelect(discord.ui.Select, _BaseOwnedComponent):
    def __init__(self, user_id: int, preview: PlotWorkbookPreview, state: PlotSelectionState):
        self.user_id = user_id
        self.preview = preview
        self.state = state
        sheet = selected_sheet(preview, state)
        options = [
            discord.SelectOption(
                label=_column_option_label(sheet, idx, column, role="x"),
                description=_column_option_description(column),
                value=str(idx),
                default=idx == state.x_index,
            )
            for idx, column in enumerate(sheet.columns[:MAX_PLOT_SELECT_OPTIONS])
        ]
        placeholder = "選擇 X 軸資料欄"
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self._guard_owner(interaction):
            return
        new_x = int(self.values[0])
        new_y = [idx for idx in self.state.y_indices if idx != new_x]
        new_state = replace(self.state, x_index=new_x, y_indices=new_y)
        clamp_plot_selection(self.preview, new_state)
        await interaction.response.edit_message(
            embed=build_plot_setup_embed(self.preview, new_state),
            view=PlotWorkbookConfigView(user_id=self.user_id, preview=self.preview, state=new_state),
        )


class _PlotYSelect(discord.ui.Select, _BaseOwnedComponent):
    def __init__(self, user_id: int, preview: PlotWorkbookPreview, state: PlotSelectionState):
        self.user_id = user_id
        self.preview = preview
        self.state = state
        sheet = selected_sheet(preview, state)
        options = [
            discord.SelectOption(
                label=_column_option_label(sheet, idx, column, role="y"),
                description=_column_option_description(column),
                value=str(idx),
                default=idx in state.y_indices,
            )
            for idx, column in enumerate(sheet.columns[:MAX_PLOT_SELECT_OPTIONS])
            if idx != state.x_index
        ]
        max_values = max(1, min(MAX_PLOT_Y_SERIES, len(options)))
        placeholder = "選擇 Y 軸資料欄（可多選）"
        super().__init__(placeholder=placeholder, min_values=1, max_values=max_values, options=options[:MAX_PLOT_SELECT_OPTIONS])

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self._guard_owner(interaction):
            return
        new_state = replace(self.state, y_indices=[int(value) for value in self.values])
        clamp_plot_selection(self.preview, new_state)
        await interaction.response.edit_message(
            embed=build_plot_setup_embed(self.preview, new_state),
            view=PlotWorkbookConfigView(user_id=self.user_id, preview=self.preview, state=new_state),
        )


class _PlotChartTypeSelect(discord.ui.Select, _BaseOwnedComponent):
    def __init__(self, user_id: int, preview: PlotWorkbookPreview, state: PlotSelectionState):
        self.user_id = user_id
        self.preview = preview
        self.state = state
        options = [
            discord.SelectOption(
                label=label,
                description=f"先用 {label} 來測試流程",
                value=value,
                default=value == state.chart_type,
            )
            for value, label in CHART_TYPE_LABELS.items()
        ]
        super().__init__(placeholder="選擇圖表類型", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self._guard_owner(interaction):
            return
        new_state = replace(self.state, chart_type=self.values[0])
        clamp_plot_selection(self.preview, new_state)
        await interaction.response.edit_message(
            embed=build_plot_setup_embed(self.preview, new_state),
            view=PlotWorkbookConfigView(user_id=self.user_id, preview=self.preview, state=new_state),
        )


class _PlotDoneButton(discord.ui.Button, _BaseOwnedComponent):
    def __init__(self, user_id: int, preview: PlotWorkbookPreview, state: PlotSelectionState):
        self.user_id = user_id
        self.preview = preview
        self.state = state
        super().__init__(label="完成設定", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self._guard_owner(interaction):
            return
        await interaction.response.send_message(
            "✅ 目前設定已記住。你可以直接按 `產生圖表` 先看第一版結果。\n\n" + selection_summary_text(self.preview, self.state),
            ephemeral=True,
        )


class _PlotGenerateButton(discord.ui.Button, _BaseOwnedComponent):
    def __init__(self, user_id: int, preview: PlotWorkbookPreview, state: PlotSelectionState):
        self.user_id = user_id
        self.preview = preview
        self.state = state
        super().__init__(label="產生圖表", style=discord.ButtonStyle.success, row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self._guard_owner(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            image_bytes = await asyncio.to_thread(render_plot_png, self.preview, self.state)
        except PlotRenderError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        except Exception:
            logger.exception("discord_plot_render_failed user=%s", self.user_id)
            await interaction.followup.send("⚠️ XE3 這次出圖失敗了，你可以先換一組 X / Y 再試一次。", ephemeral=True)
            return

        filename = _plot_output_filename(self.preview, self.state)
        file = discord.File(BytesIO(image_bytes), filename=filename)
        embed = discord.Embed(
            title="📈 圖表已產生",
            description="\n".join(
                [
                    f"• 工作表：**{selected_sheet(self.preview, self.state).name}**",
                    f"• 圖表：**{chart_type_label(self.state.chart_type)}**",
                    f"• X 軸：**{resolved_x_axis_label(self.preview, self.state)}**",
                    f"• Y 軸：**{resolved_y_axis_label(self.preview, self.state)}**",
                ]
            ),
            color=discord.Color.green(),
        )
        embed.set_image(url=f"attachment://{filename}")
        embed.set_footer(text="這是第一版出圖結果。如果你想調整欄位或標題，可以回到上一張設定卡再改。")
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)


class _PlotLabelButton(discord.ui.Button, _BaseOwnedComponent):
    def __init__(self, user_id: int, preview: PlotWorkbookPreview, state: PlotSelectionState):
        self.user_id = user_id
        self.preview = preview
        self.state = state
        super().__init__(label="自訂標題 / 軸名", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self._guard_owner(interaction):
            return
        if interaction.message is None:
            await interaction.response.send_message("⚠️ 找不到原本那張設定卡，請重新上傳一次檔案。", ephemeral=True)
            return
        await interaction.response.send_modal(
            PlotLabelsModal(
                user_id=self.user_id,
                preview=self.preview,
                state=self.state,
                message_id=interaction.message.id,
                channel_id=interaction.channel_id or 0,
            )
        )


class PlotLabelsModal(discord.ui.Modal, title="自訂圖表標題與軸名"):
    def __init__(
        self,
        *,
        user_id: int,
        preview: PlotWorkbookPreview,
        state: PlotSelectionState,
        message_id: int,
        channel_id: int,
    ) -> None:
        super().__init__()
        self.user_id = user_id
        self.preview = preview
        self.state = state
        self.message_id = int(message_id)
        self.channel_id = int(channel_id)

        self.title_input = discord.ui.TextInput(
            label="圖表標題",
            placeholder="例如：吸光值標準曲線",
            default=resolved_plot_title(preview, state)[:100],
            max_length=100,
            required=False,
        )
        self.x_input = discord.ui.TextInput(
            label="X 軸名稱",
            placeholder="例如：濃度 (mg/mL)",
            default=resolved_x_axis_label(preview, state)[:100],
            max_length=100,
            required=False,
        )
        self.y_input = discord.ui.TextInput(
            label="Y 軸名稱",
            placeholder="例如：吸光值 (OD600)",
            default=resolved_y_axis_label(preview, state)[:100],
            max_length=100,
            required=False,
        )

        self.add_item(self.title_input)
        self.add_item(self.x_input)
        self.add_item(self.y_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("這張圖表設定卡不是你的操作介面。", ephemeral=True)
            return

        new_state = replace(
            self.state,
            title=str(self.title_input.value or "").strip(),
            x_axis_label=str(self.x_input.value or "").strip(),
            y_axis_label=str(self.y_input.value or "").strip(),
        )
        clamp_plot_selection(self.preview, new_state)
        new_state, remap_note = apply_axis_label_mapping_hint(self.preview, new_state)
        channel = interaction.channel
        if channel is None and self.channel_id:
            channel = interaction.client.get_channel(self.channel_id)
            if channel is None:
                try:
                    channel = await interaction.client.fetch_channel(self.channel_id)
                except discord.DiscordException:
                    channel = None

        if channel is not None:
            try:
                message = await channel.fetch_message(self.message_id)
                await message.edit(
                    embed=build_plot_setup_embed(self.preview, new_state),
                    view=PlotWorkbookConfigView(user_id=self.user_id, preview=self.preview, state=new_state),
                )
            except discord.DiscordException:
                pass

        message = "✅ 已更新圖表標題與軸名稱。"
        if remap_note:
            message += "\n\n" + remap_note
        message += "\n\n" + selection_summary_text(self.preview, new_state)
        await interaction.response.send_message(message, ephemeral=True)


def _plot_output_filename(preview: PlotWorkbookPreview, state: PlotSelectionState) -> str:
    stem = resolved_plot_title(preview, state).strip() or selected_sheet(preview, state).name or "xe3_plot"
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in stem).strip("_")
    if not safe:
        safe = "xe3_plot"
    return f"{safe[:60]}.png"
