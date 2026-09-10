from __future__ import annotations

import logging

from agent.features.e3 import handler
from agent.features.e3.data.file_proxy import attachment_content_disposition
from agent.platforms.discord.payload_sender import MAX_SELECT_OPTIONS, with_back_entry
from agent.platforms.line import app as line_app


def test_course_list_does_not_stop_after_ten(monkeypatch):
    semester = handler._current_semester_tag()
    courses = {
        f"{semester}{410000 + index}測試課程{index}": {"_course_id": str(20000 + index)}
        for index in range(1, 13)
    }
    monkeypatch.setattr(handler, "_require_line_user", lambda _user: (1, None))
    monkeypatch.setattr(handler, "fetch_courses", lambda _user: courses)
    monkeypatch.setattr(handler, "fetch_file_links", lambda _user: {"file_links": {}})
    monkeypatch.setattr(handler, "get_cache_status", lambda _user: {"exists": True, "is_fresh": True, "age_minutes": 0})

    payload = handler._list_courses(logging.getLogger("test"), "discord:1")
    carousel = payload["messages"][-1]["contents"]

    assert carousel["type"] == "carousel"
    assert len(carousel["contents"]) == 12


def test_full_select_keeps_all_real_options_instead_of_back_button():
    entries = [
        (
            f"課程 {index}",
            "課程摘要",
            {
                "kind": "message",
                "value": f"e3 課程摘要 {index}",
                "xe3_meta": {"selector_kind": "course_summary"},
            },
        )
        for index in range(1, MAX_SELECT_OPTIONS + 1)
    ]

    result = with_back_entry(entries)

    assert len(result) == MAX_SELECT_OPTIONS
    assert [entry[0] for entry in result] == [f"課程 {index}" for index in range(1, MAX_SELECT_OPTIONS + 1)]


def test_unicode_download_filename_uses_ascii_fallback_and_utf8_value():
    header = attachment_content_disposition("實驗報告 甲.pdf")

    assert 'filename="download.pdf"' in header
    assert "filename*=UTF-8''%E5%AF%A6%E9%A9%97%E5%A0%B1%E5%91%8A%20%E7%94%B2.pdf" in header
    header.encode("ascii")


def test_file_proxy_os_error_returns_502_instead_of_500(monkeypatch):
    monkeypatch.setattr(line_app, "prepare_proxy_download", lambda _token: (_ for _ in ()).throw(OSError("missing CA")))

    response = line_app.app.test_client().get("/e3/file/test-token")

    assert response.status_code == 502
    assert "E3 下載失敗" in response.get_data(as_text=True)
