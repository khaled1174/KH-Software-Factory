"""Configured build instructions must survive the real documentation gate."""
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import evaluate_documentation as evaluation
from adw_modules import project_context as pc


class DocumentationCommandRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = evaluation.evaluate()
        cls.rows = {row['id']: row for row in cls.result['cases']}

    def test_declared_build_cannot_be_reported_absent(self):
        gate = self.rows['bad-build-absent']['gates']['proposal_gate']
        self.assertFalse(gate['passed'], gate)
        self.assertIn('build', str(gate).lower())
        self.assertIn(evaluation.GUIDE, str(gate))

    def test_operating_guide_cannot_omit_required_build_instructions(self):
        gate = self.rows['bad-build-omitted']['gates']['proposal_gate']
        self.assertFalse(gate['passed'], gate)
        self.assertIn('build', str(gate).lower())
        self.assertIn(evaluation.GUIDE, str(gate))

    def test_frozen_correct_guides_remain_accepted(self):
        correct = [row for row in self.result['cases'] if not row['defective']]
        self.assertEqual(len(correct), 6)
        for row in correct:
            with self.subTest(guide=row['id']):
                self.assertTrue(row['gates']['proposal_gate']['passed'], row['gates'])
        self.assertEqual(self.result['provider_calls'], 0)


class ConfiguredBuildGuideTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kh-command-guide-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.commands = {'quality_checks': {'build': ['python3', '-m', 'compileall', 'pkg']},
                         'required_quality_checks': ['build']}
        self.prefix = 'Run: python3 -m pkg\nTests: python3 -m unittest\n'

    def validate(self, content, **metadata):
        document = pc.ContextDocument(path='docs/agent-guide/operations.md', content=content, **metadata)
        return pc.validate_command_guide(self.root, document, self.commands)

    def test_configured_command_is_sufficient(self):
        self.validate(self.prefix + 'Build: `python3 -m compileall pkg`.\n')

    def test_real_make_target_is_sufficient(self):
        (self.root / 'Makefile').write_text('build:\n\tpython3 -m compileall pkg\n')
        self.validate(self.prefix + 'Build: make build.\n')

    def test_make_command_without_target_is_not_instructions(self):
        (self.root / 'Makefile').write_text('test:\n\tpython3 -m unittest\n')
        with self.assertRaisesRegex(pc.ContextBlocked, 'build'):
            self.validate(self.prefix + 'Build: make build.\n')

    def test_make_variable_is_not_a_target(self):
        (self.root / 'Makefile').write_text('build := debug\n')
        with self.assertRaisesRegex(pc.ContextBlocked, 'build'):
            self.validate(self.prefix + 'Build: make build.\n')

    def test_command_catalog_reference_is_sufficient(self):
        self.validate(self.prefix + 'Build: see [commands](commands.md).\n')

    def test_wrong_command_prefix_does_not_satisfy_build(self):
        with self.assertRaisesRegex(pc.ContextBlocked, 'build'):
            self.validate(self.prefix + 'Build: python3 -m compileall pkg-old.\n')

    def test_canonical_shell_quoting_preserves_space_in_argument(self):
        self.commands['quality_checks']['build'][-1] = 'path with spaces'
        self.validate(self.prefix + "Build: python3 -m compileall 'path with spaces'.\n")

    def test_unquoted_space_changes_argv_and_is_rejected(self):
        self.commands['quality_checks']['build'][-1] = 'path with spaces'
        with self.assertRaisesRegex(pc.ContextBlocked, 'build'):
            self.validate(self.prefix + 'Build: python3 -m compileall path with spaces.\n')

    def test_comma_filename_suffix_does_not_satisfy_build(self):
        with self.assertRaisesRegex(pc.ContextBlocked, 'build'):
            self.validate(self.prefix + 'Build: python3 -m compileall pkg,backup\n')

    def test_direct_command_does_not_require_utf8_makefile(self):
        (self.root / 'Makefile').write_bytes(b'# \xff\n')
        self.validate(self.prefix + 'Build: python3 -m compileall pkg.\n')

    def test_catalog_reference_does_not_require_utf8_makefile(self):
        (self.root / 'Makefile').write_bytes(b'# \xff\n')
        self.validate(self.prefix + 'Build: see [commands](commands.md).\n')

    def test_direct_command_does_not_read_oversized_makefile(self):
        (self.root / 'Makefile').write_bytes(b'#' * (pc.MAX_BYTES + 1))
        self.validate(self.prefix + 'Build: python3 -m compileall pkg.\n')

    def test_catalog_reference_does_not_read_oversized_makefile(self):
        (self.root / 'Makefile').write_bytes(b'#' * (pc.MAX_BYTES + 1))
        self.validate(self.prefix + 'Build: see [commands](commands.md).\n')

    def test_apostrophe_in_root_preserves_canonical_command(self):
        from adw_modules.data_types import ConfigDefaults
        self.root = self.root / "John's Project"
        self.root.mkdir()
        argv = ['python3', str(self.root / 'build.py')]
        defaults = ConfigDefaults(quality_checks={'build': argv}, required_quality_checks=['build'])
        self.commands = pc.command_declarations(self.root, SimpleNamespace(defaults=defaults), {'manifests': {}})
        self.validate(self.prefix + 'Build: ' + shlex.join(argv) + '\n')

    def test_project_absolute_command_uses_existing_root_normalization(self):
        from adw_modules.data_types import ConfigDefaults
        argv = ['python3', str(self.root / 'build.py')]
        defaults = ConfigDefaults(quality_checks={'build': argv}, required_quality_checks=['build'])
        self.commands = pc.command_declarations(self.root, SimpleNamespace(defaults=defaults), {'manifests': {}})
        self.validate(self.prefix + 'Build: python3 ' + str(self.root / 'build.py') + '\n')

    def test_filename_suffix_does_not_satisfy_build(self):
        with self.assertRaisesRegex(pc.ContextBlocked, 'build'):
            self.validate(self.prefix + 'Build: python3 -m compileall pkg.old.\n')

    def test_unrelated_feature_guide_needs_no_build_section(self):
        self.validate('# Sorting\nRecords sort by timestamp.\n')

    def test_historical_record_is_not_a_current_operating_guide(self):
        self.validate(self.prefix + 'Build was not configured.\n', purpose='change_record', status='historical')

    def test_optional_build_does_not_become_an_owner_requirement(self):
        self.commands['required_quality_checks'] = []
        self.validate(self.prefix)

    def test_unknown_build_does_not_imply_other_manifests_are_empty(self):
        self.commands['quality_checks'] = {}
        self.validate(self.prefix + 'The build is defined in a different manifest.\n')

    def test_architect_cannot_publish_the_same_build_omission(self):
        observed = pc.inspect_context(self.root)['observed']
        baseline = {'observed': observed, 'digest': pc.digest(observed), 'commands': self.commands}
        proposal = pc.ProjectContextProposal(status='success', baseline_digest=baseline['digest'],
            documents=[pc.ContextDocument(path='CLAUDE.md', content='Project instructions.\n'),
                       pc.ContextDocument(path='AGENTS.md', content='Project instructions.\n'),
                       pc.ContextDocument(path='docs/agent-guide/operations.md', content=self.prefix)])
        result = pc.validate_proposal(self.root, proposal, baseline)
        self.assertFalse(result['valid'], result)
        self.assertEqual(len(result['findings']), 1, result)
        self.assertIn('docs/agent-guide/operations.md', result['findings'][0])
        self.assertIn('python3 -m compileall pkg', result['findings'][0])

    def test_architect_accepts_reference_to_derived_command_catalog(self):
        observed = pc.inspect_context(self.root)['observed']
        baseline = {'observed': observed, 'digest': pc.digest(observed), 'commands': self.commands}
        proposal = pc.ProjectContextProposal(status='success', baseline_digest=baseline['digest'],
            documents=[pc.ContextDocument(path='CLAUDE.md', content='Project instructions.\n'),
                       pc.ContextDocument(path='AGENTS.md', content='Project instructions.\n'),
                       pc.ContextDocument(path='docs/agent-guide/operations.md', content=
                           self.prefix + 'Build: see [commands](commands.md).\n')])
        result = pc.validate_proposal(self.root, proposal, baseline)
        self.assertTrue(result['valid'], result)


if __name__ == '__main__':
    unittest.main()
