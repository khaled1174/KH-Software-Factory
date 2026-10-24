"""Reviewed-plan entry point through real subprocesses with a fake provider CLI."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
from pathlib import Path
import subprocess
import sys
import unittest

from tests import test_start as fixture

sys.path.insert(0, str(fixture.ADWS))
from adw_modules import project_context as pc


class ReviewedPlanCLITests(unittest.TestCase):
    setUp = fixture.StartupTests.setUp
    tearDown = fixture.StartupTests.tearDown
    unchanged = fixture.StartupTests.unchanged

    def context(self):
        observed = pc.inspect_context(self.root)['observed']
        baseline = {'observed': observed, 'digest': pc.digest(observed)}
        proposal = pc.ProjectContextProposal(status='success', baseline_digest=baseline['digest'],
            documents=[pc.ContextDocument(path='CLAUDE.md', content='Read [rules](docs/agent-guide/rules.md).\n'),
                       pc.ContextDocument(path='AGENTS.md', content='Read [rules](docs/agent-guide/rules.md).\n'),
                       pc.ContextDocument(path='docs/agent-guide/rules.md', content='Preserve owner scope.\n')])
        review = pc.ContextReviewOutput(status='success', proposal_digest=pc.digest(proposal.model_dump()), decision='approve')
        pc.publish_context(self.root, proposal, review, baseline)

    def invoke_plan(self, extra=(), mode='good'):
        return subprocess.run([sys.executable, str(fixture.ADWS / 'adw_plan_review.py'),
            '--scope-file', str(self.scope), '--adw-id', 'planonly', *extra, '--', 'Implement value 2'],
            cwd=self.root, env={**self.env, 'KH_FIXTURE_MODE': mode}, text=True, capture_output=True, timeout=60)

    def test_plan_only_approved_with_no_builder_or_source_changes(self):
        self.context()
        result = self.invoke_plan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.unchanged()
        run = self.root / 'adws/adw_data/sessions/planonly'
        receipt = json.loads((run / 'planning/approval.json').read_text())
        self.assertEqual(receipt['version'], 1)
        mapping = json.loads((run / 'agent_map.json').read_text())
        self.assertEqual(set(mapping), {'planner', 'plan_reviewer'})
        self.assertNotEqual(mapping['planner']['session_id'], mapping['plan_reviewer']['session_id'])

    def test_reviewed_plan_rejects_draft_intent_before_dispatch(self):
        self.context()
        self.intent.write_text(self.intent.read_text().replace('Status: accepted','Status: draft'))
        result = self.invoke_plan()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Intent', result.stderr)
        self.assertFalse((self.root / 'adws/adw_data/sessions/planonly').exists())

    def test_missing_context_does_not_dispatch(self):
        result = self.invoke_plan()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('bootstrap', result.stderr)
        self.assertFalse((self.root / 'adws/adw_data/sessions/planonly').exists())
        self.unchanged()

    def test_dry_run_writes_no_session_and_no_source_changes(self):
        self.context()
        result = self.invoke_plan(extra=['--dry-run'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['dry_run'])
        self.assertFalse((self.root / 'adws/adw_data/sessions/planonly').exists())
        self.unchanged()

    def test_scope_violation_fails_without_approval_or_source_changes(self):
        self.context()
        result = self.invoke_plan(mode='plan-widens')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse((self.root / 'adws/adw_data/sessions/planonly/planning/approval.json').exists())
        self.unchanged()


if __name__ == '__main__': unittest.main()
