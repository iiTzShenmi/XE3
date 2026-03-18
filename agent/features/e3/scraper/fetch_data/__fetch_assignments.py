import os
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from ..utils import save_json, load_json, ensure_course_folder, safe_name
from .. import config
from .. import db_manager

def _find_nearest_submit_time(node):
    """Try to find the fileuploadsubmissiontime related to node by searching siblings/nearby nodes."""
    # 1) try immediate next siblings (small window)
    sib = node.next_sibling
    steps = 0
    while sib and steps < 8:
        if getattr(sib, "get", None):
            classes = sib.get("class") or []
            if "fileuploadsubmissiontime" in classes:
                return sib.get_text(strip=True)
        sib = sib.next_sibling
        steps += 1

    # 2) try previous siblings
    sib = node.previous_sibling
    steps = 0
    while sib and steps < 8:
        if getattr(sib, "get", None):
            classes = sib.get("class") or []
            if "fileuploadsubmissiontime" in classes:
                return sib.get_text(strip=True)
        sib = sib.previous_sibling
        steps += 1

    # 3) fallback: global next occurrence
    time_div = node.find_next("div", class_="fileuploadsubmissiontime")
    if time_div:
        return time_div.get_text(strip=True)

    return None


def fetch_assignments(course_id, course_name, session, cookies):
    course_name = safe_name(course_name)
    course_folder = ensure_course_folder(course_id, course_name)

    # Create homework folder
    homework_folder = os.path.join(course_folder, "homework")
    os.makedirs(homework_folder, exist_ok=True)

    assignments_file = os.path.join(homework_folder, "assignments.json")
    prev_assignments = load_json(assignments_file) or []
    # Map previous entries by title for quick lookup
    prev_map = {a.get("title"): a for a in prev_assignments}

    assignments = []

    url = f"{config.E3_ASSIGNMENTS_URL}?courseid={course_id}&scope=assignment&lang=zh_tw"
    try:
        resp = session.get(url, cookies=cookies)
        resp.raise_for_status()
    except Exception as e:
        print(f"[!] Failed to fetch assignments page for {course_name}: {e}")
        return
    
    soup = BeautifulSoup(resp.text, "html.parser")

    sections = {
    # Chinese UI
    "進行中作業": "in_progress",
    "已繳作業": "submitted",
    "逾期未繳作業": "overdue",
    "預告作業": "upcoming",
    # English UI
    "In progress": "in_progress",
    "Submitted": "submitted",
    "Overdue": "overdue",
    "Upcoming": "upcoming"
}


    for h4 in soup.find_all("h4"):
        section_name = h4.get_text(strip=True).replace("", "").strip()
        if section_name not in sections:
            continue
        category = sections[section_name]

        table = None
        for sibling in h4.find_all_next(["table"], limit=5):
            if "generaltable" in (sibling.get("class") or []):
                table = sibling
                break
        if not table:
            continue

        if not table:
            continue

        for row in table.select("tbody tr"):
            cols = row.find_all("td")
            if len(cols) < 5:
                continue

            title = cols[0].get_text(strip=True)
            start_time = cols[1].get_text(strip=True)
            due_time = cols[2].get_text(strip=True)
            status = cols[3].get_text("\n", strip=True)

            link_tag = cols[4].select_one("a")
            detail_url = link_tag["href"] if link_tag else None

            content = ""
            attachments = []       # instructor-uploaded files (web)
            submitted_files = []   # student's submitted files
            assignment_folder = os.path.join(homework_folder, safe_name(title))
            os.makedirs(assignment_folder, exist_ok=True)
            submitted_folder = os.path.join(assignment_folder, "submitted")
            os.makedirs(submitted_folder, exist_ok=True)

            prev_entry = prev_map.get(title, {})

            if detail_url:
                try:
                    detail_resp = session.get(detail_url, cookies=cookies)
                    detail_resp.raise_for_status()
                    detail_soup = BeautifulSoup(detail_resp.text, "html.parser")

                    # === DESCRIPTION: remove file/submission nodes so they don't leak into content ===
                    # Clone (or operate on copy) — we'll remove nodes from soup copy to extract a clean description.
                    desc_node = detail_soup.select_one("div.assignintro, div.intro, div.generalbox")
                    if desc_node:
                        # Remove any file blocks or submission time nodes inside description before extracting text
                        for bad in desc_node.select("div.fileuploadsubmission, div.fileuploadsubmissiontime"):
                            bad.decompose()
                        content = desc_node.get_text("\n", strip=True)
                    else:
                        # fallback: some pages put content under .description or #intro
                        fallback = detail_soup.select_one("#intro, div.description, div.content")
                        if fallback:
                            for bad in fallback.select("div.fileuploadsubmission, div.fileuploadsubmissiontime"):
                                bad.decompose()
                            content = fallback.get_text("\n", strip=True)

                    # === INSTRUCTOR ATTACHMENTS (usually in description area or attachments block) ===
                    # Look for fileuploadsubmission anchors that are NOT assignsubmission_file
                    for f in detail_soup.select("div.fileuploadsubmission a"):
                        furl = f.get("href")
                        fname = safe_name(f.get_text(strip=True))
                        if not furl:
                            continue
                        abs_url = urljoin(detail_resp.url, furl)
                        if "assignsubmission_file" in furl or "submission_files" in furl:
                            # student-submitted file — handled below; skip here
                            continue
                        # Otherwise treat as web/instructor file
                        attachments.append({"name": fname, "url": abs_url, "type": "web"})
                        # Save link to database instead of downloading
                        from .. import db_manager
                        db_manager.add_assignment_file_link(course_id, title, fname, abs_url, "web")

                    # === STUDENT SUBMISSIONS: locate fileuploadsubmission blocks that are "submitted" files ===
                    # For each fileuploadsubmission element, decide whether it's a student's file by its href.
                    for sub_div in detail_soup.select("div.fileuploadsubmission"):
                        a = sub_div.select_one("a")
                        if not a or not a.get("href"):
                            continue
                        furl = a["href"]
                        abs_url = urljoin(detail_resp.url, furl)
                        fname = safe_name(a.get_text(strip=True))

                        if "assignsubmission_file" in furl or "submission_files" in furl:
                            # Find nearest submission time
                            s_time = _find_nearest_submit_time(sub_div) or None

                            # Check previous record to decide if we need to download
                            need_download = True
                            prev_subs = prev_entry.get("submitted_files", []) if prev_entry else []
                            prev_match = next((ps for ps in prev_subs if ps.get("name") == fname), None)
                            prev_time = prev_match.get("submit_time") if prev_match else None
                            if prev_time and s_time and prev_time == s_time and os.path.exists(os.path.join(submitted_folder, fname)):
                                need_download = False

                            submitted_files.append({
                                "name": fname,
                                "url": abs_url,
                                "type": "submitted",
                                "submit_time": s_time
                            })
                            
                            # Save link to database instead of downloading
                            from .. import db_manager
                            db_manager.add_assignment_file_link(course_id, title, fname, abs_url, "submitted")
                            
                            # Only download if file already exists locally (for backward compatibility)
                            local_path = os.path.join(submitted_folder, fname)
                            if os.path.exists(local_path):
                                db_manager.mark_file_downloaded(course_id, "assignment_submitted", local_path, title) 

                except Exception as e:
                    print(f"[!] Failed to fetch detail page {detail_url}: {e}")

            assignments.append({
                "title": title,
                "start_time": start_time,
                "due_time": due_time,
                "status": status,
                "url": detail_url,
                "category": category,
                "content": content,
                "attachments": attachments,
                "submitted_files": submitted_files
            })

    #remove same assignment using priority
    priority = {"submitted": 3, "in_progress": 2, "overdue": 1, "upcoming": 0}
    unique = {}

    for a in assignments:
        t = a["title"]
        if t not in unique:
            unique[t] = a
        else:
            # Keep the one with higher priority
            if priority.get(a["category"], 0) > priority.get(unique[t]["category"], 0):
                unique[t] = a
    
    #assignments = list(unique.values())
    assignments = sorted(list(unique.values()), key = lambda assignments : assignments["title"])
    #print(assignments)
    #print(assignments)
    if assignments:
        save_json(assignments_file, assignments)
        print(f"[+] Saved {len(assignments)} assignments for {course_name}")
    else:
        print(f"[-] No assignments to save for {course_name}")
