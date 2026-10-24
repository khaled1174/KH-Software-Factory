"""Release packaging must never leave an unverified ZIP behind."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kh_package", ROOT / "scripts/package.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)
eval_spec = importlib.util.spec_from_file_location('kh_evaluations', ROOT / 'scripts/run_evals.py')
evaluations = importlib.util.module_from_spec(eval_spec)
eval_spec.loader.exec_module(evaluations)


def copy_source(destination):
    def ignore(directory, names):
        return [name for name in names if name in package.EXCLUDED_PARTS or
                package.local_state((Path(directory) / name).relative_to(ROOT))]
    shutil.copytree(ROOT, destination, ignore=ignore)
    return destination

# Executable evaluation definitions only; generated evidence lives outside the tree.
EVIDENCE_ALLOWLIST = {
    'evals/eval-01-command-inventory.json',
    'evals/eval-02-commands-documented.json',
    'evals/eval-03-skill-frontmatter.json',
    'evals/eval-04-core-skills-ship.json',
    'evals/eval-05-protected-paths-valid.json',
    'evals/eval-06-governance-self-protected.json',
    'evals/eval-07-claude-md-mirrors-list.json',
    'evals/eval-08-hook-behavior.json',
    'evals/eval-10-secrets-read-denied.json',
    'evals/eval-11-manifest-verifies.json',
    'evals/eval-12-release-links-resolve.json',
    'evals/eval-13-env-sample-clean.json',
    'evals/eval-14-fable-propose-only.json',
    'evals/eval-15-astra-propose-only.json',
    'evals/eval-16-router-stays-subordinate.json',
    'evals/eval-17-router-names-controller.json',
    'evals/eval-18-no-automatic-schedules.json',
    'evals/eval-19-observer-eval-gate.json',
    'evals/eval-20-intent-template.json',
    'evals/eval-22-vendor-pinned.json',
    'evals/eval-30-model-choice-asked.json',
}


def unexpected_evidence(root):
    return {p.relative_to(root).as_posix() for folder in ('docs', 'evals')
            for p in (root / folder).rglob('*')
            if p.is_file() and p.suffix.lower() in {'.json', '.log', '.txt'}} - EVIDENCE_ALLOWLIST


class PackageTests(unittest.TestCase):
    def test_contract_evals_exclude_retired_source_wiring_checks(self):
        numbers = sorted(int(p.name.split('-')[1]) for p in (ROOT / 'evals').glob('eval-*.json'))
        self.assertEqual([*range(1, 9), *range(10, 21), 22, 30], numbers)

    def test_hook_blocks_missing_python_validation_errors_and_python_crashes(self):
        hook = ROOT / '.claude/hooks/protect-paths.sh'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '.claude').mkdir()
            (root / '.claude/protected-paths.txt').write_text('protected.txt\n')
            binaries = root / 'bin'
            binaries.mkdir()
            for case in ('missing-python', 'invalid-payload', 'python-crash'):
                with self.subTest(case=case):
                    if case == 'python-crash':
                        python = binaries / 'python3'
                        python.write_text('#!/bin/sh\nexit 1\n')
                        python.chmod(0o700)
                    env = {**_os.environ, 'CLAUDE_PROJECT_DIR': str(root)}
                    if case != 'invalid-payload':
                        env['PATH'] = str(binaries)
                    result = subprocess.run(['/bin/bash', str(hook)], input='{',
                                            capture_output=True, text=True, cwd=root, env=env)
                    self.assertEqual(2, result.returncode, result.stderr)

    def test_engine_stays_within_owner_approved_line_ceiling(self):
        policy = (ROOT / 'AGENTS.md').read_text()
        ceilings = re.findall(r'^Engine line ceiling: ([0-9]+) physical lines', policy, re.M)
        self.assertEqual(1, len(ceilings), 'record the measured engine ceiling once in AGENTS.md')
        engine = ROOT / '.claude/skills/kh/templates/adws'
        lines = sum(len(path.read_text().splitlines()) for path in engine.rglob('*.py'))
        self.assertLessEqual(lines, int(ceilings[0]),
                             'engine growth requires explicit owner approval before raising the ceiling')

    def test_living_verification_is_compact_and_has_a_stable_title(self):
        text = (ROOT / 'docs/VERIFICATION.md').read_text()
        self.assertLess(len(text.splitlines()), 200)
        self.assertEqual('# Verification', text.splitlines()[0])
        for section in ('## Baseline', '## Latest release gate', '## Not verified'):
            self.assertIn(section, text)

    def test_changelog_starts_with_one_current_release(self):
        text = (ROOT / 'CHANGELOG.md').read_text()
        self.assertEqual(['1.0.0'], re.findall(r'^## (.+)$', text, re.M))
        self.assertNotRegex(text, r'/pull/[0-9]|PR-[0-9]')
        readme = (ROOT / 'README.md').read_text()
        self.assertIn('[CHANGELOG.md](CHANGELOG.md)', readme)
        self.assertEqual('# KH Software Factory', readme.splitlines()[0])

    def test_current_documentation_has_one_small_entry_point_and_canonical_map(self):
        self.assertLessEqual(len((ROOT / 'README.md').read_text().splitlines()), 60)
        canonical = {'ARCHITECTURE.md', 'Skills_Integration_Register.md',
                     'THIRD_PARTY.md', 'VERIFICATION.md'}
        pointers = {'PROJECT_TRACKING.md', 'REVIEWED_PLANNING.md',
                    'PROJECT_CONTEXT.md', 'OPERATIONAL_MODES.md'}
        live = {p.name for p in (ROOT / 'docs').glob('*.md')
                if not re.match(r'(?:RELEASE_|VERIFICATION_)[0-9]', p.name)}
        self.assertEqual(canonical | pointers, live)
        for name in pointers:
            self.assertLessEqual(len((ROOT / 'docs' / name).read_text().splitlines()), 8)

    def test_active_docs_keep_send_ceiling_numbers_in_config_only(self):
        paths = [ROOT / p for p in ('README.md', 'START_HERE.md', 'START_HERE_AR.md', 'COMMANDS.md',
                                    'docs/ARCHITECTURE.md')]
        paths += list((ROOT / '.claude/commands').glob('*.md'))
        paths += list((ROOT / '.claude/skills/kh/references').rglob('*.md'))
        violations = []
        for path in paths:
            if not path.is_file() or path == ROOT / '.claude/skills/kh/references/config.md':
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if (re.search(r'\b(?:[0-9]+/)*[0-9]+ (?:total )?model[- ]sends?\b', line)
                        or re.search(r'(?:send ceiling|model-send ceiling|max_model_sends)[^\n]*[0-9]', line)):
                    violations.append(f'{path.relative_to(ROOT)}:{number}')
        self.assertEqual([], violations, 'numeric send budgets belong in references/config.md')

    def test_current_docs_and_instruction_links_resolve(self):
        paths = [ROOT / p for p in ('README.md', 'START_HERE.md', 'START_HERE_AR.md', 'COMMANDS.md',
                 'docs/ARCHITECTURE.md', 'docs/THIRD_PARTY.md',
                 'docs/VERIFICATION.md', 'docs/Skills_Integration_Register.md')]
        paths += list((ROOT / '.claude/commands').glob('*.md'))
        paths += list((ROOT / '.claude/skills/kh/references').rglob('*.md'))
        missing = []
        for path in paths:
            if not path.is_file():
                missing.append(str(path.relative_to(ROOT)))
                continue
            for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', path.read_text()):
                if re.match(r'(?:[a-z]+:|#)', target):
                    continue
                target = target.split('#', 1)[0]
                if target and not any((base / target).exists() for base in (path.parent, ROOT)):
                    missing.append(f'{path.relative_to(ROOT)}: {target}')
        self.assertEqual([], missing)

    def test_start_guides_have_matching_section_order(self):
        expected = [['# KH Software Factory \u2014 start here', '# \u0627\u0644\u0628\u062f\u0621 \u0645\u0639 KH Software Factory'], ['## Install', '## \u0627\u0644\u062a\u062b\u0628\u064a\u062a'], ['## Upgrade and restore', '## \u0627\u0644\u062a\u0631\u0642\u064a\u0629 \u0648\u0627\u0644\u0627\u0633\u062a\u0631\u062c\u0627\u0639'], ['## Configure tools, models and checks', '## \u0625\u0639\u062f\u0627\u062f \u0627\u0644\u0623\u062f\u0648\u0627\u062a \u0648\u0627\u0644\u0646\u0645\u0627\u0630\u062c \u0648\u0627\u0644\u0641\u062d\u0648\u0635'], ['## Intent, plan and project preparation', '## \u0627\u0644\u0646\u064a\u0629 \u0648\u0627\u0644\u062e\u0637\u0629 \u0648\u062a\u062c\u0647\u064a\u0632 \u0641\u0647\u0645 \u0627\u0644\u0645\u0634\u0631\u0648\u0639'], ['## Execute a task or recorded project', '## \u062a\u0646\u0641\u064a\u0630 \u0645\u0647\u0645\u0629 \u0623\u0648 \u062e\u0637\u0629 \u0627\u0644\u0645\u0634\u0631\u0648\u0639'], ['## Follow progress, failure and recovery', '## \u0645\u062a\u0627\u0628\u0639\u0629 \u0627\u0644\u062a\u0642\u062f\u0645 \u0648\u0627\u0644\u0641\u0634\u0644 \u0648\u0627\u0644\u0627\u0633\u062a\u0631\u062f\u0627\u062f'], ['## Documentation and enforcement', '## \u0627\u0644\u062a\u0648\u062b\u064a\u0642 \u0648\u062d\u062f\u0648\u062f \u0627\u0644\u062a\u0646\u0641\u064a\u0630'], ['### Where enforcement lives', '### \u0623\u064a\u0646 \u062a\u0637\u0628\u0642 \u0627\u0644\u062d\u0645\u0627\u064a\u0629'], ['## Optional trace UI', '## \u0648\u0627\u062c\u0647\u0629 \u0627\u0644\u062a\u062a\u0628\u0639 \u0627\u0644\u0627\u062e\u062a\u064a\u0627\u0631\u064a\u0629'], ['## Develop and verify this distribution', '## \u062a\u0637\u0648\u064a\u0631 \u0647\u0630\u0647 \u0627\u0644\u062d\u0632\u0645\u0629 \u0648\u0627\u0644\u062a\u062d\u0642\u0642 \u0645\u0646\u0647\u0627']]
        headings = lambda name: re.findall(r'^#{1,3} .+$', (ROOT / name).read_text(), re.M)
        self.assertEqual([english for english, _ in expected], headings('START_HERE.md'))
        self.assertEqual([arabic for _, arabic in expected], headings('START_HERE_AR.md'))
        commands = lambda name: re.findall(r'```(?:bash|text)\n(.*?)\n```', (ROOT / name).read_text(), re.S)
        self.assertEqual(commands('START_HERE.md'), commands('START_HERE_AR.md'))

    def test_active_markdown_keeps_arabic_in_the_translation_only(self):
        unexpected = []
        for relative in package.members():
            name = relative.as_posix()
            if (relative.suffix != '.md' or name == 'START_HERE_AR.md'
                    or name.startswith(('docs/history/', 'docs/evidence/', 'vendor/'))
                    or re.match(r'docs/(?:RELEASE_|VERIFICATION_)[0-9]', name)):
                continue
            if re.search(r'[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]', (ROOT / relative).read_text()):
                unexpected.append(name)
        self.assertEqual([], unexpected, 'only the maintained translation may contain Arabic prose')

    def test_generated_evidence_requires_explicit_allowlist(self):
        self.assertEqual(set(), unexpected_evidence(ROOT))

    def test_evidence_allowlist_rejects_unlisted_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unlisted = {'docs/generated.json', 'evals/runs/output.log', 'docs/notes.txt'}
            for rel in unlisted | {'evals/eval-01-command-inventory.json'}:
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('fixture')
            self.assertEqual(unlisted, unexpected_evidence(root))

    def test_verification_keeps_full_output_after_success_and_failure(self):
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / 'release.zip'
                captured = []
                workspaces = []

                def build(archive):
                    with zipfile.ZipFile(archive, 'w') as bundle:
                        bundle.writestr(package.NAME + '/MANIFEST.sha256', '')

                def run(command, **kwargs):
                    index = len(captured)
                    if '--project' in command:
                        project = Path(command[command.index('--project') + 1])
                        (project / 'adws').mkdir()
                        (project / 'adws/adw_start.py').write_text('fixture')
                        (project / '.kh').mkdir()
                        (project / '.kh/PROJECT_STATUS.md').write_text('fixture')
                    if index == 0:
                        workspaces.append(Path(kwargs['cwd']))
                    result = subprocess.CompletedProcess(command, int(failed and index == 0),
                        f'HEAD-STDOUT-{index}\n' + 'o' * 6000 + f'\nTAIL-STDOUT-{index}\n',
                        f'HEAD-STDERR-{index}\n' + 'e' * 6000 + f'\nTAIL-STDERR-{index}\n')
                    captured.append(result)
                    return result

                with patch.object(package, 'write_manifest'), patch.object(package, 'build', build), \
                     patch.object(package, 'skipped_local_state', return_value=[]), \
                     patch.object(package.subprocess, 'run', run), patch.object(sys, 'stderr', io.StringIO()):
                    if failed:
                        with self.assertRaisesRegex(SystemExit, 'test suite failed inside the extracted archive'):
                            self.run_main('--output', str(output))
                    else:
                        self.assertEqual(0, self.run_main('--output', str(output)))
                self.assertEqual(not failed, output.exists())
                self.assertFalse(workspaces[0].exists(), 'the extracted workspace was cleaned')
                logs = output.parent / 'release-verification'
                self.assertTrue(logs.is_dir(), 'complete verification output must survive extraction cleanup')
                self.assertEqual({text for result in captured for text in (result.stdout, result.stderr)},
                                 {p.read_text() for p in logs.rglob('*.log')})
                self.assertEqual(1 if failed else 8, len(captured))

    def run_main(self, *argv):
        with patch.object(sys, "argv", ["package.py", *argv]):
            return package.main()

    def test_any_failure_during_verification_removes_the_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "release.zip"
            for failure in (OSError("disk"), SystemExit("tests failed"), KeyboardInterrupt()):
                with self.subTest(failure=type(failure).__name__):
                    with patch.object(package, "verify", side_effect=failure), patch.object(package, "write_manifest"):
                        with self.assertRaises(type(failure)):
                            self.run_main("--output", str(output))
                    self.assertFalse(output.exists())

    def test_build_failure_also_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "release.zip"
            with patch.object(package, "build", side_effect=OSError("write")), patch.object(package, "write_manifest"):
                with self.assertRaises(OSError):
                    self.run_main("--output", str(output))
            self.assertFalse(output.exists())

    def test_skip_verify_is_labelled_unverified(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_source(Path(temporary) / 'source')
            output = Path(temporary) / "release.zip"
            result = subprocess.run([sys.executable, str(source / "scripts/package.py"), "--output", str(output),
                                     "--skip-verify"], cwd=source, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("SKIPPED", result.stdout)
            self.assertNotIn("built and verified", result.stdout)
            self.assertTrue(output.is_file())

    def test_skip_verify_test_preserves_stale_source_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_source(Path(temporary) / 'source')
            template = source / '.claude/skills/kh/templates/intent.md'
            template.write_text(template.read_text() + '\nFixture-only source change.\n')
            manifest = source / 'MANIFEST.sha256'
            before = manifest.read_bytes()
            spec = {'manifest': 'MANIFEST.sha256'}
            with patch.object(evaluations, 'ROOT', source):
                self.assertFalse(evaluations.check_manifest_verifies(spec)[0])
            result = subprocess.run([sys.executable, '-B', '-m', 'unittest',
                'tests.test_package.PackageTests.test_skip_verify_is_labelled_unverified'],
                cwd=source, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(manifest.read_bytes(), before)
            with patch.object(evaluations, 'ROOT', source):
                passed, detail = evaluations.check_manifest_verifies(spec)
            self.assertFalse(passed, detail)
            self.assertIn('templates/intent.md', detail)

    def test_intent_evaluation_requires_task_request(self):
        spec = json.loads((ROOT / 'evals/eval-20-intent-template.json').read_text())['check']
        original = (ROOT / spec['path']).read_text()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            template = source / spec['path']
            template.parent.mkdir(parents=True)
            template.write_text(original)
            with patch.object(evaluations, 'ROOT', source):
                self.assertTrue(evaluations.check_contains(spec)[0])
                template.write_text(original.replace('## Task request', '## Missing request'))
                passed, detail = evaluations.check_contains(spec)
            self.assertFalse(passed, detail)
            self.assertIn('## Task request', detail)

    def test_ci_checks_archive_once_per_feature_pr_update(self):
        workflow = yaml.load((ROOT / '.github/workflows/verify.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow['on'], {'push': {'branches': ['main']}, 'pull_request': {}, 'workflow_dispatch': {}})
        steps = workflow['jobs']['verify']['steps']
        commands = [step['run'] for step in steps if 'run' in step]
        self.assertFalse(any('-m unittest discover' in command for command in commands))
        self.assertLess(commands.index('python scripts/run_evals.py'),
                        commands.index('python scripts/package.py --output /tmp/kh-software-factory.zip'))
        self.assertIn('"$KH_BUN" install --frozen-lockfile', commands)
        self.assertIn('"$KH_BUN" run build', commands)
        self.assertIn('"$KH_BUN" run lint', commands)
        self.assertTrue(any(step.get('uses', '').startswith('actions/upload-artifact@') and
                            step.get('with', {}).get('name') == 'kh-call-counts' for step in steps))
        logs = next((step for step in steps if step.get('with', {}).get('name') == 'kh-release-verification'), {})
        self.assertTrue(logs.get('uses', '').startswith('actions/upload-artifact@'))
        self.assertEqual('always()', logs.get('if'))
        self.assertEqual('/tmp/kh-software-factory-verification', logs.get('with', {}).get('path'))


if __name__ == "__main__":
    unittest.main()
