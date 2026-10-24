import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

from logreports.reports.latency import summarize

KEYS = {"count", "min_ms", "max_ms", "mean_ms", "p95_ms"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rec(ms):
    return {"service": "api", "status": 200, "duration_ms": ms,
            "timestamp": "2026-10-17T22:00:00+00:00"}


def recs(values):
    return [rec(v) for v in values]


class LatencyTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(summarize([]), {"count": 0, "min_ms": None,
                                         "max_ms": None, "mean_ms": None,
                                         "p95_ms": None})

    def test_key_set_always_exact(self):
        self.assertEqual(set(summarize([])), KEYS)
        self.assertEqual(set(summarize(recs([5]))), KEYS)

    def test_single_record(self):
        self.assertEqual(summarize(recs([7])),
                         {"count": 1, "min_ms": 7, "max_ms": 7,
                          "mean_ms": 7, "p95_ms": 7})

    def test_nearest_rank(self):
        # (n, expected 1-based rank) with values 1..n so p95 equals the rank
        for n, rank in ((1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (10, 10),
                        (20, 19), (21, 20), (100, 95), (101, 96)):
            result = summarize(recs(range(1, n + 1)))
            self.assertEqual(result["p95_ms"], rank, n)

    def test_unsorted_input(self):
        values = [50, 3, 99, 1, 20, 7, 100, 42, 8, 15,
                  60, 2, 33, 70, 5, 90, 11, 80, 25, 4]
        result = summarize(recs(values))
        self.assertEqual(result["count"], 20)
        self.assertEqual(result["min_ms"], 1)
        self.assertEqual(result["max_ms"], 100)
        self.assertEqual(result["p95_ms"], 99)

    def test_mean(self):
        self.assertEqual(summarize(recs([1, 2, 3, 6]))["mean_ms"], 3)
        self.assertAlmostEqual(summarize(recs([0.5, 1, 2.25]))["mean_ms"],
                               3.75 / 3)

    def test_does_not_mutate_input_and_is_json_serialisable(self):
        records = recs([30, 10, 20])
        before = copy.deepcopy(records)
        result = summarize(records)
        self.assertEqual(records, before)
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "in.jsonl")
            with open(path, "w") as f:
                for v in (40, 10, 30, 20):
                    f.write(json.dumps(rec(v)) + "\n")
            proc = subprocess.run(
                [sys.executable, "-B", "-m", "logreports", path,
                 "--report", "latency"],
                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout),
                         {"count": 4, "min_ms": 10, "max_ms": 40,
                          "mean_ms": 25, "p95_ms": 40})


if __name__ == "__main__":
    unittest.main()
