"""
Fetch timetable page data
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

def fetch_timetable(course_id, course_name, session, cookies):
    """Fetch timetable page data for a specific course."""
    course_name = safe_name(course_name)
    folder = ensure_course_folder(course_id, course_name)
    timetable_file = os.path.join(folder, "timetable.json")
    
    # Use the timetable URL from config or construct it
    url = f"{config.E3_BASE_URL}/local/courseextension/timetable.php?courseid={course_id}&scopec=1"
    try:
        resp = session.get(url, cookies=cookies)
        resp.raise_for_status()
    except Exception as e:
        print(f"[!] Failed to fetch timetable page for {course_name}: {e}")
        return
    
    soup = BeautifulSoup(resp.text, "html.parser")
    main_content = soup.find("section", id="region-main")
    if not main_content:
        print(f"[-] No timetable content found for {course_name}")
        return
    
    # Check for iframe
    iframe = main_content.find("iframe")
    iframe_src = iframe.get("src", "") if iframe else ""
    
    timetable_data = {}
    exam_candidates = []
    
    # Try to fetch iframe content if it's a relative URL
    if iframe_src and not iframe_src.startswith("http"):
        from urllib.parse import urljoin
        # If iframe src is relative, try to fetch it
        iframe_url = f"{config.E3_BASE_URL}{iframe_src}" if iframe_src.startswith("/") else urljoin(url, iframe_src)
        try:
            iframe_resp = session.get(iframe_url, cookies=cookies)
            iframe_resp.raise_for_status()
            iframe_soup = BeautifulSoup(iframe_resp.text, "html.parser")
            
            # Extract all tables with id="tbl_*"
            tables = {}
            
            # Find tbl_object (main course info)
            tbl_object = iframe_soup.find("table", id="tbl_object")
            if tbl_object:
                tables["course_info"] = {}
                all_spans = tbl_object.find_all("span", attrs={"name": True})
                for span in all_spans:
                    span_name = span.get("name", "")
                    span_text = span.get_text(strip=True)
                    
                    if span_name == "cos_cname":
                        span_text = re.sub(r'^\(中文\)\s*', '', span_text)
                        tables["course_info"]["course_name_zh"] = span_text
                    elif span_name == "cos_ename":
                        span_text = re.sub(r'^\(英文\)\s*', '', span_text)
                        tables["course_info"]["course_name_en"] = span_text
                    elif span_name == "tea_name":
                        tables["course_info"]["teacher"] = span_text
                    elif span_name == "dep_name":
                        tables["course_info"]["department"] = span_text
                    elif span_name == "cos_id":
                        tables["course_info"]["course_id"] = span_text
                    elif span_name == "cos_code":
                        tables["course_info"]["course_code"] = span_text
                    elif span_name == "cos_credit":
                        tables["course_info"]["credits"] = span_text
                    elif span_name == "sel_type":
                        tables["course_info"]["required_elective"] = span_text
                    elif span_name == "cos_time":
                        tables["course_info"]["schedule"] = span_text
                    elif span_name == "col_prerequisite":
                        tables["course_info"]["prerequisite"] = span_text
                    elif span_name == "col_outline":
                        tables["course_info"]["outline"] = span_text
                        exam_candidates.extend(_extract_exam_candidates(span_text, "timetable:course_outline"))
                    elif span_name == "col_textbook":
                        tables["course_info"]["textbook"] = span_text
                exam_candidates.extend(_extract_exam_candidates(" ".join(tables["course_info"].values()), "timetable:course_info"))

            extra_tables = {}
            for table in iframe_soup.find_all("table", id=re.compile(r"^tbl_")):
                table_id = table.get("id", "")
                if table_id == "tbl_object":
                    continue
                rows = []
                for tr in table.find_all("tr"):
                    cells = [_clean_text(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])]
                    cells = [cell for cell in cells if cell]
                    if cells:
                        rows.append(cells)
                        exam_candidates.extend(_extract_exam_candidates(" ".join(cells), f"timetable:{table_id}"))
                if rows:
                    extra_tables[table_id] = rows

            if extra_tables:
                tables["tables"] = extra_tables

            timetable_data = tables
            
        except Exception as e:
            print(f"[!] Failed to fetch iframe content: {e}")
            timetable_data["iframe_parse_error"] = str(e)
    
    save_json(timetable_file, {
        "course_id": course_id,
        "course_name": course_name,
        "iframe_src": iframe_src,
        "timetable_data": timetable_data,
        "exam_candidates": exam_candidates,
    })
    print(f"[+] Saved timetable data for {course_name}")
