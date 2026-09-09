import tempfile
import unittest
from pathlib import Path

from agent.features.e3.services.monitoring import read_json_object, write_json_atomic


class MonitoringStorageTest(unittest.TestCase):
    def test_atomic_status_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            write_json_atomic(path, {"state": "success", "count": 7})
            self.assertEqual(read_json_object(path), {"state": "success", "count": 7})
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
