"""Local handshake and failure classification without provider access."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import agent_delegate as delegate

RELAY = ROOT / 'vendor/delegate-skills/skills/codex-delegate/scripts/relay.mjs'


class DiagnosticTests(unittest.TestCase):
    def test_classification_uses_observed_codes_and_keeps_uncertain_timeout(self):
        cases = [({'status': 'timeout'}, [], 124, 'timeout_uncertain'),
                 ({'status': 'codex_unavailable'}, [], 127, 'missing_prerequisite'),
                 ({}, [{'type': 'error', 'error': {'code': 'invalid_api_key'}}], 1, 'authentication_failure'),
                 ({}, [{'type': 'error', 'code': 'sandbox_violation'}], 1, 'permission_violation'),
                 ({'signal': 'SIGTERM'}, [], 0, 'transport_interruption'),
                 ({}, [{'type': 'error', 'message': 'something failed'}], 0, 'invalid_output')]
        for receipt, events, code, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(delegate.classify(receipt, events, code), expected)

    def test_missing_prerequisite_never_spawns(self):
        with patch.object(delegate.shutil, 'which', return_value=None), patch.object(delegate.subprocess, 'run') as run:
            result = delegate.compatibility(RELAY)
        run.assert_not_called()
        self.assertEqual(result['outcome'], 'missing_prerequisite')
        self.assertEqual(result['authentication'], 'unverified')
        self.assertFalse(result['compatible'])

    @unittest.skipUnless(shutil.which('node'), 'Node required for real local protocol')
    def test_handshake_never_reads_brief_credentials_or_spawns_codex(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            # No provider binaries, no home, and stdin closed: declaration still works.
            completed = subprocess.run([shutil.which('node'), str(RELAY), '--handshake'],
                cwd=root, env={'PATH': str(root), 'CODEX_HOME': str(root / 'absent')},
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)['schema'], 'kh.delegate.handshake.v1')
            self.assertEqual(list(root.iterdir()), [])

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_unknown_protocol_rejected_before_model_dispatch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            relay = root / 'relay.mjs'
            relay.write_text('console.log(JSON.stringify({schema:"future.v9"}));')
            request = delegate.DelegateRequest(prompt='Task', system_prompt='Rules', model='fixture',
                session_id='session', session_dir=str(root / 'artifacts'), raw_output_path=str(root / 'events.jsonl'),
                cwd=str(root), baseline='hash', allowed_paths=[], evidence_requirements=['report'],
                output_contract={'type': 'object'}, task_context='Task', relay_path=str(relay), run_id='run', phase_id='phase')
            original = delegate.shutil.which
            def binaries(name):
                return original('node') if name == 'node' else '/fixture/' + name
            with patch.object(delegate.shutil, 'which', side_effect=binaries):
                spawned = []
                result = delegate.run(request, on_spawn=spawned.append)
            self.assertFalse(result.terminal_success)
            self.assertEqual(spawned, [])
            normalized = json.loads(next((root / 'artifacts').glob('*/normalized.json')).read_text())
            self.assertEqual(normalized['outcome'], 'unsupported_protocol')
            self.assertEqual(normalized['dispatch_id'], request.dispatch_id)
            self.assertEqual(normalized['run_id'], 'run')
            self.assertFalse(normalized['retry_safe'])
            self.assertFalse((root / 'events.jsonl').exists())

    def test_handshake_failure_is_unknown_not_compatible(self):
        with patch.object(delegate.shutil, 'which', return_value='/local/tool'):
            for response in ('{}', 'not-json', '{"schema":"kh.delegate.handshake.v1"}'):
                with self.subTest(response=response), patch.object(delegate.subprocess, 'run',
                        return_value=subprocess.CompletedProcess([], 0, response, '')):
                    self.assertFalse(delegate.compatibility(RELAY)['compatible'])
            with patch.object(delegate.subprocess, 'run', side_effect=subprocess.TimeoutExpired('node', 5)):
                self.assertFalse(delegate.compatibility(RELAY)['compatible'])

    def test_vendor_provenance_hash_matches_modified_relay(self):
        provenance = json.loads((ROOT / 'vendor/delegate-skills/PROVENANCE.json').read_text())
        self.assertEqual(provenance['adapted_sha256']['skills/codex-delegate/scripts/relay.mjs'],
                         hashlib.sha256(RELAY.read_bytes()).hexdigest())
        self.assertEqual(provenance['license'], 'MIT')


if __name__ == '__main__':
    unittest.main()
