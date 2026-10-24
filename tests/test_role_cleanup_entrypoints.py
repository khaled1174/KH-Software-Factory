"""Entrypoint failure handlers preserve live role rows until cleanup is proven."""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
import adw_plan_review
import adw_start
from adw_modules import session
from adw_modules.scope import ScopeContract
from tests import test_core as core_fixtures


class EntrypointPendingCleanupTests(unittest.TestCase):
    def setUp(self):
        core_fixtures.CoreTests.setUp(self)
        self.cfg.agents = [self.agent.model_copy(deep=True, update={
            'name': role, 'tools': ['read'],
            'writes': ['source.py'] if role == 'builder' else []})
            for role in ('planner', 'plan_reviewer', 'builder', 'reviewer')]
        self.cfg.defaults.required_quality_checks = ['test']
        self.cfg.defaults.quality_checks = {'test': [sys.executable, '-c', 'print("fixture")']}
        self.run.session_dir = self.run.session_dir.resolve()
        self.run.context_handoff_dir = self.run.context_handoff_dir.resolve()
        self.run._lock = session.acquire_repo_lock(self.root)
        self.source = (self.root / 'source.py').read_bytes()
        self.head = core_fixtures.git(self.root, 'rev-parse', 'HEAD')
        self.prompt = 'Keep the existing source unchanged'
        self.decision = {'max_repairs': 0, 'max_plan_repairs': 0}

    def tearDown(self):
        if self.run._lock and not self.run._lock.closed:
            self.run._lock.close()
        core_fixtures.CoreTests.tearDown(self)

    def mark_pending_then_raise(self, failure):
        def fail_at_plan(run, *args, **kwargs):
            self.assertIs(run, self.run)
            # This process is genuinely alive. The fixture never signals it;
            # its row stands for an owned role whose stop was not confirmed.
            run.tracer.process_start(run.adw_id, 'agent', 'pending-fixture-role',
                                     os.getpid(), 'local entrypoint cleanup fixture')
            run.tracer.process_start(run.adw_id, 'adw', 'fixture-controller',
                                     os.getpid(), 'local entrypoint controller fixture')
            run.role_cleanup_pending = {'complete': False, 'surviving_pids': [os.getpid()]}
            raise failure
        return fail_at_plan

    def preflight(self, module):
        stack = ExitStack()
        self.addCleanup(stack.close)
        record = {'approved_sensitive': []}
        stack.enter_context(patch.object(module.intent, 'load', return_value=record))
        stack.enter_context(patch.object(module.intent, 'bind', return_value=record))
        stack.enter_context(patch.object(module.intent, 'attach', return_value={'digest': 'fixture-context'}))
        stack.enter_context(patch.object(module.project_context, 'inspect_context', return_value={'status': 'ready'}))
        stack.enter_context(patch.object(module.project_context, 'sync_commands'))
        stack.enter_context(patch.object(module.project_context, 'build_context_packet', return_value={}))
        stack.enter_context(patch.object(module.agents, 'validate'))
        stack.enter_context(patch.object(module.session, 'ensure', return_value=self.run))
        for name in ('note', 'session_finished', 'phase_started', 'phase_ended'):
            stack.enter_context(patch.object(self.run.console, name))
        return stack

    def assert_failed_session_preserves_live_role(self):
        status, ended = self.tracer.conn.execute(
            'SELECT status,ended_at FROM sessions WHERE adw_id=?', (self.run.adw_id,)).fetchone()
        self.assertEqual(status, 'fail')
        self.assertIsNotNone(ended)
        self.assertTrue(self.run._lock.closed, 'the real repository writer lease must be released')
        self.assertEqual((self.root / 'source.py').read_bytes(), self.source)
        self.assertEqual(core_fixtures.git(self.root, 'rev-parse', 'HEAD'), self.head)
        rows = dict(self.tracer.conn.execute(
            'SELECT kind,ended_at FROM processes WHERE adw_id=?', (self.run.adw_id,)))
        self.assertIsNotNone(rows['adw'], 'the controller exit itself must still be recorded')
        self.assertIsNone(rows['agent'], 'entrypoint finalization must not claim the still-live role exited')

    def test_guarded_task_exception_keeps_pending_role_row_open(self):
        failure = RuntimeError('fixture planning failed with cleanup pending')
        stack = self.preflight(adw_start)
        from adw_modules import runtime_compatibility
        stack.enter_context(patch.object(runtime_compatibility, 'structural', return_value={'blocked': []}))
        @contextmanager
        def stage(run, phase, builder):
            yield SimpleNamespace(view=run)
        stack.enter_context(patch.object(adw_start, 'durable_stage', stage))
        planner = stack.enter_context(patch.object(adw_start.planning, 'run_reviewed_plan',
            side_effect=self.mark_pending_then_raise(failure)))
        contract = ScopeContract(original_request=self.prompt, deliverables=['source remains unchanged'],
                                 allowed_paths=['source.py'], required_checks=['test'])
        with self.assertRaises(RuntimeError) as caught:
            adw_start.execute_task(self.prompt, self.cfg, self.decision, contract, adw_id=self.run.adw_id)
        self.assertIs(caught.exception, failure)
        planner.assert_called_once()
        self.assert_failed_session_preserves_live_role()

    def test_plan_review_system_exit_keeps_pending_role_row_open(self):
        failure = SystemExit(143)
        stack = self.preflight(adw_plan_review)
        stack.enter_context(patch.object(adw_plan_review.agents, 'load_config', return_value=self.cfg))
        stack.enter_context(patch.object(adw_plan_review.routing, 'resolve', return_value=(self.cfg, self.decision)))
        planner = stack.enter_context(patch.object(adw_plan_review.planning, 'run_reviewed_plan',
            side_effect=self.mark_pending_then_raise(failure)))
        with self.assertRaises(SystemExit) as caught:
            adw_plan_review.main(self.prompt, adw_id=self.run.adw_id)
        self.assertIs(caught.exception, failure)
        self.assertEqual(caught.exception.code, 143)
        planner.assert_called_once()
        self.assert_failed_session_preserves_live_role()


if __name__ == '__main__':
    unittest.main()
