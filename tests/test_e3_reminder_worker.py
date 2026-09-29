import json
from datetime import datetime, timedelta, timezone

from agent.features.e3.reminder import worker
from agent.features.e3.reminder.payloads import format_countdown_payload
from agent.features.e3.services.events import extract_events_from_fetch_all


TAIPEI_TZ = timezone(timedelta(hours=8))


class _Logger:
    def error(self, *_args, **_kwargs):
        return None

    def exception(self, *_args, **_kwargs):
        return None


def _reminder_row(now, *, fresh=True):
    updated_at = now.astimezone(timezone.utc)
    if not fresh:
        updated_at -= timedelta(hours=2)
    return {
        "user_id": 1,
        "line_user_id": "discord:123",
        "login_status": "ok",
        "last_error": None,
        "account_updated_at": updated_at.isoformat(),
        "schedule_json": '["09:00", "21:00"]',
    }


def _event(now):
    return {
        "event_uid": "event-1",
        "event_type": "exam",
        "course_id": "course-1",
        "course_name": "測試課程",
        "title": "期中考",
        "due_at": (now + timedelta(hours=3)).astimezone(timezone.utc).isoformat(),
    }


def _patch_worker_basics(monkeypatch, now, row):
    monkeypatch.setattr(worker, "taipei_now", lambda: now)
    monkeypatch.setattr(worker, "list_reminder_targets", lambda: [row])
    monkeypatch.setattr(worker, "process_periodic_syncs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "process_due_upload_queue", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "notification_succeeded", lambda *_args, **_kwargs: False)


def test_schedule_slot_remains_due_during_catchup_window():
    now = datetime(2026, 9, 29, 9, 17, tzinfo=TAIPEI_TZ)
    due = worker._due_schedule_slot(now, ["09:00", "21:00"])
    assert due == (datetime(2026, 9, 29, 9, 0, tzinfo=TAIPEI_TZ), "09:00")
    assert worker._due_schedule_slot(now.replace(minute=31), ["09:00", "21:00"]) is None


def test_due_digest_refreshes_before_sending_empty_cache(monkeypatch):
    now = datetime(2026, 9, 29, 9, 5, tzinfo=TAIPEI_TZ)
    row = _reminder_row(now, fresh=False)
    current_events = []
    synced = []
    pushed = []
    logs = []
    _patch_worker_basics(monkeypatch, now, row)

    def get_events(_user_id, start_iso, end_iso, limit=10):
        window = datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)
        return list(current_events) if window > timedelta(hours=1) else []

    def sync(*_args, **_kwargs):
        synced.append(True)
        current_events.append(_event(now))
        return [], True

    monkeypatch.setattr(worker, "get_events_due_between", get_events)
    monkeypatch.setattr(worker, "sync_user_snapshot", sync)
    monkeypatch.setattr(worker, "log_notification", lambda *args, **kwargs: logs.append((args, kwargs)))

    worker.process_due_reminders(lambda _user, payload: pushed.append(payload) or True, _Logger())

    assert synced == [True]
    assert len(pushed) == 1
    assert "期中考" in pushed[0]
    assert "沒有新的截止事件" not in pushed[0]
    assert any(
        args[1] == "scheduled_digest" and kwargs.get("details") == "2026-09-29 09:00"
        for args, kwargs in logs
    )


def test_failed_digest_retries_with_same_real_events(monkeypatch):
    now_values = [
        datetime(2026, 9, 29, 9, 0, tzinfo=TAIPEI_TZ),
        datetime(2026, 9, 29, 9, 1, tzinfo=TAIPEI_TZ),
    ]
    row = _reminder_row(now_values[0])
    event = _event(now_values[0])
    sent = set()
    pushed = []
    _patch_worker_basics(monkeypatch, now_values[0], row)
    monkeypatch.setattr(worker, "taipei_now", lambda: now_values.pop(0))

    def get_events(_user_id, start_iso, end_iso, limit=10):
        window = datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)
        return [event] if window > timedelta(hours=1) else []

    def succeeded(_user_id, notification_type, details=None):
        return (notification_type, details) in sent

    def log(_user_id, notification_type, result, details=None, **_kwargs):
        if result == "sent":
            sent.add((notification_type, details))

    outcomes = iter([False, True])
    monkeypatch.setattr(worker, "get_events_due_between", get_events)
    monkeypatch.setattr(worker, "notification_succeeded", succeeded)
    monkeypatch.setattr(worker, "log_notification", log)

    def push(_user, payload):
        pushed.append(payload)
        return next(outcomes)

    worker.process_due_reminders(push, _Logger())
    worker.process_due_reminders(push, _Logger())

    assert len(pushed) == 2
    assert all("期中考" in payload for payload in pushed)
    assert all("沒有新的截止事件" not in payload for payload in pushed)


def test_timezone_less_e3_deadline_is_stored_as_utc():
    courses = {
        "測試課程": {
            "_course_id": "course-1",
            "assignments": {
                "assignments": [
                    {
                        "title": "Homework 1",
                        "category": "upcoming",
                        "due": "2026-10-01 23:59",
                    }
                ]
            },
        }
    }

    events = extract_events_from_fetch_all(courses)

    assert len(events) == 1
    assert events[0]["due_at"] == "2026-10-01T15:59:00+00:00"


def test_list_shaped_assignments_keep_attachments_and_replace_calendar_duplicate():
    assignment = {
        "title": "Homework 1",
        "category": "in_progress",
        "due_time": "2026/10/06 23:59",
        "attachments": [
            {
                "name": "question.pdf",
                "url": "https://e3p.nycu.edu.tw/pluginfile.php/1/question.pdf",
            }
        ],
    }
    courses = {"測試課程": {"_course_id": "course-1", "assignments": [assignment]}}
    calendar = [
        {
            "event_id": "calendar-1",
            "course_id": "course-1",
            "course_name": "測試課程",
            "title": "Homework 1",
            "due_at": "2026-10-06T15:59:00+00:00",
        }
    ]

    events = extract_events_from_fetch_all(courses, calendar_events=calendar)

    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload["attachments"][0]["name"] == "question.pdf"


def test_unfinished_homework_reminder_includes_only_teacher_attachments():
    now = datetime(2026, 9, 29, 9, 0, tzinfo=TAIPEI_TZ)
    row = {
        "event_uid": "homework-1",
        "event_type": "homework",
        "course_id": "course-1",
        "course_name": "測試課程",
        "title": "Homework 1",
        "due_at": (now + timedelta(hours=12)).astimezone(timezone.utc).isoformat(),
        "payload_json": '{"attachments":[{"name":"question.pdf","url":"https://e3p.nycu.edu.tw/pluginfile.php/1/question.pdf"}],"submitted_files":[{"name":"answer.pdf","url":"https://e3p.nycu.edu.tw/pluginfile.php/1/answer.pdf"}]}',
    }

    payload = format_countdown_payload(row, 12, "discord:123")

    assert isinstance(payload, dict)
    assert "question.pdf" in payload["text"]
    assert "answer.pdf" not in payload["text"]
    footer = payload["messages"][0]["contents"]["footer"]
    assert len(footer["contents"]) == 1
    action = footer["contents"][0]["action"]
    assert action["uri"].endswith("question.pdf")
    assert action["xe3_meta"]["direct_download"] is True
