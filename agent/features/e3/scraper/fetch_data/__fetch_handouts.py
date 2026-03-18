import os
import requests
from bs4 import BeautifulSoup
from ..utils import ensure_course_folder, safe_name
from urllib.parse import urljoin
from .. import config
from .. import db_manager

def fetch_handouts(course_id, course_name, session, cookies, save_links_only=True):
    """
    Fetch handouts and save links to database.
    If save_links_only is False, also download files (for backward compatibility).
    """
    course_name = safe_name(course_name)

    # Setup course folder
    folder = ensure_course_folder(course_id, course_name)
    folder = safe_name(os.path.basename(folder))  # sanitize name only
    handout_folder = os.path.join(config.BASE_DIR, folder, "handouts")
    os.makedirs(handout_folder, exist_ok=True)

    # Step 1: visit course main page
    url = f"{config.E3_ASSIGNMENTS_URL}?courseid={course_id}&lang=zh_tw"
    try:
        resp = session.get(url, cookies=cookies)
        resp.raise_for_status()
    except Exception as e:
        print(f"[!] Failed to fetch handouts page for {course_name}: {e}")
        return
    
    soup = BeautifulSoup(resp.text, "html.parser")

    # Step 2: parse table rows
    rows = soup.select("td.cell.c1")  # folder/class name
    file_cells = soup.select("td.cell.c3")  # download links

    if len(rows) != len(file_cells):
        print("[!] Warning: number of folder cells and file cells mismatch")

    print(f"[+] Updating handouts links...")
    for i in range(len(rows)):
        folder_name = safe_name(rows[i].get_text(strip=True))
        folder_path = os.path.join(handout_folder, folder_name)
        os.makedirs(folder_path, exist_ok=True)

        links = file_cells[i].select("a[href]")
        for link in links:
            file_url = link["href"]
            filename = safe_name(link.get_text(strip=True))
            
            # Make URL absolute
            abs_url = urljoin(resp.url, file_url)
            
            # Save link to database
            db_manager.add_handout_link(course_id, folder_name, filename, abs_url)
            
            # Only download if save_links_only is False
            if not save_links_only:
                filepath = os.path.join(folder_path, filename)
                if not os.path.exists(filepath):
                    try:
                        print(f"[+] Downloading {filename} -> {folder_name}")
                        file_resp = session.get(abs_url, cookies=cookies)
                        file_resp.raise_for_status()
                        with open(filepath, "wb") as f:
                            f.write(file_resp.content)
                        db_manager.mark_file_downloaded(course_id, "handout", filepath)
                    except Exception as e:
                        print(f"[!] Failed to download {filename}: {e}")
                else:
                    db_manager.mark_file_downloaded(course_id, "handout", filepath)
    
    print(f"[+] Handouts links updated for {course_name}")
