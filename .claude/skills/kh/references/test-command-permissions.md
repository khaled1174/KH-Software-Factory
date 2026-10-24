# Exact native test commands

Read this reference when a role must run a native test, including a negative
database-connection test. Controller quality checks are separate from builder
tools. A file-only builder can write code while the host checks it.

## Declare argv and literal process values

Merge actual commands into the project's existing defaults. This example uses
a deliberately unreachable local fixture, not credentials for a real database.
It assumes a builder timeout of 1800 seconds; copy the command actually generated
for the selected role rather than changing its capture syntax.

```yaml
defaults:
  quality_checks:
    test: [python3, -B, -m, unittest, discover, -s, tests]
  test_environment_overrides:
    DATABASE_URL:
      - postgresql://fixture:fixture@localhost:1/kh_negative_fixture
  required_test_commands:
    - "DATABASE_URL=postgresql://fixture:fixture@localhost:1/kh_negative_fixture python3 adws/kh_exec.py --class test --timeout 450 -- python3 -B -m unittest discover -s tests"
```

test_environment_overrides defaults to {} and accepts only DATABASE_URL and
TEST_DATABASE_URL with nonempty literal value lists. Dollar expansion, backticks,
control characters and newlines are refused. KH quotes configured values.
required_test_commands defaults to []; keep it empty when host checks satisfy
the task. It declares execution obligations, not authority.

## Existing capture grammar

Native execution requires a configured shell tool and the provider's existing
permission. Use the controller capture to record actual command outcomes.

The capture cap is one quarter of the role timeout, clamped to 30–600 seconds.
For the example timeout the base command is:

```bash
python3 adws/kh_exec.py --class test --timeout 450 -- python3 -B -m unittest discover -s tests
```

Each declared value adds exactly these two forms for test captures:

```bash
DATABASE_URL=postgresql://fixture:fixture@localhost:1/kh_negative_fixture python3 adws/kh_exec.py --class test --timeout 450 -- python3 -B -m unittest discover -s tests
env DATABASE_URL=postgresql://fixture:fixture@localhost:1/kh_negative_fixture python3 adws/kh_exec.py --class test --timeout 450 -- python3 -B -m unittest discover -s tests
```

TEST_DATABASE_URL works only when its own literal value is declared. Do not
combine both names, use env -i or sh -c, add pipes/separators, change argv order
or timeout, or add overrides to lint/typecheck/build. Even different quoting may
fall outside the literal grant. Raw quality commands are not native captures.
The override reaches only the capture/test subprocess; it does not reconfigure
the controller environment, database server or container. Infrastructure policy
still rejects prohibited commands configured by an owner. A shell grant is not
an OS/network sandbox.

## Validate before building

Owner required commands are checked before planning; plan additions are checked
in validate_plan before the builder. Revised plans, amendments and cached
approvals retain existing test obligations. A new owner-required test prevents
reuse of an older plan that omits it. Planning cannot widen tools or grants.

Match the builder's declared tools, exact capture, configured test value and
known infrastructure restrictions. Missing shell should either remain a host
check or be explicitly configured for a later run when the task requires it.
Do not remove mandatory tests, invent permission or mutate infrastructure to
satisfy a plan. Local --doctor/--dry-run checks do not prove native approval.
permission_status: unverified means actual tool evidence is still needed.

If the native tool denies an otherwise matching command, retain its reason and
use the authorized host's exact approval mechanism. Never weaken the guard or
grant blanket Bash permission. Record actual exit/result evidence; a successful
environment override alone does not prove that the application correctly handles
a failed connection. Run real project checks and independent review afterward.

[Model choice](model-choice.md) and the
[configuration contract](config.md) retain their separate responsibilities.
