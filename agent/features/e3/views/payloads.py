from __future__ import annotations

from copy import deepcopy
from typing import Any

META_KEY = "xe3_meta"


def merge_meta(*sources: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in sources:
        if isinstance(source, dict):
            for key, value in source.items():
                if value is not None:
                    merged[key] = value
    return merged


def attach_message_meta(message: dict[str, Any], meta: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    enriched = deepcopy(message)
    merged = merge_meta(enriched.get(META_KEY), meta or {}, extra)
    if merged:
        enriched[META_KEY] = merged
    return enriched


def payload_meta(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    raw = payload.get(META_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def message_meta(message: Any, inherited: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = message.get(META_KEY) if isinstance(message, dict) else None
    return merge_meta(inherited or {}, raw if isinstance(raw, dict) else {})


def line_response(text: str, messages: list[dict[str, Any]] | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"text": text}
    if messages:
        payload["messages"] = messages
    merged = merge_meta(meta or {})
    if merged:
        payload[META_KEY] = merged
    return payload
