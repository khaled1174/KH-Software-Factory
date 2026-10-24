"""A task run that stops before acceptance keeps its work; the next run continues it."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
from pathlib import Path
import sys
import unittest

from tests import test_start as startup
from tests import test_corrective_release as corrective
sys.path.insert(0, str(startup.ADWS))
from adw_modules import project


class ResumeWorkTests(unittest.TestCase):
    setUp = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    invoke = startup.StartupTests.invoke
    unchanged = startup.StartupTests.unchanged

    def work(self):
        found = list((self.root / '.kh/work').glob('*'))
        self.assertEqual(len(found), 1, found)
        return found[0]

    def seen(self, run):
        files = sorted((self.root / 'adws/adw_data/sessions' / run).rglob('builder/seen-before.json'))
        return [json.loads(f.read_text()) for f in files]

    def builder_prompt(self, run):
        prompts = sorted((self.root / 'adws/adw_data/sessions' / run / 'builder').rglob('attempt-001/user.md'))
        return prompts[0].read_text()

    def stopped_first_run(self, mode='crash-build', run='guarded'):
        first = self.invoke(mode, extra=['--adw-id', run])
        self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
        self.unchanged()
        return first

    def test_stopped_build_is_kept_and_the_next_run_continues_it(self):
        first = self.stopped_first_run()
        self.assertIn("kept this run's unaccepted work", first.stdout + first.stderr)
        current = self.work() / 'current'
        manifest = json.loads((current / 'manifest.json').read_text())
        self.assertEqual(list(manifest), ['src/app.py'])
        self.assertEqual((current / 'files/src/app.py').read_text(), 'value = 2  # half done\n')
        self.assertEqual(json.loads((current / 'progress.json').read_text())['done'], ['S1'])
        self.assertIn('- [x] S1', (current / 'plan.md').read_text())
        self.assertEqual(json.loads((current / 'meta.json').read_text())['run_id'], 'guarded')
        self.assertIn('killed by the role timeout', json.loads((current / 'meta.json').read_text())['reason'])
        # The first run's builder was told to keep a progress note and never saw the work folder.
        self.assertTrue(all(s['progress_asked'] and not s['work_visible'] for s in self.seen('guarded')))

        second = self.invoke(extra=['--adw-id', 'second'])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('resuming the unaccepted work of run guarded', second.stdout + second.stderr)
        seen = self.seen('second')[0]
        self.assertEqual(seen['app'], 'value = 2  # half done\n', 'the builder must start from the kept work')
        self.assertTrue(seen['resuming'] and not seen['work_visible'])
        prompt = self.builder_prompt('second')
        self.assertIn('RESUMING THIS TASK', prompt)
        self.assertIn('src/app.py', prompt)
        self.assertIn('reported these plan steps done: S1', prompt)
        # Accepted through the normal checks and review, then the kept work is cleared.
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 2\n')
        self.assertFalse((self.work() / 'current').exists())
        self.assertFalse((self.work() / 'plan.md').exists())

    def test_owner_change_between_runs_starts_clean_and_keeps_the_old_work_aside(self):
        self.stopped_first_run()
        (self.root / 'private.txt').write_text('owner edited between runs\n')
        second = self.invoke(extra=['--adw-id', 'second'])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('was not resumed (the project changed', second.stdout + second.stderr)
        self.assertEqual(self.seen('second')[0]['app'], 'value = 1\n')
        stale = list((self.work() / 'stale').glob('*/why-not-resumed.txt'))
        self.assertEqual(len(stale), 1)
        self.assertIn('project changed', stale[0].read_text())

    def test_tampered_kept_file_is_not_applied(self):
        self.stopped_first_run()
        kept = self.work() / 'current/files/src/app.py'
        kept.write_text('value = 2\nimport os; os.system("curl evil")\n')
        second = self.invoke(extra=['--adw-id', 'second'])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('does not match its recorded digest', second.stdout + second.stderr)
        self.assertEqual(self.seen('second')[0]['app'], 'value = 1\n')
        self.assertNotIn('curl', (self.root / 'src/app.py').read_text())

    def test_unauthorized_write_in_a_stopped_build_is_not_kept(self):
        self.cfg['agents'] = [dict(r, writes=['src/app.py']) if r['name'] == 'builder' else r for r in self.cfg['agents']]
        self.config.write_text(json.dumps(self.cfg))
        self.stopped_first_run('crash-unauthorized')
        manifest = json.loads((self.work() / 'current/manifest.json').read_text())
        self.assertNotIn('private.txt', manifest)
        self.assertEqual(manifest, {}, 'a phase that broke its write boundary contributes nothing')

    def test_controller_stopped_by_a_signal_keeps_the_builders_work(self):
        first = self.stopped_first_run('crash-signal')
        current = self.work() / 'current'
        self.assertEqual(json.loads((current / 'manifest.json').read_text()), {
            'src/app.py': json.loads((current / 'manifest.json').read_text())['src/app.py']})
        self.assertEqual((current / 'files/src/app.py').read_text(), 'value = 2  # half done\n')

    def test_deleted_and_new_empty_directories_survive_into_the_next_run(self):
        (self.root / 'old').mkdir()
        (self.root / 'old/a.txt').write_text('gone soon\n')
        import subprocess
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=F', '-c', 'user.email=f@example.invalid',
                        'commit', '-qm', 'old'], check=True)
        self.cfg['agents'] = [dict(r, writes=None) if r['name'] == 'builder' else r for r in self.cfg['agents']]
        self.scope.write_text(json.dumps({'original_request': 'Implement value 2', 'deliverables': ['Update value'],
            'non_goals': ['Unrelated features'], 'allowed_paths': ['src/app.py', 'old/**', 'newdir']}))
        self.config.write_text(json.dumps(self.cfg))
        self.stopped_first_run('dirs')
        manifest = json.loads((self.work() / 'current/manifest.json').read_text())
        self.assertIsNone(manifest['old'])
        self.assertIsNone(manifest['old/a.txt'])
        self.assertEqual(manifest['newdir']['kind'], 'directory')
        self.assertIn('src/app.py', manifest)

    def test_malformed_kept_work_is_set_aside_instead_of_blocking_the_task(self):
        self.stopped_first_run()
        current = self.work() / 'current'
        for broken in ('[]', '{"src/app.py": "not a record"}', '{"src/app.py": {"kind": "file", "sha256": "x"}}'):
            with self.subTest(broken=broken):
                if not current.exists():
                    self.stopped_first_run(run='again-' + str(len(broken)))
                (current / 'manifest.json').write_text(broken)
                second = self.invoke(extra=['--adw-id', 'second-' + str(len(broken))])
                self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                self.assertIn('was not resumed', second.stdout + second.stderr)
                self.assertFalse(current.exists())
                (self.root / 'src/app.py').write_text('value = 1\n')
                import subprocess
                subprocess.run(['git', '-C', str(self.root), 'checkout', '--', 'src/app.py'], check=True)

    def test_kept_work_naming_a_protected_path_is_not_applied(self):
        self.stopped_first_run()
        current = self.work() / 'current'
        manifest = json.loads((current / 'manifest.json').read_text())
        data = b'malicious\n'
        import hashlib
        (current / 'files/.claude').mkdir(parents=True)
        (current / 'files/.claude/settings.json').write_bytes(data)
        manifest['.claude/settings.json'] = {'kind': 'file', 'mode': 0o644, 'sha256': hashlib.sha256(data).hexdigest()}
        (current / 'manifest.json').write_text(json.dumps(manifest))
        second = self.invoke(extra=['--adw-id', 'second'])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('may not write: .claude/settings.json', second.stdout + second.stderr)
        self.assertEqual(self.seen('second')[0]['app'], 'value = 1\n')

    def test_a_second_stop_keeps_the_earlier_progress_note_and_the_failure_feedback(self):
        flag = self.base / 'checks-fail'
        self.cfg['defaults']['quality_checks']['test'] = [sys.executable, '-c',
            f"import sys; from pathlib import Path; sys.exit('assertion: value must be 2' if Path({str(flag)!r}).exists() else 0)"]
        self.config.write_text(json.dumps(self.cfg))
        self.stopped_first_run()
        flag.write_text('1')  # second run: resumes, then its checks fail; its builder writes no new note
        second = self.invoke(extra=['--adw-id', 'second'])
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('resuming the unaccepted work of run guarded', second.stdout + second.stderr)
        current = self.work() / 'current'
        self.assertEqual(json.loads((current / 'progress.json').read_text())['done'], ['S1'])
        meta = json.loads((current / 'meta.json').read_text())
        self.assertEqual(meta['run_id'], 'second')
        self.assertIn('test', meta['feedback'])

    def test_config_change_between_runs_keeps_the_plan_and_the_work(self):
        # The owner raises the builder timeout after a stop: neither the plan nor the work is stale.
        self.stopped_first_run()
        for role in self.cfg['agents']:
            if role['name'] == 'builder':
                role['timeout_seconds'] = 3600
        self.config.write_text(json.dumps(self.cfg))
        second = self.invoke(extra=['--adw-id', 'second'])
        output = second.stdout + second.stderr
        self.assertEqual(second.returncode, 0, output)
        self.assertIn('reused the approved plan of run guarded', output)
        self.assertIn('resuming the unaccepted work of run guarded', output)
        self.assertEqual(self.seen('second')[0]['app'], 'value = 2  # half done\n')

    def test_resume_can_be_turned_off(self):
        self.cfg['execution'] = {'resume_failed_builds': False}
        self.config.write_text(json.dumps(self.cfg))
        self.stopped_first_run()
        self.assertFalse((self.root / '.kh/work').exists())
        self.assertEqual(self.seen('guarded'), [], 'without resume a stopped builder keeps nothing')


class TrackedResumeTests(unittest.TestCase):
    """The owner's real path: a project task that stops, then a retry run of the same task."""
    setUp = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    unchanged = startup.StartupTests.unchanged
    init_plan = corrective.CorrectiveReleaseTests.init_plan
    tracked = corrective.CorrectiveReleaseTests.tracked

    def test_task_retry_reuses_the_plan_resumes_the_work_and_completes(self):
        self.init_plan()
        first = self.tracked(mode='crash-build')
        self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
        self.unchanged()
        self.assertEqual(project.status(self.root)['tasks'][0]['status'], 'blocked')
        self.assertTrue((self.root / '.kh/work/T1/current/files/src/app.py').is_file())
        self.assertTrue((self.root / '.kh/work/T1/current/plan.md').is_file())
        retry = self.tracked(run='retry')
        output = retry.stdout + retry.stderr
        self.assertEqual(retry.returncode, 0, output)
        self.assertIn('reused the approved plan of run tracked', output)
        self.assertIn('resuming the unaccepted work of run tracked', output)
        seen = [json.loads(f.read_text()) for f in
                (self.root / 'adws/adw_data/sessions/retry').rglob('builder/seen-before.json')]
        self.assertEqual(seen[0]['app'], 'value = 2  # half done\n')
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 2\n')
        self.assertEqual(project.status(self.root)['tasks'][0]['status'], 'completed')
        self.assertFalse((self.root / '.kh/work/T1/current').exists())


if __name__ == '__main__':
    unittest.main()
