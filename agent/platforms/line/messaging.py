import base64
import hashlib
import hmac

import requests

from agent.core.config import line_channel_access_token, line_channel_secret


LINE_TEXT_LIMIT = 5000


def e3_quick_reply_items():
    commands = [
        ("課程", "e3 course"),
        ("成績", "e3 grades"),
        ("近期作業", "e3 近期 作業"),
        ("近期行事曆", "e3 近期 行事曆"),
        ("全部時間軸", "e3 timeline"),
        ("提醒設定", "e3 remind show"),
        ("狀態", "e3 狀態"),
        ("重登", "e3 relogin"),
        ("幫助", "e3 幫助"),
    ]
    return [
        {
            "type": "action",
            "action": {
                "type": "message",
                "label": label,
                "text": text,
            },
        }
        for label, text in commands
    ]


def default_quick_reply_items():
    return e3_quick_reply_items()


def normalize_line_text(text):
    text = str(text or "").strip()
    if len(text) <= LINE_TEXT_LIMIT:
        return text
    truncated = text[: LINE_TEXT_LIMIT - 12].rstrip()
    return truncated + "\n...(已截斷)"


def normalize_line_messages(response_payload, quick_reply_items=None):
    if isinstance(response_payload, dict):
        messages = response_payload.get("messages") or []
        fallback_text = normalize_line_text(response_payload.get("text") or "")
        normalized = []
        for idx, message in enumerate(messages):
            item = dict(message)
            if item.get("type") == "text":
                item["text"] = normalize_line_text(item.get("text"))
            if idx == 0:
                qr = response_payload.get("quick_reply_items", quick_reply_items)
                if qr:
                    item["quickReply"] = {"items": qr}
            normalized.append(item)
        if not normalized and fallback_text:
            normalized = [{"type": "text", "text": fallback_text}]
        return fallback_text, normalized

    text = normalize_line_text(response_payload)
    message = {"type": "text", "text": text}
    if quick_reply_items:
        message["quickReply"] = {"items": quick_reply_items}
    return text, [message]


def verify_signature(request, logger):
    signature = request.headers.get("X-Line-Signature")
    secret = line_channel_secret()
    if not signature or not secret:
        logger.warning(
            "signature_verification_failed reason=missing_signature_or_secret has_signature=%s has_secret=%s",
            bool(signature),
            bool(secret),
        )
        return False

    body = request.get_data(cache=True)
    hash_value = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    expected_signature = base64.b64encode(hash_value).decode()
    return hmac.compare_digest(signature, expected_signature)


def reply_to_line(reply_token, messages, logger):
    if not reply_token:
        logger.warning("skip_reply reason=empty_reply_token")
        return False

    token = line_channel_access_token()
    if not token:
        logger.error("line_reply_failed reason=missing_channel_access_token")
        return False

    url = "https://api.line.me/v2/bot/message/reply"
    headers = {"Authorization": f"Bearer {token}"}
    data = {"replyToken": reply_token, "messages": messages}
    try:
        response = requests.post(url, json=data, headers=headers, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        body = ""
        if getattr(exc, "response", None) is not None:
            body = exc.response.text
        logger.error("line_reply_failed error=%s body=%s", exc, body[:1000])
        return False


def push_to_line(user_id, response_payload, logger, quick_reply_items=None):
    if not user_id:
        logger.warning("skip_push reason=missing_user_id")
        return False

    token = line_channel_access_token()
    if not token:
        logger.error("line_push_failed reason=missing_channel_access_token")
        return False

    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Authorization": f"Bearer {token}"}
    if quick_reply_items is None:
        quick_reply_items = default_quick_reply_items()
    _, messages = normalize_line_messages(response_payload, quick_reply_items=quick_reply_items)
    data = {"to": user_id, "messages": messages}
    try:
        response = requests.post(url, json=data, headers=headers, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        body = ""
        if getattr(exc, "response", None) is not None:
            body = exc.response.text
        logger.error("line_push_failed error=%s body=%s", exc, body[:1000])
        return False


def send_line_response(reply_token, user_id, response_payload, logger, quick_reply_items=None):
    if quick_reply_items is None:
        quick_reply_items = default_quick_reply_items()
    fallback_text, messages = normalize_line_messages(response_payload, quick_reply_items=quick_reply_items)
    sent = False
    if reply_token:
        sent = reply_to_line(reply_token, messages, logger)
    if sent:
        return True
    if user_id:
        return push_to_line(user_id, fallback_text, logger, quick_reply_items=quick_reply_items)
    return False
