from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from agent.features.e3.reminder.payloads import format_countdown_payload
from agent.platforms.discord.components_v2 import build_select_layout, validate_layout
from agent.platforms.discord.payload_sender import edit_message_from_payload, send_payload
from agent.platforms.discord.views import DiscordViewCallbacks


async def _noop(*args):
    return None


@pytest.fixture
def callbacks() -> DiscordViewCallbacks:
    return DiscordViewCallbacks(
        run_command=_noop,
        run_uri_action=_noop,
        run_test_reminder=_noop,
        run_login_modal=_noop,
        schedule_command_for_slots=lambda slots: "",
    )


def test_selector_layout_uses_native_separators_and_select(callbacks):
    summary = discord.Embed(
        title="選擇作業詳情",
        description=(
            "請從下方選擇。\n\n"
            "━━━━━━━━━━━━\n🟠 作業\n━━━━━━━━━━━━\n\n"
            "1️⃣ **Homework**\n📝 作業｜測試課程"
        ),
        color=discord.Colour.teal(),
    )
    entries = [
        (
            "Homework",
            "作業｜測試課程",
            {
                "kind": "message",
                "label": "查看詳情",
                "value": "e3 詳情 1",
                "xe3_meta": {"event_type": "homework"},
            },
        )
    ]

    view = build_select_layout(summary, callbacks=callbacks, user_id=1, entries=entries)
    validate_layout(view)
    payload = view.to_components()[0]["components"]

    assert any(item["type"] == 14 for item in payload)
    text = "\n".join(item.get("content", "") for item in payload)
    assert "### 🟠 作業" in text
    assert "### ━" not in text
    select = next(item for item in payload if item["type"] == 1)["components"][0]
    assert select["type"] == 3
    assert select["options"][0]["emoji"]["name"] == "📝"


@pytest.mark.asyncio
async def test_edit_converts_legacy_message_without_mixing_embeds(callbacks):
    message = SimpleNamespace(edit=AsyncMock(), flags=SimpleNamespace(components_v2=False))
    payload = {
        "messages": [
            {
                "type": "flex",
                "altText": "課程摘要",
                "contents": {
                    "type": "bubble",
                    "header": {
                        "type": "box",
                        "backgroundColor": "#0F766E",
                        "contents": [{"type": "text", "text": "課程摘要"}],
                    },
                    "body": {
                        "type": "box",
                        "contents": [{"type": "text", "text": "生物化學（二）"}],
                    },
                },
            }
        ]
    }

    edited = await edit_message_from_payload(
        message,
        payload,
        user_id=1,
        callbacks=callbacks,
        send_text_chunks=_noop,
    )

    assert edited is True
    kwargs = message.edit.await_args.kwargs
    assert kwargs["content"] is None
    assert kwargs["embeds"] == []
    assert kwargs["attachments"] == []
    assert isinstance(kwargs["view"], discord.ui.LayoutView)


@pytest.mark.asyncio
async def test_components_v2_message_stays_v2_for_text_result(callbacks):
    message = SimpleNamespace(edit=AsyncMock(), flags=SimpleNamespace(components_v2=True))

    edited = await edit_message_from_payload(
        message,
        "⚠️ 找不到指定的課程。",
        user_id=1,
        callbacks=callbacks,
        send_text_chunks=_noop,
    )

    assert edited is True
    kwargs = message.edit.await_args.kwargs
    assert isinstance(kwargs["view"], discord.ui.LayoutView)
    assert "embed" not in kwargs
    assert kwargs["embeds"] == []


@pytest.mark.asyncio
async def test_reminder_notification_sends_only_layout_view(callbacks):
    target = SimpleNamespace(send=AsyncMock())

    await send_payload(
        target,
        "⏰ **E3 提醒 09:00**\n早安，今天沒有截止事件。",
        user_id=1,
        callbacks=callbacks,
        send_text_chunks=_noop,
    )

    kwargs = target.send.await_args.kwargs
    assert list(kwargs) == ["view"]
    assert isinstance(kwargs["view"], discord.ui.LayoutView)
    validate_layout(kwargs["view"])


@pytest.mark.asyncio
async def test_homework_reminder_attachment_uses_direct_download_button(callbacks):
    target = SimpleNamespace(send=AsyncMock())
    due_at = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    row = {
        "event_uid": "homework-1",
        "event_type": "homework",
        "course_id": "course-1",
        "course_name": "測試課程",
        "title": "Homework 1",
        "due_at": due_at,
        "payload_json": '{"attachments":[{"name":"question.pdf","url":"https://e3p.nycu.edu.tw/pluginfile.php/1/question.pdf"}]}',
    }
    payload = format_countdown_payload(row, 12, "discord:123")

    await send_payload(
        target,
        payload,
        user_id=123,
        callbacks=callbacks,
        send_text_chunks=_noop,
    )

    kwargs = target.send.await_args.kwargs
    assert list(kwargs) == ["view"]
    view = kwargs["view"]
    assert view.timeout == 86400
    serialized = str(view.to_components())
    assert "question.pdf" in serialized
    assert "custom_id" in serialized
    assert "https://e3p.nycu.edu.tw" not in serialized
