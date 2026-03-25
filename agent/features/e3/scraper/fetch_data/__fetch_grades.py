import os
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..utils import save_json, ensure_course_folder
from .. import config


def _clean_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def _row_level(cells):
    classes = " ".join(" ".join(cell.get("class", [])) for cell in cells)
    match = re.search(r"\blevel(\d+)\b", classes)
    return int(match.group(1)) if match else None


def _item_kind(cell):
    icon = cell.select_one("img.itemicon")
    if icon and icon.get("alt"):
        return _clean_text(icon.get("alt"))
    icon_i = cell.select_one("i.itemicon")
    if icon_i and icon_i.get("title"):
        return _clean_text(icon_i.get("title"))
    return ""


def fetch_grades(course_id, course_name, session, cookies):
    """Fetch detailed grades for a specific course while keeping legacy compatibility."""
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
    main_content = soup.find("section", id="region-main") or soup
    grades_table = main_content.find("table", class_="generaltable") or main_content.find("table")
    if not grades_table:
        print(f"[-] No grades table found for {course_name}")
        return

    header_cells = grades_table.select("thead th")
    column_names = [_clean_text(cell.get_text(" ", strip=True)) for cell in header_cells]

    grade_items = []
    legacy_grades = {}
    current_category = ""

    for row in grades_table.find_all("tr")[1:]:
        cols = row.find_all("td")
        if len(cols) < 3:
            continue

        item_cell = cols[0]
        item_elem = item_cell.find("a") or item_cell.find("span")
        item_name = _clean_text(item_elem.get_text(" ", strip=True) if item_elem else item_cell.get_text(" ", strip=True))
        if not item_name:
            continue

        score = _clean_text(cols[2].get_text(" ", strip=True)) if len(cols) > 2 else ""
        weight = _clean_text(cols[1].get_text(" ", strip=True)) if len(cols) > 1 else ""
        grade_range = _clean_text(cols[3].get_text(" ", strip=True)) if len(cols) > 3 else ""
        feedback = _clean_text(cols[4].get_text("\n", strip=True)) if len(cols) > 4 else ""

        link_tag = item_cell.find("a", href=True)
        activity_url = urljoin(resp.url, link_tag["href"]) if link_tag else ""
        item_kind = _item_kind(item_cell)
        level = _row_level(cols)
        row_classes = row.get("class", [])
        is_category = any("cat" in cls.lower() for cls in row_classes) or "category" in item_name.lower()
        is_calculated = "calculator" in " ".join(str(icon.get("class", [])) for icon in item_cell.select("i, img")) or "計算" in item_kind

        if is_category:
            current_category = item_name

        item_payload = {
            "item_name": item_name,
            "score": score or "-",
            "weight": weight,
            "range": grade_range,
            "feedback": feedback,
            "activity_url": activity_url,
            "item_kind": item_kind,
            "level": level,
            "category": current_category if current_category and current_category != item_name else "",
            "is_category": is_category,
            "is_calculated": bool(is_calculated),
            "row_classes": row_classes,
        }
        grade_items.append(item_payload)

        # Preserve the old flat mapping for existing bot logic.
        if not is_category:
            legacy_grades[item_name] = score or "-"

    summary = {
        "total_items": len(grade_items),
        "scored_items": sum(1 for item in grade_items if item.get("score") not in {"", "-", None}),
        "category_count": sum(1 for item in grade_items if item.get("is_category")),
        "calculated_count": sum(1 for item in grade_items if item.get("is_calculated")),
    }

    payload = {
        "course_id": str(course_id),
        "course_name": str(course_name),
        "source_url": url,
        "columns": column_names,
        "summary": summary,
        "grade_items": grade_items,
        "grades": legacy_grades,
    }

    if grade_items:
        save_json(grades_file, payload)
        print(f"[+] Updated {len(grade_items)} grade rows for {course_name}")
    else:
        print(f"[-] No grades found for {course_name}")
