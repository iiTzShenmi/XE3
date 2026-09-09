import unittest
from pathlib import Path
from unittest.mock import patch

from agent.features.e3.services.client import _read_home_page_preview


class DashboardParserRegressionTest(unittest.TestCase):
    def test_only_current_semester_courses_are_counted(self):
        fixture = Path(__file__).parent / "fixtures" / "e3_dashboard_courses.html"
        with patch("agent.features.e3.services.client.current_semester_tag", return_value="115上"):
            preview = _read_home_page_preview(fixture)

        self.assertEqual(preview["course_count"], 2)
        self.assertEqual(preview["all_course_count"], 3)
        self.assertIn("Physical Chemistry", preview["sample_courses"][0])


if __name__ == "__main__":
    unittest.main()
