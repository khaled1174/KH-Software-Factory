"""Reviewed context lifecycle, backward compatibility and selection receipts."""
import json
from pathlib import Path
import unittest
from tests import test_project_context as helpers
pc = helpers.pc


class ContextLifecycleTests(unittest.TestCase):
    setUp = helpers.ContextTests.setUp
    tearDown = helpers.ContextTests.tearDown
    proposal = helpers.ContextTests.proposal
    review = helpers.ContextTests.review
    publish = helpers.ContextTests.publish

    def with_documents(self, documents):
        proposal, baseline = self.proposal()
        proposal.documents.extend(documents)
        pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        return pc.build_context_packet(self.root, 'edit main', ['src/main.py'])

    def test_history_is_retained_but_never_loaded_as_current_rules(self):
        packet = self.with_documents([
            pc.ContextDocument(path='docs/agent-guide/old.md', content='Old color', status='superseded'),
            pc.ContextDocument(path='docs/agent-guide/current.md', content='Current color',
                               supersedes=['docs/agent-guide/old.md']),
            pc.ContextDocument(path='docs/agent-guide/history.md', content='Change history',
                               purpose='change_record', status='historical')])
        paths = {d['path'] for d in packet['documents']}
        self.assertIn('docs/agent-guide/current.md', paths)
        self.assertNotIn('docs/agent-guide/old.md', paths)
        self.assertNotIn('docs/agent-guide/history.md', paths)
        self.assertEqual((self.root/'docs/agent-guide/old.md').read_text(), 'Old color')
        omitted = {d['path']: d['reason'] for d in packet['selection']['omitted']}
        self.assertEqual(omitted['docs/agent-guide/old.md'], 'superseded')
        self.assertEqual(omitted['docs/agent-guide/history.md'], 'historical')

    def test_topic_hint_adds_applicable_guide_with_explicit_reason(self):
        self.with_documents([pc.ContextDocument(path='docs/agent-guide/auth.md', content='Auth rules',
            applies_to=['backend/auth/**'], topics=['authentication'])])
        packet = pc.build_context_packet(self.root, 'Review authentication in main', ['src/main.py'])
        selected = {d['path']: d for d in packet['selection']['selected']}
        self.assertEqual(selected['docs/agent-guide/auth.md']['reason'], 'topic_match')
        self.assertIn('CLAUDE.md', selected)
        self.assertEqual(selected['CLAUDE.md']['reason'], 'root_instruction')
        ordinary = pc.build_context_packet(self.root, 'edit main', ['src/main.py'])
        self.assertNotIn('docs/agent-guide/auth.md', {d['path'] for d in ordinary['documents']})

    def test_content_refresh_preserves_omitted_metadata_and_reviews_explicit_transition(self):
        path = 'docs/agent-guide/history.md'
        self.with_documents([pc.ContextDocument(path=path, content='Retired interface',
            purpose='change_record', status='historical', applies_to=['legacy/**'],
            topics=['legacy'], source_revision='old-revision')])
        proposal, baseline = self.proposal()
        proposal.documents.append(pc.ContextDocument(path=path, content='Retired interface'))
        # Validation resolves omissions before the independent review is bound.
        self.assertTrue(pc.validate_proposal(self.root, proposal, baseline)['valid'])
        resolved = proposal.documents[-1]
        self.assertEqual((resolved.purpose, resolved.status), ('change_record', 'historical'))
        self.assertEqual(resolved.applies_to, ['legacy/**'])
        self.assertEqual(resolved.topics, ['legacy'])
        self.assertEqual(resolved.source_revision, 'old-revision')
        review = self.review(proposal)
        pc.publish_context(self.root, proposal, review, baseline)
        packet = pc.build_context_packet(self.root, 'legacy', ['legacy/main.py'])
        self.assertNotIn(path, {row['path'] for row in packet['documents']})
        # Reactivation must be explicit and needs a review of the new metadata.
        proposal, baseline = self.proposal()
        proposal.documents.append(pc.ContextDocument(path=path, content='Retired interface',
            purpose='guide', status='active', applies_to=['src/**'], topics=[]))
        self.assertTrue(pc.validate_proposal(self.root, proposal, baseline)['valid'])
        with self.assertRaisesRegex(pc.ContextBlocked, 'review'):
            pc.publish_context(self.root, proposal, review, baseline)
        pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        packet = pc.build_context_packet(self.root, 'edit main', ['src/main.py'])
        self.assertIn(path, {row['path'] for row in packet['documents']})

    def test_refresh_review_sees_prior_and_resolved_metadata_and_binds_prior_index(self):
        import contextlib
        from types import SimpleNamespace
        from unittest.mock import patch
        path = 'docs/agent-guide/history.md'
        self.with_documents([pc.ContextDocument(path=path, content='Retired interface',
            purpose='change_record', status='historical')])
        proposal, _ = self.proposal()
        proposal.documents.append(pc.ContextDocument(path=path, content='Retired interface'))
        captured = {}

        @contextlib.contextmanager
        def phase(params):
            yield SimpleNamespace(phase=SimpleNamespace(params=params))

        def execute(run, phase, call):
            brief = json.loads(call.prompt)
            captured.update(brief['baseline'])
            previous = next(row for row in brief['baseline']['indexed_documents'] if row['path'] == path)
            self.assertEqual(previous['status'], 'historical')
            if call.output_type is pc.ProjectContextProposal:
                return proposal
            reviewed = next(row for row in brief['proposal']['documents'] if row['path'] == path)
            self.assertEqual(reviewed['status'], 'historical')
            self.assertEqual(brief['proposal_digest'], pc.digest(brief['proposal']))
            return self.review(proposal)

        run = SimpleNamespace(repo_root=self.root, cfg=object(), phase=phase,
            session_dir=self.root/'.kh/session', adw_id='refresh')
        with patch('adw_modules.agents.resolve', return_value=SimpleNamespace(writes=None)), \
             patch('adw_modules.agents.execute', side_effect=execute):
            pc.bootstrap(run, refresh=True)
        # A metadata-only owner change cannot be hidden behind unchanged contents.
        index = json.loads((self.root/pc.INDEX).read_text())
        index['documents'][-1]['topics'] = ['changed']
        index['digest'] = pc.digest({k:v for k,v in index.items() if k != 'digest'})
        (self.root/pc.INDEX).write_text(json.dumps(index))
        self.assertFalse(pc.validate_proposal(self.root, proposal, captured)['valid'])

    def test_root_and_nested_instruction_lifecycle_cannot_be_disabled(self):
        for path in ('CLAUDE.md', 'AGENTS.md'):
            proposal, baseline = self.proposal()
            next(d for d in proposal.documents if d.path == path).status = 'historical'
            self.assertFalse(pc.validate_proposal(self.root, proposal, baseline)['valid'])

    def test_invalid_supersession_target_and_live_replacement_are_rejected(self):
        for target, previous in [('docs/agent-guide/missing.md', []),
                ('docs/agent-guide/old.md', [pc.ContextDocument(path='docs/agent-guide/old.md', content='still active')])]:
            proposal, baseline = self.proposal()
            proposal.documents.extend(previous + [pc.ContextDocument(path='docs/agent-guide/new.md',
                content='New', supersedes=[target])])
            self.assertFalse(pc.validate_proposal(self.root, proposal, baseline)['valid'])

    def test_supersession_cycle_is_rejected(self):
        proposal, baseline = self.proposal()
        proposal.documents.extend([
            pc.ContextDocument(path='docs/agent-guide/a.md', content='a', status='superseded', supersedes=['docs/agent-guide/b.md']),
            pc.ContextDocument(path='docs/agent-guide/b.md', content='b', status='superseded', supersedes=['docs/agent-guide/a.md'])])
        self.assertFalse(pc.validate_proposal(self.root, proposal, baseline)['valid'])

    def test_legacy_index_without_metadata_keeps_rules(self):
        self.publish()
        index = json.loads((self.root/pc.INDEX).read_text())
        for row in index['documents']:
            for key in ('purpose', 'status', 'topics', 'supersedes', 'source_revision'):
                row.pop(key, None)
        index['digest'] = pc.digest({k:v for k,v in index.items() if k != 'digest'})
        (self.root/pc.INDEX).write_text(json.dumps(index))
        packet = pc.build_context_packet(self.root, 'edit main', ['src/main.py'])
        self.assertEqual(len(packet['documents']), 3)
        self.assertEqual(packet['selection']['selected'][0]['reason'], 'root_instruction')

    def test_index_metadata_tampering_cannot_disable_root_rules(self):
        self.publish()
        index = json.loads((self.root/pc.INDEX).read_text())
        index['documents'][0]['status'] = 'historical'
        index['digest'] = pc.digest({k:v for k,v in index.items() if k != 'digest'})
        (self.root/pc.INDEX).write_text(json.dumps(index))
        self.assertEqual(pc.inspect_context(self.root)['status'], 'blocked')

    def test_owner_guide_content_stays_preserved_with_metadata(self):
        owner = self.root/'docs/agent-guide/owner.md';owner.parent.mkdir(parents=True)
        owner.write_text('Owner rule in Arabic: احفظ البيانات')
        proposal, baseline = self.proposal()
        proposal.documents.append(pc.ContextDocument(path='docs/agent-guide/owner.md',
            content='Overwritten', topics=['database']))
        self.assertFalse(pc.validate_proposal(self.root, proposal, baseline)['valid'])
        self.assertIn('احفظ البيانات', owner.read_text())

    def test_topics_cannot_pull_foreign_directory_instructions_into_packet(self):
        nested = self.root/'mobile/AGENTS.md';nested.parent.mkdir();nested.write_text('Mobile only')
        proposal, baseline = self.proposal();proposal.retained_paths = ['mobile/AGENTS.md']
        pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        index = json.loads((self.root/pc.INDEX).read_text())
        next(r for r in index['documents'] if r['path'] == 'mobile/AGENTS.md')['topics'] = ['authentication']
        index['digest'] = pc.digest({k:v for k,v in index.items() if k != 'digest'})
        (self.root/pc.INDEX).write_text(json.dumps(index))
        packet = pc.build_context_packet(self.root, 'Review authentication', ['src/main.py'])
        self.assertNotIn('mobile/AGENTS.md', {d['path'] for d in packet['documents']})
