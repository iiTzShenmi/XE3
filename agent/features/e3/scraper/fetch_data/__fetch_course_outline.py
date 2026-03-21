"""
Fetch course outline page data
"""
import os
import re

import requests
from bs4 import BeautifulSoup

from ..utils import save_json, ensure_course_folder, safe_name
from .. import config


EXAM_KEYWORDS = [
    "exam",
    "midterm",
    "final",
    "quiz",
    "期中",
    "期末",
    "考試",
    "測驗",
]


def _clean_text(text):
    return re.sub(r"\s+", " ", str(text or "").strip())


def _extract_exam_candidates(text, source):
    cleaned = _clean_text(text)
    lowered = cleaned.lower()
    if not cleaned or not any(keyword in lowered for keyword in EXAM_KEYWORDS):
        return []

    date_patterns = [
        r"\d{4}[/-]\d{1,2}[/-]\d{1,2}(?:\s+\d{1,2}:\d{2})?",
        r"\d{4}年\d{1,2}月\d{1,2}日(?:\s*\d{1,2}[:：]\d{2})?",
        r"\d{1,2}[/-]\d{1,2}(?:\s+\d{1,2}:\d{2})?",
    ]
    date_hits = []
    for pattern in date_patterns:
        date_hits.extend(re.findall(pattern, cleaned))

    return [
        {
            "source": source,
            "text": cleaned[:300],
            "date_mentions": list(dict.fromkeys(date_hits))[:5],
        }
    ]


def _collect_outline_sections(main_content):
    sections = []
    exam_candidates = []
    for heading in main_content.select("h2, h3, h4, .sectionname, .activity-header"):
        title = _clean_text(heading.get_text(" ", strip=True))
        if not title:
            continue
        body_parts = []
        for sibling in heading.find_next_siblings(limit=4):
            sibling_text = _clean_text(sibling.get_text(" ", strip=True))
            if sibling.name in {"h2", "h3", "h4"}:
                break
            if sibling_text:
                body_parts.append(sibling_text)
        body_text = " ".join(body_parts)[:1200]
        if not body_text and heading.parent:
            parent_text = _clean_text(heading.parent.get_text(" ", strip=True))
            if parent_text and parent_text != title:
                body_text = parent_text[:1200]
        if title or body_text:
            sections.append({"title": title, "text": body_text})
            exam_candidates.extend(_extract_exam_candidates(f"{title} {body_text}", f"section:{title or 'untitled'}"))
    return sections, exam_candidates

def fetch_course_outline(course_id, course_name, session, cookies):
    """Fetch course outline page data for a specific course."""
    course_name = safe_name(course_name)
    folder = ensure_course_folder(course_id, course_name)
    outline_file = os.path.join(folder, "course_outline.json")
    
    # Use course view URL - the outline is typically on the main course page
    url = f"{config.E3_BASE_URL}/course/view.php?id={course_id}"
    try:
        resp = session.get(url, cookies=cookies)
        resp.raise_for_status()
    except Exception as e:
        print(f"[!] Failed to fetch course outline page for {course_name}: {e}")
        return
    
    soup = BeautifulSoup(resp.text, "html.parser")
    main_content = soup.find("section", id="region-main")
    if not main_content:
        print(f"[-] No course outline content found for {course_name}")
        return
    
    activities = []
    exam_candidates = []
    
    # Find all activity items
    activity_items = main_content.find_all("li", class_=lambda x: x and "activity" in x.lower())
    
    for item in activity_items:
        # Find the link element
        link_elem = item.find("a", class_=lambda x: x and "aalink" in x.lower())
        if link_elem:
            name_elem = link_elem.find("span", class_="instancename")
            if name_elem:
                # Remove accesshide spans
                for accesshide in name_elem.find_all("span", class_="accesshide"):
                    accesshide.decompose()
                name = name_elem.get_text(strip=True)
                link = link_elem.get("href", "")
                
                # Extract activity type from icon
                activity_type = ""
                icon_elem = item.find("img", class_="activityicon")
                if icon_elem:
                    activity_type = icon_elem.get("alt", "")
                
                # Extract module ID if available
                module_id = item.get("id", "").replace("module-", "") if item.get("id", "").startswith("module-") else ""
                
                activity_data = {
                    "name": name,
                    "type": activity_type,
                    "link": link,
                    "module_id": module_id
                }
                exam_candidates.extend(_extract_exam_candidates(name, f"activity:{module_id or name}"))
                
                # If it's a folder link, extract folder ID
                if "mod/folder/view.php" in link:
                    folder_id_match = re.search(r'id=(\d+)', link)
                    if folder_id_match:
                        activity_data["folder_id"] = folder_id_match.group(1)
                        activity_data["file_links"] = []  # Would be populated by fetching folder page
                
                activities.append(activity_data)

    outline_sections, section_exam_candidates = _collect_outline_sections(main_content)
    exam_candidates.extend(section_exam_candidates)
    page_text = _clean_text(main_content.get_text(" ", strip=True))[:4000]
    
    if activities:
        save_json(outline_file, {
            "course_id": course_id,
            "course_name": course_name,
            "activities": activities,
            "total_activities": len(activities),
            "outline_sections": outline_sections,
            "exam_candidates": exam_candidates,
            "page_text": page_text,
        })
        print(f"[+] Saved {len(activities)} activities for {course_name}")
    else:
        save_json(outline_file, {
            "course_id": course_id,
            "course_name": course_name,
            "activities": [],
            "total_activities": 0,
            "outline_sections": outline_sections,
            "exam_candidates": exam_candidates,
            "page_text": page_text,
        })
        print(f"[-] No activities found for {course_name}")
