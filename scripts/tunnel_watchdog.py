#!/usr/bin/env python3
import json
import time

import requests

from agent.core.config import (
    discord_bot_token,
    discord_notify_user_id,
    public_base_url,
    tunnel_watchdog_state_file,
)


WATCH_INTERVAL_SECONDS = 60


def load_state():
    path = tunnel_watchdog_state_file()
    if not path.exists():
        return {"healthy": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"healthy": None}


def save_state(state):
    tunnel_watchdog_state_file().write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _discord_headers() -> dict[str, str] | None:
    token = discord_bot_token()
    if not token:
        return None
    return {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "XE3-Watchdog/1.0",
    }


def push_discord_dm(text):
    headers = _discord_headers()
    recipient = discord_notify_user_id()
    if not headers or not recipient:
        return False
    try:
        channel_response = requests.post(
            "https://discord.com/api/v10/users/@me/channels",
            headers=headers,
            json={"recipient_id": str(recipient)},
            timeout=10,
        )
        if channel_response.status_code == 429:
            return False
        channel_response.raise_for_status()
        channel_id = (channel_response.json() or {}).get("id")
        if not channel_id:
            return False
        message_response = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=headers,
            json={"content": text[:1900]},
            timeout=10,
        )
        if message_response.status_code == 429:
            return False
        message_response.raise_for_status()
        return True
    except requests.RequestException:
        return False


def check_health():
    base_url = public_base_url()
    if not base_url:
        return False, "PUBLIC_BASE_URL 未設定，或尚未取得 cloudflared 公開網址。"
    response = requests.get(f"{base_url}/healthz", timeout=10)
    if response.status_code != 200:
        return False, f"healthz 回應異常：HTTP {response.status_code}"
    return True, base_url


def main():
    while True:
        state = load_state()
        healthy, detail = False, ""
        try:
            healthy, detail = check_health()
        except Exception as exc:  # pragma: no cover
            healthy = False
            detail = str(exc)

        previous = state.get("healthy")
        if previous is None:
            state = {"healthy": healthy, "detail": detail}
            save_state(state)
        elif previous != healthy:
            if healthy:
                push_discord_dm(f"✅ Cloudflare tunnel 已恢復\n{detail}")
            else:
                push_discord_dm(f"⚠️ Cloudflare tunnel 目前無法連線\n{detail}")
            state = {"healthy": healthy, "detail": detail}
            save_state(state)

        time.sleep(WATCH_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
