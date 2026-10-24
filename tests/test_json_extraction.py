"""Documenter responses and fail-closed response framing contracts."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import agents, terminal_evidence
from adw_modules.data_types import CCRequest, EnvelopeBase
from adw_modules.documentation import DocumentationProposalOutput

FIXTURES = ROOT / 'tests/fixtures/json_extraction'


class DocumenterResponseTests(unittest.TestCase):
    def test_recorded_documenter_responses_are_not_truncated_at_inner_fences(self):
        provenance = json.loads((FIXTURES / 'samples.json').read_text())
        self.assertEqual(len(provenance['samples']), 5)
        for sample in provenance['samples']:
            with self.subTest(response=sample['response_file']):
                raw = (FIXTURES / sample['response_file']).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), sample['response_sha256'])
                actual = terminal_evidence.extract_json(raw.decode())
                DocumentationProposalOutput.model_validate(actual)
                canonical = json.dumps(actual, sort_keys=True, separators=(',', ':')).encode()
                self.assertEqual(hashlib.sha256(canonical).hexdigest(), sample['expected_object_sha256'])


class FramedEvidenceTests(unittest.TestCase):
    def test_selected_role_evidence_is_bound_to_final_disk_for_typed_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / 'phase').mkdir()
            report = root / 'declared-review.md'
            report.write_text('Reviewed final files.\n')
            request = CCRequest(prompt='Review', system_prompt='Review role', model='fixture',
                session_id='fixture', session_dir=str(root / 'sessions'),
                raw_output_path=str(root / 'raw.jsonl'), cwd=str(root), evidence_paths=['phase/**'])
            text = '{"metadata": true}\n```json\n' + json.dumps({
                'status': 'success', 'artifacts': [str(report)]}) + '\n```'
            seal = terminal_evidence.EvidenceSeal(request, text)
            typed = EnvelopeBase.model_validate(agents._extract_json(text))
            self.assertEqual(typed.artifacts, [str(report)])
            self.assertTrue(seal.verify()['verified'])
            report.write_text('Changed after declaring success.\n')
            verified = seal.verify()
            self.assertFalse(verified['verified'])
            self.assertIn('declared-review.md', verified['changed_paths'])


class ResponseFramingTests(unittest.TestCase):
    payload = {'status': 'success', 'summary': 'An intact response',
               'artifacts': [], 'notes_for_next_agent': 'Keep {braces} and ``` fences intact.'}

    def test_plain_fenced_and_prose_wrapped_responses(self):
        encoded = json.dumps(self.payload)
        for wrapped in (encoded, '```json\n' + encoded + '\n```',
                        '```JSON\n' + encoded + '\n```', '```\n' + encoded + '\n```',
                        'A short explanation.\n' + encoded + '\nDone.',
                        'Text {placeholder} before.\n' + encoded + '\nText {aside} after.',
                        'A [note] and "quoted prose" before.\n' + encoded):
            with self.subTest(response=wrapped[:35]):
                self.assertEqual(terminal_evidence.extract_json(wrapped), self.payload)

    def test_multiple_objects_allow_only_one_unambiguous_role_response(self):
        encoded = json.dumps(self.payload)
        for wrapped in ('{"debug": true}\n' + encoded,
                        encoded + '\n{"timing_ms": 10}',
                        '{"debug": {"nested": true}}\n' + encoded + '\n{"timing_ms": 10}'):
            with self.subTest(response=wrapped[:35]):
                self.assertEqual(terminal_evidence.extract_json(wrapped), self.payload)

    def test_multiple_role_objects_are_rejected_instead_of_choosing_a_draft(self):
        encoded = json.dumps(self.payload)
        for wrapped in (encoded + '\n' + encoded,
                        '{"status": "fail"}\n' + encoded,
                        '{"status": "unfinished"}\n' + encoded,
                        '{"example": 1}\n{"example": 2}'):
            with self.subTest(response=wrapped[:35]):
                with self.assertRaisesRegex(ValueError, 'ambiguous'):
                    terminal_evidence.extract_json(wrapped)

    def test_nested_objects_are_part_of_the_complete_outer_object(self):
        payload = {**self.payload, 'observations': [{'nested': {'status': 'fail'}}]}
        self.assertEqual(terminal_evidence.extract_json(json.dumps(payload)), payload)

    def test_empty_object_is_extracted_then_rejected_by_the_existing_schema(self):
        self.assertEqual(terminal_evidence.extract_json('{}'), {})
        with self.assertRaises(ValueError):
            EnvelopeBase.model_validate(terminal_evidence.extract_json('{}'))

    def test_arrays_and_encoded_strings_do_not_expose_nested_role_objects(self):
        encoded = json.dumps(self.payload)
        for wrapped in (json.dumps([self.payload]), json.dumps(encoded), '[]', 'null', '17'):
            with self.subTest(response=wrapped[:35]):
                with self.assertRaisesRegex(ValueError, 'no JSON object'):
                    terminal_evidence.extract_json(wrapped)

    def test_malformed_json_is_never_repaired_or_replaced_with_a_nested_object(self):
        encoded = json.dumps(self.payload)
        for wrapped in ('{"status": "success",}', '{"wrapper": ' + encoded,
                        '{"wrapper": ' + encoded + ', "broken": }',
                        '{"status": "success"\n' + encoded,
                        encoded + '\n{"status": "fail",}',
                        '{"status": "fail",}\n' + encoded,
                        '{"summary": "unterminated ' + encoded,
                        '{status: "fail"}\n' + encoded,
                        "{'status': 'fail'}\n" + encoded,
                        '{wrapper: ] ' + encoded + ' }',
                        '[wrapper: } ' + encoded + ' ]'):
            with self.subTest(response=wrapped[:35]):
                with self.assertRaisesRegex(ValueError, 'invalid JSON'):
                    terminal_evidence.extract_json(wrapped)

    def test_absent_json_fails_with_a_clear_message(self):
        for wrapped in ('', 'No structured response.', '```json\nnot JSON\n```'):
            with self.subTest(response=wrapped):
                with self.assertRaisesRegex(ValueError, 'no JSON object'):
                    terminal_evidence.extract_json(wrapped)

    def test_non_json_numeric_constants_fail_closed(self):
        for constant in ('NaN', 'Infinity', '-Infinity'):
            with self.subTest(constant=constant):
                with self.assertRaisesRegex(ValueError, 'invalid JSON'):
                    terminal_evidence.extract_json('{"status": "success", "value": ' + constant + '}')

    def test_unmatched_delimiters_and_truncated_suffixes_do_not_accept_partial_results(self):
        encoded = json.dumps(self.payload)
        for wrapped in (encoded + '}', ']' + encoded, encoded + ']', '}' + encoded,
                        encoded + ', "status": "fail"}', encoded + ' "unfinished',
                        '"unfinished', '"escaped quote \\" but no closing quote'):
            with self.subTest(response=wrapped[:35]):
                with self.assertRaisesRegex(ValueError, 'invalid JSON'):
                    terminal_evidence.extract_json(wrapped)
