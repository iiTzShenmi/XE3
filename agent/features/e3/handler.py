import html
import json
import re
from datetime import datetime, timedelta, timezone
from logging import Logger
from typing import Any, Optional

from .services.client import check_status, fetch_courses, fetch_file_links, fetch_timeline_snapshot, get_cache_status, login_and_sync, make_user_key
from .services.client import clear_runtime_data
from .data import course_runtime, file_catalog
from .utils.common import (
    assignment_items as _assignment_items,
    course_name_for_display as _course_name_for_display,
    current_semester_tag as _current_semester_tag,
    discord_bold as _discord_bold,
    extract_semester_tag as _extract_semester_tag,
    format_due_at_for_display as _format_due_at_for_display,
    format_due_at_full as _format_due_at_full,
    is_assignment_completed as _is_assignment_completed,
    is_discord_user_key as _is_discord_user_key,
    matches_course_keyword as _matches_course_keyword,
    normalize_due_at as _normalize_due_at,
    parse_due_at_sort_key as _parse_due_at_sort_key,
    shorten_course_name as _shorten_course_name,
    shorten_title as _shorten_title,
)
from .views.file_views import (
    _build_file_course_bubble,
    _build_file_download_flex,
    _build_file_folder_bubble,
    _build_file_nav_bubble,
    _file_page_size,
    _payload_file_entries,
)
from .views.timeline_views import (
    _build_detail_flex,
    _build_timeline_flex,
    _event_payload,
    _event_title_for_display,
    _event_type_label_for_display,
    _filter_rows_within_days,
    _timeline_heading,
    _timeline_rows_sorted,
)
from .views.course_cards import build_course_detail_flex, build_course_summary_flex
from .views.payloads import attach_message_meta, line_response
from .data.db import (
    delete_user_data,
    ensure_reminder_prefs,
    get_event_by_uid,
    get_e3_account_by_user_id,
    get_grade_items,
    get_reminder_prefs,
    get_timeline_event_details,
    get_timeline_events,
    get_upcoming_events,
    init_db,
    mark_missing_events_inactive,
    upsert_grade_item,
    update_reminder_enabled,
    update_reminder_schedule,
    update_login_state,
    upsert_e3_account,
    upsert_event,
    upsert_user,
)
from .services.events import extract_events_from_fetch_all
from .services.grade_calculator import calculate_grade_target
from .data.file_proxy import build_proxy_url
from .services.secrets import decrypt_secret, encrypt_secret


ASYNC_ACTIONS = {"login", "relogin", "重新登入"}
_LAST_EVENT_INDEX = {}
_LAST_NEWS_INDEX = {}
EVENT_TYPE_ALIASES = {
    "作業": "homework",
    "homework": "homework",
    "hw": "homework",
    "行事曆": "calendar",
    "calendar": "calendar",
    "考試": "exam",
    "exam": "exam",
    "academic": "academic",
    "學業": "academic",
}


def _format_e3_error(exc: Exception) -> str:
    message = str(exc).strip()
    lowered = message.lower()
    if "exceeded 30 redirects" in lowered:
        return (
            "⚠️ E3 登入失敗：登入流程發生過多重新導向。\n"
            "這通常不是單純帳密錯誤，比較像 E3/SSO 暫時異常、cookie 被拒絕，或登入頁流程已改變。"
        )
    if "timeout" in lowered:
        return "⏱️ E3 登入失敗：登入頁回應逾時，請稍後再試。"
    return "⚠️ E3 登入失敗，請確認帳密、ChromeDriver 或 Selenium 環境。"


def _parse_e3_action(text: str) -> tuple[str, str, list[str]]:
    command = text.strip()
    parts = command.split(maxsplit=1)
    raw_action = parts[1].strip() if len(parts) > 1 else ""
    tokens = raw_action.split()
    verb = tokens[0].lower() if tokens else ""
    return raw_action, verb, tokens


def handle_e3_command(text: str, logger: Logger, line_user_id: Optional[str] = None) -> Any:
    init_db()

    action, verb, tokens = _parse_e3_action(text)
    action_head = tokens[0] if tokens else ""

    if not action or action.lower() in {"help", "幫助", "功能"}:
        if _is_discord_user_key(line_user_id):
            return (
                "🤖 **XE3 E3 Help**\n"
                "──────────\n"
                "📚 **Core commands**\n"
                f"• {_discord_command_hint('e3 login <帳號> <密碼>', line_user_id)}\n"
                f"• {_discord_command_hint('e3 relogin', line_user_id)}\n"
                f"• {_discord_command_hint('e3 logout', line_user_id)}\n"
                f"• {_discord_command_hint('e3 course', line_user_id)}\n"
                f"• {_discord_command_hint('e3 today', line_user_id)}\n"
                f"• {_discord_command_hint('e3 week', line_user_id)}\n"
                f"• {_discord_command_hint('e3 news', line_user_id)}\n"
                f"• {_discord_command_hint('e3 grades', line_user_id)}\n"
                f"• {_discord_command_hint('e3 成績試算 <課號或課名> <目標分數>', line_user_id)}\n"
                f"• {_discord_command_hint('e3 files <課名關鍵字>', line_user_id)}\n"
                "──────────\n"
                "🗓️ **Timeline**\n"
                f"• {_discord_command_hint('e3 upcoming [homework/exam]', line_user_id)}\n"
                f"• {_discord_command_hint('e3 timeline [homework/exam]', line_user_id)}\n"
                f"• {_discord_command_hint('e3 詳情 <編號>', line_user_id)}\n"
                "──────────\n"
                "⏰ **Reminder**\n"
                f"• {_discord_command_hint('e3 remind show', line_user_id)}\n"
                f"• {_discord_command_hint('e3 remind schedule both|morning|evening', line_user_id)}"
            )
        return (
            "📘 E3 指令：\n"
            "1) e3 login <帳號> <密碼>\n"
            "2) e3 relogin / e3 refresh\n"
            "3) e3 logout\n"
            "4) e3 課程 / e3 course\n"
            "5) e3 today / e3 今日\n"
            "6) e3 week / e3 本週\n"
            "7) e3 news / e3 公告\n"
            "8) e3 近期 [作業/行事曆/考試]\n"
            "9) e3 timeline / e3 行事曆 [作業/行事曆/考試]\n"
            "10) e3 詳情 <編號>\n"
            "11) e3 狀態\n"
            "12) e3 grades / e3 成績\n"
            "13) e3 成績試算 <課號或課名> <目標分數>\n"
            "14) e3 files <課名關鍵字>\n"
            "15) e3 remind show/on/off/schedule both|morning|evening\n"
            "說明：課程指令會顯示目前學期（例如 114上 / 114下）"
        )

    if action_head == "狀態" or verb == "status":
        return _check_e3_status(line_user_id)

    if action.startswith("課程摘要") or (verb == "course" and len(tokens) >= 3 and tokens[1].lower() in {"summary", "overview"}):
        return _course_summary(action, tokens, logger, line_user_id)

    if action.startswith("課程詳情") or (verb == "course" and len(tokens) >= 3 and tokens[1].lower() in {"detail", "details"}):
        return _course_detail(action, tokens, logger, line_user_id)

    if action.startswith("課程作業") or (verb in {"course", "courses"} and len(tokens) >= 3 and tokens[1].lower() in {"homework", "assignments"}):
        return _course_homework(action, tokens, logger, line_user_id)

    if action.startswith("作業詳情") or (verb in {"homework", "assignment", "assignments"} and len(tokens) >= 2 and tokens[1].lower() in {"detail", "details"}):
        return _course_homework_detail(action, tokens, logger, line_user_id)

    if action_head == "課程" or verb in {"course", "courses"}:
        return _list_courses(logger, line_user_id)

    if action.startswith("成績詳情") or (verb in {"grade", "grades"} and len(tokens) >= 2 and tokens[1].lower() in {"detail", "details"}):
        return _grade_detail(action, tokens, logger, line_user_id)

    if action.startswith("成績試算") or action.startswith("目標成績") or verb in {"passcalc", "gradecalc", "target"}:
        return _grade_target(action, tokens, logger, line_user_id)

    if action_head == "成績" or verb in {"grade", "grades"}:
        return _list_grades(logger, line_user_id)

    if action.startswith("公告詳情") or (verb in {"news", "forum", "forums"} and len(tokens) >= 2 and tokens[1].lower() in {"detail", "details"}):
        return _news_detail(action, tokens, logger, line_user_id)

    if action_head == "公告" or verb in {"news", "forum", "forums"}:
        return _list_news(tokens, logger, line_user_id)

    if action.startswith("檔案資料夾") or (verb in {"file", "files"} and len(tokens) >= 3 and tokens[1].lower() in {"folders", "folder"}):
        return _file_folders(action, tokens, logger, line_user_id)

    if action.startswith("檔案詳情") or (verb in {"file", "files"} and len(tokens) >= 3 and tokens[1].lower() in {"detail", "details", "download"}):
        return _file_detail(action, tokens, logger, line_user_id)

    if action_head == "檔案" or verb in {"file", "files", "materials"}:
        return _list_files(tokens, logger, line_user_id)

    if verb == "login":
        return _queue_async(action, line_user_id)

    if action in ASYNC_ACTIONS - {"login"} or verb in {"relogin", "refresh", "update"} or action_head in {"更新", "刷新"}:
        return _queue_async(action, line_user_id)

    if action_head == "登出" or verb == "logout":
        return _logout(line_user_id)

    if verb == "remind" or action_head == "提醒":
        return _handle_remind(tokens, line_user_id)

    if action_head == "作業":
        return _upcoming(["upcoming", "作業"], line_user_id)

    if action_head == "考試":
        return _upcoming(["upcoming", "考試"], line_user_id)

    if action_head == "今日" or verb == "today":
        return _today(line_user_id, logger)

    if action_head == "本週" or verb == "week":
        return _week(line_user_id, logger)

    if action_head == "近期" or verb == "upcoming":
        return _upcoming(tokens, line_user_id)

    if action_head == "行事曆":
        return _timeline(["timeline", "行事曆"], line_user_id, logger)

    if verb in {"timeline", "calendar"}:
        return _timeline(tokens, line_user_id, logger)

    if action.startswith("詳情") or verb in {"detail", "details"}:
        return _event_detail(action, tokens, line_user_id)

    return "❓ 不支援的 E3 指令，請輸入：e3 幫助"


def run_e3_async_command(text: str, logger: Logger, line_user_id: Optional[str] = None) -> str:
    init_db()

    action, verb, tokens = _parse_e3_action(text)

    if verb == "login":
        return _login(action, logger, line_user_id)

    if action in {"重新登入", "更新", "刷新"} or verb in {"relogin", "refresh", "update"}:
        return _relogin(logger, line_user_id)

    return "沒有可執行的背景 E3 任務。"


def _queue_async(action, line_user_id):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    if action.startswith("login"):
        tokens = action.split()
        if len(tokens) < 3:
            return f"請使用 {_discord_command_hint('e3 login <帳號> <密碼>', line_user_id)}。" if _is_discord_user_key(line_user_id) else "用法：e3 login <帳號> <密碼>"
        return "⏳ XE3 is signing you in and pulling your E3 home data now." if _is_discord_user_key(line_user_id) else "⏳ E3 登入已開始，正在驗證帳號並讀取首頁內容。完成後會再推播結果給你。"

    row = get_e3_account_by_user_id(user_id)
    if not row:
        return f"⚠️ I can't find a linked account yet.\nStart with {_discord_command_hint('e3 login <帳號> <密碼>', line_user_id)}." if _is_discord_user_key(line_user_id) else "找不到已綁定帳號，請先 `e3 login <帳號> <密碼>`。"
    return "⏳ XE3 started a full refresh. I’ll bring back the updated result as soon as it’s ready." if _is_discord_user_key(line_user_id) else "⏳ E3 強制更新已開始，完成後會再推播最新同步結果給你。"


def _check_e3_status(line_user_id):
    user_key = make_user_key(line_user_id) if line_user_id else None
    runtime_status = check_status(user_key=user_key)
    if not runtime_status["available"]:
        return (
            f"⚠️ **XE3 status: unavailable**\n{_discord_separator(user_key)}\n"
            f"XE3 can't find the E3 runtime at `{runtime_status['e3_root']}`."
            if _is_discord_user_key(user_key)
            else f"⚠️ E3 狀態：不可用\n找不到 E3 專案：{runtime_status['e3_root']}"
        )

    if not line_user_id:
        return "⚠️ E3 狀態：需要 LINE 使用者身分"

    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    account_row = get_e3_account_by_user_id(user_id)
    if not account_row:
        return "⚠️ E3 狀態：未綁定帳號\n請先輸入 `e3 login <帳號> <密碼>`。"

    login_status = account_row["login_status"] or "unknown"
    has_password = bool(account_row["encrypted_password"])
    has_cookie = bool(runtime_status.get("has_cookie"))
    has_courses = bool(runtime_status.get("has_courses"))
    has_home_html = bool(runtime_status.get("has_home_html"))
    user_name = runtime_status.get("user_name") or ""
    user_email = runtime_status.get("user_email") or ""
    last_error = account_row["last_error"] or ""
    reminder_prefs = get_reminder_prefs(user_id)
    reminder_enabled = bool(reminder_prefs["enabled"]) if reminder_prefs else False
    reminder_schedule = _load_reminder_schedule(reminder_prefs)

    if login_status == "ok" and has_password and (has_cookie or has_home_html or has_courses):
        headline = "🟢 E3 狀態：已登入"
    elif login_status == "error":
        headline = "⚠️ E3 狀態：登入異常"
    else:
        headline = "🟡 E3 狀態：已綁定，尚未就緒"

    if _is_discord_user_key(user_key):
        lines = [
            f"{headline}",
            _discord_separator(user_key),
            f"👤 **Account:** `{account_row['e3_account']}`",
            f"🪪 **Name:** {_discord_bold(user_name or 'Not available yet', user_key)}",
            f"📧 **Email:** {user_email or 'Not available yet'}",
            f"🔐 **Password:** {'saved' if has_password else 'not saved'}",
            f"🍪 **Session cookie:** {'ready' if has_cookie else 'missing'}",
            f"📚 **Course cache:** {'ready' if has_courses else 'missing'}",
            f"⏰ **Reminders:** {'on' if reminder_enabled else 'off'}",
            f"🕘 **Schedule:** {', '.join(reminder_schedule) if reminder_schedule else 'not set'}",
        ]
    else:
        lines = [headline]
        lines.append(f"帳號：{account_row['e3_account']}")
        lines.append(f"姓名：{user_name or '尚未取得'}")
        lines.append(f"Email：{user_email or '尚未取得'}")
        lines.append(f"密碼：{'已儲存' if has_password else '未儲存'}")
        lines.append(f"Cookie：{'可用' if has_cookie else '未找到'}")
        lines.append(f"課程快取：{'可用' if has_courses else '未找到'}")
        lines.append(f"提醒：{'開啟' if reminder_enabled else '關閉'}")
        lines.append(f"提醒時段：{', '.join(reminder_schedule) if reminder_schedule else '未設定'}")
    if last_error:
        lines.append(f"⚠️ **Last error:** {last_error}" if _is_discord_user_key(user_key) else f"最近錯誤：{last_error}")
    if not (has_password and (has_cookie or has_home_html or has_courses)):
        lines.append(
            f"💡 Try {_discord_command_hint('e3 relogin', user_key)} or sign in again with {_discord_command_hint('e3 login <帳號> <密碼>', user_key)}."
            if _is_discord_user_key(user_key)
            else "建議：輸入 `e3 relogin` 或重新 `e3 login <帳號> <密碼>`。"
        )
    return "\n".join(lines)


