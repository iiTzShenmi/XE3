from __future__ import annotations

import re
from typing import Any

from ..utils.common import course_name_for_display


def _parse_float(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_weight_percent(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text == "-":
        return None
    match = re.search(r"(-?\d+(?:\.\d+)?)", text.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_range_max(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text == "-":
        return None
    matches = re.findall(r"(-?\d+(?:\.\d+)?)", text.replace(",", ""))
    if not matches:
        return None
    try:
        numbers = [float(match) for match in matches]
    except ValueError:
        return None
    if not numbers:
        return None
    return max(numbers)


def _weighted_items_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    grades_payload = (payload or {}).get("grades") or {}
    items = grades_payload.get("grade_items") if isinstance(grades_payload, dict) else None
    if not isinstance(items, list):
        return []

    weighted_items: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        if row.get("is_category") or row.get("is_calculated"):
            continue
        weight = _parse_weight_percent(row.get("weight"))
        range_max = _parse_range_max(row.get("range"))
        if weight is None or range_max is None or range_max <= 0:
            continue
        score = _parse_float(row.get("score"))
        weighted_items.append(
            {
                "item_name": str(row.get("item_name") or "").strip() or "未命名項目",
                "weight": weight,
                "range_max": range_max,
                "score": score,
            }
        )
    return weighted_items


def calculate_grade_target(payload: dict[str, Any], target_grade: float) -> dict[str, Any]:
    weighted_items = _weighted_items_from_payload(payload)
    course_id = str((payload or {}).get("_course_id") or "").strip()
    course_name = course_name_for_display((payload or {}).get("course_name") or (payload or {}).get("_folder_name") or "")

    if not weighted_items:
        return {
            "ok": False,
            "reason": "no_weight_data",
            "course_id": course_id,
            "course_name": course_name,
        }

    total_weight = sum(float(item["weight"]) for item in weighted_items)
    completed_items = [item for item in weighted_items if item["score"] is not None]
    remaining_items = [item for item in weighted_items if item["score"] is None]

    earned_weighted = 0.0
    completed_weight = 0.0
    for item in completed_items:
        earned_weighted += float(item["score"]) / float(item["range_max"]) * float(item["weight"])
        completed_weight += float(item["weight"])

    remaining_weight = max(0.0, total_weight - completed_weight)
    target_weighted = (float(target_grade) / 100.0) * total_weight
    required_weighted = target_weighted - earned_weighted

    if remaining_weight <= 0:
        return {
            "ok": True,
            "course_id": course_id,
            "course_name": course_name,
            "target_grade": float(target_grade),
            "total_weight": total_weight,
            "earned_weighted": earned_weighted,
            "completed_weight": completed_weight,
            "remaining_weight": remaining_weight,
            "required_weighted": required_weighted,
            "required_average": None,
            "remaining_items": remaining_items,
            "status": "complete",
        }

    required_average = required_weighted / remaining_weight * 100.0
    per_item_targets = []
    for item in remaining_items:
        needed_score = max(0.0, required_average) / 100.0 * float(item["range_max"])
        per_item_targets.append(
            {
                "item_name": item["item_name"],
                "weight": item["weight"],
                "range_max": item["range_max"],
                "needed_score": needed_score,
            }
        )

    status = "reachable"
    if required_average <= 0:
        status = "already_reached"
    elif required_average > 100:
        status = "impossible"

    return {
        "ok": True,
        "course_id": course_id,
        "course_name": course_name,
        "target_grade": float(target_grade),
        "total_weight": total_weight,
        "earned_weighted": earned_weighted,
        "completed_weight": completed_weight,
        "remaining_weight": remaining_weight,
        "required_weighted": required_weighted,
        "required_average": required_average,
        "remaining_items": remaining_items,
        "per_item_targets": per_item_targets,
        "weighted_item_count": len(weighted_items),
        "status": status,
    }
