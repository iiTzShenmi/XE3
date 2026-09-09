from bs4 import BeautifulSoup
import os
from ..utils import safe_name, save_json
from .. import config

def extract_course():
    """Extract course list from saved HTML and return course dict."""
    if not os.path.exists(config.E3_MY_HTML):
        print(f"[!] Error: {config.E3_MY_HTML} not found")
        return {}
    
    try:
        with open(config.E3_MY_HTML, "r", encoding="utf-8") as f:
            html = f.read()
    except Exception as e:
        print(f"[!] Error reading HTML file: {e}")
        return {}

    soup = BeautifulSoup(html, "html.parser")
    courses = {}
    semester_filter = config.current_semester_filter()

    # find div
    for div in soup.find_all("div", class_="layer2_right_current_course_stu_link"):
        a_tag = div.find("a", class_="course-link")
        if a_tag:
            text = ' '.join(a_tag.get_text(strip=True).split())  # 去掉多餘空白
            href = a_tag.get("href", "").replace("\n", "")

            # get course ID
            if "id=" in href:
                course_id = href.split("id=")[1].split("&")[0]  # Handle multiple params
                
                # Apply semester filter if configured
                if semester_filter in text:
                    courses[course_id] = safe_name(text)

    # save as JSON
    try:
        save_json(config.COURSES_FILE, courses)
        print(f"[+] Success, found {len(courses)} courses for {semester_filter}")
    except Exception as e:
        print(f"[!] Error saving courses file: {e}")

    return courses
