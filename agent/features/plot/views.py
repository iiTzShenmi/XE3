from __future__ import annotations

import discord

from agent.features.plot.service import (
    MAX_PLOT_SELECT_OPTIONS,
    PLOT_PREVIEW_ROW_COUNT,
    PlotSelectionState,
    PlotWorkbookPreview,
    chart_type_label,
    resolved_plot_title,
    resolved_x_axis_label,
    resolved_y_axis_label,
    selected_sheet,
)


def build_plot_setup_embed(preview: PlotWorkbookPreview, state: PlotSelectionState) -> discord.Embed:
    sheet = selected_sheet(preview, state)
    x_label = sheet.columns[state.x_index].name if sheet.columns and state.x_index < len(sheet.columns) else "尚未選擇"
    y_labels = [sheet.columns[idx].name for idx in state.y_indices if idx < len(sheet.columns)]
    y_label = "、".join(y_labels) if y_labels else "尚未選擇"
    lines = [
        "請先確認工作表、X 軸、Y 軸和圖表類型。設定好後，直接按下 `產生圖表` 就能先看第一版結果。",
        "",
        "━━━━━━━━━━━━",
        "📁 檔案資訊",
        "━━━━━━━━━━━━",
        f"• 檔名：`{preview.filename}`",
        f"• 來源：**{'Excel' if preview.file_kind == 'excel' else 'CSV'}**",
        f"• 工作表數：`{len(preview.sheets)}`",
        f"• 目前工作表：**{sheet.name}**",
        f"• 資料列：`{sheet.row_count}`",
        f"• 欄位數：`{len(sheet.columns)}`",
        "",
        "━━━━━━━━━━━━",
        "⚙️ 目前設定",
        "━━━━━━━━━━━━",
        f"• 圖表標題：**{resolved_plot_title(preview, state)}**",
        f"• X 軸名稱：**{resolved_x_axis_label(preview, state)}**",
        f"  ↳ 對應欄位：**{x_label}**",
        f"• Y 軸名稱：**{resolved_y_axis_label(preview, state)}**",
        f"  ↳ 對應欄位：**{y_label}**",
        f"• 圖表：**{chart_type_label(state.chart_type)}**",
    ]

    if not sheet.has_header:
        lines.extend(
            [
                "",
                "⚠️ 這份資料看起來沒有欄名，所以 XE3 先用 `預設X軸 / 預設Y軸 / 預設Y軸2...` 來幫你對齊預覽。",
                "你現在可以直接繼續設定；如果想讓後面的出圖更穩，我也會附上一份 `plot_template.csv` 給你參考。",
            ]
        )

    if len(sheet.columns) > MAX_PLOT_SELECT_OPTIONS:
        lines.extend(
            [
                "",
                f"⚠️ 這個工作表有 `{len(sheet.columns)}` 欄，這一版的下拉選單先顯示前 `{MAX_PLOT_SELECT_OPTIONS}` 欄給你測流程。",
            ]
        )

    preview_lines = []
    for column in sheet.columns[:8]:
        icon = "🔢" if column.numeric else ("🕒" if "日期" in column.inferred_type else "🔤")
        preview_lines.append(f"• {icon} **{column.name}**｜{column.inferred_type}")
        if column.sample_values:
            preview_lines.append(f"  例：`{' / '.join(column.sample_values[:5])}`")

    if preview_lines:
        lines.extend(["", "━━━━━━━━━━━━", "🧪 欄位預覽", "━━━━━━━━━━━━", *preview_lines])

    aligned_lines = _aligned_preview_lines(preview, sheet, state)
    if aligned_lines:
        lines.extend(["", "━━━━━━━━━━━━", f"🔍 對齊預覽（前 {PLOT_PREVIEW_ROW_COUNT} 筆）", "━━━━━━━━━━━━", *aligned_lines])

    embed = discord.Embed(
        title="📈 Excel 圖表設定",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="先確認欄位對齊，再按「產生圖表」。")
    return embed


def _aligned_preview_lines(preview, sheet, state: PlotSelectionState) -> list[str]:
    if not sheet.preview_rows or not sheet.columns:
        return []

    selected_indices = [state.x_index, *state.y_indices]
    selected_indices = [idx for idx in selected_indices if idx < len(sheet.columns)]
    if not selected_indices:
        return []

    lines: list[str] = []
    x_alias = resolved_x_axis_label(preview, state)[:12] or "X"
    y_alias = resolved_y_axis_label(preview, state)[:12] or "Y"
    for row_idx, row in enumerate(sheet.preview_rows[:PLOT_PREVIEW_ROW_COUNT], start=1):
        parts = []
        for position, idx in enumerate(selected_indices):
            if idx >= len(sheet.columns):
                continue
            value = row[idx] if idx < len(row) else ""
            if position == 0:
                label = x_alias
            else:
                label = y_alias if position == 1 else f"Y{position}"
            parts.append(f"{label}=`{value or '∅'}`")
        if parts:
            lines.append(f"• 第 {row_idx} 筆｜" + " ｜ ".join(parts))
    return lines
