"""Exact DB environment prefixes grant a single captured test process only."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ADWS = ROOT / ".claude/skills/kh/templates/adws"
sys.path.insert(0, str(ADWS))
from adw_modules.command_grants import capture_commands, inner_argv, validate_overrides


class CommandGrantTests(unittest.TestCase):
    def test_existing_capture_grants_stay_exact_and_overrides_default_to_none(self):
        checks = {"test": ["python3", "-m", "unittest"], "lint": ["ruff", "check", "."],
                  "unknown": ["ignored"], "typecheck": [], "build": ["make", "artifact with spaces"]}
        self.assertEqual(capture_commands(checks, 450), [
            "python3 adws/kh_exec.py --class test --timeout 450 -- python3 -m unittest",
            "python3 adws/kh_exec.py --class lint --timeout 450 -- ruff check .",
            "python3 adws/kh_exec.py --class build --timeout 450 -- make 'artifact with spaces'"])

    def test_only_two_prefix_forms_for_each_declared_test_value_are_generated(self):
        checks = {name: ["python3", "check.py"] for name in ("test", "lint", "typecheck", "build")}
        grants = capture_commands(checks, 37, {"DATABASE_URL": ["fixture-db", "fixture-db"],
                                               "TEST_DATABASE_URL": ["fixture-test"]})
        self.assertEqual(len(grants), 8)
        base = "python3 adws/kh_exec.py --class test --timeout 37 -- python3 check.py"
        self.assertEqual(grants[4:], ["DATABASE_URL=fixture-db " + base, "env DATABASE_URL=fixture-db " + base,
                                     "TEST_DATABASE_URL=fixture-test " + base, "env TEST_DATABASE_URL=fixture-test " + base])
        self.assertFalse(any("unlisted-value" in command or "PGHOST=" in command for command in grants))
        self.assertFalse(any(command.startswith(("env ", "DATABASE_URL=", "TEST_DATABASE_URL="))
                             and "--class test --" not in command.replace("--timeout 37 ", "") for command in grants))
        self.assertEqual(capture_commands({"lint": ["ruff", "check"]}, 37, {"DATABASE_URL": ["fixture-db"]}),
                         ["python3 adws/kh_exec.py --class lint --timeout 37 -- ruff check"])

    def test_override_schema_is_strict_literal_and_does_not_mutate_owner_input(self):
        values = {"DATABASE_URL": [" value with spaces ", "quote'and\"double", " value with spaces "]}
        normalized = validate_overrides(values)
        self.assertEqual(normalized, {"DATABASE_URL": [" value with spaces ", "quote'and\"double"]})
        self.assertEqual(len(values["DATABASE_URL"]), 3)
        bad = [None, [], {"PGHOST": ["localhost"]}, {"DATABASE_URL": "url"}, {"DATABASE_URL": []},
               {"DATABASE_URL": [""]}, {"DATABASE_URL": ["  "]}, {"DATABASE_URL": [42]},
               *[{"DATABASE_URL": [value]} for value in ("a\0b", "a\nb", "a\rb", "a\tb", "$HOME", "`id`")]]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_overrides(value)
        for cap in (0, -1, True, 1.5, "450"):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                capture_commands({"test": ["python3"]}, cap)

    def test_inner_argv_recognizes_only_the_generated_capture_grammar(self):
        argv = ["python3", "check.py", "a literal ; | && argument", "space ' quoted"]
        generated = capture_commands({"test": argv}, 29, {"DATABASE_URL": ["url with 'quotes' and spaces"]})
        for command in generated:
            self.assertEqual(inner_argv(command), argv)
        base = capture_commands({"test": ["python3", "check.py"]}, 29)[0]
        invalid = ["env " + base, "PGHOST=x " + base, "env PGHOST=x " + base,
                   "DATABASE_URL=x TEST_DATABASE_URL=y " + base, "env -i DATABASE_URL=x " + base,
                   "sh -c " + shlex.quote(base), base + " | cat", base + " && true", base + "; true",
                   "DATABASE_URL=$HOME " + base, "DATABASE_URL=`id` " + base,
                   "'DATABASE_URL=value with space' " + base,
                   "DATABASE_URL=x " + base.replace("--class test", "--class lint"),
                   base.replace("--timeout 29", "--timeout 0"), " " + base,
                   base + " 'a\0b'", "docker restart shared-test", "ALTER SYSTEM SET max_connections=300"]
        for command in invalid:
            with self.subTest(command=command):
                self.assertIsNone(inner_argv(command))

    def test_both_forms_reach_only_the_captured_child_and_keep_worker_and_parent_env(self):
        with tempfile.TemporaryDirectory(prefix="db command grants ") as temporary:
            root = Path(temporary)
            (root / "adws").mkdir()
            shutil.copy2(ADWS / "kh_exec.py", root / "adws/kh_exec.py")
            child = [sys.executable, "-c", "import json,os;print(json.dumps({k:os.environ.get(k) for k in "
                     "('DATABASE_URL','TEST_DATABASE_URL')}))"]
            literal = "fixture://user:pa'ss\"word@example/a db?x=1&y=2"
            grants = capture_commands({"test": child}, 5, {"DATABASE_URL": [literal]})
            worker = ("import json,os,subprocess,sys;before={k:os.environ.get(k) for k in "
                      "('DATABASE_URL','TEST_DATABASE_URL')};p=subprocess.run(['/bin/sh','-c',sys.argv[1]],"
                      "capture_output=True,text=True);after={k:os.environ.get(k) for k in before};"
                      "print(json.dumps({'before':before,'after':after,'code':p.returncode,"
                      "'output':p.stdout,'error':p.stderr}))")
            with patch.dict(os.environ, {"DATABASE_URL": "parent-db", "TEST_DATABASE_URL": "parent-test"}):
                for command in grants[1:]:
                    env = {**os.environ, "DATABASE_URL": "worker-db", "TEST_DATABASE_URL": "worker-test",
                           "KH_EVIDENCE_DIR": str(root / "evidence")}
                    done = subprocess.run([sys.executable, "-c", worker, command], cwd=root, env=env,
                                          capture_output=True, text=True, timeout=15)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    observed = json.loads(done.stdout)
                    self.assertEqual(observed["code"], 0, observed["error"])
                    self.assertEqual(observed["before"], {"DATABASE_URL": "worker-db", "TEST_DATABASE_URL": "worker-test"})
                    self.assertEqual(observed["before"], observed["after"])
                    actual_child = json.loads(observed["output"].splitlines()[0])
                    self.assertEqual(actual_child, {"DATABASE_URL": literal, "TEST_DATABASE_URL": "worker-test"})
                    self.assertIn("KH_EXEC_RECEIPT", observed["output"])
                    self.assertEqual((os.environ["DATABASE_URL"], os.environ["TEST_DATABASE_URL"]),
                                     ("parent-db", "parent-test"))
            self.assertEqual(len(list((root / "evidence/exec").glob("*/receipt.json"))), 2)


if __name__ == "__main__":
    unittest.main()
