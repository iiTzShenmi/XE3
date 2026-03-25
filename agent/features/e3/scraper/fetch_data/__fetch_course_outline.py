"""
Fetch course outline page data
"""
import os
import re
from urllib.parse import urljoin, urlparse, parse_qs

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


def _extract_activity_meta(item, page_url):
    link_elem = item.find("a", class_=lambda x: x and "aalink" in x.lower()) or item.find("a", href=True)
    if not link_elem:
        return None

    name_elem = link_elem.find("span", class_="instancename") or link_elem
    for accesshide in name_elem.find_all("span", class_="accesshide"):
        accesshide.decompose()
    name = _clean_text(name_elem.get_text(" ", strip=True))
    if not name:
        return None

    link = urljoin(page_url, link_elem.get("href", ""))
    parsed = urlparse(link)
    query = parse_qs(parsed.query)
    path = parsed.path or ""
    module_type = ""
    if "/mod/" in path:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            module_type = parts[1]

    icon_elem = item.find("img", class_=lambda x: x and "activityicon" in x.lower())
    icon_alt = _clean_text(icon_elem.get("alt")) if icon_elem else ""
    module_id = item.get("id", "").replace("module-", "") if item.get("id", "").startswith("module-") else ""
    instance_id = (query.get("id") or [""])[0]
    availability_text = _clean_text(" ".join(node.get_text(" ", strip=True) for node in item.select(".availabilityinfo, .description .dimmed_text, .showavailability")))
    description = _clean_text(" ".join(node.get_text(" ", strip=True) for node in item.select(".description, .contentafterlink, .activity-information")))
    visible = "dimmed" not in " ".join(item.get("class", []))

    payload = {
        "name": name,
        "type": icon_alt,
        "module_type": module_type,
        "link": link,
        "module_id": module_id,
        "instance_id": instance_id,
        "icon_alt": icon_alt,
        "description": description,
        "availability": availability_text,
        "visible": visible,
    }

    if "mod/folder/view.php" in link and instance_id:
        payload["folder_id"] = instance_id
    if "mod/forum/view.php" in link and instance_id:
        payload["forum_id"] = instance_id
    if "mod/assign/view.php" in link and instance_id:
        payload["assign_id"] = instance_id
    if "mod/quiz/view.php" in link and instance_id:
        payload["quiz_id"] = instance_id
    if "mod/page/view.php" in link and instance_id:
        payload["page_id"] = instance_id
    if "mod/url/view.php" in link and instance_id:
        payload["url_id"] = instance_id

    return payload

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
    sections = []

    section_nodes = main_content.select("li[id^='section-'], .course-section, .section.main")
    if not section_nodes:
        section_nodes = [main_content]

    for sec_idx, section in enumerate(section_nodes, start=1):
        section_title = _clean_text(" ".join(node.get_text(" ", strip=True) for node in section.select(".sectionname, h3.sectionname, h4.sectionname, .section-title"))) or f"Section {sec_idx}"
        section_id = section.get("id", "")
        section_summary = _clean_text(" ".join(node.get_text(" ", strip=True) for node in section.select(".summary, .content .summarytext")))[:1200]

        section_payload = {
            "section_id": section_id,
            "section_title": section_title,
            "summary": section_summary,
            "activities": [],
        }
        exam_candidates.extend(_extract_exam_candidates(f"{section_title} {section_summary}", f"course_section:{section_id or section_title}"))

        for item in section.find_all("li", class_=lambda x: x and "activity" in " ".join(x).lower()):
            activity_data = _extract_activity_meta(item, resp.url)
            if not activity_data:
                continue
            activity_data["section_id"] = section_id
            activity_data["section_title"] = section_title
            exam_candidates.extend(_extract_exam_candidates(f"{activity_data.get('name', '')} {activity_data.get('description', '')}", f"activity:{activity_data.get('module_id') or activity_data.get('instance_id') or activity_data.get('name')}"))
            section_payload["activities"].append(activity_data)
            activities.append(activity_data)

        if section_payload["activities"] or section_summary:
            sections.append(section_payload)

    outline_sections, section_exam_candidates = _collect_outline_sections(main_content)
    exam_candidates.extend(section_exam_candidates)
    page_text = _clean_text(main_content.get_text(" ", strip=True))[:4000]
    
    payload = {
        "course_id": course_id,
        "course_name": course_name,
        "source_url": url,
        "activities": activities,
        "total_activities": len(activities),
        "sections": sections,
        "outline_sections": outline_sections,
        "exam_candidates": exam_candidates,
        "page_text": page_text,
    }
    save_json(outline_file, payload)
    if activities:
        print(f"[+] Saved {len(activities)} activities for {course_name}")
    else:
        print(f"[-] No activities found for {course_name}")
