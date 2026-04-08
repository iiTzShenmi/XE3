from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit


def sanitize_line_uri(url: str | None) -> str:
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


def count_file_items(link_payload: dict | None) -> int:
    link_payload = link_payload or {}
    handouts = link_payload.get("handouts") or []
    assignments = link_payload.get("assignments") or {}
    assignment_count = 0
    for entry in assignments.values():
        assignment_count += len((entry or {}).get("web_files") or [])
    return len(handouts) + assignment_count


def collect_file_entries(course_id: str, course_name: str, links: dict | None) -> list[dict]:
    links = links or {}
    entries: list[dict] = []
    for item in links.get("handouts") or []:
        entries.append(
            {
                "course_id": course_id,
                "course_name": course_name,
                "folder": item.get("folder") or "講義",
                "kind": "講義",
                "title": item.get("name") or "未命名檔案",
                "source_url": sanitize_line_uri(item.get("url") or ""),
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
                    "source_url": sanitize_line_uri(web_file.get("url") or ""),
                    "accent": "#D97706",
                }
            )
    return entries


def group_file_entries(entries: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        folder = str(entry.get("folder") or "未分類").strip()
        groups.setdefault(folder, []).append(entry)
    return sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
