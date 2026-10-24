import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

from logreports.core import load_records
from logreports.reports.daily import summarize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rec(timestamp):
    return {"service": "api", "status": 200, "duration_ms": 1,
            "timestamp": timestamp}


def write_jsonl(path, rows):
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


class DailyTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(summarize([]), {})

    def test_single_date(self):
        records = [rec("2026-10-17T01:00:00+00:00"),
                   rec("2026-10-17T22:00:00+00:00")]
        self.assertEqual(summarize(records), {"2026-10-17": 2})

    def test_multiple_dates_and_counts(self):
        records = [rec("2026-10-17T01:00:00+00:00"),
                   rec("2026-10-19T05:00:00+00:00"),
                   rec("2026-10-17T23:59:59+00:00"),
                   rec("2026-10-19T00:00:00+00:00"),
                   rec("2026-10-19T12:00:00+00:00")]
        self.assertEqual(summarize(records),
                         {"2026-10-17": 2, "2026-10-19": 3})

    def test_utc_midnight_boundary(self):
        records = [rec("2026-10-17T23:59:59.999999+00:00"),
                   rec("2026-10-18T00:00:00+00:00")]
        self.assertEqual(summarize(records),
                         {"2026-10-17": 1, "2026-10-18": 1})

    def test_loader_normalises_offset_to_utc_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "in.jsonl")
            write_jsonl(path, [rec("2026-10-18T01:30:00+03:00"),
                               rec("2026-10-17T23:30:00-02:00"),
                               rec("2026-10-17T22:00:00Z")])
            result = summarize(load_records(path))
        self.assertEqual(result, {"2026-10-17": 2, "2026-10-18": 1})

    def test_does_not_mutate_input_and_is_json_serialisable(self):
        records = [rec("2026-10-17T22:00:00+00:00"),
                   rec("2026-10-18T22:00:00+00:00")]
        before = copy.deepcopy(records)
        result = summarize(records)
        self.assertEqual(records, before)
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "in.jsonl")
            write_jsonl(path, [rec("2026-10-17T22:00:00Z"),
                               rec("2026-10-18T01:00:00+02:00"),
                               rec("2026-10-18T00:00:00Z")])
            proc = subprocess.run(
                [sys.executable, "-B", "-m", "logreports", path,
                 "--report", "daily"],
                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout),
                         {"2026-10-17": 2, "2026-10-18": 1})


if __name__ == "__main__":
    unittest.main()
