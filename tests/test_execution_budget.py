"""Global controller limits, restart protection and actual agent dispatch accounting."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import agents, execution_budget as budget
from adw_modules.data_types import KHConfig, AgentConfig, PromptEngineering, AgentCall, GenericOutput, Phase, PhaseParams, PiResult
from adw_modules.execution_policy import resolve_execution_policy, resolve_bootstrap_policy, validate_light_paths


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = SimpleNamespace(session_dir=Path(self.temp.name))
        # Exercise accounting mechanics with explicit fixed limits.
        self.cfg = KHConfig(execution={'max_output_corrections': 2, 'deadline_seconds': 900})
        self.policy = resolve_execution_policy(self.cfg, {}, 'light')

    def test_current_defaults_leave_room_for_slow_roles_and_one_correction_per_phase(self):
        cfg = KHConfig()
        full, light = resolve_execution_policy(cfg, {}, 'full'), resolve_execution_policy(cfg, {}, 'light')
        self.assertEqual((full.max_model_sends, full.deadline_seconds), (34, 10800))
        self.assertEqual((light.max_model_sends, light.deadline_seconds), (14, 5400))
        self.assertEqual(resolve_bootstrap_policy(cfg, {}).deadline_seconds, 5400)
        self.assertEqual(full.max_output_corrections, 6)
        self.assertEqual(AgentConfig(name='planner', prompt_engineering=PromptEngineering(system='s', user='u')).timeout_seconds, 1800)

    def test_defaults_and_preserved_lower_limits(self):
        self.assertEqual((self.policy.max_model_sends, self.policy.max_plan_repairs, self.policy.max_repairs), (10,1,1))
        self.assertEqual(resolve_execution_policy(self.cfg, {}, 'full').max_model_sends, 30)
        lower = resolve_execution_policy(self.cfg, {'max_plan_repairs':0,'max_repairs':0}, 'light')
        self.assertEqual((lower.max_model_sends,lower.max_plan_repairs,lower.max_repairs), (6,0,0))
        self.cfg.review.panel=['correctness','security']
        self.assertEqual(resolve_execution_policy(self.cfg, {}, 'light'), self.policy)
        self.assertEqual(resolve_execution_policy(self.cfg, {}, 'full').max_model_sends, 30)

    def test_strict_settings(self):
        for value in (True, '10', 0, 1001):
            with self.assertRaises(ValueError):
                KHConfig(execution={'max_model_sends':value})
        with self.assertRaises(ValueError):
            KHConfig(execution={'unknown':True})

    def test_retired_panel_does_not_change_execution_limits(self):
        cfg=KHConfig()
        full=resolve_execution_policy(cfg, {}, 'full')
        light=resolve_execution_policy(cfg, {}, 'light')
        cfg.review.panel=['scope','correctness','unknown-legacy-lens']
        self.assertEqual(full,resolve_execution_policy(cfg, {}, 'full'))
        self.assertEqual(light,resolve_execution_policy(cfg, {}, 'light'))

    def test_global_corrections_and_shared_views(self):
        budget.start_budget(self.run, self.policy)
        child = copy.copy(self.run)
        budget.reserve_model_send(self.run)
        budget.reserve_model_send(child, 'correction')
        budget.reserve_model_send(self.run, 'correction')
        with self.assertRaisesRegex(budget.BudgetExceeded, 'OUTPUT_CORRECTION'):
            budget.reserve_model_send(child, 'correction')
        self.assertEqual(budget.summary(self.run)['model_sends'], 3)
        saved = json.loads(self.run.execution_budget.path.read_text())
        self.assertEqual(saved['output_corrections'], 2)
        self.assertIn('OUTPUT_CORRECTION',saved['stop_reason'])

    def test_aggregate_limit_and_review_reservation(self):
        policy=self.policy.model_copy(update={'max_model_sends':4})
        budget.start_budget(self.run, policy)
        for _ in range(3): budget.reserve_model_send(self.run)
        with self.assertRaisesRegex(budget.BudgetExceeded, 'insufficient sends'):
            budget.ensure_remaining_sends(self.run,2)
        self.assertEqual(budget.summary(self.run)['model_sends'],3)

    def test_default_light_total_never_exceeds_ten(self):
        budget.start_budget(self.run,self.policy)
        for _ in range(8): budget.reserve_model_send(self.run)
        for _ in range(2): budget.reserve_model_send(self.run,'correction')
        with self.assertRaisesRegex(budget.BudgetExceeded,'MODEL_SEND_BUDGET'):
            budget.reserve_model_send(self.run)
        self.assertEqual(budget.summary(self.run)['model_sends'],10)

    def test_deadline_uses_monotonic_and_never_resets(self):
        budget.start_budget(self.run, self.policy)
        self.run.execution_budget.started_monotonic -= 901
        with self.assertRaisesRegex(budget.BudgetExceeded, 'DEADLINE'):
            budget.remaining_timeout(self.run, 300)
        fresh=SimpleNamespace(session_dir=self.run.session_dir)
        with self.assertRaisesRegex(budget.BudgetExceeded, 'RUN_ALREADY_STARTED'):
            budget.start_budget(fresh,self.policy)

    def test_bootstrap_accounting_is_separate_and_legacy_noop(self):
        budget.reserve_model_send(self.run)
        self.assertEqual(budget.remaining_timeout(self.run,60),60)
        budget.start_budget(self.run, resolve_bootstrap_policy(self.cfg,{}), stage='bootstrap')
        budget.reserve_model_send(self.run)
        bootstrap_path=self.run.execution_budget.path
        budget.start_budget(self.run,self.policy)
        self.assertEqual(budget.summary(self.run)['model_sends'],0)
        self.assertEqual(json.loads(bootstrap_path.read_text())['model_sends'],1)

    def test_repairs_share_code_allowance(self):
        budget.start_budget(self.run,self.policy)
        budget.record_repair(self.run,'code')
        with self.assertRaisesRegex(budget.BudgetExceeded,'CODE_REPAIR'):
            budget.record_repair(self.run,'code')
        self.assertEqual(budget.summary(self.run)['code_repairs'],1)

    def test_light_scope_and_owner_risk_paths(self):
        validate_light_paths(self.cfg,self.policy,['src/app.py'])
        self.cfg.execution.light_risk_paths=['db/migrations/**']
        for paths in (['src/'],['**'], ['../app'],[],['db/migrations/1.sql'],[f'src/{n}.py' for n in range(9)]):
            with self.assertRaisesRegex(ValueError,'REQUIRES_FULL_MODE'):
                validate_light_paths(self.cfg,self.policy,paths)

    def test_actual_dispatch_corrections_global_across_phases(self):
        run=self.run
        run.cfg=KHConfig(agents=[AgentConfig(name='planner', coding_agent='claude_code', timeout_seconds=300,
            prompt_engineering=PromptEngineering(system='system',user='user'))])
        run.context_handoff_dir=Path(self.temp.name)/'evidence';run.context_handoff_dir.mkdir()
        run.adw_id='budget';run.repo_root=Path(self.temp.name)
        run.agent_map={};run.save_agent_map=MagicMock();run.tracer=MagicMock();run.console=MagicMock();run.add_usage=MagicMock()
        run.required_requirements=[]
        budget.start_budget(run,self.policy)
        phase=Phase(phase_id='one',adw_id='budget',seq=1,params=PhaseParams(name='planner',kind='agent',owner='planner',description='Check bounded model dispatch'))
        call=AgentCall(output_type=GenericOutput,prompt='task')
        malformed=PiResult(text='invalid',returncode=0,terminal_success=True)
        with patch.object(agents.prompts,'render',return_value='task'), patch.object(agents.agent_cc,'run',return_value=malformed) as transport, patch.object(agents.permissions,'snapshot',return_value={}), patch.object(agents.permissions,'enforce',return_value=[]), patch.object(agents,'_persist_envelope'):
            with self.assertRaisesRegex(RuntimeError,'never produced valid'):
                agents._execute(run,phase,call)
            phase.phase_id='two'
            with self.assertRaisesRegex(budget.BudgetExceeded,'OUTPUT_CORRECTION'):
                agents._execute(run,phase,call)
            self.assertEqual(transport.call_count,4)
            self.assertEqual(budget.summary(run)['model_sends'],4)
            self.assertLessEqual(transport.call_args.args[0].timeout_seconds,300)


if __name__=='__main__': unittest.main()