def _list_courses(logger, line_user_id):
    _, err = _require_line_user(line_user_id)
    if err:
        return err

    try:
        data = fetch_courses(make_user_key(line_user_id))
        file_snapshot = fetch_file_links(make_user_key(line_user_id))
        cache_status = get_cache_status(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_list_courses_failed error=%s", exc)
        return (
            f"⚠️ XE3 couldn't load your local E3 data.\nTry {_discord_command_hint('e3 login <帳號> <密碼>', line_user_id)} or {_discord_command_hint('e3 relogin', line_user_id)}."
            if _is_discord_user_key(line_user_id)
            else "E3 本地資料讀取失敗，請先 `e3 login <帳號> <密碼>` 或 `e3 relogin`。"
        )

    if not isinstance(data, dict) or not data:
        return _discord_empty_state(
            f"I can't see any course data yet. Start with {_discord_command_hint('e3 login <帳號> <密碼>', line_user_id)}.",
            line_user_id,
        ) if _is_discord_user_key(line_user_id) else "目前沒有可用課程資料，請先 `e3 login <帳號> <密碼>`。"

    semester_tag = _current_semester_tag()
    current_courses = _current_semester_courses(data, semester_tag=semester_tag)

    if not current_courses:
        return (
            _discord_empty_state(
                f"我目前還找不到 **{semester_tag}** 的課程資料。\n試試 {_discord_command_hint('e3 relogin', line_user_id)} 重新整理。",
                line_user_id,
                emoji="📚",
            )
            if _is_discord_user_key(line_user_id)
            else f"目前找不到 {semester_tag} 學期課程，請先 `e3 relogin` 重新同步。"
        )

    file_links = file_snapshot.get("file_links") or {}
    if _is_discord_user_key(line_user_id):
        text_lines = [
            f"📚 **{semester_tag} 學期課程列表**",
            "這裡是你目前同步到 XE3 的課程。",
            _discord_separator(line_user_id),
            _format_cache_status_text(cache_status),
            "",
        ]
    else:
        text_lines = [f"📚 你的 {semester_tag} 學期 E3 課程：", _format_cache_status_text(cache_status)]
    bubbles = []
    for idx, (display_name, payload) in enumerate(current_courses[:10], start=1):
        summary = course_runtime.build_course_summary(
            idx,
            display_name,
            payload,
            file_links.get(str((payload or {}).get("_course_id") or "").strip()) or {},
        )
        if _is_discord_user_key(line_user_id):
            text_lines.append(f"• **{summary['course_label']}**")
            text_lines.append(
                f"  作業 `{summary['homework_count']}` · 成績 `{summary['grade_count']}` · 檔案 `{summary['file_count']}`"
            )
            text_lines.append("")
        else:
            text_lines.append(f"{idx}. {summary['course_label']}")
            text_lines.append(f"   作業 {summary['homework_count']}｜成績 {summary['grade_count']}｜檔案 {summary['file_count']}")
        bubbles.append(_build_course_bubble(summary))

    messages = [item for item in [_build_cache_status_flex(cache_status, "課程快取")] if item]
    if bubbles:
        messages.append(
            {
                "type": "flex",
                "altText": "\n".join(text_lines),
                "contents": {
                    "type": "carousel",
                    "contents": bubbles,
                },
            }
        )
    return _line_response("\n".join(text_lines), messages=messages or None)


def _is_meaningful_grade(score):
    text = str(score or "").strip()
    return bool(text) and text != "-"


def extract_grade_items(courses):
    items = []
    if not isinstance(courses, dict):
        return items

    for display_name, payload in courses.items():
        if not isinstance(payload, dict):
            continue
        grades_payload = payload.get("grades") or {}
        course_id = str(payload.get("_course_id") or "").strip()
        course_name = _course_name_for_display(display_name)
        if isinstance(grades_payload, dict) and isinstance(grades_payload.get("grade_items"), list):
            for row in grades_payload.get("grade_items") or []:
                if not isinstance(row, dict):
                    continue
                score = row.get("score")
                if not _is_meaningful_grade(score):
                    continue
                if row.get("is_category") or row.get("is_calculated"):
                    continue
                item_text = re.sub(r"\s+", " ", str(row.get("item_name") or "").replace("\u000b", " ")).strip()
                score_text = re.sub(r"\s+", " ", str(score or "")).strip()
                if not item_text or not score_text:
                    continue
                items.append(
                    {
                        "course_id": course_id,
                        "course_name": course_name,
                        "item_name": item_text,
                        "score": score_text,
                    }
                )
            continue

        grades = grades_payload if isinstance(grades_payload, dict) else {}
        for item_name, score in grades.items():
            if not _is_meaningful_grade(score):
                continue
            item_text = re.sub(r"\s+", " ", str(item_name or "").replace("\u000b", " ")).strip()
            score_text = re.sub(r"\s+", " ", str(score or "")).strip()
            if not item_text or not score_text:
                continue
            items.append(
                {
                    "course_id": course_id,
                    "course_name": course_name,
                    "item_name": item_text,
                    "score": score_text,
                }
            )
    return items


def sync_grade_items(user_id, courses):
    existing = {
        (row["course_id"], row["item_name"]): row["score"]
        for row in get_grade_items(user_id)
    }
    changes = []
    for item in extract_grade_items(courses):
        key = (item["course_id"], item["item_name"])
        old_score = existing.get(key)
        if old_score != item["score"]:
            change = dict(item)
            change["old_score"] = old_score
            changes.append(change)
        upsert_grade_item(
            user_id,
            item["course_id"],
            item["course_name"],
            item["item_name"],
            item["score"],
        )
    return changes


def _format_grade_change_summary(changes, user_key=None):
    if not changes:
        return ""
    lines = ["📊 **New grade updates**" if _is_discord_user_key(user_key) else "📊 新成績："]
    for idx, item in enumerate(changes[:5], start=1):
        course_name = _shorten_course_name(item["course_name"], max_len=24)
        if item.get("old_score"):
            lines.append(
                f"• **{course_name}** · {item['item_name']} · **{item['old_score']} → {item['score']}**"
                if _is_discord_user_key(user_key)
                else f"{idx}. {course_name}｜{item['item_name']}：{item['old_score']} -> {item['score']}"
            )
        else:
            lines.append(
                f"• **{course_name}** · {item['item_name']} · **{item['score']}**"
                if _is_discord_user_key(user_key)
                else f"{idx}. {course_name}｜{item['item_name']}：{item['score']}"
            )
    if len(changes) > 5:
        lines.append(f"• ...and `{len(changes) - 5}` more" if _is_discord_user_key(user_key) else f"另有 {len(changes) - 5} 筆更新。")
    return "\n".join(lines)


def _list_grades(logger, line_user_id):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    try:
        data = fetch_courses(make_user_key(line_user_id))
        cache_status = get_cache_status(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_list_grades_failed error=%s", exc)
        return (
            f"⚠️ XE3 couldn't load your grade data.\nTry {_discord_command_hint('e3 relogin', line_user_id)}."
            if _is_discord_user_key(line_user_id)
            else "E3 成績資料讀取失敗，請先 `e3 relogin`。"
        )

    grade_items = extract_grade_items(data)
    if not grade_items:
        return _discord_empty_state("目前還沒有新成績，先喘口氣吧。🎉", line_user_id, emoji="📊") if _is_discord_user_key(line_user_id) else "目前沒有可用成績資料。"
    grouped = _group_grade_items_by_course(grade_items)
    if _is_discord_user_key(line_user_id):
        lines = ["📊 **成績總覽**", "先選一門課，我再展開該課的成績明細。", _format_cache_status_text(cache_status), ""]
    else:
        lines = ["📊 E3 成績：", _format_cache_status_text(cache_status)]
    bubbles = []
    for idx, course_group in enumerate(grouped[:10], start=1):
        if _is_discord_user_key(line_user_id):
            lines.append(f"**{course_group['course_label']}**")
            lines.append(f"• 已登錄 `{len(course_group['items'])}` 筆成績")
            preview = course_group["items"][:2]
            for item in preview:
                lines.append(f"• {item['item_name']} · **{item['score']}**")
            remaining = len(course_group["items"]) - len(preview)
            if remaining > 0:
                lines.append(f"• 還有 `{remaining}` 筆")
            lines.append("")
        else:
            lines.append(f"{idx}. {course_group['course_label']}")
            for item in course_group["items"][:2]:
                lines.append(f"   {item['item_name']}：{item['score']}")
            remaining = len(course_group["items"]) - 2
            if remaining > 0:
                lines.append(f"   ...另有 {remaining} 筆")
        bubbles.append(_build_grade_bubble(course_group, idx))

    messages = [item for item in [_build_cache_status_flex(cache_status, "成績快取")] if item]
    if bubbles:
        messages.append(
            {
                "type": "flex",
                "altText": "\n".join(lines),
                "contents": {
                    "type": "carousel",
                    "contents": bubbles,
                },
            }
        )
    return _line_response("\n".join(lines), messages=messages or None)


def _group_grade_items_by_course(items):
    grouped = {}
    for item in items:
        course_id = str(item.get("course_id") or "").strip()
        course_name = _course_name_for_display(item.get("course_name"))
        key = (course_id, course_name)
        grouped.setdefault(
            key,
            {
                "course_id": course_id,
                "course_name": course_name,
                "course_label": f"{course_id} {course_name}".strip(),
                "items": [],
            },
        )
        grouped[key]["items"].append(
            {
                "item_name": _shorten_title(item.get("item_name"), max_len=28),
                "score": str(item.get("score") or "").strip(),
            }
        )
    ordered = list(grouped.values())
    ordered.sort(key=lambda row: (row["course_id"], row["course_name"]))
    return ordered


def _build_grade_bubble(course_group, index):
    preview_items = course_group["items"][:4]
    body_contents = [
        {"type": "text", "text": course_group["course_id"] or "未提供課號", "size": "sm", "color": "#475569"},
        {"type": "text", "text": f"共 {len(course_group['items'])} 筆成績", "size": "sm", "color": "#334155"},
    ]
    for item in preview_items:
        body_contents.append(
            {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "text",
                        "text": item["item_name"],
                        "size": "sm",
                        "color": "#0F172A",
                        "flex": 4,
                        "wrap": True,
                    },
                    {
                        "type": "text",
                        "text": item["score"],
                        "size": "sm",
                        "color": "#1D4ED8",
                        "weight": "bold",
                        "flex": 2,
                        "align": "end",
                        "wrap": True,
                    },
                ],
            }
        )
    remaining = len(course_group["items"]) - len(preview_items)
    if remaining > 0:
        body_contents.append(
            {
                "type": "text",
                "text": f"...另有 {remaining} 筆成績",
                "size": "xs",
                "color": "#64748B",
                "wrap": True,
            }
        )

    return attach_message_meta({
        "type": "bubble",
        "size": "kilo",
        "xe3_meta": {
            "selector_kind": "grade_course",
            "selector_summary_title": "選擇課程成績",
            "selector_section": "📊 課程成績",
            "item_title": course_group["course_name"],
            "course_name": course_group["course_name"],
            "course_id": course_group["course_id"],
        },
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#7C3AED",
            "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": "成績", "color": "#EDE9FE", "size": "xs"},
                {"type": "text", "text": course_group["course_name"], "color": "#FFFFFF", "weight": "bold", "wrap": True},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": body_contents,
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "color": "#7C3AED",
                    "action": {
                        "type": "message",
                        "label": "查看成績",
                        "text": f"e3 成績詳情 {course_group['course_id'] or course_group['course_name']}",
                        "xe3_meta": {
                            "selector_kind": "grade_course_detail",
                            "entry_kind": "grade_course",
                            "group_label": "查看成績",
                            "item_title": course_group["course_name"],
                            "course_name": course_group["course_name"],
                            "course_id": course_group["course_id"],
                            "option_description": f"已登錄 {len(course_group['items'])} 筆成績",
                        },
                    },
                },
            ],
        },
    })


def _grade_detail(action, tokens, logger, line_user_id):
    target = _extract_course_target(action, tokens, "成績詳情", "detail")
    if not target:
        return f"請使用 {_discord_command_hint('e3 成績詳情 <課號或課名>', line_user_id)}。" if _is_discord_user_key(line_user_id) else "用法：e3 成績詳情 <課號或課名>"

    try:
        data = fetch_courses(make_user_key(line_user_id))
        cache_status = get_cache_status(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_grade_detail_failed error=%s", exc)
        return (
            f"⚠️ XE3 couldn't load that course's grade data.\nTry {_discord_command_hint('e3 relogin', line_user_id)}."
            if _is_discord_user_key(line_user_id)
            else "E3 成績資料讀取失敗，請先 `e3 relogin`。"
        )

    semester_tag = _current_semester_tag()
    matched_group = None
    for display_name, payload in _current_semester_courses(data, semester_tag=semester_tag):
        course_id = str((payload or {}).get("_course_id") or "").strip()
        course_name = _course_name_for_display(display_name)
        searchable = f"{course_id} {course_name}"
        if not _matches_course_keyword(searchable, target):
            continue
        items = [item for item in extract_grade_items({display_name: payload}) if _is_meaningful_grade(item.get("score"))]
        if not items:
            matched_group = {
                "course_id": course_id,
                "course_name": course_name,
                "course_label": f"{course_id} {course_name}".strip(),
                "items": [],
            }
        else:
            matched_group = _group_grade_items_by_course(items)[0]
        break

    if matched_group is None:
        return (
            f"📊 我找不到和 **{target}** 對應的課程成績。"
            if _is_discord_user_key(line_user_id)
            else f"找不到「{target}」的課程成績。"
        )

    if not matched_group["items"]:
        return _discord_empty_state(f"**{matched_group['course_name']}** 目前還沒有可顯示的成績。", line_user_id, emoji="📊") if _is_discord_user_key(line_user_id) else f"{matched_group['course_name']} 目前沒有可用成績資料。"

    if _is_discord_user_key(line_user_id):
        lines = [
            "📊 **課程成績詳情**",
            f"**{matched_group['course_label']}**",
            _format_cache_status_text(cache_status),
            "",
        ]
        for item in matched_group["items"]:
            lines.append(f"• **{item['item_name']}**")
            lines.append(f"  分數：**{item['score']}**")
            lines.append("")
    else:
        lines = [f"📊 {matched_group['course_label']} 成績：", _format_cache_status_text(cache_status)]
        for item in matched_group["items"]:
            lines.append(f"- {item['item_name']}：{item['score']}")

    flex = attach_message_meta(
        {
            "type": "flex",
            "altText": "\n".join(lines),
            "contents": {
                "type": "bubble",
                "xe3_meta": {
                    "selector_kind": "grade_course_detail",
                    "item_title": matched_group["course_name"],
                    "course_name": matched_group["course_name"],
                    "course_id": matched_group["course_id"],
                },
                "size": "mega",
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#7C3AED",
                    "paddingAll": "12px",
                    "contents": [
                        {"type": "text", "text": "課程成績詳情", "color": "#FFFFFF", "weight": "bold", "size": "md"},
                    ],
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "contents": (
                        [
                            {"type": "text", "text": matched_group["course_name"], "weight": "bold", "wrap": True, "size": "lg"},
                            {"type": "text", "text": matched_group["course_id"] or "未提供課號", "size": "sm", "color": "#475569"},
                            {"type": "separator", "margin": "md"},
                        ]
                        + [
                            {
                                "type": "box",
                                "layout": "baseline",
                                "spacing": "sm",
                                "contents": [
                                    {"type": "text", "text": item["item_name"], "size": "sm", "wrap": True, "flex": 4, "color": "#0F172A"},
                                    {"type": "text", "text": item["score"], "size": "sm", "wrap": True, "flex": 2, "align": "end", "weight": "bold", "color": "#1D4ED8"},
                                ],
                            }
                            for item in matched_group["items"]
                        ]
                    ),
                },
                "footer": {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "height": "sm",
                            "color": "#7C3AED",
                            "action": {"type": "message", "label": "回到成績列表", "text": "e3 grades"},
                        },
                    ],
                },
            },
        }
    )
    messages = [item for item in [_build_cache_status_flex(cache_status, "成績快取")] if item]
    messages.append(flex)
    return _line_response("\n".join(lines).strip(), messages=messages or None)


