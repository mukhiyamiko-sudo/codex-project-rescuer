import json
import tempfile
import unittest
from pathlib import Path

import main


class SelfTestTests(unittest.TestCase):
    def test_self_test_writes_success_report(self):
        with tempfile.TemporaryDirectory() as temp:
            report_path = Path(temp) / "self-test.json"

            exit_code = main.run_self_test(report_path)
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["restored_count"], 1)
        self.assertTrue(report["current_project_preserved"])
        self.assertEqual(report["backup_count"], 1)


if __name__ == "__main__":
    unittest.main()
