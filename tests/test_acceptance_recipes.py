"""Concrete harness assertions plus recipe output through the real adapter boundary."""
import json
from pathlib import Path
import sys
import unittest
from tests import test_verification_evidence as fixtures
from adw_modules import verification_evidence as ve
from adw_modules.data_types import VerificationAdapter


class AcceptanceRecipeTests(unittest.TestCase):
    setUp=fixtures.AdapterCommandTests.setUp

    def execute(self, mode='good'):
        adws=Path(__file__).resolve().parents[1]/'.claude/skills/kh/templates/adws'
        script='import sys;sys.path.insert(0,'+repr(str(adws))+')\n'+r'''
import csv,io,json,os,sqlite3
from pathlib import Path
from adw_modules.acceptance_recipe import emit
mode=MODE
# Real independent outcome assertion; pass metadata is created only afterwards.
assert list(csv.DictReader(io.StringIO('id,value\n1,2\n'))) == [{'id':'1','value':'2'}]
try:
    db=sqlite3.connect('file:/this-fixture-directory-does-not-exist/db?mode=rw',uri=True)
except sqlite3.OperationalError:
    pass
else:
    db.close();raise AssertionError('Unavailable database unexpectedly connected')
r,w=os.pipe();os.close(w);reader=os.fdopen(r,'rb')
if mode!='open-reader':reader.close()
output=Path(os.environ['KH_VERIFICATION_RESULT']);(output.parent/'capture.txt').write_text('Verified CSV content and expected unavailable database')
cases=[{'name':'download contents','requirement':'CSV contains actual rows','passed':True,'assertions':1,'surfaces':['src/Button.tsx']},
       {'name':'database unavailable','requirement':'Unavailable DB fails explicitly','passed':True,'assertions':1,'surfaces':['src/Button.tsx']}]
if mode=='empty':cases=[]
if mode=='failed-assertion':cases[0]['passed']=False
if mode=='no-assertions':cases[0]['assertions']=0
cleanup={'confirmed':mode!='unknown-cleanup','active_children':0,'open_readers':int(not reader.closed),'open_connections':0}
try:
    code=emit(candidate_sha256='b'*64 if mode=='stale' else os.environ['KH_CANDIDATE_SHA256'],
        discovered_tests=2,cases=cases,evidence=['capture.txt'],cleanup=cleanup,unresolved_differences=1 if mode=='visual-difference' else 0)
finally:reader.close()
raise SystemExit(code)
'''
        script=script.replace('mode=MODE','mode='+repr(mode))
        self.run.cfg.defaults.verification_adapters=[VerificationAdapter(name='recipe',kind='e2e',
            applicable_paths=['src/**'],argv=[sys.executable,'-S','-c',script])]
        changed=['src/Button.tsx']
        if mode=='partial-coverage':
            (self.root/'src/Other.tsx').write_text('another affected component')
            changed.append('src/Other.tsx')
        return ve.run_adapters(self.run,changed)

    def test_actual_assertions_and_closed_resources_produce_candidate_bound_evidence(self):
        result=self.execute();self.assertTrue(result.passed,result.failures)
        report=json.loads(next(self.handoff.rglob('recipe-cases.json')).read_text())
        self.assertEqual(len(report['cases']),2)
        self.assertTrue(report['cleanup']['confirmed'])
        self.assertEqual(report['cleanup']['open_readers'],0)

    def test_empty_failed_assertion_no_assertions_stale_and_unconfirmed_cleanup_fail(self):
        for mode in ('empty','failed-assertion','no-assertions','stale','unknown-cleanup','open-reader','partial-coverage','visual-difference'):
            with self.subTest(mode=mode):
                result=self.execute(mode);self.assertFalse(result.passed)
                self.assertTrue(result.failures)
                self.assertTrue(any(path.endswith('recipe-cases.json') for path in result.artifacts),
                    'Negative cases must execute the real harness, not fail due to an import/setup error')

    def test_formatter_does_not_overwrite_existing_evidence(self):
        from adw_modules.acceptance_recipe import emit
        output=self.root/'result.json';output.write_text('owner record')
        env={'KH_VERIFICATION_RESULT':str(output),'KH_CANDIDATE_SHA256':'a'*64,'KH_CHANGED_PATHS':'["src/Button.tsx"]'}
        with self.assertRaises(FileExistsError):
            emit(candidate_sha256='a'*64,discovered_tests=1,
                cases=[{'name':'case','requirement':'requirement','passed':True,'assertions':1,'surfaces':['src/Button.tsx']}],
                evidence=[],cleanup={'confirmed':True,'active_children':0,'open_readers':0,'open_connections':0},unresolved_differences=0,environ=env)
        self.assertEqual(output.read_text(),'owner record')
