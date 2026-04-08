#!/usr/bin/env python3
import json
import time

import requests

from agent.core.config import (
    line_channel_access_token,
    public_base_url,
    tunnel_watchdog_state_file,
)


WATCH_INTERVAL_SECONDS = 60
RECIPIENT_ENV_FALLBACK = "LINE_NOTIFY_USER_ID"


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


def line_recipient():
    import os

    return os.getenv(RECIPIENT_ENV_FALLBACK, "").strip()


def push_line_message(text):
    token = line_channel_access_token()
    recipient = line_recipient()
    if not token or not recipient:
        return False
    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}"},
            json={"to": recipient, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
        if response.status_code == 429:
            return False
        response.raise_for_status()
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
                push_line_message(f"✅ Cloudflare tunnel 已恢復\n{detail}")
            else:
                push_line_message(f"⚠️ Cloudflare tunnel 目前無法連線\n{detail}")
            state = {"healthy": healthy, "detail": detail}
            save_state(state)

        time.sleep(WATCH_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
