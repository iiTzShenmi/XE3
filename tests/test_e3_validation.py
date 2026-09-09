import json
import tempfile
import unittest
from pathlib import Path

from agent.features.e3.services.validation import validate_workspace
from agent.features.e3.utils.common import current_semester_tag
from agent.features.e3.services.client import _read_all_courses_data


class WorkspaceValidationTest(unittest.TestCase):
    def _workspace(self, root: Path) -> tuple[Path, Path]:
        semester = current_semester_tag()
        workspace = root / "discord_test"
        workspace.mkdir()
        (workspace / "courses_current.json").write_text(
            json.dumps({"1001": f"{semester}410012物理化學(一)"}, ensure_ascii=False),
            encoding="utf-8",
        )
        course = workspace / f"1001_{semester}410012物理化學(一)"
        course.mkdir()
        return workspace, course

    def test_valid_workspace_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, course = self._workspace(Path(tmp))
            (course / "grades.json").write_text('{"grade_items": []}', encoding="utf-8")
            report = validate_workspace(workspace)
        self.assertTrue(report["valid"])
        self.assertEqual(report["issue_count"], 0)

    def test_invalid_section_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, course = self._workspace(Path(tmp))
            grades = course / "grades.json"
            grades.write_text('{"grade_items": {}}', encoding="utf-8")
            report = validate_workspace(workspace, quarantine_invalid=True)
            self.assertFalse(grades.exists())
            self.assertTrue(any((workspace / "quarantine").rglob("grades.json")))
        self.assertEqual(report["issue_count"], 1)
        self.assertEqual(report["issues"][0]["code"], "wrong_child_type")

    def test_stale_same_semester_folder_is_not_reintroduced(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _course = self._workspace(Path(tmp))
            semester = current_semester_tag()
            stale = workspace / f"9999_{semester}999999已退選課程"
            stale.mkdir()
            (stale / "news.json").write_text("[]", encoding="utf-8")
            courses = _read_all_courses_data(workspace, workspace / "courses_current.json")
        self.assertEqual(len(courses), 1)
        self.assertFalse(any("999999" in name for name in courses))

    def test_missing_course_index_is_fatal_validation_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "discord_test"
            workspace.mkdir()
            report = validate_workspace(workspace)
        self.assertFalse(report["valid"])
        self.assertEqual(report["issues"][0]["code"], "missing_course_index")


if __name__ == "__main__":
    unittest.main()
