"""Controller evidence paths survive JSON escaping at the worker boundary."""
import json
import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import permissions, workspace
from adw_modules.data_types import (AgentCall, AgentConfig, GenericOutput, KHConfig,
                                   PlanOutput, PromptEngineering)


class WorkspaceInputPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kh-input-paths-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.agent = AgentConfig(name='plan_reviewer', writes=[],
            prompt_engineering=PromptEngineering(system='system.md', user='user.md'))
        self.phase = SimpleNamespace(phase_id='review', params=SimpleNamespace(owner='plan_reviewer'))
        self.external = str(self.base / 'external-evidence.md')
        self.binding = '0123456789abcdef' * 4

    def owner(self, name='project \u0623\u0648'):
        source = self.base / name
        source.mkdir()
        handoff = source / 'adws/adw_data/sessions/run/context_handoff'
        evidence = handoff / 'architect/project_architect/evidence.md'
        evidence.parent.mkdir(parents=True)
        evidence.write_text('Independent architect evidence.\n', encoding='utf-8')
        run = SimpleNamespace(repo_root=source, cfg=KHConfig(), adw_id='run',
                              context_handoff_dir=handoff)
        return run, evidence

    def test_json_review_brief_rebases_real_evidence_without_changing_binding(self):
        run, evidence = self.owner()
        proposal = {'artifacts': [str(evidence)],
                    'artifact_declarations': [{'path': str(evidence), 'bytes': evidence.stat().st_size}]}
        brief = {'proposal_digest': self.binding, 'proposal': proposal,
                 'external': self.external, 'types': [True, False, None, 2, 2.5],
                 'escaped_text': 'A quote: " and a backslash: \\'}
        with workspace.AgentWorkspace(run, self.phase, self.agent) as isolated:
            worker_evidence = isolated.root / evidence.relative_to(run.repo_root)
            self.assertEqual(worker_evidence.read_bytes(), evidence.read_bytes())
            for ensure_ascii in (True, False):
                with self.subTest(ensure_ascii=ensure_ascii):
                    prompt = json.dumps(brief, ensure_ascii=ensure_ascii)
                    call = AgentCall(output_type=GenericOutput, prompt=prompt)
                    mapped = isolated.remap_input(call)
                    actual = json.loads(mapped.prompt)
                    expected = {**brief, 'proposal': {
                        'artifacts': [str(worker_evidence)],
                        'artifact_declarations': [{'path': str(worker_evidence),
                                                   'bytes': evidence.stat().st_size}]}}
                    self.assertEqual(actual, expected)
                    self.assertEqual([type(v) for v in actual['types']],
                                     [bool, bool, type(None), int, float])
                    self.assertEqual(call.prompt, prompt, 'owner input must remain unchanged')
                    wrapped = AgentCall(output_type=GenericOutput, prompt=f'Review {evidence}:\n' + prompt)
                    mapped_wrapped = isolated.remap_input(wrapped)
                    self.assertEqual(json.loads(mapped_wrapped.prompt.removeprefix(f'Review {worker_evidence}:\n')),
                                     expected)

    def test_plain_unicode_prompt_rebases_paths_and_preserves_external_text(self):
        run, evidence = self.owner()
        prompt = f'Inspect {evidence}. External evidence stays at {self.external}.'
        call = AgentCall(output_type=GenericOutput, prompt=prompt)
        with workspace.AgentWorkspace(run, self.phase, self.agent) as isolated:
            expected = isolated.root / evidence.relative_to(run.repo_root)
            mapped = isolated.remap_input(call)
            self.assertEqual(mapped.prompt,
                             f'Inspect {expected}. External evidence stays at {self.external}.')
            self.assertEqual(call.prompt, prompt)

    def test_ascii_json_without_owner_paths_stays_byte_for_byte_unchanged(self):
        run, _ = self.owner()
        prompt = ('{ "binding" : "' + self.binding + '", "external" : "' + self.external +
                  '", "value" : 1e+02, "unknown" : "\\u0645", "empty" : null }')
        call = AgentCall(output_type=GenericOutput, prompt=prompt)
        with workspace.AgentWorkspace(run, self.phase, self.agent) as isolated:
            self.assertEqual(isolated.remap_input(call).prompt, prompt)

    def test_quoted_prose_path_before_colon_is_not_a_json_key(self):
        run, evidence = self.owner()
        prompt = f'Inspect "{evidence}": verify this file. External "{self.external}": unchanged.'
        call = AgentCall(output_type=GenericOutput, prompt=prompt)
        with workspace.AgentWorkspace(run, self.phase, self.agent) as isolated:
            expected = isolated.root / evidence.relative_to(run.repo_root)
            self.assertEqual(expected.read_bytes(), evidence.read_bytes())
            self.assertEqual(isolated.remap_input(call).prompt,
                f'Inspect "{expected}": verify this file. External "{self.external}": unchanged.')
            self.assertEqual(call.prompt, prompt)

    def test_unicode_worker_temp_root_keeps_literal_and_json_path_semantics(self):
        run, evidence = self.owner('project ASCII')
        worker_temp = self.base / 'worker \u0645\u0643\u0627\u0646'
        worker_temp.mkdir()
        with patch.object(tempfile, 'tempdir', str(worker_temp)):
            with workspace.AgentWorkspace(run, self.phase, self.agent) as isolated:
                expected = str(isolated.root / evidence.relative_to(run.repo_root))
                plain = AgentCall(output_type=GenericOutput, prompt=f'Inspect {evidence}')
                self.assertEqual(isolated.remap_input(plain).prompt, f'Inspect {expected}')
                for ensure_ascii in (True, False):
                    call = AgentCall(output_type=GenericOutput,
                        prompt=json.dumps({'path': str(evidence)}, ensure_ascii=ensure_ascii))
                    self.assertEqual(json.loads(isolated.remap_input(call).prompt), {'path': expected})

    def test_typed_prior_and_plan_keep_types_and_inputs_with_json_escaped_roots(self):
        run, evidence = self.owner('project \u0623\u0648 "quoted" \\ root')
        previous = GenericOutput(status='success', artifacts=[str(evidence)],
            artifact_declarations=[{'path': str(evidence), 'sha256': self.binding}],
            observations=[{'path': str(evidence), 'external': self.external, 'verified': True}])
        plan = PlanOutput(status='success', artifacts=[str(evidence)],
            acceptance_criteria=['Keep the approved behavior'], allowed_paths=['src/value.py'],
            required_test_commands=['python3 -B -m unittest'], notes_for_next_agent=self.binding)
        call = AgentCall(output_type=GenericOutput, prompt=f'Read {evidence}', previous=previous, plan=plan)
        original_previous, original_plan = previous.model_dump(), plan.model_dump()
        with workspace.AgentWorkspace(run, self.phase, self.agent) as isolated:
            expected = str(isolated.root / evidence.relative_to(run.repo_root))
            mapped = isolated.remap_input(call)
            self.assertIs(type(mapped.previous), GenericOutput)
            self.assertIs(type(mapped.plan), PlanOutput)
            self.assertEqual(mapped.previous.artifacts, [expected])
            self.assertEqual(mapped.previous.artifact_declarations[0].path, expected)
            self.assertEqual(mapped.previous.artifact_declarations[0].sha256, self.binding)
            self.assertEqual(mapped.previous.observations,
                             [{'path': expected, 'external': self.external, 'verified': True}])
            self.assertEqual(mapped.plan.artifacts, [expected])
            self.assertEqual(mapped.plan.allowed_paths, plan.allowed_paths)
            self.assertEqual(mapped.plan.required_test_commands, plan.required_test_commands)
            self.assertEqual(mapped.plan.notes_for_next_agent, self.binding)
            self.assertEqual(previous.model_dump(), original_previous)
            self.assertEqual(plan.model_dump(), original_plan)
            self.assertEqual(call.prompt, f'Read {evidence}')

    def promote_evidence(self, run):
        with workspace.AgentWorkspace(run, self.phase, self.agent) as isolated:
            isolated.view.agent_baseline = permissions.snapshot(isolated.view)
            evidence = isolated.view.context_handoff_dir / 'evidence.md'
            evidence.write_text('Reviewed evidence.\n', encoding='utf-8')
            binding = hashlib.sha256(evidence.read_bytes()).hexdigest()
            envelope = GenericOutput(status='success', artifacts=[str(evidence)],
                artifact_declarations=[{'path': str(evidence), 'sha256': binding}],
                observations=[{'path': str(evidence), 'external': self.external, 'binding': self.binding}])
            original = envelope.model_dump()
            expected = run.repo_root / evidence.relative_to(isolated.root)
            promoted = isolated.promote(envelope)
            self.assertIs(type(promoted), GenericOutput)
            self.assertEqual(promoted.artifacts, [str(expected)])
            self.assertEqual(promoted.artifact_declarations[0].path, str(expected))
            self.assertEqual(promoted.artifact_declarations[0].sha256, binding)
            self.assertEqual(promoted.observations,
                [{'path': str(expected), 'external': self.external, 'binding': self.binding}])
            self.assertEqual(expected.read_bytes(), evidence.read_bytes())
            self.assertEqual(envelope.model_dump(), original)

    def test_promote_preserves_json_with_quoted_and_backslashed_owner_root(self):
        run, _ = self.owner('owner "quoted" \\ root')
        self.promote_evidence(run)

    def test_promote_rebases_json_from_quoted_and_backslashed_worker_root(self):
        run, _ = self.owner('owner ASCII')
        temporary = self.base / 'worker "quoted" \\ root'
        temporary.mkdir()
        with patch.object(tempfile, 'tempdir', str(temporary)):
            self.promote_evidence(run)


if __name__ == '__main__':
    unittest.main()
