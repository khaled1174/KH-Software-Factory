#!/usr/bin/env python3
"""Measure existing documentation gates against frozen offline guide fixtures.

The agents.execute orchestration boundary is substituted with supplied structured
data and targeted invocation of its current mandatory gates. The actual proposal
closure comes from prepare_candidate; no gate is copied, weakened or given gold
labels. This does not measure full role acceptance or model semantic review.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import agent_cc, agents, dispositions, documentation, gates
from adw_modules import permissions, project_context as pc, quality, scope
from adw_modules.data_types import AgentConfig, KHConfig

CORPUS = ROOT / 'tests/fixtures/documentation_quality'
GUIDE = 'docs/agent-guide/project.md'
RUNTIME = ROOT / '.claude/skills/kh/templates/adws/adw_modules'
RUNTIME_FILES = ('agents.py', 'data_types.py', 'dispositions.py', 'documentation.py',
                 'gates.py', 'permissions.py', 'project_context.py', 'quality.py',
                 'scope.py', 'workspace.py')
CONTROLS = {'missing_artifact': 'artifacts_exist', 'empty_artifact': 'files_non_empty',
            'wrong_candidate': 'proposal_gate', 'unapproved_document': 'proposal_gate',
            'invented_owner_decision': 'claims_gate', 'changed_guide': 'project_context_readiness'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def relative(value):
    path = PurePosixPath(value)
    if (not value or value == '.' or path.is_absolute() or path.as_posix() != value
            or '..' in path.parts or '\\' in value or any(ord(c) < 32 for c in value)):
        raise ValueError('Unsafe fixture path: ' + str(value))
    return path


def fixture_file(folder, value):
    path = folder / relative(value)
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError('Fixture file escapes corpus: ' + str(value))
    return path


def read_corpus(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / 'corpus.json').read_text())
    if manifest.get('schema_version') not in (1, 2):
        raise ValueError('Unsupported documentation corpus schema')
    ids = [row['id'] for row in manifest['cases']]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate documentation fixture ID')
    for field, directory in [('base_hashes', 'base'), ('before_hashes', 'before')]:
        for name, expected in manifest[field].items():
            path = fixture_file(folder, manifest[directory] + '/' + str(relative(name)))
            if sha(path.read_bytes()) != expected:
                raise ValueError('Fixture source hash mismatch: ' + name)
    for row in manifest['cases']:
        if manifest['schema_version'] == 1 and any(key in row for key in ('source_change_paths', 'parallel_source_change')):
            raise ValueError('Source selection and parallel fixtures require corpus schema 2')
        if type(row['defective']) is not bool:
            raise ValueError('A fixture gold label must be boolean')
        if type(row['source_changed_after_review']) is not bool:
            raise ValueError('A fixture source-change flag must be boolean')
        if type(row.get('parallel_source_change', False)) is not bool:
            raise ValueError('A fixture parallel flag must be boolean')
        changes = row.get('source_change_paths')
        if changes is not None and (not isinstance(changes, list) or not changes
                or any(not isinstance(p, str) or p not in manifest['before_hashes'] for p in changes)
                or len(changes) != len(set(changes)) or not row['source_changed_after_review']):
            raise ValueError('Fixture source changes must select distinct existing before snapshots')
        if row.get('parallel_source_change') and not row['source_changed_after_review']:
            raise ValueError('A parallel source-change fixture needs a changed source snapshot')
        if sha(fixture_file(folder, row['guide']).read_bytes()) != row['guide_sha256']:
            raise ValueError('Fixture guide hash mismatch: ' + row['id'])
    return manifest


class _Captured(Exception):
    pass


@contextlib.contextmanager
def _phase(params):
    yield SimpleNamespace(phase=SimpleNamespace(params=params, phase_id='offline_document'))


def _normalised(report, root):
    # The checks/notes remain intact, but temporary workspace spelling is not
    # part of a reproducible result or a gold label.
    return json.loads(json.dumps({'passed': report.passed, **report.model_dump()}).replace(
        root.as_posix(), '<workspace>'))


def run_fixture(folder, manifest, guide_text, *, source_changed=False, control=None,
                source_change_paths=None, parallel_source_change=False):
    """Run real gates without case IDs, categories or gold labels in their view."""
    with tempfile.TemporaryDirectory(prefix='kh-doc-eval-') as temporary:
        root = Path(temporary).resolve()
        base = fixture_file(folder, manifest['base'])
        before = fixture_file(folder, manifest['before'])
        change_paths = list(manifest['before_hashes'] if source_change_paths is None else source_change_paths)
        for name in manifest['base_hashes']:
            target = root / relative(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            initial = before / relative(name)
            source = initial if source_changed and name in change_paths else base / relative(name)
            target.write_bytes(source.read_bytes())
        before_hashes = {name: sha((root / name).read_bytes()) for name in manifest['base_hashes']}
        observed = pc.inspect_context(root)['observed']
        baseline = {'observed': observed, 'digest': pc.digest(observed)}
        proposal = pc.ProjectContextProposal(status='success', baseline_digest=baseline['digest'], documents=[
            pc.ContextDocument(path='CLAUDE.md', content='Read [guide](docs/agent-guide/project.md).\n'),
            pc.ContextDocument(path='AGENTS.md', content='Read [guide](docs/agent-guide/project.md).\n'),
            pc.ContextDocument(path=GUIDE, content=guide_text, applies_to=['logreports/**', 'tests/**'])])
        # Model a legacy ready snapshot, including previously approved mistakes.
        # A new fact gate can reject those bytes at fresh bootstrap, so seed the
        # old state using real projection/source bindings rather than weakening
        # or substituting any gate measured below. This is not semantic approval.
        baseline['guide_source_bindings'] = {document.path: {
            'applies_to': pc._applicability(document.path, document.applies_to),
            'source_digests': pc.source_digests(root, document.model_dump(), observed)}
            for document in proposal.documents}
        index, contents = pc._publication_content(root, proposal, baseline)
        pc._write_context(root, index, contents, {**observed['documents'], pc.INDEX: None})
        initial_status = pc.inspect_context(root)['status']
        if initial_status != 'ready':
            raise RuntimeError('Fixture publication did not produce its declared ready baseline')
        parallel_trace = None
        if parallel_source_change:
            # Two physical source copies reproduce merge order, not scheduling
            # or the production candidate-import protocol. Gates see only the
            # resulting main snapshot and its retained, reviewed B guide.
            lanes = {}
            for ticket in ('A', 'B'):
                lane = root / '.kh/offline-lanes' / ticket
                for name in [*manifest['base_hashes'], GUIDE]:
                    target = lane / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((root / name).read_bytes())
                lanes[ticket] = lane
            def lane_hashes(ticket):
                return {name: sha((lanes[ticket] / name).read_bytes()) for name in manifest['base_hashes']}
            parallel_trace = {'events': ['B_reviewed', 'A_and_B_forked'],
                'ticket_a_source_at_fork': lane_hashes('A'),
                'ticket_b_source_at_fork': lane_hashes('B'),
                'ticket_b_document_at_fork': sha((lanes['B'] / GUIDE).read_bytes())}
            if any(parallel_trace[f'ticket_{ticket}_source_at_fork'] != before_hashes for ticket in ('a', 'b')):
                raise RuntimeError('Parallel fixture did not fork the reviewed source snapshot')
            for name in change_paths:
                (lanes['A'] / name).write_bytes((base / name).read_bytes())
            parallel_trace['events'].append('A_changed')
            parallel_trace['ticket_a_source_after_change'] = lane_hashes('A')
            for name in change_paths:
                if sha((root / name).read_bytes()) != before_hashes[name]:
                    raise RuntimeError('Parallel fixture main baseline drifted before A was applied')
                (root / name).write_bytes((lanes['A'] / name).read_bytes())
            parallel_trace['events'].append('A_merged')
            parallel_trace['ticket_b_source_after_a_merge'] = lane_hashes('B')
            parallel_trace['ticket_b_document_after_a_merge'] = sha((lanes['B'] / GUIDE).read_bytes())
        elif source_changed:
            for name in change_paths:
                (root / name).write_bytes((base / name).read_bytes())
        after_hashes = {name: sha((root / name).read_bytes()) for name in manifest['base_hashes']}
        changed = [name for name in before_hashes if before_hashes[name] != after_hashes[name]]
        if control == 'changed_guide':
            (root / GUIDE).write_text(guide_text + '\nUnreviewed owner edit.\n')

        prompt = root / 'adws/offline-prompt.md'
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text('Offline fixture input; never dispatch a provider.\n')
        stub = root / '.kh/offline-tools/claude'
        stub.parent.mkdir(parents=True)
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "Provider dispatch is forbidden in this evaluator" >&2\nexit 97\n')
        stub.chmod(0o700)
        prompts = {'system': str(prompt), 'user': str(prompt)}
        defaults = json.loads((root / 'adws/adw_kh_config/kh.config.yaml').read_text())['defaults']
        choice = {'coding_agent': 'claude_code', 'model': 'offline-fixture',
                  'thinking': 'medium', 'supported_thinking': ['medium']}
        cfg = KHConfig(defaults=defaults, routing={'default_profile': 'offline', 'profiles': {
            'offline': {'roles': {'builder': [choice], 'documenter': [choice]}}}}, agents=[
            AgentConfig(name='builder', model='offline-fixture', writes=None, prompt_engineering=prompts),
            AgentConfig(name='documenter', model='offline-fixture', writes=[], tools=['read'], prompt_engineering=prompts)])
        cfg.execution.documentation.enabled = True
        allowed = [GUIDE, *manifest['before_hashes']]
        run = SimpleNamespace(repo_root=root, cfg=cfg, adw_id='offline', owned_paths=set(),
            session_dir=root / 'adws/adw_data/sessions/offline',
            context_handoff_dir=root / 'adws/adw_data/sessions/offline/context_handoff/documenter',
            scope_contract=scope.ScopeContract(original_request='Document current behavior',
                deliverables=['Required current project guide'], allowed_paths=allowed),
            evidence_policy={}, owner_decisions=[], phase=_phase)
        run.context_handoff_dir.mkdir(parents=True)
        artifact = run.context_handoff_dir / 'proposal.md'
        artifact.write_text('Fixture proposal inspected the supplied project snapshot.\n')
        if control == 'missing_artifact':
            artifact.unlink()
        elif control == 'empty_artifact':
            artifact.write_text('')
        run.task_baseline = permissions.snapshot(run)
        plan = SimpleNamespace(allowed_paths=allowed, documentation=documentation.DocumentationPlan(
            decision='required', reason='Publish the requested current project guide', paths=[GUIDE]))
        reports = {}

        def offline_execute(view, phase, call):
            if call.output_type is not documentation.DocumentationProposalOutput:
                raise RuntimeError('Unexpected provider call in offline evaluation')
            candidate = quality.source_fingerprint(view)
            output = documentation.DocumentationProposalOutput(status='success',
                summary='Fixture documentation proposal', artifacts=[str(artifact)],
                notes_for_next_agent=('Owner decision EVAL-X1 permits this.' if control == 'invented_owner_decision' else ''),
                candidate_sha256='0' * 64 if control == 'wrong_candidate' else candidate,
                documents=[pc.ContextDocument(path=GUIDE, content=guide_text,
                                               applies_to=['logreports/**', 'tests/**'])])
            if control == 'unapproved_document':
                output.documents.append(pc.ContextDocument(path='app_docs/unapproved.md',
                    content='An unapproved write target.', purpose='change_record', status='historical',
                    applies_to=['logreports/**']))
            # These are the existing mandatory gates for this output kind and
            # evidence policy plus the actual call's gates (including closure).
            effective = list(dict.fromkeys([gates.artifacts_exist, dispositions.claims_gate, *call.gates]))
            for gate in effective:
                reports[gate.__name__] = _normalised(gate(output, view), root)
            raise _Captured

        previous_cwd = Path.cwd()
        try:
            os.chdir(root)
            # Replace agent orchestration with targeted gate invocation.
            # Validation, permissions, scope and measured gates stay real.
            with patch.dict(os.environ, {'CLAUDE_CODE_PATH': str(stub)}), \
                    patch.object(agent_cc, 'CC_PATH', str(stub)), patch.object(agents, 'execute', offline_execute):
                try:
                    documentation.prepare_candidate(run, plan, changed)
                except _Captured:
                    pass
                except SystemExit as error:
                    raise RuntimeError('Offline fixture preflight failed: ' + str(error)) from error
                else:
                    raise RuntimeError('Offline dispatcher was not reached')
        finally:
            os.chdir(previous_cwd)
        inspected = pc.inspect_context(root)
        if inspected['status'] not in {'ready', 'stale', 'blocked'}:
            raise RuntimeError('Unexpected context inspection result')
        reports['project_context_readiness'] = {'passed': inspected['status'] == 'ready',
            'status': inspected['status'], 'conflicts': inspected['conflicts']}
        result = {'gates': reports, 'source_before': before_hashes, 'source_after': after_hashes,
                'changed_paths': sorted(changed), 'initial_context_status': initial_status,
                'guide_sha256': sha(guide_text.encode()), 'provider_calls': 0}
        if parallel_trace is not None:
            parallel_trace['main_document_before_b_acceptance'] = sha((root / GUIDE).read_bytes())
            parallel_trace['events'].append('before_B_acceptance')
            result['parallel_merge_order'] = parallel_trace
        return result


def score(rows, name):
    counts = {'caught': 0, 'missed': 0, 'false_alarm': 0, 'correct_accept': 0}
    for row in rows:
        detected = (any(not report['passed'] for report in row['gates'].values()) if name == 'combined'
                    else not row['gates'][name]['passed'])
        key = ('caught' if detected else 'missed') if row['defective'] else (
            'false_alarm' if detected else 'correct_accept')
        counts[key] += 1
    return counts


def evaluate(folder=CORPUS):
    folder = Path(folder).resolve()
    manifest = read_corpus(folder)
    rows = []
    for case in manifest['cases']:
        measured = run_fixture(folder, manifest, fixture_file(folder, case['guide']).read_text(),
                               source_changed=case['source_changed_after_review'],
                               source_change_paths=case.get('source_change_paths'),
                               parallel_source_change=case.get('parallel_source_change', False))
        # Gold labels enter scoring only after real gate execution has ended.
        rows.append({'id': case['id'], 'category': case['category'],
                     'defective': case['defective'], **measured})
    controls = []
    guide = fixture_file(folder, next(c['guide'] for c in manifest['cases'] if not c['defective'])).read_text()
    for name, target in CONTROLS.items():
        result = run_fixture(folder, manifest, guide, control=name)
        if result['gates'][target]['passed']:
            raise RuntimeError('Evaluator failed its real-gate integrity control: ' + name)
        controls.append({'id': name, 'target_gate': target, 'rejected': True, 'gates': result['gates']})
    names = sorted(rows[0]['gates'])
    if any(sorted(row['gates']) != names for row in rows):
        raise RuntimeError('Inconsistent gate inventory across fixtures')
    return {'schema_version': 1, 'corpus_sha256': sha((folder / 'corpus.json').read_bytes()),
        'evaluator_sha256': sha(Path(__file__).read_bytes()),
        'runtime_sha256': {name: sha((RUNTIME / name).read_bytes()) for name in RUNTIME_FILES},
        'counts': {'defective_guides': sum(r['defective'] for r in rows),
                   'correct_guides': sum(not r['defective'] for r in rows), 'integrity_controls': len(controls)},
        'metrics': {name: score(rows, name) for name in [*names, 'combined']},
        'cases': rows, 'integrity_controls': controls, 'provider_calls': 0,
        'limitations': ['Measures targeted deterministic documentation handoff gates and current context readiness only.',
            'agents.execute orchestration is substituted; current mandatory gate assembly is explicit, not automatically inherited.',
            'Correct-guide controls have unchanged source; accurate retained guides after source drift are not evaluated.',
            'The parallel fixture reproduces source forks and merge order, not concurrent scheduling or candidate-import acceptance.',
            'Legacy context seeding uses controller projection, not model semantic review or task acceptance.',
            'Structured fixture outputs bypass extraction; parser behavior has independent regression tests.',
            'No claim of general factual correctness, production false-alarm rate, live quality, speed or cost.',
            'Build omission is a coverage case; exclusive test-inventory claims are outside this corpus.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=CORPUS)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(args.corpus)
    except (ValueError, RuntimeError, OSError, SystemExit) as error:
        print('Documentation evaluation setup failed: ' + str(error), file=sys.stderr)
        return 1
    text = json.dumps(result, indent=2) + '\n'
    if args.output:
        try:
            with args.output.open('x') as output:
                output.write(text)
        except OSError as error:
            print('Cannot create evaluation output: ' + str(error), file=sys.stderr)
            return 1
    else:
        print(text, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