def _parse_grade_target_request(action, tokens):
    if not tokens:
        return "", None

    raw_tokens = list(tokens)
    if raw_tokens and raw_tokens[0] in {"成績試算", "目標成績"}:
        raw_tokens = raw_tokens[1:]
    elif raw_tokens and raw_tokens[0].lower() in {"passcalc", "gradecalc", "target"}:
        raw_tokens = raw_tokens[1:]

    if len(raw_tokens) < 2:
        match = re.match(r"^(?:成績試算|目標成績)\s+(.+?)\s+(-?\d+(?:\.\d+)?)\s*$", action.strip())
        if not match:
            return "", None
        course_target = match.group(1).strip()
        try:
            return course_target, float(match.group(2))
        except ValueError:
            return course_target, None

    target_token = str(raw_tokens[-1] or "").strip()
    course_target = " ".join(str(part or "").strip() for part in raw_tokens[:-1]).strip()
    try:
        target_grade = float(target_token)
    except ValueError:
        target_grade = None
    return course_target, target_grade


def _format_grade_target_result(result, *, cache_status, line_user_id):
    course_label = f"{result.get('course_id') or ''} {result.get('course_name') or ''}".strip()
    title = "目標成績試算"

    if not result.get("ok"):
        if result.get("reason") == "no_weight_data":
            text = (
                f"📊 **{title}**\n"
                f"**{course_label or '這門課'}**\n\n"
                "目前抓到的 E3 成績資料沒有足夠的配分資訊，XE3 暫時沒辦法可靠試算。\n"
                "通常是老師只放了分數，還沒填權重或配分。"
            )
        elif result.get("reason") == "invalid_weight_data":
            text = (
                f"📊 **{title}**\n"
                f"**{course_label or '這門課'}**\n\n"
                "目前這門課的配分資料看起來不合理，已知配分總和超過 100%，XE3 先不幫你硬算，避免誤導。"
            )
        else:
            text = f"📊 **{title}**\n目前暫時無法完成試算。"
        text = f"{text}\n\n{_format_cache_status_text(cache_status)}"
        messages = [
            _build_text_summary_flex(title, text, color="#7C3AED", alt_text=text),
            *[item for item in [_build_cache_status_flex(cache_status, '成績快取')] if item],
        ]
        return _line_response(text, messages=messages)

    target_grade = float(result.get("target_grade") or 0.0)
    total_weight = float(result.get("total_weight") or 0.0)
    completed_weight = float(result.get("completed_weight") or 0.0)
    remaining_weight = float(result.get("remaining_weight") or 0.0)
    earned_weighted = float(result.get("earned_weighted") or 0.0)
    required_average = result.get("required_average")
    status = str(result.get("status") or "").strip()
    known_weight_note = ""
    assumption_note = ""
    if total_weight < 99.5:
        known_weight_note = f"⚠️ 目前只辨識到 **{total_weight:.1f}%** 配分，以下結果屬於估算。"
    assumed_weight_count = int(result.get("assumed_weight_count") or 0)
    assumed_weight_each = result.get("assumed_weight_each")
    explicit_weight_total = float(result.get("explicit_weight_total") or 0.0)
    assumption_mode = str(result.get("assumption_mode") or "").strip()
    if assumed_weight_count > 0 and assumed_weight_each is not None:
        if assumption_mode == "all_equal":
            assumption_note = (
                f"🧩 這門課有 **{assumed_weight_count}** 個項目沒有標配分，"
                f"XE3 先假設平均分攤：**100 ÷ {assumed_weight_count} = {float(assumed_weight_each):.2f}%**。"
            )
        elif assumption_mode == "remaining_equal":
            remaining_pool = max(0.0, 100.0 - explicit_weight_total)
            assumption_note = (
                f"🧩 已知配分先保留 **{explicit_weight_total:.1f}%**，"
                f"剩下 **{remaining_pool:.1f}%** 平均分給 **{assumed_weight_count}** 個未標配分項目，"
                f"每項先估 **{float(assumed_weight_each):.2f}%**。"
            )

    lines = [
        f"📊 **{title}**",
        f"**{course_label or '未命名課程'}**",
        "",
        f"🎯 目標總成績：**{target_grade:.1f}**",
        f"📌 已辨識配分：**{total_weight:.1f}%**",
        f"✅ 已完成配分：**{completed_weight:.1f}%**",
        f"🕓 剩餘配分：**{remaining_weight:.1f}%**",
        f"📈 目前已拿到加權：**{earned_weighted:.2f} 分**",
    ]

    if known_weight_note:
        lines.extend(["", known_weight_note])
    if assumption_note:
        lines.extend(["", assumption_note])

    if status == "already_reached":
        lines.extend(["", "🎉 依目前已知配分來看，你已經達到這個目標了。"])
    elif status == "impossible":
        lines.extend(
            [
                "",
                f"🚨 依目前已知配分來看，後續平均需要 **{float(required_average or 0.0):.1f} / 100**。",
                "這代表只靠剩餘項目已經無法達標，除非目前配分資料還不完整。",
            ]
        )
    elif status == "complete":
        lines.extend(["", "📦 目前已經沒有剩餘配分項目可試算。"])
    else:
        lines.extend(["", f"🧮 接下來平均需要：**{float(required_average or 0.0):.1f} / 100**"])
        per_item_targets = list(result.get("per_item_targets") or [])
        if per_item_targets:
            lines.extend(["", "━━━━━━━━━━━━", "📝 剩餘項目估算", "━━━━━━━━━━━━"])
            for item in per_item_targets[:6]:
                item_name = _shorten_title(item.get("item_name"), max_len=36)
                needed_score = float(item.get("needed_score") or 0.0)
                range_max = float(item.get("range_max") or 0.0)
                weight = float(item.get("weight") or 0.0)
                lines.append(f"• **{item_name}**")
                lines.append(f"  目標：**{needed_score:.1f} / {range_max:.1f}** ｜配分 {weight:.1f}%")
            remaining = len(per_item_targets) - min(len(per_item_targets), 6)
            if remaining > 0:
                lines.append(f"  還有 {remaining} 個項目未展開。")

    lines.extend(["", _format_cache_status_text(cache_status)])
    text = "\n".join(lines)
    messages = [
        _build_text_summary_flex(title, text, color="#7C3AED", alt_text=text),
        *[item for item in [_build_cache_status_flex(cache_status, "成績快取")] if item],
    ]
    return _line_response(text, messages=messages)


def _grade_target(action, tokens, logger, line_user_id):
    target, target_grade = _parse_grade_target_request(action, tokens)
    if not target or target_grade is None:
        hint = _discord_command_hint("e3 成績試算 <課號或課名> <目標分數>", line_user_id)
        return f"請使用 {hint}。" if _is_discord_user_key(line_user_id) else "用法：e3 成績試算 <課號或課名> <目標分數>"
    if target_grade <= 0 or target_grade > 100:
        return "目標分數請輸入 0 到 100 之間的數字。"

    try:
        data = fetch_courses(make_user_key(line_user_id))
        cache_status = get_cache_status(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_grade_target_failed error=%s", exc)
        return (
            f"⚠️ XE3 暫時讀不到成績資料。\n先試試 {_discord_command_hint('e3 relogin', line_user_id)}。"
            if _is_discord_user_key(line_user_id)
            else "E3 成績資料讀取失敗，請先 `e3 relogin`。"
        )

    semester_tag = _current_semester_tag()
    matched_payload = None
    for display_name, payload in _current_semester_courses(data, semester_tag=semester_tag):
        course_id = str((payload or {}).get("_course_id") or "").strip()
        course_name = _course_name_for_display(display_name)
        searchable = f"{course_id} {course_name}"
        if _matches_course_keyword(searchable, target):
            matched_payload = dict(payload or {})
            matched_payload.setdefault("_course_id", course_id)
            matched_payload.setdefault("_folder_name", display_name)
            matched_payload.setdefault("course_name", course_name)
            break

    if matched_payload is None:
        return (
            f"📊 我找不到和 **{target}** 對應的課程。"
            if _is_discord_user_key(line_user_id)
            else f"找不到「{target}」對應的課程。"
        )

    result = calculate_grade_target(matched_payload, target_grade)
    return _format_grade_target_result(result, cache_status=cache_status, line_user_id=line_user_id)


def _normalize_news_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = text.replace("\u000b", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_news_sort_key(*values: str) -> datetime:
    current_year = datetime.now(timezone(timedelta(hours=8))).year
    patterns = [
        re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日.*?(\d{1,2}):(\d{2})"),
        re.compile(r"(\d{1,2})月\s*(\d{1,2})日,?\s*(\d{1,2}):(\d{2})"),
    ]
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        for pattern in patterns:
            match = pattern.search(text)
            if not match:
                continue
            try:
                groups = match.groups()
                if len(groups) == 5:
                    year, month, day, hour, minute = groups
                else:
                    month, day, hour, minute = groups
                    year = str(current_year)
                return datetime(int(year), int(month), int(day), int(hour), int(minute), tzinfo=timezone(timedelta(hours=8)))
            except ValueError:
                continue
    return datetime.min.replace(tzinfo=timezone.utc)


def _store_last_news_index(line_user_id, items):
    if not line_user_id:
        return
    _LAST_NEWS_INDEX[line_user_id] = {
        idx: dict(item)
        for idx, item in items
    }


def _extract_news_target(action, tokens):
    if tokens and tokens[0] == "公告詳情":
        return " ".join(tokens[1:]).strip()
    if len(tokens) >= 3 and tokens[0].lower() in {"news", "forum", "forums"} and tokens[1].lower() in {"detail", "details"}:
        return " ".join(tokens[2:]).strip()
    match = re.match(r"^公告詳情\s*(.+)$", action.strip())
    if match:
        return match.group(1).strip()
    return ""


def _parse_news_filters(tokens):
    course_keyword = ""
    recent_days = None
    rest = [str(token or "").strip() for token in tokens[1:] if str(token or "").strip()]
    course_parts: list[str] = []
    idx = 0
    while idx < len(rest):
        token = rest[idx].lower()
        if token in {"recent", "近期"}:
            recent_days = 7
            if idx + 1 < len(rest):
                try:
                    parsed = int(rest[idx + 1])
                except ValueError:
                    parsed = None
                if parsed and parsed > 0:
                    recent_days = parsed
                    idx += 1
            idx += 1
            continue
        if token in {"course", "課程"}:
            idx += 1
            while idx < len(rest):
                lookahead = rest[idx].lower()
                if lookahead in {"recent", "近期"}:
                    idx -= 1
                    break
                course_parts.append(rest[idx])
                idx += 1
            idx += 1
            continue
        course_parts.append(rest[idx])
        idx += 1
    course_keyword = " ".join(course_parts).strip()
    return course_keyword, recent_days


def _collect_news_items(courses):
    semester_tag = _current_semester_tag()
    items = []
    seen = set()
    for display_name, payload in _current_semester_courses(courses, semester_tag=semester_tag):
        if not isinstance(payload, dict):
            continue
        course_id = str(payload.get("_course_id") or "").strip()
        course_name = _course_name_for_display(display_name)

        for entry in payload.get("news") or []:
            if not isinstance(entry, dict):
                continue
            title = _normalize_news_text(entry.get("title") or "未命名公告")
            content = _normalize_news_text(entry.get("content") or "")
            excerpt = _shorten_title(content, 120)
            key = (course_id, title.lower(), excerpt.lower())
            if key in seen:
                continue
            seen.add(key)
            attachments = [item for item in (entry.get("attachments") or []) if isinstance(item, dict)]
            items.append(
                {
                    "source_type": "news",
                    "course_id": course_id,
                    "course_name": course_name,
                    "title": title,
                    "author": _normalize_news_text(entry.get("author") or ""),
                    "time_text": _normalize_news_text(entry.get("date") or ""),
                    "content": content,
                    "excerpt": excerpt,
                    "attachments": attachments,
                    "url": str(entry.get("url") or "").strip(),
                    "sort_key": _parse_news_sort_key(entry.get("author"), entry.get("date")),
                }
            )

        forums_payload = payload.get("forums") or {}
        forums = forums_payload.get("forums") if isinstance(forums_payload, dict) else []
        for forum in forums or []:
            if not isinstance(forum, dict):
                continue
            forum_title = _normalize_news_text(forum.get("title") or "公告")
            for discussion in forum.get("discussions") or []:
                if not isinstance(discussion, dict):
                    continue
                posts = [post for post in (discussion.get("posts") or []) if isinstance(post, dict)]
                first_post = posts[0] if posts else {}
                title = _normalize_news_text(discussion.get("title") or forum_title or "未命名公告")
                content = _normalize_news_text(first_post.get("content") or "")
                excerpt = _shorten_title(content, 120)
                key = (course_id, title.lower(), excerpt.lower())
                if key in seen:
                    continue
                seen.add(key)
                attachments = [item for item in (first_post.get("attachments") or []) if isinstance(item, dict)]
                items.append(
                    {
                        "source_type": "forum",
                        "course_id": course_id,
                        "course_name": course_name,
                        "title": title,
                        "author": _normalize_news_text(first_post.get("author") or ""),
                        "time_text": _normalize_news_text(first_post.get("time") or ""),
                        "content": content,
                        "excerpt": excerpt,
                        "attachments": attachments,
                        "url": str(discussion.get("url") or forum.get("url") or "").strip(),
                        "forum_title": forum_title,
                        "sort_key": _parse_news_sort_key(first_post.get("time")),
                    }
                )

    items.sort(
        key=lambda item: (
            item.get("sort_key") or datetime.min.replace(tzinfo=timezone.utc),
            item.get("course_name") or "",
            item.get("title") or "",
        ),
        reverse=True,
    )
    return items


def _filter_news_items(items, course_keyword="", recent_days=None):
    filtered = list(items)
    if course_keyword:
        filtered = [
            item
            for item in filtered
            if _matches_course_keyword(f"{item.get('course_id') or ''} {item.get('course_name') or ''}", course_keyword)
        ]
    if recent_days:
        now_local = datetime.now(timezone(timedelta(hours=8)))
        cutoff = now_local - timedelta(days=recent_days)
        filtered = [
            item
            for item in filtered
            if isinstance(item.get("sort_key"), datetime) and item["sort_key"] >= cutoff
        ]
    return filtered


def _news_command_with_filters(course_keyword="", recent_days=None):
    parts = ["e3", "news"]
    if recent_days:
        parts.extend(["recent", str(recent_days)])
    if course_keyword:
        parts.extend(["course", course_keyword])
    return " ".join(parts)


def _build_news_attachment_entries(item, line_user_id, parent_command=""):
    if not line_user_id:
        return []
    entries = []
    for attachment in item.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        source_url = str(attachment.get("url") or "").strip()
        title = _normalize_news_text(attachment.get("name") or "公告附件")
        if not source_url:
            continue
        entries.append(
            {
                "kind": "老師附件",
                "course_name": item.get("course_name") or item.get("title") or "公告附件",
                "title": title,
                "url": build_proxy_url(line_user_id, source_url, filename=title),
                "accent": "#0F766E",
                "parent_command": parent_command,
            }
        )
    return entries


def _build_news_bubble(item, index, *, back_command="e3 news"):
    course_label = f"{item['course_id']} {item['course_name']}".strip()
    source_label = "課程公告" if item.get("source_type") == "news" else "討論區"
    time_label = item.get("time_text") or "未提供時間"
    excerpt = item.get("excerpt") or "沒有更多摘要內容。"
    attachment_count = len(item.get("attachments") or [])
    attachment_text = f"｜附件 {attachment_count}" if attachment_count else ""
    return attach_message_meta(
        {
            "type": "bubble",
            "size": "kilo",
            "xe3_meta": {
                "selector_kind": "news_item",
                "selector_summary_title": "選擇公告",
                "selector_section": "📰 公告",
                "selector_back_command": back_command,
                "item_title": item["title"],
                "course_name": item["course_name"],
                "course_id": item["course_id"],
                "option_description": f"{course_label}｜{time_label}"[:100],
            },
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#0F766E",
                "paddingAll": "12px",
                "contents": [
                    {"type": "text", "text": source_label, "color": "#CCFBF1", "size": "xs"},
                    {"type": "text", "text": item["title"], "color": "#FFFFFF", "weight": "bold", "wrap": True},
                ],
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {"type": "text", "text": course_label, "size": "sm", "weight": "bold", "wrap": True, "color": "#0F172A"},
                    {"type": "text", "text": f"🕒 {time_label}{attachment_text}", "size": "xs", "wrap": True, "color": "#475569"},
                    {"type": "separator", "margin": "sm"},
                    {"type": "text", "text": excerpt, "size": "sm", "wrap": True, "color": "#334155"},
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "style": "primary",
                        "height": "sm",
                        "color": "#0F766E",
                        "action": {
                            "type": "message",
                            "label": "查看公告",
                            "text": f"e3 公告詳情 n{index}",
                            "xe3_meta": {
                                "selector_summary_title": "選擇公告",
                                "selector_section": "📰 公告",
                                "selector_back_command": back_command,
                                "selector_kind": "news_detail",
                                "entry_kind": "news_item",
                            "group_label": "查看公告",
                                "item_title": item["title"],
                                "course_name": item["course_name"],
                                "course_id": item["course_id"],
                                "option_description": f"{course_label}｜{time_label}"[:100],
                            },
                        },
                    }
                ],
            },
        }
    )


