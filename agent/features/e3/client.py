import html as html_lib
import json
import os
import re
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlparse

from agent.config import e3_cache_ttl_minutes, e3_runtime_root, legacy_e3_runtime_root
from .scraper import config as scraper_config
from .scraper import db_manager as scraper_db_manager
from .scraper import utils as scraper_utils
from .scraper.get_course import extract_course as scraper_extract_course
from .scraper.get_course import get_user_data as scraper_get_user_data


_E3_SYNC_LOCK = threading.Lock()


def get_runtime_root() -> Path:
    root = e3_runtime_root()
    legacy_root = legacy_e3_runtime_root()
    if not root.exists() and legacy_root.exists():
        legacy_root.rename(root)
    elif legacy_root.exists() and not any(root.iterdir()):
        for item in legacy_root.iterdir():
            shutil.move(str(item), str(root / item.name))
        shutil.rmtree(legacy_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_user_key(line_user_id: str) -> str:
    if not line_user_id:
        raise ValueError("line_user_id is required for direct E3 integration")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", line_user_id)


def _runtime_paths_for_user(user_key: str) -> dict[str, str]:
    workspace = get_runtime_root() / user_key
    workspace.mkdir(parents=True, exist_ok=True)
    return {
        "BASE_DIR": str(workspace),
        "COOKIE_FILE": str(workspace / "cookies.json"),
        "COURSES_FILE": str(workspace / "courses_114.json"),
        "E3_MY_HTML": str(workspace / "e3_my.html"),
        "LAST_RUN_FILE": str(workspace / "last_run.json"),
    }


@contextmanager
def _patched_e3_runtime(user_key: str) -> Iterator[dict[str, str]]:
    new_paths = _runtime_paths_for_user(user_key)
    original = {key: getattr(scraper_config, key) for key in new_paths}
    original_utils_base_dir = scraper_utils.BASE_DIR
    original_cookie_file = scraper_get_user_data.COOKIE_FILE
    original_db_links = scraper_db_manager.LINKS_DB_FILE

    try:
        for key, value in new_paths.items():
            setattr(scraper_config, key, value)

        scraper_utils.BASE_DIR = new_paths["BASE_DIR"]
        scraper_get_user_data.COOKIE_FILE = new_paths["COOKIE_FILE"]
        scraper_extract_course.config.COURSES_FILE = new_paths["COURSES_FILE"]
        scraper_extract_course.config.E3_MY_HTML = new_paths["E3_MY_HTML"]
        scraper_db_manager.LINKS_DB_FILE = os.path.join(new_paths["BASE_DIR"], "file_links_db.json")

        yield new_paths
    finally:
        for key, value in original.items():
            setattr(scraper_config, key, value)
        scraper_utils.BASE_DIR = original_utils_base_dir
        scraper_get_user_data.COOKIE_FILE = original_cookie_file
        scraper_extract_course.config.COURSES_FILE = original["COURSES_FILE"]
        scraper_extract_course.config.E3_MY_HTML = original["E3_MY_HTML"]
        scraper_db_manager.LINKS_DB_FILE = original_db_links


def _load_json(path: os.PathLike[str] | str) -> Any:
    file_path = Path(path)
    if not file_path.exists():
        return None
    with file_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_courses_index(courses_file_path: str) -> dict[str, dict[str, str]]:
    raw = _load_json(courses_file_path)
    if not isinstance(raw, dict):
        return {}

    result = {}
    for course_id, raw_name in raw.items():
        if not isinstance(raw_name, str):
            continue
        display_name = raw_name.strip()
        if not display_name:
            continue
        result[display_name] = {
            "_course_id": str(course_id).strip(),
            "_source": "courses_index",
        }
    return result


def _read_all_courses_data(base_dir: str, courses_file_path: str | None = None) -> dict[str, dict[str, Any]]:
    base_path = Path(base_dir)
    all_data = {}
    if courses_file_path:
        all_data.update(_read_courses_index(courses_file_path))
    if not base_path.exists():
        return all_data

    for course_folder in sorted(base_path.iterdir()):
        if not course_folder.is_dir():
            continue

        course_data = {}
        news_data = _load_json(course_folder / "news.json")
        if news_data:
            course_data["news"] = news_data

        forums_data = _load_json(course_folder / "forums.json")
        if forums_data:
            course_data["forums"] = forums_data

        assignments_data = _load_json(course_folder / "homework" / "assignments.json")
        if assignments_data:
            course_data["assignments"] = assignments_data

        grades_data = _load_json(course_folder / "grades.json")
        if grades_data:
            course_data["grades"] = grades_data

        outline_data = _load_json(course_folder / "course_outline.json")
        if outline_data:
            course_data["course_outline"] = outline_data

        timetable_data = _load_json(course_folder / "timetable.json")
        if timetable_data:
            course_data["timetable"] = timetable_data

        homework_page_data = _load_json(course_folder / "homework_page.json")
        if homework_page_data:
            course_data["homework_page"] = homework_page_data

        display_name = course_folder.name
        course_data["_folder_name"] = course_folder.name
        if "_" in course_folder.name:
            course_data["_course_id"] = course_folder.name.split("_", 1)[0]
            display_name = course_folder.name.split("_", 1)[1]

        base_payload = all_data.get(display_name, {})
        if not isinstance(base_payload, dict):
            base_payload = {}
        merged = dict(base_payload)
        merged.update(course_data)
        all_data[display_name] = merged

    return all_data


def _read_home_page_preview(html_path: str) -> dict[str, Any]:
    file_path = Path(html_path)
    if not file_path.exists():
        return {
            "page_title": "",
            "course_count": 0,
            "sample_courses": [],
            "user_name": "",
            "user_email": "",
        }

    html = file_path.read_text(encoding="utf-8")
    html_unescaped = html_lib.unescape(html)
    page_title = ""
    title_match = re.search(r"<title>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        page_title = re.sub(r"\s+", " ", title_match.group(1)).strip()

    course_names = []
    for match in re.finditer(r'class="course-link"[^>]*>(.*?)</a>', html, flags=re.IGNORECASE | re.DOTALL):
        text = re.sub(r"<.*?>", "", match.group(1))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            course_names.append(text)

    user_name = ""
    login_name_match = re.search(
        r'您以<a[^>]*>(.*?)</a>登入',
        html_unescaped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if login_name_match:
        user_name = re.sub(r"\s+", " ", login_name_match.group(1)).strip()

    user_email = ""
    email_match = re.search(r'href="mailto:([^"]+)"', html_unescaped, flags=re.IGNORECASE)
    if email_match:
        user_email = unquote(email_match.group(1).strip())

    return {
        "page_title": page_title,
        "course_count": len(course_names),
        "sample_courses": course_names[:5],
        "user_name": user_name,
        "user_email": user_email,
    }


def _parse_calendar_timestamp(url: str | None) -> str | None:
    if not url:
        return None

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    values = query.get("time") or []
    if not values:
        return None

    try:
        timestamp = int(values[0])
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _read_home_calendar_events(html_path: str, course_lookup: dict[str, str] | None = None) -> list[dict[str, str]]:
    file_path = Path(html_path)
    if not file_path.exists():
        return []

    html = file_path.read_text(encoding="utf-8")
    html_unescaped = html_lib.unescape(html)
    pattern = re.compile(
        r'<div class="event .*?data-region="event-item">.*?'
        r'data-event-id="(?P<event_id>\d+)".*?'
        r'href="(?P<href>[^"]+)".*?'
        r'title="(?P<title>[^"]*)">.*?'
        r'<div class="date small">(?P<date_html>.*?)</div>',
        flags=re.IGNORECASE | re.DOTALL,
    )

    events = []
    for match in pattern.finditer(html_unescaped):
        href = match.group("href").strip()
        due_at = _parse_calendar_timestamp(href)
        if not due_at:
            continue

        course_id = ""
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        course_values = query.get("course") or []
        if course_values:
            course_id = course_values[0].strip()

        events.append(
            {
                "event_id": match.group("event_id").strip(),
                "course_id": course_id,
                "course_name": (course_lookup or {}).get(course_id, ""),
                "title": re.sub(r"\s+", " ", match.group("title")).strip(),
                "due_at": due_at,
                "date_label": re.sub(r"<.*?>", "", match.group("date_html")).replace(",", " ").strip(),
                "source": "calendar_upcoming",
                "url": href,
            }
        )

    return events


def _build_course_lookup(courses: dict[str, dict[str, Any]]) -> dict[str, str]:
    lookup = {}
    if not isinstance(courses, dict):
        return lookup

    for display_name, payload in courses.items():
        if not isinstance(payload, dict):
            continue
        course_id = str(payload.get("_course_id") or "").strip()
        if course_id:
            lookup[course_id] = display_name
    return lookup


def check_status(user_key: str | None = None) -> dict[str, Any]:
    e3_root = Path(__file__).resolve().parent / "scraper"
    runtime_root = get_runtime_root()
    status = {
        "e3_root": str(e3_root),
        "runtime_root": str(runtime_root),
        "available": e3_root.exists(),
    }
    if user_key:
        workspace = runtime_root / user_key
        status["workspace"] = str(workspace)
        status["has_workspace"] = workspace.exists()
        status["has_cookie"] = (workspace / "cookies.json").exists()
        status["has_courses"] = (workspace / "courses_114.json").exists()
        status["has_home_html"] = (workspace / "e3_my.html").exists()
        preview = _read_home_page_preview(workspace / "e3_my.html")
        status["user_name"] = preview.get("user_name") or ""
        status["user_email"] = preview.get("user_email") or ""
    return status


def fetch_courses(user_key: str) -> dict[str, dict[str, Any]]:
    paths = _runtime_paths_for_user(user_key)
    return _read_all_courses_data(paths["BASE_DIR"], paths["COURSES_FILE"])


def fetch_timeline_snapshot(user_key: str) -> dict[str, Any]:
    paths = _runtime_paths_for_user(user_key)
    courses = _read_all_courses_data(paths["BASE_DIR"], paths["COURSES_FILE"])
    course_lookup = _build_course_lookup(courses)
    return {
        "courses": courses,
        "calendar_events": _read_home_calendar_events(paths["E3_MY_HTML"], course_lookup),
        "home_preview": _read_home_page_preview(paths["E3_MY_HTML"]),
        "workspace": paths["BASE_DIR"],
    }


def clear_runtime_data(user_key: str) -> None:
    workspace = get_runtime_root() / user_key
    if workspace.exists():
        shutil.rmtree(workspace)


def fetch_file_links(user_key: str) -> dict[str, Any]:
    paths = _runtime_paths_for_user(user_key)
    file_links = _load_json(Path(paths["BASE_DIR"]) / "file_links_db.json")
    if not isinstance(file_links, dict):
        file_links = {}
    courses = _read_all_courses_data(paths["BASE_DIR"], paths["COURSES_FILE"])
    return {
        "courses": courses,
        "file_links": file_links,
        "workspace": paths["BASE_DIR"],
    }


def get_cache_status(user_key: str) -> dict[str, Any]:
    paths = _runtime_paths_for_user(user_key)
    candidates = [
        Path(paths["COURSES_FILE"]),
        Path(paths["E3_MY_HTML"]),
        Path(paths["BASE_DIR"]) / "file_links_db.json",
        Path(paths["LAST_RUN_FILE"]),
    ]
    existing = [path for path in candidates if path.exists()]
    ttl_minutes = e3_cache_ttl_minutes()
    if not existing:
        return {
            "exists": False,
            "ttl_minutes": ttl_minutes,
            "is_fresh": False,
            "age_seconds": None,
            "age_minutes": None,
            "last_updated_at": "",
        }

    latest = max(existing, key=lambda path: path.stat().st_mtime)
    last_dt = datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
    age_seconds = max(0.0, (datetime.now(timezone.utc) - last_dt).total_seconds())
    return {
        "exists": True,
        "ttl_minutes": ttl_minutes,
        "is_fresh": age_seconds <= ttl_minutes * 60,
        "age_seconds": age_seconds,
        "age_minutes": int(age_seconds // 60),
        "last_updated_at": last_dt.isoformat(),
    }


def login_and_sync(
    account: str,
    password: str,
    user_key: str,
    update_data: bool = False,
    update_links: bool = False,
) -> dict[str, Any]:
    with _E3_SYNC_LOCK:
        with _patched_e3_runtime(user_key) as paths:
            scraper_get_user_data.get_user_data(account, password, update_data=update_data, update_links=update_links)
            courses = _read_all_courses_data(paths["BASE_DIR"], paths["COURSES_FILE"])
            course_lookup = _build_course_lookup(courses)
            return {
                "courses": courses,
                "calendar_events": _read_home_calendar_events(paths["E3_MY_HTML"], course_lookup),
                "home_preview": _read_home_page_preview(paths["E3_MY_HTML"]),
                "workspace": paths["BASE_DIR"],
            }
