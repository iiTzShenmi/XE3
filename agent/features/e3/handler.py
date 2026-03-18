import json
import re
from datetime import datetime, timedelta, timezone
from logging import Logger
from typing import Any, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from .client import check_status, fetch_courses, fetch_file_links, fetch_timeline_snapshot, get_cache_status, login_and_sync, make_user_key
from .client import clear_runtime_data
from .db import (
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
    update_login_state,
    upsert_e3_account,
    upsert_event,
    upsert_user,
)
from .events import extract_events_from_fetch_all
from .file_proxy import build_proxy_url
from .secrets import decrypt_secret, encrypt_secret


ASYNC_ACTIONS = {"login", "relogin", "重新登入"}
_LAST_EVENT_INDEX = {}
EVENT_TYPE_ALIASES = {
    "作業": "homework",
    "homework": "homework",
    "hw": "homework",
    "行事曆": "calendar",
    "calendar": "calendar",
    "考試": "exam",
    "exam": "exam",
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
        return (
            "📘 E3 指令：\n"
            "1) e3 login <帳號> <密碼>\n"
            "2) e3 relogin / e3 refresh\n"
            "3) e3 logout\n"
            "4) e3 課程 / e3 course\n"
            "5) e3 近期 [作業/行事曆/考試]\n"
            "6) e3 timeline / e3 行事曆 [作業/行事曆/考試]\n"
            "7) e3 詳情 <編號>\n"
            "8) e3 狀態\n"
            "9) e3 grades / e3 成績\n"
            "10) e3 files <課名關鍵字>\n"
            "11) e3 remind show/on/off\n"
            "說明：課程指令會顯示目前學期（例如 114上 / 114下）"
        )

    if action_head == "狀態" or verb == "status":
        return _check_e3_status(line_user_id)

    if action.startswith("課程詳情") or (verb == "course" and len(tokens) >= 3 and tokens[1].lower() in {"detail", "details"}):
        return _course_detail(action, tokens, logger, line_user_id)

    if action_head == "課程" or verb in {"course", "courses"}:
        return _list_courses(logger, line_user_id)

    if action_head == "成績" or verb in {"grade", "grades"}:
        return _list_grades(logger, line_user_id)

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
            return "用法：e3 login <帳號> <密碼>"
        return "⏳ E3 登入已開始，正在驗證帳號並讀取首頁內容。完成後會再推播結果給你。"

    row = get_e3_account_by_user_id(user_id)
    if not row:
        return "找不到已綁定帳號，請先 `e3 login <帳號> <密碼>`。"
    return "⏳ E3 強制更新已開始，完成後會再推播最新同步結果給你。"


def _check_e3_status(line_user_id):
    user_key = make_user_key(line_user_id) if line_user_id else None
    runtime_status = check_status(user_key=user_key)
    if not runtime_status["available"]:
        return f"⚠️ E3 狀態：不可用\n找不到 E3 專案：{runtime_status['e3_root']}"

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
    reminder_schedule = _default_reminder_schedule()

    if login_status == "ok" and has_password and (has_cookie or has_home_html or has_courses):
        headline = "🟢 E3 狀態：已登入"
    elif login_status == "error":
        headline = "⚠️ E3 狀態：登入異常"
    else:
        headline = "🟡 E3 狀態：已綁定，尚未就緒"

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
        lines.append(f"最近錯誤：{last_error}")
    if not (has_password and (has_cookie or has_home_html or has_courses)):
        lines.append("建議：輸入 `e3 relogin` 或重新 `e3 login <帳號> <密碼>`。")
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
        return "E3 本地資料讀取失敗，請先 `e3 login <帳號> <密碼>` 或 `e3 relogin`。"

    if not isinstance(data, dict) or not data:
        return "目前沒有可用課程資料，請先 `e3 login <帳號> <密碼>`。"

    semester_tag = _current_semester_tag()
    current_courses = _current_semester_courses(data, semester_tag=semester_tag)

    if not current_courses:
        return f"目前找不到 {semester_tag} 學期課程，請先 `e3 relogin` 重新同步。"

    file_links = file_snapshot.get("file_links") or {}
    text_lines = [f"📚 你的 {semester_tag} 學期 E3 課程：", _format_cache_status_text(cache_status)]
    bubbles = []
    for idx, (display_name, payload) in enumerate(current_courses[:10], start=1):
        summary = _build_course_summary(idx, display_name, payload, file_links.get(str((payload or {}).get("_course_id") or "").strip()) or {})
        text_lines.append(f"{idx}. {summary['course_label']}")
        text_lines.append(f"   作業 {summary['homework_count']}｜成績 {summary['grade_count']}｜檔案 {summary['file_count']}")
        bubbles.append(_build_course_bubble(summary))

    messages = [_build_cache_status_flex(cache_status, "課程快取")]
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
        grades = payload.get("grades") or {}
        if not isinstance(grades, dict):
            continue
        course_id = str(payload.get("_course_id") or "").strip()
        course_name = _course_name_for_display(display_name)
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


def _format_grade_change_summary(changes):
    if not changes:
        return ""
    lines = ["📊 新成績："]
    for idx, item in enumerate(changes[:5], start=1):
        course_name = _shorten_course_name(item["course_name"], max_len=24)
        if item.get("old_score"):
            lines.append(f"{idx}. {course_name}｜{item['item_name']}：{item['old_score']} -> {item['score']}")
        else:
            lines.append(f"{idx}. {course_name}｜{item['item_name']}：{item['score']}")
    if len(changes) > 5:
        lines.append(f"另有 {len(changes) - 5} 筆更新。")
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
        return "E3 成績資料讀取失敗，請先 `e3 relogin`。"

    grade_items = extract_grade_items(data)
    if not grade_items:
        return "目前沒有可用成績資料。"
    grouped = _group_grade_items_by_course(grade_items)
    lines = ["📊 E3 成績：", _format_cache_status_text(cache_status)]
    bubbles = []
    for idx, course_group in enumerate(grouped[:10], start=1):
        lines.append(f"{idx}. {course_group['course_label']}")
        for item in course_group["items"][:3]:
            lines.append(f"   {item['item_name']}：{item['score']}")
        remaining = len(course_group["items"]) - 3
        if remaining > 0:
            lines.append(f"   ...另有 {remaining} 筆")
        bubbles.append(_build_grade_bubble(course_group))

    messages = [_build_cache_status_flex(cache_status, "成績快取")]
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


def _build_grade_bubble(course_group):
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

    return {
        "type": "bubble",
        "size": "kilo",
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
    }


def _matches_course_keyword(course_label, keyword):
    if not keyword:
        return True
    left = re.sub(r"\s+", "", str(course_label or "")).lower()
    right = re.sub(r"\s+", "", str(keyword or "")).lower()
    return right in left


def _assignment_items(payload):
    assignments = (payload or {}).get("assignments") or {}
    if isinstance(assignments, dict):
        items = assignments.get("assignments") or []
        return items if isinstance(items, list) else []
    if isinstance(assignments, list):
        return assignments
    return []


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
                submitted_files = item.get("submitted_files") or []
                if category not in {"in_progress", "upcoming"}:
                    continue
                if submitted_files:
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
        return "用法：e3 files <課名關鍵字>"

    try:
        snapshot = fetch_file_links(make_user_key(line_user_id))
        cache_status = get_cache_status(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_list_files_failed error=%s", exc)
        return "E3 檔案資料讀取失敗，請先 `e3 relogin`。"

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
        return f"找不到包含「{keyword}」的課程檔案，請先 `e3 relogin` 更新資料。"

    lines = [f"📎 與「{keyword}」相關的課程檔案：", _format_cache_status_text(cache_status)]
    bubbles = []
    for course_id, course_name, links in matches[:5]:
        all_files = _collect_file_entries(course_id, course_name, links)
        folder_groups = _group_file_entries(all_files)
        preview_lines = [f"{folder}｜{len(items)} 個檔案" for folder, items in folder_groups[:3]]
        remaining = max(0, len(folder_groups) - len(preview_lines))
        if remaining:
            preview_lines.append(f"還有 {remaining} 個資料夾，點「查看資料夾」查看。")
        if not preview_lines:
            preview_lines = ["目前沒有可用檔案"]
        lines.append(f"- {course_id} {course_name}".strip())
        for line in preview_lines:
            lines.append(f"  {line}")
        bubbles.append(_build_file_course_bubble(course_id, course_name, preview_lines))

    if not bubbles:
        return f"「{keyword}」目前沒有可用檔案連結。"

    messages = [_build_cache_status_flex(cache_status, "檔案快取")]
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


def _count_active_assignments(payload):
    count = 0
    for item in _assignment_items(payload):
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip().lower()
        submitted_files = item.get("submitted_files") or []
        if category and category not in {"in_progress", "upcoming"}:
            continue
        if submitted_files:
            continue
        count += 1
    return count


def _count_grade_items(payload):
    grades = (payload or {}).get("grades") or {}
    if not isinstance(grades, dict):
        return 0
    return sum(1 for score in grades.values() if _is_meaningful_grade(score))


def _count_file_items(link_payload):
    handouts = link_payload.get("handouts") or []
    assignments = link_payload.get("assignments") or {}
    assignment_count = 0
    for entry in assignments.values():
        assignment_count += len((entry or {}).get("web_files") or [])
    return len(handouts) + assignment_count


def _sanitize_line_uri(url):
    raw = str(url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    safe_path = quote(parts.path, safe="/:@-._~!$&'()*+,;=")
    safe_query = quote(parts.query, safe="=&/:?@-._~!$'()*+,;")
    safe_fragment = quote(parts.fragment, safe="-._~!$&'()*+,;=:@/?")
    return urlunsplit((parts.scheme, parts.netloc, safe_path, safe_query, safe_fragment))


def _collect_file_entries(course_id, course_name, links):
    entries = []
    for item in links.get("handouts") or []:
        entries.append(
            {
                "course_id": course_id,
                "course_name": course_name,
                "folder": item.get("folder") or "講義",
                "kind": "講義",
                "title": item.get("name") or "未命名檔案",
                "source_url": _sanitize_line_uri(item.get("url") or ""),
                "accent": "#2563EB",
            }
        )
    for assignment_title, entry in (links.get("assignments") or {}).items():
        for web_file in (entry or {}).get("web_files") or []:
            entries.append(
                {
                    "course_id": course_id,
                    "course_name": course_name,
                    "folder": assignment_title or "作業附件",
                    "kind": "作業附件",
                    "title": f"{assignment_title} / {web_file.get('name') or '附件'}",
                    "source_url": _sanitize_line_uri(web_file.get("url") or ""),
                    "accent": "#D97706",
                }
            )
    return entries


def _group_file_entries(entries):
    groups = {}
    for entry in entries:
        folder = str(entry.get("folder") or "未分類").strip()
        groups.setdefault(folder, []).append(entry)
    ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    return ordered


def _build_course_summary(index, display_name, payload, link_payload):
    course_id = str((payload or {}).get("_course_id") or "").strip()
    course_name = _course_name_for_display(display_name)
    course_label = f"{course_id} {course_name}".strip()
    return {
        "index": index,
        "course_id": course_id,
        "course_name": course_name,
        "course_label": course_label,
        "homework_count": _count_active_assignments(payload),
        "grade_count": _count_grade_items(payload),
        "file_count": _count_file_items(link_payload or {}),
    }


def _build_course_bubble(summary):
    return {
        "type": "bubble",
        "size": "kilo",
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
                        "label": "查看詳情",
                        "text": f"e3 課程詳情 {summary['index']}",
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "action": {
                        "type": "message",
                        "label": "查看資料夾",
                        "text": f"e3 檔案資料夾 {summary['course_id'] or summary['course_name']}",
                    },
                },
            ],
        },
    }


