from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

import discord

from agent.platforms.discord.views import (
    CommandSelect,
    DiscordViewCallbacks,
    MessageCommandButton,
    ReminderScheduleSelect,
    ReminderTestButton,
    ReminderToggleButton,
)


MAX_LAYOUT_TEXT = 3900
MAX_LAYOUT_COMPONENTS = 40
_DECORATIVE_LINE_RE = re.compile(r"^[\s─━═—-]{3,}$")
_INLINE_HEADING_RE = re.compile(r"^[─━═—-]{2,}\s*(.+?)\s*[─━═—-]{2,}$")


@dataclass(frozen=True)
class LayoutBlock:
    text: str
    divider_before: bool = False


def _compact_lines(lines: Iterable[str]) -> str:
    compacted: list[str] = []
    previous_blank = False
    for raw in lines:
        line = str(raw or "").rstrip()
        if not line.strip():
            if compacted and not previous_blank:
                compacted.append("")
            previous_blank = True
            continue
        compacted.append(line.strip())
        previous_blank = False
    while compacted and not compacted[-1]:
        compacted.pop()
    return "\n".join(compacted)


def _is_decorative_line(line: str) -> bool:
    return bool(_DECORATIVE_LINE_RE.fullmatch(str(line or "").strip()))


def _split_description(description: str) -> list[LayoutBlock]:
    lines = str(description or "").splitlines()
    blocks: list[LayoutBlock] = []
    current: list[str] = []

    def flush(*, divider_before: bool = False) -> None:
        text = _compact_lines(current)
        current.clear()
        if text:
            blocks.append(LayoutBlock(text=text, divider_before=divider_before))

    idx = 0
    pending_divider = False
    while idx < len(lines):
        stripped = lines[idx].strip()
        if _is_decorative_line(stripped):
            next_idx = idx + 1
            while next_idx < len(lines) and not lines[next_idx].strip():
                next_idx += 1
            after_idx = next_idx + 1
            while after_idx < len(lines) and not lines[after_idx].strip():
                after_idx += 1
            if next_idx < len(lines) and after_idx < len(lines) and _is_decorative_line(lines[after_idx]):
                flush(divider_before=pending_divider)
                heading = lines[next_idx].strip().replace("**", "")
                blocks.append(LayoutBlock(text=f"### {heading}", divider_before=True))
                pending_divider = False
                idx = after_idx + 1
                continue
            flush(divider_before=pending_divider)
            pending_divider = True
            idx += 1
            continue

        inline_heading = _INLINE_HEADING_RE.fullmatch(stripped)
        if inline_heading:
            flush(divider_before=pending_divider)
            blocks.append(LayoutBlock(text=f"### {inline_heading.group(1).strip()}", divider_before=True))
            pending_divider = False
            idx += 1
            continue

        if pending_divider and stripped:
            flush(divider_before=False)
            pending_divider = False
            current.append(stripped)
        else:
            current.append(lines[idx])
        idx += 1

    flush(divider_before=pending_divider)
    return blocks


def _semantic_accent(embed: discord.Embed | None) -> discord.Colour:
    if embed is not None and embed.color is not None:
        return discord.Colour(embed.color.value)
    title = str(getattr(embed, "title", "") or "")
    if any(token in title for token in ("考試", "倒數", "錯誤", "失敗")):
        return discord.Colour.from_rgb(185, 28, 28)
    if "提醒" in title:
        return discord.Colour.from_rgb(217, 119, 6)
    if "成績" in title:
        return discord.Colour.from_rgb(124, 58, 237)
    if any(token in title for token in ("檔案", "教材", "公告")):
        return discord.Colour.from_rgb(37, 99, 235)
    return discord.Colour.from_rgb(15, 118, 110)


def _truncate_layout_text(parts: list[str], limit: int = MAX_LAYOUT_TEXT) -> list[str]:
    result: list[str] = []
    used = 0
    for part in parts:
        clean = str(part or "").strip()
        if not clean:
            continue
        remaining = limit - used
        if remaining <= 0:
            break
        if len(clean) > remaining:
            suffix = "\n-# 內容已截短"
            if remaining <= len(suffix):
                clean = suffix[-remaining:]
            else:
                clean = clean[: remaining - len(suffix)].rstrip() + suffix
        result.append(clean)
        used += len(clean)
    return result


def _button_for_action(
    callbacks: DiscordViewCallbacks,
    user_id: int,
    action: dict[str, str],
    *,
    primary: bool,
) -> discord.ui.Button | None:
    kind = str(action.get("kind") or "")
    label = str(action.get("label") or "開啟")[:80]
    if kind == "uri":
        url = str(action.get("value") or "")
        if not url or len(url) > 512:
            return None
        return discord.ui.Button(label=label, url=url)
    if kind != "message":
        return None
    meta = action.get("xe3_meta") if isinstance(action.get("xe3_meta"), dict) else {}
    is_navigation = str(meta.get("entry_kind") or "") == "navigation" or "上一頁" in label or "回到" in label
    style = discord.ButtonStyle.primary if primary and not is_navigation else discord.ButtonStyle.secondary
    return MessageCommandButton(
        callbacks,
        user_id,
        label,
        str(action.get("value") or ""),
        style=style,
    )


