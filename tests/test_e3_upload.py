from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import requests
import pytest

from agent.features.e3.services import upload


class FakeResponse:
    def __init__(
        self,
        payload=None,
        *,
        text="",
        url="https://e3p.nycu.edu.tw/repository/repository_ajax.php",
        status_code=200,
        headers=None,
    ):
        self._payload = payload
        self.text = text
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}

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


def _edit_form_html():
    return """
    <html><body>
      <form method="post" action="https://e3p.nycu.edu.tw/mod/assign/view.php">
        <input type="hidden" name="lastmodified" value="1777255350">
        <input type="hidden" name="id" value="230655">
        <input type="hidden" name="userid" value="48616">
        <input type="hidden" name="action" value="savesubmission">
        <input type="hidden" name="sesskey" value="secret">
        <input type="hidden" name="_qf__mod_assign_submission_form" value="1">
        <input type="hidden" name="mform_isexpanded_id_submissionheader" value="1">
        <input type="hidden" name="files_filemanager" value="123">
        <input type="submit" name="submitbutton" value="儲存更改">
        <input type="submit" name="cancel" value="取消">
      </form>
      <script>
        M.cfg = {"courseId":"27444","contextInstanceId":"230655","sesskey":"secret","userId":"48616","contextid":"456"};
        var options = {"client_id":"client","ctx_id":456,"maxbytes":1073741824,"areamaxbytes":-1,
          "repositories":[{"type":"upload","id":5}]};
      </script>
    </body></html>
    """


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


def test_edit_form_parser_uses_filemanager_parent_and_excludes_cancel():
    context = upload._parse_edit_context(_edit_form_html(), "27444", "230655")

    assert context["files_filemanager"] == "123"
    assert context["lastmodified"] == "1777255350"
    assert context["client_id"] == "client"
    assert context["ctx_id"] == "456"
    assert context["repo_id"] == "5"
    assert "cancel" not in context
    assert "submitbutton" not in context


def test_edit_form_parser_rejects_missing_submission_form():
    with pytest.raises(upload.E3UploadError) as caught:
        upload._parse_edit_context("<html><body>not an edit page</body></html>", "27444", "230655")

    assert caught.value.status == "missing_submission_form"


def test_assignment_response_rejects_enrol_redirect():
    response = FakeResponse(url="https://e3p.nycu.edu.tw/enrol/index.php?id=27444")

    with pytest.raises(upload.E3UploadError) as caught:
        upload._validate_assignment_response(response, _target(), stage="讀取提交表單")

    assert caught.value.status == "enrol_redirect"


def test_draft_list_returns_uploaded_filenames_and_sets_expected_fields():
    session = FakeSession(FakeResponse({"itemid": 123, "list": [{"filename": "report%20file.pdf"}], "filecount": 1}))

    filenames = upload._list_draft_filenames(session, "https://example.invalid/edit", _context())

    assert filenames == ("report file.pdf",)
    assert session.kwargs["data"] == {
        "sesskey": "secret",
        "client_id": "client",
        "filepath": "/",
        "itemid": "123",
    }


