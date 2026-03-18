import os
import requests
from bs4 import BeautifulSoup
from ..utils import save_json, ensure_course_folder
from .. import config

def fetch_grades(course_id, course_name, session, cookies):
    """Fetch grades for a specific course."""
    folder = ensure_course_folder(course_id, course_name)
    grades_file = os.path.join(folder, "grades.json")

    url = f"{config.E3_GRADES_URL}?id={course_id}&lang=zh_tw"
    try:
        resp = session.get(url, cookies=cookies)
        resp.raise_for_status()
    except Exception as e:
        print(f"[!] Failed to fetch grades page for {course_name}: {e}")
        return

    soup = BeautifulSoup(resp.text, "html.parser")
    grades = {}
    main_content = soup.find("section", id="region-main") or soup
    grades_table = main_content.find("table", class_="generaltable") or main_content.find("table")
    if not grades_table:
        print(f"[-] No grades table found for {course_name}")
        return

    for row in grades_table.find_all("tr")[1:]:
        cols = row.find_all("td")
        if len(cols) < 3:
            continue

        item_elem = cols[0].find("a") or cols[0].find("span")
        item = item_elem.get_text(strip=True) if item_elem else cols[0].get_text(strip=True)
        score = cols[2].get_text(strip=True) if len(cols) > 2 else ""
        grade_range = cols[3].get_text(strip=True) if len(cols) > 3 else ""

        if not item:
            continue

        if score:
            grades[item] = score
        elif grade_range:
            grades[item] = "-"

    if grades:
        save_json(grades_file, grades)
        print(f"[+] Updated {len(grades)} grades for {course_name}")
    else:
        print(f"[-] No grades found for {course_name}")
