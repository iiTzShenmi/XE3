import logging
import json
import os
import shutil
import subprocess
import time

import requests
from flask import Flask, Response, request

from agent.config import (
    auto_reload_enabled,
    cloudflared_url_file,
    line_notify_user_id,
    port,
    tunnel_watchdog_state_file,
)
from agent.features.e3.reminders import start_reminder_worker
from agent.features.weather import handle_city_weather, handle_location_weather
from agent.features.e3 import handle_e3_command
from agent.features.e3.file_proxy import FileProxyError, FileProxySessionExpired, prepare_proxy_download
from agent.platforms.line.background import (
    build_processing_ack,
    is_background_e3_command,
    register_background_command,
    start_e3_background_task,
)
from agent.platforms.line.messaging import (
    e3_quick_reply_items,
    push_to_line,
    send_line_response,
    verify_signature,
)


app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _systemctl_state(unit_name, user=False):
    cmd = ["systemctl"]
    if user:
        cmd.append("--user")
    cmd.extend(["is-active", unit_name])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
    except Exception as exc:  # pragma: no cover
        return f"error:{exc}"
    state = (result.stdout or result.stderr or "").strip()
    return state or f"exit:{result.returncode}"


def _process_active(pattern):
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # pragma: no cover
        return False
    return result.returncode == 0 and bool((result.stdout or "").strip())


def _read_watchdog_state():
    path = tunnel_watchdog_state_file()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _tunnel_status_summary():
    url_path = cloudflared_url_file()
    url = ""
    if url_path.exists():
        try:
            url = url_path.read_text(encoding="utf-8").strip()
        except OSError:
            url = ""
    active = _process_active("cloudflared tunnel --url")
    if active and url:
        return f"active ({url})"
    if active:
        return "active (等待公開網址)"
    return "inactive"


def _watchdog_status_summary():
    active = _process_active("scripts/tunnel_watchdog.py")
    state = _read_watchdog_state() or {}
    healthy = state.get("healthy")
    detail = str(state.get("detail") or "").strip()
    if active and healthy is True:
        return f"active (healthy)"
    if active and healthy is False:
        return f"active (unhealthy: {detail})" if detail else "active (unhealthy)"
    if active:
        return "active"
    if healthy is True:
        return "inactive (last seen healthy)"
    if healthy is False:
        return f"inactive (last seen unhealthy: {detail})" if detail else "inactive (last seen unhealthy)"
    return "inactive"


def _memory_summary():
    total_kb = None
    avail_kb = None
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail_kb = int(line.split()[1])
    except OSError:
        return "記憶體：unknown"

    if not total_kb or avail_kb is None:
        return "記憶體：unknown"

    used_kb = max(0, total_kb - avail_kb)
    used_gb = used_kb / 1024 / 1024
    total_gb = total_kb / 1024 / 1024
    percent = (used_kb / total_kb) * 100 if total_kb else 0
    return f"記憶體：{used_gb:.1f}/{total_gb:.1f} GB ({percent:.0f}%)"


def _disk_summary():
    usage = shutil.disk_usage("/")
    used_gb = (usage.total - usage.free) / 1024 / 1024 / 1024
    total_gb = usage.total / 1024 / 1024 / 1024
    percent = ((usage.total - usage.free) / usage.total) * 100 if usage.total else 0
    return f"磁碟：{used_gb:.1f}/{total_gb:.1f} GB ({percent:.0f}%)"


def _uptime_summary():
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            seconds = int(float(handle.read().split()[0]))
    except OSError:
        return "開機時間：unknown"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"開機時間：{days}d {hours}h {minutes}m"
    return f"開機時間：{hours}h {minutes}m"


def _load_summary():
    load1, load5, load15 = os.getloadavg()
    cores = os.cpu_count() or 1
    ratio = load1 / cores if cores else load1
    if ratio < 0.5:
        level = "輕"
    elif ratio < 1.0:
        level = "中"
    else:
        level = "高"
    return f"系統負載：{load1:.2f} / {load5:.2f} / {load15:.2f}（1/5/15 分鐘，{cores} 核心，{level}）"