def _build_news_detail_flex(item):
    course_label = f"{item['course_id']} {item['course_name']}".strip()
    source_label = "課程公告" if item.get("source_type") == "news" else "討論區"
    author = item.get("author") or "未提供作者"
    time_text = item.get("time_text") or "未提供時間"
    content = item.get("content") or "這則公告目前沒有可顯示的內文。"
    attachments = item.get("attachments") or []
    body_contents = [
        {"type": "text", "text": item["title"], "weight": "bold", "wrap": True, "size": "lg", "color": "#0F172A"},
        {"type": "text", "text": course_label, "size": "sm", "wrap": True, "color": "#334155"},
        {"type": "text", "text": f"🗂️ 來源｜{source_label}", "size": "xs", "wrap": True, "color": "#475569"},
        {"type": "text", "text": f"👤 發布者｜{author}", "size": "xs", "wrap": True, "color": "#475569"},
        {"type": "text", "text": f"🕒 時間｜{time_text}", "size": "xs", "wrap": True, "color": "#475569"},
        {"type": "separator", "margin": "md"},
        {"type": "text", "text": "內容摘要", "weight": "bold", "size": "sm", "color": "#0F172A"},
        {"type": "text", "text": _shorten_title(content, 900), "size": "sm", "wrap": True, "color": "#334155"},
    ]
    if attachments:
        body_contents.extend(
            [
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "附件", "weight": "bold", "size": "sm", "color": "#0F172A"},
            ]
            + [
                {"type": "text", "text": f"📎 {_normalize_news_text(attachment.get('name') or '未命名附件')}", "size": "sm", "wrap": True, "color": "#334155"}
                for attachment in attachments[:5]
            ]
        )
        if len(attachments) > 5:
            body_contents.append({"type": "text", "text": f"還有 {len(attachments) - 5} 個附件會在下一步接上下載入口。", "size": "xs", "wrap": True, "color": "#64748B"})
    footer_contents = [
        {
            "type": "button",
            "style": "primary",
            "height": "sm",
            "color": "#0F766E",
            "action": {"type": "message", "label": "回到公告列表", "text": "e3 news"},
        }
    ]
    if item.get("url"):
        footer_contents.append(
            {
                "type": "button",
                "style": "secondary",
                "height": "sm",
                "action": {"type": "uri", "label": "開啟 E3", "uri": item["url"]},
            }
        )
    return attach_message_meta(
        {
            "type": "flex",
            "altText": f"📰 {item['title']}",
            "contents": {
                "type": "bubble",
                "size": "mega",
                "xe3_meta": {
                    "selector_kind": "news_detail",
                    "item_title": item["title"],
                    "course_name": item["course_name"],
                    "course_id": item["course_id"],
                },
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#0F766E",
                    "paddingAll": "12px",
                    "contents": [
                        {"type": "text", "text": "公告詳情", "color": "#FFFFFF", "weight": "bold", "size": "md"},
                    ],
                },
                "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body_contents},
                "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": footer_contents},
            },
        }
    )


def _list_news(tokens, logger, line_user_id):
    _, err = _require_line_user(line_user_id)
    if err:
        return err

    try:
        data = fetch_courses(make_user_key(line_user_id))
        cache_status = get_cache_status(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_list_news_failed error=%s", exc)
        return (
            f"⚠️ XE3 目前讀不到公告資料。\n先試試 {_discord_command_hint('e3 relogin', line_user_id)}。"
            if _is_discord_user_key(line_user_id)
            else "E3 公告資料讀取失敗，請先 `e3 relogin`。"
        )

    course_keyword, recent_days = _parse_news_filters(tokens)
    items = _filter_news_items(_collect_news_items(data), course_keyword=course_keyword, recent_days=recent_days)
    if not items:
        filter_note = []
        if course_keyword:
            filter_note.append(f"課程：{course_keyword}")
        if recent_days:
            filter_note.append(f"最近 {recent_days} 天")
        suffix = f"（{'｜'.join(filter_note)}）" if filter_note else ""
        text = f"📰 目前還沒有可顯示的近期公告{suffix}。" if _is_discord_user_key(line_user_id) else f"目前沒有可顯示的近期公告{suffix}。"
        flex = _build_text_summary_flex("近期公告", text, color="#0F766E", alt_text=text)
        return _line_response(text, messages=[flex] if flex else None)

    back_command = _news_command_with_filters(course_keyword=course_keyword, recent_days=recent_days)
    visible = list(enumerate(items[:10], start=1))
    _store_last_news_index(line_user_id, visible)

    heading = "📰 **近期公告**"
    if course_keyword:
        heading += f"\n📚 課程篩選：**{course_keyword}**"
    if recent_days:
        heading += f"\n🕒 範圍：最近 **{recent_days}** 天"

    if _is_discord_user_key(line_user_id):
        lines = [
            heading,
            "請從下方下拉選單挑一則，我會直接幫你打開，不洗版。",
            "",
        ]
        for idx, item in visible:
            course_label = f"{item['course_id']} {item['course_name']}".strip()
            lines.append(f"{idx}. **{item['title']}**")
            lines.append(f"   {course_label}")
            if item.get("time_text"):
                lines.append(f"   {item['time_text']}")
            lines.append("")
    else:
        lines = ["📰 近期公告：", *( [f"課程篩選：{course_keyword}"] if course_keyword else [] ), *( [f"範圍：最近 {recent_days} 天"] if recent_days else [] ), ""]
        for idx, item in visible:
            lines.append(f"{idx}. {item['title']}｜{item['course_name']}")
            if item.get("time_text"):
                lines.append(f"   {item['time_text']}")
            lines.append("")
    if len(items) > 10:
        lines.append(f"另有 {len(items) - 10} 則公告，先用篩選縮小範圍會更好讀。")
    text = "\n".join(lines).strip()

    messages = [item for item in [_build_cache_status_flex(cache_status, "公告快取")] if item]
    messages.append(
        {
                "type": "flex",
                "altText": text,
                "contents": {
                    "type": "carousel",
                    "contents": [_build_news_bubble(item, idx, back_command=back_command) for idx, item in visible],
                },
            }
        )
    return _line_response(text, messages=messages or None)


def _news_detail(action, tokens, logger, line_user_id):
    target = _extract_news_target(action, tokens)
    if not target:
        return f"請使用 {_discord_command_hint('e3 公告詳情 <編號>', line_user_id)}。" if _is_discord_user_key(line_user_id) else "用法：e3 公告詳情 <編號>"

    item = None
    if re.fullmatch(r"n\d+", target.lower()):
        try:
            index = int(target[1:])
        except ValueError:
            index = 0
        item = (_LAST_NEWS_INDEX.get(line_user_id) or {}).get(index)
    elif target.isdigit():
        item = (_LAST_NEWS_INDEX.get(line_user_id) or {}).get(int(target))

    if item is None:
        try:
            data = fetch_courses(make_user_key(line_user_id))
        except Exception as exc:
            logger.error("e3_news_detail_reload_failed error=%s", exc)
            data = {}
        items = _collect_news_items(data)
        if re.fullmatch(r"n\d+", target.lower()):
            idx = int(target[1:])
            if 1 <= idx <= len(items):
                item = items[idx - 1]

    if item is None:
        return (
            f"📰 我找不到這則公告。\n先回到 {_discord_command_hint('e3 news', line_user_id)} 重新選一次。"
            if _is_discord_user_key(line_user_id)
            else "找不到這則公告，請先重新執行 `e3 news`。"
        )

    author = item.get("author") or "未提供作者"
    time_text = item.get("time_text") or "未提供時間"
    course_label = f"{item['course_id']} {item['course_name']}".strip()
    content = item.get("content") or "這則公告目前沒有可顯示的內文。"
    attachments = item.get("attachments") or []
    lines = [
        "📰 **公告詳情**",
        f"**{item['title']}**",
        course_label,
        f"👤 發布者｜{author}",
        f"🕒 時間｜{time_text}",
        "",
        "內容摘要",
        _shorten_title(content, 500),
    ]
    if attachments:
        lines.extend(["", "附件"] + [f"• {_normalize_news_text(attachment.get('name') or '未命名附件')}" for attachment in attachments[:5]])
        if len(attachments) > 5:
            lines.append(f"• 還有 {len(attachments) - 5} 個附件")
    flex = _build_news_detail_flex(item)
    attachment_entries = _build_news_attachment_entries(item, line_user_id, parent_command=f"e3 公告詳情 {target}")
    attachment_flex = _build_file_download_flex(
        attachment_entries,
        f"📎 {item['title']} 附件",
        item["title"],
    ) if attachment_entries else None
    messages = [item for item in [flex, attachment_flex] if item]
    return _line_response("\n".join(lines).strip(), messages=messages or None)


def _current_semester_courses(courses, semester_tag=None):
    semester_tag = semester_tag or _current_semester_tag()
    filtered = []
    for display_name, payload in (courses or {}).items():
        if _extract_semester_tag(display_name) != semester_tag:
            continue
        filtered.append((display_name, payload))
    filtered.sort(
        key=lambda item: (
            str((item[1] or {}).get("_course_id") or "").strip(),
            _course_name_for_display(item[0]),
        )
    )
    return filtered


def _filter_active_homework_rows(rows, courses):
    active_pairs = set()
    if isinstance(courses, dict):
        for display_name, payload in courses.items():
            if not isinstance(payload, dict):
                continue
            course_id = str(payload.get("_course_id") or "").strip()
            for item in _assignment_items(payload):
                if not isinstance(item, dict):
                    continue
                category = str(item.get("category") or "").strip().lower()
                if category not in {"in_progress", "upcoming"}:
                    continue
                if _is_assignment_completed(item):
                    continue
                title = re.sub(r"\s+", " ", str(item.get("title") or "").strip())
                if course_id and title:
                    active_pairs.add((course_id, title))

    if not active_pairs:
        return [row for row in rows if row["event_type"] != "homework"]

    filtered = []
    for row in rows:
        if row["event_type"] != "homework":
            filtered.append(row)
            continue
        title = re.sub(r"\s+", " ", str(row["title"] or "").strip())
        course_id = str(row["course_id"] or "").strip()
        if (course_id, title) in active_pairs:
            filtered.append(row)
    return filtered


def _list_files(tokens, logger, line_user_id):
    _, err = _require_line_user(line_user_id)
    if err:
        return err

    keyword = " ".join(tokens[1:]).strip() if len(tokens) >= 2 else ""
    if not keyword:
        return f"請使用 {_discord_command_hint('e3 files <課名關鍵字>', line_user_id)}。" if _is_discord_user_key(line_user_id) else "用法：e3 files <課名關鍵字>"

    try:
        snapshot = fetch_file_links(make_user_key(line_user_id))
        cache_status = get_cache_status(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_list_files_failed error=%s", exc)
        return (
            f"⚠️ XE3 couldn't load your file index.\nTry {_discord_command_hint('e3 relogin', line_user_id)}."
            if _is_discord_user_key(line_user_id)
            else "E3 檔案資料讀取失敗，請先 `e3 relogin`。"
        )

    courses = snapshot.get("courses") or {}
    file_links = snapshot.get("file_links") or {}
    semester_tag = _current_semester_tag()
    matches = []

    for display_name, payload in _current_semester_courses(courses, semester_tag=semester_tag):
        course_id = str((payload or {}).get("_course_id") or "").strip()
        course_name = _course_name_for_display(display_name)
        searchable = f"{course_id} {course_name}"
        if not _matches_course_keyword(searchable, keyword):
            continue
        links = file_links.get(course_id) or {}
        matches.append((course_id, course_name, links))

    if not matches:
        return (
            f"📎 我找不到和 **{keyword}** 相符的教材。\n如果這門課是最近才加入的，可以試試 {_discord_command_hint('e3 relogin', line_user_id)}。"
            if _is_discord_user_key(line_user_id)
            else f"找不到包含「{keyword}」的課程檔案，請先 `e3 relogin` 更新資料。"
        )

    lines = [f"📎 **Materials related to {keyword}**", _format_cache_status_text(cache_status), ""] if _is_discord_user_key(line_user_id) else [f"📎 與「{keyword}」相關的課程檔案：", _format_cache_status_text(cache_status)]
    bubbles = []
    for course_id, course_name, links in matches[:5]:
        all_files = file_catalog.collect_file_entries(course_id, course_name, links)
        folder_groups = file_catalog.group_file_entries(all_files)
        preview_lines = [f"{folder}｜{len(items)} 個檔案" for folder, items in folder_groups[:3]]
        remaining = max(0, len(folder_groups) - len(preview_lines))
        if remaining:
            preview_lines.append(f"還有 {remaining} 個資料夾，點「查看資料夾」查看。")
        if not preview_lines:
            preview_lines = ["目前沒有可用檔案"]
        if _is_discord_user_key(line_user_id):
            lines.append(f"**{course_id} {course_name}**".strip())
            for line in preview_lines:
                lines.append(f"• {line}")
            lines.append("")
        else:
            lines.append(f"- {course_id} {course_name}".strip())
            for line in preview_lines:
                lines.append(f"  {line}")
        bubbles.append(_build_file_course_bubble(course_id, course_name, preview_lines))

    if not bubbles:
        return _discord_empty_state(f"I found the course, but there aren't any downloadable materials for **{keyword}** yet.", line_user_id, emoji="📎") if _is_discord_user_key(line_user_id) else f"「{keyword}」目前沒有可用檔案連結。"

    messages = [item for item in [_build_cache_status_flex(cache_status, "檔案快取")] if item]
    messages.append(
        {
            "type": "flex",
            "altText": "\n".join(lines),
            "contents": {
                "type": "carousel",
                "contents": bubbles,
            },
        }
    )
    return _line_response("\n".join(lines), messages=messages)


def _extract_file_target_and_page(action, tokens):
    page = 1
    folder_index = None
    raw_target = ""
    if tokens and tokens[0] in {"檔案詳情", "檔案資料夾"}:
        raw_target = " ".join(tokens[1:]).strip()
    elif len(tokens) >= 3:
        raw_target = " ".join(tokens[2:]).strip()
    else:
        match = re.match(r"^檔案(?:詳情|資料夾)\s*(.+)$", action.strip())
        if match:
            raw_target = match.group(1).strip()

    if not raw_target:
        return "", 1, None

    page_match = re.search(r"(?:\s+|^)(?:p|page|頁)(\d+)$", raw_target, flags=re.IGNORECASE)
    if page_match:
        page = max(1, int(page_match.group(1)))
        raw_target = raw_target[: page_match.start()].strip()
    folder_match = re.search(r"(?:\s+|^)(?:f|folder)(\d+)$", raw_target, flags=re.IGNORECASE)
    if folder_match:
        folder_index = max(1, int(folder_match.group(1)))
        raw_target = raw_target[: folder_match.start()].strip()
    return raw_target, page, folder_index


def _extract_course_index(action, tokens):
    if len(tokens) >= 2 and tokens[1].isdigit():
        return int(tokens[1])
    if len(tokens) >= 3 and tokens[2].isdigit():
        return int(tokens[2])
    match = re.match(r"^課程詳情\s*(\d+)$", action.strip())
    if match:
        return int(match.group(1))
    return None


def _extract_course_target(action, tokens, command_head_zh, command_noun_en):
    raw_target = ""
    if tokens and tokens[0] == command_head_zh:
        raw_target = " ".join(tokens[1:]).strip()
    elif len(tokens) >= 3 and tokens[0].lower() in {"course", "courses"} and tokens[1].lower() == command_noun_en:
        raw_target = " ".join(tokens[2:]).strip()
    else:
        match = re.match(rf"^{re.escape(command_head_zh)}\s*(.+)$", action.strip())
        if match:
            raw_target = match.group(1).strip()
    return raw_target


def _extract_indexed_target(action, tokens, command_head_zh):
    raw_target = ""
    if tokens and tokens[0] == command_head_zh:
        raw_target = " ".join(tokens[1:]).strip()
    else:
        match = re.match(rf"^{re.escape(command_head_zh)}\s*(.+)$", action.strip())
        if match:
            raw_target = match.group(1).strip()

    if not raw_target:
        return "", None

    index = None
    index_match = re.search(r"(?:\s+|^)(?:i|item|#)(\d+)$", raw_target, flags=re.IGNORECASE)
    if index_match:
        index = max(1, int(index_match.group(1)))
        raw_target = raw_target[: index_match.start()].strip()
    return raw_target, index


def _count_active_assignments(payload):
    count = 0
    for item in _assignment_items(payload):
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip().lower()
        if category and category not in {"in_progress", "upcoming"}:
            continue
        if _is_assignment_completed(item):
            continue
        count += 1
    return count


def _count_completed_assignments(payload):
    count = 0
    for item in _assignment_items(payload):
        if _is_assignment_completed(item):
            count += 1
    return count


def _build_course_bubble(summary):
    return {
        "type": "bubble",
        "size": "kilo",
        "xe3_meta": {
            "selector_kind": "course_summary",
            "entry_kind": "course_summary",
            "item_title": summary["course_name"],
            "course_name": summary["course_name"],
            "course_id": summary["course_id"],
            "selector_summary_title": "選擇課程",
            "selector_section": "📘 課程",
            "option_label": summary["course_label"] or summary["course_name"],
            "option_description": f"作業 {summary['homework_count']}｜成績 {summary['grade_count']}｜檔案 {summary['file_count']}",
        },
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0F766E",
            "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": "課程", "color": "#CCFBF1", "size": "xs"},
                {"type": "text", "text": summary["course_name"], "color": "#FFFFFF", "weight": "bold", "wrap": True},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {"type": "text", "text": summary["course_id"] or "未提供課號", "size": "sm", "color": "#475569"},
                {"type": "text", "text": f"作業 {summary['homework_count']}｜成績 {summary['grade_count']}", "size": "sm", "wrap": True},
                {"type": "text", "text": f"檔案 {summary['file_count']}", "size": "sm", "wrap": True},
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "color": "#0F766E",
                    "action": {
                        "type": "message",
                        "label": "查看課程",
                        "text": f"e3 課程摘要 {summary['index']}",
                        "xe3_meta": {
                            "selector_kind": "course_summary",
                            "entry_kind": "course_summary",
                            "item_title": summary["course_name"],
                            "course_name": summary["course_name"],
                            "course_id": summary["course_id"],
                            "option_label": summary["course_label"] or summary["course_name"],
                            "option_description": f"作業 {summary['homework_count']}｜成績 {summary['grade_count']}｜檔案 {summary['file_count']}",
                        },
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "action": {
                        "type": "message",
                        "label": "查看教材",
                        "text": f"e3 檔案資料夾 {summary['course_id'] or summary['course_name']}",
                        "xe3_meta": {
                            "selector_kind": "file_folder",
                            "entry_kind": "course_materials",
                            "item_title": summary["course_name"],
                            "course_name": summary["course_name"],
                            "course_id": summary["course_id"],
                        },
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "action": {
                        "type": "message",
                        "label": "查看作業",
                        "text": f"e3 課程作業 {summary['course_id'] or summary['course_name']}",
                        "xe3_meta": {
                            "selector_kind": "course_homework_detail",
                            "entry_kind": "course_homework",
                            "item_title": summary["course_name"],
                            "course_name": summary["course_name"],
                            "course_id": summary["course_id"],
                        },
                    },
                },
            ],
        },
    }


