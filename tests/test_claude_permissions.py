"""Claude roles can write inside their untrusted isolated copy, and nowhere else.

Real Claude Code (2.1.295) in -p mode ignores .claude/settings*.json in a folder
nobody trusted interactively, and under `--permission-mode default` refuses the
evidence file every role must write ("Claude requested permissions to write
to …, but you haven't granted it yet"). The adapter now runs `acceptEdits`
(edits inside the working directory only) and carries the owner's own rules
through `--settings`.
"""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import agent_cc  # noqa: E402
from adw_modules.data_types import CCRequest  # noqa: E402

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
record = {'argv': args, 'cwd': os.getcwd()}
if '--settings' in args:
    path = args[args.index('--settings') + 1]
    record['settings_path'] = path
    record['settings'] = json.loads(Path(path).read_text())
Path(os.environ['FAKE_CLAUDE_LOG']).write_text(json.dumps(record))
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '{}',
                  'session_id': 'sid', 'total_cost_usd': 0}))
'''

SANDBOX = {
    'enabled': True, 'failIfUnavailable': True, 'allowUnsandboxedCommands': False,
    'autoAllowBashIfSandboxed': False,
    'network': {'allowedDomains': [], 'deniedDomains': ['*'], 'strictAllowlist': True},
    'filesystem': {'denyRead': ['~/.ssh', '~/.aws', '~/.config/gh']},
}
SECRET_DENIES = ['Read(~/.ssh)', 'Read(~/.ssh/**)', 'Read(~/.aws)',
                 'Read(~/.aws/**)', 'Read(~/.config/gh)', 'Read(~/.config/gh/**)']


class ClaudePermissionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='kh-agent-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.work = self.base / 'workspace'
        (self.work / '.claude').mkdir(parents=True)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(agent_cc.INHERIT_ENV, None)

    def settings(self, name, permissions):
        (self.work / '.claude' / name).write_text(json.dumps({'permissions': permissions, 'hooks': {'x': 1}}))

    def request(self):
        return CCRequest(prompt='go', system_prompt='sys', model='sonnet', session_id='sid',
                         session_dir=str(self.base / 'sessions'),
                         raw_output_path=str(self.base / 'attempt' / 'raw_output.jsonl'),
                         tools=['read', 'write'], cwd=str(self.work))

    def test_owner_rules_are_carried_without_edit_grants_and_anchored_to_the_copy(self):
        self.settings('settings.json', {
            'allow': ['Bash(swift:*)', 'Write(//Users/owner/project/**)', 'Edit', 'WebFetch(domain:example.com)',
                      'Read(/docs/**)', 'Bash(swift:*)'],
            'deny': ['Read(./.env)', 'Read(/secrets/**)', 'Edit(//etc/**)', 'Bash(curl:*)'],
            'ask': ['Bash(git push:*)'],
            'additionalDirectories': ['/Users/owner/elsewhere'], 'defaultMode': 'bypassPermissions'})
        self.settings('settings.local.json', {'allow': ['Bash(npm test:*)', 'MultiEdit(/src/**)']})
        anchor = '//' + str(self.work.resolve()).lstrip('/')
        self.assertEqual(agent_cc.owner_permissions(self.work), {
            'allow': ['Bash(swift:*)', 'WebFetch(domain:example.com)', f'Read({anchor}/docs/**)',
                      'Bash(npm test:*)'],
            'deny': ['Read(./.env)', f'Read({anchor}/secrets/**)', 'Edit(//etc/**)', 'Bash(curl:*)'],
            'ask': ['Bash(git push:*)']})

    def test_missing_malformed_or_linked_settings_carry_nothing(self):
        self.assertEqual(agent_cc.owner_permissions(self.work), {})
        (self.work / '.claude/settings.json').write_text('{not json')
        (self.base / 'elsewhere.json').write_text(json.dumps({'permissions': {'allow': ['Bash(rm:*)']}}))
        (self.work / '.claude/settings.local.json').symlink_to(self.base / 'elsewhere.json')
        self.assertEqual(agent_cc.owner_permissions(self.work), {})
        (self.work / '.claude/settings.json').write_text(json.dumps({'permissions': {'allow': 'Bash', 'deny': [7, '']}}))
        self.assertEqual(agent_cc.owner_permissions(self.work), {})

    def test_command_accepts_edits_and_never_bypasses(self):
        cmd = agent_cc._command(self.request(), False, '/elsewhere/settings.json')
        self.assertEqual(cmd[cmd.index('--permission-mode') + 1], 'acceptEdits')
        self.assertEqual(cmd[cmd.index('--settings') + 1], '/elsewhere/settings.json')
        self.assertNotIn('bypassPermissions', cmd)
        self.assertNotIn('--dangerously-skip-permissions', cmd)
        self.assertLess(cmd.index('--settings'), cmd.index('--'), 'the variadic --tools must stay last')
        self.assertNotIn('--settings', agent_cc._command(self.request(), False))

    def run_fake(self):
        fake = self.base / 'claude'
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o700)
        log = self.base / 'claude.json'
        with patch.object(agent_cc, 'CC_PATH', str(fake)), patch.dict(os.environ, {'FAKE_CLAUDE_LOG': str(log)}):
            agent_cc.run(self.request())
        return json.loads(log.read_text())

    def test_every_send_requires_the_sandbox_even_when_user_setup_is_inherited(self):
        self.settings('settings.json', {'allow': ['Bash(printf:*)', 'WebFetch(domain:example.com)'],
                                        'deny': ['Read(./.env)'], 'ask': ['Bash(git push:*)']})
        for inherited in ('0', '1'):
            with self.subTest(inherited=inherited), patch.dict(os.environ, {agent_cc.INHERIT_ENV: inherited}):
                record = self.run_fake()
                self.assertEqual(record['settings'].get('sandbox'), SANDBOX)
                self.assertEqual(record['settings']['permissions'], {
                    'allow': ['Bash(printf:*)', 'WebFetch(domain:example.com)'],
                    'deny': ['Read(./.env)', *SECRET_DENIES], 'ask': ['Bash(git push:*)']})
                self.assertNotIn('--dangerously-skip-permissions', record['argv'])

    def test_generated_settings_live_outside_the_copy_and_are_removed(self):
        self.settings('settings.json', {'allow': ['Bash(swift:*)', 'Write']})
        record = self.run_fake()
        path = Path(record['settings_path'])
        self.assertEqual(record['settings'], {'disableClaudeAiConnectors': True,
                                              'sandbox': SANDBOX,
                                              'permissions': {'allow': ['Bash(swift:*)'], 'deny': SECRET_DENIES}})
        self.assertFalse(str(path.resolve()).startswith(str(self.work.resolve()) + os.sep),
                         'an agent that can edit its copy must not reach its own grants')
        self.assertFalse(path.exists(), 'removed after the send')
        evidence = self.base / 'attempt' / 'claude-settings.json'
        self.assertEqual(json.loads(evidence.read_text()), record['settings'])
        self.assertEqual(record['argv'][record['argv'].index('--permission-mode') + 1], 'acceptEdits')

    def test_no_owner_rules_still_carries_sandbox_and_read_denies(self):
        record = self.run_fake()
        self.assertEqual(record['settings'], {'disableClaudeAiConnectors': True, 'sandbox': SANDBOX,
                                              'permissions': {'deny': SECRET_DENIES}})
        self.assertIn('acceptEdits', record['argv'])

    def test_no_owner_rules_and_inherited_setup_still_has_mandatory_settings(self):
        with patch.dict(os.environ, {agent_cc.INHERIT_ENV: '1'}):
            record = self.run_fake()
        self.assertIn('--settings', record['argv'])
        self.assertEqual(record['settings'], {'sandbox': SANDBOX, 'permissions': {'deny': SECRET_DENIES}})
        self.assertIn('acceptEdits', record['argv'])


if __name__ == '__main__':
    unittest.main()