def _build_file_course_bubble(course_id, course_name, preview_lines):
    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1D4ED8",
            "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": "檔案", "color": "#DBEAFE", "size": "xs"},
                {"type": "text", "text": course_name, "color": "#FFFFFF", "weight": "bold", "wrap": True},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {"type": "text", "text": course_id or "未提供課號", "size": "sm", "color": "#475569"},
                *[
                    {"type": "text", "text": line, "size": "sm", "wrap": True, "color": "#334155"}
                    for line in preview_lines[:4]
                ],
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "color": "#1D4ED8",
                    "action": {
                        "type": "message",
                        "label": "查看資料夾",
                        "text": f"e3 檔案資料夾 {course_id or course_name}",
                    },
                }
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
    lines = []
    user_name = preview.get("user_name") or ""
    user_email = preview.get("user_email") or ""
    if user_name:
        lines.append(f"👤 姓名：{user_name}")
    if user_email:
        lines.append(f"📧 Email：{user_email}")
    if not lines:
        lines.append("👤 姓名：未取得")
        lines.append("📧 Email：未取得")
    return "\n".join(lines)


def _current_semester_tag(now=None):
    taipei_tz = timezone(timedelta(hours=8))
    now = now or datetime.now(taipei_tz)
    year = now.year
    month = now.month

    if month >= 9:
        roc_year = year - 1911
        term = "上"
    elif month == 1:
        roc_year = year - 1912
        term = "上"
    elif 2 <= month <= 6:
        roc_year = year - 1912
        term = "下"
    else:
        roc_year = year - 1911
        term = "上"

    return f"{roc_year}{term}"


