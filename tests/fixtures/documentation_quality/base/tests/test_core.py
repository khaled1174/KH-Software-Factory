import json
import tempfile
from pathlib import Path
import unittest
from logreports.core import load_records

class CoreTests(unittest.TestCase):
    def load(self, row):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'input.jsonl'
            path.write_text(json.dumps(row)+'\n')
            return load_records(path)
    def row(self):
        return {'service':'api','status':200,'duration_ms':12,'timestamp':'2026-10-18T01:00:00+03:00'}
    def test_normalizes_timestamp(self):
        self.assertEqual(self.load(self.row())[0]['timestamp'],'2026-10-17T22:00:00+00:00')
    def test_rejects_invalid_status(self):
        with self.assertRaises(ValueError):self.load({**self.row(),'status':True})
    def test_rejects_naive_timestamp(self):
        with self.assertRaises(ValueError):self.load({**self.row(),'timestamp':'2026-10-18T01:00:00'})
    def test_rejects_nonfinite_duration(self):
        with self.assertRaises(ValueError):self.load({**self.row(),'duration_ms':float('inf')})
