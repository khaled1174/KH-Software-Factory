"""Methodology metrics derive from unique controller attempts and typed receipts."""
import unittest
from tests import test_project as fixture
from adw_modules import project


class MethodologyMetricsTests(unittest.TestCase):
    def metrics(self, tasks, histories):
        from adw_modules.methodology_metrics import metrics
        return metrics({'tasks':tasks},histories)

    def test_repeated_running_events_are_one_attempt_and_retries_are_distinct(self):
        task={'id':'T1','status':'completed','run_id':'r2','receipt':{'cost':{'usd':2.0,'complete':True},
            'execution':{'code_repairs':1},'documentation':{'status':'published'},
            'handoffs':[{'phase':'review_1_evidence1_scope'},{'phase':'review_1_evidence1_correctness'}]}}
        history={'T1':[{'event':'running','run_id':'r1'}, {'event':'running','run_id':'r1'},
            {'event':'blocked','run_id':'r1'},
            {'event':'attempt_summary','run_id':'r1','detail':{'cost':{'usd':1.25,'complete':True},'execution':{'code_repairs':2},'handoffs':[]}},
            {'event':'running','run_id':'r2'}, {'event':'completed','run_id':'r2','detail':task['receipt']}]}
        result=self.metrics([task],history)
        self.assertEqual(result['attempts'],2)
        self.assertEqual(result['first_attempt_acceptance']['accepted'],0)
        self.assertEqual(result['repairs']['code_observed'],3)
        self.assertEqual(result['repairs']['evidence_waves_observed'],1)
        self.assertEqual(result['cost']['recorded_usd'],3.25)
        self.assertTrue(result['cost']['complete'])
        self.assertEqual(result['documentation']['assessed_accepted_tasks'],1)

    def test_missing_legacy_data_is_unknown_not_zero_or_success(self):
        tasks=[{'id':'T1','status':'completed','run_id':'old','receipt':{}}]
        result=self.metrics(tasks,{})
        self.assertIsNone(result['attempts'])
        self.assertIsNone(result['first_attempt_acceptance']['rate_percent'])
        self.assertFalse(result['cost']['complete'])
        self.assertEqual(result['documentation']['unrecorded_accepted_tasks'],1)
        self.assertEqual(result['repairs']['unknown_attempts'],1)

    def test_partial_cost_and_active_attempt_never_change_acceptance(self):
        tasks=[{'id':'T1','status':'blocked','run_id':'r1','receipt':None},
               {'id':'T2','status':'running','run_id':'r2','receipt':None}]
        history={'T1':[{'event':'running','run_id':'r1'},{'event':'blocked','run_id':'r1'},
            {'event':'attempt_summary','run_id':'r1','detail':{'cost':{'usd':1000000,'complete':False}}}],
            'T2':[{'event':'running','run_id':'r2'}]}
        result=self.metrics(tasks,history)
        self.assertEqual(result['attempts'],2)
        self.assertEqual(result['cost']['recorded_usd'],1000000)
        self.assertFalse(result['cost']['complete'])
        self.assertEqual(result['cost']['unknown_attempts'],2)
        self.assertEqual([t['status'] for t in tasks],['blocked','running'])
        self.assertIsNone(result['documentation']['coverage_percent'])

    def test_real_project_records_failed_attempt_spend_and_projects_metrics(self):
        case=fixture.ProjectTests();case.setUp()
        try:
            case.init();project.begin_task(case.root,'T1','first')
            project.fail_task(case.root,'T1','first','Real failed attempt',attempt_summary={
                'cost':{'usd':4.5,'complete':True},'execution':{'code_repairs':0},'handoffs':[]})
            snapshot=project.status(case.root)
            self.assertEqual(snapshot['methodology_metrics']['attempts'],1)
            self.assertEqual(snapshot['methodology_metrics']['cost']['recorded_usd'],4.5)
            self.assertEqual(snapshot['tasks'][0]['status'],'blocked')
            self.assertIn('Methodology observations',(case.root/'.kh/PROJECT_STATUS.md').read_text())
        finally:case.tearDown()

    def test_optional_null_documentation_is_unrecorded(self):
        task={'id':'T1','status':'completed','run_id':'old','receipt':{'documentation':None}}
        result=self.metrics([task],{})
        self.assertEqual(result['documentation']['unrecorded_accepted_tasks'],1)

    def test_unrepresentable_price_stays_unknown_without_stopping(self):
        receipt={'cost':{'usd':10**1000,'complete':True}}
        task={'id':'T1','status':'completed','run_id':'r1','receipt':receipt}
        result=self.metrics([task],{'T1':[{'event':'running','run_id':'r1'},
            {'event':'completed','run_id':'r1','detail':receipt}]})
        self.assertFalse(result['cost']['complete'])
        self.assertEqual(result['cost']['unknown_attempts'],1)
