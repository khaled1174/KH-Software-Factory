"""Documentation impact is approved scope, never a permission grant."""
from pathlib import Path
from types import SimpleNamespace
import unittest
import yaml
from tests import test_planning_review as helpers
from adw_modules import planning, scope
from adw_modules.data_types import KHConfig


class DocumentationPlanningTests(unittest.TestCase):
    setUp = helpers.PlanningTests.setUp
    plan = helpers.PlanningTests.plan
    review = staticmethod(helpers.PlanningTests.review)
    execute = helpers.PlanningTests.execute

    def doc_plan(self, decision='required', paths=None, reason='Public behavior changed; update the current guide.'):
        from adw_modules.documentation import DocumentationPlan
        return DocumentationPlan(decision=decision, paths=paths if paths is not None else ['docs/agent-guide/widget.md'], reason=reason)

    def test_required_impact_cannot_be_missing(self):
        with self.assertRaisesRegex(planning.PlanningBlocked, 'documentation'):
            planning.validate_plan(self.plan(), self.contract, require_documentation=True)

    def test_approved_paths_cover_exact_documentation_targets(self):
        plan = self.plan().model_copy(update={'documentation': self.doc_plan(),
            'allowed_paths':['src/widget.py','docs/agent-guide/widget.md']})
        contract = self.contract.model_copy(update={'allowed_paths':plan.allowed_paths})
        planning.validate_plan(plan, contract, require_documentation=True)
        with self.assertRaisesRegex(planning.PlanningBlocked, 'documentation'):
            planning.validate_plan(plan.model_copy(update={'allowed_paths':['src/widget.py']}), self.contract)

    def test_source_control_glob_and_traversal_are_not_document_targets(self):
        for path in ('src/widget.py', '.claude/skills/x.md', 'AGENTS.md', 'docs/**/*.md', 'app_docs/../x.md'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.doc_plan(paths=[path])

    def test_not_needed_has_reason_and_no_paths(self):
        for updates in ({'reason':' '}, {'paths':['app_docs/work.md']}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.doc_plan(**{'decision':'not_needed', 'paths':[], **updates})

    def test_legacy_omission_is_compatible_and_new_template_enabled(self):
        self.assertFalse(KHConfig().execution.documentation.enabled)
        planning.validate_plan(self.plan(), self.contract)
        template = Path(__file__).resolve().parents[1]/'.claude/skills/kh/templates/kh.config.yaml'
        self.assertTrue(yaml.safe_load(template.read_text())['execution']['documentation']['enabled'])

    def test_revision_cannot_drop_required_documentation(self):
        from adw_modules.documentation import validate_documentation_plan
        old = self.doc_plan()
        plan = self.plan().model_copy(update={'documentation':self.doc_plan(decision='not_needed',paths=[])})
        with self.assertRaisesRegex(ValueError, 'preserve'):
            validate_documentation_plan(plan, self.contract, previous=old)

    def test_enabled_controller_blocks_unassessed_plan_before_approval(self):
        self.run.cfg.execution.documentation.enabled = True
        self.run.repo_root = Path(self.temp.name)
        def callback(run, phase, call):
            return self.plan() if call.output_type is planning.ReviewedPlanOutput else self.review(call)
        with self.assertRaisesRegex(planning.PlanningBlocked, 'documentation'):
            self.execute(callback, budget=0)
        self.assertFalse((Path(self.run.session_dir)/'planning/approval.json').exists())
