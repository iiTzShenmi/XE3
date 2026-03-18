"""
Fetch homework/assignments page data
"""
import os
import requests
from bs4 import BeautifulSoup
from ..utils import save_json, ensure_course_folder, safe_name
from .. import config

def fetch_homework(course_id, course_name, session, cookies):
    """Fetch homework page data for a specific course."""
    course_name = safe_name(course_name)
    folder = ensure_course_folder(course_id, course_name)
    homework_file = os.path.join(folder, "homework_page.json")
    
    url = f"{config.E3_ASSIGNMENTS_URL}?courseid={course_id}&scope=assignment&lang=zh_tw"
    try:
        resp = session.get(url, cookies=cookies)
        resp.raise_for_status()
    except Exception as e:
        print(f"[!] Failed to fetch homework page for {course_name}: {e}")
        return
    
    soup = BeautifulSoup(resp.text, "html.parser")
    main_content = soup.find("section", id="region-main")
    if not main_content:
        print(f"[-] No homework content found for {course_name}")
        return
    
    homeworks = []
    
    # Find all homework tables (could be multiple sections: in-progress, submitted, etc.)
    tables = main_content.find_all("table", class_="generaltable")
    
    for table in tables:
        rows = table.find_all("tr")[1:]  # Skip header row
        
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 4:
                # Extract homework name
                name = cells[0].get_text(strip=True) if cells[0] else ""
                
                # Extract start time
                start_time = cells[1].get_text(strip=True) if len(cells) > 1 else "-"
                
                # Extract end time
                end_time = cells[2].get_text(strip=True) if len(cells) > 2 else "-"
                
                # Extract submission status
                status_text = cells[3].get_text(strip=True) if len(cells) > 3 else ""
                # Parse status: "15 個已繳\n0 個未繳"
                import re
                submitted = 0
                not_submitted = 0
                if status_text:
                    submitted_match = re.search(r'(\d+)\s*個已繳', status_text)
                    not_submitted_match = re.search(r'(\d+)\s*個未繳', status_text)
                    if submitted_match:
                        submitted = int(submitted_match.group(1))
                    if not_submitted_match:
                        not_submitted = int(not_submitted_match.group(1))
                
                # Extract view link
                view_link = ""
                if len(cells) > 4:
                    link_elem = cells[4].find("a")
                    if link_elem:
                        view_link = link_elem.get("href", "")
                
                if name:
                    homeworks.append({
                        "name": name,
                        "start_time": start_time,
                        "end_time": end_time,
                        "submitted_count": submitted,
                        "not_submitted_count": not_submitted,
                        "view_link": view_link
                    })
    
    if homeworks:
        save_json(homework_file, {
            "course_id": course_id,
            "course_name": course_name,
            "homeworks": homeworks,
            "total_homeworks": len(homeworks)
        })
        print(f"[+] Saved {len(homeworks)} homeworks for {course_name}")
    else:
        print(f"[-] No homeworks found for {course_name}")

