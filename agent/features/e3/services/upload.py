from __future__ import annotations

import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..scraper import config
from ..scraper.get_course.get_user_data import build_authenticated_session
from ..utils.common import (
    assignment_items,
    course_name_for_display,
    current_semester_tag,
    extract_semester_tag,
    is_assignment_completed,
    matches_course_keyword,
)
from .client import fetch_courses, get_runtime_root, make_user_key


E3_ASSIGN_VIEW_URL = f"{config.E3_BASE_URL}/mod/assign/view.php"
E3_REPOSITORY_AJAX_URL = f"{config.E3_BASE_URL}/repository/repository_ajax.php?action=upload"
DEFAULT_UPLOAD_REPO_ID = "5"
DEFAULT_MAX_BYTES = "1073741824"
DEFAULT_AREA_MAX_BYTES = "-1"


class E3UploadError(Exception):
    """User-facing E3 upload failure."""


@dataclass(frozen=True)
class AssignmentTarget:
    course_id: str
    course_name: str
    title: str
    cmid: str
    detail_url: str
    due_at: str
    category: str
    completed: bool
    submitted_count: int

    @property
    def value(self) -> str:
        return f"{self.course_id}:{self.cmid}"


@dataclass(frozen=True)
class UploadResult:
    course_id: str
    course_name: str
    assignment_title: str
    cmid: str
    filename: str
    submitted_file_count: int
    replaced_existing: bool


def _runtime_cookie_file(line_user_id: str) -> Path:
    return get_runtime_root() / make_user_key(line_user_id) / "cookies.json"


