"""A wrong changed-file claim is corrected from the controller's measurement, not charged as a defect."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
import sys
import unittest
from pathlib import Path

from tests import test_core

from adw_modules import agent_cc, agents
from adw_modules.data_types import AgentCall, BuildOutput, PhaseParams
from adw_modules.agents import GateFailure


class ReportGateTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    call = test_core.CoreTests.call

    def build(self, claims, artifacts=None):
        """A builder whose k-th report claims claims[k] (the last one repeats)."""
        sends = []

        def responder(request, **_):
            (Path(request.cwd) / 'source.py').write_text('value = 2\n')
            sends.append(request.prompt)
            claim = claims[min(len(sends), len(claims)) - 1]
            report = {'status': 'success', 'summary': 'Changed value', 'changed_files': claim}
            if artifacts:
                report['artifacts'] = artifacts
            return agent_cc.CCRunResult(terminal_success=True, text=json.dumps(report))
        return sends, responder

    def test_wrong_claim_is_corrected_with_the_measured_set_at_no_retry_cost(self):
        sends, responder = self.build([[], ['source.py']])
        result = self.call(responder)           # the phase allows zero retries
        self.assertEqual(result.changed_files, ['source.py'])
        self.assertEqual(len(sends), 2)
        self.assertIn("The controller measured this change set: ['source.py']", sends[1])
        self.assertIn('do not edit any file to fix this', sends[1])

    def test_claim_that_stays_wrong_fails_after_a_bounded_number_of_corrections(self):
        sends, responder = self.build([[]])
        with self.assertRaisesRegex(GateFailure, r'exact changed-file set'):
            self.call(responder)
        self.assertEqual(len(sends), 1 + agents.REPORT_ONLY_CORRECTIONS)

    def test_claim_fixed_on_the_last_free_correction_is_accepted(self):
        sends, responder = self.build([[], [], ['source.py']])
        self.assertEqual(self.call(responder).changed_files, ['source.py'])
        self.assertEqual(len(sends), 3)

    def test_other_violations_get_no_extra_correction(self):
        sends, responder = self.build([['source.py']], artifacts=['missing.md'])
        with self.assertRaises(GateFailure):
            self.call(responder, output=BuildOutput)
        self.assertEqual(len(sends), 1)

    def test_correct_claim_is_accepted_at_once(self):
        sends, responder = self.build([['source.py']])
        self.assertEqual(self.call(responder).changed_files, ['source.py'])
        self.assertEqual(len(sends), 1)


if __name__ == '__main__':
    unittest.main()
