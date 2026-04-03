from .payloads import attach_message_meta


def build_course_detail_flex(detail, alt_text):
    body_contents = [
        {"type": "text", "text": detail["course_name"], "weight": "bold", "wrap": True, "size": "lg"},
        {"type": "text", "text": detail["course_id"] or "未提供課號", "size": "sm", "color": "#475569"},
        {"type": "separator", "margin": "md"},
        {
            "type": "text",
            "text": f"未完成作業：{detail['homework_count']}　已完成作業：{detail['completed_homework_count']}　行事曆：{detail['calendar_count']}　檔案：{detail['file_count']}",
            "size": "sm",
            "wrap": True,
        },
    ]
    if detail.get("course_info_lines"):
        body_contents.append({"type": "separator", "margin": "md"})
        body_contents.append({"type": "text", "text": "課綱重點", "weight": "bold", "size": "sm", "margin": "md"})
        for line in detail["course_info_lines"]:
            body_contents.append({"type": "text", "text": line, "size": "sm", "wrap": True})
    if detail.get("grade_summary_lines"):
        body_contents.append({"type": "separator", "margin": "md"})
        body_contents.append({"type": "text", "text": "成績摘要", "weight": "bold", "size": "sm", "margin": "md"})
        for line in detail["grade_summary_lines"]:
            body_contents.append({"type": "text", "text": line, "size": "sm", "wrap": True})
    return attach_message_meta(
        {
        "type": "flex",
        "altText": alt_text,
        "contents": {
            "type": "bubble",
            "xe3_meta": {
                "selector_kind": "course_detail",
                "item_title": detail["course_name"],
                "course_name": detail["course_name"],
                "course_id": detail["course_id"],
            },
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#0F766E",
                "paddingAll": "12px",
                "contents": [
                    {"type": "text", "text": "課程詳情", "color": "#FFFFFF", "weight": "bold", "size": "md"},
                ],
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": body_contents,
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "style": "primary",
                        "height": "sm",
                        "color": "#0F766E",
                        "action": {
                            "type": "message",
                            "label": "重點摘要",
                            "text": f"e3 課程摘要 {detail['index']}",
                            "xe3_meta": {
                                "selector_kind": "course_summary",
                                "entry_kind": "course_summary",
                                "item_title": detail["course_name"],
                                "course_name": detail["course_name"],
                                "course_id": detail["course_id"],
                            },
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "height": "sm",
                        "action": {
                            "type": "message",
                            "label": "查看教材",
                            "text": f"e3 檔案資料夾 {detail['course_id'] or detail['course_name']}",
                            "xe3_meta": {
                                "selector_kind": "file_folder",
                                "entry_kind": "course_materials",
                                "item_title": detail["course_name"],
                                "course_name": detail["course_name"],
                                "course_id": detail["course_id"],
                            },
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "height": "sm",
                        "action": {
                            "type": "message",
                            "label": "查看作業",
                            "text": f"e3 課程作業 {detail['course_id'] or detail['course_name']}",
                            "xe3_meta": {
                                "selector_kind": "course_homework_detail",
                                "entry_kind": "course_homework",
                                "item_title": detail["course_name"],
                                "course_name": detail["course_name"],
                                "course_id": detail["course_id"],
                            },
                        },
                    },
                    {
                        "type": "button",
                        "style": "primary",
                        "height": "sm",
                        "color": "#0F766E",
                        "action": {
                            "type": "message",
                            "label": "回到課程列表",
                            "text": "e3 course",
                            "xe3_meta": {
                                "selector_kind": "course_summary",
                            },
                        },
                    },
                ],
            },
        },
    },
        selector_kind="course_detail",
        item_title=detail["course_name"],
        course_name=detail["course_name"],
        course_id=detail["course_id"],
    )


