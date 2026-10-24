"""Count real fake-provider dispatches for the same two-ticket workload.

Standalone evidence collection (no live model calls):
  KH_NEXT_BENCHMARK_ROOT=/path/to/archive python3 tests/test_next_call_counts.py --benchmark result.json
Use --benchmark-contexts for the retained cold/warm sequential workload.
Omit the environment variable to measure this checkout. Counts include every
provider invocation, including corrections, and are recorded outside the project root.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import test_start as fixture

RUNTIME_ROOT = Path(os.environ.get('KH_NEXT_BENCHMARK_ROOT', fixture.ROOT)).resolve()
ADWS = RUNTIME_ROOT / '.claude/skills/kh/templates/adws'
sys.path.insert(0, str(ADWS))
from adw_modules import project, project_context
if os.environ.get('KH_NEXT_BENCHMARK_ROOT'):
    for module in (project, project_context):
        if not Path(module.__file__).resolve().is_relative_to(ADWS):
            raise RuntimeError('Baseline benchmark requires a fresh process; another runtime is already imported')


class NextCallCountTests(unittest.TestCase):
    setUp = fixture.StartupTests.setUp
    tearDown = fixture.StartupTests.tearDown
    def measure(self, *, documentation=False, warm_context=False,
                scoped_guides=False, expected_context_status='ready'):
        (self.root / 'src/other.py').write_text('value = 1\n')
        (self.root / 'intents/other.md').write_text(fixture.stamp_acceptance(
            self.intent.read_text().split('Accepted digest:')[0].replace(
                'Implement value 2', 'Implement other value 2')))
        code = fixture.FAKE.replace("'src/app.py'", 'target_name')
        if documentation:
            code = code.replace('planner|builder|reviewer)', 'planner|builder|reviewer|documenter)')
            code = code.replace("elif role=='builder':\n", """
    result['documentation']={'decision':'required','reason':'Document each changed value and its supported behavior','paths':doc_paths}
    result['allowed_paths']+=doc_paths
elif role=='documenter':
    brief=json.JSONDecoder().raw_decode(user)[0]
    documents=[{'path':p,'content':'The documented value is now value = 2. Run the configured test.\\n',
        'purpose':'change_record' if p.startswith('app_docs/') else 'guide',
        'status':'historical' if p.startswith('app_docs/') else 'active',
        'applies_to':[target_name],'topics':['value']} for p in brief['documentation']['paths']]
    result.update(candidate_sha256=brief['candidate_sha256'],documented_paths=brief['changed_paths'],documents=documents)
elif role=='builder':
""")
        code = code.replace(r'this is the measurement\): (\S+)',
                            r'this is the measurement\): ([^\n]+)')
        code = code.replace('def data(prefix):',
            "target_name = 'src/other.py' if 'Implement other value 2' in user else 'src/app.py'\n"
            "doc_paths = ['app_docs/'+Path(target_name).stem+'.md','docs/agent-guide/'+Path(target_name).stem+'.md']\ndef data(prefix):")
        if scoped_guides:
            code = code.replace("elif role=='plan_reviewer':", """
    result['documents'] += [{'path':'docs/agent-guide/'+Path(path).stem+'.md',
        'content':'Scoped behavior: '+Path(path).read_text(), 'applies_to':[path],
        'topics':[Path(path).stem]} for path in ('src/app.py','src/other.py')]
elif role=='plan_reviewer':""")
            code = code.replace("'topics':['value']", "'topics':[Path(target_name).stem]")
        # O_APPEND + one os.write per event gives an independent process-safe
        # count. The path lies outside the project and isolated worker copies.
        code = code.replace("output=Path(re.search", """counter=os.environ.get('KH_BENCHMARK_CALL_LOG')
