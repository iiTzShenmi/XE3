import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent.features.e3.scraper.update_all import _is_due, _run_fetch
from agent.features.e3.reminder.worker import _recently_synced
from agent.features.e3.scraper.get_course.get_user_data import get_user_data


class IncrementalSyncTest(unittest.TestCase):
    def test_fresh_file_is_skipped_and_stale_file_is_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "news.json"
            path.write_text("[]", encoding="utf-8")
            self.assertFalse(_is_due(path, 60))
            old = time.time() - 61 * 60
            os.utime(path, (old, old))
            self.assertTrue(_is_due(path, 60))
            self.assertTrue(_is_due(path, 60, force=True))

    def test_fetcher_false_result_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            result = _run_fetch("course:grades", missing, 60, lambda: False)
        self.assertEqual(result["status"], "failed")

    def test_periodic_sync_uses_account_age_instead_of_clock_minute(self):
        now = datetime.now(timezone.utc)
        fresh = {"account_updated_at": (now - timedelta(minutes=59)).isoformat()}
        stale = {"account_updated_at": (now - timedelta(minutes=61)).isoformat()}
        self.assertTrue(_recently_synced(fresh, now, minutes=60))
        self.assertFalse(_recently_synced(stale, now, minutes=60))

    def test_dashboard_failure_is_distinct_from_zero_courses(self):
        with patch(
            "agent.features.e3.scraper.get_course.get_user_data.ensure_authenticated_session",
            return_value=(None, None),
        ):
            self.assertIsNone(get_user_data("account", "password"))


if __name__ == "__main__":
    unittest.main()