def build_course_summary_flex(detail, alt_text, index):
    body_contents = [
        {"type": "text", "text": detail["course_name"], "weight": "bold", "wrap": True, "size": "lg"},
        {"type": "separator", "margin": "md"},
        {"type": "text", "text": f"▶️ 未完成作業：{detail['homework_count']}", "size": "sm", "wrap": True},
        {"type": "text", "text": f"▶️ 行事曆：{detail['calendar_count']}", "size": "sm", "wrap": True},
        {"type": "text", "text": f"▶️ 檔案：{detail['file_count']}", "size": "sm", "wrap": True},
    ]
    if detail.get("exam_lines"):
        body_contents.extend(
            [
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "🔴 考試提醒", "weight": "bold", "size": "sm", "margin": "md"},
                *[
                    {"type": "text", "text": line, "size": "sm", "wrap": True}
                    for line in detail["exam_lines"]
                ],
            ]
        )
    if detail["homework_lines"]:
        body_contents.extend(
            [
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "🟠 作業", "weight": "bold", "size": "sm", "margin": "md"},
                *[
                    {"type": "text", "text": line, "size": "sm", "wrap": True}
                    for line in detail["homework_lines"]
                ],
            ]
        )
    if detail["calendar_lines"]:
        body_contents.extend(
            [
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "🟢 行事曆", "weight": "bold", "size": "sm", "margin": "md"},
                *[
                    {"type": "text", "text": line, "size": "sm", "wrap": True}
                    for line in detail["calendar_lines"]
                ],
            ]
        )
    if detail["file_lines"]:
        body_contents.extend(
            [
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "📎 檔案", "weight": "bold", "size": "sm", "margin": "md"},
                *[
                    {"type": "text", "text": line, "size": "sm", "wrap": True}
                    for line in detail["file_lines"]
                ],
            ]
        )

    return attach_message_meta(
        {
        "type": "flex",
        "altText": alt_text,
        "contents": {
            "type": "bubble",
            "xe3_meta": {
                "selector_kind": "course_summary",
                "item_title": detail["course_name"],
                "course_name": detail["course_name"],
                "course_id": detail["course_id"],
            },
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#0F766E",
                "paddingAll": "12px",
                "contents": [
                    {"type": "text", "text": "課程摘要", "color": "#FFFFFF", "weight": "bold", "size": "md"},
                ],
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": body_contents,
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "style": "primary",
                        "height": "sm",
                        "color": "#0F766E",
                        "action": {
                            "type": "message",
                            "label": "課程詳情",
                            "text": f"e3 課程詳情 {index}",
                            "xe3_meta": {
                                "selector_kind": "course_detail",
                                "entry_kind": "course_detail",
                                "item_title": detail["course_name"],
                                "course_name": detail["course_name"],
                                "course_id": detail["course_id"],
                            },
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "height": "sm",
                        "action": {
                            "type": "message",
                            "label": "查看教材",
                            "text": f"e3 檔案資料夾 {detail['course_id'] or detail['course_name']}",
                            "xe3_meta": {
                                "selector_kind": "file_folder",
                                "entry_kind": "course_materials",
                                "item_title": detail["course_name"],
                                "course_name": detail["course_name"],
                                "course_id": detail["course_id"],
                            },
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "height": "sm",
                        "action": {
                            "type": "message",
                            "label": "查看作業",
                            "text": f"e3 課程作業 {detail['course_id'] or detail['course_name']}",
                            "xe3_meta": {
                                "selector_kind": "course_homework_detail",
                                "entry_kind": "course_homework",
                                "item_title": detail["course_name"],
                                "course_name": detail["course_name"],
                                "course_id": detail["course_id"],
                            },
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "height": "sm",
                        "action": {
                            "type": "message",
                            "label": "回到課程列表",
                            "text": "e3 course",
                            "xe3_meta": {
                                "selector_kind": "course_summary",
                            },
                        },
                    },
                ],
            },
        },
    },
        selector_kind="course_summary",
        item_title=detail["course_name"],
        course_name=detail["course_name"],
        course_id=detail["course_id"],
    )
