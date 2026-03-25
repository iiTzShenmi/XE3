"""
Fetch timetable page data
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


def _collect_script_urls(soup, base_url):
    urls = []
    for script in soup.find_all("script", src=True):
        abs_url = urljoin(base_url, script.get("src", ""))
        if abs_url and abs_url not in urls:
            urls.append(abs_url)
    return urls


def _collect_hidden_params(soup):
    hidden = {}
    for node in soup.select("input[type='hidden'][name]"):
        name = str(node.get("name") or "").strip()
        value = str(node.get("value") or "").strip()
        if name:
            hidden[name] = value
    return hidden


def _fetch_json(session, url, payload, cookies=None):
    resp = session.post(url, data=payload, cookies=cookies)
    resp.raise_for_status()
    return resp.json()


def _fetch_text(session, url, payload, cookies=None):
    resp = session.post(url, data=payload, cookies=cookies)
    resp.raise_for_status()
    return resp.text


def _strip_numeric_keys(value):
    if isinstance(value, list):
        return [_strip_numeric_keys(item) for item in value]
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.isdigit():
                continue
            cleaned[key_text] = _strip_numeric_keys(item)
        return cleaned
    return value

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
    
    iframe = main_content.find("iframe")
    iframe_src = iframe.get("src", "") if iframe else ""
    timetable_data = {}
    exam_candidates = []
    page_nav_links = []
    for anchor in soup.select("a[href][data-key]"):
        href = urljoin(resp.url, anchor.get("href", ""))
        label = _clean_text(anchor.get_text(" ", strip=True))
        data_key = _clean_text(anchor.get("data-key"))
        if href and label:
            page_nav_links.append({"label": label, "href": href, "data_key": data_key})

    iframe_meta = {}
    course_outline_data = {}
    if iframe_src:
        iframe_url = urljoin(url, iframe_src)
        iframe_host = urlparse(iframe_url).netloc
        iframe_meta = {
            "src": iframe_url,
            "host": iframe_host,
            "query": {key: values[0] if len(values) == 1 else values for key, values in parse_qs(urlparse(iframe_url).query).items()},
        }
        try:
            iframe_resp = session.get(iframe_url, cookies=cookies if iframe_url.startswith(config.E3_BASE_URL) else None)
            iframe_resp.raise_for_status()
            iframe_soup = BeautifulSoup(iframe_resp.text, "html.parser")
            iframe_meta.update(
                {
                    "page_title": _clean_text(iframe_soup.title.get_text(" ", strip=True) if iframe_soup.title else ""),
                    "hidden_params": _collect_hidden_params(iframe_soup),
                    "script_urls": _collect_script_urls(iframe_soup, iframe_resp.url),
                    "body_text_preview": _clean_text(iframe_soup.get_text(" ", strip=True))[:1500],
                }
            )

            if iframe_host == "timetable.nycu.edu.tw":
                endpoint_base = f"{urlparse(iframe_url).scheme}://{iframe_host}/"
                hidden_params = iframe_meta.get("hidden_params") or {}
                query_params = iframe_meta.get("query") or {}
                req_payload = {
                    "acy": hidden_params.get("acy") or query_params.get("Acy") or "",
                    "sem": hidden_params.get("sem") or query_params.get("Sem") or "",
                    "cos_id": hidden_params.get("cos_id") or query_params.get("CrsNo") or str(course_id),
                    "user": hidden_params.get("user") or "",
                    "_token": hidden_params.get("_token") or "",
                }
                lang_value = hidden_params.get("lang") or query_params.get("lang") or "zh-tw"
                course_outline_data["request_payload"] = dict(req_payload)
                course_outline_data["lang"] = lang_value
                try:
                    course_outline_data["view_html"] = _fetch_text(
                        session,
                        f"{endpoint_base}?r=main/getViewHtmlContents",
                        {"fun": "timetable_crsoutline", "fLang": lang_value},
                    )
                except Exception as e:
                    course_outline_data["view_html_error"] = str(e)
                for key, endpoint in (
                    ("base", "getCrsOutlineBase"),
                    ("description", "getCrsOutlineDescription"),
                    ("syllabus", "getCrsOutlineSyllabuses"),
                    ("optional", "getCrsOutlineOptional"),
                ):
                    try:
                        course_outline_data[key] = _fetch_json(
                            session,
                            f"{endpoint_base}?r=main/{endpoint}",
                            req_payload,
                        )
                    except Exception as e:
                        course_outline_data[f"{key}_error"] = str(e)

                for key in ("base", "description", "syllabus", "optional"):
                    if key in course_outline_data:
                        course_outline_data[f"{key}_normalized"] = _strip_numeric_keys(course_outline_data[key])

                base_data = course_outline_data.get("base") or {}
                desc_data = course_outline_data.get("description") or {}
                if isinstance(base_data, dict):
                    exam_candidates.extend(_extract_exam_candidates(" ".join(str(v) for v in base_data.values()), "timetable:endpoint_base"))
                if isinstance(desc_data, dict):
                    exam_candidates.extend(_extract_exam_candidates(" ".join(str(v) for v in desc_data.values()), "timetable:endpoint_description"))
                syllabus_data = course_outline_data.get("syllabus") or []
                if isinstance(syllabus_data, list):
                    for row in syllabus_data:
                        if isinstance(row, dict):
                            exam_candidates.extend(_extract_exam_candidates(" ".join(str(v) for v in row.values()), "timetable:endpoint_syllabus"))

            tables = {}
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
            iframe_meta["iframe_parse_error"] = str(e)

    save_json(timetable_file, {
        "course_id": course_id,
        "course_name": course_name,
        "iframe_src": iframe_src,
        "iframe_meta": iframe_meta,
        "page_nav_links": page_nav_links,
        "course_outline_data": course_outline_data,
        "timetable_data": timetable_data,
        "exam_candidates": exam_candidates,
    })
    print(f"[+] Saved timetable data for {course_name}")
