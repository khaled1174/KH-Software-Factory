"""Report: daily."""


def summarize(records):
    counts = {}
    for record in records:
        day = record["timestamp"][:10]
        counts[day] = counts.get(day, 0) + 1
    return counts