def _extract_semester_tag(display_name):
    match = re.match(r"^(\d{2,3}[上下])", (display_name or "").strip())
    return match.group(1) if match else None


def _strip_semester_prefix(display_name):
    cleaned = re.sub(r"^\d{2,3}[上下]", "", (display_name or "").strip())
    cleaned = cleaned.replace("_", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _course_name_for_display(course_name):
    text = _strip_semester_prefix(course_name) if course_name else "-"
    matches = list(re.finditer(r"[\u4e00-\u9fff]", text))
    if matches:
        end = matches[-1].end()
        while end < len(text) and text[end] in ")）】] ":
            end += 1
        text = text[:end]
    text = re.sub(r"\s+", " ", text).strip(" -_|,")
    return text or "-"


def _shorten_course_name(course_name, max_len=28):
    text = _course_name_for_display(course_name)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _shorten_title(title, max_len=32):
    text = re.sub(r"\s+", " ", (title or "").strip())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _format_due_at_for_display(value):
    if not value:
        return "N/A"

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)

    taipei_tz = timezone(timedelta(hours=8))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=taipei_tz)
    else:
        dt = dt.astimezone(taipei_tz)
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    return dt.strftime("%m/%d") + f" ({weekdays[dt.weekday()]}) " + dt.strftime("%H:%M")


def _format_event_type_label(event_type):
    mapping = {
        "calendar": "行事曆",
        "homework": "作業",
        "exam": "考試",
    }
    return mapping.get(event_type, event_type)


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


def _timeline_heading(event_type):
    section_emoji = {
        "exam": "🧪",
        "homework": "📝",
        "calendar": "🗓️",
    }.get(event_type, "📌")
    return f"{section_emoji} 【{_format_event_type_label(event_type)}】"


def _line_response(text, messages=None):
    payload = {"text": text}
    if messages:
        payload["messages"] = messages
    return payload


