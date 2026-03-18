"""
Fetch course outline page data
"""
import os
import requests
from bs4 import BeautifulSoup
from ..utils import save_json, ensure_course_folder, safe_name
from .. import config
import re

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
                
                # If it's a folder link, extract folder ID
                if "mod/folder/view.php" in link:
                    folder_id_match = re.search(r'id=(\d+)', link)
                    if folder_id_match:
                        activity_data["folder_id"] = folder_id_match.group(1)
                        activity_data["file_links"] = []  # Would be populated by fetching folder page
                
                activities.append(activity_data)
    
    if activities:
        save_json(outline_file, {
            "course_id": course_id,
            "course_name": course_name,
            "activities": activities,
            "total_activities": len(activities)
        })
        print(f"[+] Saved {len(activities)} activities for {course_name}")
    else:
        print(f"[-] No activities found for {course_name}")

