"""Report: failures."""


def summarize(records):
    counts = {}
    for record in records:
        if record["status"] >= 400:
            service = record["service"]
            counts[service] = counts.get(service, 0) + 1
    return counts
