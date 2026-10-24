import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

from logreports.reports.status_counts import summarize

KEYS = {"2xx", "3xx", "4xx", "5xx"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rec(status):
    return {"service": "api", "status": status, "duration_ms": 1,
            "timestamp": "2026-10-17T22:00:00+00:00"}


class StatusCountsTests(unittest.TestCase):
    def test_empty_input_all_zeros(self):
        self.assertEqual(summarize([]), {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0})

    def test_zero_counts_for_unseen_classes(self):
        result = summarize([rec(200), rec(201)])
        self.assertEqual(result, {"2xx": 2, "3xx": 0, "4xx": 0, "5xx": 0})

    def test_mixed_counts(self):
        records = [rec(s) for s in (200, 204, 301, 404, 404, 418, 500, 503, 599)]
        self.assertEqual(summarize(records),
                         {"2xx": 2, "3xx": 1, "4xx": 3, "5xx": 3})

    def test_class_boundaries(self):
        for status, name in ((200, "2xx"), (299, "2xx"), (300, "3xx"),
                             (399, "3xx"), (400, "4xx"), (499, "4xx"),
                             (500, "5xx"), (599, "5xx")):
            result = summarize([rec(status)])
            self.assertEqual(result[name], 1, status)
            self.assertEqual(sum(result.values()), 1, status)

    def test_key_set_always_exact(self):
        self.assertEqual(set(summarize([])), KEYS)
        self.assertEqual(set(summarize([rec(500)])), KEYS)

    def test_does_not_mutate_input_and_is_json_serialisable(self):
        records = [rec(200), rec(404)]
        before = copy.deepcopy(records)
        result = summarize(records)
        self.assertEqual(records, before)
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "in.jsonl")
            with open(path, "w") as f:
                for s in (200, 404, 500, 502):
                    f.write(json.dumps(rec(s)) + "\n")
            proc = subprocess.run(
                [sys.executable, "-B", "-m", "logreports", path,
                 "--report", "status_counts"],
                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout),
                         {"2xx": 1, "3xx": 0, "4xx": 1, "5xx": 2})


if __name__ == "__main__":
    unittest.main()
