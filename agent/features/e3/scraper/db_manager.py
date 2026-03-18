"""
Database manager for file links storage
Uses JSON files as a simple database system
"""
import os
import json
from typing import Dict, List, Optional
from . import config
from .utils import load_json, save_json

LINKS_DB_FILE = os.path.join(config.BASE_DIR, "file_links_db.json")

def init_links_db():
    """Initialize the file links database if it doesn't exist."""
    if not os.path.exists(LINKS_DB_FILE):
        save_json(LINKS_DB_FILE, {})
    return load_json(LINKS_DB_FILE) or {}

def get_course_links(course_id: str) -> Dict:
    """Get all file links for a specific course."""
    db = init_links_db()
    return db.get(course_id, {
        "handouts": [],
        "assignments": {},
        "submitted_files": {}
    })

def save_course_links(course_id: str, links: Dict):
    """Save file links for a course."""
    db = init_links_db()
    db[course_id] = links
    save_json(LINKS_DB_FILE, db)

def add_handout_link(course_id: str, folder_name: str, file_name: str, file_url: str):
    """Add a handout file link."""
    links = get_course_links(course_id)
    if "handouts" not in links:
        links["handouts"] = []
    
    # Check if link already exists
    for item in links["handouts"]:
        if item.get("folder") == folder_name and item.get("name") == file_name:
            item["url"] = file_url
            item["downloaded"] = False
            save_course_links(course_id, links)
            return
    
    links["handouts"].append({
        "folder": folder_name,
        "name": file_name,
        "url": file_url,
        "downloaded": False
    })
    save_course_links(course_id, links)

def add_assignment_file_link(course_id: str, assignment_title: str, file_name: str, file_url: str, file_type: str = "web"):
    """Add an assignment file link (web attachment or submitted file)."""
    links = get_course_links(course_id)
    if "assignments" not in links:
        links["assignments"] = {}
    
    if assignment_title not in links["assignments"]:
        links["assignments"][assignment_title] = {
            "web_files": [],
            "submitted_files": []
        }
    
    file_entry = {
        "name": file_name,
        "url": file_url,
        "downloaded": False
    }
    
    if file_type == "web":
        # Check if already exists
        for item in links["assignments"][assignment_title]["web_files"]:
            if item.get("name") == file_name:
                item["url"] = file_url
                item["downloaded"] = False
                save_course_links(course_id, links)
                return
        links["assignments"][assignment_title]["web_files"].append(file_entry)
    else:  # submitted
        # Check if already exists
        for item in links["assignments"][assignment_title]["submitted_files"]:
            if item.get("name") == file_name:
                item["url"] = file_url
                item["downloaded"] = False
                save_course_links(course_id, links)
                return
        links["assignments"][assignment_title]["submitted_files"].append(file_entry)
    
    save_course_links(course_id, links)

def mark_file_downloaded(course_id: str, file_type: str, file_path: str, assignment_title: Optional[str] = None):
    """Mark a file as downloaded."""
    links = get_course_links(course_id)
    
    if file_type == "handout":
        for item in links.get("handouts", []):
            if item.get("name") == os.path.basename(file_path):
                item["downloaded"] = True
                item["local_path"] = file_path
                break
    elif file_type == "assignment_web" and assignment_title:
        if assignment_title in links.get("assignments", {}):
            for item in links["assignments"][assignment_title].get("web_files", []):
                if item.get("name") == os.path.basename(file_path):
                    item["downloaded"] = True
                    item["local_path"] = file_path
                    break
    elif file_type == "assignment_submitted" and assignment_title:
        if assignment_title in links.get("assignments", {}):
            for item in links["assignments"][assignment_title].get("submitted_files", []):
                if item.get("name") == os.path.basename(file_path):
                    item["downloaded"] = True
                    item["local_path"] = file_path
                    break
    
    save_course_links(course_id, links)

def get_all_links() -> Dict:
    """Get all file links for all courses."""
    return init_links_db()

def get_all_file_links_for_course(course_id: str) -> Dict:
    """Get all file links for a specific course (compatibility function)."""
    return get_course_links(course_id)

