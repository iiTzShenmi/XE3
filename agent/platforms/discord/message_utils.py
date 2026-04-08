from __future__ import annotations

from typing import Any

import discord

from agent.features.e3.views.payloads import META_KEY, merge_meta, message_meta, payload_meta
from agent.platforms.discord.rendering import bubble_description, bubble_header_lines, bubble_title, format_discord_text, hex_to_color


def response_text(payload: Any) -> str:
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


def special_text_embed(text: str) -> discord.Embed | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    normalized = raw.replace("**", "").strip()
    if normalized.startswith("⏰ E3 提醒") or normalized.startswith("⏰ E3 倒數提醒") or normalized.startswith("⏰ 提醒測試"):
        lines = [line.rstrip() for line in raw.splitlines()]
        title = lines[0].replace("**", "").strip() if lines else "⏰ XE3 提醒"
        body = format_discord_text("\n".join(line for line in lines[1:] if line is not None).strip())
        return discord.Embed(title=title, description=body or "目前沒有提醒內容。", color=discord.Color.orange())
    if normalized.startswith("📊 成績更新"):
        lines = [line.rstrip() for line in raw.splitlines()]
        title = lines[0].replace("**", "").strip() if lines else "📊 成績更新"
        body = format_discord_text("\n".join(line.replace("**", "") for line in lines[1:] if line is not None).strip())
        return discord.Embed(title=title, description=body or "有新的成績內容。", color=discord.Color.green())
    return None


def chunk_text(text: str, limit: int = 1900) -> list[str]:
    raw = format_discord_text(str(text or "").strip())
    if not raw:
        return ["（空白回覆）"]
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


async def send_text_chunks(target: Any, text: str, *, ephemeral: bool = False) -> None:
    for idx, chunk in enumerate(chunk_text(text)):
        if isinstance(target, discord.Interaction):
            if not target.response.is_done() and idx == 0:
                await target.response.send_message(chunk, ephemeral=ephemeral)
            else:
                await target.followup.send(chunk, ephemeral=ephemeral)
        else:
            await target.send(chunk)


def bubble_actions(bubble: dict[str, Any], inherited_meta: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []

    def walk(node: Any, active_meta: dict[str, Any]) -> None:
        if isinstance(node, dict):
            current_meta = merge_meta(active_meta, node.get(META_KEY))
            if node.get("type") == "button":
                action = node.get("action") or {}
                action_type = action.get("type")
                action_meta = merge_meta(current_meta, action.get(META_KEY))
                if action_type == "message":
                    actions.append(
                        {
                            "kind": "message",
                            "label": str(action.get("label") or "開啟"),
                            "value": str(action.get("text") or ""),
                            META_KEY: action_meta,
                        }
                    )
                elif action_type == "uri":
                    actions.append(
                        {
                            "kind": "uri",
                            "label": str(action.get("label") or "開啟"),
                            "value": str(action.get("uri") or ""),
                            META_KEY: action_meta,
                        }
                    )
                return
            for key, value in node.items():
                if key == META_KEY:
                    continue
                walk(value, current_meta)
        elif isinstance(node, list):
            for item in node:
                walk(item, active_meta)

    walk(bubble, inherited_meta or {})
    return actions


def extract_embed_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        text = response_text(payload)
        special = special_text_embed(text)
        if special is not None:
            return [{"embed": special, "actions": [], "text": None, "meta": {}}]
        return [{"embed": None, "actions": [], "text": text, "meta": {}}]

    root_meta = payload_meta(payload)
    messages = payload.get("messages") or []
    items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        current_message_meta = message_meta(message, root_meta)
        if message.get("type") == "text":
            text = str(message.get("text") or "")
            special = special_text_embed(text)
            if special is not None:
                items.append({"embed": special, "actions": [], "text": None, "meta": current_message_meta})
            else:
                items.append({"embed": None, "actions": [], "text": text, "meta": current_message_meta})
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
            bubble_meta = merge_meta(current_message_meta, bubble.get(META_KEY))
            embed = discord.Embed(
                title=bubble_title(bubble),
                description=bubble_description(bubble),
                color=hex_to_color(((bubble.get("header") or {}).get("backgroundColor"))) or discord.Color.blurple(),
            )
            header_lines = bubble_header_lines(bubble)
            if header_lines:
                header_hint = header_lines[0].strip()
                if header_hint and header_hint != str(embed.title or "").strip():
                    embed.set_footer(text=header_hint[:2048])
            items.append(
                {
                    "embed": embed,
                    "actions": bubble_actions(bubble, bubble_meta),
                    "text": None,
                    "meta": bubble_meta,
                }
            )

    if not items:
        text = response_text(payload)
        special = special_text_embed(text)
        if special is not None:
            items.append({"embed": special, "actions": [], "text": None, "meta": root_meta})
        else:
            items.append({"embed": None, "actions": [], "text": text, "meta": root_meta})
    return items
