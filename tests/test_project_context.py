"""Deterministic context publication and isolated bootstrap contract tests."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import contextlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import project_context as pc


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'src').mkdir()
        (self.root / 'src/main.py').write_text('original = True\n')

    def tearDown(self):
        self.tmp.cleanup()

    def proposal(self):
        observed = pc.inspect_context(self.root)['observed']
        baseline = {'observed': observed, 'digest': pc.digest(observed)}
        proposal = pc.ProjectContextProposal(status='success', baseline_digest=baseline['digest'],
            documents=[pc.ContextDocument(path='CLAUDE.md', content='Read [rules](docs/agent-guide/rules.md).\n'),
                       pc.ContextDocument(path='AGENTS.md', content='Read [rules](docs/agent-guide/rules.md).\n'),
                       pc.ContextDocument(path='docs/agent-guide/rules.md', content='Preserve owner scope.\n')])
        return proposal, baseline

    def review(self, proposal, **updates):
        return pc.ContextReviewOutput(status='success', proposal_digest=pc.digest(proposal.model_dump()),
                                      decision='approve', **updates)

    def publish(self):
        proposal, baseline = self.proposal()
        return pc.publish_context(self.root, proposal, self.review(proposal), baseline)

    def test_root_changelog_stays_out_of_agent_evidence_and_warm_context(self):
        from adw_modules.data_types import KHConfig
        path = self.root / 'CHANGELOG.md'
        path.write_text('HUMAN HISTORY SENTINEL\n')
        cfg = KHConfig()
        _, briefs, calls = self.bootstrap_commands(cfg)
        self.assertEqual(calls, 2)
        self.assertNotIn('CHANGELOG.md', json.dumps(briefs))
        self.assertNotIn('HUMAN HISTORY SENTINEL', json.dumps(briefs))
        before = (self.root / pc.INDEX).read_bytes()
        path.write_text('UPDATED HUMAN HISTORY SENTINEL\n')
        self.assertEqual(pc.inspect_context(self.root)['status'], 'ready')
        packet = pc.build_context_packet(self.root, 'edit main', ['src/**'])
        self.assertNotIn('CHANGELOG.md', json.dumps(packet))
        self.assertNotIn('HUMAN HISTORY SENTINEL', json.dumps(packet))
        _, _, calls = self.bootstrap_commands(cfg)
        self.assertEqual(calls, 0, 'human history cannot trigger warm context review')
        self.assertEqual((self.root / pc.INDEX).read_bytes(), before)
        self.assertEqual(path.read_text(), 'UPDATED HUMAN HISTORY SENTINEL\n')

    def test_publish_ready_packet_contains_actual_rules_and_source_is_preserved(self):
        self.publish()
        packet = pc.build_context_packet(self.root, 'edit main', ['src/main.py'])
        self.assertEqual(pc.inspect_context(self.root)['status'], 'ready')
        self.assertEqual(len(packet['documents']), 3)
        self.assertIn('Preserve owner scope.', str(packet))
        self.assertEqual((self.root / 'src/main.py').read_text(), 'original = True\n')
        self.assertFalse((self.root / pc.JOURNAL).exists())

    def bootstrap_commands(self, cfg):
        proposal = None
        briefs = []
        @contextlib.contextmanager
        def phase(params):
            yield SimpleNamespace(phase=SimpleNamespace(params=params))
        run = SimpleNamespace(repo_root=self.root, cfg=cfg, phase=phase,
            session_dir=self.root / '.kh/session', adw_id='commands-test')
        def execute(run, phase, call):
            nonlocal proposal
            briefs.append(json.loads(call.prompt))
            if phase.params.owner == 'project_architect':
                proposal, _ = self.proposal()
                return proposal
            return self.review(proposal)
        with patch('adw_modules.agents.resolve', return_value=SimpleNamespace(writes=None)), \
             patch('adw_modules.agents.execute', side_effect=execute) as calls:
            receipt = pc.bootstrap(run, max_repairs=0)
        return receipt, briefs, calls.call_count

    def test_bootstrap_commands_guide_uses_configuration_and_package_declarations(self):
        from adw_modules.data_types import KHConfig, ConfigDefaults
        (self.root / 'package.json').write_text(json.dumps({'scripts': {'build': 'tsc', 'test': 'node --test'}}))
        argv = ['python3', '-m', 'unittest', '[literal](missing.md)']
        cfg = KHConfig(defaults=ConfigDefaults(quality_checks={'test': argv},
                                               required_quality_checks=['test', 'build']))
        receipt, briefs, calls = self.bootstrap_commands(cfg)
        target = self.root / 'docs/agent-guide/commands.md'
        self.assertTrue(target.is_file(), 'The controller must publish the commands guide')
        expected = {'quality_checks': {'test': argv},
            'required_quality_checks': ['build', 'test'],
            'package_scripts': {'package.json': {'build': 'tsc', 'test': 'node --test'}}}
        body = json.loads(target.read_text().split('```json\n')[1].split('\n```')[0])
        self.assertEqual(body, expected)
        self.assertEqual(briefs[0]['observed_commands'], expected)
        self.assertEqual(calls, 2)
        self.assertIn(target.relative_to(self.root).as_posix(),
            [row['path'] for row in pc.build_context_packet(self.root, 'edit source', ['src/**'])['documents']])

    def test_commands_sync_does_not_rebind_unrelated_stale_context(self):
        from adw_modules.data_types import KHConfig
        self.publish()
        cfg = KHConfig()
        pc.sync_commands(self.root, cfg)
        before = (self.root / pc.INDEX).read_bytes()
        guide = (self.root / pc.COMMANDS_PATH).read_bytes()
        cfg.defaults.quality_checks = {'test': ['python3', '-m', 'unittest']}
        for path in ['docs/agent-guide/rules.md', 'src/new.py', 'package.json']:
            target = self.root / path
            original = target.read_bytes() if target.exists() else None
            with self.subTest(path=path):
                target.write_text('{}')
                pc.sync_commands(self.root, cfg)
                self.assertEqual((self.root / pc.INDEX).read_bytes(), before)
                self.assertEqual((self.root / pc.COMMANDS_PATH).read_bytes(), guide)
                self.assertEqual(pc.inspect_context(self.root)['status'], 'stale')
            target.unlink() if original is None else target.write_bytes(original)

    def test_commands_guide_project_paths_are_stable_with_quotes_and_backslashes(self):
        from adw_modules.data_types import KHConfig
        root = self.root / 'quoted " \\ project'
        root.mkdir()
        cfg = KHConfig()
        argv = ['python3', str(root / 'check.py')]
        cfg.defaults.quality_checks = {'test': argv}
        data = pc.command_declarations(root, cfg, {'manifests': {}})
        self.assertEqual(data['quality_checks']['test'], ['python3', '<project>/check.py'])
        self.assertEqual(cfg.defaults.quality_checks['test'], argv)

    def test_commands_sync_preserves_owner_authorship_even_for_identical_content(self):
        from adw_modules.data_types import KHConfig
        cfg = KHConfig()
        target = self.root / pc.COMMANDS_PATH
        target.parent.mkdir(parents=True)
        target.write_text('Owner commands.\n')
        self.publish()
        before = (self.root / pc.INDEX).read_bytes()
        with self.assertRaisesRegex(pc.ContextBlocked, 'Preserve owner'):
            pc.sync_commands(self.root, cfg)

        self.assertEqual(target.read_text(), 'Owner commands.\n')
        self.assertEqual((self.root / pc.INDEX).read_bytes(), before)
        target.write_text(pc.render_commands(pc.command_declarations(self.root, cfg, pc.inspect_context(self.root)['observed'])))
        pc.sync_commands(self.root, cfg)
        row = next(r for r in pc.inspect_context(self.root)['index']['documents'] if r['path'] == pc.COMMANDS_PATH)
        self.assertFalse(row['managed'])
        cfg.defaults.quality_checks = {'test': ['python3']}
        with self.assertRaisesRegex(pc.ContextBlocked, 'Preserve owner'):
            pc.sync_commands(self.root, cfg)


    def test_bootstrap_refresh_preserves_owner_commands_lifecycle_and_applicability(self):
        from adw_modules.data_types import KHConfig
        cfg = KHConfig()
        target = self.root / pc.COMMANDS_PATH
        target.parent.mkdir(parents=True)
        target.write_text(pc.render_commands(pc.command_declarations(self.root, cfg, {'manifests': {}})))
        proposal, baseline = self.proposal()
        proposal.retained_paths = [pc.COMMANDS_PATH]
        pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        index = pc.inspect_context(self.root)['index']
        row = next(r for r in index['documents'] if r['path'] == pc.COMMANDS_PATH)
        row.update(purpose='change_record', status='historical', applies_to=['src/special.py'])
        index.pop('digest'); index['digest'] = pc.digest(index)
        pc._atomic(self.root, pc.INDEX, json.dumps(index))
        before = target.read_bytes()
        proposal, baseline = self.proposal()
        baseline['commands'] = pc.command_declarations(self.root, cfg, baseline['observed'])
        pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        after = next(r for r in pc.inspect_context(self.root)['index']['documents'] if r['path'] == pc.COMMANDS_PATH)
        self.assertEqual(after['status'], 'historical')
        self.assertEqual(after['purpose'], 'change_record')
        self.assertEqual(after['applies_to'], ['src/special.py'])
        self.assertFalse(after['managed'])
        self.assertEqual(target.read_bytes(), before)

        # A previously reviewed generated guide has the same metadata contract.
        index = pc.inspect_context(self.root)['index']
        next(r for r in index['documents'] if r['path'] == pc.COMMANDS_PATH)['managed'] = True
        index.pop('digest'); index['digest'] = pc.digest(index)
        pc._atomic(self.root, pc.INDEX, json.dumps(index))
        cfg.defaults.quality_checks = {'test': ['python3']}
        pc.sync_commands(self.root, cfg)
        after = next(r for r in pc.inspect_context(self.root)['index']['documents'] if r['path'] == pc.COMMANDS_PATH)
        self.assertEqual(after['status'], 'historical')
        self.assertEqual(after['purpose'], 'change_record')
        self.assertEqual(after['applies_to'], ['src/special.py'])
    def test_generated_commands_budget_and_protected_target_block_before_publication(self):
        from adw_modules.data_types import KHConfig
        cfg = KHConfig()
        cfg.defaults.quality_checks = {'test': ['x' * pc.MAX_BYTES]}
        with self.assertRaisesRegex(pc.ContextBlocked, 'budget|128 KiB'):
            self.bootstrap_commands(cfg)
        cfg.defaults.quality_checks = {}
        cfg.defaults.protected_files = [pc.COMMANDS_PATH]
        with self.assertRaisesRegex(pc.ContextBlocked, 'Preserve protected'):
            self.bootstrap_commands(cfg)
        self.assertFalse((self.root / pc.INDEX).exists())
        self.assertFalse((self.root / pc.COMMANDS_PATH).exists())
        self.assertFalse((self.root / 'CLAUDE.md').exists())

    def test_package_command_declarations_use_bounded_safe_reader(self):
        from adw_modules.data_types import KHConfig
        target = self.root / 'package.json'
        for content in [b'{broken', b'\xff', b' ' * (pc.MAX_BYTES + 1), b'{"scripts": []}']:
            with self.subTest(content_prefix=content[:12]):
                target.write_bytes(content)
                with self.assertRaisesRegex(pc.ContextBlocked, 'package.json'):
                    self.bootstrap_commands(KHConfig())
                self.assertFalse((self.root / pc.INDEX).exists())
        target.unlink()
        target.symlink_to(self.root / 'src/main.py')
        with self.assertRaisesRegex(pc.ContextBlocked, 'Symlink.*package.json'):
            self.bootstrap_commands(KHConfig())

    def test_publication_never_restores_a_late_owner_edit_to_retained_instructions(self):
        target = self.root / 'CLAUDE.md'
        target.write_text('Owner instructions.\n')
        for refresh in [False, True]:
            proposal, baseline = self.proposal()
            proposal.documents = [d for d in proposal.documents if d.path != 'CLAUDE.md']
            proposal.retained_paths = ['CLAUDE.md']
            if not refresh:
                pc.publish_context(self.root, proposal, self.review(proposal), baseline)
                continue
            project = pc._publication_content
            def owner_edit(*args):
                result = project(*args)
                target.write_text('New owner instructions.\n')
                return result
            with patch.object(pc, '_publication_content', side_effect=owner_edit), \
                 self.assertRaisesRegex(pc.ContextBlocked, 'Owner changed'):
                pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        self.assertEqual(target.read_text(), 'New owner instructions.\n')

    def test_warm_commands_sync_refuses_late_document_or_index_changes(self):
        from adw_modules.data_types import KHConfig
        self.publish()
        cfg = KHConfig()
        pc.sync_commands(self.root, cfg)
        cfg.defaults.quality_checks = {'test': ['python3']}
        packet = pc._context_packet
        for path in [pc.COMMANDS_PATH, pc.INDEX]:
            target = self.root / path
            before = target.read_bytes()
            def owner_edit(*args, **kwargs):
                result = packet(*args, **kwargs)
                target.write_text('Late owner edit.\n')
                return result
            with self.subTest(path=path), patch.object(pc, '_context_packet', side_effect=owner_edit), \
                 self.assertRaisesRegex(pc.ContextBlocked, 'Owner changed'):
                pc.sync_commands(self.root, cfg)
            self.assertEqual(target.read_text(), 'Late owner edit.\n')
            target.write_bytes(before)

    def test_stale_review_cannot_publish_any_file(self):
        proposal, baseline = self.proposal()
        review = self.review(proposal)
        proposal.documents[-1].content = 'changed'
        with self.assertRaises(pc.ContextBlocked):
            pc.publish_context(self.root, proposal, review, baseline)
        self.assertFalse((self.root / 'CLAUDE.md').exists())

    def test_owner_edit_during_review_blocks_publication(self):
        proposal, baseline = self.proposal()
        (self.root / 'AGENTS.md').write_text('Owner rules')
        with self.assertRaisesRegex(pc.ContextBlocked, 'owner|changed|Preserve'):
            pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        self.assertEqual((self.root / 'AGENTS.md').read_text(), 'Owner rules')

    def test_source_path_and_traversal_and_symlink_rejected(self):
        for bad in ('src/main.py', '../outside.md', '.claude/commands/x.md'):
            proposal, baseline = self.proposal()
            proposal.documents.append(pc.ContextDocument(path=bad, content='No'))
            self.assertFalse(pc.validate_proposal(self.root, proposal, baseline)['valid'])
        (self.root / 'docs').symlink_to(self.root / 'src', target_is_directory=True)
        proposal, baseline = self.proposal()
        self.assertFalse(pc.validate_proposal(self.root, proposal, baseline)['valid'])

    def test_existing_instructions_cannot_be_rewritten(self):
        (self.root / 'CLAUDE.md').write_text('Owner scope is final')
        proposal, baseline = self.proposal()
        self.assertFalse(pc.validate_proposal(self.root, proposal, baseline)['valid'])

    def test_missing_link_and_unresolved_decisions_block(self):
        proposal, baseline = self.proposal()
        proposal.documents[-1].content = '[missing](missing.md)'
        self.assertFalse(pc.validate_proposal(self.root, proposal, baseline)['valid'])
        proposal.documents[-1].content = 'rules'
        proposal.unresolved_decisions = ['Choose a stack']
        self.assertFalse(pc.validate_proposal(self.root, proposal, baseline)['valid'])

    def test_context_owner_edit_or_added_nested_instructions_blocks_dispatch(self):
        self.publish()
        (self.root / 'src/AGENTS.md').write_text('New nested instruction')
        self.assertEqual(pc.inspect_context(self.root)['status'], 'stale')
        with self.assertRaisesRegex(pc.ContextBlocked, 'refresh'):
            pc.build_context_packet(self.root, 'task', ['src/**'])

    def test_context_index_tampering_is_not_ready(self):
        self.publish()
        index_path = self.root / pc.INDEX
        index = json.loads(index_path.read_text())
        index['architecture_status'] = 'anything'
        index_path.write_text(json.dumps(index))
        self.assertEqual(pc.inspect_context(self.root)['status'], 'stale')

    def test_interrupt_recovers_without_claiming_ready(self):
        proposal, baseline = self.proposal()
        atomic = pc._atomic
        count = 0
        def interrupt(root, rel, content):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError('interrupted')
            return atomic(root, rel, content)
        with patch.object(pc, '_atomic', side_effect=interrupt):
            with self.assertRaises(OSError):
                pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        self.assertEqual(pc.inspect_context(self.root)['status'], 'blocked')
        pc.recover_publication(self.root)
        self.assertEqual(pc.inspect_context(self.root)['status'], 'missing')
        self.assertFalse((self.root / 'CLAUDE.md').exists())

    def test_recovery_never_overwrites_intervening_owner_edit(self):
        self.test_interrupt_recovers_without_claiming_ready()
        (self.root / '.kh').mkdir(exist_ok=True)
        (self.root / 'CLAUDE.md').write_text('owner newer')
        (self.root / pc.JOURNAL).write_text(json.dumps({'schema_version': 1, 'entries': [
            {'path': 'CLAUDE.md', 'before': None, 'after': 'generated'}]}))
        with self.assertRaisesRegex(pc.ContextBlocked, 'Owner edit'):
            pc.recover_publication(self.root)
        self.assertEqual((self.root / 'CLAUDE.md').read_text(), 'owner newer')

    def test_reviewer_findings_cannot_be_approved(self):
        proposal, baseline = self.proposal()
        with self.assertRaises(pc.ContextBlocked):
            pc.publish_context(self.root, proposal, self.review(proposal, findings=['missing check']), baseline)

    def test_repair_budget_and_fresh_phase_names(self):
        proposal, baseline = self.proposal()
        revised = proposal.model_copy(deep=True)
        revised.documents[-1].content += 'Check actual results.\n'
        first_review = pc.ContextReviewOutput(status='success', proposal_digest=pc.digest(proposal.model_dump()),
                                              decision='revise', findings=['Require actual results'])
        phases = []
        @contextlib.contextmanager
        def phase(params):
            phases.append(params.name)
            yield SimpleNamespace(phase=SimpleNamespace(params=params))
        run = SimpleNamespace(repo_root=self.root, cfg=object(), phase=phase,
                              session_dir=self.root / '.kh/session', adw_id='test')
        def execute(current_run, current_phase, call):
            if current_phase.params.owner == 'project_architect':
                return revised if current_phase.params.name.endswith('_1') else proposal
            if current_phase.params.name.endswith('_0'):
                return first_review
            # A repaired context must recheck the historical finding, not only
            # approve the new proposal digest. Phase count and budget stay fixed.
            brief = json.loads(call.prompt)
            return self.review(revised, revision_context_digest=brief['revision_context_digest'],
                revision_checks=[{'finding_id': row['id'], 'disposition': 'resolved',
                    'evidence': 'Read the actual-results instruction in docs/agent-guide/rules.md'}
                    for row in brief['revision_context']['findings']])
        with patch('adw_modules.agents.resolve', return_value=SimpleNamespace(writes=None)), \
             patch('adw_modules.agents.execute', side_effect=execute) as execute_mock:
            receipt = pc.bootstrap(run, max_repairs=1)
        self.assertEqual(receipt['status'], 'ready')
        self.assertEqual(execute_mock.call_count, 4)
        self.assertEqual(len(phases), len(set(phases)))

    def test_runtime_evidence_does_not_change_inventory(self):
        proposal, baseline = self.proposal()
        runtime = self.root / 'adws/adw_data/sessions/test'
        runtime.mkdir(parents=True)
        (runtime / 'phase.md').write_text('runtime evidence')
        # The adws source directory should pre-exist a real controller session.
        self.assertEqual(pc.digest(pc._observed(self.root)), baseline['digest'])
        pc.publish_context(self.root, proposal, self.review(proposal), baseline)

    def test_custom_runtime_inventory_exclusion(self):
        custom = self.root / 'custom-evidence'
        custom.mkdir()
        observed = pc._observed(self.root, ['custom-evidence'])
        (custom / 'agent.md').write_text('runtime')
        self.assertEqual(observed, pc._observed(self.root, ['custom-evidence']))

    def test_required_context_budget_blocks_publication(self):
        proposal, baseline = self.proposal()
        proposal.documents[-1].content = 'x' * pc.MAX_BYTES
        self.assertFalse(pc.validate_proposal(self.root, proposal, baseline)['valid'])

    def test_scoped_context_does_not_apply_mobile_instructions_to_web(self):
        for area in ('web', 'mobile'):
            folder = self.root / 'apps' / area
            folder.mkdir(parents=True)
            (folder / 'AGENTS.md').write_text(area + ' rules')
        proposal, baseline = self.proposal()
        proposal.retained_paths = ['apps/web/AGENTS.md', 'apps/mobile/AGENTS.md']
        proposal.documents.append(pc.ContextDocument(path='docs/agent-guide/mobile.md',
            content='Mobile specifics', applies_to=['apps/mobile/**']))
        pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        packet = pc.build_context_packet(self.root, 'web change', ['apps/web/**'])
        paths = {d['path'] for d in packet['documents']}
        self.assertIn('apps/web/AGENTS.md', paths)
        self.assertIn('CLAUDE.md', paths)
        self.assertNotIn('apps/mobile/AGENTS.md', paths)
        self.assertNotIn('docs/agent-guide/mobile.md', paths)
        unscoped = pc.build_context_packet(self.root, 'plan', [])
        mobile = next(d for d in unscoped['documents'] if d['path'] == 'apps/mobile/AGENTS.md')
        self.assertEqual(mobile['applies_to'], ['apps/mobile/**'])

    def test_manifest_drift_invalidates_context_but_source_content_does_not(self):
        (self.root / 'package.json').write_text('{"scripts":{"test":"vitest"}}')
        self.publish()
        (self.root / 'src/main.py').write_text('ordinary implementation change')
        self.assertEqual(pc.inspect_context(self.root)['status'], 'ready')
        (self.root / 'package.json').write_text('{"scripts":{"test":"true"}}')
        self.assertEqual(pc.inspect_context(self.root)['status'], 'stale')

    def test_added_source_file_invalidates_navigation(self):
        self.publish()
        (self.root / 'src/new.py').write_text('new module')
        self.assertEqual(pc.inspect_context(self.root)['status'], 'stale')

    def test_parent_links_inside_project_are_valid_but_escape_is_rejected(self):
        proposal, baseline = self.proposal()
        proposal.documents[-1].content = '[router](../../CLAUDE.md)'
        self.assertTrue(pc.validate_proposal(self.root, proposal, baseline)['valid'])
        proposal.documents[-1].content = '[escape](../../../outside.md)'
        self.assertFalse(pc.validate_proposal(self.root, proposal, baseline)['valid'])

    def test_removed_link_target_blocks_context(self):
        proposal, baseline = self.proposal()
        proposal.documents[-1].content = '[source](../../src/main.py)'
        pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        (self.root / 'src/main.py').unlink()
        inspection = pc.inspect_context(self.root)
        self.assertEqual(inspection['status'], 'stale')
        self.assertTrue(any('target missing' in error for error in inspection['conflicts']))

    def test_reviewed_refresh_updates_unchanged_generated_docs(self):
        self.publish()
        proposal, baseline = self.proposal()
        proposal.documents[-1].content = 'Updated architecture navigation.\n'
        self.assertTrue(pc.validate_proposal(self.root, proposal, baseline)['valid'])
        pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        self.assertEqual(pc.inspect_context(self.root)['status'], 'ready')
        self.assertEqual((self.root / 'docs/agent-guide/rules.md').read_text(), proposal.documents[-1].content)

    def test_refresh_preserves_owner_edit_and_relinquishes_generated_ownership(self):
        self.publish()
        file = self.root / 'docs/agent-guide/rules.md'
        file.write_text('Owner customized rule')
        proposal, baseline = self.proposal()
        self.assertFalse(pc.validate_proposal(self.root, proposal, baseline)['valid'])
        proposal.documents[-1].content = file.read_text()
        pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        row = next(row for row in pc.inspect_context(self.root)['index']['documents'] if row['path'] == 'docs/agent-guide/rules.md')
        self.assertFalse(row['managed'])
        proposal, baseline = self.proposal()
        proposal.documents[-1].content = 'Try overwriting owner later'
        self.assertFalse(pc.validate_proposal(self.root, proposal, baseline)['valid'])

    def test_tracked_generated_directory_remains_in_project_map(self):
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True)
        for directory in ('build', 'dist'):
            (self.root / directory).mkdir()
        (self.root / 'build/source.py').write_text('tracked source')
        (self.root / 'dist/output.js').write_text('untracked output')
        subprocess.run(['git', 'add', 'build/source.py'], cwd=self.root, check=True)
        observed = pc._observed(self.root, generated_paths=['**/build/', '**/dist/'])
        self.assertIn('build/source.py', observed['files'])
        self.assertNotIn('dist/output.js', observed['files'])

    def test_non_git_build_source_is_not_blindly_discarded(self):
        (self.root / 'build').mkdir()
        (self.root / 'build/source.py').write_text('real source')
        self.assertIn('build/source.py', pc._observed(self.root)['files'])

    def test_custom_generated_paths_are_persisted_and_respected(self):
        (self.root / 'buildout').mkdir()
        (self.root / 'buildout/result.txt').write_text('generated')
        observed = pc._observed(self.root, generated_paths=['**/buildout/'])
        self.assertNotIn('buildout/result.txt', observed['files'])
        proposal, unused = self.proposal()
        baseline = {'observed': observed, 'digest': pc.digest(observed), 'generated_paths': ['**/buildout/']}
        proposal.baseline_digest = baseline['digest']
        pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        (self.root / 'buildout/next.txt').write_text('new generated output')
        inspection = pc.inspect_context(self.root)
        self.assertEqual(inspection['status'], 'ready')
        self.assertEqual(inspection['index']['generated_paths'], ['**/buildout/'])

    def test_framework_trees_are_not_inventoried_as_project_source(self):
        for rel in ('.claude/commands/kh-start.md', 'adws/adw_start.py',
                    'vendor/delegate-skills/skills/relay.mjs', '.kh-backups/abc/restore.json'):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('installed framework file\n')
        (self.root / 'vendor/own-lib').mkdir(parents=True)
        (self.root / 'vendor/own-lib/app.py').write_text('owner vendored code\n')
        files = pc.inspect_context(self.root)['observed']['files']
        self.assertIn('src/main.py', files)
        self.assertIn('vendor/own-lib/app.py', files)
        for rel in ('.claude/commands/kh-start.md', 'adws/adw_start.py',
                    'vendor/delegate-skills/skills/relay.mjs', '.kh-backups/abc/restore.json'):
            self.assertNotIn(rel, files)

    def test_retained_path_outside_tracked_documents_names_the_tracked_set(self):
        (self.root / 'README.md').write_text('owner readme\n')
        proposal, baseline = self.proposal()
        proposal.retained_paths = ['README.md']
        result = pc.validate_proposal(self.root, proposal, baseline)
        self.assertFalse(result['valid'])
        finding = next(f for f in result['findings'] if 'README.md' in f)
        self.assertIn('tracked context document', finding)
        self.assertNotIn('does not exist', finding)

    def test_inspection_is_read_only(self):
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*'))
        self.assertEqual(pc.inspect_context(self.root)['status'], 'missing')
        self.assertEqual(before, sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*')))

    def test_three_thousand_source_files_fit_the_directory_map(self):
        for number in range(3000):
            (self.root / 'src' / f'module_{number:04}.py').write_text('value = 1\n')
        self.publish()
        try:
            packet = pc.build_context_packet(self.root, 'edit main', ['src/**'])
        except pc.ContextBlocked as error:
            self.fail(f'A 3000-file project must fit: {error}')
        self.assertLessEqual(len(json.dumps(packet['project_map']).encode()), 32768)
        self.assertEqual(sum(row['file_count'] for row in packet['project_map']), 3004)
        self.assertTrue(all(len(Path(row['path']).parts) <= 2 for row in packet['project_map']))
        run = SimpleNamespace(repo_root=self.root, session_dir=self.root / '.kh/report',
                              adw_id='map', cfg=object())
        receipt = pc.write_understanding(run, packet)
        record = json.loads((self.root / receipt['receipt_path']).read_text())
        self.assertEqual(record['source_areas']['src'], 3001)

    def test_twenty_thousand_file_map_has_its_own_bound_and_omission_counts(self):
        # The fixture has four other files: two routers, one guide and main.py.
        for number in range(19996):
            folder = self.root / 'components' / f'area-{number // 10:04}'
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f'file-{number % 10}.py').write_text('value = 1\n')
        self.publish()
        inspection = pc.inspect_context(self.root)
        self.assertEqual(len(inspection['observed']['files']), 20000)
        try:
            packet = pc._context_packet(self.root, inspection, '', [])
        except pc.ContextBlocked as error:
            self.fail(f'The map must have an independent 32 KiB budget: {error}')
        encoded = len(json.dumps(packet['project_map']).encode())
        self.assertLessEqual(encoded, 32768)
        self.assertEqual(packet['map_budget']['used_bytes'], encoded)
        self.assertEqual(packet['map_budget']['limit_bytes'], 32768)
        self.assertGreater(packet['map_budget']['omitted_directories'], 0)
        self.assertEqual(len(packet['project_map']) + packet['map_budget']['omitted_directories'], 2003)
        self.assertEqual(sum(row['file_count'] for row in packet['project_map']) +
                         packet['map_budget']['omitted_files'], 20000)
        self.assertTrue(all(len(Path(row['path']).parts) <= 2 for row in packet['project_map']))
        inspection['observed']['files'].reverse()
        self.assertEqual(packet['project_map'], pc._context_packet(self.root, inspection, '', [])['project_map'])
        (self.root / 'components/area-0000/overflow.py').write_text('value = 1\n')
        with self.assertRaises(pc.ContextBlocked) as caught:
            pc.build_context_packet(self.root, '', [])
        self.assertIn('inventory exceeds 20000', str(caught.exception))
        self.assertNotIn('unreadable', str(caught.exception))

    def test_escaped_packet_over_budget_does_not_publish_an_index(self):
        proposal, baseline = self.proposal()
        proposal.documents[-1].content = '"' * 66000
        self.assertTrue(pc.validate_proposal(self.root, proposal, baseline)['valid'])
        @contextlib.contextmanager
        def phase(params):
            yield SimpleNamespace(phase=SimpleNamespace(params=params))
        run = SimpleNamespace(repo_root=self.root, cfg=object(), phase=phase,
                              session_dir=self.root / '.kh/session', adw_id='budget')
        def execute(_run, current, _call):
            return proposal if current.params.owner == 'project_architect' else self.review(proposal)
        with patch('adw_modules.agents.resolve', return_value=SimpleNamespace(writes=None)), \
             patch('adw_modules.agents.execute', side_effect=execute):
            with self.assertRaisesRegex(pc.ContextBlocked, '128 KiB|budget'):
                pc.bootstrap(run, max_repairs=0)
        for path in (pc.INDEX, pc.JOURNAL, 'CLAUDE.md', 'AGENTS.md', 'docs/agent-guide/rules.md'):
            self.assertFalse((self.root / path).exists(), path)
        self.assertEqual(pc.inspect_context(self.root)['status'], 'missing')
        self.assertEqual((self.root / 'src/main.py').read_text(), 'original = True\n')

    def test_unindexed_large_or_non_utf8_docs_are_preserved_with_warnings(self):
        (self.root / 'docs').mkdir()
        path = self.root / 'docs/spec.md'
        for data in (b'x' * 131073, b'Legacy specification: \xe9\n'):
            with self.subTest(data_kind='large' if len(data) > 131072 else 'latin-1'):
                path.write_bytes(data)
                inspection = pc.inspect_context(self.root)
                self.assertNotEqual(inspection['status'], 'blocked', inspection)
                self.publish()
                inspection = pc.inspect_context(self.root)
                self.assertEqual(inspection['status'], 'ready', inspection)
                self.assertTrue(any('docs/spec.md' in warning for warning in inspection['warnings']))
                packet = pc.build_context_packet(self.root, 'edit main', ['src/**'])
                self.assertEqual(packet['warnings'], inspection['warnings'])
                self.assertNotIn('docs/spec.md', {row['path'] for row in packet['documents']})
                self.assertEqual(path.read_bytes(), data)

    def test_unreadable_instruction_or_indexed_guide_names_the_file_and_blocks(self):
        self.publish()
        for relative in ('CLAUDE.md', 'docs/AGENTS.md', 'docs/agent-guide/rules.md'):
            path = self.root / relative
            before = path.read_bytes() if path.exists() else None
            for data in (b'x' * 131073, b'Invalid UTF-8: \xe9'):
                with self.subTest(path=relative, size=len(data)):
                    path.write_bytes(data)
                    inspection = pc.inspect_context(self.root)
                    self.assertEqual(inspection['status'], 'blocked')
                    self.assertIn(relative, '; '.join(inspection['conflicts']))
                    run = SimpleNamespace(repo_root=self.root, cfg=object())
                    with self.assertRaises(pc.ContextBlocked) as caught:
                        pc.bootstrap(run)
                    self.assertIn(relative, str(caught.exception))
                    self.assertIn('--refresh cannot', str(caught.exception))
            if before is None:
                path.unlink()
            else:
                path.write_bytes(before)

    def test_projection_still_blocks_an_unreadable_unselected_indexed_guide(self):
        from adw_modules import documentation
        for kind in ('historical', 'conditional'):
            with self.subTest(kind=kind):
                proposal, baseline = self.proposal()
                guide = proposal.documents[-1]
                guide.status = 'historical' if kind == 'historical' else 'active'
                if kind == 'conditional':
                    guide.read_when = [pc.cd.ReadCondition(id='rules-change', condition='When updating owner rules')]
                    proposal.documents.append(pc.ContextDocument(path=pc.cd.CATALOG_PATH, content=''))
                self.assertTrue(pc.validate_proposal(self.root, proposal, baseline)['valid'])
                pc.publish_context(self.root, proposal, self.review(proposal), baseline)
                receipt = {'decision': 'required', 'candidate_sha256': 'fixture', 'documents': [],
                           'prior_index_digest': pc.digest(pc._read(self.root, pc.INDEX))}
                path = self.root / 'docs/agent-guide/rules.md'
                path.write_bytes(b'x' * 131073)
                with patch.object(documentation.quality, 'source_fingerprint', return_value='fixture'):
                    with self.assertRaisesRegex(pc.ContextBlocked, 'docs/agent-guide/rules.md'):
                        documentation.context_projection(SimpleNamespace(repo_root=self.root, adw_id='projection'), receipt)
                path.write_text(guide.content)

    def test_optional_doc_skipping_does_not_hide_symlinks_or_nonregular_files(self):
        (self.root / 'docs').mkdir()
        path = self.root / 'docs/unsafe.md'
        path.symlink_to(self.root / 'src/main.py')
        self.assertEqual(pc.inspect_context(self.root)['status'], 'blocked')
        path.unlink()
        _os.mkfifo(path)
        inspection = pc.inspect_context(self.root)
        self.assertEqual(inspection['status'], 'blocked')
        self.assertIn('docs/unsafe.md', '; '.join(inspection['conflicts']))

    def test_default_virtual_environments_are_excluded_without_losing_existing_patterns(self):
        import yaml
        for relative in ('.venv/lib/site.py', 'apps/web/.venv/lib/site.py'):
            path = self.root / relative
            path.parent.mkdir(parents=True)
            path.write_text('generated dependency\n')
        defaults = pc.ConfigDefaults().generated_paths
        self.assertIn('**/.venv/', defaults)
        template = yaml.safe_load((ROOT / '.claude/skills/kh/templates/kh.config.yaml').read_text())
        self.assertEqual(template['defaults']['generated_paths'], defaults)
        files = pc.inspect_context(self.root)['observed']['files']
        self.assertNotIn('.venv/lib/site.py', files)
        self.assertNotIn('apps/web/.venv/lib/site.py', files)
        self.assertIn('src/main.py', files)
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True)
        subprocess.run(['git', 'add', '.venv/lib/site.py'], cwd=self.root, check=True)
        self.assertIn('.venv/lib/site.py', pc.inspect_context(self.root)['observed']['files'])


class SourceBindingTests(unittest.TestCase):
    setUp = ContextTests.setUp
    tearDown = ContextTests.tearDown
    proposal = ContextTests.proposal
    review = ContextTests.review
    def scoped(self):
        (self.root / 'src/reports.py').write_text('report = 1\n')
        (self.root / 'src/unrelated.py').write_text('unrelated = 1\n')
        proposal, baseline = self.proposal()
        proposal.documents.append(pc.ContextDocument(path='docs/agent-guide/testing.md',
            content='Reports return 1.\n', applies_to=['src/reports*.py']))
        pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        return 'docs/agent-guide/testing.md'

    def test_scoped_source_changes_are_detected_without_git_and_outside_changes_are_ignored(self):
        guide = self.scoped()
        self.assertFalse((self.root / '.git').exists())
        before = (self.root / pc.INDEX).read_bytes()
        (self.root / 'src/unrelated.py').write_text('unrelated = 2\n')
        self.assertEqual(pc.inspect_context(self.root)['status'], 'ready')
        (self.root / 'src/reports.py').write_text('report = 2\n')
        result = pc.inspect_context(self.root)
        self.assertEqual(result['status'], 'stale')
        self.assertIn(guide, str(result['conflicts']))
        self.assertIn('src/reports.py', str(result['conflicts']))
        self.assertEqual((self.root / pc.INDEX).read_bytes(), before)
        (self.root / 'src/reports.py').write_text('report = 1\n')
        self.assertEqual(pc.inspect_context(self.root)['status'], 'ready')
        index = json.loads(before)
        next(row for row in index['documents'] if row['path'] == guide).pop('source_digests', None)
        index.pop('digest'); index['digest'] = pc.digest(index)
        pc._atomic(self.root, pc.INDEX, json.dumps(index))
        self.assertEqual(pc.inspect_context(self.root)['status'], 'stale')

    def test_general_guides_keep_structure_only_freshness(self):
        proposal, baseline = self.proposal()
        proposal.documents.append(pc.ContextDocument(path='docs/agent-guide/overview.md',
            content='General navigation.\n', applies_to=['**']))
        pc.publish_context(self.root, proposal, self.review(proposal), baseline)
        (self.root / 'src/main.py').write_text('original = False\n')
        self.assertEqual(pc.inspect_context(self.root)['status'], 'ready')
        (self.root / 'src/new.py').write_text('new = True\n')
        self.assertEqual(pc.inspect_context(self.root)['status'], 'stale')

    def test_retaining_a_stale_guide_does_not_approve_its_current_source(self):
        guide = self.scoped()
        (self.root / 'src/reports.py').write_text('report = 2\n')
        proposal, baseline = self.proposal()
        proposal.retained_paths = [guide]
        validation = pc.validate_proposal(self.root, proposal, baseline)
        self.assertFalse(validation['valid'])
        self.assertIn(guide, str(validation['findings']))

    def test_source_drift_after_context_review_blocks_publication(self):
        self.scoped()
        proposal, baseline = self.proposal()
        proposal.documents.append(pc.ContextDocument(path='docs/agent-guide/testing.md',
            content='Reports return 1.\n', applies_to=['src/reports*.py']))
        self.assertTrue(pc.validate_proposal(self.root, proposal, baseline)['valid'])
        review = self.review(proposal)
        (self.root / 'src/reports.py').write_text('report = 2\n')
        with self.assertRaisesRegex(pc.ContextBlocked, '[Ss]ource'):
            pc.publish_context(self.root, proposal, review, baseline)

    def test_source_hashing_refuses_links_pipes_oversize_and_replacement(self):
        self.assertTrue(hasattr(pc, '_source_file_digest'), 'Source hashing needs the safe bounded reader')
        target = self.root / 'src/main.py'
        link = self.root / 'src/link.py'; link.symlink_to(target)
        pipe = self.root / 'src/pipe.py'; _os.mkfifo(pipe)
        for relative, limit in [('src/link.py', 1024), ('src/pipe.py', 1024), ('src/main.py', 2)]:
            with self.subTest(relative=relative), self.assertRaises(pc.ContextBlocked):
                pc._source_file_digest(self.root, relative, limit)
        original = _os.fstat
        calls = 0
        def replace(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                replacement = self.root / 'src/replacement'
                replacement.write_bytes(target.read_bytes())
                replacement.replace(target)
            return original(fd)
        with patch.object(pc.os, 'fstat', side_effect=replace), self.assertRaisesRegex(pc.ContextBlocked, 'changed'):
            pc._source_file_digest(self.root, 'src/main.py', 1024)

    def test_inspection_refuses_nonregular_or_oversize_scoped_source(self):
        self.scoped()
        target = self.root / 'src/reports.py'
        original = target.read_bytes()
        index = (self.root / pc.INDEX).read_bytes()
        for kind in ('symlink', 'fifo', 'oversize'):
            with self.subTest(kind=kind):
                target.unlink()
                if kind == 'symlink':
                    target.symlink_to(self.root / 'src/unrelated.py')
                elif kind == 'fifo':
                    _os.mkfifo(target)
                else:
                    with target.open('wb') as stream:
                        stream.truncate(pc.ConfigDefaults().snapshot_max_bytes + 1)
                result = pc.inspect_context(self.root)
                self.assertEqual(result['status'], 'blocked')
                self.assertIn('src/reports.py', str(result['conflicts']))
                self.assertEqual((self.root / pc.INDEX).read_bytes(), index)
                target.unlink(); target.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