def _require_line_user(line_user_id):
    if not line_user_id:
        return None, "這個 E3 指令需要 LINE 使用者身分（請在 1:1 聊天中使用）。"
    user_id = upsert_user(line_user_id)
    return user_id, None


def _sync_events_for_user(user_id, courses, calendar_events=None):
    events = extract_events_from_fetch_all(courses, calendar_events=calendar_events)
    active_event_uids = []
    for event in events:
        active_event_uids.append(event["event_uid"])
        upsert_event(
            user_id=user_id,
            event_uid=event["event_uid"],
            event_type=event["event_type"],
            course_id=event.get("course_id"),
            course_name=event.get("course_name"),
            title=event["title"],
            due_at=event["due_at"],
            payload_json=event["payload_json"],
        )
    mark_missing_events_inactive(user_id, active_event_uids)
    return events


def _format_home_preview(preview):
    user_key = preview.get("_user_key")
    lines = []
    user_name = preview.get("user_name") or ""
    user_email = preview.get("user_email") or ""
    if user_name:
        lines.append(f"👤 **Name:** {_discord_bold(user_name, user_key)}" if _is_discord_user_key(user_key) else f"👤 姓名：{user_name}")
    if user_email:
        lines.append(f"📧 **Email:** {user_email}" if _is_discord_user_key(user_key) else f"📧 Email：{user_email}")
    if not lines:
        if _is_discord_user_key(user_key):
            lines.append("👤 **Name:** Not available yet")
            lines.append("📧 **Email:** Not available yet")
        else:
            lines.append("👤 姓名：未取得")
            lines.append("📧 Email：未取得")
    return "\n".join(lines)


def _discord_command_hint(command: str, user_key=None) -> str:
    return f"`{command}`" if _is_discord_user_key(user_key) else command


def _discord_separator(user_key=None) -> str:
    return "──────────" if _is_discord_user_key(user_key) else ""


def _discord_empty_state(message: str, user_key=None, emoji: str = "✨") -> str:
    return f"{emoji} {message}" if _is_discord_user_key(user_key) else message


def _parse_event_type_filter(tokens):
    if len(tokens) < 2:
        return None, None

    raw_filter = tokens[1].strip().lower()
    event_type = EVENT_TYPE_ALIASES.get(raw_filter)
    if event_type:
        return event_type, None
    return None, "類型只支援：作業 / 行事曆 / 考試"


def _default_reminder_schedule():
    return ["09:00", "21:00"]


def _load_reminder_schedule(prefs) -> list[str]:
    raw = None
    if prefs is None:
        raw = None
    elif isinstance(prefs, dict):
        raw = prefs.get("schedule_json")
    else:
        try:
            raw = prefs["schedule_json"]
        except (KeyError, IndexError, TypeError):
            raw = None
    if not raw:
        return _default_reminder_schedule()
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return _default_reminder_schedule()
    if not isinstance(parsed, list):
        return _default_reminder_schedule()
    normalized = []
    for value in parsed:
        slot = str(value or "").strip()
        if slot and slot not in normalized:
            normalized.append(slot)
    return normalized or _default_reminder_schedule()


def _schedule_choice_from_value(value: str) -> list[str] | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"both", "all", "09+21", "09:00+21:00"}:
        return ["09:00", "21:00"]
    if normalized in {"morning", "am", "09", "09:00"}:
        return ["09:00"]
    if normalized in {"evening", "pm", "21", "21:00"}:
        return ["21:00"]
    return None


def _line_response(text, messages=None):
    return line_response(text, messages=messages)


def _format_cache_status_text(cache_status):
    if not cache_status or not cache_status.get("exists"):
        return "*(目前還沒有本地快取，XE3 會在下次同步後建立。)*" if cache_status is not None else "🕒 目前沒有快取，XE3 會在背景重新整理。"

    age_minutes = int(cache_status.get("age_minutes") or 0)
    ttl_minutes = int(cache_status.get("ttl_minutes") or 15)
    if cache_status.get("is_fresh"):
        return f"*(資料於 {age_minutes} 分鐘前同步完成，目前是最新狀態 ✨)*"
    return f"*(Local snapshot is {age_minutes} min old. XE3 will refresh it in the background; freshness window is {ttl_minutes} min.)*"


def _build_cache_status_flex(cache_status, title):
    return None


def _store_last_event_index(line_user_id, ordered_groups):
    if not line_user_id:
        return
    mapping = {}
    for group in ordered_groups:
        items = group[-1]
        for idx, row in items:
            event_uid = row["event_uid"] if isinstance(row, dict) else row["event_uid"]
            if event_uid:
                mapping[idx] = event_uid
    _LAST_EVENT_INDEX[line_user_id] = mapping


def _format_reminder_summary(enabled, schedule, timezone_name="Asia/Taipei"):
    schedule_text = ", ".join(schedule) if schedule else "未設定"
    return (
        "⏰ E3 提醒設定\n"
        "──────────\n"
        f"🟢 狀態：{'開啟' if enabled else '關閉'}\n"
        f"🌏 時區：{timezone_name}\n"
        f"🕘 時段：{schedule_text}\n"
        "▶️ 可先按 Test Reminder 看實際訊息內容。"
    )


def _build_reminder_settings_flex(enabled, schedule, alt_text):
    status_text = "已開啟" if enabled else "已關閉"
    status_color = "#15803D" if enabled else "#B91C1C"
    bg_color = "#F0FDF4" if enabled else "#FEF2F2"
    schedule_text = " / ".join(schedule) if schedule else "尚未設定"

    def _button(label, text, style="secondary", color="#2563EB"):
        button = {
            "type": "button",
            "height": "sm",
            "style": style,
            "action": {
                "type": "message",
                "label": label,
                "text": text,
            },
        }
        if style == "primary":
            button["color"] = color
        return button

    return {
        "type": "flex",
        "altText": alt_text,
        "contents": {
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#0F766E",
                "paddingAll": "14px",
                "contents": [
                    {
                        "type": "text",
                        "text": "提醒設定",
                        "color": "#FFFFFF",
                        "weight": "bold",
                        "size": "lg",
                    },
                    {
                        "type": "text",
                        "text": "⏰ 每天固定時段自動推送近期事件",
                        "color": "#CCFBF1",
                        "size": "xs",
                        "margin": "sm",
                    },
                ],
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {
                        "type": "box",
                        "layout": "vertical",
                        "backgroundColor": bg_color,
                        "cornerRadius": "12px",
                        "paddingAll": "12px",
                        "spacing": "sm",
                        "contents": [
                            {
                                "type": "text",
                                "text": f"🟢 狀態｜{status_text}",
                                "weight": "bold",
                                "color": status_color,
                                "size": "sm",
                            },
                            {
                                "type": "text",
                                "text": "🌏 時區｜Asia/Taipei",
                                "size": "xs",
                                "color": "#475569",
                            },
                            {
                                "type": "text",
                                "text": f"🕘 時段｜{schedule_text}",
                                "size": "sm",
                                "wrap": True,
                                "color": "#0F172A",
                            },
                            {
                                "type": "text",
                                "text": "──────────",
                                "size": "xs",
                                "color": "#94A3B8",
                            },
                            {
                                "type": "text",
                                "text": "📦 每天會先用本地快取整理，再在時段內送出提醒。",
                                "size": "xs",
                                "wrap": True,
                                "color": "#475569",
                            },
                        ],
                    },
                    {
                        "type": "text",
                        "text": "⚙️ 快速操作",
                        "weight": "bold",
                        "size": "sm",
                        "color": "#334155",
                    },
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "spacing": "sm",
                        "contents": [
                            _button("開啟提醒", "e3 remind on", style="primary", color="#15803D"),
                            _button("關閉提醒", "e3 remind off", style="primary", color="#B91C1C"),
                        ],
                    },
                    {
                        "type": "text",
                        "text": "🧭 提醒節奏",
                        "weight": "bold",
                        "size": "sm",
                        "color": "#334155",
                    },
                    {
                        "type": "text",
                        "text": "09:00：早安摘要 + 近期截止提醒\n21:00：晚間截止整理",
                        "size": "sm",
                        "wrap": True,
                        "color": "#334155",
                    },
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    _button("重新整理設定", "e3 remind show"),
                ],
            },
        },
    }


def _build_text_summary_flex(title, text, *, color="#2563EB", alt_text=None):
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    if lines and lines[0].strip() == title.strip():
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)

    body_contents = []
    for line in lines:
        if not line.strip():
            body_contents.append({"type": "separator", "margin": "md"})
            continue
        body_contents.append(
            {
                "type": "text",
                "text": line,
                "size": "sm",
                "wrap": True,
                "color": "#0F172A",
            }
        )

    if not body_contents:
        body_contents = [
            {
                "type": "text",
                "text": "目前沒有更多內容。",
                "size": "sm",
                "wrap": True,
                "color": "#475569",
            }
        ]

    return {
        "type": "flex",
        "altText": alt_text or text,
        "contents": {
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": color,
                "paddingAll": "12px",
                "contents": [
                    {
                        "type": "text",
                        "text": title,
                        "color": "#FFFFFF",
                        "weight": "bold",
                        "size": "lg",
                    },
                ],
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": body_contents,
            },
        },
    }


def _collect_course_calendar_events(snapshot, course_id):
    items = []
    for event in snapshot.get("calendar_events") or []:
        if str(event.get("course_id") or "").strip() != course_id:
            continue
        due_at = event.get("due_at")
        if not due_at:
            continue
        items.append(event)
    items.sort(key=lambda item: item.get("due_at") or "")
    return items[:3]


def _normalize_title_token(text):
    return re.sub(r"\s+", " ", str(text or "").strip()).casefold()


def _assignment_completion_map(payload):
    mapping = {}
    for item in _assignment_items(payload):
        if not isinstance(item, dict):
            continue
        title = _normalize_title_token(item.get("title") or item.get("name"))
        if not title:
            continue
        mapping[title] = _is_assignment_completed(item)
    return mapping


def _clean_outline_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def _short_exam_topic(topic: str) -> str:
    clean = html.unescape(_clean_outline_text(topic))
    if not clean:
        return ""
    direct = re.search(r"((?:Exam|Midterm|Final Exam|Quiz)\s*[\w\-()\/.: ]*)", clean, flags=re.IGNORECASE)
    if direct:
        return _clean_outline_text(direct.group(1))
    zh_direct = re.search(r"((?:期中考|期末考|小考|測驗)[^｜,，;；]*)", clean)
    if zh_direct:
        return _clean_outline_text(zh_direct.group(1))
    return _shorten_title(clean, 36)


def _short_exam_date(class_date: str) -> str:
    clean = html.unescape(_clean_outline_text(class_date))
    if not clean:
        return ""
    matches = re.findall(r"(\d{4}-\d{2}-\d{2}(?:\([^)]+\))?)", clean)
    if matches:
        return matches[-1]
    return clean