def _build_chksys_report():
    return (
        "🛠️ 系統檢查\n"
        f"主服務：{_systemctl_state('multi-task-agent.service')}\n"
        f"Tunnel：{_tunnel_status_summary()}\n"
        f"Watchdog：{_watchdog_status_summary()}\n"
        f"{_load_summary()}\n"
        f"{_memory_summary()}\n"
        f"{_disk_summary()}\n"
        f"{_uptime_summary()}"
    )


def _handle_chksys(reply_token, line_user_id):
    report = _build_chksys_report()
    target_user = line_notify_user_id() or line_user_id
    pushed = push_to_line(target_user, report, logger)
    ack = "🛠️ 系統狀態已傳送。" if pushed else "⚠️ 系統狀態查詢完成，但推播失敗，請檢查 LINE_NOTIFY_USER_ID。"
    if reply_token:
        send_line_response(reply_token, line_user_id, ack, logger)
    elif line_user_id:
        push_to_line(line_user_id, ack, logger)


def _render_proxy_error_page(title, message, suggestion=""):
    extra = f"<p>{suggestion}</p>" if suggestion else ""
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: linear-gradient(180deg, #f8fafc 0%, #e2e8f0 100%);
      color: #0f172a;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }}
    .card {{
      max-width: 560px;
      width: 100%;
      background: #ffffff;
      border-radius: 18px;
      box-shadow: 0 20px 40px rgba(15, 23, 42, 0.12);
      padding: 28px;
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 24px;
    }}
    p {{
      line-height: 1.6;
      margin: 0 0 10px;
      color: #334155;
    }}
    .hint {{
      margin-top: 18px;
      padding: 14px 16px;
      border-radius: 12px;
      background: #eff6ff;
      color: #1d4ed8;
      font-weight: 600;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    <p>{message}</p>
    {extra}
  </div>
</body>
</html>"""


def _normalize_shortcut_text(text):
    normalized = (text or "").strip()
    lowered = normalized.lower()
    shortcut_map = {
        "課程": "e3 course",
        "course": "e3 course",
        "近期": "e3 近期",
        "upcoming": "e3 近期",
        "作業": "e3 近期 作業",
        "homework": "e3 近期 作業",
        "行事曆": "e3 timeline 行事曆",
        "calendar": "e3 timeline 行事曆",
        "timeline": "e3 timeline",
        "狀態": "e3 狀態",
        "status": "e3 狀態",
        "重登": "e3 relogin",
        "relogin": "e3 relogin",
        "幫助": "e3 幫助",
        "help": "e3 幫助",
    }
    return shortcut_map.get(normalized) or shortcut_map.get(lowered) or normalized


def _handle_weather_text(text, reply_token, line_user_id):
    parts = text[2:].strip()
    if parts:
        reply = handle_city_weather(parts, logger)
    else:
        reply = handle_location_weather(None, logger)
    if reply_token:
        send_line_response(reply_token, line_user_id, reply, logger)
    else:
        logger.warning("skip_reply reason=missing_reply_token message_type=text")


def _handle_e3_text(text, reply_token, line_user_id):
    if is_background_e3_command(text):
        accepted, existing = register_background_command(line_user_id, text)
        if not accepted:
            logger.info(
                "e3_background_duplicate user=%s text=%s age_ms=%s",
                line_user_id,
                text,
                int((time.time() - existing["started_at"]) * 1000),
            )
            ack = "⏳ 這個 E3 指令已在處理中，請稍等一下，我完成後會直接推播結果。"
        else:
            logger.info("e3_background_queued user=%s text=%s", line_user_id, text)
            ack = build_processing_ack(text)
        if reply_token:
            send_line_response(
                reply_token,
                line_user_id,
                ack,
                logger,
                quick_reply_items=e3_quick_reply_items(),
            )
        else:
            logger.warning("skip_reply reason=missing_reply_token message_type=text")
        if accepted:
            start_e3_background_task(
                text,
                line_user_id,
                logger,
                lambda user_id, payload: push_to_line(user_id, payload, logger),
            )
        return

    result = handle_e3_command(text, logger, line_user_id)
    if reply_token:
        send_line_response(
            reply_token,
            line_user_id,
            result,
            logger,
            quick_reply_items=e3_quick_reply_items(),
        )
    else:
        logger.warning("skip_reply reason=missing_reply_token message_type=text")


def _handle_homevault(reply_token, line_user_id):
    reply = (
        "支援指令：\n"
        "1) 天氣 台北\n"
        "2) 天氣\n"
        "3) e3 login <帳號> <密碼>\n"
        "4) e3 relogin\n"
        "5) e3 課程 / e3 course\n"
        "6) e3 近期 [作業/行事曆/考試]\n"
        "7) e3 timeline / e3 行事曆 [作業/行事曆/考試]\n"
        "8) e3 詳情 <編號>\n"
        "9) e3 grades / e3 成績\n"
        "10) e3 files <課名關鍵字>\n"
        "11) e3 remind show/on/off\n"
        "12) e3 幫助\n"
        "13) chksys"
    )
    if reply_token:
        send_line_response(
            reply_token,
            line_user_id,
            reply,
            logger,
            quick_reply_items=e3_quick_reply_items(),
        )
    else:
        logger.warning("skip_reply reason=missing_reply_token message_type=text")


@app.route("/callback", methods=["POST"])
def callback():
    if not verify_signature(request, logger):
        return "Unauthorized", 401

    data = request.get_json(silent=True) or {}
    events = data.get("events")
    if not isinstance(events, list):
        logger.warning("invalid_callback_payload reason=missing_events_field")
        return "Bad Request", 400

    for event in events:
        if not isinstance(event, dict):
            logger.warning("invalid_event_payload reason=event_not_dict")
            continue

        if event.get("type") != "message":
            continue

        reply_token = event.get("replyToken")
        source = event.get("source", {})
        line_user_id = source.get("userId") if isinstance(source, dict) else None
        message = event.get("message", {})
        if not isinstance(message, dict):
            logger.warning("invalid_message_payload reason=message_not_dict")
            continue

        message_type = message.get("type")
        if message_type == "text":
            text = _normalize_shortcut_text((message.get("text") or "").strip())
            logger.info("line_text_received user=%s text=%s", line_user_id, text)

            if text.startswith("天氣"):
                _handle_weather_text(text, reply_token, line_user_id)
            elif text.lower().startswith("e3"):
                _handle_e3_text(text, reply_token, line_user_id)
            elif text.lower() == "chksys":
                _handle_chksys(reply_token, line_user_id)
            elif text.lower() == "homevault":
                _handle_homevault(reply_token, line_user_id)
            else:
                logger.info("ignore_message reason=unknown_text text=%s", text)
            continue

        if message_type == "location":
            latitude = message.get("latitude")
            longitude = message.get("longitude")
            if latitude is None or longitude is None:
                logger.warning("invalid_location_payload reason=missing_coordinates")
                continue
            reply = handle_location_weather((latitude, longitude), logger)
            if reply_token:
                send_line_response(reply_token, line_user_id, reply, logger)
            else:
                logger.warning("skip_reply reason=missing_reply_token message_type=location")

    return "OK"


@app.route("/healthz", methods=["GET"])
def healthz():
    return "ok", 200


@app.route("/e3/file/<token>", methods=["GET"])
def e3_file_proxy(token):
    try:
        download = prepare_proxy_download(token)
    except FileProxySessionExpired as exc:
        return (
            _render_proxy_error_page(exc.title, exc.message, "請回到 LINE 並輸入 `e3 relogin`，再重新點一次檔案。"),
            exc.status_code,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    except FileProxyError as exc:
        return (
            _render_proxy_error_page(exc.title, exc.message),
            exc.status_code,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    except requests.RequestException:
        logger.exception("e3_file_proxy_request_failed")
        return (
            _render_proxy_error_page("E3 下載失敗", "伺服器目前無法從 E3 取得檔案，請稍後再試。"),
            502,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    upstream = download["response"]

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    response = Response(generate(), content_type=download["content_type"])
    response.headers["Content-Disposition"] = f'attachment; filename="{download["filename"]}"'
    content_length = upstream.headers.get("Content-Length")
    if content_length:
        response.headers["Content-Length"] = content_length
    return response


def _should_start_background_worker():
    if not auto_reload_enabled():
        return True
    import os

    return os.getenv("WERKZEUG_RUN_MAIN") == "true"


if _should_start_background_worker():
    start_reminder_worker(lambda user_id, payload: push_to_line(user_id, payload, logger), logger)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=port(), threaded=True, use_reloader=auto_reload_enabled())