def _load_cookie_dict(line_user_id: str) -> dict[str, str]:
    cookie_file = _runtime_cookie_file(line_user_id)
    if not cookie_file.exists():
        raise E3UploadError("找不到 E3 session，請先執行 `/e3 login` 或 `/e3 relogin`。")
    try:
        data = json.loads(cookie_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise E3UploadError("E3 session 檔案讀取失敗，請先重新登入。") from exc
    if not isinstance(data, dict):
        raise E3UploadError("E3 session 格式異常，請先重新登入。")
    return {str(key): str(value) for key, value in data.items() if value}


def _authenticated_session(line_user_id: str) -> requests.Session:
    cookies = _load_cookie_dict(line_user_id)
    session = build_authenticated_session(cookies)
    session.headers.update(
        {
            "Origin": config.E3_BASE_URL,
            "Referer": config.E3_BASE_URL + "/",
        }
    )
    return session


def _needs_login(response: requests.Response) -> bool:
    url = str(response.url or "").lower()
    text = response.text or ""
    return "login" in url or "登入本網站" in text or 'id="loginbtn"' in text


def _assignment_cmid(url: str | None) -> str:
    parsed = urlparse(str(url or ""))
    values = parse_qs(parsed.query).get("id") or []
    return str(values[0]).strip() if values else ""


def _course_matches(course_id: str, course_name: str, keyword: str) -> bool:
    text = str(keyword or "").strip()
    if not text:
        return True
    if text == course_id:
        return True
    return matches_course_keyword(f"{course_id} {course_name}", text)


def list_assignment_targets(line_user_id: str, course_keyword: str = "", *, include_completed: bool = True) -> list[AssignmentTarget]:
    courses = fetch_courses(make_user_key(line_user_id))
    semester_tag = current_semester_tag()
    targets: list[AssignmentTarget] = []

    for display_name, payload in (courses or {}).items():
        if extract_semester_tag(display_name) != semester_tag:
            continue
        if not isinstance(payload, dict):
            continue
        course_id = str(payload.get("_course_id") or "").strip()
        course_name = course_name_for_display(display_name)
        if not course_id or not _course_matches(course_id, course_name, course_keyword):
            continue

        for item in assignment_items(payload):
            if not isinstance(item, dict):
                continue
            detail_url = str(item.get("detail_url") or item.get("url") or "").strip()
            cmid = _assignment_cmid(detail_url)
            title = re.sub(r"\s+", " ", str(item.get("title") or item.get("name") or "").strip())
            if not cmid or not title:
                continue
            completed = is_assignment_completed(item)
            if completed and not include_completed:
                continue
            submitted_files = [entry for entry in (item.get("submitted_files") or []) if isinstance(entry, dict)]
            due_at = str(item.get("due") or item.get("due_time") or item.get("due_date") or item.get("deadline") or "").strip()
            targets.append(
                AssignmentTarget(
                    course_id=course_id,
                    course_name=course_name,
                    title=title,
                    cmid=cmid,
                    detail_url=urljoin(config.E3_BASE_URL, detail_url),
                    due_at=due_at,
                    category=str(item.get("category") or "").strip(),
                    completed=completed,
                    submitted_count=len(submitted_files),
                )
            )

    targets.sort(key=lambda item: (1 if item.completed else 0, item.course_id, item.title.casefold(), item.cmid))
    return targets


def _resolve_course_id(line_user_id: str, course: str) -> str:
    matches = {(target.course_id, target.course_name) for target in list_assignment_targets(line_user_id, course)}
    if not matches:
        raise E3UploadError(f"找不到符合 `{course}` 的課程作業，請先 `/e3 relogin` 更新快取。")
    if len(matches) > 1:
        options = ", ".join(f"{course_id} {name}" for course_id, name in sorted(matches)[:5])
        raise E3UploadError(f"`{course}` 對應到多門課，請從 autocomplete 選課號。候選：{options}")
    return next(iter(matches))[0]


def resolve_assignment_target(line_user_id: str, course: str, assignment_ref: str) -> AssignmentTarget:
    course_id = _resolve_course_id(line_user_id, course)
    raw_ref = str(assignment_ref or "").strip()
    if ":" in raw_ref:
        ref_course_id, raw_ref = raw_ref.split(":", 1)
        if ref_course_id and ref_course_id != course_id:
            raise E3UploadError("選到的作業不屬於指定課程，已取消上傳。")

    matches = []
    for target in list_assignment_targets(line_user_id, course_id):
        if target.course_id != course_id:
            continue
        if raw_ref == target.cmid or raw_ref.casefold() == target.title.casefold():
            matches.append(target)

    if not matches:
        raise E3UploadError("找不到這門課底下對應的作業，已取消上傳。")
    if len(matches) > 1:
        raise E3UploadError("作業選擇不夠明確，請從 autocomplete 選一個作業。")
    return matches[0]


def _extract_m_cfg(html: str) -> dict[str, Any]:
    match = re.search(r"M\.cfg\s*=\s*(\{.*?\});", html, flags=re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def _input_value(soup: BeautifulSoup, name: str, default: str = "") -> str:
    node = soup.find("input", attrs={"name": name})
    if node is None:
        return default
    return str(node.get("value") or default)


def _submit_form_fields(soup: BeautifulSoup) -> dict[str, str]:
    form = None
    for candidate in soup.find_all("form"):
        action_input = candidate.find("input", attrs={"name": "action"})
        if action_input and str(action_input.get("value") or "") == "savesubmission":
            form = candidate
            break
    if form is None:
        form = soup

    fields: dict[str, str] = {}
    for node in form.find_all(["input", "textarea", "select"]):
        name = str(node.get("name") or "").strip()
        if not name:
            continue
        if node.name == "select":
            selected = node.find("option", selected=True) or node.find("option")
            fields[name] = str(selected.get("value") or "") if selected else ""
            continue
        fields[name] = str(node.get("value") or "")
    return fields


def _extract_quoted_value(html: str, key: str) -> str:
    patterns = [
        rf'"{re.escape(key)}"\s*:\s*"([^"]+)"',
        rf"'{re.escape(key)}'\s*:\s*'([^']+)'",
        rf"{re.escape(key)}\s*[:=]\s*['\"]([^'\"]+)['\"]",
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return ""


def _extract_number_value(html: str, key: str) -> str:
    patterns = [
        rf'"{re.escape(key)}"\s*:\s*(-?\d+)',
        rf"'{re.escape(key)}'\s*:\s*(-?\d+)",
        rf"{re.escape(key)}\s*[:=]\s*(-?\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return ""


def _extract_upload_repo_id(html: str) -> str:
    upload_then_id = re.search(r'"type"\s*:\s*"upload".{0,600}?"id"\s*:\s*(\d+)', html, flags=re.DOTALL)
    if upload_then_id:
        return upload_then_id.group(1)
    id_then_upload = re.search(r'"id"\s*:\s*(\d+).{0,600}?"type"\s*:\s*"upload"', html, flags=re.DOTALL)
    if id_then_upload:
        return id_then_upload.group(1)
    return DEFAULT_UPLOAD_REPO_ID


def _parse_edit_context(html: str, expected_course_id: str, expected_cmid: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    m_cfg = _extract_m_cfg(html)
    fields = _submit_form_fields(soup)

    page_course_id = str(m_cfg.get("courseId") or "").strip()
    page_cmid = str(m_cfg.get("contextInstanceId") or fields.get("id") or "").strip()
    if page_course_id and page_course_id != str(expected_course_id):
        raise E3UploadError("E3 編輯頁課程和你選的課程不一致，已取消上傳。")
    if page_cmid and page_cmid != str(expected_cmid):
        raise E3UploadError("E3 編輯頁作業和你選的作業不一致，已取消上傳。")

    itemid = fields.get("files_filemanager") or _extract_number_value(html, "itemid")
    sesskey = fields.get("sesskey") or str(m_cfg.get("sesskey") or "")
    userid = fields.get("userid") or str(m_cfg.get("userId") or "")
    ctx_id = _extract_number_value(html, "ctx_id") or str(m_cfg.get("contextid") or "")
    client_id = _extract_quoted_value(html, "client_id")
    if not client_id:
        client_id = f"xe3{itemid}"

    required = {
        "itemid": itemid,
        "sesskey": sesskey,
        "userid": userid,
        "ctx_id": ctx_id,
        "lastmodified": fields.get("lastmodified") or "",
        "id": fields.get("id") or expected_cmid,
    }
    missing = [key for key, value in required.items() if not str(value or "").strip()]
    if missing:
        raise E3UploadError(f"E3 編輯頁缺少必要欄位：{', '.join(missing)}。請先重新登入後再試。")

    return {
        **fields,
        "id": required["id"],
        "userid": userid,
        "sesskey": sesskey,
        "files_filemanager": itemid,
        "ctx_id": ctx_id,
        "client_id": client_id,
        "repo_id": _extract_upload_repo_id(html),
        "maxbytes": _extract_number_value(html, "maxbytes") or DEFAULT_MAX_BYTES,
        "areamaxbytes": _extract_number_value(html, "areamaxbytes") or DEFAULT_AREA_MAX_BYTES,
    }


def _submitted_file_count(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    links = soup.select('a[href*="assignsubmission_file"], a[href*="submission_files"]')
    return len({str(link.get("href") or "") for link in links if link.get("href")})


def _has_submitted_status(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).casefold()
    return "已繳交" in text or "submitted" in text


def _page_contains_filename(html: str, filename: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    if filename in text:
        return True
    for link in soup.select("a[href]"):
        if filename == str(link.get_text(" ", strip=True) or "").strip():
            return True
    return False


def _remove_existing_submission(session: requests.Session, target: AssignmentTarget) -> None:
    confirm_url = f"{E3_ASSIGN_VIEW_URL}?id={target.cmid}&action=removesubmissionconfirm"
    response = session.get(confirm_url, headers={"Referer": target.detail_url}, allow_redirects=True)
    response.raise_for_status()
    if _needs_login(response):
        raise E3UploadError("E3 session 已過期，請先 `/e3 relogin`。")

    soup = BeautifulSoup(response.text or "", "html.parser")
    m_cfg = _extract_m_cfg(response.text or "")
    payload = {
        "id": _input_value(soup, "id", target.cmid),
        "action": _input_value(soup, "action", "removesubmission"),
        "userid": _input_value(soup, "userid", str(m_cfg.get("userId") or "")),
        "sesskey": _input_value(soup, "sesskey", str(m_cfg.get("sesskey") or "")),
    }
    if not payload["userid"] or not payload["sesskey"]:
        raise E3UploadError("無法取得刪除舊作業所需欄位，已取消。")

    post_response = session.post(
        E3_ASSIGN_VIEW_URL,
        data=payload,
        headers={"Origin": config.E3_BASE_URL, "Referer": confirm_url},
        allow_redirects=True,
    )
    post_response.raise_for_status()
    if _needs_login(post_response):
        raise E3UploadError("E3 session 已過期，請先 `/e3 relogin`。")


def _fetch_assignment_view(session: requests.Session, target: AssignmentTarget) -> str:
    response = session.get(target.detail_url, allow_redirects=True)
    response.raise_for_status()
    if _needs_login(response):
        raise E3UploadError("E3 session 已過期，請先 `/e3 relogin`。")
    return response.text or ""


def _fetch_edit_context(session: requests.Session, target: AssignmentTarget) -> tuple[str, dict[str, str]]:
    edit_url = f"{E3_ASSIGN_VIEW_URL}?id={target.cmid}&action=editsubmission"
    response = session.get(edit_url, headers={"Referer": target.detail_url}, allow_redirects=True)
    response.raise_for_status()
    if _needs_login(response):
        raise E3UploadError("E3 session 已過期，請先 `/e3 relogin`。")
    html = response.text or ""
    return edit_url, _parse_edit_context(html, target.course_id, target.cmid)


def _upload_to_draft(
    session: requests.Session,
    edit_url: str,
    context: dict[str, str],
    filename: str,
    content: bytes,
    content_type: str,
) -> None:
    data = [
        ("title", ""),
        ("author", ""),
        ("license", "unknown"),
        ("itemid", context["files_filemanager"]),
        ("repo_id", context.get("repo_id") or DEFAULT_UPLOAD_REPO_ID),
        ("p", ""),
        ("page", ""),
        ("env", "filemanager"),
        ("sesskey", context["sesskey"]),
        ("client_id", context["client_id"]),
        ("itemid", context["files_filemanager"]),
        ("maxbytes", context.get("maxbytes") or DEFAULT_MAX_BYTES),
        ("areamaxbytes", context.get("areamaxbytes") or DEFAULT_AREA_MAX_BYTES),
        ("ctx_id", context["ctx_id"]),
        ("savepath", "/"),
    ]
    files = {"repo_upload_file": (filename, content, content_type)}
    response = session.post(
        E3_REPOSITORY_AJAX_URL,
        data=data,
        files=files,
        headers={"Origin": config.E3_BASE_URL, "Referer": edit_url},
        allow_redirects=True,
    )
    response.raise_for_status()
    if _needs_login(response):
        raise E3UploadError("E3 session 已過期，請先 `/e3 relogin`。")
    if "error" in (response.text or "").casefold():
        raise E3UploadError("E3 回報檔案上傳失敗，請確認檔案大小、格式或是否已存在同名檔案。")


def _save_assignment_submission(session: requests.Session, edit_url: str, context: dict[str, str]) -> str:
    payload = dict(context)
    payload.update(
        {
            "action": "savesubmission",
            "_qf__mod_assign_submission_form": payload.get("_qf__mod_assign_submission_form") or "1",
            "mform_isexpanded_id_submissionheader": payload.get("mform_isexpanded_id_submissionheader") or "1",
            "submitbutton": payload.get("submitbutton") or "儲存更改",
        }
    )
    for transient in ("ctx_id", "client_id", "repo_id", "maxbytes", "areamaxbytes"):
        payload.pop(transient, None)

    response = session.post(
        E3_ASSIGN_VIEW_URL,
        data=payload,
        headers={"Origin": config.E3_BASE_URL, "Referer": edit_url},
        allow_redirects=True,
    )
    response.raise_for_status()
    if _needs_login(response):
        raise E3UploadError("E3 session 已過期，請先 `/e3 relogin`。")
    return response.text or ""


def upload_assignment_submission(
    line_user_id: str,
    course: str,
    assignment_ref: str,
    filename: str,
    content: bytes,
    *,
    content_type: str | None = None,
    replace_existing: bool = False,
) -> UploadResult:
    if not content:
        raise E3UploadError("Discord 附件是空的，已取消上傳。")

    target = resolve_assignment_target(line_user_id, course, assignment_ref)
    session = _authenticated_session(line_user_id)
    current_html = _fetch_assignment_view(session, target)
    existing_count = _submitted_file_count(current_html)
    if existing_count and not replace_existing:
        raise E3UploadError(
            f"這份作業目前已有 `{existing_count}` 個已繳檔案。為了避免覆蓋錯作業，請確認後把 `replace_existing` 設為 True。"
        )
    if existing_count and replace_existing:
        _remove_existing_submission(session, target)

    edit_url, context = _fetch_edit_context(session, target)
    guessed_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    safe_filename = Path(filename or "upload").name or "upload"
    _upload_to_draft(session, edit_url, context, safe_filename, content, guessed_type)
    final_html = _save_assignment_submission(session, edit_url, context)

    if not _has_submitted_status(final_html):
        raise E3UploadError("E3 沒有顯示已繳交狀態，請回 E3 網頁確認是否成功。")
    if not _page_contains_filename(final_html, safe_filename):
        raise E3UploadError("E3 已回到作業頁，但頁面上找不到剛上傳的檔名，請回 E3 網頁確認。")

    return UploadResult(
        course_id=target.course_id,
        course_name=target.course_name,
        assignment_title=target.title,
        cmid=target.cmid,
        filename=safe_filename,
        submitted_file_count=_submitted_file_count(final_html),
        replaced_existing=bool(existing_count and replace_existing),
    )
