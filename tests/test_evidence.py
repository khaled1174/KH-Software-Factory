"""Durable evidence never promotes candidate source or fabricates acceptance."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
from pathlib import Path
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules.evidence import publish_staged_evidence
from adw_modules.scope import ScopeViolation
from adw_modules.tracer import Tracer
from adw_modules.data_types import EventRecord


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.owner = self.root/'owner'
        self.stage = self.root/'stage'
        self.owner.mkdir(); self.stage.mkdir()
        relative = Path('adws/adw_data/sessions/probe/context_handoff')
        self.session = (self.owner/relative).parent
        self.handoff = self.owner/relative
        self.source = self.stage/relative
        self.handoff.mkdir(parents=True); self.source.mkdir(parents=True)
        self.tracer = Tracer(self.owner/'trace.db', self.session/'events.jsonl')
        self.tracer.session_start('probe', 'fixture')
        self.run = SimpleNamespace(repo_root=self.owner, session_dir=self.session,
            context_handoff_dir=self.handoff, tracer=self.tracer, adw_id='probe')
        self.view = SimpleNamespace(repo_root=self.stage, context_handoff_dir=self.source)

    def tearDown(self):
        self.tracer.conn.close()
        self.temp.cleanup()

    def test_failed_candidate_publishes_evidence_without_promoting_source(self):
        (self.owner/'source.py').write_text('owner version')
        (self.stage/'source.py').write_text('candidate rejected')
        (self.stage/'new.py').write_text('unapproved addition')
        (self.source/'review.md').write_text('Rejected: outside task scope.')
        self.tracer.session_finish('probe', ok=False)
        result = publish_staged_evidence(self.run, self.view, status='failed')
        self.assertEqual((self.handoff/'review.md').read_text(), 'Rejected: outside task scope.')
        self.assertEqual((self.owner/'source.py').read_text(), 'owner version')
        self.assertFalse((self.owner/'new.py').exists())
        self.assertEqual(self.tracer.conn.execute('SELECT status FROM sessions').fetchone()[0], 'fail')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['source_promoted'], 'not established by evidence publication')

    def test_references_remapped_and_exact_originals_preserved(self):
        artifact = str(self.source/'review.md')
        destination = str(self.handoff/'review.md')
        (self.source/'review.md').write_text('Concrete finding')
        record = {'artifacts':[artifact], 'document_path':artifact, 'approved':False,
                  'summary':'Keep literal '+artifact, 'command':'cat '+artifact,
                  'nested':{'output_artifact':artifact}}
        raw = json.dumps(record, indent=4).encode()
        envelope = self.session/'reviewer/phases/review/envelope-original.json'
        envelope.parent.mkdir(parents=True); envelope.write_bytes(raw)
        prompt = envelope.parent/'user.md'
        prompt.write_text('Original model prompt '+artifact)
        self.tracer.conn.execute('INSERT INTO envelopes (envelope_id,adw_id,payload_json,valid) VALUES (?,?,?,?)',
                                 ('env-fixture','probe',raw.decode(),1))
        self.tracer.event(EventRecord(adw_id='probe', phase_id='review',type='handoff',name='review',payload=record))
        events_before = self.tracer.events_jsonl.read_bytes()
        publish_staged_evidence(self.run,self.view)
        rewritten = json.loads(envelope.read_text())
        self.assertEqual(rewritten['artifacts'],[destination])
        self.assertEqual(rewritten['document_path'],destination)
        self.assertEqual(rewritten['nested']['output_artifact'],destination)
        self.assertEqual(rewritten['summary'],record['summary'])
        self.assertEqual(rewritten['command'],record['command'])
        self.assertFalse(rewritten['approved'])
        self.assertEqual(prompt.read_text(),'Original model prompt '+artifact)
        for table in ('events','envelopes'):
            payload = json.loads(self.tracer.conn.execute(f'SELECT payload_json FROM {table}').fetchone()[0])
            self.assertEqual(payload['artifacts'],[destination])
        event = json.loads(self.tracer.events_jsonl.read_text())
        self.assertEqual(event['payload']['artifacts'],[destination])
        originals = [p.read_bytes() for p in (self.session/'evidence-reference-originals').rglob('*.original')]
        self.assertIn(raw, originals)
        self.assertIn(events_before, originals)
        mapping = json.loads((self.session/'artifact-map.json').read_text())
        self.assertEqual(mapping['mappings'][0]['staging_root'],str(self.stage))
        # A finally block can safely publish the same evidence again.
        publish_staged_evidence(self.run,self.view,status='failed')
        self.assertEqual(len(json.loads((self.session/'artifact-map.json').read_text())['mappings']),1)

    def test_prior_controller_evidence_cannot_be_overwritten(self):
        (self.handoff/'previous.md').write_text('original accepted evidence')
        (self.source/'previous.md').write_text('worker replacement')
        (self.source/'new.md').write_text('new evidence')
        with self.assertRaises(ScopeViolation):
            publish_staged_evidence(self.run,self.view)
        self.assertEqual((self.handoff/'previous.md').read_text(),'original accepted evidence')
        self.assertFalse((self.handoff/'new.md').exists())

    def test_source_member_and_destination_parent_symlinks_rejected(self):
        outside = self.root/'outside'; outside.mkdir()
        secret = outside/'secret'; secret.write_text('outside')
        (self.source/'linked.md').symlink_to(secret)
        with self.assertRaises(ScopeViolation): publish_staged_evidence(self.run,self.view)
        self.assertFalse((self.handoff/'linked.md').exists())
        (self.source/'linked.md').unlink()
        (self.source/'nested').mkdir()
        (self.source/'nested/report.md').write_text('evidence')
        (self.handoff/'nested').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ScopeViolation): publish_staged_evidence(self.run,self.view)
        self.assertFalse((outside/'report.md').exists())

    def test_source_root_symlink_rejected(self):
        replacement = self.root/'replacement'; replacement.mkdir()
        (replacement/'report.md').write_text('outside source')
        self.source.rmdir()
        self.source.symlink_to(replacement,target_is_directory=True)
        with self.assertRaises(ScopeViolation): publish_staged_evidence(self.run,self.view)
        self.assertFalse((self.handoff/'report.md').exists())


if __name__ == '__main__': unittest.main()
