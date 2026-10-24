"""Terminal reports bind to bounded, regular evidence on the final disk."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import terminal_evidence
from adw_modules.data_types import CCRequest


class TerminalEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.evidence = self.root / 'phase'
        self.evidence.mkdir()
        self.report = self.evidence / 'review.md'
        self.report.write_text('Approved: checked the final diff.\n')
        self.request = CCRequest(prompt='Review', system_prompt='Review role', model='fixture',
            session_id='fixture', session_dir=str(self.root / 'sessions'),
            raw_output_path=str(self.root / 'raw.jsonl'), cwd=str(self.root),
            evidence_paths=['phase/**'])

    def seal(self, **fields):
        return terminal_evidence.EvidenceSeal(self.request, json.dumps({'status': 'success', **fields}))

    def test_final_regular_evidence_has_equal_hash_and_bytes(self):
        seal = self.seal(artifacts=['review.md'], artifact_declarations=[{'path': 'review.md', 'base': 'evidence'}])
        result = seal.verify()
        self.assertTrue(result['verified'])
        self.assertEqual(result['terminal_sha256'], result['final_sha256'])
        self.assertEqual(result['final_files']['phase/review.md']['bytes'], self.report.stat().st_size)

    def test_report_content_creation_deletion_and_mode_changes_invalidate_seal(self):
        for change in ('content', 'create', 'delete', 'mode'):
            with self.subTest(change=change):
                self.report.write_text('Approved report')
                self.report.chmod(0o600)
                extra = self.evidence / 'extra.json'
                extra.unlink(missing_ok=True)
                seal = self.seal()
                if change == 'content': self.report.write_text('Rejected report')
                elif change == 'create': extra.write_text('{}')
                elif change == 'delete': self.report.unlink()
                else: self.report.chmod(0o400)
                self.assertFalse(seal.verify()['verified'])
                self.report.touch()
                self.report.chmod(0o600)

    def test_declaration_only_file_outside_phase_is_bound(self):
        other = self.root / 'outside-phase.md'
        other.write_text('Declared report')
        seal = self.seal(artifact_declarations=[{'path': str(other)}])
        other.write_text('Changed report')
        result = seal.verify()
        self.assertFalse(result['verified'])
        self.assertIn('outside-phase.md', result['changed_paths'])

    def test_malformed_response_keeps_phase_binding_for_normal_json_correction(self):
        seal = terminal_evidence.EvidenceSeal(self.request, 'not JSON')
        self.assertTrue(seal.verify()['verified'])
        self.report.write_text('Changed despite malformed response')
        self.assertFalse(seal.verify()['verified'])

    def test_transcripts_outside_phase_are_not_evidence_mutations(self):
        seal = self.seal()
        (self.root / 'raw.jsonl').write_text('new controller transcript line')
        self.assertTrue(seal.verify()['verified'])

    def test_symlink_fifo_and_hardlink_are_rejected_before_reading(self):
        path = self.evidence / 'unsafe'
        for kind in ('symlink', 'fifo', 'hardlink'):
            with self.subTest(kind=kind):
                if kind == 'symlink': path.symlink_to(self.report)
                elif kind == 'fifo': os.mkfifo(path)
                else: os.link(self.report, path)
                try:
                    with self.assertRaisesRegex(ValueError, 'regular'):
                        self.seal()
                finally:
                    path.unlink()

    def test_declared_artifact_cannot_escape_or_be_missing(self):
        for path in ('../outside.md', 'missing.md'):
            with self.subTest(path=path), self.assertRaises((OSError, ValueError, RuntimeError)):
                self.seal(artifacts=[path])

    def test_symlink_parent_cannot_redirect_declared_evidence(self):
        (self.root / 'alias').symlink_to(self.evidence, target_is_directory=True)
        with self.assertRaises((OSError, ValueError, RuntimeError)):
            self.seal(artifacts=['alias/review.md'])

    def test_size_entry_and_time_bounds_fail_without_truncation(self):
        self.request.evidence_snapshot_max_bytes = 1
        with self.assertRaisesRegex(ValueError, 'snapshot_max_bytes'):
            self.seal()
        self.request.evidence_snapshot_max_bytes = 10000
        with patch.object(terminal_evidence, 'MAX_ENTRIES', 1):
            with self.assertRaisesRegex(ValueError, 'entry/time'):
                self.seal()
        with patch.object(terminal_evidence, 'CAPTURE_SECONDS', -1):
            with self.assertRaisesRegex(ValueError, 'entry/time'):
                self.seal()

    def test_mutation_during_read_is_rejected(self):
        original = os.read
        changed = False
        def mutate(fd, size):
            nonlocal changed
            block = original(fd, size)
            if block and not changed:
                changed = True
                self.report.write_text('Rewritten while being hashed')
            return block
        with patch.object(terminal_evidence.os, 'read', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'changed during capture'):
                self.seal()


if __name__ == '__main__':
    unittest.main()
