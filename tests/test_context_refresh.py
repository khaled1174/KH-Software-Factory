"""Reviewed context refresh before a later task, through real controller entry points."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())
import json
import os
from pathlib import Path
import sys
import subprocess
import unittest
from unittest.mock import patch

from tests import test_start as startup
from tests import test_corrective_release as corrective
sys.path.insert(0, str(startup.ADWS))
import adw_project
from adw_modules import agent_cc, project, project_context as pc


class ContextRefreshTests(unittest.TestCase):
    setUp = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    invoke = startup.StartupTests.invoke
    unchanged = startup.StartupTests.unchanged
    init_plan = corrective.CorrectiveReleaseTests.init_plan

    def ready_context(self):
        observed = pc.inspect_context(self.root)['observed']
        baseline = {'observed': observed, 'digest': pc.digest(observed)}
        proposal = pc.ProjectContextProposal(status='success', baseline_digest=baseline['digest'],
            documents=[
                pc.ContextDocument(path='CLAUDE.md', content='Read [rules](docs/agent-guide/rules.md).\n'),
                pc.ContextDocument(path='AGENTS.md', content='Read [rules](docs/agent-guide/rules.md).\n'),
                pc.ContextDocument(path='docs/agent-guide/rules.md', content='Preserve owner scope.\n')])
        review = pc.ContextReviewOutput(status='success', proposal_digest=pc.digest(proposal.model_dump()),
                                       decision='approve')
        pc.publish_context(self.root, proposal, review, baseline)
        self.assertEqual(pc.inspect_context(self.root)['status'], 'ready')

    def stale_context(self):
        self.ready_context()
        (self.root / 'src/added.py').write_text('owner_file = True\n')
        self.assertEqual(pc.inspect_context(self.root)['status'], 'stale')

    def test_requested_refresh_is_reviewed_before_build_and_preserves_owner_file(self):
        self.stale_context()
        result = self.invoke(extra=['--refresh-context'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run = self.root / 'adws/adw_data/sessions/guarded'
        receipt = json.loads((run / 'context-approval.json').read_text())
        self.assertEqual(receipt['status'], 'ready')
        self.assertEqual(pc.inspect_context(self.root)['status'], 'ready')
        self.assertEqual((self.root / 'src/added.py').read_text(), 'owner_file = True\n')
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 2\n')
        phases = [p.name for p in (run / 'context_handoff').iterdir()]
        self.assertTrue(any('context_review_' in p for p in phases), phases)
        routing = json.loads((run / 'routing.json').read_text())
        self.assertIn('project_architect', [row['role'] for row in routing['roles']])

    def test_retired_scout_setting_keeps_guarded_context_without_service_calls(self):
        self.cfg['execution'] = {'jev_scout': {'enabled': True}}
        self.cfg['defaults']['generated_paths'] = ['**/custom-build/']
        self.config.write_text(json.dumps(self.cfg))
        generated = self.root / 'custom-build/output.py'
        generated.parent.mkdir()
        generated.write_text('generated = True\n')
        driver = self.base / 'scout-context-driver.py'
        driver.write_text('import json, sys\nfrom pathlib import Path\n' +
            f'sys.path.insert(0, {str(startup.ADWS)!r})\n' + '''
import adw_start
from types import SimpleNamespace
from unittest.mock import patch
from adw_modules import project_context
def forbidden_scout(*args, **kwargs):
    Path('.kh/scout-called').write_text('Retired service was called')
    raise AssertionError('Retired Jev scout must not run')
adw_start.jev_scout = SimpleNamespace(advise=forbidden_scout)
original = project_context.write_understanding
def capture(run, packet):
    (run.repo_root / '.kh/role-context.json').write_text(json.dumps(packet))
    (run.repo_root / '.kh/observed-context.json').write_text(json.dumps(
        project_context.inspect_context(run.repo_root)['observed']['files']))
    return original(run, packet)
project_context.write_understanding = capture
with patch('urllib.request.urlopen', side_effect=AssertionError('No external scout request allowed')):
    raise SystemExit(adw_start.main(prompt='Implement value 2', scope_file=sys.argv[1], adw_id='guarded'))
''')
        result = subprocess.run([sys.executable, str(driver), str(self.scope)], cwd=self.root,
            env={**self.env, 'KH_FIXTURE_MODE': 'good'}, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / '.kh/scout-called').exists())
        paths = json.loads((self.root / '.kh/observed-context.json').read_text())
        self.assertIn('src/app.py', paths)
        self.assertNotIn('custom-build/output.py', paths)
        packet = json.loads((self.root / '.kh/role-context.json').read_text())
        self.assertNotIn('jev_scout', packet)
        self.assertTrue(all('file_count' in row for row in packet['project_map']))
        self.assertLessEqual(len(json.dumps(packet['project_map']).encode()), 32768)
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 2\n')
        run = self.root / 'adws/adw_data/sessions/guarded'
        self.assertTrue((run / 'acceptance.json').is_file())
        mapping = json.loads((run / 'agent_map.json').read_text())
        self.assertNotEqual(mapping['builder']['session_id'], mapping['reviewer']['session_id'])
        self.assertTrue(list(run.rglob('command.log')))
        self.assertNotIn('"jev_scout":', result.stdout)

    def test_refresh_owner_question_never_dispatches_builder(self):
        self.stale_context()
        result = self.invoke('owner-questions', extra=['--refresh-context'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Owner decisions needed', result.stderr)
        run = self.root / 'adws/adw_data/sessions/guarded'
        mapping = json.loads((run / 'agent_map.json').read_text())
        self.assertNotIn('builder', mapping)
        self.assertFalse((run / 'plan_reviewer').exists(), 'owner questions stop before context review')
        self.assertFalse((run / 'context-approval.json').exists())
        self.unchanged()

    def test_dry_run_previews_refresh_without_dispatch_or_context_change(self):
        self.stale_context()
        index = self.root / pc.INDEX
        before = index.read_bytes()
        result = self.invoke(extra=['--refresh-context', '--dry-run'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        preview = json.loads(result.stdout)
        self.assertEqual(preview['context']['status'], 'stale')
        self.assertTrue(preview['refresh_context'])
        self.assertIsNotNone(preview['bootstrap_execution'])
        self.assertEqual(index.read_bytes(), before)
        self.assertFalse((self.root / 'adws/adw_data').exists())

    def test_advisory_cannot_refresh_the_owner_context(self):
        self.stale_context()
        index = self.root / pc.INDEX
        before = index.read_bytes()
        result = self.invoke(extra=['--refresh-context', '--review-existing', '--advisory'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refresh', result.stderr.lower())
        self.assertEqual(index.read_bytes(), before)
        self.assertFalse((self.root / 'adws/adw_data').exists())

    def test_review_existing_missing_context_never_bootstraps(self):
        self.assertEqual(pc.inspect_context(self.root)['status'], 'missing')
        result = self.invoke(extra=['--review-existing', '--advisory'])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'CLAUDE.md').exists())
        self.assertFalse((self.root / 'AGENTS.md').exists())
        self.assertFalse((self.root / pc.INDEX).exists())
        self.assertFalse((self.root / 'adws/adw_data').exists())
        self.unchanged()

    def test_continuous_refreshes_stale_context_before_second_task(self):
        self.init_plan()
        self.ready_context()
        events = []

        def notify(event):
            events.append(event)
            if event['task_id'] == 'T1':
                # An owner file added after acceptance invalidates the next task's map.
                (self.root / 'src/added.py').write_text('owner_file = True\n')
                self.assertEqual(pc.inspect_context(self.root)['status'], 'stale')

        with patch.dict(os.environ, {**self.env, 'KH_FIXTURE_MODE': 'batch'}), \
                patch.object(agent_cc, 'CC_PATH', str(self.cli)):
            result = adw_project.run_continuous(self.root, notify=notify)
        self.assertEqual(result['exit_code'], 0, result)
        self.assertEqual([event['task_id'] for event in events], ['T1', 'T2'])
        self.assertEqual(project.status(self.root)['totals']['completed'], 2)
        second = project.load_task(self.root, 'T2')
        run = self.root / 'adws/adw_data/sessions' / second['run_id']
        self.assertTrue((run / 'context-approval.json').is_file())
        self.assertEqual(pc.inspect_context(self.root)['status'], 'ready')
        self.assertEqual((self.root / 'src/added.py').read_text(), 'owner_file = True\n')