if counter:
    descriptor=os.open(counter,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    try:os.write(descriptor,(json.dumps({'role':role,'target':target_name,'cwd':os.getcwd(),
        'phase':Path(re.search(r'Write evidence only under (.+)',system)[1]).parent.name})+'\\n').encode())
    finally:os.close(descriptor)
output=Path(re.search""")
        self.cli.write_text(code)
        if documentation:
            self.cfg['execution'] = {'documentation': {'enabled': True}, 'recovery_rounds': 0}
            role = dict(next(a for a in self.cfg['agents'] if a['name'] == 'reviewer'))
            role['name'] = 'documenter'
            role['writes'] = ['**']
            system = self.base / 'documenter-system.md'
            system.write_text('ROLE documenter')
            role['prompt_engineering'] = {**role['prompt_engineering'], 'system': str(system)}
            self.cfg['agents'].append(role)
            for profile in self.cfg['routing']['profiles'].values():
                profile['roles']['documenter'] = [{'coding_agent': 'claude_code', 'model': 'sonnet',
                    'thinking': 'high', 'supported_thinking': ['high']}]
        self.cfg['defaults']['quality_checks']['test'] = [sys.executable, '-c',
            "import json; from pathlib import Path; values = {str(p): p.read_text().strip() for p in Path('src').glob('*.py')}; "
            "assert all(value in ('value = 1', 'value = 2') for value in values.values()); "
            "print('BENCHMARK_SOURCE:' + json.dumps(values, sort_keys=True))"]
        self.config.write_text(json.dumps(self.cfg))
        plan = {'schema_version': 1, 'project': {'id': 'app', 'name': 'Two independent changes'},
            'milestones': [{'id': 'M1', 'title': 'Feature'}], 'tasks': [
                {'id': ident, 'milestone_id': 'M1', 'title': ident, 'request': request,
                 'deliverables': ['The scoped value becomes 2'], 'allowed_paths': [path], 'required_checks': ['test']}
                for ident, request, path in [('T1', 'Implement value 2', 'src/app.py'),
                                              ('T2', 'Implement other value 2', 'src/other.py')]]}
        if documentation:
            for task in plan['tasks']:
                stem = Path(task['allowed_paths'][0]).stem
                task['allowed_paths'] += [f'app_docs/{stem}.md', f'docs/agent-guide/{stem}.md']
        project.init_project(self.root, plan)
        log = self.base / 'provider-calls.jsonl'
        preparation = {'calls': 0, 'by_role': {}, 'events': [], 'receipt': None}
        if warm_context:
            prep_log = self.base / 'context-preparation-calls.jsonl'
            prepared = subprocess.run([sys.executable, '-B', str(ADWS / 'adw_bootstrap.py')],
                cwd=self.root, env={**self.env, 'KH_BENCHMARK_CALL_LOG': str(prep_log)},
                capture_output=True, text=True, timeout=60)
            self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
            prep_events = [json.loads(line) for line in prep_log.read_text().splitlines()]
            prep_receipts = list(self.root.rglob('context-approval.json'))
            self.assertEqual(len(prep_receipts), 1)
            prep_receipt = json.loads(prep_receipts[0].read_text())
            self.assertEqual(prep_receipt['status'], 'ready')
            self.assertEqual(Counter(e['role'] for e in prep_events),
                {'project_architect': 1, 'plan_reviewer': 1})
            preparation = {'calls': len(prep_events),
                'by_role': dict(Counter(e['role'] for e in prep_events)),
                'events': prep_events, 'receipt': prep_receipt}
        context_before = project_context.inspect_context(self.root)
        self.assertEqual(context_before['status'], 'ready' if warm_context else 'missing')
        started = time.monotonic()
        result = subprocess.run([sys.executable, '-B', str(ADWS / 'adw_project.py'),
            'run-continuous'], cwd=self.root,
            env={**self.env, 'KH_BENCHMARK_CALL_LOG': str(log)}, capture_output=True, text=True, timeout=180)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stdout[-5000:] + result.stderr[-5000:])
        snapshot = project.status(self.root)
        self.assertEqual(snapshot['totals']['completed'], 2)
        self.assertEqual(snapshot['totals']['completion_percent'], 100)
        for path in ('src/app.py', 'src/other.py'):
            self.assertEqual((self.root / path).read_text(), 'value = 2\n')
        self.assertEqual((self.root / 'private.txt').read_text(), 'owner private\n')
        self.assertEqual((self.root / '.env').read_text(), 'FIXTURE_ONLY=unchanged\n')
        receipts = []
        for task in snapshot['tasks']:
            receipt = task['receipt']
            self.assertTrue(receipt['checks_passed'])
            self.assertTrue(receipt['review_approved'])
            self.assertTrue(receipt['checks'])
            # Fresh configured checks must have actual artifacts under this
            # authoritative run, never a previous task's old check output.
            for check in receipt['checks']:
                self.assertIn(receipt['run_id'], check['output_artifact'])
                artifact = self.root / check['output_artifact']
                self.assertTrue(artifact.is_file())
                output_text = artifact.read_text()
                measured = next(line.split('BENCHMARK_SOURCE:', 1)[1]
                    for line in output_text.splitlines() if line.startswith('BENCHMARK_SOURCE:'))
                checked_values = json.loads(measured)
                changed_source = 'src/app.py' if task['id'] == 'T1' else 'src/other.py'
                self.assertEqual(checked_values[changed_source], 'value = 2')
                self.assertTrue(check['passed'])
                self.assertEqual(check['returncode'], 0)
                self.assertFalse(check.get('reused_from_run'))
            if documentation:
                docs = receipt['documentation']
                self.assertEqual(docs['status'], 'published')
                self.assertEqual(docs['decision'], 'required')
                self.assertTrue(docs['checks_passed'] and docs['review_approved'])
                for path in docs['paths']:
                    self.assertIn('value = 2', (self.root / path).read_text())
            receipts.append({'task_id': task['id'], 'run_id': receipt['run_id'],
                'source_sha256': receipt['source_sha256'], 'checks': receipt['checks'],
                'documentation': receipt['documentation']})
        context_after = project_context.inspect_context(self.root)
        self.assertEqual(context_after['status'], expected_context_status, context_after.get('conflicts'))
        approvals = []
        for approval in sorted(self.root.rglob('context-approval.json')):
            approvals.append({'path': str(approval.relative_to(self.root)),
                'receipt': json.loads(approval.read_text())})
        self.assertTrue(approvals, 'Reviewed project context must have its actual approval receipt')
        events = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertTrue(events)
        return {'strategy': 'legacy', 'calls': len(events), 'by_role': dict(Counter(e['role'] for e in events)),
            'provider_script_sha256': hashlib.sha256(code.encode()).hexdigest(),
            'context_state': 'warm' if warm_context else 'cold',
            'context_preparation': preparation,
            'context_evidence': {'before_status': context_before['status'],
                'before_digest': context_before.get('digest'), 'after_status': context_after['status'],
                'after_digest': context_after['digest'], 'approvals': approvals},
            'events': events, 'elapsed_seconds': round(elapsed, 3), 'completed_tasks': 2,
            'accepted_source_verified': True, 'guarded_files_unchanged': True,
            'fresh_checks_verified': True, 'documentation_enabled': documentation, 'receipts': receipts,
            'required_test_commands': self.cfg['defaults'].get('required_test_commands', [])}
    def test_same_workload_has_complete_acceptance_evidence(self):
        measurement = self.measure()
        self.assertEqual(measurement['calls'], 10)
        self.assertEqual(measurement['by_role'], {'project_architect': 1, 'plan_reviewer': 3,
            'planner': 2, 'builder': 2, 'reviewer': 2})
        self.assert_bootstrap_preparation(measurement)
        self.assertFalse(list((self.root / '.kh/parallel').glob('*/lanes/*')))
    def assert_bootstrap_preparation(self, measurement):
        # Cold preparation must finish before any ticket planner is dispatched.
        # The actual independent approval binds the reviewed publication,
        # in addition to the measured dispatch count.
        self.assertEqual([e['role'] for e in measurement['events'][:2]],
            ['project_architect', 'plan_reviewer'])
        approvals = measurement['context_evidence']['approvals']
        self.assertEqual(len(approvals), 1)
        self.assertNotIn('.kh/parallel/', approvals[0]['path'])
        self.assertEqual(approvals[0]['receipt']['digest'],
            measurement['context_evidence']['after_digest'])
    def test_warm_legacy_excludes_preparation_from_execution_count(self):
        measurement = self.measure(warm_context=True)
        self.assertEqual(measurement['calls'], 8)
        self.assertEqual(measurement['by_role'], {'plan_reviewer': 2,
            'planner': 2, 'builder': 2, 'reviewer': 2})
        self.assertEqual(measurement['context_preparation']['calls'], 2)

    def test_scoped_guides_add_no_calls_when_documentation_is_enabled(self):
        # Fixed dispatch budget for the seeded workload.
        for strategy, expected in [('legacy', 12)]:
            with self.subTest(strategy=strategy):
                case = NextCallCountTests(); case.setUp()
                try:
                    measured = case.measure(documentation=True, scoped_guides=True)
                    self.assertEqual(measured['calls'], expected, measured['by_role'])
                    self.assertEqual(measured['by_role']['project_architect'], 1)
                finally:
                    case.tearDown()
    def test_disabled_documentation_refreshes_only_before_next_consumption(self):
        for strategy, expected in [('legacy', 12)]:
            with self.subTest(strategy=strategy):
                case = NextCallCountTests(); case.setUp()
                try:
                    measured = case.measure(scoped_guides=True, expected_context_status='stale')
                    self.assertEqual(measured['calls'], expected, measured['by_role'])
                    self.assertEqual(measured['by_role']['project_architect'], 2)
                    self.assertEqual(sum('_context_review_' in row['phase'] for row in measured['events']), 2)
                    # T1 refreshes before T2; unconfigured T2 leaves its guide stale.
                finally:
                    case.tearDown()


def benchmark(output):
    results = []
    for warm_context in (False, True):
        case = NextCallCountTests()
        case.setUp()
        try:
            results.append(case.measure(warm_context=warm_context))
        finally:
            case.tearDown()
    runtime_digest = hashlib.sha256()
    for path in sorted(ADWS.rglob('*.py')):
        runtime_digest.update(str(path.relative_to(ADWS)).encode() + b'\0' + path.read_bytes() + b'\0')
    payload = {'schema_version': 1, 'runtime_root': str(RUNTIME_ROOT),
        'runtime_python_sha256': runtime_digest.hexdigest(),
        'fixture_sha256': hashlib.sha256(fixture.FAKE.encode()).hexdigest(),
        'provider': 'deterministic fake CLI',
        'context_preparation_reported_separately': True,
        'limitations': ['Not a live-model cost, quality or speed benchmark.'],
        'results': results}
    Path(output).write_text(json.dumps(payload, indent=2) + '\n')
    print(json.dumps([{k: r[k] for k in ('strategy', 'context_state', 'calls', 'by_role', 'elapsed_seconds')} for r in results], indent=2))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] in ('--benchmark', '--benchmark-contexts'):
        benchmark(sys.argv[2])
    else:
        unittest.main()
