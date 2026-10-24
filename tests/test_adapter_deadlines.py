"""Adapters charge local protocol/catalog work to the dispatch deadline."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import agent_delegate, process_io


class DeadlineTests(unittest.TestCase):



    def test_delegate_handshake_exhaustion_prevents_dispatch(self):
        with tempfile.TemporaryDirectory() as raw:
            clock = [100.0]
            request = agent_delegate.DelegateRequest(prompt='Task', system_prompt='Rules', model='fixture',
                session_id='s', session_dir=str(Path(raw) / 'artifacts'), raw_output_path=str(Path(raw) / 'out'),
                cwd=raw, baseline='hash', allowed_paths=[], evidence_requirements=['report'],
                output_contract={'type': 'object'}, task_context='Task', timeout_seconds=1)
            def handshake(relay, node, timeout):
                self.assertEqual(timeout, 1)
                clock[0] += 2
                return {'compatible': True}
            with patch.object(agent_delegate.time, 'monotonic', side_effect=lambda: clock[0]), \
                    patch.object(agent_delegate, 'compatibility', side_effect=handshake), \
                    patch.object(agent_delegate.subprocess, 'Popen') as dispatch:
                result = agent_delegate.run(request)
                self.assertFalse(result.terminal_success)
                dispatch.assert_not_called()

    def test_delegate_supervisor_uses_remaining_not_original_timeout(self):
        with tempfile.TemporaryDirectory() as raw:
            clock = [100.0]
            request = agent_delegate.DelegateRequest(prompt='Task', system_prompt='Rules', model='fixture',
                session_id='s', session_dir=str(Path(raw) / 'artifacts'), raw_output_path=str(Path(raw) / 'out'),
                cwd=raw, baseline='hash', allowed_paths=[], evidence_requirements=['report'],
                output_contract={'type': 'object'}, task_context='Task', timeout_seconds=10)
            def handshake(relay, node, timeout):
                clock[0] += 4
                return {'compatible': True}
            with patch.object(agent_delegate.time, 'monotonic', side_effect=lambda: clock[0]), \
                    patch.object(agent_delegate, 'compatibility', side_effect=handshake), \
                    patch.object(agent_delegate.subprocess, 'Popen') as dispatch:
                process = dispatch.return_value
                process.poll.return_value = 0
                agent_delegate.run(request)  # Missing fixture receipt is a transport failure.
                process.wait.assert_called_once_with(timeout=6)


if __name__ == '__main__':
    unittest.main()
