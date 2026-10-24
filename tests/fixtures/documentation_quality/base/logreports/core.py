"""Load JSON-lines HTTP observations for local analysis."""
import json
import math
from datetime import datetime, timezone

def load_records(path):
    records = []
    with open(path, encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row.get('service'), str) or not row['service']:
                raise ValueError(f'line {number}: service is required')
            if type(row.get('status')) is not int or not 200 <= row['status'] <= 599:
                raise ValueError(f'line {number}: invalid HTTP status')
            duration = row.get('duration_ms')
            if type(duration) not in (int, float) or duration < 0 or not math.isfinite(duration):
                raise ValueError(f'line {number}: invalid duration')
            stamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError(f'line {number}: timestamp requires timezone')
            records.append({**row, 'timestamp': stamp.astimezone(timezone.utc).isoformat()})
    return records
