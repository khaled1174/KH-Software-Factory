"""Owner-authorized revisions against the real project ledger and intent files."""
from copy import deepcopy
from contextlib import closing
import hashlib
import json
import sqlite3
import unittest

from tests import test_project as fixture
from adw_modules import project, session


class ProjectRevisionTests(unittest.TestCase):
    setUp = fixture.ProjectTests.setUp
    tearDown = fixture.ProjectTests.tearDown
    files = fixture.ProjectTests.files
    receipt = fixture.ProjectTests.receipt
    reviewing = fixture.ProjectTests.reviewing
    complete = fixture.ProjectTests.complete

    def init(self):
        document = fixture.plan()
        task = document['tasks'][0]
        task.update({'weight': 4, 'constraints': ['Keep the public API'],
                     'non_goals': ['No deployment']})
        prerequisite = {**deepcopy(task), 'id': 'P', 'title': 'Prerequisite',
                        'request': 'Prepare the prerequisite', 'weight': 1}
        task['dependencies'] = ['P']
        document['tasks'].insert(0, prerequisite)
        project.init_project(self.root, document)
        return document

    def definition(self, task='T1', **updates):
        with closing(sqlite3.connect(self.root / '.kh/project.db')) as conn:
            value = json.loads(conn.execute('SELECT definition FROM tasks WHERE id=?', (task,)).fetchone()[0])
        return {**value, **updates}

    def accepted(self, definition, *, draft=False):
        directory = self.root / 'intents'
        directory.mkdir(exist_ok=True)
        text = (f"# Intent: {definition['id']}\n\nStatus: {'draft' if draft else 'accepted'}\n\n"
                f"## Task request\n\n{definition['request']}\n\n"
                '## Problem\n\nThis work is required.\n\n'
                '## Proposed outcome\n\nThe requested work passes its checks.\n\n'
                '## Acceptance\n\nAccepted by: Project owner\nAccepted on: 2026-10-13\n')
        # Fixture acceptance is owner input, independent of the revision API.
        text += 'Accepted digest: ' + hashlib.sha256(text.encode()).hexdigest() + '\n'
        (directory / (definition['id'] + '.md')).write_text(text)

    def ledger(self):
        with closing(sqlite3.connect(self.root / '.kh/project.db')) as conn:
            return {name: conn.execute(f'SELECT * FROM {name} ORDER BY 1').fetchall()
                    for name in ('project', 'tasks', 'attempts', 'events')}

    def children(self):
        original = self.definition()
        return [{**original, 'title': 'First unit', 'request': 'Build the first unit',
                 'allowed_paths': ['src/first.py'], 'dependencies': [], 'weight': 1,
                 'constraints': [], 'non_goals': [], 'required_checks': []},
                {**original, 'id': 'T1b', 'title': 'Second unit', 'request': 'Build the second unit',
                 'allowed_paths': ['src/second.py'], 'dependencies': ['T1'], 'weight': 3,
                 'constraints': [], 'non_goals': [], 'required_checks': []}]

    def api(self, name):
        method = getattr(project, name, None)
        self.assertTrue(callable(method), f'Project ledger lacks the owner {name} operation')
        return method

    def test_amend_replaces_definition_and_appends_owner_event(self):
        document = self.init()
        replacement = self.definition(title='Smaller foundation', request='Build the smaller foundation',
                                      allowed_paths=['src/foundation.py'])
        self.accepted(replacement)
        old = self.definition()
        history = self.ledger()['events']
        result = self.api('amend_task')(self.root, 'T1', replacement, reason='Use a verifiable unit')
        current = project.load_task(self.root, 'T1')
        self.assertEqual(current['request'], 'Build the smaller foundation')
        self.assertEqual(current['allowed_paths'], ['src/foundation.py'])
        self.assertEqual(current['status'], 'pending')
        self.assertEqual(result['project'], document['project'])
        self.assertEqual([m['id'] for m in result['milestones']], ['M1', 'M2'])
        ledger = self.ledger()
        self.assertEqual(ledger['events'][:-1], history)
        event = ledger['events'][-1]
        self.assertEqual(event[3], 'amended')
        detail = json.loads(event[4])
        self.assertEqual(detail['reason'], 'Use a verifiable unit')
        self.assertEqual(detail['before'], old)
        self.assertEqual(detail['after'], replacement)
        self.assertFalse((self.root / '.kh/intent-digests.json').exists())

    def test_blocked_amend_starts_fresh_without_changing_prior_evidence(self):
        self.init()
        self.complete('P', 'pre')
        project.begin_task(self.root, 'T1', 'old-run')
        project.record_handoff(self.root, 'T1', 'old-run',
                               {'phase': 'builder', 'status': 'failed', 'summary': 'Prior candidate'})
        project.fail_task(self.root, 'T1', 'old-run', 'Too broad to verify')
        receipt_path = project.receipt_path(self.root, 'T1', 'old-run')
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_text('{"unaccepted":"prior evidence"}\n')
        candidate = self.root / '.kh/work/T1/current/files/src/first.py'
        candidate.parent.mkdir(parents=True)
        candidate.write_text('saved source')
        approval = self.root / 'adws/adw_data/sessions/old-run/plan-approval.json'
        approval.parent.mkdir(parents=True, exist_ok=True)
        approval.write_text('{"approved":"old plan"}\n')
        protected = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                     for path in (receipt_path, candidate, approval)}
        before = self.ledger()
        previous = project.load_task(self.root, 'T1')
        replacement = self.definition(request='Build a revised foundation')
        self.accepted(replacement)
        result = self.api('amend_task')(self.root, 'T1', replacement, reason='Resolve the oversized request')
        current = project.load_task(self.root, 'T1')
        self.assertEqual(current['status'], 'pending')
        self.assertIsNone(current['run_id'])
        self.assertIsNone(current['blocker'])
        self.assertIsNone(current['detail'])
        self.assertEqual(current['evidence'], [])
        self.assertEqual(self.ledger()['attempts'], before['attempts'])
        self.assertEqual(self.ledger()['events'][:-1], before['events'])
        self.assertEqual(json.loads(self.ledger()['events'][-1][4])['previous_state'], previous)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected}, protected)
        self.assertTrue(result['revision']['stale_candidate'])
        self.assertIn('retained', result['revision']['candidate_message'])
        project.begin_task(self.root, 'T1', 'new-run')
        self.assertEqual(self.ledger()['attempts'], [('new-run', 'T1'), ('old-run', 'T1'), ('pre', 'P')])

    def test_amend_requires_owner_accepted_exact_intent(self):
        self.init()
        replacement = self.definition(request='Revised owner request')
        amend = self.api('amend_task')
        for kind in ('missing', 'draft', 'mismatch'):
            with self.subTest(kind=kind):
                if kind == 'draft':
                    self.accepted(replacement, draft=True)
                elif kind == 'mismatch':
                    self.accepted({**replacement, 'request': 'Different owner request'})
                before = self.files()
                with self.assertRaisesRegex(project.ProjectError, 'intent'):
                    amend(self.root, 'T1', replacement, reason='Owner revision')
                self.assertEqual(self.files(), before)

    def test_amend_rejects_completed_target_and_any_active_task(self):
        self.init()
        replacement = self.definition(request='Revised owner request')
        self.accepted(replacement)
        amend = self.api('amend_task')
        project.begin_task(self.root, 'P', 'active')
        before = self.files()
        with self.assertRaisesRegex(project.ProjectError, 'active'):
            amend(self.root, 'T1', replacement, reason='Owner revision')
        self.assertEqual(self.files(), before)
        project.transition(self.root, 'P', 'active', 'verifying')
        project.transition(self.root, 'P', 'active', 'reviewing')
        project.complete_task(self.root, 'P', 'active', self.receipt('P', 'active'))
        completed = self.definition('P', request='Replace accepted prerequisite')
        self.accepted(completed)
        before = self.files()
        with self.assertRaisesRegex(project.ProjectError, 'pending|completed'):
            amend(self.root, 'P', completed, reason='Owner revision')
        self.assertEqual(self.files(), before)

    def test_amend_bad_dag_identity_and_empty_reason_leave_ledger_unchanged(self):
        self.init()
        amend = self.api('amend_task')
        replacement = self.definition(request='Revised owner request')
        self.accepted(replacement)
        for updates, reason in (({'dependencies': ['T2']}, 'Cycle'), ({'dependencies': ['missing']}, 'Missing'),
                                ({'id': 'new-id'}, 'Rename'), ({}, ' ')):
            before = self.files()
            with self.subTest(updates=updates), self.assertRaises(project.ProjectError):
                amend(self.root, 'T1', {**replacement, **updates}, reason=reason)
            self.assertEqual(self.files(), before)

    def test_amend_dry_run_has_no_writes_and_shows_proposed_task(self):
        self.init()
        replacement = self.definition(request='Previewed owner revision')
        self.accepted(replacement)
        # A read-only preview must not recreate even a missing controller lock.
        (self.root / '.kh/controller.lock').unlink()
        before = self.files()
        result = self.api('amend_task')(self.root, 'T1', replacement, reason='Preview', dry_run=True)
        self.assertTrue(result['dry_run'])
        proposed = next(task for task in result['tasks'] if task['id'] == 'T1')
        self.assertEqual(proposed['request'], 'Previewed owner revision')
        self.assertEqual(project.load_task(self.root, 'T1')['request'], 'Build the foundation')
        self.assertEqual(self.files(), before)

    def test_split_inherits_boundary_and_rewires_only_terminal_replacements(self):
        self.init()
        children = self.children()
        children.append({**deepcopy(children[1]), 'id': 'T1c', 'request': 'Build a parallel final unit',
                         'allowed_paths': ['src/final/'], 'weight': 1, 'dependencies': ['T1']})
        children[1]['weight'] = 2
        for child in children:
            self.accepted(child)
        before = self.ledger()
        result = self.api('split_task')(self.root, 'T1', children, reason='Separate verifiable units')
        self.assertEqual([task['id'] for task in result['tasks']], ['P', 'T1', 'T1b', 'T1c', 'T2'])
        by_id = {task['id']: task for task in result['tasks']}
        self.assertEqual(by_id['T1']['dependencies'], ['P'])
        self.assertEqual(by_id['T1b']['dependencies'], ['P', 'T1'])
        self.assertEqual(by_id['T1c']['dependencies'], ['P', 'T1'])
        self.assertEqual(by_id['T2']['dependencies'], ['T1b', 'T1c'])
        for ident in ('T1', 'T1b', 'T1c'):
            task = by_id[ident]
            self.assertEqual(task['milestone_id'], 'M1')
            self.assertEqual(task['constraints'], ['Keep the public API'])
            self.assertEqual(task['non_goals'], ['No deployment'])
            self.assertEqual(task['required_checks'], ['test'])
            self.assertEqual(task['status'], 'pending')
        self.assertEqual(sum(task['weight'] for task in result['tasks']), 8)
        self.assertEqual(self.ledger()['events'][:len(before['events'])], before['events'])
        self.assertFalse((self.root / '.kh/intent-digests.json').exists())
        self.complete('P', 'pre')
        self.complete('T1', 'first')
        self.assertEqual(project.status(self.root)['next_tasks'], ['T1b', 'T1c'])
        with self.assertRaisesRegex(project.ProjectError, 'incomplete dependencies'):
            project.begin_task(self.root, 'T2', 'too-early')

    def test_split_refuses_expansion_bad_weights_and_invalid_dag_without_writes(self):
        self.init()
        split = self.api('split_task')
        children = self.children()
        for child in children:
            self.accepted(child)
        mutations = [lambda c: c[1].update(allowed_paths=['other/']),
                     lambda c: c[1].update(weight=4),
                     lambda c: c[0].update(dependencies=['T1b']),
                     lambda c: c[1].update(dependencies=['missing']),
                     lambda c: c[0].update(id='renamed'),
                     lambda c: c[1].update(id='T2')]
        for mutate in mutations:
            proposed = deepcopy(children)
            mutate(proposed)
            before = self.files()
            with self.subTest(proposed=proposed), self.assertRaises(project.ProjectError):
                split(self.root, 'T1', proposed, reason='Split safely')
            self.assertEqual(self.files(), before)

    def test_split_requires_acceptance_for_every_child_and_at_least_two_children(self):
        self.init()
        split = self.api('split_task')
        children = self.children()
        self.accepted(children[0])
        for draft in (False, True):
            if draft:
                self.accepted(children[1], draft=True)
            before = self.files()
            with self.assertRaisesRegex(project.ProjectError, 'intent'):
                split(self.root, 'T1', children, reason='Split safely')
            self.assertEqual(self.files(), before)
        with self.assertRaisesRegex(project.ProjectError, 'two|2'):
            split(self.root, 'T1', children[:1], reason='Not a split')

    def test_split_blocked_history_stays_and_dry_run_does_not_write(self):
        self.init()
        self.complete('P', 'pre')
        project.begin_task(self.root, 'T1', 'old-run')
        project.fail_task(self.root, 'T1', 'old-run', 'Split this large task')
        children = self.children()
        for child in children:
            self.accepted(child)
        split = self.api('split_task')
        candidate = self.root / '.kh/work/T1/current/meta.json'
        candidate.parent.mkdir(parents=True)
        candidate.write_text('{"run_id":"old-run","plan":"prior approval"}\n')
        kept = (candidate.read_bytes(), candidate.stat().st_mtime_ns)
        before = self.files()
        preview = split(self.root, 'T1', children, reason='Separate units', dry_run=True)
        self.assertEqual(self.files(), before)
        self.assertEqual(preview['next_tasks'], ['T1'])
        history = self.ledger()
        result = split(self.root, 'T1', children, reason='Separate units')
        self.assertEqual(self.ledger()['attempts'], history['attempts'])
        self.assertEqual(self.ledger()['events'][:len(history['events'])], history['events'])
        self.assertEqual(next(t for t in result['tasks'] if t['id'] == 'T1')['status'], 'pending')
        self.assertEqual((candidate.read_bytes(), candidate.stat().st_mtime_ns), kept)
        self.assertTrue(result['revision']['stale_candidate'])

    def test_split_refuses_active_completed_and_completed_downstream_tasks(self):
        self.init()
        children = self.children()
        for child in children:
            self.accepted(child)
        split = self.api('split_task')
        project.begin_task(self.root, 'P', 'active')
        before = self.files()
        with self.assertRaisesRegex(project.ProjectError, 'active'):
            split(self.root, 'T1', children, reason='Separate units')
        self.assertEqual(self.files(), before)
        project.fail_task(self.root, 'P', 'active', 'Interrupted')
        # Characterize refusal even for a corrupt/imported ledger with an accepted dependent.
        with closing(sqlite3.connect(self.root / '.kh/project.db')) as conn:
            conn.execute("UPDATE tasks SET status='completed' WHERE id='T2'")
            conn.commit()
        before = self.files()
        with self.assertRaisesRegex(project.ProjectError, 'completed'):
            split(self.root, 'T1', children, reason='Separate units')
        self.assertEqual(self.files(), before)
        with closing(sqlite3.connect(self.root / '.kh/project.db')) as conn:
            conn.execute("UPDATE tasks SET status='completed' WHERE id='T1'")
            conn.commit()
        before = self.files()
        with self.assertRaisesRegex(project.ProjectError, 'pending|completed'):
            split(self.root, 'T1', children, reason='Separate units')
        self.assertEqual(self.files(), before)

    def test_revision_respects_the_existing_repository_lease(self):
        self.init()
        replacement = self.definition(request='Owner revision')
        self.accepted(replacement)
        before = self.files()
        with session.acquire_repo_lock(self.root):
            with self.assertRaisesRegex(RuntimeError, 'BUSY'):
                self.api('amend_task')(self.root, 'T1', replacement, reason='Change the task')
        self.assertEqual(self.files(), before)

    def test_split_database_failure_rolls_back_definitions_positions_and_events(self):
        self.init()
        children = self.children()
        for child in children:
            self.accepted(child)
        with closing(sqlite3.connect(self.root / '.kh/project.db')) as conn:
            conn.execute("CREATE TRIGGER reject_child_event BEFORE INSERT ON events "
                         "WHEN NEW.event='split_created' BEGIN "
                         "SELECT RAISE(ABORT, 'owner ledger failure'); END")
            conn.commit()
        before = self.ledger()
        snapshot = project.status(self.root)
        with self.assertRaisesRegex(project.ProjectError, 'owner ledger failure'):
            self.api('split_task')(self.root, 'T1', children, reason='Separate units')
        self.assertEqual(self.ledger(), before)
        self.assertEqual(project.status(self.root), snapshot)

    def test_split_decimal_weights_keep_total_exact(self):
        document = fixture.plan()
        document['tasks'][0]['weight'] = 0.3
        project.init_project(self.root, document)
        children = self.children()
        children[0]['weight'], children[1]['weight'] = 0.1, 0.2
        for child in children:
            self.accepted(child)
        result = self.api('split_task')(self.root, 'T1', children, reason='Separate units')
        self.assertEqual([task['weight'] for task in result['tasks']], [0.1, 0.2, 3])
        # Small intentional weight growth must be rejected rather than hidden by a tolerance.
        before = self.ledger()
        children[0]['weight'], children[1]['weight'] = 0.01, 0.09000000000000001
        children[1]['id'] = 'T1d'
        with self.assertRaisesRegex(project.ProjectError, 'weight'):
            self.api('split_task')(self.root, 'T1', children, reason='Incorrect total')
        self.assertEqual(self.ledger(), before)


if __name__ == '__main__':
    unittest.main()
