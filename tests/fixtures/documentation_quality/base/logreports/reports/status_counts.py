"""Report: status_counts."""
CLASSES = ("2xx", "3xx", "4xx", "5xx")


def summarize(records):
    counts = {name: 0 for name in CLASSES}
    for record in records:
        name = "%dxx" % (record["status"] // 100)
        if name in counts:
            counts[name] += 1
    return counts
