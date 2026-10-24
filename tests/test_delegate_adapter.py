"""No network/provider access: exercise the real pinned relay with a fake CLI."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import dataclasses
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude/skills/kh/templates/adws"))
from adw_modules.agent_delegate import DelegateRequest, _brief, _validate, normalize_result, run
from adw_modules import agent_delegate
from adw_modules.process_io import RoleTimeout
from tests import test_core as core_fixtures


class DelegateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="delegate fixture ")
        self.base = Path(self.tmp.name)
        self.request = DelegateRequest(
            prompt="Return the requested evidence", task_context="Review the source contract",
            system_prompt="Use explicit scope", model="fixture-model", session_id="controller-id",
            session_dir=str(self.base / "artifacts"), raw_output_path=str(self.base / "raw.jsonl"),
            cwd=str(self.base), baseline="head:abc snapshot:123", allowed_paths=["src/**"],
            evidence_requirements=["file references"], output_contract={"type": "object"},
        )
        self.receipt = {"schema": "delegate-relay.result.v1", "status": "completed", "exitCode": 0,
                        "workdir": str(self.base), "model": "fixture-model", "sandbox": "read-only",
                        "effort": "medium",
                        "finalMessage": '{"status":"success","summary":"fixture"}', "signal": None}
        self.events = [{"type": "turn.completed", "usage": {"input_tokens": 12, "cached_input_tokens": 3, "output_tokens": 4}}]

    def tearDown(self):
        self.tmp.cleanup()

    def test_completion_not_acceptance_and_usage(self):
        result = normalize_result(self.receipt, self.events, 0, self.request)
        self.assertTrue(result.terminal_success)
        self.assertFalse(result.reported_error)
        self.assertEqual(result.session_id, "controller-id")
        self.assertEqual(result.tokens, 16)
        self.assertEqual(result.usage.input_tokens, 9)

    def test_completion_receipt_cannot_hide_failures(self):
        for field, value in [("status", "timeout"), ("exitCode", 9), ("error", "provider error"),
                             ("signal", "SIGTERM"), ("finalMessage", ""), ("model", "wrong"),
                             ("effort", "low"), ("effort", None),
                             ("sandbox", "workspace-write"), ("workdir", "/other"), ("exitCode", False)]:
            with self.subTest(field=field, value=value):
                result = normalize_result({**self.receipt, field: value}, self.events, 0, self.request)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(result.terminal_success)
        self.assertFalse(normalize_result(self.receipt, self.events, 1, self.request).terminal_success)

    def test_terminal_event_required_and_errors_are_fatal(self):
        for events in ([], [{"type": "item.completed"}], self.events * 2,
                       [{"type": "error", "message": "failed"}] + self.events,
                       self.events + [{"type": "item.completed"}]):
            self.assertFalse(normalize_result(self.receipt, events, 0, self.request).terminal_success)

    def test_unmappable_permissions_fail_before_dispatch(self):
        for changes in ({"tools": []}, {"tools": ["read"]}, {"extensions": ["hook.ts"]},
                        {"sandbox": "danger-full-access"},
                        {"sandbox": "workspace-write", "sandbox_scope": ["src/**"]},
                        {"allowed_paths": ["../outside"]}, {"baseline": ""}, {"task_context": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                _validate(dataclasses.replace(self.request, **changes))
        request = dataclasses.replace(self.request, sandbox="workspace-write", sandbox_scope=["**"])
        _validate(request)
        brief = json.loads(_brief(request))
        self.assertEqual(brief["allowed_task_paths"], ["src/**"])
        self.assertEqual(brief["native_sandbox_scope"], ["**"])
        self.assertEqual(brief["requested_effort"], "medium")

    def test_unsupported_effort_is_rejected_before_spawn(self):
        for changes in ({"thinking": "none"}, {"thinking": "ultra"}, {"thinking": ""},
                        {"thinking": "HIGH"}, {"thinking": None},
                        {"thinking": "xhigh", "supported_thinking": ("low", "medium", "high")},
                        {"supported_thinking": ()}, {"supported_thinking": ("ultra",)},
                        {"supported_thinking": "medium"}):
            with self.subTest(changes=changes), patch("subprocess.Popen") as spawn:
                with self.assertRaises(ValueError):
                    run(dataclasses.replace(self.request, **changes))
                spawn.assert_not_called()

    def test_supported_effort_brief_and_receipt_agreement(self):
        for level in ("low", "medium", "high", "xhigh"):
            with self.subTest(level=level):
                request = dataclasses.replace(self.request, thinking=level, supported_thinking=(level,))
                _validate(request)
                self.assertEqual(json.loads(_brief(request))["requested_effort"], level)
                result = normalize_result({**self.receipt, "effort": level}, self.events, 0, request)
                self.assertTrue(result.terminal_success, result.reported_error)
        receipt = {k: v for k, v in self.receipt.items() if k != "effort"}
        self.assertFalse(normalize_result(receipt, self.events, 0, self.request).terminal_success)

    def _fake_cli(self):
        binary = self.base / "bin"
        binary.mkdir()
        executable = binary / "codex"
        executable.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys, time
if "--version" in sys.argv:
    print("codex-cli fixture-0")
    sys.exit(0)
pathlib.Path(os.environ["KH_TEST_ARGS"]).write_text(json.dumps(sys.argv[1:]))
if os.environ.get("KH_TEST_DISPATCHES"):
    with open(os.environ["KH_TEST_DISPATCHES"], "a") as record: record.write("dispatch\\n")
mode = os.environ.get("KH_TEST_MODE", "ok")
if mode == "timeout":
    print("stream disconnected", file=sys.stderr, flush=True)
    time.sleep(10)
final = pathlib.Path(sys.argv[sys.argv.index("-o")+1])
final.write_text('{"status":"success","summary":"fixture"}')
print(json.dumps({"type":"thread.started", "thread_id":"provider-thread"}))
if mode == "malformed":
    print("not-json")
if mode == "error":
    print(json.dumps({"type":"error","message":"failed"}))
if mode != "missing":
    print(json.dumps({"type":"turn.completed","usage":{"input_tokens":12,"cached_input_tokens":3,"output_tokens":4}}))
sys.exit(7 if mode == "nonzero" else 0)
''')
        executable.chmod(0o755)
        return {"PATH": str(binary) + os.pathsep + os.environ["PATH"],
                "KH_TEST_ARGS": str(self.base / "argv.json")}

    @unittest.skipUnless(shutil.which("node") and os.name == "posix", "Node and POSIX required")
    def test_real_relay_subprocess_and_failure_cases(self):
        env = self._fake_cli()
        spawned, exited = [], []
        with patch.dict(os.environ, env):
            result = run(self.request, on_spawn=spawned.append, on_exit=exited.append)
        self.assertEqual(result.returncode, 0, result.reported_error)
        self.assertEqual(spawned, exited)
        argv = json.loads((self.base / "argv.json").read_text())
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertIn('approval_policy="never"', argv)
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("sandbox_workspace_write.exclude_slash_tmp=true", argv)
        self.assertIn("sandbox_workspace_write.exclude_tmpdir_env_var=true", argv)
        self.assertIn("sandbox_workspace_write.network_access=false", argv)
        self.assertIn("model_reasoning_effort=medium", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        normalized = list((self.base / "artifacts").glob("*/normalized.json"))
        metadata = json.loads(normalized[0].read_text())
        self.assertFalse(metadata["accepted"])
        self.assertEqual(metadata["requested_effort"], "medium")
        self.assertEqual(metadata["effective_effort"], "medium")
        self.assertEqual(metadata["effort_verification"], "relay_receipt")
        for mode in ("malformed", "missing", "error", "nonzero", "timeout"):
            with self.subTest(mode=mode), patch.dict(os.environ, {**env, "KH_TEST_MODE": mode}):
                if mode == "timeout":
                    with self.assertRaises(RoleTimeout) as caught:
                        run(dataclasses.replace(self.request, timeout_seconds=1))
                    result = caught.exception.result
                else:
                    result = run(dataclasses.replace(self.request, timeout_seconds=1))
                self.assertFalse(result.terminal_success)
                self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(shutil.which("node") and os.name == "posix", "Node and POSIX required")
    def test_real_relay_forwards_nondefault_effort_to_codex(self):
        env = self._fake_cli()
        for level in ("low", "high", "xhigh"):
            with self.subTest(level=level), patch.dict(os.environ, env):
                events = []
                result = run(dataclasses.replace(self.request, thinking=level), on_event=events.append)
                self.assertTrue(result.terminal_success, result.reported_error)
                argv = json.loads((self.base / "argv.json").read_text())
                self.assertIn(f"model_reasoning_effort={level}", argv)
                metadata = next(e for e in events if e["type"] == "delegate_result")
                self.assertEqual(metadata["requested_effort"], level)
                self.assertEqual(metadata["effective_effort"], level)
                self.assertFalse(metadata["accepted"])

    def test_completion_after_deadline_raises_timeout_and_retains_receipt(self):
        with patch.object(agent_delegate.time, 'monotonic', return_value=0) as clock, \
             patch.object(agent_delegate, 'compatibility', return_value={'compatible': True}), \
             patch.object(agent_delegate.subprocess, 'Popen') as spawn:
            process = spawn.return_value
            process.poll.return_value = 0
            process.wait.side_effect = lambda timeout: setattr(clock, 'return_value', 2)
            with self.assertRaises(RoleTimeout) as caught:
                run(dataclasses.replace(self.request, timeout_seconds=1))
        self.assertIn('completed after its deadline', str(caught.exception))
        self.assertEqual(caught.exception.result.returncode, 124)
        receipt = json.loads(next((self.base / 'artifacts').glob('*/normalized.json')).read_text())
        self.assertEqual(receipt['outcome'], 'timeout_uncertain')
        self.assertFalse(receipt['retry_safe'])
        self.assertFalse(receipt['accepted'])


class DelegateTimeoutControllerTests(unittest.TestCase):
    setUp = core_fixtures.CoreTests.setUp
    tearDown = core_fixtures.CoreTests.tearDown

    @unittest.skipUnless(shutil.which('node') and os.name == 'posix', 'Node and POSIX required')
    def test_stream_disconnect_during_timeout_dispatches_once_without_retry(self):
        self.base = self.root.parent
        env = DelegateTests._fake_cli(self)
        dispatches = self.base / 'dispatches.txt'
        self.agent.coding_agent = 'delegate_codex'
        self.agent.model = 'fixture-model'
        self.agent.timeout_seconds = 1
        self.cfg.execution.transient_retry_delay_seconds = 1
        failure = None
        with patch.dict(os.environ, {**env, 'KH_TEST_MODE': 'timeout',
                                     'KH_TEST_DISPATCHES': str(dispatches)}):
            try:
                with self.run.phase(core_fixtures.PhaseParams(name='build', kind='agent', owner='builder',
                    description='A supervisor timeout must not be retried as a disconnect')) as phase:
                    phase.call(core_fixtures.AgentCall(output_type=core_fixtures.BuildOutput,
                                                     prompt='implement value 2'))
            except RuntimeError as error:
                failure = error
        names = [row[0] for row in self.tracer.conn.execute('SELECT name FROM events')]
        self.assertEqual(dispatches.read_text().splitlines(), ['dispatch'], names)
        self.assertIsInstance(failure, RoleTimeout)
        self.assertEqual(names.count('model_send'), 1)
        self.assertNotIn('transient_retry', names)
        self.assertEqual((self.root / 'source.py').read_text(), 'value = 1\n')
        receipts = list(self.run.session_dir.rglob('normalized.json'))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual(receipt['outcome'], 'timeout_uncertain')
        self.assertFalse(receipt['retry_safe'])


if __name__ == "__main__":
    unittest.main()
