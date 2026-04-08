from __future__ import annotations

import io
from typing import Callable
from urllib.parse import urlparse

import discord
import requests

from agent.features.e3.data.file_proxy import (
    FileProxyError,
    prepare_proxy_download,
    prepare_user_download,
    sanitize_download_filename,
)


def extract_proxy_token(url: str, *, public_base_url_getter: Callable[[], str]) -> str | None:
    base = public_base_url_getter()
    parsed = urlparse(str(url or ""))
    if base and str(url).startswith(base + "/e3/file/"):
        return str(url).split("/e3/file/", 1)[1]
    if parsed.path.startswith("/e3/file/"):
        return parsed.path.split("/e3/file/", 1)[1]
    return None


def download_discord_attachment(
    user_id: int,
    action: dict[str, str],
    fallback_name: str,
    *,
    public_base_url_getter: Callable[[], str],
    attachment_max_bytes_getter: Callable[[], int],
) -> tuple[discord.File | None, str | None]:
    source = str(action.get("value") or "").strip()
    if not source:
        return None, None

    try:
        token = extract_proxy_token(source, public_base_url_getter=public_base_url_getter)
        if token:
            payload = prepare_proxy_download(token)
        else:
            payload = prepare_user_download(
                f"discord:{user_id}",
                source,
                filename=fallback_name,
                max_bytes=attachment_max_bytes_getter(),
            )
    except FileProxyError as exc:
        return None, exc.message
    except requests.RequestException:
        return None, "目前無法從 E3 下載這個檔案。"

    response = payload["response"]
    filename = sanitize_download_filename(payload.get("filename") or fallback_name or "download")
    try:
        data = response.content
    finally:
        response.close()

    if len(data) > attachment_max_bytes_getter():
        return None, "這個檔案太大，無法直接上傳到 Discord。"

    return discord.File(io.BytesIO(data), filename=filename), None