def _course_outline_summary(payload):
    if not isinstance(payload, dict):
        return {}

    timetable = payload.get("timetable") or {}
    if not isinstance(timetable, dict):
        timetable = {}

    outline = timetable.get("course_outline_data") or {}
    if not isinstance(outline, dict):
        outline = {}

    base = outline.get("base_normalized") or outline.get("base") or {}
    desc = outline.get("description_normalized") or outline.get("description") or {}
    syllabus = outline.get("syllabus_normalized") or outline.get("syllabus") or []
    if not isinstance(base, dict):
        base = {}
    if not isinstance(desc, dict):
        desc = {}
    if not isinstance(syllabus, list):
        syllabus = []

    exam_lines = []
    for row in syllabus:
        if not isinstance(row, dict):
            continue
        topic = _clean_outline_text(row.get("class_data"))
        class_date = _clean_outline_text(row.get("class_date"))
        if not topic or not class_date:
            continue
        if not any(keyword in topic.lower() for keyword in ("exam", "midterm", "final", "quiz", "期中", "期末", "考試", "測驗")):
            continue
        short_date = _short_exam_date(class_date)
        short_topic = _short_exam_topic(topic)
        if short_date and short_topic:
            exam_lines.append(f"{short_date}｜{short_topic}")

    return {
        "teacher": _clean_outline_text(base.get("tea_name") or base.get("Instructors") or base.get("teacher_id")),
        "credits": _clean_outline_text(base.get("cos_credit")),
        "schedule": _clean_outline_text(base.get("cos_time") or desc.get("crs_meeting_time")),
        "textbook": _clean_outline_text(desc.get("crs_textbook")),
        "prerequisite": _clean_outline_text(desc.get("crs_prerequisite")),
        "grading": _clean_outline_text(desc.get("crs_exam_score")),
        "outline": _clean_outline_text(desc.get("crs_outline")),
        "meeting_place": _clean_outline_text(desc.get("crs_meeting_place")),
        "contact": _clean_outline_text(desc.get("crs_contact")),
        "exam_lines": exam_lines[:3],
        "syllabus_count": len(syllabus),
    }


def _course_grade_summary(payload):
    grades = payload.get("grades") or {}
    if not isinstance(grades, dict):
        return {}

    summary = grades.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    items = grades.get("grade_items") or []
    if not isinstance(items, list):
        items = []

    graded = []
    feedback_count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        score = str(item.get("score") or "").strip()
        if score and score != "-":
            graded.append(item)
        if str(item.get("feedback") or "").strip():
            feedback_count += 1

    latest_lines = []
    for item in graded[:3]:
        title = _shorten_title(str(item.get("item_name") or "評分項目"), 28)
        score = str(item.get("score") or "-").strip() or "-"
        score_range = str(item.get("range") or "").strip()
        latest_lines.append(f"{title}｜{score}" + (f" / {score_range}" if score_range and score_range != "-" else ""))

    return {
        "total_items": int(summary.get("total_items") or len(items) or 0),
        "scored_items": int(summary.get("scored_items") or len(graded) or 0),
        "feedback_count": feedback_count,
        "latest_lines": latest_lines,
    }


def _collect_course_homework_items(payload):
    items = []
    for item in _assignment_items(payload):
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip().lower()
        if category and category not in {"in_progress", "upcoming"}:
            continue
        if _is_assignment_completed(item):
            continue
        due_raw = item.get("due") or item.get("due_time") or item.get("due_date") or item.get("deadline") or item.get("截止")
        items.append(
            {
                "title": str(item.get("title") or item.get("name") or "未命名作業").strip(),
                "due_at": due_raw,
            }
        )
    return items[:3]
def _extract_detail_index(action, tokens):
    if len(tokens) >= 2 and tokens[1].isdigit():
        return int(tokens[1])

    match = re.match(r"^詳情\s*(\d+)$", action.strip())
    if match:
        return int(match.group(1))
    return None


def _format_event_detail(row, index, user_key=None):
    payload = _event_payload(row)
    type_label = _event_type_label_for_display(row, payload)
    title = _event_title_for_display(row, payload)

    lines = [f"🔎 **事件詳情 #{index}**" if _is_discord_user_key(user_key) else f"🔎 事件詳情 #{index}"]
    lines.append("──────────" if _is_discord_user_key(user_key) else "")
    lines.append(f"🗂️ 類型：{_discord_bold(type_label, user_key)}")
    lines.append(f"📚 課程：{_discord_bold(_course_name_for_display(row['course_name'] or row['course_id'] or '-'), user_key)}")
    lines.append(f"📝 標題：{_discord_bold(title, user_key)}")
    lines.append(f"⏰ 截止：{_format_due_at_full(row['due_at'], user_key)}")

    date_label = payload.get("date_label")
    if date_label:
        lines.append(f"📅 顯示日期：{date_label}")

    url = payload.get("url")
    if url:
        lines.append(f"🔗 連結：{url}")

    event_id = payload.get("event_id")
    if event_id:
        lines.append(f"🆔 事件 ID：`{event_id}`" if _is_discord_user_key(user_key) else f"事件 ID：{event_id}")

    attachments = payload.get("attachments") or []
    submitted_files = payload.get("submitted_files") or []
    if attachments:
        lines.append(f"📎 附件：{len(attachments)} 個")
        for item in attachments[:3]:
            if isinstance(item, dict):
                lines.append(f"• {str(item.get('name') or '附件').strip()}")
    if submitted_files:
        lines.append(f"📤 已繳檔案：{len(submitted_files)} 個")
        for item in submitted_files[:3]:
            if isinstance(item, dict):
                lines.append(f"• {str(item.get('name') or '已繳檔案').strip()}")

    return "\n".join(line for line in lines if line != "")


def _format_timeline(rows, header):
    lines = [header]
    ordered_groups = _build_timeline_display_groups(rows)
    for event_type, items in ordered_groups:
        if not items:
            continue
        lines.append(_timeline_heading(event_type))
        for idx, row in items:
            due_at = _format_due_at_for_display(row["due_at"])
            course_name = _shorten_course_name(row["course_name"] or row["course_id"] or "-")
            title = _shorten_title(row["title"])
            icon = "👉" if event_type == "homework" else "📍"
            lines.append(f"{idx}. {due_at} ｜{course_name}")
            lines.append(f"   {icon} {title}")
            lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _build_timeline_display_groups(rows):
    ordered_rows = _timeline_rows_sorted(rows)
    if not ordered_rows:
        return []

    ordered = []
    display_index = 1
    if len({str(row["event_type"]) for row in ordered_rows}) == 1:
        event_type = str(ordered_rows[0]["event_type"])
        ordered.append((event_type, [(idx, row) for idx, row in enumerate(ordered_rows, start=1)]))
        return ordered

    mixed_items = []
    for row in ordered_rows:
        mixed_items.append((display_index, row))
        display_index += 1
    ordered.append(("mixed", mixed_items))
    return ordered


def _build_timeline_urgency_groups(rows):
    ordered_rows = _timeline_rows_sorted(rows)
    if not ordered_rows:
        return []

    now_utc = datetime.now(timezone.utc)
    buckets = [
        ("urgent", "🚨 48 小時內", []),
        ("week", "📅 本週內", []),
        ("later", "📌 之後", []),
    ]

    for row in ordered_rows:
        due_dt = _parse_due_at_sort_key(row["due_at"])
        delta = due_dt - now_utc
        if delta <= timedelta(hours=48):
            buckets[0][2].append(row)
        elif delta <= timedelta(days=7):
            buckets[1][2].append(row)
        else:
            buckets[2][2].append(row)

    ordered = []
    display_index = 1
    for bucket_key, bucket_label, bucket_rows in buckets:
        if not bucket_rows:
            continue
        indexed_rows = []
        for row in bucket_rows:
            indexed_rows.append((display_index, row))
            display_index += 1
        ordered.append((bucket_key, bucket_label, indexed_rows))
    return ordered


def _filter_rows_by_event_type(rows, event_type):
    if not event_type:
        return rows
    if event_type == "academic":
        return [row for row in rows if row["event_type"] in {"homework", "exam"}]
    return [row for row in rows if row["event_type"] == event_type]


def _build_timeline_messages(rows, header, event_type=None, line_user_id=None, use_triage=None):
    filtered_rows = _filter_rows_by_event_type(rows, event_type)
    if not filtered_rows:
        return None, [], []

    filtered_rows = _timeline_rows_sorted(filtered_rows)
    use_triage = (event_type is None) if use_triage is None else bool(use_triage)
    ordered_groups = _build_timeline_urgency_groups(filtered_rows) if use_triage else _build_timeline_display_groups(filtered_rows)
    text_sections = []
    messages = []
    for group in ordered_groups:
        if use_triage:
            group_event_type, heading, items = group
        else:
            group_event_type, items = group
            heading = "🗓️ 時間軸" if group_event_type == "mixed" else _timeline_heading(group_event_type)
        if not items:
            continue
        if use_triage:
            section_lines = [header] if not text_sections else []
        elif group_event_type == "mixed":
            section_lines = [header] if not text_sections else []
        elif not text_sections:
            section_lines = [header, _timeline_heading(group_event_type)]
        else:
            section_lines = [_timeline_heading(group_event_type)]
        if use_triage:
            section_lines.append(heading)
        for idx, row in items:
            due_at = _format_due_at_for_display(row["due_at"], line_user_id)
            course_name = _shorten_course_name(row["course_name"] or row["course_id"] or "-")
            payload = _event_payload(row)
            title = _shorten_title(_event_title_for_display(row, payload))
            icon = "👉" if row["event_type"] == "homework" else "📍"
            type_label = _event_type_label_for_display(row, payload)
            if _is_discord_user_key(line_user_id):
                urgency_icon = "🚨" if group_event_type == "urgent" else ("📅" if group_event_type == "week" else "📌")
                section_lines.append(f"{urgency_icon} **{course_name}**")
                if use_triage or group_event_type == "mixed":
                    section_lines.append(f"• [{type_label}] {title}")
                else:
                    section_lines.append(f"• {title}")
                section_lines.append(f"• 截止 {due_at}")
                section_lines.append("")
            else:
                section_lines.append(f"{idx}. {due_at} ｜{course_name}")
                if use_triage or group_event_type == "mixed":
                    section_lines.append(f"   {icon} [{type_label}] {title}")
                else:
                    section_lines.append(f"   {icon} {title}")
                section_lines.append("")
        if section_lines[-1] == "":
            section_lines.pop()
        section_text = "\n".join(section_lines)
        text_sections.append(section_text)
        hero_title = heading
        flex = _build_timeline_flex(
            items,
            section_text,
            hero_title,
            event_type=None if use_triage or group_event_type == "mixed" else group_event_type,
            line_user_id=line_user_id,
        )
        if flex:
            messages.append(flex)

    return "\n\n".join(text_sections), messages, ordered_groups


def _event_detail(action, tokens, line_user_id):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    index = _extract_detail_index(action, tokens)
    if index is None or index <= 0:
        return "用法：e3 詳情 <編號>"

    row = None
    event_uid = (_LAST_EVENT_INDEX.get(line_user_id) or {}).get(index)
    if event_uid:
        row = get_event_by_uid(user_id, event_uid)

    if row is None:
        rows = get_timeline_event_details(user_id, limit=50)
        display_rows = []
        for _, items in _build_timeline_display_groups(rows):
            display_rows.extend(items)
        for display_index, candidate in display_rows:
            if display_index == index:
                row = candidate
                break

    if row is None:
        return f"找不到第 {index} 筆事件，請先輸入 `e3 近期` 或 `e3 timeline` 確認編號。"

    text = _format_event_detail(row, index, line_user_id)
    flex = _build_detail_flex(row, index, text, line_user_id=line_user_id)
    payload = {}
    payload_json = row["payload_json"] or ""
    if payload_json:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            payload = {}

    messages = [flex] if flex else []
    file_entries = _payload_file_entries(payload, line_user_id, str(row["title"] or "事件檔案"))
    if file_entries:
        file_flex = _build_file_download_flex(file_entries, f"📎 {row['title']} 檔案", str(row["title"] or "事件檔案"))
        if file_flex:
            messages.append(file_flex)
    return _line_response(text, messages=messages or None)


