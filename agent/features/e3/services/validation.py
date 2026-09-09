from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.features.e3.utils.common import current_semester_tag, extract_semester_tag

from .monitoring import write_json_atomic


SECTION_RULES: dict[str, tuple[type, str | None, type | None]] = {
    "news.json": (list, None, None),
    "forums.json": (dict, "forums", list),
    "grades.json": (dict, "grade_items", list),
    "timetable.json": (dict, None, None),
    "course_outline.json": (dict, None, None),
    "homework_page.json": (dict, "homeworks", list),
    "assignments.json": (list, None, None),
}


def _issue(path: Path, code: str, message: str, root: Path) -> dict[str, str]:
    try:
        relative = str(path.relative_to(root))
    except ValueError:
        relative = str(path)
    return {"file": relative, "code": code, "message": message}


def _load_json(path: Path, expected_type: type, root: Path) -> tuple[Any, dict[str, str] | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, _issue(path, "invalid_json", f"JSON 解析失敗：line {exc.lineno}", root)
    except OSError as exc:
        return None, _issue(path, "read_error", str(exc), root)
    if not isinstance(value, expected_type):
        return None, _issue(path, "wrong_root_type", f"預期 {expected_type.__name__}，實際為 {type(value).__name__}", root)
    return value, None


def _current_course_folders(workspace: Path, active_course_ids: set[str] | None = None) -> list[Path]:
    semester = current_semester_tag()
    folders = []
    for path in workspace.iterdir() if workspace.exists() else []:
        if not path.is_dir() or path.name in {"quarantine", ".sync-staging"}:
            continue
        course_id = path.name.split("_", 1)[0] if "_" in path.name else ""
        if active_course_ids and course_id not in active_course_ids:
            continue
        display_name = path.name.split("_", 1)[1] if "_" in path.name else path.name
        if extract_semester_tag(display_name) == semester:
            folders.append(path)
    return folders


def validate_workspace(workspace: str | Path, *, quarantine_invalid: bool = False) -> dict[str, Any]:
    root = Path(workspace)
    checked_files = 0
    checked_courses = 0
    issues: list[dict[str, str]] = []

    courses_path = root / "courses_current.json"
    courses, courses_issue = _load_json(courses_path, dict, root) if courses_path.exists() else ({}, None)
    if courses_path.exists():
        checked_files += 1
    else:
        issues.append(_issue(courses_path, "missing_course_index", "缺少當期課程索引", root))
    if courses_issue:
        issues.append(courses_issue)
    elif isinstance(courses, dict):
        for course_id, course_name in courses.items():
            if not str(course_id).strip() or not isinstance(course_name, str) or not course_name.strip():
                issues.append(_issue(courses_path, "invalid_course", "課程 ID 與名稱必須為非空字串", root))
                break
            if extract_semester_tag(course_name) != current_semester_tag():
                issues.append(_issue(courses_path, "wrong_semester", f"課程 {course_id} 不屬於當前學期", root))

    invalid_section_paths: list[Path] = []
    active_course_ids = {str(course_id).strip() for course_id in courses} if isinstance(courses, dict) else set()
    for folder in _current_course_folders(root, active_course_ids):
        checked_courses += 1
        candidates = [folder / name for name in SECTION_RULES if name != "assignments.json"]
        candidates.append(folder / "homework" / "assignments.json")
        for path in candidates:
            if not path.exists():
                continue
            checked_files += 1
            expected_type, child_key, child_type = SECTION_RULES[path.name]
            value, issue = _load_json(path, expected_type, root)
            if issue:
                issues.append(issue)
                invalid_section_paths.append(path)
                continue
            if child_key and child_type and child_key in value and not isinstance(value[child_key], child_type):
                issues.append(_issue(path, "wrong_child_type", f"{child_key} 必須為 {child_type.__name__}", root))
                invalid_section_paths.append(path)

    links_path = root / "file_links_db.json"
    if links_path.exists():
        checked_files += 1
        _, issue = _load_json(links_path, dict, root)
        if issue:
            issues.append(issue)
            invalid_section_paths.append(links_path)

    quarantined = []
    if quarantine_invalid and invalid_section_paths:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantine_root = root / "quarantine" / stamp
        for source in dict.fromkeys(invalid_section_paths):
            relative = source.relative_to(root)
            destination = quarantine_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            quarantined.append(str(relative))

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "valid": not issues,
        "checked_courses": checked_courses,
        "checked_files": checked_files,
        "issue_count": len(issues),
        "issues": issues[:50],
        "quarantined": quarantined,
    }
    write_json_atomic(root / "validation_report.json", report)
    return report


def read_validation_report(workspace: str | Path) -> dict[str, Any]:
    path = Path(workspace) / "validation_report.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
