import unittest
from datetime import datetime, timedelta, timezone

from agent.features.e3.utils.common import current_semester_tag, extract_semester_tag, strip_semester_prefix


class SemesterHelpersTest(unittest.TestCase):
    def test_current_semester_uses_taipei_academic_year(self):
        taipei = timezone(timedelta(hours=8))
        self.assertEqual(current_semester_tag(datetime(2026, 9, 7, tzinfo=taipei)), "115上")
        self.assertEqual(current_semester_tag(datetime(2027, 1, 15, tzinfo=taipei)), "115上")
        self.assertEqual(current_semester_tag(datetime(2027, 2, 1, tzinfo=taipei)), "115下")

    def test_semester_tag_accepts_raw_and_sanitized_course_names(self):
        self.assertEqual(extract_semester_tag("【115上】410012物理化學(一)"), "115上")
        self.assertEqual(extract_semester_tag("115上410012物理化學(一)"), "115上")
        self.assertEqual(strip_semester_prefix("【115上】410012物理化學(一)"), "410012物理化學(一)")


if __name__ == "__main__":
    unittest.main()
