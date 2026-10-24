"""Approved test requirements reach the actual builder guard before dispatch."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests import test_core, test_start
from adw_modules.command_limits import command_limit
from adw_modules.data_types import AgentCall, BuildOutput, PhaseParams, PlanOutput




class StartupCommandPreflightTests(unittest.TestCase):
    setUp=test_start.StartupTests.setUp
    tearDown=test_start.StartupTests.tearDown
    invoke=test_start.StartupTests.invoke
    unchanged=test_start.StartupTests.unchanged

    def test_owner_test_conflict_stops_before_any_planning_or_builder_process(self):
        self.cfg['defaults']['required_test_commands']=['docker restart fixture-database']
        self.config.write_text(json.dumps(self.cfg))
        for extra in (['--dry-run'],[]):
            with self.subTest(extra=extra):
                result=self.invoke(extra=extra)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn('RUNTIME_INCOMPATIBLE',result.stdout+result.stderr)
                self.assertIn('docker restart',result.stdout+result.stderr)
                self.assertFalse((self.root/'adws/adw_data').exists(),'known owner conflict must stop before provider planning')
        self.unchanged()

    def test_planner_receives_owner_native_test_requirement_and_capture_choices(self):
        self.cfg['defaults']['required_test_commands']=['python3 -B -m unittest']
        self.config.write_text(json.dumps(self.cfg))
        self.cli.write_text(test_start.FAKE.replace("result['acceptance_criteria']=['The scoped behavior returns value 2']",
            "result['acceptance_criteria']=['The scoped behavior returns value 2']\n"
            "    result['required_test_commands']=brief['native_test_contract']['required_test_commands']"))
        result=self.invoke()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        session_dir=self.root/'adws/adw_data/sessions/guarded'
        prompt=next(session_dir.glob('planner/phases/*/attempt-001/user.md')).read_text()
        self.assertIn('native_test_contract',prompt)
        self.assertIn('python3 -B -m unittest',prompt)
        self.assertIn('capture_commands',prompt)


if __name__=='__main__':unittest.main()