def _add_actions(
    container: discord.ui.Container,
    callbacks: DiscordViewCallbacks,
    user_id: int,
    actions: list[dict[str, str]],
) -> None:
    buttons: list[discord.ui.Button] = []
    for action in actions[:5]:
        button = _button_for_action(callbacks, user_id, action, primary=not buttons)
        if button is not None:
            buttons.append(button)
    if buttons:
        container.add_item(discord.ui.ActionRow(*buttons))


def _embed_container(
    embed: discord.Embed,
    *,
    callbacks: DiscordViewCallbacks,
    user_id: int,
    actions: list[dict[str, str]] | None = None,
    text_limit: int = MAX_LAYOUT_TEXT,
) -> discord.ui.Container:
    container = discord.ui.Container(accent_colour=_semantic_accent(embed))
    title = str(embed.title or "XE3").strip()
    raw_blocks = _split_description(str(embed.description or ""))[:12]
    text_parts = [f"## {title}"] + [block.text for block in raw_blocks]
    footer = str(getattr(getattr(embed, "footer", None), "text", "") or "").strip()
    if footer and footer != title:
        text_parts.append(f"-# {footer}")
    safe_parts = _truncate_layout_text(text_parts, limit=text_limit)

    if safe_parts:
        container.add_item(discord.ui.TextDisplay(safe_parts[0]))
    for block, safe_text in zip(raw_blocks, safe_parts[1:]):
        if block.divider_before:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.TextDisplay(safe_text))
    if len(safe_parts) > 1 + len(raw_blocks):
        container.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.TextDisplay(safe_parts[-1]))

    if actions:
        container.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))
        _add_actions(container, callbacks, user_id, actions)
    return container


def build_embed_layout(
    embeds: list[discord.Embed],
    *,
    callbacks: DiscordViewCallbacks,
    user_id: int,
    actions: list[dict[str, str]] | None = None,
    timeout: float = 600,
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    remaining_text = MAX_LAYOUT_TEXT
    for idx, embed in enumerate(embeds[:6]):
        item_actions = actions if idx == 0 else None
        container = _embed_container(
            embed,
            callbacks=callbacks,
            user_id=user_id,
            actions=item_actions,
            text_limit=remaining_text,
        )
        try:
            view.add_item(container)
        except ValueError:
            break
        remaining_text -= container.content_length()
        if remaining_text < 80:
            break
    return view


def build_text_layout(
    text: str,
    *,
    title: str | None = None,
    color: discord.Colour | None = None,
    timeout: float = 600,
) -> discord.ui.LayoutView:
    raw = str(text or "（空白回覆）").strip()
    lines = raw.splitlines()
    if title is None and lines:
        candidate = lines[0].replace("**", "").strip()
        if len(candidate) <= 100 and (len(lines) > 1 or candidate[:1] in "⚠️❌✅📊⏰📚📎📰📝🗓️🎉❓🧹"):
            title = candidate
            raw = "\n".join(lines[1:]).strip() or "目前沒有其他內容。"
    embed = discord.Embed(title=title or "XE3", description=raw, color=color)
    return build_embed_layout(embeds=[embed], callbacks=_NOOP_CALLBACKS, user_id=0, timeout=timeout)


def build_select_layout(
    summary: discord.Embed,
    *,
    callbacks: DiscordViewCallbacks,
    user_id: int,
    entries: list[tuple[str, str, dict[str, str]]],
    timeout: float = 600,
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    container = _embed_container(summary, callbacks=callbacks, user_id=user_id)
    container.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))
    container.add_item(
        discord.ui.ActionRow(
            CommandSelect(
                callbacks,
                user_id,
                entries,
                placeholder=str(summary.title or "選擇一個項目"),
            )
        )
    )
    view.add_item(container)
    return view


def build_reminder_layout(
    embed: discord.Embed,
    *,
    callbacks: DiscordViewCallbacks,
    user_id: int,
    enabled: bool,
    schedule: list[str],
    timeout: float = 600,
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    container = _embed_container(embed, callbacks=callbacks, user_id=user_id)
    container.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))
    container.add_item(
        discord.ui.ActionRow(
            ReminderToggleButton(callbacks, user_id, enabled),
            ReminderTestButton(callbacks, user_id),
        )
    )
    container.add_item(discord.ui.ActionRow(ReminderScheduleSelect(callbacks, user_id, schedule)))
    view.add_item(container)
    return view


async def _noop_command(interaction: discord.Interaction, user_id: int, command: str) -> None:
    return None


async def _noop_uri(
    interaction: discord.Interaction,
    user_id: int,
    action: dict[str, str],
    description: str,
    label: str,
) -> None:
    return None


async def _noop_test(interaction: discord.Interaction, user_id: int) -> None:
    return None


async def _noop_login(interaction: discord.Interaction, account: str, password: str) -> None:
    return None


_NOOP_CALLBACKS = DiscordViewCallbacks(
    run_command=_noop_command,
    run_uri_action=_noop_uri,
    run_test_reminder=_noop_test,
    run_login_modal=_noop_login,
    schedule_command_for_slots=lambda slots: "",
)


def validate_layout(view: discord.ui.LayoutView) -> None:
    if view.content_length() > 4000:
        raise ValueError("Components v2 text exceeds Discord's 4000-character limit")
    components = view.to_components()
    if not components:
        raise ValueError("Components v2 layout is empty")
    if view._total_children > MAX_LAYOUT_COMPONENTS:
        raise ValueError("Components v2 layout exceeds Discord's 40-component limit")
