from __future__ import annotations

import requests
import pytest

from agent.features.e3.services import upload


class FakeResponse:
    def __init__(self, payload=None, *, text="", url="https://e3p.nycu.edu.tw/repository/repository_ajax.php"):
        self._payload = payload
        self.text = text
        self.url = url
        self.status_code = 200

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def post(self, _url, **kwargs):
        self.kwargs = kwargs
        return self.response


def _target(*, submitted_count=0):
    return upload.AssignmentTarget(
        course_id="27444",
        course_name="測試課程",
        title="Lab01",
        cmid="230655",
        detail_url="https://e3p.nycu.edu.tw/mod/assign/view.php?id=230655",
        start_at="2026/09/07 00:00",
        due_at="2026/09/21 23:59",
        category="in_progress",
        completed=False,
        submitted_count=submitted_count,
    )


def _context():
    return {
        "files_filemanager": "123",
        "repo_id": "5",
        "sesskey": "secret",
        "client_id": "client",
        "maxbytes": "0",
        "areamaxbytes": "-1",
        "ctx_id": "456",
    }


def test_sanitize_upload_filename_removes_path_controls_and_preserves_suffix():
    name = upload.sanitize_upload_filename("../bad\\name\x00\n" + "測" * 100 + ".xlsx")

    assert "/" not in name
    assert "\\" not in name
    assert "\x00" not in name
    assert "\n" not in name
    assert name.endswith(".xlsx")
    assert len(name.encode("utf-8")) <= upload.MAX_UPLOAD_FILENAME_BYTES


def test_upload_response_allows_filename_containing_error_and_sets_timeout():
    response = FakeResponse({"file": "error_report.pdf", "url": "https://example.invalid/draft"})
    session = FakeSession(response)

    filename = upload._upload_to_draft(session, "https://example.invalid/edit", _context(), "error_report.pdf", b"ok", "application/pdf")

    assert filename == "error_report.pdf"
    assert session.kwargs["timeout"] == upload.E3_REQUEST_TIMEOUT


def test_upload_response_rejects_structured_error():
    session = FakeSession(FakeResponse({"error": "檔案格式不允許"}))

    with pytest.raises(upload.E3UploadError) as caught:
        upload._upload_to_draft(session, "https://example.invalid/edit", _context(), "test.exe", b"bad", "application/octet-stream")

    assert caught.value.status == "upload_rejected"


def test_request_maps_timeout_to_user_facing_error():
    class TimeoutSession:
        def get(self, _url, **_kwargs):
            raise requests.Timeout("slow")

    with pytest.raises(upload.E3UploadError) as caught:
        upload._request(TimeoutSession(), "get", "https://example.invalid", stage="讀取提交表單")

    assert caught.value.status == "request_timeout"


def test_replace_existing_never_deletes_submission(monkeypatch):
    monkeypatch.setattr(upload, "resolve_assignment_target", lambda *_args, **_kwargs: _target(submitted_count=1))
    monkeypatch.setattr(upload, "_authenticated_session", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(upload, "_fetch_assignment_view", lambda *_args, **_kwargs: '<a href="assignsubmission_file/a.pdf">a.pdf</a>')
    monkeypatch.setattr(
        upload,
        "_remove_existing_submission",
        lambda *_args, **_kwargs: pytest.fail("existing submission must not be deleted"),
    )

    with pytest.raises(upload.E3UploadError) as caught:
        upload.upload_assignment_submission("discord:1", "27444", "27444:230655", "new.pdf", b"new", replace_existing=True)

    assert caught.value.status == "replace_unsupported"


def test_saved_draft_is_not_reported_as_submitted(monkeypatch):
    pages = iter(
        [
            "<html><body>尚未提交</body></html>",
            '<html><body>draft.pdf<form><input name="action" value="submitforgrading"></form></body></html>',
        ]
    )
    monkeypatch.setattr(upload, "resolve_assignment_target", lambda *_args, **_kwargs: _target())
    monkeypatch.setattr(upload, "_authenticated_session", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(upload, "_fetch_assignment_view", lambda *_args, **_kwargs: next(pages))
    monkeypatch.setattr(upload, "_fetch_edit_context", lambda *_args, **_kwargs: ("https://example.invalid/edit", _context()))
    monkeypatch.setattr(upload, "_upload_to_draft", lambda *_args, **_kwargs: "draft.pdf")
    monkeypatch.setattr(upload, "_save_assignment_submission", lambda *_args, **_kwargs: "")

    with pytest.raises(upload.E3UploadError) as caught:
        upload.upload_assignment_submission("discord:1", "27444", "27444:230655", "draft.pdf", b"draft")

    assert caught.value.status == "final_submit_required"


def test_preflight_never_uploads_or_saves(monkeypatch):
    monkeypatch.setattr(upload, "resolve_assignment_target", lambda *_args, **_kwargs: _target())
    monkeypatch.setattr(upload, "_authenticated_session", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(upload, "_fetch_assignment_view", lambda *_args, **_kwargs: "<html><body>尚未提交</body></html>")
    monkeypatch.setattr(upload, "_fetch_edit_context", lambda *_args, **_kwargs: ("https://example.invalid/edit", _context()))
    monkeypatch.setattr(upload, "_upload_to_draft", lambda *_args, **_kwargs: pytest.fail("preflight must not upload"))
    monkeypatch.setattr(upload, "_save_assignment_submission", lambda *_args, **_kwargs: pytest.fail("preflight must not save"))

    result = upload.preflight_assignment_upload(
        "discord:1",
        "27444",
        "27444:230655",
        filename="../lab.xlsx",
        file_size=123,
    )

    assert result.filename == "_lab.xlsx"
    assert result.submitted is False
