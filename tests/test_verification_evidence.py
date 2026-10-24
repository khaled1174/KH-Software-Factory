"""Adapters prove coverage, not just exit zero."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.claude/skills/kh/templates/adws'))
from adw_modules import verification_evidence as ve
class VerificationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'capture.png').write_bytes(b'fixture evidence')
        self.result={'schema_version':1,'candidate_sha256':'a'*64,'status':'passed',
            'affected_surfaces':['src/Button.tsx'],'covered_surfaces':['src/Button.tsx'],
            'evidence':['capture.png'],'discovered_tests':1,'executed_tests':1,'unresolved_differences':0}
    def tearDown(self):self.temp.cleanup()
    def check(self):return ve.validate_result(self.result,self.root,'a'*64,['src/Button.tsx'])
    def test_real_coverage_passes(self):self.assertTrue(self.check().passed)
    def test_empty_story_or_no_discovery_fails(self):
        self.result['discovered_tests']=0;self.assertFalse(self.check().passed)
    def test_preview_exit_zero_does_not_accept_differences(self):
        self.result['unresolved_differences']=1;self.assertFalse(self.check().passed)
    def test_unknown_status_fails(self):
        self.result['status']='preview';self.assertFalse(self.check().passed)
    def test_stale_candidate_fails(self):
        self.result['candidate_sha256']='b'*64;self.assertFalse(self.check().passed)
    def test_applicable_check_cannot_skip(self):
        self.result['status']='not_applicable';self.assertFalse(self.check().passed)
    def test_missing_coverage_and_empty_evidence_fail(self):
        self.result['covered_surfaces']=[];self.assertFalse(self.check().passed)
        self.result['covered_surfaces']=['src/Button.tsx'];(self.root/'capture.png').write_bytes(b'')
        self.assertFalse(self.check().passed)
    def test_symlink_evidence_fails(self):
        (self.root/'alias.png').symlink_to(self.root/'capture.png')
        self.result['evidence']=['alias.png'];self.assertFalse(self.check().passed)
    def test_no_applicable_ui_is_controller_decision(self):
        self.assertEqual(ve.affected_surfaces(['backend/job.py'],['src/**']),[])
    def test_coverage_types_cannot_coerce(self):
        self.result['executed_tests']='1';self.assertFalse(self.check().passed)

class AdapterCommandTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from adw_modules.data_types import ConfigDefaults, KHConfig
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.handoff = self.root / 'adws/adw_data/sessions/run/handoff'
        self.handoff.mkdir(parents=True)
        self.run = SimpleNamespace(repo_root=self.root, context_handoff_dir=self.handoff,
            cfg=KHConfig(defaults=ConfigDefaults(), agents=[]), phases=[])
        (self.root / 'src').mkdir()
        (self.root / 'src/Button.tsx').write_text('original')

    def configure(self, extra=''):
        from adw_modules.data_types import VerificationAdapter
        script = '''import os,json
from pathlib import Path
p=Path(os.environ['KH_VERIFICATION_RESULT'])
(p.parent/'capture.png').write_bytes(b'fixture capture')
r={'schema_version':1,'candidate_sha256':os.environ['KH_CANDIDATE_SHA256'],
   'status':'passed','affected_surfaces':json.loads(os.environ['KH_CHANGED_PATHS']),
   'covered_surfaces':json.loads(os.environ['KH_CHANGED_PATHS']),
   'evidence':['capture.png'],'discovered_tests':1,'executed_tests':1,'unresolved_differences':0}
''' + extra + "\np.write_text(json.dumps(r))\n"
        self.run.cfg.defaults.verification_adapters = [VerificationAdapter(
            name='ui', kind='visual', applicable_paths=['src/**'], argv=[sys.executable, '-c', script])]

    def test_real_subprocess_green_keeps_evidence(self):
        self.configure()
        result = ve.run_adapters(self.run, ['src/Button.tsx'])
        self.assertTrue(result.passed, result.failures)
        self.assertEqual(len(result.checks), 1)
        self.assertTrue(any(p.endswith('capture.png') for p in result.artifacts))
        self.assertTrue(all(Path(p).is_file() for p in result.artifacts))

    def test_real_subprocess_exit_zero_with_diff_fails(self):
        self.configure("r['unresolved_differences']=1")
        result = ve.run_adapters(self.run, ['src/Button.tsx'])
        self.assertFalse(result.passed)
        self.assertIn('visual_differences', result.failures[0])
        self.assertTrue(any(p.endswith('capture.png') for p in result.artifacts))

    def test_real_subprocess_source_mutation_fails(self):
        self.configure("Path('src/Button.tsx').write_text('mutated')")
        result = ve.run_adapters(self.run, ['src/Button.tsx'])
        self.assertFalse(result.passed)
        self.assertIn('VERIFICATION_SOURCE_CHANGED', result.failures[0])

    def test_nonapplicable_adapter_does_not_launch(self):
        self.configure("raise RuntimeError('must not launch')")
        result = ve.run_adapters(self.run, ['backend/job.py'])
        self.assertTrue(result.passed)
        self.assertEqual(result.checks, [])

    def test_empty_adapters_do_not_imply_ui_coverage(self):
        result = ve.run_adapters(self.run, ['src/Button.tsx'])
        self.assertTrue(result.passed)
        self.assertEqual(result.checks, [])

    def test_adapter_config_rejects_unsafe_names_and_duplicate_names(self):
        from adw_modules.data_types import VerificationAdapter, ConfigDefaults
        kwargs = dict(name='ui',kind='visual',applicable_paths=['src/**'],argv=['runner'])
        for update in ({'name':'../ui'}, {'argv':[]}, {'timeout_seconds':True},
                       {'applicable_paths':['../src/**']}, {'kind':'preview'}):
            with self.subTest(update=update):
                with self.assertRaises(ValueError):
                    VerificationAdapter(**{**kwargs, **update})
        with self.assertRaises(ValueError):
            ConfigDefaults(verification_adapters=[VerificationAdapter(**kwargs),VerificationAdapter(**kwargs)])
