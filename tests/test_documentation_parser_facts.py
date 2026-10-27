"""Source-bound claims about standard JSON error coordinates, without providers."""
from pathlib import Path
import ast
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import evaluate_documentation as evaluation
from adw_modules import project_context as pc


class ParserPositionRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = evaluation.evaluate()

    def test_standard_parser_coordinates_cannot_be_declared_absent(self):
        row = next(row for row in self.result['cases'] if row['id'] == 'bad-parser-position')
        gate = row['gates']['proposal_gate']
        self.assertFalse(gate['passed'], gate)
        self.assertIn('lineno', str(gate))
        self.assertIn(evaluation.GUIDE, str(gate))

    def test_all_frozen_correct_guides_still_pass(self):
        for row in self.result['cases']:
            if not row['defective']:
                with self.subTest(guide=row['id']):
                    self.assertTrue(row['gates']['proposal_gate']['passed'], row['gates'])
        self.assertEqual(self.result['provider_calls'], 0)


class ParserPositionClaimTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='kh-json-claim-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / 'loader.py'
        self.source.write_text('import json\n\ndef load(text):\n    return json.loads(text)\n')
        self.claim = 'JSONDecodeError has no parser line number, lineno or column information (loader.py:4).\n'

    def validate(self, content=None, **metadata):
        doc = pc.ContextDocument(path='docs/agent-guide/loader.md',
            content=self.claim if content is None else content, **metadata)
        return pc.validate_library_claims(self.root, doc)

    def test_cited_standard_call_refutes_negative_position_claim(self):
        with self.assertRaisesRegex(pc.ContextBlocked, 'lineno'):
            self.validate()

    def test_load_and_qualified_error_name_are_supported(self):
        self.source.write_text('import json\n\ndef load(stream):\n    return json.load(stream)\n')
        with self.assertRaisesRegex(pc.ContextBlocked, 'lineno'):
            self.validate(self.claim.replace('JSONDecodeError', 'json.JSONDecodeError'))

    def test_crlf_claim_paragraph_has_the_same_fact_check(self):
        with self.assertRaisesRegex(pc.ContextBlocked, 'lineno'):
            self.validate(('# Error coordinates\n\n' + self.claim).replace('\n', '\r\n'))

    def test_positive_coordinates_are_accepted(self):
        self.validate('JSONDecodeError provides lineno and colno (loader.py:4).\n')

    def test_missing_loader_prefix_is_distinct_from_parser_coordinates(self):
        self.validate('The loader adds no input-record prefix to JSONDecodeError (loader.py:4).\n')
        self.validate('JSONDecodeError has no line number for the outer input record; lineno is relative to the JSON text (loader.py:4).\n')
        self.validate('JSONDecodeError has no parser line number for the outer input record (loader.py:4).\n')
        self.validate('JSONDecodeError has no parser line number is an incorrect claim (loader.py:4).\n')
        self.validate('JSONDecodeError has no line number (loader.py:4) for the outer input record; its lineno is relative to the JSON text.\n')
        self.validate('JSONDecodeError has no line number (loader.py:4)\nfor the outer input record; its lineno is relative to the JSON text.\n')
        self.validate('Incorrect example:\nJSONDecodeError has no line number (loader.py:4).\n')

    def test_custom_exception_is_not_assumed_to_be_the_library_type(self):
        self.source.write_text('import json\nclass JSONDecodeError(Exception): pass\ndef load(text):\n    return json.loads(text)\n')
        self.validate()

    def test_custom_exception_assignment_and_alias_are_not_standard(self):
        for binding in ['from builtins import ValueError as JSONDecodeError', 'JSONDecodeError = ValueError']:
            with self.subTest(binding=binding):
                self.source.write_text('import json\n' + binding + '\ndef load(text):\n'
                    '    try: return json.loads(text)\n'
                    '    except json.JSONDecodeError as err: raise JSONDecodeError(str(err))\n')
                self.validate()

    def test_shadowed_json_binding_is_not_assumed_standard(self):
        sources = [
            'import json\njson = custom_json\ndef load(text):\n    return json.loads(text)\n',
            'import json\n\ndef load(text, json):\n    return json.loads(text)\n',
            'import json\nfrom custom import json\ndef load(text):\n    return json.loads(text)\n',
            'import json\nimport custom as json\ndef load(text):\n    return json.loads(text)\n',
            'import json\nfrom custom import *\ndef load(text):\n    return json.loads(text)\n',
            'import json\njson.loads = custom_loads\ndef load(text):\n    return json.loads(text)\n',
            'import json, sys\nsys.modules["json"] = custom_json\ndef load(text):\n    return json.loads(text)\n',
            'import json\nexec("json = custom_json")\ndef load(text):\n    return json.loads(text)\n',
        ]
        for source in sources:
            with self.subTest(source=source):
                self.source.write_text(source)
                self.validate()

    def test_custom_json_import_is_not_executed(self):
        self.source.write_text('from custom import json\n\ndef load(text):\n    return json.loads(text)\n')
        self.validate()

    def test_project_local_json_module_is_not_assumed_standard(self):
        shadow = self.root / 'json.py'
        shadow.write_text("raise RuntimeError('do not import local modules')\n")
        self.validate()

    def test_project_local_json_package_is_not_assumed_standard(self):
        package = self.root / 'json'
        package.mkdir()
        (package / '__init__.py').write_text("raise RuntimeError('do not import local packages')\n")
        self.validate()

    def test_local_module_symlink_is_detected_without_following_it(self):
        (self.root / 'json.py').symlink_to('missing-custom-module.py')
        self.validate()

    def test_sibling_json_module_is_not_assumed_standard(self):
        folder = self.root / 'pkg'
        folder.mkdir()
        self.source.rename(folder / 'loader.py')
        (folder / 'json.py').write_text("raise RuntimeError('do not import sibling modules')\n")
        self.validate(self.claim.replace('loader.py', 'pkg/loader.py'))

    def test_cited_source_is_never_executed(self):
        self.source.write_text("import json\nraise RuntimeError('do not execute project code')\ndef load(text):\n    return json.loads(text)\n")
        before = self.source.read_bytes(), self.source.stat().st_mtime_ns
        with self.assertRaisesRegex(pc.ContextBlocked, 'lineno'):
            self.validate()
        self.assertEqual((self.source.read_bytes(), self.source.stat().st_mtime_ns), before)

    def test_quoted_and_fenced_incorrect_examples_are_not_current_claims(self):
        for opening, closing in [('```text', '```'), ('~~~text', '~~~'), ('````text', '`````')]:
            with self.subTest(fence=opening):
                self.validate('Incorrect example:\n' + opening + '\n' + self.claim + closing + '\n')
        self.validate('> Incorrect claim: ' + self.claim)
        self.validate('```text\n' + self.claim)

    def test_historical_record_is_preserved(self):
        self.validate(purpose='change_record', status='historical')

    def test_wrong_source_line_stays_under_existing_independent_review(self):
        self.validate(self.claim.replace(':4', ':1'))

    def test_architect_refuses_new_publication_of_the_same_false_claim(self):
        observed = pc.inspect_context(self.root)['observed']
        baseline = {'observed': observed, 'digest': pc.digest(observed)}
        proposal = pc.ProjectContextProposal(status='success', baseline_digest=baseline['digest'],
            documents=[pc.ContextDocument(path='CLAUDE.md', content='Project instructions.\n'),
                       pc.ContextDocument(path='AGENTS.md', content='Project instructions.\n'),
                       pc.ContextDocument(path='docs/agent-guide/loader.md', content=self.claim)])
        result = pc.validate_proposal(self.root, proposal, baseline)
        self.assertFalse(result['valid'], result)
        self.assertEqual(len(result['findings']), 1, result)
        self.assertIn('lineno', result['findings'][0])

    def test_deep_unrelated_ast_does_not_abort_architect_validation(self):
        self.source.write_text('import json\nvalue = thing' + '.a' * 1200 + '\ndef load(text):\n'
            '    return json.loads(text)\n')
        observed = pc.inspect_context(self.root)['observed']
        baseline = {'observed': observed, 'digest': pc.digest(observed)}
        proposal = pc.ProjectContextProposal(status='success', baseline_digest=baseline['digest'],
            documents=[pc.ContextDocument(path='CLAUDE.md', content='Project instructions.\n'),
                       pc.ContextDocument(path='AGENTS.md', content='Project instructions.\n'),
                       pc.ContextDocument(path='docs/agent-guide/loader.md', content=self.claim)])
        before = self.source.read_bytes(), self.source.stat().st_mtime_ns
        result = pc.validate_proposal(self.root, proposal, baseline)
        self.assertIsInstance(result['valid'], bool)
        self.assertIsInstance(result['findings'], list)
        self.assertEqual((self.source.read_bytes(), self.source.stat().st_mtime_ns), before)

    def test_parser_depth_rejection_stays_under_existing_review(self):
        with patch.object(ast, 'parse', side_effect=RecursionError('unsupported AST depth')):
            self.validate()

    def test_impossible_large_line_reference_does_not_abort_check(self):
        self.validate(self.claim.replace(':4', ':' + '9' * 5000))

    def test_unterminated_long_fence_stays_bounded(self):
        def expired(*_):
            raise TimeoutError('unbounded Markdown fence processing')
        previous = signal.signal(signal.SIGALRM, expired)
        try:
            signal.alarm(1)
            self.validate('`' * pc.MAX_BYTES)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)


if __name__ == '__main__':
    unittest.main()
