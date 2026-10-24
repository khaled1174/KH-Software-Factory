"""Report: latency summary (count, min, max, mean, nearest-rank p95)."""
import math


def summarize(records):
    values = sorted(r["duration_ms"] for r in records)
    n = len(values)
    if n == 0:
        return {"count": 0, "min_ms": None, "max_ms": None,
                "mean_ms": None, "p95_ms": None}
    rank = (95 * n + 99) // 100  # integer ceil(0.95 * n)
    return {
        "count": n,
        "min_ms": values[0],
        "max_ms": values[-1],
        "mean_ms": math.fsum(values) / n,
        "p95_ms": values[rank - 1],
    }
