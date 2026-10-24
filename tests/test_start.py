"""Full startup through actual CLI subprocesses, with a deterministic fake model."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil
import sqlite3
import unittest

ROOT = Path(__file__).resolve().parents[1]
ADWS = ROOT / '.claude/skills/kh/templates/adws'

FAKE = r'''#!/usr/bin/env python3
import json,os,re,sys
from pathlib import Path
system=sys.argv[sys.argv.index('--append-system-prompt')+1]
# Real CLI contract: the prompt follows `--`, or arrives on stdin when too large for argv.
user=sys.argv[sys.argv.index('--')+1] if '--' in sys.argv else sys.stdin.read()
def data(prefix):
    return json.JSONDecoder().raw_decode(user.split(prefix,1)[1].lstrip())[0]
role=re.search(r'ROLE (project_architect|plan_reviewer|planner|builder|reviewer)',system)[1]
output=Path(re.search(r'Write evidence only under (.+)',system)[1])
evidence=output/'evidence.md'
evidence.write_text('Actual fixture evidence for '+role)
result={'status':'success','summary':role,'artifacts':[str(evidence)]}
mode=os.environ.get('KH_FIXTURE_MODE','good')
# Provider failures as Claude Code reports them: the first N sends of one role
# end in an API error event and exit 1, before the role does any work.
if mode.split('-',1)[0] in ('outage','quota') and role==mode.split('-',1)[1]:
    counter=output/'fake-provider-failures';seen=int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(seen+1))
    if seen<int(os.environ.get('KH_FIXTURE_FAILURES','1')):
        text=('API Error: 503 Service Unavailable. This is a server-side issue, usually temporary' if mode.startswith('outage')
              else 'API Error: 429 Claude AI usage limit reached. Your limit resets at 5pm')
        print(json.dumps({'type':'result','subtype':'success','is_error':True,'api_error_status':503 if mode.startswith('outage') else 429,'result':text}))
        raise SystemExit(1)
# A gate correction carries only the findings: other roles answer it with their
# previous report unchanged, so a deliberate defect survives the correction.
if ('failed validation' in user and role!='project_architect' and (output/'fake-last.json').exists()
        and not (mode=='plan-incoherent' and role=='planner')):
    print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':(output/'fake-last.json').read_text()}))
    raise SystemExit(0)
if role=='project_architect':
    corrected='failed validation' in user
    brief=json.JSONDecoder().raw_decode(user)[0] if not corrected else {}
    if mode=='fake-context-evidence':result['artifacts']=['src/app.py']
    if mode=='architect-forgets-evidence' and not corrected:
        evidence.unlink();result['artifacts']=[]
    # A correction carries only the gate findings; keep the brief's digest from the first send.
    if corrected:brief=json.loads((output/'fake-brief.json').read_text())
    else:(output/'fake-brief.json').write_text(json.dumps({'baseline':{'digest':brief['baseline']['digest']}}))
    if mode=='owner-questions':result['unresolved_decisions']=['Which JWT signing key rotation policy applies?']
    result.update(baseline_digest=brief['baseline']['digest'],documents=[
        {'path':'CLAUDE.md','content':'# Project router\nRead [architecture](docs/agent-guide/architecture.md).\n'},
        {'path':'AGENTS.md','content':'# Agent router\nRead [architecture](docs/agent-guide/architecture.md).\n'},
        {'path':'docs/agent-guide/architecture.md','content':'# Architecture\nImplementation lives in src/app.py. Run the configured test command.\n'}])
elif role=='plan_reviewer':
    brief=json.JSONDecoder().raw_decode(user)[0]
    if 'proposal_digest' in brief:
        result.update(proposal_digest=brief['proposal_digest'],decision='approve')
    else:
        reject=mode=='plan-reject' or mode in ('plan-repair','plan-incoherent') and brief['version']==1
        result.update(light_eligible=True,light_eligibility_reason='Small isolated fixture change',plan_digest=brief['plan_digest'],version=brief['version'],decision='needs_owner' if mode=='plan-owner' else 'revise' if reject else 'approve',
            findings=[{'id':'F1','severity':'major','evidence':'Validation needs explicit behavior','correction':'State the expected value'}] if reject else [],
            resolved_finding_ids=[f['id'] for f in brief['previous_findings']] if not reject else [])
        if mode=='stale-plan-review':result['plan_digest']='0'*64
elif role=='planner':
    if mode=='plan-incoherent' and 'failed validation' in user:  # a correction carries only the findings
        brief=json.loads((output/'fake-planner-brief.json').read_text())
    else:
        brief=json.JSONDecoder().raw_decode(user)[0]
        if mode=='plan-incoherent':(output/'fake-planner-brief.json').write_text(json.dumps(brief))
    result['acceptance_criteria']=['The scoped behavior returns value 2']
    result['allowed_paths']={'plan-widens':['src/app.py','private.txt'],'plan-empty':[],'plan-glob-widens':['src/*.py'],
        'touch-test':['src/app.py','tests/test_app.py'],'dirs':['src/app.py','old/**','newdir']}.get(mode,['src/app.py'])
    if mode=='fake-evidence':result['artifacts']=['src/app.py']
    ids=list(brief['requirements'])
    result.update(steps=[{'id':'S1','action':'Implement value 2 and verify behavior'+(' explicitly' if brief['version']>1 else ''),
        'paths':result['allowed_paths'] or ['src/app.py'],'requirement_ids':ids,'validation':['Run configured test and assert value equals 2']}],
        requirement_coverage={i:['S1'] for i in ids},required_checks=brief['required_checks'])
    if mode=='plan-order':  # S1 uses FakeValue, which only S2 creates
        result['allowed_paths']=['src/app.py','src/FakeValue.py']
        result['steps']=[{'id':'S1','action':'Implement value 2 and test it with FakeValue','paths':['src/app.py'],
            'requirement_ids':ids,'validation':['Run configured test']},
            {'id':'S2','action':'Create the FakeValue test double','paths':['src/FakeValue.py'],
            'requirement_ids':ids,'validation':['Run configured test']}]
        result['requirement_coverage']={i:['S1','S2'] for i in ids}
    if mode=='plan-incoherent' and brief['version']>1 and 'failed validation' not in user:
        result['requirement_coverage']={i:['S1','S9'] for i in ids}  # names a step that does not exist
    if brief['version']>1:
        result['finding_responses']=[{'finding_id':'F1','disposition':'fixed','explanation':'Made behavior explicit','reference':'S1'}]

elif role=='builder':
    target=Path('src/app.py')
    previous=target.read_text()
    (output/'seen-before.json').write_text(json.dumps({'app':previous,'work_visible':Path('.kh/work').exists(),
        'resuming':'RESUMING THIS TASK' in user,'progress_asked':'progress.json' in user}))
    if mode=='crash-signal':
        # The controller itself is stopped (Ctrl-C, supervisor) while the builder works.
        import signal,time
        target.write_text('value = 2  # half done\n')
        os.kill(os.getppid(),signal.SIGTERM);time.sleep(30);raise SystemExit(1)
    if mode=='dirs':
        # Deletes a whole directory and adds an empty one, then stops.
        import shutil
        shutil.rmtree('old');Path('newdir').mkdir();target.write_text('value = 2  # half done\n')
        print('killed by the role timeout',file=sys.stderr);raise SystemExit(124)
    if mode in ('crash-build','crash-unauthorized'):
        # Stops mid-work like a role timeout: files written, no report.
        target.write_text('value = 2  # half done\n')
        if mode=='crash-unauthorized':Path('private.txt').write_text('outside scope')
        (output/'progress.json').write_text(json.dumps({'done':['S1'],'next':'wiring the value'}))
        print('killed by the role timeout',file=sys.stderr);raise SystemExit(124)
    text='value = 2\n'
    if mode=='semantic':text+='unrelated_feature = True\n'
    if mode in ('review-repair','forgetful-review') and 'unrelated_feature' not in previous:text+='unrelated_feature = True\n'
    if mode=='test-repair' and 'value = 3' not in previous:text='value = 3\n'
    if mode in ('floor-noqa','floor-forgetful'):text='value = 2  # type: ignore\n'
    if mode=='batch' and previous=='value = 2\n':text='value = 22\n'  # the batch's second task
    if previous!='value = 1\n' and 'acceptance_criteria' not in user.split('PLAN:',1)[1]:
        raise SystemExit('repair attempt lost the plan')
    target.write_text(text)
    if mode=='unauthorized':Path('private.txt').write_text('outside scope')
    if mode=='dir-kind':Path('private.txt').unlink();Path('private.txt').mkdir()
    if mode=='dir-mode':os.chmod('src',0o777)
    if mode=='touch-test':
        Path('tests').mkdir(exist_ok=True);Path('tests/test_app.py').write_text('def test_value(): pass\n')
    result['changed_files']=['src/app.py'] if previous!=text else []
    if mode=='touch-test':result['changed_files'].append('tests/test_app.py')
    if mode=='observations':
        result['observations']=[{'title':'Related follow-up','issue':'Fixture note','suggestion':'Review later',
            'principle':'Keep work bounded','skill':'kh-scope-guard','evidence':[str(evidence)]}]
else:
    paths=data('concrete reason: ')
    requirements=data('Controller acceptance requirements: ')
    diff=Path(re.search(r'this is the measurement\): (\S+)',user)[1])
    if not diff.is_file() or '+value = 2' not in diff.read_text() and '+value = 3' not in diff.read_text():
        raise SystemExit('controller diff missing or empty: '+str(diff))
    if 'tests/test_app.py' in paths and 'verification files' not in user:
        raise SystemExit('verification files were not flagged to the reviewer')
    if mode=='forgetful-review':
        requirements=[r for r in requirements if not r.startswith('Prior review blocker')]
    if mode=='floor-forgetful':
        requirements=[r for r in requirements if not r.startswith('Floor guard')]
    if mode=='owner-forgetful':
        requirements=[r for r in requirements if not r.startswith('Owner attention')]
    outside='unrelated_feature' in Path('src/app.py').read_text()
    if mode=='reviewer-write':Path('src/app.py').write_text('reviewer changed source\n')
    result.update(approved=not outside,blocking=['Remove unrelated feature'] if outside else [],
       findings=[{'requirement':r,'met':not outside,'evidence':'Inspected src/app.py and fixture check'} for r in requirements],
       scope_findings=[{'path':p,'within_scope':not outside,'reason':'Inspected task delta against non-goals'} for p in paths])
(output/'fake-last.json').write_text(json.dumps(result))
print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':json.dumps(result)}))
'''


def stamp_acceptance(text):
    """Bind an accepted fixture intent to its content, as the owner does.

    The digest covers the intent without its `Accepted digest` line, so for a
    text ending in a newline the line appended here hashes exactly `text`.
    """
    import hashlib
    return text + 'Accepted digest: ' + hashlib.sha256(text.encode()).hexdigest() + '\n'


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='kh start ')
        self.base=Path(self.temp.name); self.root=self.base/'owner project';self.root.mkdir()
        (self.root/'src').mkdir(); (self.root/'src/app.py').write_text('value = 1\n')
        (self.root/'private.txt').write_text('owner private\n')
        (self.root/'.env').write_text('FIXTURE_ONLY=unchanged\n')
        (self.root/'intents').mkdir()
        self.intent=self.root/'intents/change-value.md'
        self.intent.write_text(stamp_acceptance('''# Intent: Change value

Status: accepted
Author: Fixture originator
Date: 2026-09-26

## Task request

Implement value 2

## Problem

The fixture still returns value 1.

## Proposed outcome

The fixture returns value 2.

## Acceptance

Accepted by: Fixture owner
Accepted on: 2026-09-26
'''))
        for args in [['init','-q'],['add','.'],['-c','user.name=Fixture','-c','user.email=test@example.invalid','commit','-qm','baseline']]:
            subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)
        self.cli=self.base/'claude';self.cli.write_text(FAKE);self.cli.chmod(0o700)
        configdir=self.root/'adws/adw_kh_config';configdir.mkdir(parents=True)
        roles=[]
        for role in ['planner','builder','reviewer','plan_reviewer','project_architect']:
            system=configdir/f'{role}.md';system.write_text('ROLE '+role)
            user=configdir/'user.md';user.write_text('{{prompt}}\n{{previous_envelope}}\nPLAN:{{plan_envelope}}')
            roles.append({'name':role,'coding_agent':'claude_code','model':'sonnet','thinking':'high',
                          'writes':None if role=='builder' else [],
                          'prompt_engineering':{'system':str(system),'user':str(user)}})
        self.cfg={'defaults':{'quality_checks':{'test':[sys.executable,'-c',
                     "from pathlib import Path; assert 'value = 2' in Path('src/app.py').read_text()"]},
                     'required_quality_checks':['test']}, 'agents':roles,
                  'routing':{'default_profile':'current','profiles':{'current':{'max_repairs':1},'alternate':{'max_repairs':0}}}}
        for profile in self.cfg['routing']['profiles'].values():
            profile['roles']={role:[{'coding_agent':'claude_code','model':'sonnet','thinking':'high','supported_thinking':['high']}] for role in ['plan_reviewer','project_architect']}
        self.config=configdir/'kh.config.yaml';self.config.write_text(json.dumps(self.cfg))
        self.scope=self.base/'scope.json';self.scope.write_text(json.dumps({'original_request':'Implement value 2',
            'deliverables':['Update value'], 'non_goals':['Unrelated features'], 'allowed_paths':['src/app.py']}))
        self.env={**os.environ,'CLAUDE_CODE_PATH':str(self.cli),'PYTHONDONTWRITEBYTECODE':'1'}

    def tearDown(self):self.temp.cleanup()

    def invoke(self,mode='good',extra=(),scoped=True):
        scope=['--scope-file',str(self.scope)] if scoped else []
        return subprocess.run([sys.executable,str(ADWS/'adw_start.py'),*scope,
            '--adw-id','guarded',*extra,'--','Implement value 2'],cwd=self.root,
            env={**self.env,'KH_FIXTURE_MODE':mode},text=True,capture_output=True,timeout=60)

    def invoke_bootstrap(self, mode='good'):
        self.config.write_text(json.dumps(self.cfg))
        result=subprocess.run([sys.executable,'-B',str(ADWS/'adw_bootstrap.py'),
            '--config',str(self.config)],cwd=self.root,env={**self.env,'KH_FIXTURE_MODE':mode},
            capture_output=True,text=True,timeout=60)
        from adw_modules import project_context
        return {'ok':result.returncode==0,'error':result.stdout+result.stderr,
                'context_status':project_context.inspect_context(self.root)['status']}

    def unchanged(self):
        self.assertEqual((self.root/'src/app.py').read_text(),'value = 1\n')
        self.assertEqual((self.root/'private.txt').read_text(),'owner private\n')
        self.assertEqual((self.root/'.env').read_text(),'FIXTURE_ONLY=unchanged\n')


    def test_next_task_planner_receives_updated_configured_commands_without_bootstrap(self):
        first = self.invoke()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        (self.root / 'src/app.py').write_text('value = 1\n')
        self.cfg['defaults']['quality_checks']['test'][-1] += '\n# warm-command-v2'
        self.config.write_text(json.dumps(self.cfg))
        contract = json.loads(self.scope.read_text())
        contract['deliverables'].append('Verify the current configured command')
        self.scope.write_text(json.dumps(contract))  # Require a fresh task plan, not reuse of the first plan.
        code = FAKE.replace("    result['acceptance_criteria']=", """    guide=next((d for d in brief['context']['documents'] if d['path']=='docs/agent-guide/commands.md'),None)
    assert guide is not None and 'warm-command-v2' in guide['content'], 'Planner did not receive current configured commands'
    result['acceptance_criteria']=""")
        self.cli.write_text(code)
        second = self.invoke(extra=['--adw-id', 'warm-commands'])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        directory = self.root / 'adws/adw_data/sessions/warm-commands'
        self.assertFalse(list(directory.glob('context-attempt-*.json')))
        roles = json.loads((directory / 'agent_map.json').read_text())
        self.assertIn('planner', roles)
        self.assertNotIn('project_architect', roles)

    def test_missing_or_draft_intent_blocks_before_any_model_dispatch(self):
        accepted=self.intent.read_text()
        for content in (None, accepted.replace('Status: accepted','Status: draft')):
            with self.subTest(content=content):
                if content is None:self.intent.unlink()
                else:self.intent.write_text(content)
                result=self.invoke()
                self.assertNotEqual(result.returncode,0)
                self.assertIn('Intent',result.stderr)
                self.assertFalse((self.root/'adws/adw_data').exists())
                self.unchanged()

    def test_accepted_intent_for_another_request_cannot_start_task(self):
        self.intent.write_text(self.intent.read_text().replace('Implement value 2','Implement value 3'))
        result=self.invoke()
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Task request',result.stderr)
        self.assertFalse((self.root/'adws/adw_data').exists())

    def test_failed_check_discards_whole_task(self):
        self.cfg['defaults']['quality_checks']['test']=[sys.executable,'-c','raise SystemExit(7)']
        self.config.write_text(json.dumps(self.cfg))
        result=self.invoke()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.unchanged()
        self.assertNotIn('reviewer',json.loads((self.root/'adws/adw_data/sessions/guarded/agent_map.json').read_text()))
        self.assertTrue(list((self.root/'adws/adw_data/sessions/guarded/context_handoff').rglob('command.log')))

    def test_semantic_drift_inside_allowed_file_cannot_promote(self):
        result=self.invoke('semantic')
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.unchanged()

    def test_path_drift_rejected_despite_broad_builder_scope(self):
        result=self.invoke('unauthorized')
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('scope',result.stderr)
        self.unchanged()

    def test_unscoped_start_is_bounded_by_plan_paths(self):
        # No --scope-file: the plan supplies the boundary, so a write outside it is rejected.
        result=self.invoke('unauthorized',scoped=False)
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('scope',result.stderr)
        self.unchanged()
        run=self.root/'adws/adw_data/sessions/guarded'
        self.assertIsNone(json.loads((run/'scope.json').read_text())['allowed_paths'])
        self.assertEqual(json.loads((run/'effective_scope.json').read_text())['allowed_paths'],['src/app.py'])

    def test_unscoped_success_records_effective_scope(self):
        result=self.invoke(scoped=False)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.root/'src/app.py').read_text(),'value = 2\n')
        inspected=subprocess.run([sys.executable,str(ADWS/'adw_scope.py'),'guarded'],cwd=self.root,env=self.env,
                                 text=True,capture_output=True,timeout=10)
        self.assertEqual(json.loads(inspected.stdout)['effective_scope']['allowed_paths'],['src/app.py'])

    def test_plan_cannot_widen_owner_scope(self):
        result=self.invoke('plan-widens')
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('outside the owner scope',result.stdout+result.stderr)
        self.unchanged()
        self.assertFalse((self.root/'adws/adw_data/sessions/guarded/effective_scope.json').exists())

    def test_plan_glob_cannot_widen_owner_glob(self):
        # Owner allows one-character module names only; the plan asks for every module.
        contract=json.loads(self.scope.read_text());contract['allowed_paths']=['src/?.py']
        self.scope.write_text(json.dumps(contract))
        result=self.invoke('plan-glob-widens')
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('outside the owner scope',result.stdout+result.stderr)
        self.unchanged()
        run=self.root/'adws/adw_data/sessions/guarded'
        self.assertFalse((run/'effective_scope.json').exists())
        self.assertFalse((run/'builder').exists())      # rejected before any build

    def test_plan_without_paths_is_rejected(self):
        result=self.invoke('plan-empty',scoped=False)
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('allowed_paths',result.stdout+result.stderr)
        self.unchanged()

    def test_controller_diff_is_evidence_and_survives_promotion(self):
        result=self.invoke()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        run=self.root/'adws/adw_data/sessions/guarded'
        patches=list((run/'context_handoff').glob('controller_diff_*.patch'))
        self.assertEqual(len(patches),1)
        self.assertIn('+value = 2',patches[0].read_text())
        self.assertIn('-value = 1',patches[0].read_text())

    def test_changed_verification_files_become_a_review_requirement(self):
        contract=json.loads(self.scope.read_text());contract['allowed_paths']=['src/app.py','tests/**']
        self.scope.write_text(json.dumps(contract))
        result=self.invoke('touch-test')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertTrue((self.root/'tests/test_app.py').is_file())
        run=self.root/'adws/adw_data/sessions/guarded'
        prompts=[p.read_text() for p in (run/'reviewer/phases').rglob('user.md')]
        self.assertTrue(prompts and all('tests/test_app.py' in p and 'weaken' in p for p in prompts))
        envelopes=[json.loads(p.read_text()) for p in (run/'reviewer/phases').rglob('envelope-*.json')]
        self.assertTrue(any(any('verification files' in f['requirement'] for f in e['findings']) for e in envelopes))

    def test_reviewer_cannot_modify_source(self):
        result=self.invoke('reviewer-write')
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.unchanged()

    def test_final_test_repair_is_retested_before_promotion(self):
        result=self.invoke('test-repair')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.root/'src/app.py').read_text(),'value = 2\n')

    def test_review_correction_gets_fresh_checks_and_review(self):
        result=self.invoke('review-repair')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.root/'src/app.py').read_text(),'value = 2\n')
        run=self.root/'adws/adw_data/sessions/guarded'
        self.assertEqual(len(list((run/'reviewer/phases').iterdir())),2)

    def test_dry_run_even_with_save_flag_writes_nothing(self):
        result=self.invoke(extra=['--dry-run','--save-profile','--profile','alternate'])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['profile'],'alternate')
        self.assertFalse((self.root/'.kh').exists())
        self.assertFalse((self.root/'adws/adw_data').exists())
        self.unchanged()

    def test_save_profile_and_scope_inspection(self):
        result=self.invoke(extra=['--save-profile','--profile','alternate'])
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads((self.root/'.kh/routing.json').read_text()),{'profile':'alternate'})
        inspected=subprocess.run([sys.executable,str(ADWS/'adw_scope.py')],cwd=self.root,env=self.env,
                                 text=True,capture_output=True,timeout=10)
        self.assertEqual(inspected.returncode,0,inspected.stderr)
        self.assertEqual(json.loads(inspected.stdout)['run_id'],'guarded')

    def test_unknown_scope_fields_and_no_review_rejected(self):
        contract=json.loads(self.scope.read_text());contract['surprise']='ignored?'
        self.scope.write_text(json.dumps(contract));result=self.invoke()
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'adws/adw_data').exists())
        del contract['surprise'];contract['review_required']=False;self.scope.write_text(json.dumps(contract))
        result=self.invoke();self.assertNotEqual(result.returncode,0)
        self.unchanged()

    def test_unchanged_source_cannot_stand_in_for_plan_evidence(self):
        result=self.invoke('fake-evidence')
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('phase evidence',result.stderr)
        self.unchanged()

    def test_direct_api_enforces_readonly_reviewer(self):
        self.cfg['agents'][2]['writes']=None
        self.config.write_text(json.dumps(self.cfg))
        code=("import sys;sys.path.insert(0,sys.argv[1]);import adw_start;"
              "from adw_modules import agents,scope;"
              "adw_start.execute_task('Implement value 2',agents.load_config(),"
              "{'max_repairs':0},scope.load_contract(sys.argv[2]),'direct')")
        result=subprocess.run([sys.executable,'-c',code,str(ADWS),str(self.scope)],cwd=self.root,
            env={**self.env,'KH_FIXTURE_MODE':'reviewer-write'},text=True,capture_output=True,timeout=30)
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('prohibited paths',result.stderr)
        self.unchanged()

    def test_observations_record_only_after_durable_promotion(self):
        helper=self.root/'.claude/skills/task-observer/scripts/observer_store.py'
        helper.parent.mkdir(parents=True)
        shutil.copyfile(ROOT/'.claude/skills/task-observer/scripts/observer_store.py',helper)
        result=self.invoke('observations')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        with sqlite3.connect(self.root/'.kh-observer/observations.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0],1)
            raw=db.execute('SELECT provenance FROM occurrences').fetchone()[0]
        for item in json.loads(raw)['evidence']:
            self.assertTrue((self.root/item['path']).is_file())


if __name__=='__main__':unittest.main()
