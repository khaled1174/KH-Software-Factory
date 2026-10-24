"""Codex refuses to run outside a git repository; KH says so before spending anything.

Real Codex CLI 0.162.0 outside a repository: "Not inside a trusted directory
and --skip-git-repo-check was not specified." After `git init` (no commit) it
proceeds. A project that is not a repository is copied as plain files, so the
isolated copy is not one either.
"""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import agents  # noqa: E402


class CodexGitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='kh-codex-git-')
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        # Routing checks executable presence before the Git boundary. These
        # fixtures must never reach a provider or require an installed relay.
        bin_dir = self.project / 'cli-fixtures'
        bin_dir.mkdir()
        for name in ('node', 'codex'):
            executable = bin_dir / name
            executable.write_text('#!' + sys.executable + '\nraise SystemExit("Unexpected fixture CLI dispatch")\n')
            executable.chmod(0o700)
        environment = patch.dict(os.environ, {'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', '')})
        environment.start()
        self.addCleanup(environment.stop)
        cwd = Path.cwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, cwd)

    def problems(self):
        cfg = agents.load_config(str(ROOT / '.claude/skills/kh/templates/kh.config.yaml'))
        builder = next(a for a in cfg.agents if a.name == 'builder')
        builder.coding_agent, builder.model, builder.thinking = 'delegate_codex', 'gpt-5.6', 'high'
        builder.tools, builder.harness_engineering = None, []
        try:
            agents.validate(cfg, ['builder'])
        except SystemExit as error:
            return str(error)
        return ''

    def test_codex_role_outside_a_repository_is_refused_before_dispatch(self):
        message = self.problems()
        self.assertIn('Codex runs only inside a git repository', message)
        self.assertIn('git init', message)

    def test_an_initialised_repository_satisfies_codex(self):
        subprocess.run(['git', 'init', '-q'], cwd=self.project, check=True)
        self.assertNotIn('git repository', self.problems())


if __name__ == '__main__':
    unittest.main()
