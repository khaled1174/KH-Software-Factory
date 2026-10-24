"""Fixed-input equivalence for content digest helpers."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import unittest

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import conditional_docs, planning, project_context, utils


# Frozen from 8c261aa; do not replace these oracles with the shared implementation.
def legacy_context_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def legacy_conditional_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def legacy_planning_digest(value):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode='json')
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode()).hexdigest()


class FixedRecord(BaseModel):
    path: Path
    created: datetime
    values: list[str]


class SharedDigestTests(unittest.TestCase):
    def test_fixed_inputs_match_all_three_legacy_functions(self):
        values = [None, False, True, 0, -7, 3.25, float('nan'), float('inf'),
                  'Escaped \u0627\u0644\u0639\u0631\u0628\u064a\u0629 and \U0001f680',
                  {'z': [1, {'b': False, 'a': None}], 'a': 'line\ntext'}, []]
        for value in values:
            for old in (legacy_context_digest, legacy_conditional_digest, legacy_planning_digest):
                with self.subTest(value=value, legacy=old.__name__):
                    self.assertEqual(old(value), utils.digest(value))
        for value in (object(), {'unsupported'}):
            with self.subTest(unsupported=type(value).__name__):
                with self.assertRaises(TypeError):
                    utils.digest(value)

    def test_fixed_pydantic_input_matches_legacy_planning(self):
        value = FixedRecord(path=Path('src/example.py'),
                            created=datetime(2026, 10, 23, 12, tzinfo=timezone.utc),
                            values=['fixed', '\u00e9'])
        self.assertEqual(legacy_planning_digest(value), utils.digest(value))
        self.assertEqual(legacy_planning_digest(value), planning.digest(value))

    def test_all_existing_digest_entrypoints_use_the_shared_function(self):
        for module in (planning, project_context, conditional_docs):
            with self.subTest(module=module.__name__):
                self.assertIs(module.digest, utils.digest)


if __name__ == '__main__':
    unittest.main()
