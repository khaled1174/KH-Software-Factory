"""Format actual project-test observations for KH's existing verification adapter.

This helper executes no tests and owns no resources. The project harness must
perform the assertions and observe cleanup before calling emit. The controller
still owns adapter execution, source identity, coverage and final acceptance.
"""
from __future__ import annotations
import json
import os
from pathlib import Path, PurePosixPath


def _surface(value):
    return (isinstance(value, str) and bool(value) and not PurePosixPath(value).is_absolute()
            and PurePosixPath(value).as_posix() == value and '..' not in PurePosixPath(value).parts
            and not any(c in value for c in '*?[]\\') and not any(ord(c) < 32 for c in value))


def emit(*, candidate_sha256, discovered_tests, cases, evidence, cleanup, unresolved_differences, environ=None):
    """Write fresh case/result evidence; return 0 only for proven reported outcomes.

    cases: actual executed cases with name, requirement, passed (bool), positive
    assertion count and the surfaces they really exercised. cleanup describes
    resources owned by that harness, not every process/server on the host.
    """
    env = os.environ if environ is None else environ
    output = Path(env['KH_VERIFICATION_RESULT'])
    if not output.is_absolute() or not output.parent.is_dir():
        raise ValueError('Recipe output must be an existing controller-owned absolute directory')
    current = Path(output.anchor)
    for part in output.parent.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError('Recipe output must not traverse symlinks')
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    affected = json.loads(env['KH_CHANGED_PATHS'])
    if not isinstance(affected, list) or not affected or any(not _surface(p) for p in affected):
        raise ValueError('Controller changed paths must be concrete surfaces')
    errors, covered, names = [], set(), set()
    if not isinstance(cases, list) or not cases:
        errors.append('No executed cases')
        cases = [] if not isinstance(cases, list) else cases
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get('name'), str) or not case['name'].strip()
                or not isinstance(case.get('requirement'), str) or not case['requirement'].strip()
                or type(case.get('passed')) is not bool or case['passed'] is not True
                or type(case.get('assertions')) is not int or case['assertions'] < 1
                or not isinstance(case.get('surfaces'), list) or not case['surfaces']
                or any(not _surface(path) for path in case['surfaces'])):
            errors.append('Every executed case needs a passed assertion, requirement and actual surfaces')
            continue
        if case['name'] in names:
            errors.append('Duplicate executed case name')
        names.add(case['name']);covered.update(case['surfaces'])
    fields = {'confirmed', 'active_children', 'open_readers', 'open_connections'}
    if (not isinstance(cleanup, dict) or set(cleanup) != fields or cleanup.get('confirmed') is not True
            or any(type(cleanup.get(key)) is not int or cleanup[key] != 0 for key in fields - {'confirmed'})):
        errors.append('Owned resource cleanup is missing, unknown or incomplete')
    if type(discovered_tests) is not int or discovered_tests < 1 or len(cases) > discovered_tests:
        errors.append('Discovery/execution counts are not valid positive observations')
    if candidate_sha256 != env['KH_CANDIDATE_SHA256']:
        errors.append('Reported candidate is stale or different')
    if not set(affected) <= covered:
        errors.append('Actual case surfaces do not cover all affected surfaces')
    if type(unresolved_differences) is not int or unresolved_differences != 0:
        errors.append('Visual/interaction differences are unresolved or unknown')
    if not isinstance(evidence, list) or any(not isinstance(path, str) for path in evidence):
        raise ValueError('Evidence must be actual relative artifact paths')
    result = {'schema_version': 1, 'candidate_sha256': candidate_sha256,
        'status': 'failed' if errors else 'passed', 'affected_surfaces': affected,
        'covered_surfaces': sorted(covered), 'evidence': [*evidence, 'recipe-cases.json'],
        'discovered_tests': discovered_tests, 'executed_tests': len(cases), 'unresolved_differences': unresolved_differences}
    # The outer controller still validates the complete adapter schema/artifacts.
    # Keep this project-harness helper standard-library only.
    report_path = output.parent/'recipe-cases.json'
    with report_path.open('x', encoding='utf-8') as stream:
        json.dump({'candidate_sha256': candidate_sha256, 'cases': cases, 'cleanup': cleanup,
                   'errors': errors}, stream, ensure_ascii=False, indent=2, allow_nan=False)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return 0 if not errors else 1