def _course_detail(action, tokens, logger, line_user_id):
    _, err = _require_line_user(line_user_id)
    if err:
        return err

    index = _extract_course_index(action, tokens)
    if index is None or index <= 0:
        return f"請使用 {_discord_command_hint('e3 課程詳情 <編號>', line_user_id)}。" if _is_discord_user_key(line_user_id) else "用法：e3 課程詳情 <編號>"

    try:
        courses = fetch_courses(make_user_key(line_user_id))
        timeline_snapshot = fetch_timeline_snapshot(make_user_key(line_user_id))
        file_snapshot = fetch_file_links(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_course_detail_failed error=%s", exc)
        return (
            f"⚠️ XE3 couldn't load that course detail.\nTry {_discord_command_hint('e3 relogin', line_user_id)} first."
            if _is_discord_user_key(line_user_id)
            else "課程詳情讀取失敗，請先 `e3 relogin`。"
        )

    semester_tag = _current_semester_tag()
    current_courses = _current_semester_courses(courses, semester_tag=semester_tag)

    if index > len(current_courses):
        return (
            f"⚠️ 我找不到課程 `#{index}`。\n先用 {_discord_command_hint('e3 course', line_user_id)} 確認目前的編號。"
            if _is_discord_user_key(line_user_id)
            else f"找不到第 {index} 門課程，請先輸入 `e3 course` 確認編號。"
        )

    display_name, payload = current_courses[index - 1]
    detail = course_runtime.build_course_detail_payload(display_name, payload, timeline_snapshot, file_snapshot, line_user_id)
    detail["index"] = index

    if _is_discord_user_key(line_user_id):
        text_lines = [
            f"📘 **課程詳情 #{index}**",
            _discord_separator(line_user_id),
            f"📚 **{detail['course_id']} {detail['course_name']}**".strip(),
            f"📝 作業 `{detail['homework_count']}` 未完成 · ✅ `{detail['completed_homework_count']}` 已完成",
            f"📅 行事曆 `{detail['calendar_count']}` · 📎 教材 `{detail['file_count']}`",
            "",
            f"🧾 **課綱重點**",
            *[f"• {line}" for line in detail["course_info_lines"]],
            "",
            _discord_separator(line_user_id),
            "",
            f"📊 **成績摘要**",
            *[f"• {line}" for line in detail["grade_summary_lines"]],
        ]
    else:
        text_lines = [
            f"📘 課程詳情 #{index}",
            f"課程：{course_id} {course_name}".strip(),
            f"未完成作業：{detail['homework_count']}｜已完成作業：{detail['completed_homework_count']}｜行事曆：{detail['calendar_count']}｜檔案：{detail['file_count']}",
            "課綱重點：" + ("；".join(detail["course_info_lines"]) if detail["course_info_lines"] else "-"),
            "成績摘要：" + ("；".join(detail["grade_summary_lines"]) if detail["grade_summary_lines"] else "-"),
            "課綱考試提醒：" + ("；".join(detail["exam_lines"]) if detail["exam_lines"] else "-"),
            "作業：" + ("；".join(detail["homework_lines"]) if detail["homework_lines"] else "-"),
            "行事曆：" + ("；".join(detail["calendar_lines"]) if detail["calendar_lines"] else "-"),
            "檔案：" + ("；".join(detail["file_lines"]) if detail["file_lines"] else "-"),
        ]
    text = "\n".join(text_lines)
    flex = build_course_detail_flex(detail, text)
    return _line_response(text, messages=[flex] if flex else None)


def _course_summary(action, tokens, logger, line_user_id):
    _, err = _require_line_user(line_user_id)
    if err:
        return err

    index = _extract_course_index(action, tokens)
    if index is None or index <= 0:
        return f"請使用 {_discord_command_hint('e3 課程摘要 <編號>', line_user_id)}。" if _is_discord_user_key(line_user_id) else "用法：e3 課程摘要 <編號>"

    try:
        courses = fetch_courses(make_user_key(line_user_id))
        timeline_snapshot = fetch_timeline_snapshot(make_user_key(line_user_id))
        file_snapshot = fetch_file_links(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_course_summary_failed error=%s", exc)
        return (
            f"⚠️ XE3 暫時無法打開這門課。\n先試試 {_discord_command_hint('e3 relogin', line_user_id)}。"
            if _is_discord_user_key(line_user_id)
            else "課程摘要讀取失敗，請先 `e3 relogin`。"
        )

    semester_tag = _current_semester_tag()
    current_courses = _current_semester_courses(courses, semester_tag=semester_tag)
    if index > len(current_courses):
        return (
            f"⚠️ 我找不到課程 `#{index}`。\n先用 {_discord_command_hint('e3 course', line_user_id)} 確認目前編號。"
            if _is_discord_user_key(line_user_id)
            else f"找不到第 {index} 門課程，請先輸入 `e3 course` 確認編號。"
        )

    display_name, payload = current_courses[index - 1]
    detail = course_runtime.build_course_detail_payload(display_name, payload, timeline_snapshot, file_snapshot, line_user_id)
    detail["index"] = index

    if _is_discord_user_key(line_user_id):
        text_lines = [
            "📘 **課程摘要**",
            _discord_separator(line_user_id),
            f"📚 **{detail['course_name']}**",
            "",
            f"▶️ 未完成作業：`{detail['homework_count']}`",
            f"▶️ 行事曆：`{detail['calendar_count']}`",
            f"▶️ 檔案：`{detail['file_count']}`",
            "",
            "🔴 **考試提醒**",
            *[f"• {line}" for line in detail["exam_lines"]],
            "",
            "🟠 **作業**",
            *[f"• {line}" for line in detail["homework_lines"]],
            "",
            "🟢 **行事曆**",
            *[f"• {line}" for line in detail["calendar_lines"]],
            "",
            "📎 **教材 / 檔案**",
            *[f"• {line}" for line in detail["file_lines"]],
        ]
    else:
        text_lines = [
            f"📘 課程摘要 #{index}",
            f"課程：{detail['course_id']} {detail['course_name']}".strip(),
            f"未完成作業：{detail['homework_count']}｜已完成作業：{detail['completed_homework_count']}｜行事曆：{detail['calendar_count']}｜檔案：{detail['file_count']}",
            "課綱考試提醒：" + ("；".join(detail["exam_lines"]) if detail["exam_lines"] else "-"),
            "作業：" + ("；".join(detail["homework_lines"]) if detail["homework_lines"] else "-"),
            "行事曆：" + ("；".join(detail["calendar_lines"]) if detail["calendar_lines"] else "-"),
            "檔案：" + ("；".join(detail["file_lines"]) if detail["file_lines"] else "-"),
        ]
    text = "\n".join(text_lines)
    flex = build_course_summary_flex(detail, text, index)
    return _line_response(text, messages=[flex] if flex else None)


def _course_homework(action, tokens, logger, line_user_id):
    _, err = _require_line_user(line_user_id)
    if err:
        return err

    target = _extract_course_target(action, tokens, "課程作業", "homework")
    if not target:
        return f"請使用 {_discord_command_hint('e3 課程作業 <課號或課名>', line_user_id)}。" if _is_discord_user_key(line_user_id) else "用法：e3 課程作業 <課號或課名>"

    try:
        courses = fetch_courses(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_course_homework_failed error=%s", exc)
        return (
            f"⚠️ XE3 couldn't load that homework list.\nTry {_discord_command_hint('e3 relogin', line_user_id)}."
            if _is_discord_user_key(line_user_id)
            else "課程作業讀取失敗，請先 `e3 relogin`。"
        )

    semester_tag = _current_semester_tag()
    matched_course = None
    for display_name, payload in _current_semester_courses(courses, semester_tag=semester_tag):
        course_id = str((payload or {}).get("_course_id") or "").strip()
        course_name = _course_name_for_display(display_name)
        searchable = f"{course_id} {course_name}"
        if _matches_course_keyword(searchable, target):
            matched_course = (course_id, course_name, payload)
            break

    if not matched_course:
        return (
            f"📝 我找不到和 **{target}** 對應的課程。"
            if _is_discord_user_key(line_user_id)
            else f"找不到「{target}」的課程作業。"
        )

    course_id, course_name, payload = matched_course
    items = course_runtime.collect_course_homework_entries(payload)

    if not items:
        return _discord_empty_state(f"先鬆一口氣，**{course_name}** 目前沒有需要查看的作業。🎉", line_user_id, emoji="📝") if _is_discord_user_key(line_user_id) else f"{course_name} 目前沒有可查看的作業。"

    lines = [f"📝 **{course_name} homework** (`{len(items)}` item(s))", _discord_separator(line_user_id)] if _is_discord_user_key(line_user_id) else [f"📝 {course_name} 作業列表（{len(items)} 筆）"]
    for idx, item in enumerate(items[:10], start=1):
        status_text = "已完成" if item.get("completed") else "未完成"
        if _is_discord_user_key(line_user_id):
            icon = "✅" if item.get("completed") else "🚨"
            lines.append(f"{icon} **{item['title']}**")
            lines.append(f"• Status: `{status_text}`")
            lines.append(f"• Due {_format_due_at_for_display(item['due_at'], line_user_id)}")
            lines.append("")
        else:
            lines.append(f"{idx}. [{status_text}] {item['title']}｜{_format_due_at_for_display(item['due_at'], line_user_id)}")
    if len(items) > 10:
        lines.append(f"…and `{len(items) - 10}` more." if _is_discord_user_key(line_user_id) else f"還有 {len(items) - 10} 筆作業。")
    text = "\n".join(lines)
    flex = course_runtime.build_course_homework_flex(course_name, course_id, items, text, line_user_id=line_user_id)
    return _line_response(text, messages=[flex] if flex else None)


def _course_homework_detail(action, tokens, logger, line_user_id):
    _, err = _require_line_user(line_user_id)
    if err:
        return err

    target, index = _extract_indexed_target(action, tokens, "作業詳情")
    if not target or not index:
        return f"請使用 {_discord_command_hint('e3 作業詳情 <課號或課名> i1', line_user_id)}。" if _is_discord_user_key(line_user_id) else "用法：e3 作業詳情 <課號或課名> i1"

    try:
        courses = fetch_courses(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_course_homework_detail_failed error=%s", exc)
        return (
            f"⚠️ XE3 couldn't load that homework detail.\nTry {_discord_command_hint('e3 relogin', line_user_id)}."
            if _is_discord_user_key(line_user_id)
            else "作業詳情讀取失敗，請先 `e3 relogin`。"
        )

    semester_tag = _current_semester_tag()
    matched_course = None
    for display_name, payload in _current_semester_courses(courses, semester_tag=semester_tag):
        course_id = str((payload or {}).get("_course_id") or "").strip()
        course_name = _course_name_for_display(display_name)
        searchable = f"{course_id} {course_name}"
        if _matches_course_keyword(searchable, target):
            matched_course = (course_id, course_name, payload)
            break

    if not matched_course:
        return f"📝 我找不到和 **{target}** 對應的課程。" if _is_discord_user_key(line_user_id) else f"找不到「{target}」的課程作業。"

    course_id, course_name, payload = matched_course
    items = course_runtime.collect_course_homework_entries(payload)
    if index > len(items):
        return (
            f"⚠️ 我找不到作業 `#{index}`。\n先用 {_discord_command_hint(f'e3 課程作業 {course_id or course_name}', line_user_id)} 看看目前列表。"
            if _is_discord_user_key(line_user_id)
            else f"找不到第 {index} 個作業，請先輸入 `e3 課程作業 {course_id or course_name}`。"
        )

    selected = items[index - 1]
    raw = selected.get("_raw") or {}
    detail_url = str(raw.get("detail_url") or raw.get("url") or "").strip()
    attachments = [item for item in (raw.get("attachments") or []) if isinstance(item, dict)]
    submitted_files = [item for item in (raw.get("submitted_files") or []) if isinstance(item, dict)]

    lines = [f"📝 **{course_name} / 作業詳情 #{index}**", _discord_separator(line_user_id)] if _is_discord_user_key(line_user_id) else [f"📝 {course_name} / 作業詳情 #{index}"]
    lines.append(f"📝 標題：{_discord_bold(selected['title'], line_user_id)}" if _is_discord_user_key(line_user_id) else f"標題：{selected['title']}")
    lines.append(f"📌 狀態：{'已完成' if selected.get('completed') else '未完成'}" if _is_discord_user_key(line_user_id) else f"狀態：{'已完成' if selected.get('completed') else '未完成'}")
    lines.append(f"⏰ 截止：{_format_due_at_full(selected.get('due_at'), line_user_id)}")
    if attachments:
        lines.append(f"📎 附件：{len(attachments)} 個" if _is_discord_user_key(line_user_id) else f"附件：{len(attachments)} 個")
        for item in attachments[:3]:
            lines.append(f"• {str(item.get('name') or '附件').strip()}" if _is_discord_user_key(line_user_id) else f"  - {str(item.get('name') or '附件').strip()}")
    if submitted_files:
        lines.append(f"📤 已繳檔案：{len(submitted_files)} 個" if _is_discord_user_key(line_user_id) else f"已繳檔案：{len(submitted_files)} 個")
        for item in submitted_files[:3]:
            lines.append(f"• {str(item.get('name') or '已繳檔案').strip()}" if _is_discord_user_key(line_user_id) else f"  - {str(item.get('name') or '已繳檔案').strip()}")
    if detail_url:
        lines.append(f"🔗 連結：{detail_url}" if _is_discord_user_key(line_user_id) else f"連結：{detail_url}")

    file_entries = []
    for kind, items_list, accent in (
        ("作業附件", attachments, "#D97706"),
        ("已繳檔案", submitted_files, "#475569"),
    ):
        for item in items_list:
            source_url = str(item.get("url") or "").strip()
            title = str(item.get("name") or "").strip() or kind
            if not source_url:
                continue
            file_entries.append(
                {
                    "kind": kind,
                    "course_name": selected["title"],
                    "title": title,
                    "url": build_proxy_url(line_user_id, source_url, filename=title),
                    "accent": accent,
                }
            )

    messages = []
    if file_entries:
        alt_text = f"📝 {selected['title']} 檔案列表"
        flex = _build_file_download_flex(file_entries, alt_text, selected["title"])
        if flex:
            messages.append(flex)

    return _line_response("\n".join(lines), messages=messages or None)


def _file_detail(action, tokens, logger, line_user_id):
    _, err = _require_line_user(line_user_id)
    if err:
        return err

    target, page, folder_index = _extract_file_target_and_page(action, tokens)
    if not target:
        return "用法：e3 檔案詳情 <課號或課名> [p2]"

    try:
        snapshot = fetch_file_links(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_file_detail_failed error=%s", exc)
        return "檔案列表讀取失敗，請先 `e3 relogin`。"

    courses = snapshot.get("courses") or {}
    file_links = snapshot.get("file_links") or {}
    semester_tag = _current_semester_tag()
    matched_course = None
    for display_name, payload in _current_semester_courses(courses, semester_tag=semester_tag):
        course_id = str((payload or {}).get("_course_id") or "").strip()
        course_name = _course_name_for_display(display_name)
        searchable = f"{course_id} {course_name}"
        if _matches_course_keyword(searchable, target):
            matched_course = (course_id, course_name, file_links.get(course_id) or {})
            break

    if not matched_course:
        return f"找不到「{target}」的課程檔案。"

    course_id, course_name, links = matched_course
    entries = file_catalog.collect_file_entries(course_id, course_name, links)
    if not entries:
        return f"{course_name} 目前沒有可下載檔案。"

    folder_groups = file_catalog.group_file_entries(entries)
    folder_name = None
    if folder_index is not None:
        if folder_index > len(folder_groups):
            return f"找不到第 {folder_index} 個資料夾，請先輸入 `e3 檔案資料夾 {course_id or course_name}`。"
        folder_name, entries = folder_groups[folder_index - 1]

    page_size = _file_page_size(line_user_id)
    total_pages = max(1, (len(entries) + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    chunk = entries[start : start + page_size]
    for entry in chunk:
        entry["url"] = build_proxy_url(line_user_id, entry.get("source_url") or "", filename=entry.get("title") or "")

    title = f"{course_name} / {folder_name}" if folder_name else course_name
    lines = [f"📥 {title} 檔案列表（第 {page}/{total_pages} 頁，共 {len(entries)} 個）："]
    for idx, entry in enumerate(chunk, start=start + 1):
        lines.append(f"{idx}. [{entry['kind']}] {entry['title']}")
    if total_pages > 1:
        base_command = f"e3 檔案詳情 {course_id or course_name}"
        if folder_index is not None:
            base_command += f" f{folder_index}"
        lines.append(f"輸入 `{base_command} p{page + 1}` 可查看下一頁。" if page < total_pages else "已是最後一頁。")
    text = "\n".join(lines)
    alt_text = f"📥 {title} 檔案列表 第 {page}/{total_pages} 頁"
    course_key = course_id or course_name
    if folder_index is not None:
        course_key = f"{course_key} f{folder_index}"
    nav = _build_file_nav_bubble(course_key, page, total_pages)
    bubble_entries = list(chunk)
    if nav:
        bubble_entries.append(
            {
                "kind": "分頁",
                "course_name": title,
                "title": "檔案太多時，請用分頁查看。",
                "url": "",
                "accent": "#0F172A",
                "_nav": nav,
            }
        )
    flex = _build_file_download_flex(bubble_entries, alt_text, title)
    messages = [item for item in [flex] if item]
    return _line_response(text, messages=messages or None)


def _file_folders(action, tokens, logger, line_user_id):
    _, err = _require_line_user(line_user_id)
    if err:
        return err

    target, _, _ = _extract_file_target_and_page(action, tokens)
    if not target:
        return "用法：e3 檔案資料夾 <課號或課名>"

    try:
        snapshot = fetch_file_links(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_file_folders_failed error=%s", exc)
        return "資料夾列表讀取失敗，請先 `e3 relogin`。"

    courses = snapshot.get("courses") or {}
    file_links = snapshot.get("file_links") or {}
    semester_tag = _current_semester_tag()
    matched_course = None
    for display_name, payload in _current_semester_courses(courses, semester_tag=semester_tag):
        course_id = str((payload or {}).get("_course_id") or "").strip()
        course_name = _course_name_for_display(display_name)
        searchable = f"{course_id} {course_name}"
        if _matches_course_keyword(searchable, target):
            matched_course = (course_id, course_name, file_links.get(course_id) or {})
            break

    if not matched_course:
        return f"找不到「{target}」的課程資料夾。"

    course_id, course_name, links = matched_course
    groups = file_catalog.group_file_entries(file_catalog.collect_file_entries(course_id, course_name, links))
    if not groups:
        return f"{course_name} 目前沒有可用資料夾。"

    text_lines = [f"🗂️ {course_name} 資料夾："]
    bubbles = []
    for idx, (folder_name, items) in enumerate(groups[:10], start=1):
        text_lines.append(f"{idx}. {folder_name}｜{len(items)} 個檔案")
        bubbles.append(_build_file_folder_bubble(course_id or course_name, folder_name, len(items), idx))
    if len(groups) > 10:
        text_lines.append(f"另有 {len(groups) - 10} 個資料夾未顯示。")
    return _line_response(
        "\n".join(text_lines),
        messages=[
            {
                "type": "flex",
                "altText": f"🗂️ {course_name} 資料夾列表",
                "contents": {"type": "carousel", "contents": bubbles},
            }
        ],
    )


def _handle_remind(tokens, line_user_id):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    prefs = ensure_reminder_prefs(user_id)
    subcommand = tokens[1].lower() if len(tokens) >= 2 else "show"

    if subcommand in {"show", "狀態"}:
        schedule = _load_reminder_schedule(prefs)
        text = _format_reminder_summary(bool(prefs["enabled"]), schedule, prefs["timezone"])
        flex = _build_reminder_settings_flex(bool(prefs["enabled"]), schedule, text)
        return _line_response(text, messages=[flex] if flex else None)

    if subcommand in {"on", "開啟"}:
        update_reminder_enabled(user_id, True)
        prefs = get_reminder_prefs(user_id)
        schedule = _load_reminder_schedule(prefs)
        text = "✅ 已開啟 E3 自動提醒。\n\n" + _format_reminder_summary(True, schedule, prefs["timezone"])
        flex = _build_reminder_settings_flex(True, schedule, text)
        return _line_response(text, messages=[flex] if flex else None)

    if subcommand in {"off", "關閉"}:
        update_reminder_enabled(user_id, False)
        prefs = get_reminder_prefs(user_id)
        schedule = _load_reminder_schedule(prefs)
        text = "🛑 已關閉 E3 自動提醒。\n\n" + _format_reminder_summary(False, schedule, prefs["timezone"])
        flex = _build_reminder_settings_flex(False, schedule, text)
        return _line_response(text, messages=[flex] if flex else None)

    if subcommand in {"schedule", "time", "時段"}:
        choice = _schedule_choice_from_value(tokens[2] if len(tokens) >= 3 else "")
        if not choice:
            return "⚠️ 用法：`e3 remind schedule both|morning|evening`"
        update_reminder_schedule(user_id, choice)
        prefs = get_reminder_prefs(user_id)
        enabled = bool(prefs["enabled"]) if prefs else False
        text = "🕘 已更新提醒時段。\n\n" + _format_reminder_summary(enabled, choice, prefs["timezone"])
        flex = _build_reminder_settings_flex(enabled, choice, text)
        return _line_response(text, messages=[flex] if flex else None)

    return "⚠️ 用法：`e3 remind show`、`e3 remind on`、`e3 remind off`、`e3 remind schedule both|morning|evening`"


def _login(action, logger, line_user_id):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    tokens = action.split()
    if len(tokens) < 3:
            return f"請使用 {_discord_command_hint('e3 login <帳號> <密碼>', line_user_id)}。" if _is_discord_user_key(line_user_id) else "用法：e3 login <帳號> <密碼>"

    account = tokens[1].strip()
    password = tokens[2].strip()

    try:
        result = login_and_sync(account, password, make_user_key(line_user_id), update_data=True, update_links=True)
        courses = result["courses"]
        calendar_events = result.get("calendar_events") or []
        preview = result["home_preview"]
        if isinstance(preview, dict):
            preview = dict(preview)
            preview["_user_key"] = line_user_id
        events = _sync_events_for_user(user_id, courses, calendar_events=calendar_events)
        grade_changes = sync_grade_items(user_id, courses)
        upsert_e3_account(user_id, account, encrypt_secret(password), status="ok", error=None)
        if _is_discord_user_key(line_user_id):
            reply = (
                "✅ **You're in. XE3 is synced and ready.**\n"
                f"{_discord_separator(line_user_id)}\n"
                f"📚 Synced **{len(courses)}** course(s)\n"
                f"🗓️ Tracked **{len(events)}** timeline event(s)\n"
                f"{_format_home_preview(preview)}"
            )
        else:
            reply = (
                "✅ E3 登入成功。\n"
                f"已同步課程：{len(courses)} 門，時間軸事件：{len(events)} 筆。\n"
                f"{_format_home_preview(preview)}"
            )
        grade_summary = _format_grade_change_summary(grade_changes, line_user_id)
        if grade_summary:
            reply += "\n" + grade_summary
        return reply
    except Exception as exc:
        logger.error("e3_login_failed error=%s", exc)
        upsert_e3_account(user_id, account, encrypt_secret(password), status="error", error=str(exc))
        return _format_e3_error(exc)


def _relogin(logger, line_user_id):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    row = get_e3_account_by_user_id(user_id)
    if not row:
        return f"⚠️ I can't find a linked account yet.\nStart with {_discord_command_hint('e3 login <帳號> <密碼>', line_user_id)}." if _is_discord_user_key(line_user_id) else "找不到已綁定帳號，請先 `e3 login <帳號> <密碼>`。"

    account = row["e3_account"]
    encrypted_password = row["encrypted_password"]
    if not encrypted_password:
        return f"⚠️ Your saved credentials are missing.\nPlease sign in again with {_discord_command_hint('e3 login <帳號> <密碼>', line_user_id)}." if _is_discord_user_key(line_user_id) else "找不到已儲存密碼，請重新執行 `e3 login <帳號> <密碼>`。"

    try:
        password = decrypt_secret(encrypted_password)
        result = login_and_sync(account, password, make_user_key(line_user_id), update_data=True, update_links=True)
        courses = result["courses"]
        calendar_events = result.get("calendar_events") or []
        preview = result["home_preview"]
        if isinstance(preview, dict):
            preview = dict(preview)
            preview["_user_key"] = line_user_id
        events = _sync_events_for_user(user_id, courses, calendar_events=calendar_events)
        grade_changes = sync_grade_items(user_id, courses)
        update_login_state(user_id, "ok", None)
        if _is_discord_user_key(line_user_id):
            reply = (
                "✅ **All caught up. XE3 refreshed your E3 data.**\n"
                f"{_discord_separator(line_user_id)}\n"
                f"📚 Synced **{len(courses)}** course(s)\n"
                f"🗓️ Tracked **{len(events)}** timeline event(s)\n"
                f"{_format_home_preview(preview)}"
            )
        else:
            reply = (
                "✅ E3 重新登入成功。\n"
                f"已同步課程：{len(courses)} 門，時間軸事件：{len(events)} 筆。\n"
                f"{_format_home_preview(preview)}"
            )
        grade_summary = _format_grade_change_summary(grade_changes, line_user_id)
        if grade_summary:
            reply += "\n" + grade_summary
        return reply
    except Exception as exc:
        logger.error("e3_relogin_failed error=%s", exc)
        update_login_state(user_id, "error", str(exc))
        if "Exceeded 30 redirects" in str(exc):
            return _format_e3_error(exc)
        return f"⚠️ XE3 couldn't refresh your E3 session.\nPlease sign in again with {_discord_command_hint('e3 login <帳號> <密碼>', line_user_id)}." if _is_discord_user_key(line_user_id) else "E3 重新登入失敗，請重新輸入 `e3 login <帳號> <密碼>`。"


def _logout(line_user_id):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    delete_user_data(user_id)
    clear_runtime_data(make_user_key(line_user_id))
    return "🧹 **You're signed out.** XE3 cleared your local link, event cache, and login workspace." if _is_discord_user_key(line_user_id) else "🧹 E3 已登出，並清除本地綁定、事件快取與登入工作目錄。"


def _upcoming(tokens, line_user_id):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    event_type, filter_error = _parse_event_type_filter(tokens)
    if filter_error:
        return f"⚠️ {filter_error}"

    rows = get_upcoming_events(user_id, limit=10)
    if not rows:
        return _discord_empty_state(f"目前還沒有近期事件。\n可以先用 {_discord_command_hint('e3 login', line_user_id)} 或 {_discord_command_hint('e3 relogin', line_user_id)} 同步資料。", line_user_id, emoji="⏰") if _is_discord_user_key(line_user_id) else "目前沒有近期事件，請先 `e3 login` 或 `e3 relogin` 進行同步。"
    cache_status = get_cache_status(make_user_key(line_user_id))
    try:
        courses = fetch_courses(make_user_key(line_user_id))
    except Exception:
        courses = {}
    rows = _filter_active_homework_rows(rows, courses)
    if event_type == "homework" and not rows:
        return _discord_empty_state("先喘口氣吧，目前沒有未繳且即將到期的作業。🎉", line_user_id, emoji="📝") if _is_discord_user_key(line_user_id) else "目前沒有未繳且尚未過期的作業。"
    text, messages, ordered_groups = _build_timeline_messages(
        rows,
        "⏰ **近期提醒**" if _is_discord_user_key(line_user_id) else "⏰ 近期提醒（前 10 筆）：",
        event_type=event_type,
        line_user_id=line_user_id,
    )
    if not text:
        return _discord_empty_state("目前沒有符合條件的近期事件。", line_user_id, emoji="📅") if _is_discord_user_key(line_user_id) else "目前沒有符合條件的近期事件。"
    text = f"{text}\n\n{_format_cache_status_text(cache_status)}"
    messages = [item for item in [_build_cache_status_flex(cache_status, "近期事件快取")] if item] + (messages or [])
    _store_last_event_index(line_user_id, ordered_groups)
    return _line_response(text, messages=messages or None)


def _sync_timeline_snapshot_for_user(user_id, line_user_id, logger):
    try:
        snapshot = fetch_timeline_snapshot(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_timeline_snapshot_fetch_failed error=%s", exc)
        return None, {}

    if snapshot:
        _sync_events_for_user(
            user_id,
            snapshot.get("courses") or {},
            calendar_events=snapshot.get("calendar_events") or [],
        )

    try:
        courses = (snapshot or {}).get("courses") or fetch_courses(make_user_key(line_user_id))
    except Exception:
        courses = {}
    return snapshot, courses


def _load_timeline_rows(user_id, line_user_id, logger, *, limit=50):
    snapshot, courses = _sync_timeline_snapshot_for_user(user_id, line_user_id, logger)
    rows = get_timeline_events(user_id, limit=limit)
    rows = _filter_active_homework_rows(rows, courses)
    return snapshot, courses, rows


def _rows_due_between(rows, start_dt, end_dt):
    filtered = []
    for row in rows:
        due_dt = _parse_due_at_sort_key(row["due_at"])
        if start_dt <= due_dt <= end_dt:
            filtered.append(row)
    return filtered


def _week_date_heading(value):
    dt = _normalize_due_at(value)
    if dt is None:
        return "未指定日期"
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    return f"{weekdays[dt.weekday()]} {dt.month}/{dt.day}"


def _build_week_messages(rows, line_user_id=None):
    ordered_rows = _timeline_rows_sorted(rows)
    if not ordered_rows:
        return None

    grouped = []
    current_key = None
    current_items = []
    for row in ordered_rows:
        dt = _normalize_due_at(row["due_at"])
        date_key = dt.date().isoformat() if dt else str(row["due_at"] or "")
        if date_key != current_key:
            if current_items:
                grouped.append((current_key, current_items))
            current_key = date_key
            current_items = [row]
        else:
            current_items.append(row)
    if current_items:
        grouped.append((current_key, current_items))

    sections = ["📅 **本週重點**", ""]
    for _date_key, items in grouped:
        heading = _week_date_heading(items[0]["due_at"])
        sections.extend(["━━━━━━━━━━━━", heading, "━━━━━━━━━━━━", ""])
        for row in items:
            payload = _event_payload(row)
            course_name = _course_name_for_display(row["course_name"] or row["course_id"] or "-")
            title = _event_title_for_display(row, payload)
            if row["event_type"] == "homework":
                icon = "📝"
            elif row["event_type"] == "exam":
                icon = "🧪"
            else:
                icon = "🗓️"
            sections.append(f"{icon} **{course_name}**")
            sections.append(f"• {title}")
            sections.append(f"• 截止：{_format_due_at_full(row['due_at'], line_user_id)}")
            sections.append("")

    while sections and sections[-1] == "":
        sections.pop()
    return "\n".join(sections)


def _today(line_user_id, logger):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    cache_status = get_cache_status(make_user_key(line_user_id))
    _snapshot, _courses, rows = _load_timeline_rows(user_id, line_user_id, logger, limit=50)
    if not rows:
        if _is_discord_user_key(line_user_id):
            text = f"🗓️ 目前還沒有可用的事件資料，先試試 {_discord_command_hint('e3 login', line_user_id)} 或 {_discord_command_hint('e3 relogin', line_user_id)}。"
            return _line_response(text, messages=[_build_text_summary_flex("今天重點", text, color="#0F766E", alt_text=text)])
        return "目前沒有可用事件資料，請先 `e3 login` 或 `e3 relogin`。"

    taipei_tz = timezone(timedelta(hours=8))
    now_local = datetime.now(taipei_tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)
    rows = _rows_due_between(
        rows,
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
    )

    if not rows:
        if _is_discord_user_key(line_user_id):
            text = "🗓️ 今天暫時沒有作業、考試或課程事件。"
            return _line_response(text, messages=[_build_text_summary_flex("今天重點", text, color="#0F766E", alt_text=text)])
        return "今天暫時沒有作業、考試或課程事件。"

    text, messages, ordered_groups = _build_timeline_messages(
        rows,
        "🗓️ **今天重點**" if _is_discord_user_key(line_user_id) else "🗓️ 今日重點：",
        line_user_id=line_user_id,
        use_triage=False,
    )
    if not text:
        if _is_discord_user_key(line_user_id):
            text = "🗓️ 今天暫時沒有作業、考試或課程事件。"
            return _line_response(text, messages=[_build_text_summary_flex("今天重點", text, color="#0F766E", alt_text=text)])
        return "今天暫時沒有作業、考試或課程事件。"
    text = f"{text}\n\n{_format_cache_status_text(cache_status)}"
    summary_flex = _build_text_summary_flex("今天重點", text, color="#0F766E", alt_text=text)
    messages = ([summary_flex] if summary_flex else []) + [item for item in [_build_cache_status_flex(cache_status, "今日事件快取")] if item] + (messages or [])
    _store_last_event_index(line_user_id, ordered_groups)
    return _line_response(text, messages=messages or None)


def _week(line_user_id, logger):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    cache_status = get_cache_status(make_user_key(line_user_id))
    _snapshot, _courses, rows = _load_timeline_rows(user_id, line_user_id, logger, limit=80)
    if not rows:
        if _is_discord_user_key(line_user_id):
            text = f"📅 目前還沒有可用的事件資料，先試試 {_discord_command_hint('e3 login', line_user_id)} 或 {_discord_command_hint('e3 relogin', line_user_id)}。"
            return _line_response(text, messages=[_build_text_summary_flex("本週重點", text, color="#1D4ED8", alt_text=text)])
        return "目前沒有可用事件資料，請先 `e3 login` 或 `e3 relogin`。"

    taipei_tz = timezone(timedelta(hours=8))
    now_local = datetime.now(taipei_tz)
    end_local = now_local + timedelta(days=7)
    rows = _rows_due_between(
        rows,
        now_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
    )

    if not rows:
        if _is_discord_user_key(line_user_id):
            text = "📅 未來 7 天暫時沒有作業、考試或課程事件。"
            return _line_response(text, messages=[_build_text_summary_flex("本週重點", text, color="#1D4ED8", alt_text=text)])
        return "未來 7 天暫時沒有作業、考試或課程事件。"

    text = _build_week_messages(rows, line_user_id if _is_discord_user_key(line_user_id) else None)
    if not text:
        if _is_discord_user_key(line_user_id):
            text = "📅 未來 7 天暫時沒有作業、考試或課程事件。"
            return _line_response(text, messages=[_build_text_summary_flex("本週重點", text, color="#1D4ED8", alt_text=text)])
        return "未來 7 天暫時沒有作業、考試或課程事件。"
    text = f"{text}\n\n{_format_cache_status_text(cache_status)}"
    summary_flex = _build_text_summary_flex("本週重點", text, color="#1D4ED8", alt_text=text)
    messages = ([summary_flex] if summary_flex else []) + [item for item in [_build_cache_status_flex(cache_status, "本週事件快取")] if item]
    return _line_response(text, messages=messages or None)


def _timeline(tokens, line_user_id, logger):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    event_type, filter_error = _parse_event_type_filter(tokens)
    if filter_error:
        return f"⚠️ {filter_error}"

    cache_status = get_cache_status(make_user_key(line_user_id))
    _snapshot, _courses, rows = _load_timeline_rows(user_id, line_user_id, logger, limit=50)
    if not rows:
        return _discord_empty_state(f"目前還沒有可用的時間軸資料，先試試 {_discord_command_hint('e3 login', line_user_id)} 或 {_discord_command_hint('e3 relogin', line_user_id)}。", line_user_id, emoji="🗓️") if _is_discord_user_key(line_user_id) else "目前沒有可用時間軸事件，請先 `e3 login` 或 `e3 relogin`。"
    rows = _filter_rows_within_days(rows, 30)
    text, messages, ordered_groups = _build_timeline_messages(
        rows,
        "🗓️ **學業時間軸（30 天內）**" if _is_discord_user_key(line_user_id) else "🗓️ E3 時間軸（30 天內）：",
        event_type=event_type,
        line_user_id=line_user_id,
    )
    if not text:
        return _discord_empty_state("目前沒有符合條件的時間軸事件。", line_user_id, emoji="📅") if _is_discord_user_key(line_user_id) else "目前沒有符合條件的時間軸事件。"
    text = f"{text}\n\n{_format_cache_status_text(cache_status)}"
    messages = [item for item in [_build_cache_status_flex(cache_status, "時間軸快取")] if item] + (messages or [])
    _store_last_event_index(line_user_id, ordered_groups)
    return _line_response(text, messages=messages or None)
