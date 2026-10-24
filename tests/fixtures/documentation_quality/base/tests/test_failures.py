import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

from logreports.reports.failures import summarize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rec(service, status):
    return {"service": service, "status": status, "duration_ms": 1,
            "timestamp": "2026-10-17T22:00:00+00:00"}


class FailuresTests(unittest.TestCase):
    def test_empty_input_returns_empty_object(self):
        self.assertEqual(summarize([]), {})

    def test_only_success_input_returns_empty_object(self):
        records = [rec("api", 200), rec("web", 204), rec("api", 301)]
        self.assertEqual(summarize(records), {})

    def test_boundary_399_not_counted_400_counted(self):
        self.assertEqual(summarize([rec("api", 399)]), {})
        self.assertEqual(summarize([rec("api", 400)]), {"api": 1})

    def test_mixed_counts_across_services(self):
        records = [rec("api", 500), rec("api", 404), rec("api", 200),
                   rec("web", 503), rec("db", 200), rec("db", 301)]
        self.assertEqual(summarize(records), {"api": 2, "web": 1})
        self.assertNotIn("db", summarize(records))

    def test_does_not_mutate_input_and_is_json_serialisable(self):
        records = [rec("api", 200), rec("api", 404)]
        before = copy.deepcopy(records)
        result = summarize(records)
        self.assertEqual(records, before)
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "in.jsonl")
            with open(path, "w") as f:
                for service, status in (("api", 200), ("api", 500),
                                        ("web", 404), ("web", 502),
                                        ("db", 201)):
                    f.write(json.dumps(rec(service, status)) + "\n")
            proc = subprocess.run(
                [sys.executable, "-B", "-m", "logreports", path,
                 "--report", "failures"],
                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"api": 1, "web": 2})


if __name__ == "__main__":
    unittest.main()
