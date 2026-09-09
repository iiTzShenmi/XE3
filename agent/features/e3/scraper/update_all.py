import os
import time
from datetime import datetime, timezone

from .utils import save_json, safe_name, load_json
from .fetch_data.__fetch_news import fetch_news
from .fetch_data.__fetch_forums import fetch_forums
from .fetch_data.__fetch_handouts import fetch_handouts
from .fetch_data.__fetch_assignments import fetch_assignments 
from .fetch_data.__fetch_grades import fetch_grades
from .fetch_data.__fetch_homework import fetch_homework
from .fetch_data.__fetch_timetable import fetch_timetable
from .fetch_data.__fetch_course_outline import fetch_course_outline
from . import config
from .http import build_session as build_http_session

def build_session(cookies=None):
    return build_http_session(cookies)

def load_cookies():
    """Load cookies from file."""
    cookies = load_json(config.COOKIE_FILE)
    if cookies and isinstance(cookies, dict):
        return cookies
    return {}

def load_courses():
    """Load courses from file."""
    courses = load_json(config.COURSES_FILE)
    if courses and isinstance(courses, dict):
        return courses
    return {}

def _is_due(path, interval_minutes, force=False):
    if force or not os.path.exists(path):
        return True
    age_seconds = max(0.0, time.time() - os.path.getmtime(path))
    return age_seconds >= interval_minutes * 60


def _run_fetch(name, output_path, interval_minutes, fetch_fn, *, force=False):
    if not _is_due(output_path, interval_minutes, force=force):
        return {"name": name, "status": "skipped", "reason": "fresh"}
    started = time.monotonic()
    try:
        result = fetch_fn()
    except Exception as exc:
        return {
            "name": name,
            "status": "failed",
            "error": str(exc)[:300],
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    if result is False:
        return {
            "name": name,
            "status": "failed",
            "error": "fetcher reported failure",
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    return {
        "name": name,
        "status": "updated",
        "result": result if isinstance(result, bool) else None,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def _write_run_report(run_type, started_at, endpoint_results):
    failed = sum(1 for item in endpoint_results if item.get("status") == "failed")
    updated = sum(1 for item in endpoint_results if item.get("status") == "updated")
    skipped = sum(1 for item in endpoint_results if item.get("status") == "skipped")
    report = {
        "status": "success" if failed == 0 else "partial",
        "type": run_type,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "endpoints": endpoint_results,
    }
    save_json(config.LAST_RUN_FILE, report)
    return report


def __update_course_data(session=None, cookies=None, force=False):
    """Update course data only (news, assignments, grades) - no file downloads."""
    cookies = cookies or load_cookies()
    if not cookies:
        print("[!] Warning: No cookies found. Please login first.")
        return {"status": "failed", "reason": "missing_cookies", "endpoints": []}
    
    courses = load_courses()
    if not courses:
        print("[!] Warning: No courses found. Please fetch courses first.")
        return {"status": "failed", "reason": "missing_courses", "endpoints": []}
    
    session = session or build_session(cookies)

    print(f"[*] Updating course data for {len(courses)} courses")
    started_at = datetime.now(timezone.utc).isoformat()
    endpoint_results = []

    for cid, cname in courses.items():
        cname = safe_name(cname)
        folder = os.path.join(config.BASE_DIR, f"{cid}_{cname}")
        print(f"\n=== Updating course data {cid}: {cname} ===")
        dynamic = config.DYNAMIC_SYNC_INTERVAL_MINUTES
        static = config.STATIC_SYNC_INTERVAL_MINUTES
        specs = [
            ("news", os.path.join(folder, "news.json"), dynamic, lambda: fetch_news(cid, cname, session, cookies)),
            ("forums", os.path.join(folder, "forums.json"), dynamic, lambda: fetch_forums(cid, cname, session, cookies)),
            ("assignments", os.path.join(folder, "homework", "assignments.json"), dynamic, lambda: fetch_assignments(cid, cname, session, cookies)),
            ("grades", os.path.join(folder, "grades.json"), dynamic, lambda: fetch_grades(cid, cname, session, cookies)),
            ("homework", os.path.join(folder, "homework_page.json"), dynamic, lambda: fetch_homework(cid, cname, session, cookies)),
            ("timetable", os.path.join(folder, "timetable.json"), static, lambda: fetch_timetable(cid, cname, session, cookies)),
            ("course_outline", os.path.join(folder, "course_outline.json"), static, lambda: fetch_course_outline(cid, cname, session, cookies)),
        ]
        for name, output_path, interval, fetch_fn in specs:
            result = _run_fetch(f"{cid}:{name}", output_path, interval, fetch_fn, force=force)
            endpoint_results.append(result)
            if result["status"] == "failed":
                print(f"[!] {name} failed for {cid}: {result.get('error')}")

    print("\n[+] Course data update complete!")
    return _write_run_report("data", started_at, endpoint_results)

def __update_file_links(session=None, cookies=None, force=False):
    """Update file links database (handouts and assignment files) - no downloads."""
    cookies = cookies or load_cookies()
    if not cookies:
        print("[!] Warning: No cookies found. Please login first.")
        return {"status": "failed", "reason": "missing_cookies", "endpoints": []}
    
    courses = load_courses()
    if not courses:
        print("[!] Warning: No courses found. Please fetch courses first.")
        return {"status": "failed", "reason": "missing_courses", "endpoints": []}
    
    session = session or build_session(cookies)

    print(f"[*] Updating file links for {len(courses)} courses")

    started_at = datetime.now(timezone.utc).isoformat()
    endpoint_results = []
    links_path = os.path.join(config.BASE_DIR, "file_links_db.json")
    links_due = _is_due(links_path, config.STATIC_SYNC_INTERVAL_MINUTES, force=force)
    for cid, cname in courses.items():
        cname = safe_name(cname)
        print(f"\n=== Updating file links {cid}: {cname} ===")
        result = _run_fetch(
            f"{cid}:handouts",
            links_path,
            config.STATIC_SYNC_INTERVAL_MINUTES,
            lambda: fetch_handouts(cid, cname, session, cookies, save_links_only=True),
            force=links_due,
        )
        endpoint_results.append(result)
        if result["status"] == "failed":
            print(f"[!] fetch_handouts links failed for {cid}: {result.get('error')}")
        
        # Assignment file links are already saved in fetch_assignments
        # (they're saved when parsing assignment details)

    print("\n[+] File links update complete!")
    return _write_run_report("links", started_at, endpoint_results)

def __update_all(session=None, cookies=None, force=False):
    """Update both course data and file links (for backward compatibility)."""
    __update_course_data(session=session, cookies=cookies, force=force)
    __update_file_links(session=session, cookies=cookies, force=force)