def _format_cache_status_text(cache_status):
    if not cache_status or not cache_status.get("exists"):
        return "🕒 Cache: unavailable. Use Force Update to sync from E3."

    age_minutes = int(cache_status.get("age_minutes") or 0)
    ttl_minutes = int(cache_status.get("ttl_minutes") or 15)
    if cache_status.get("is_fresh"):
        return f"🕒 Cache: {age_minutes} min old, serving local snapshot instantly."
    return f"⚠️ Cache: {age_minutes} min old, older than {ttl_minutes} min. Tap Force Update for fresh E3 data."


def _build_cache_status_flex(cache_status, title):
    if not cache_status or not cache_status.get("exists"):
        header_text = "No local cache yet"
        body_text = "Use Force Update to fetch the latest data from E3."
        accent = "#B45309"
    elif cache_status.get("is_fresh"):
        age_minutes = int(cache_status.get("age_minutes") or 0)
        header_text = f"Fresh cache · {age_minutes} min old"
        body_text = "This response is served from local data for faster performance."
        accent = "#15803D"
    else:
        age_minutes = int(cache_status.get("age_minutes") or 0)
        ttl_minutes = int(cache_status.get("ttl_minutes") or 15)
        header_text = f"Stale cache · {age_minutes} min old"
        body_text = f"Older than the {ttl_minutes}-minute freshness window. Force Update if you need real-time data."
        accent = "#B45309"

    return {
        "type": "flex",
        "altText": f"{title}: {header_text}",
        "contents": {
            "type": "bubble",
            "size": "kilo",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": accent,
                "paddingAll": "12px",
                "contents": [
                    {"type": "text", "text": title, "color": "#FFFFFF", "size": "xs"},
                    {"type": "text", "text": header_text, "color": "#FFFFFF", "weight": "bold", "wrap": True},
                ],
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {"type": "text", "text": body_text, "size": "sm", "wrap": True, "color": "#334155"},
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "button",
                        "style": "primary",
                        "height": "sm",
                        "color": accent,
                        "action": {
                            "type": "message",
                            "label": "Force Update",
                            "text": "e3 refresh",
                        },
                    }
                ],
            },
        },
    }


def _store_last_event_index(line_user_id, ordered_groups):
    if not line_user_id:
        return
    mapping = {}
    for _, items in ordered_groups:
        for idx, row in items:
            event_uid = row["event_uid"] if isinstance(row, dict) else row["event_uid"]
            if event_uid:
                mapping[idx] = event_uid
    _LAST_EVENT_INDEX[line_user_id] = mapping


def _format_reminder_summary(enabled, schedule, timezone_name="Asia/Taipei"):
    return (
        "⏰ E3 提醒設定\n"
        f"狀態：{'開啟' if enabled else '關閉'}\n"
        f"時區：{timezone_name}\n"
        f"時段：{', '.join(schedule) if schedule else '未設定'}\n"
        "提醒時間固定為每天 09:00 與 21:00，可直接點按按鈕開關。"
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
                        "text": "每天固定時段自動推送近期事件",
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
                                "text": f"狀態｜{status_text}",
                                "weight": "bold",
                                "color": status_color,
                                "size": "sm",
                            },
                            {
                                "type": "text",
                                "text": "時區｜Asia/Taipei",
                                "size": "xs",
                                "color": "#475569",
                            },
                            {
                                "type": "text",
                                "text": f"時段｜{schedule_text}",
                                "size": "sm",
                                "wrap": True,
                                "color": "#0F172A",
                            },
                        ],
                    },
                    {
                        "type": "text",
                        "text": "快速切換",
                        "weight": "bold",
                        "size": "sm",
                        "color": "#334155",
                    },
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "spacing": "sm",
                        "contents": [
                            _button("開啟", "e3 remind on", style="primary", color="#15803D"),
                            _button("關閉", "e3 remind off", style="primary", color="#B91C1C"),
                        ],
                    },
                    {
                        "type": "text",
                        "text": "提醒時間",
                        "weight": "bold",
                        "size": "sm",
                        "color": "#334155",
                    },
                    {
                        "type": "text",
                        "text": "每天固定推送兩次：09:00、21:00",
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


def _build_timeline_flex(rows, alt_text, hero_title, event_type=None):
    bubbles = []
    accent = {
        "exam": "#B22222",
        "homework": "#D97706",
        "calendar": "#2563EB",
    }.get(event_type, "#4B5563")
    for idx, row in rows[:10]:
        due_at = _format_due_at_for_display(row["due_at"])
        course_name = _course_name_for_display(row["course_name"] or row["course_id"] or "-")
        title = _shorten_title(row["title"], max_len=44)
        bubbles.append(
            {
                "type": "bubble",
                "size": "kilo",
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": accent,
                    "paddingAll": "12px",
                    "contents": [
                        {
                            "type": "text",
                            "text": hero_title,
                            "color": "#FFFFFF",
                            "weight": "bold",
                            "size": "sm",
                        },
                        {
                            "type": "text",
                            "text": due_at,
                            "color": "#FFFFFF",
                            "size": "xs",
                            "margin": "sm",
                        },
                    ],
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "contents": [
                        {
                            "type": "text",
                            "text": course_name,
                            "weight": "bold",
                            "size": "md",
                            "wrap": True,
                        },
                        {
                            "type": "text",
                            "text": title,
                            "size": "sm",
                            "wrap": True,
                            "color": "#374151",
                        },
                        {
                            "type": "text",
                            "text": f"編號 #{idx}",
                            "size": "xs",
                            "color": "#6B7280",
                        },
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
                            "color": accent,
                            "action": {
                                "type": "message",
                                "label": "查看詳情",
                                "text": f"e3 詳情 {idx}",
                            },
                        }
                    ],
                },
            }
        )

    if not bubbles:
        return None

    return {
        "type": "flex",
        "altText": alt_text,
        "contents": {
            "type": "carousel",
            "contents": bubbles,
        },
    }