def test_save_submission_excludes_cancel_and_follows_expected_redirect():
    class SaveSession:
        def __init__(self):
            self.saved_data = None

        def post(self, _url, **kwargs):
            self.saved_data = kwargs["data"]
            assert kwargs["allow_redirects"] is False
            return FakeResponse(
                status_code=303,
                url=upload.E3_ASSIGN_VIEW_URL,
                headers={"Location": "/mod/assign/view.php?id=230655&action=view"},
            )

        def get(self, url, **_kwargs):
            return FakeResponse(
                text="<html><body>已提交 report.pdf</body></html>",
                url=url,
            )

    session = SaveSession()
    context = {**_context(), "id": "230655", "userid": "48616", "lastmodified": "1", "cancel": "取消"}

    html = upload._save_assignment_submission(
        session,
        "https://e3p.nycu.edu.tw/mod/assign/view.php?id=230655&action=editsubmission",
        context,
        _target(),
        operation_id="test-operation",
    )

    assert "已提交" in html
    assert session.saved_data["action"] == "savesubmission"
    assert session.saved_data["submitbutton"] == "儲存更改"
    assert "cancel" not in session.saved_data
    assert session.saved_data["files_filemanager"] == "123"


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
    monkeypatch.setattr(upload, "resolve_assignment_target", lambda *_args, **_kwargs: _target())
    monkeypatch.setattr(upload, "_authenticated_session", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(upload, "_fetch_assignment_view", lambda *_args, **_kwargs: "<html><body>尚未提交</body></html>")
    monkeypatch.setattr(upload, "_fetch_edit_context", lambda *_args, **_kwargs: ("https://example.invalid/edit", _context()))
    draft_lists = iter([(), ("draft.pdf",)])
    monkeypatch.setattr(upload, "_list_draft_filenames", lambda *_args, **_kwargs: next(draft_lists))
    monkeypatch.setattr(upload, "_upload_to_draft", lambda *_args, **_kwargs: "draft.pdf")
    monkeypatch.setattr(
        upload,
        "_save_assignment_submission",
        lambda *_args, **_kwargs: '<html><body>draft.pdf<form><input name="action" value="submitforgrading"></form></body></html>',
    )

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


def test_multiple_files_share_one_draft_and_save_once(monkeypatch):
    final_html = """
        <html><body>
          <table><tr><td>繳交狀態</td><td>已提交</td></tr></table>
          <a href="assignsubmission_file/report.pdf">report.pdf</a>
          <a href="assignsubmission_file/data.csv">data.csv</a>
        </body></html>
    """
    uploaded: list[tuple[str, str]] = []
    saved: list[str] = []
    monkeypatch.setattr(upload, "resolve_assignment_target", lambda *_args, **_kwargs: _target())
    monkeypatch.setattr(upload, "_authenticated_session", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(upload, "_fetch_assignment_view", lambda *_args, **_kwargs: "<html><body>尚未提交</body></html>")
    monkeypatch.setattr(upload, "_fetch_edit_context", lambda *_args, **_kwargs: ("https://example.invalid/edit", _context()))
    draft_lists = iter([(), ("report.pdf", "data.csv")])
    monkeypatch.setattr(upload, "_list_draft_filenames", lambda *_args, **_kwargs: next(draft_lists))

    def fake_upload(_session, _edit_url, context, filename, _content, _content_type):
        uploaded.append((context["files_filemanager"], filename))
        return filename

    monkeypatch.setattr(upload, "_upload_to_draft", fake_upload)
    monkeypatch.setattr(upload, "_save_assignment_submission", lambda *_args, **_kwargs: saved.append("saved") or final_html)

    result = upload.upload_assignment_files(
        "discord:1",
        "27444",
        "27444:230655",
        [
            upload.AssignmentUploadFile("report.pdf", b"pdf", "application/pdf"),
            upload.AssignmentUploadFile("data.csv", b"csv", "text/csv"),
        ],
    )

    assert uploaded == [("123", "report.pdf"), ("123", "data.csv")]
    assert saved == ["saved"]
    assert result.filenames == ("report.pdf", "data.csv")
    assert result.submitted_file_count == 2


def test_multiple_files_do_not_save_after_partial_draft_failure(monkeypatch):
    monkeypatch.setattr(upload, "resolve_assignment_target", lambda *_args, **_kwargs: _target())
    monkeypatch.setattr(upload, "_authenticated_session", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(upload, "_fetch_assignment_view", lambda *_args, **_kwargs: "<html><body>尚未提交</body></html>")
    monkeypatch.setattr(upload, "_fetch_edit_context", lambda *_args, **_kwargs: ("https://example.invalid/edit", _context()))
    monkeypatch.setattr(upload, "_list_draft_filenames", lambda *_args, **_kwargs: ())
    calls = 0

    def fake_upload(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise upload.E3UploadError("second failed", status="upload_rejected")
        return "first.pdf"

    monkeypatch.setattr(upload, "_upload_to_draft", fake_upload)
    monkeypatch.setattr(upload, "_save_assignment_submission", lambda *_args, **_kwargs: pytest.fail("partial batch must not be saved"))

    with pytest.raises(upload.E3UploadError) as caught:
        upload.upload_assignment_files(
            "discord:1",
            "27444",
            "27444:230655",
            [
                upload.AssignmentUploadFile("first.pdf", b"first"),
                upload.AssignmentUploadFile("second.pdf", b"second"),
            ],
        )

    assert caught.value.status == "upload_rejected"


def test_multiple_files_reject_duplicate_sanitized_names():
    with pytest.raises(upload.E3UploadError) as caught:
        upload._normalize_upload_files(
            [
                upload.AssignmentUploadFile("same.pdf", b"first"),
                upload.AssignmentUploadFile("SAME.pdf", b"second"),
            ]
        )

    assert caught.value.status == "duplicate_filename"


def test_queue_stores_multi_file_manifest(monkeypatch, tmp_path):
    target = replace(_target(), due_at="2026/12/31 23:59")
    captured: dict = {}
    monkeypatch.setattr(upload, "resolve_assignment_target", lambda *_args, **_kwargs: target)
    monkeypatch.setattr(upload, "_target_overdue", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(upload, "get_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(upload, "make_user_key", lambda value: value.replace(":", "_"))

    def fake_create(**kwargs):
        captured.update(kwargs)
        return 17

    monkeypatch.setattr(upload, "create_e3_upload_queue_entry", fake_create)

    result = upload.queue_assignment_upload_files(
        "discord:1",
        "27444",
        "27444:230655",
        [
            upload.AssignmentUploadFile("report.pdf", b"pdf", "application/pdf"),
            upload.AssignmentUploadFile("data.csv", b"csv", "text/csv"),
        ],
    )

    manifest = json.loads(captured["files_json"])
    assert result.queue_id == 17
    assert result.filenames == ("report.pdf", "data.csv")
    assert [item["filename"] for item in manifest] == ["report.pdf", "data.csv"]
    assert [Path(item["file_path"]).read_bytes() for item in manifest] == [b"pdf", b"csv"]


def test_queue_worker_submits_bundle_once_and_removes_all_files(monkeypatch, tmp_path):
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    first = queue_dir / "report.pdf"
    second = queue_dir / "data.csv"
    first.write_bytes(b"pdf")
    second.write_bytes(b"csv")
    manifest = [
        {"filename": first.name, "content_type": "application/pdf", "file_path": str(first)},
        {"filename": second.name, "content_type": "text/csv", "file_path": str(second)},
    ]
    row = {
        "id": 17,
        "line_user_id": "discord:1",
        "course_id": "27444",
        "course_name": "測試課程",
        "cmid": "230655",
        "assignment_title": "Lab01",
        "filename": first.name,
        "file_path": str(first),
        "files_json": json.dumps(manifest),
        "attempts": 0,
        "replace_existing": 0,
    }
    received: list[upload.AssignmentUploadFile] = []
    sent: list[int] = []
    pushed: list[tuple[str, str]] = []
    monkeypatch.setattr(upload, "list_due_e3_uploads", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(upload, "mark_e3_upload_attempt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(upload, "mark_e3_upload_sent", lambda queue_id: sent.append(queue_id))

    def fake_upload(_user, _course, _assignment, files, **_kwargs):
        received.extend(files)
        return upload.UploadResult("27444", "測試課程", "Lab01", "230655", ("report.pdf", "data.csv"), 2, False)

    monkeypatch.setattr(upload, "upload_assignment_files", fake_upload)

    upload.process_due_upload_queue(lambda user, text: pushed.append((user, text)), logger=upload.LOGGER)

    assert [(item.filename, item.content) for item in received] == [("report.pdf", b"pdf"), ("data.csv", b"csv")]
    assert sent == [17]
    assert not first.exists()
    assert not second.exists()
    assert pushed and "檔案（2）" in pushed[0][1]
