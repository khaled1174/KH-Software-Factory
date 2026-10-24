"""Regression evidence for KH audit findings; no real model is called."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import agents, context_guard, permissions, gates, quality, agent_cc, workspace
from adw_modules.data_types import (AgentConfig, PromptEngineering, ConfigDefaults, KHConfig,
    ObservabilityConfig, AgentCall, BuildOutput, PlanOutput, ReviewOutput, ReviewFinding,
    CCRequest, PhaseParams, QualityCheckSpec, QualityResult, GenericOutput, ScopeFinding)
from adw_modules.runner import Run
from adw_modules.tracer import Tracer


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, text=True,
                          capture_output=True).stdout.rstrip('\n')


class CoreTests(unittest.TestCase):
    def test_retired_scout_configuration_keeps_strict_boolean_compatibility(self):
        from pydantic import ValidationError
        self.assertFalse(KHConfig().execution.jev_scout.enabled)
        self.assertTrue(KHConfig(execution={'jev_scout': {'enabled': True}}).execution.jev_scout.enabled)
        with self.assertRaises(ValidationError):
            KHConfig(execution={'jev_scout': {'enabled': 'yes'}})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'project with spaces'
        self.root.mkdir()
        self.previous = Path.cwd()
        os.chdir(self.root)
        git(self.root, 'init', '-q')
        git(self.root, 'config', 'user.name', 'KH Test')
        git(self.root, 'config', 'user.email', 'test@example.invalid')
        (self.root / 'source.py').write_text('value = 1\n')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-qm', 'baseline')
        prompt = self.root / 'prompt.md'
        prompt.write_text('{{prompt}}\n{{context_handoff_dir}}\n{{previous_envelope}}')
        self.agent = AgentConfig(name='builder', coding_agent='claude_code', model='sonnet',
            writes=['source.py'], prompt_engineering=PromptEngineering(system=str(prompt),user=str(prompt)))
        self.cfg = KHConfig(defaults=ConfigDefaults(data_dir='adws/adw_data'),
            observability=ObservabilityConfig(db='adws/adw_data/kh.db'), agents=[self.agent])
        self.tracer = Tracer(self.cfg.observability.db, 'adws/adw_data/sessions/probe/events.jsonl')
        self.run = Run(self.cfg, 'probe', self.tracer, 'test')
        self.tracer.session_start('probe', 'test')
        self.run.required_requirements = ['implement value 2']

    def tearDown(self):
        self.tracer.conn.close()
        os.chdir(self.previous)
        self.temp.cleanup()

    def call(self, responder, output=BuildOutput, name='build'):
        with patch.object(agent_cc, 'run', side_effect=responder):
            with self.run.phase(PhaseParams(name=name, kind='agent', owner='builder',
                  description='Exercise isolated worker result promotion', retries=0)) as ph:
                return ph.call(AgentCall(output_type=output, prompt='implement value 2'))

    def successful_worker(self, request, **_):
        (Path(request.cwd) / 'source.py').write_text('value = 2\n')
        return agent_cc.CCRunResult(terminal_success=True,
            text=json.dumps({'status':'success','summary':'Changed value','changed_files':['source.py']}))

    def test_prior_evidence_symlink_rejected_before_dispatch(self):
        handoff = Path(self.run.context_handoff_dir)
        handoff.mkdir(parents=True, exist_ok=True)
        (handoff / 'outside').symlink_to(self.root.parent, target_is_directory=True)
        with patch.object(agent_cc, 'run') as transport:
            with self.assertRaises(permissions.PermissionBreach):
                with self.run.phase(PhaseParams(name='build', kind='agent', owner='builder',
                    description='Reject escaping prior evidence', retries=0)) as ph:
                    ph.call(AgentCall(output_type=BuildOutput, prompt='implement value 2'))
            transport.assert_not_called()

    def test_allowed_change_promoted_and_owner_draft_preserved(self):
        (self.root/'owner draft.txt').write_text('owner')
        before_head=git(self.root,'rev-parse','HEAD')
        result=self.call(self.successful_worker)
        self.assertEqual(result.changed_files,['source.py'])
        self.assertEqual((self.root/'source.py').read_text(),'value = 2\n')
        self.assertEqual((self.root/'owner draft.txt').read_text(),'owner')
        self.assertEqual(git(self.root,'rev-parse','HEAD'),before_head)

    def test_parse_failure_discards_all_worker_changes(self):
        def invalid(request, **_):
            (Path(request.cwd)/'source.py').write_text('bad')
            return agent_cc.CCRunResult(terminal_success=True,text='invalid JSON')
        with self.assertRaises(RuntimeError):self.call(invalid)
        self.assertEqual((self.root/'source.py').read_text(),'value = 1\n')
        attempts=list(self.run.session_dir.rglob('attempt-*/user.md'))
        self.assertEqual(len(attempts),3)

    def test_transport_failure_cannot_promote_success_claim(self):
        def invalid(request, **kwargs):
            result=self.successful_worker(request,**kwargs)
            result.returncode=1
            return result
        with self.assertRaises(RuntimeError):self.call(invalid)
        self.assertEqual((self.root/'source.py').read_text(),'value = 1\n')

    def test_unauthorized_write_rejected_even_when_transport_raises(self):
        def invalid(request, **_):
            (Path(request.cwd)/'owner.txt').write_text('bad')
            raise OSError('fixture transport failure')
        with self.assertRaises(permissions.PermissionBreach):self.call(invalid)
        self.assertFalse((self.root/'owner.txt').exists())

    def test_undeclared_change_blocks_promotion(self):
        def invalid(request,**kwargs):
            result=self.successful_worker(request,**kwargs)
            result.text=json.dumps({'status':'success','changed_files':[]})
            return result
        with self.assertRaises(agents.GateFailure):self.call(invalid)
        self.assertEqual((self.root/'source.py').read_text(),'value = 1\n')

    def test_deletion_claim_is_accepted(self):
        def remove(request,**_):
            (Path(request.cwd)/'source.py').unlink()
            return agent_cc.CCRunResult(terminal_success=True,text=json.dumps(
                {'status':'success','summary':'Removed obsolete file','changed_files':['source.py']}))
        self.call(remove)
        self.assertFalse((self.root/'source.py').exists())

    def test_owner_concurrent_edit_stops_import(self):
        def edit(request,**kwargs):
            result=self.successful_worker(request,**kwargs)
            (self.root/'source.py').write_text('owner intervened')
            return result
        with self.assertRaises(permissions.PermissionBreach):self.call(edit)
        self.assertEqual((self.root/'source.py').read_text(),'owner intervened')

    def test_worker_commit_does_not_change_owner_head(self):
        original=git(self.root,'rev-parse','HEAD')
        def edit(request,**kwargs):
            result=self.successful_worker(request,**kwargs)
            git(request.cwd,'config','user.name','Worker')
            git(request.cwd,'config','user.email','worker@example.invalid')
            git(request.cwd,'add','source.py')
            git(request.cwd,'commit','-qm','worker commit')
            return result
        with self.assertRaises(permissions.PermissionBreach):self.call(edit)
        self.assertEqual(git(self.root,'rev-parse','HEAD'),original)
        self.assertEqual((self.root/'source.py').read_text(),'value = 1\n')

    def test_same_size_untracked_and_ignored_edits_detected(self):
        (self.root/'.gitignore').write_text('ignored.txt\n')
        for name in ['source.py','untracked.txt','ignored.txt']:
            (self.root/name).write_text('old')
        before=permissions.snapshot(self.run)
        for name in ['source.py','untracked.txt','ignored.txt']:
            (self.root/name).write_text('new')
        touched=permissions.changed_paths(before,permissions.snapshot(self.run))
        self.assertTrue({'source.py','untracked.txt','ignored.txt'}<=set(touched))

    def test_direct_recovery_preserves_dirty_and_index_bytes(self):
        (self.root/'source.py').write_text('owner staged')
        git(self.root,'add','source.py')
        original_index=(self.root/'.git/index').read_bytes()
        before=permissions.snapshot(self.run)
        (self.root/'source.py').write_text('worker staged')
        git(self.root,'add','source.py')
        readonly=self.agent.model_copy(update={'writes':[]})
        with self.assertRaises(permissions.PermissionBreach):permissions.enforce(self.run,None,readonly,before)
        self.assertEqual((self.root/'source.py').read_text(),'owner staged')
        self.assertEqual((self.root/'.git/index').read_bytes(),original_index)

    def test_no_git_still_captures_files(self):
        child=self.root/'nonrepo';child.mkdir()
        run=NS(repo_root=child)
        before=permissions.snapshot(run)
        (child/'file.txt').write_text('change')
        self.assertIn('file.txt',permissions.changed_paths(before,permissions.snapshot(run)))

    def test_control_prompts_protected_even_explicit_allow(self):
        allowed=self.agent.model_copy(update={'writes':['**']})
        self.assertFalse(permissions.permitted('adws/adw_data/prompt_engineering/builder/system.md',allowed,self.cfg))
        self.assertFalse(permissions.permitted('adws/adw_data/sessions/other/evidence.md',allowed,self.cfg))

    def test_default_protected_runtime_files_reject_a_broad_writer(self):
        allowed = self.agent.model_copy(update={'writes': ['**']})
        defaults = KHConfig()
        self.assertTrue(permissions.permitted('src/app.py', allowed, defaults))
        for path in ('adws/adw_modules/x.py', 'adws/adw_start.py'):
            with self.subTest(path=path):
                self.assertFalse(permissions.permitted(path, allowed, defaults))

    def test_approval_rejects_blockers_and_out_of_scope_findings(self):
        self.run.scope_review_paths = ['source.py']
        approved = ReviewOutput(status='success', approved=True,
            findings=[ReviewFinding(requirement=self.run.required_requirements[0],
                                    met=True, evidence='source.py:1')],
            scope_findings=[ScopeFinding(path='source.py', within_scope=True, reason='Approved source path')])
        self.assertTrue(gates.verdict_consistent(approved, self.run).passed)
        blocked = approved.model_copy(update={'blocking': ['Fix the source defect']})
        report = gates.verdict_consistent(blocked, self.run)
        self.assertFalse(report.passed)
        self.assertTrue(any(check.item == 'approved vs blocking' and not check.ok for check in report.checks))
        outside = approved.model_copy(update={'scope_findings': [
            ScopeFinding(path='source.py', within_scope=False, reason='Outside the accepted scope')]})
        report = gates.verdict_consistent(outside, self.run)
        self.assertFalse(report.passed)
        self.assertTrue(any(check.item == 'scope verdict' and not check.ok for check in report.checks))

    def test_empty_proof_and_uncovered_review_fail(self):
        self.assertFalse(gates.artifacts_exist(PlanOutput(status='success'),self.run).passed)
        self.assertFalse(gates.verdict_consistent(ReviewOutput(status='success',approved=True),self.run).passed)
        partial=ReviewOutput(status='success',approved=True,findings=[ReviewFinding(requirement='other',met=True,evidence='file:1')])
        self.assertFalse(gates.verdict_consistent(partial,self.run).passed)

    def test_quality_missing_commands_fail_without_execution(self):
        with self.run.phase(PhaseParams(name='quality',kind='code',owner='quality',description='Check missing commands fail closed')):
            with patch.object(subprocess,'Popen',side_effect=AssertionError('must not execute placeholder')):
                result=quality.run_quality(self.run)
        self.assertFalse(result.passed)
        self.assertEqual({c.returncode for c in result.checks},{126})

    def test_quality_timeout_returns_evidence(self):
        with self.run.phase(PhaseParams(name='quality',kind='code',owner='quality',description='Capture a real timed out subprocess')):
            result=quality._run(QualityCheckSpec(name='timeout',area='backend',operation='build',
                argv=[sys.executable,'-c','import time;print("started",flush=True);time.sleep(20)'],timeout_seconds=1),self.run)
        self.assertEqual(result.returncode,124)
        self.assertIn('started',result.output_tail)
        self.assertTrue(Path(result.output_artifact).is_file())

    def test_legacy_auto_commit_setting_does_not_commit_promoted_source(self):
        (self.root/'owner.txt').write_text('owner')
        before = git(self.root, 'rev-parse', 'HEAD')
        self.cfg.defaults.auto_commit=True
        self.call(self.successful_worker)
        self.assertEqual((self.root/'source.py').read_text(), 'value = 2\n')
        self.assertEqual(git(self.root, 'diff', '--cached', '--name-only'), '')
        self.assertEqual(before, git(self.root, 'rev-parse', 'HEAD'))
        self.assertEqual((self.root/'owner.txt').read_text(), 'owner')

    def test_empty_tool_list_and_batched_events(self):
        req=CCRequest(prompt='x',system_prompt='x',model='sonnet',session_id='id',
                      session_dir='s',raw_output_path='o',tools=[])
        cmd=agent_cc._command(req,False)
        self.assertEqual(cmd[cmd.index('--tools')+1],'')
        self.assertNotIn('bypassPermissions',cmd)
        tracker=agent_cc.ToolCallTracker()
        tracker.observe({'type':'assistant','message':{'content':[
            {'type':'tool_use','id':'a','name':'Read','input':{}},
            {'type':'tool_use','id':'b','name':'Read','input':{}}]}})
        records=tracker.observe({'type':'user','message':{'content':[
            {'type':'tool_result','tool_use_id':'a','content':'one'},
            {'type':'tool_result','tool_use_id':'b','content':'two'}]}})
        self.assertEqual([r['tool_call_id'] for r in records],['a','b'])
        self.assertEqual(tracker._open,{})

    def _occupied(self, text, percent, window=100_000):
        return agent_cc.CCRunResult(terminal_success=True, text=text,
                                    context_tokens=int(window*percent/100), context_window=window)

    def test_context_guard_rotates_a_builder_onto_a_fresh_session(self):
        seen=[]
        def responder(request,**_):
            seen.append((request.session_id,request.prompt))
            (Path(request.cwd)/'source.py').write_text('value = 2\n')
            if len(seen)==1: return self._occupied('invalid JSON',80)
            return self._occupied(json.dumps({'status':'success','summary':'Changed value',
                                              'changed_files':['source.py']}),5)
        self.call(responder)
        self.assertNotEqual(seen[0][0],seen[1][0])     # the exhausted session is not rejoined
        self.assertIn('CONTEXT ROTATION',seen[1][1])
        self.assertIn('implement value 2',seen[1][1])  # the original task crosses the boundary
        # The brief states what the controller measured on disk, not what the agent claimed.
        self.assertIn('source.py',seen[1][1].split('FILES ALREADY CHANGED ON DISK')[1])

    def test_context_guard_refuses_rather_than_rotating_an_independent_reviewer(self):
        reviewer=AgentConfig(name='reviewer',coding_agent='claude_code',model='sonnet',writes=[],
            prompt_engineering=self.agent.prompt_engineering)
        self.cfg.agents.append(reviewer)
        calls=[]
        def responder(request,**_):
            calls.append(request.session_id)
            return self._occupied('invalid JSON',80)
        with patch.object(agent_cc,'run',side_effect=responder):
            with self.assertRaises(context_guard.ContextExhausted) as raised:
                with self.run.phase(PhaseParams(name='review',kind='agent',owner='reviewer',
                        description='Independent review must not resume from a summary',retries=0)) as ph:
                    ph.call(AgentCall(output_type=ReviewOutput,prompt='review value 2'))
        self.assertIn('SCOPE_TOO_LARGE_FOR_REVIEW',str(raised.exception))
        self.assertEqual(1,len(calls))                 # refused before the second dispatch

    def test_context_guard_stops_rotating_when_the_task_itself_does_not_fit(self):
        self.cfg.defaults.context_guard.max_rotations=1
        calls=[]
        def responder(request,**_):
            calls.append(request.session_id)
            (Path(request.cwd)/'source.py').write_text('value = 2\n')
            return self._occupied('invalid JSON',80)   # a fresh session is full too
        with patch.object(agent_cc,'run',side_effect=responder):
            with self.assertRaises(context_guard.ContextExhausted) as raised:
                self.call(responder)
        self.assertIn('CONTEXT_DOES_NOT_FIT',str(raised.exception))
        self.assertEqual(2,len(calls))                 # one rotation, then refusal

    def test_runtime_compaction_inside_one_session_is_recorded(self):
        seen=[]
        def responder(request,**_):
            seen.append(request.session_id)
            (Path(request.cwd)/'source.py').write_text('value = 2\n')
            if len(seen)==1: return self._occupied('invalid JSON',60)
            # Same session, window suddenly far emptier: the runtime compacted.
            return self._occupied(json.dumps({'status':'success','summary':'Changed value',
                                              'changed_files':['source.py']}),12)
        self.call(responder)
        self.assertEqual(seen[0],seen[1])              # no controller rotation happened
        events=[json.loads(l) for l in
                (self.root/'adws/adw_data/sessions/probe/events.jsonl').read_text().splitlines()]
        observed=[e for e in events if e['name']=='runtime_compaction_observed']
        self.assertEqual(1,len(observed))
        self.assertEqual(60_000,observed[0]['payload']['context_tokens_before'])
        self.assertEqual(12_000,observed[0]['payload']['context_tokens_after'])

    def test_context_guard_stays_out_when_the_window_is_undeclared(self):
        seen=[]
        def responder(request,**_):
            seen.append(request.session_id)
            (Path(request.cwd)/'source.py').write_text('value = 2\n')
            text=('invalid JSON' if len(seen)==1 else
                  json.dumps({'status':'success','summary':'Changed value','changed_files':['source.py']}))
            # A registry that declares no ceiling is unknown, not empty.
            return agent_cc.CCRunResult(terminal_success=True,text=text,
                                        context_tokens=9_000_000,context_window=0)
        self.call(responder)
        self.assertEqual(seen[0],seen[1])

    def test_env_allowlist_bounds_dotenv_keys_and_leaves_the_shell_alone(self):
        """ENV-01: a declared boundary withholds .env-sourced names only."""
        from adw_modules import utils
        self.addCleanup(utils.set_dotenv_allowlist,None)
        with patch.object(utils,'DOTENV_KEYS',frozenset({'PROVIDER_KEY','UNRELATED_TOKEN'})), \
                patch.dict(os.environ,{'PROVIDER_KEY':'k','UNRELATED_TOKEN':'t','OPERATOR_OWN':'o'}):
            self.assertEqual([],utils.set_dotenv_allowlist(None))
            self.assertEqual('t',utils.operator_env()['UNRELATED_TOKEN'])
            self.assertEqual(['UNRELATED_TOKEN'],utils.set_dotenv_allowlist(['PROVIDER_KEY']))
            env=utils.operator_env()
            self.assertEqual('k',env['PROVIDER_KEY'])
            self.assertNotIn('UNRELATED_TOKEN',env)
            # A name the operator exported themselves is never withheld, and
            # PATH survives, or every provider CLI and check would break.
            self.assertEqual('o',env['OPERATOR_OWN'])
            self.assertIn('PATH',env)
            utils.set_dotenv_allowlist([])
            self.assertNotIn('PROVIDER_KEY',utils.operator_env())
        with self.assertRaises(ValueError):
            ConfigDefaults(env_allowlist=['A=1'])

    def test_dotenv_files_never_reach_the_isolated_workspace(self):
        """Credentials cross through the process environment, not a copied file."""
        (self.root/'.env').write_text('PROVIDER_KEY=secret\n')
        (self.root/'.env.local').write_text('UNRELATED_TOKEN=secret\n')
        excluded=workspace.exclusions(self.run)
        self.assertIn('.env',excluded)
        self.assertIn('.env.local',excluded)
        snap=permissions.snapshot(NS(repo_root=self.root,cfg=self.cfg,snapshot_excludes=excluded))
        self.assertIn('source.py',snap)
        self.assertNotIn('.env',snap)
        self.assertNotIn('.env.local',snap)
        def inspect(request,**_):
            self.assertFalse((Path(request.cwd)/'.env').exists())
            self.assertFalse((Path(request.cwd)/'.env.local').exists())
            return self.successful_worker(request)
        self.call(inspect)
        self.assertEqual('PROVIDER_KEY=secret\n',(self.root/'.env').read_text())

    def test_hook_empty_or_comment_only_list_blocks_every_edit(self):
        """A protected-paths list with no effective entries fails closed."""
        project=self.root/'hook-fixture'
        (project/'.claude').mkdir(parents=True)
        (project/'open.txt').write_text('living\n')
        hook=ROOT/'.claude/hooks/protect-paths.sh'
        def run_hook(target):
            payload=json.dumps({'tool_input':{'file_path':target}})
            env={**os.environ,'CLAUDE_PROJECT_DIR':str(project)}
            return subprocess.run(['bash',str(hook)],input=payload,capture_output=True,
                text=True,env=env,cwd=project).returncode
        for content in ('','# only a comment\n'):
            (project/'.claude/protected-paths.txt').write_text(content)
            self.assertEqual(2,run_hook('open.txt'))


    def test_prompts_survive_multiple_phases(self):
        self.call(self.successful_worker,name='build1')
        def second(request,**_):
            (Path(request.cwd)/'source.py').write_text('value = 3\n')
            return agent_cc.CCRunResult(terminal_success=True,text=json.dumps({'status':'success','changed_files':['source.py']}))
        self.call(second,name='build2')
        self.assertEqual(len(list(self.run.session_dir.rglob('attempt-001/user.md'))),2)
        self.assertEqual(len(list(self.run.session_dir.rglob('envelope-*.json'))),2)


class DocumenterPromptTests(unittest.TestCase):
    setUp = CoreTests.setUp
    tearDown = CoreTests.tearDown

    def dispatch(self, guarded, *, user=None, prompt='Candidate brief sentinel'):
        from adw_modules import documentation
        from adw_modules.data_types import DocumentOutput
        bundled = ROOT / '.claude/skills/kh/templates/prompt_engineering/documenter'
        self.cfg.agents = [self.agent.model_copy(update={
            'name': 'documenter', 'writes': [],
            'prompt_engineering': PromptEngineering(system=str(bundled/'system.md'),
                                                    user=str(user or bundled/'user.md'))})]
        output = documentation.DocumentationProposalOutput if guarded else DocumentOutput
        payload = {'status': 'success', 'summary': 'Prompt transport fixture'}
        if guarded:
            payload.update(candidate_sha256='a'*64, documented_paths=[],
                           documents=[{'path': 'app_docs/fixture.md', 'content': 'Fixture'}])
        requests = []
        def responder(request, **_):
            requests.append(request)
            evidence = Path(request.system_prompt.split('Write evidence only under ', 1)[1].splitlines()[0])/'fixture.md'
            evidence.write_text('Prompt transport fixture\n')
            payload['artifacts'] = [str(evidence)]
            if not guarded:
                payload['document_path'] = str(evidence)
            return agent_cc.CCRunResult(terminal_success=True, text=json.dumps(payload))
        with patch.object(agent_cc, 'run', side_effect=responder):
            with self.run.phase(PhaseParams(name='document', kind='agent', owner='documenter',
                                           description='Inspect actual documenter dispatch', retries=0)) as ph:
                ph.call(AgentCall(output_type=output, prompt=prompt))
        self.assertEqual(len(requests), 1)
        return requests[0]

    def test_guarded_request_excludes_standalone_copy_and_report(self):
        request = self.dispatch(True)
        self.assertIn('Candidate brief sentinel', request.prompt)
        self.assertIn('DocumentationProposalOutput', request.system_prompt)
        self.assertNotIn('mkdir -p app_docs && cp', request.prompt)
        self.assertNotIn('ONLY valid JSON matching `DocumentOutput`', request.prompt)

    def test_standalone_request_keeps_copy_and_report(self):
        request = self.dispatch(False)
        self.assertIn('Candidate brief sentinel', request.prompt)
        self.assertIn('mkdir -p app_docs && cp', request.prompt)
        self.assertIn('ONLY valid JSON matching `DocumentOutput`', request.prompt)

    def test_unmarked_custom_template_preserves_marker_in_owner_input(self):
        custom = self.root/'custom-documenter.md'
        custom.write_text('{{prompt}}\nCustom template tail')
        text = 'Owner input\n## Standalone DocumentOutput\nOwner input tail'
        request = self.dispatch(True, user=custom, prompt=text)
        self.assertIn(text, request.prompt)
        self.assertIn('Custom template tail', request.prompt)


if __name__=='__main__':unittest.main()