def _build_detail_flex(row, index, alt_text):
    payload = {}
    payload_json = row["payload_json"] or ""
    if payload_json:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            payload = {}

    action_buttons = [
        {
            "type": "button",
            "style": "primary",
            "height": "sm",
            "color": "#2563EB",
            "action": {
                "type": "message",
                "label": "回到時間軸",
                "text": "e3 timeline",
            },
        }
    ]
    if payload.get("url"):
        action_buttons.append(
            {
                "type": "button",
                "style": "link",
                "height": "sm",
                "action": {
                    "type": "uri",
                    "label": "開啟 E3",
                    "uri": payload["url"],
                },
            }
        )

    return {
        "type": "flex",
        "altText": alt_text,
        "contents": {
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#2563EB",
                "paddingAll": "12px",
                "contents": [
                    {
                        "type": "text",
                        "text": f"事件詳情 #{index}",
                        "color": "#FFFFFF",
                        "weight": "bold",
                        "size": "md",
                    }
                ],
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {"type": "text", "text": _format_event_type_label(row["event_type"]), "size": "sm", "color": "#6B7280"},
                    {"type": "text", "text": _course_name_for_display(row["course_name"] or row["course_id"] or "-"), "weight": "bold", "wrap": True},
                    {"type": "text", "text": row["title"], "wrap": True, "size": "sm"},
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": f"截止：{_format_due_at_full(row['due_at'])}", "size": "sm", "wrap": True},
                    {"type": "text", "text": f"顯示日期：{payload.get('date_label') or '-'}", "size": "sm", "wrap": True},
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": action_buttons,
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


def _collect_course_homework_items(payload):
    items = []
    for item in _assignment_items(payload):
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip().lower()
        submitted_files = item.get("submitted_files") or []
        if category and category not in {"in_progress", "upcoming"}:
            continue
        if submitted_files:
            continue
        due_raw = item.get("due") or item.get("due_date") or item.get("deadline") or item.get("截止")
        items.append(
            {
                "title": str(item.get("title") or item.get("name") or "未命名作業").strip(),
                "due_at": due_raw,
            }
        )
    return items[:3]


def _build_course_detail_flex(detail, alt_text):
    body_contents = [
        {"type": "text", "text": detail["course_name"], "weight": "bold", "wrap": True, "size": "lg"},
        {"type": "text", "text": detail["course_id"] or "未提供課號", "size": "sm", "color": "#475569"},
        {"type": "separator", "margin": "md"},
        {"type": "text", "text": f"作業：{detail['homework_count']}　行事曆：{detail['calendar_count']}　檔案：{detail['file_count']}", "size": "sm", "wrap": True},
    ]
    if detail["homework_lines"]:
        body_contents.append({"type": "text", "text": "作業", "weight": "bold", "size": "sm", "margin": "md"})
        for line in detail["homework_lines"]:
            body_contents.append({"type": "text", "text": line, "size": "sm", "wrap": True})
    if detail["calendar_lines"]:
        body_contents.append({"type": "text", "text": "行事曆", "weight": "bold", "size": "sm", "margin": "md"})
        for line in detail["calendar_lines"]:
            body_contents.append({"type": "text", "text": line, "size": "sm", "wrap": True})
    if detail["file_lines"]:
        body_contents.append({"type": "text", "text": "檔案", "weight": "bold", "size": "sm", "margin": "md"})
        for line in detail["file_lines"]:
            body_contents.append({"type": "text", "text": line, "size": "sm", "wrap": True})

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
                "paddingAll": "12px",
                "contents": [
                    {"type": "text", "text": "課程詳情", "color": "#FFFFFF", "weight": "bold", "size": "md"},
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
                        "style": "secondary",
                        "height": "sm",
                        "action": {
                            "type": "message",
                            "label": "查看資料夾",
                            "text": f"e3 檔案資料夾 {detail['course_id'] or detail['course_name']}",
                        },
                    },
                    {
                        "type": "button",
                        "style": "primary",
                        "height": "sm",
                        "color": "#0F766E",
                        "action": {
                            "type": "message",
                            "label": "回到課程列表",
                            "text": "e3 course",
                        },
                    }
                ],
            },
        },
    }


def _format_due_at_full(value):
    if not value:
        return "N/A"

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)

    taipei_tz = timezone(timedelta(hours=8))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=taipei_tz)
    else:
        dt = dt.astimezone(taipei_tz)
    return dt.strftime("%Y/%m/%d %H:%M")


def _extract_detail_index(action, tokens):
    if len(tokens) >= 2 and tokens[1].isdigit():
        return int(tokens[1])

    match = re.match(r"^詳情\s*(\d+)$", action.strip())
    if match:
        return int(match.group(1))
    return None


