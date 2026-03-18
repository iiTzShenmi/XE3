import json
import requests
from .utils import save_json, safe_name, load_json
from .fetch_data.__fetch_news import fetch_news
from .fetch_data.__fetch_handouts import fetch_handouts
from .fetch_data.__fetch_assignments import fetch_assignments 
from .fetch_data.__fetch_grades import fetch_grades
from .fetch_data.__fetch_homework import fetch_homework
from .fetch_data.__fetch_timetable import fetch_timetable
from .fetch_data.__fetch_course_outline import fetch_course_outline
from . import config

def build_session(cookies=None):
    session = requests.Session()
    session.headers.update({"User-Agent": config.USER_AGENT})
    if cookies and isinstance(cookies, dict):
        session.cookies.update(cookies)
    return session

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

def __update_course_data(session=None, cookies=None):
    """Update course data only (news, assignments, grades) - no file downloads."""
    cookies = cookies or load_cookies()
    if not cookies:
        print("[!] Warning: No cookies found. Please login first.")
        return
    
    courses = load_courses()
    if not courses:
        print("[!] Warning: No courses found. Please fetch courses first.")
        return
    
    session = session or build_session(cookies)

    print(f"[*] Updating course data for {len(courses)} courses")

    for cid, cname in courses.items():
        cname = safe_name(cname)
        print(f"\n=== Updating course data {cid}: {cname} ===")
        try:
            fetch_news(cid, cname, session, cookies)
        except Exception as e:
            print(f"[!] fetch_news failed for {cid}: {e}")
            
        try:
            fetch_assignments(cid, cname, session, cookies)
        except Exception as e:
            print(f"[!] fetch_assignments failed for {cid}: {e}")

        try:
            fetch_grades(cid, cname, session, cookies)
        except Exception as e:
            print(f"[!] fetch_grades failed for {cid}: {e}")
        
        try:
            fetch_homework(cid, cname, session, cookies)
        except Exception as e:
            print(f"[!] fetch_homework failed for {cid}: {e}")
        
        try:
            fetch_timetable(cid, cname, session, cookies)
        except Exception as e:
            print(f"[!] fetch_timetable failed for {cid}: {e}")
        
        try:
            fetch_course_outline(cid, cname, session, cookies)
        except Exception as e:
            print(f"[!] fetch_course_outline failed for {cid}: {e}")

    print("\n[+] Course data update complete!")
    save_json(config.LAST_RUN_FILE, {"status": "success", "type": "data"})

def __update_file_links(session=None, cookies=None):
    """Update file links database (handouts and assignment files) - no downloads."""
    cookies = cookies or load_cookies()
    if not cookies:
        print("[!] Warning: No cookies found. Please login first.")
        return
    
    courses = load_courses()
    if not courses:
        print("[!] Warning: No courses found. Please fetch courses first.")
        return
    
    session = session or build_session(cookies)

    print(f"[*] Updating file links for {len(courses)} courses")

    for cid, cname in courses.items():
        cname = safe_name(cname)
        print(f"\n=== Updating file links {cid}: {cname} ===")
        try:
            # Save links only, no downloads
            fetch_handouts(cid, cname, session, cookies, save_links_only=True)
        except Exception as e:
            print(f"[!] fetch_handouts links failed for {cid}: {e}")
        
        # Assignment file links are already saved in fetch_assignments
        # (they're saved when parsing assignment details)

    print("\n[+] File links update complete!")
    save_json(config.LAST_RUN_FILE, {"status": "success", "type": "links"})

def __update_all(session=None, cookies=None):
    """Update both course data and file links (for backward compatibility)."""
    __update_course_data(session=session, cookies=cookies)
    __update_file_links(session=session, cookies=cookies)
