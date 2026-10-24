import argparse
import importlib
import json
from .core import load_records

parser = argparse.ArgumentParser(description='Local JSON-lines HTTP log reports')
parser.add_argument('input')
parser.add_argument('--report', choices=['overview', 'status_counts', 'latency', 'failures', 'daily'], default='overview')
args = parser.parse_args()
records = load_records(args.input)
if args.report == 'overview':
    result = {'records': len(records), 'services': sorted({row['service'] for row in records})}
else:
    result = importlib.import_module('logreports.reports.' + args.report).summarize(records)
print(json.dumps(result, sort_keys=True))