def _format_event_detail(row, index):
    payload = {}
    payload_json = row["payload_json"] or ""
    if payload_json:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            payload = {}

    lines = [f"🔎 事件詳情 #{index}"]
    lines.append(f"類型：{_format_event_type_label(row['event_type'])}")
    lines.append(f"課程：{_course_name_for_display(row['course_name'] or row['course_id'] or '-')}")
    lines.append(f"標題：{row['title']}")
    lines.append(f"截止：{_format_due_at_full(row['due_at'])}")

    date_label = payload.get("date_label")
    if date_label:
        lines.append(f"顯示日期：{date_label}")

    url = payload.get("url")
    if url:
        lines.append(f"連結：{url}")

    event_id = payload.get("event_id")
    if event_id:
        lines.append(f"事件 ID：{event_id}")

    return "\n".join(lines)


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
    grouped_rows = {"exam": [], "homework": [], "calendar": []}
    for row in rows:
        grouped_rows.setdefault(row["event_type"], []).append(row)

    section_order = ["exam", "homework", "calendar"]
    ordered = []
    display_index = 1
    for event_type in section_order:
        items = grouped_rows.get(event_type) or []
        section_items = []
        for row in items:
            section_items.append((display_index, row))
            display_index += 1
        ordered.append((event_type, section_items))
    return ordered


def _filter_rows_by_event_type(rows, event_type):
    if not event_type:
        return rows
    return [row for row in rows if row["event_type"] == event_type]


def _build_timeline_messages(rows, header, event_type=None):
    filtered_rows = _filter_rows_by_event_type(rows, event_type)
    if not filtered_rows:
        return None, [], []

    ordered_groups = _build_timeline_display_groups(filtered_rows)
    text_sections = []
    messages = []
    for group_event_type, items in ordered_groups:
        if not items:
            continue
        if not text_sections:
            section_lines = [header, _timeline_heading(group_event_type)]
        else:
            section_lines = [_timeline_heading(group_event_type)]
        for idx, row in items:
            due_at = _format_due_at_for_display(row["due_at"])
            course_name = _shorten_course_name(row["course_name"] or row["course_id"] or "-")
            title = _shorten_title(row["title"])
            icon = "👉" if group_event_type == "homework" else "📍"
            section_lines.append(f"{idx}. {due_at} ｜{course_name}")
            section_lines.append(f"   {icon} {title}")
            section_lines.append("")
        if section_lines[-1] == "":
            section_lines.pop()
        section_text = "\n".join(section_lines)
        text_sections.append(section_text)
        flex = _build_timeline_flex(items, section_text, _timeline_heading(group_event_type), event_type=group_event_type)
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

    text = _format_event_detail(row, index)
    flex = _build_detail_flex(row, index, text)
    return _line_response(text, messages=[flex] if flex else None)


def _course_detail(action, tokens, logger, line_user_id):
    _, err = _require_line_user(line_user_id)
    if err:
        return err

    index = _extract_course_index(action, tokens)
    if index is None or index <= 0:
        return "用法：e3 課程詳情 <編號>"

    try:
        courses = fetch_courses(make_user_key(line_user_id))
        timeline_snapshot = fetch_timeline_snapshot(make_user_key(line_user_id))
        file_snapshot = fetch_file_links(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_course_detail_failed error=%s", exc)
        return "課程詳情讀取失敗，請先 `e3 relogin`。"

    semester_tag = _current_semester_tag()
    current_courses = _current_semester_courses(courses, semester_tag=semester_tag)

    if index > len(current_courses):
        return f"找不到第 {index} 門課程，請先輸入 `e3 course` 確認編號。"

    display_name, payload = current_courses[index - 1]
    course_id = str((payload or {}).get("_course_id") or "").strip()
    course_name = _course_name_for_display(display_name)
    links = (file_snapshot.get("file_links") or {}).get(course_id) or {}
    homework_items = _collect_course_homework_items(payload)
    calendar_items = _collect_course_calendar_events(timeline_snapshot, course_id)
    all_file_entries = _collect_file_entries(course_id, course_name, links)
    file_lines = [f"{entry['kind']}｜{entry['title']}" for entry in all_file_entries[:3]]
    remaining_files = len(all_file_entries) - len(file_lines)
    if remaining_files > 0:
        file_lines.append(f"還有 {remaining_files} 個檔案，點「查看資料夾」查看。")

    detail = {
        "course_id": course_id,
        "course_name": course_name,
        "homework_count": _count_active_assignments(payload),
        "calendar_count": len(calendar_items),
        "file_count": _count_file_items(links),
        "homework_lines": [
            f"{_shorten_title(item['title'], 26)}｜{_format_due_at_for_display(item['due_at'])}"
            for item in homework_items
        ] or ["目前沒有未完成作業"],
        "calendar_lines": [
            f"{_shorten_title(item['title'], 26)}｜{_format_due_at_for_display(item['due_at'])}"
            for item in calendar_items
        ] or ["目前沒有近期行事曆"],
        "file_lines": file_lines or ["目前沒有可用檔案"],
    }

    text_lines = [
        f"📘 課程詳情 #{index}",
        f"課程：{course_id} {course_name}".strip(),
        f"作業：{detail['homework_count']}｜行事曆：{detail['calendar_count']}｜檔案：{detail['file_count']}",
        "作業：" + ("；".join(detail["homework_lines"]) if detail["homework_lines"] else "-"),
        "行事曆：" + ("；".join(detail["calendar_lines"]) if detail["calendar_lines"] else "-"),
        "檔案：" + ("；".join(detail["file_lines"]) if detail["file_lines"] else "-"),
    ]
    text = "\n".join(text_lines)
    flex = _build_course_detail_flex(detail, text)
    return _line_response(text, messages=[flex] if flex else None)


def _build_file_download_flex(entries, alt_text, course_name):
    bubbles = []
    for entry in entries:
        if entry.get("_nav"):
            bubbles.append(entry["_nav"])
            continue
        if not entry.get("url"):
            continue
        bubbles.append(
            {
                "type": "bubble",
                "size": "kilo",
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": entry["accent"],
                    "paddingAll": "12px",
                    "contents": [
                        {"type": "text", "text": entry["kind"], "color": "#FFFFFF", "weight": "bold", "size": "sm"},
                        {"type": "text", "text": course_name, "color": "#FFFFFF", "size": "xs", "wrap": True, "margin": "sm"},
                    ],
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": entry["title"], "wrap": True, "size": "sm"},
                    ],
                },
                "footer": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "height": "sm",
                            "color": entry["accent"],
                            "action": {
                                "type": "uri",
                                "label": "開啟檔案",
                                "uri": entry["url"],
                            },
                        }
                    ],
                },
            }
        )
    if not bubbles:
        return None
    return {
        "type": "flex",
        "altText": alt_text,
        "contents": {
            "type": "carousel",
            "contents": bubbles,
        },
    }


def _build_file_folder_bubble(course_key, folder_name, file_count, index):
    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1D4ED8",
            "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": "資料夾", "color": "#DBEAFE", "size": "xs"},
                {"type": "text", "text": folder_name, "color": "#FFFFFF", "weight": "bold", "wrap": True},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": f"{file_count} 個檔案", "size": "sm", "color": "#334155"},
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "color": "#1D4ED8",
                    "action": {
                        "type": "message",
                        "label": "查看檔案",
                        "text": f"e3 檔案詳情 {course_key} f{index}",
                    },
                }
            ],
        },
    }


def _build_file_nav_bubble(course_key, page, total_pages):
    contents = []
    if page > 1:
        contents.append(
            {
                "type": "button",
                "style": "secondary",
                "height": "sm",
                "action": {
                    "type": "message",
                    "label": "上一頁",
                    "text": f"e3 檔案詳情 {course_key} p{page - 1}",
                },
            }
        )
    if page < total_pages:
        contents.append(
            {
                "type": "button",
                "style": "primary",
                "height": "sm",
                "color": "#1D4ED8",
                "action": {
                    "type": "message",
                    "label": "下一頁",
                    "text": f"e3 檔案詳情 {course_key} p{page + 1}",
                },
            }
        )
    if not contents:
        return None
    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0F172A",
            "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": f"第 {page}/{total_pages} 頁", "color": "#FFFFFF", "weight": "bold", "size": "sm"},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "檔案太多時，請用分頁查看。", "size": "sm", "wrap": True},
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": contents,
        },
    }


def _file_page_size(user_id):
    user_key = str(user_id or "")
    if user_key.startswith("discord:"):
        return 25
    return 8


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
    entries = _collect_file_entries(course_id, course_name, links)
    if not entries:
        return f"{course_name} 目前沒有可下載檔案。"

    folder_groups = _group_file_entries(entries)
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
    groups = _group_file_entries(_collect_file_entries(course_id, course_name, links))
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
        schedule = _default_reminder_schedule()
        text = _format_reminder_summary(bool(prefs["enabled"]), schedule, prefs["timezone"])
        flex = _build_reminder_settings_flex(bool(prefs["enabled"]), schedule, text)
        return _line_response(text, messages=[flex] if flex else None)

    if subcommand in {"on", "開啟"}:
        update_reminder_enabled(user_id, True)
        prefs = get_reminder_prefs(user_id)
        schedule = _default_reminder_schedule()
        text = "✅ 已開啟 E3 自動提醒。\n\n" + _format_reminder_summary(True, schedule, prefs["timezone"])
        flex = _build_reminder_settings_flex(True, schedule, text)
        return _line_response(text, messages=[flex] if flex else None)

    if subcommand in {"off", "關閉"}:
        update_reminder_enabled(user_id, False)
        prefs = get_reminder_prefs(user_id)
        schedule = _default_reminder_schedule()
        text = "🛑 已關閉 E3 自動提醒。\n\n" + _format_reminder_summary(False, schedule, prefs["timezone"])
        flex = _build_reminder_settings_flex(False, schedule, text)
        return _line_response(text, messages=[flex] if flex else None)

    return "⚠️ 用法：`e3 remind show`、`e3 remind on`、`e3 remind off`"


def _login(action, logger, line_user_id):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    tokens = action.split()
    if len(tokens) < 3:
        return "用法：e3 login <帳號> <密碼>"

    account = tokens[1].strip()
    password = tokens[2].strip()

    try:
        result = login_and_sync(account, password, make_user_key(line_user_id), update_data=True, update_links=True)
        courses = result["courses"]
        calendar_events = result.get("calendar_events") or []
        preview = result["home_preview"]
        events = _sync_events_for_user(user_id, courses, calendar_events=calendar_events)
        grade_changes = sync_grade_items(user_id, courses)
        upsert_e3_account(user_id, account, encrypt_secret(password), status="ok", error=None)
        reply = (
            "✅ E3 登入成功。\n"
            f"已同步課程：{len(courses)} 門，時間軸事件：{len(events)} 筆。\n"
            f"{_format_home_preview(preview)}"
        )
        grade_summary = _format_grade_change_summary(grade_changes)
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
        return "找不到已綁定帳號，請先 `e3 login <帳號> <密碼>`。"

    account = row["e3_account"]
    encrypted_password = row["encrypted_password"]
    if not encrypted_password:
        return "找不到已儲存密碼，請重新執行 `e3 login <帳號> <密碼>`。"

    try:
        password = decrypt_secret(encrypted_password)
        result = login_and_sync(account, password, make_user_key(line_user_id), update_data=True, update_links=True)
        courses = result["courses"]
        calendar_events = result.get("calendar_events") or []
        preview = result["home_preview"]
        events = _sync_events_for_user(user_id, courses, calendar_events=calendar_events)
        grade_changes = sync_grade_items(user_id, courses)
        update_login_state(user_id, "ok", None)
        reply = (
            "✅ E3 重新登入成功。\n"
            f"已同步課程：{len(courses)} 門，時間軸事件：{len(events)} 筆。\n"
            f"{_format_home_preview(preview)}"
        )
        grade_summary = _format_grade_change_summary(grade_changes)
        if grade_summary:
            reply += "\n" + grade_summary
        return reply
    except Exception as exc:
        logger.error("e3_relogin_failed error=%s", exc)
        update_login_state(user_id, "error", str(exc))
        if "Exceeded 30 redirects" in str(exc):
            return _format_e3_error(exc)
        return "E3 重新登入失敗，請重新輸入 `e3 login <帳號> <密碼>`。"


def _logout(line_user_id):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    delete_user_data(user_id)
    clear_runtime_data(make_user_key(line_user_id))
    return "🧹 E3 已登出，並清除本地綁定、事件快取與登入工作目錄。"


def _upcoming(tokens, line_user_id):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    event_type, filter_error = _parse_event_type_filter(tokens)
    if filter_error:
        return f"⚠️ {filter_error}"

    rows = get_upcoming_events(user_id, limit=10)
    if not rows:
        return "目前沒有近期事件，請先 `e3 login` 或 `e3 relogin` 進行同步。"
    cache_status = get_cache_status(make_user_key(line_user_id))
    try:
        courses = fetch_courses(make_user_key(line_user_id))
    except Exception:
        courses = {}
    rows = _filter_active_homework_rows(rows, courses)
    if event_type == "homework" and not rows:
        return "目前沒有未繳且尚未過期的作業。"
    text, messages, ordered_groups = _build_timeline_messages(rows, "⏰ 近期提醒（前 10 筆）：", event_type=event_type)
    if not text:
        return "目前沒有符合條件的近期事件。"
    text = f"{text}\n\n{_format_cache_status_text(cache_status)}"
    messages = [_build_cache_status_flex(cache_status, "近期事件快取")] + (messages or [])
    _store_last_event_index(line_user_id, ordered_groups)
    return _line_response(text, messages=messages or None)


def _timeline(tokens, line_user_id, logger):
    user_id, err = _require_line_user(line_user_id)
    if err:
        return err

    event_type, filter_error = _parse_event_type_filter(tokens)
    if filter_error:
        return f"⚠️ {filter_error}"

    try:
        snapshot = fetch_timeline_snapshot(make_user_key(line_user_id))
    except Exception as exc:
        logger.error("e3_timeline_fetch_failed error=%s", exc)
        snapshot = None

    if snapshot:
        _sync_events_for_user(
            user_id,
            snapshot.get("courses") or {},
            calendar_events=snapshot.get("calendar_events") or [],
        )

    cache_status = get_cache_status(make_user_key(line_user_id))

    rows = get_timeline_events(user_id, limit=20)
    if not rows:
        return "目前沒有可用時間軸事件，請先 `e3 login` 或 `e3 relogin`。"
    try:
        courses = (snapshot or {}).get("courses") or fetch_courses(make_user_key(line_user_id))
    except Exception:
        courses = {}
    rows = _filter_active_homework_rows(rows, courses)
    text, messages, ordered_groups = _build_timeline_messages(rows, "🗓️ E3 時間軸（前 20 筆）：", event_type=event_type)
    if not text:
        return "目前沒有符合條件的時間軸事件。"
    text = f"{text}\n\n{_format_cache_status_text(cache_status)}"
    messages = [_build_cache_status_flex(cache_status, "時間軸快取")] + (messages or [])
    _store_last_event_index(line_user_id, ordered_groups)
    return _line_response(text, messages=messages or None)
