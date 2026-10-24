"""Strict, controller-bound behavioral evidence from owner-configured adapters.

Adapters are trusted project commands, not a filesystem sandbox. Any source
mutation fails verification and the guarded task controller rejects promotion.
No command exit status can substitute for a complete evidence result.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import shlex
import signal
import stat
import subprocess
import tempfile
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .data_types import GateReport, QualityCheckResult, QualityResult
from . import quality
from .utils import operator_env


class AdapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["passed", "failed", "not_applicable"]
    affected_surfaces: list[str]
    covered_surfaces: list[str]
    evidence: list[str] = Field(min_length=1)
    discovered_tests: int = Field(ge=0)
    executed_tests: int = Field(ge=0)
    unresolved_differences: int = Field(ge=0)


def affected_surfaces(paths, patterns):
    """Return deterministic changed surfaces selected by project-owned globs."""
    from .permissions import covers
    return sorted({path for path in paths if any(covers(path, pattern) for pattern in patterns)})


def _regular_path(root, relative):
    if (not isinstance(relative, str) or not relative or '\\' in relative
            or ':' in relative or any(ord(c) < 32 for c in relative)):
        raise ValueError("invalid evidence path")
    parts = relative.split('/')
    if PurePosixPath(relative).is_absolute() or any(p in ('', '.', '..') for p in parts):
        raise ValueError("evidence must be a safe relative file")
    current = Path(root)
    if current.is_symlink() or not current.is_dir():
        raise ValueError("evidence root must be a real directory")
    for index, part in enumerate(parts):
        current = current / part
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError("symlink evidence is forbidden")
        if index < len(parts) - 1 and not stat.S_ISDIR(mode):
            raise ValueError("evidence parent must be a directory")
    info = current.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        raise ValueError("evidence must be a nonempty regular file")
    return current


def validate_result(data, evidence_root, candidate_sha256, affected):
    """Validate typed coverage and artifacts against the controller's snapshot."""
    report = GateReport()
    try:
        parsed = AdapterResult.model_validate(data)
        # Literal[1] accepts bool in Pydantic; protocol versions must not coerce.
        if type(data.get('schema_version')) is not int:
            raise ValueError("schema_version must be an integer")
    except (ValidationError, ValueError, TypeError) as error:
        return report.check("adapter_schema", False, str(error))
    report.check("candidate_sha256", parsed.candidate_sha256 == candidate_sha256,
                 "evidence must describe the current candidate")
    report.check("status", parsed.status == 'passed', "applicable adapter must pass")
    report.check("affected_surfaces", bool(affected) and sorted(parsed.affected_surfaces) == sorted(set(affected)),
                 "affected surfaces must exactly match controller discovery")
    report.check("covered_surfaces", len(set(parsed.covered_surfaces)) == len(parsed.covered_surfaces)
                 and set(affected) <= set(parsed.covered_surfaces), "all affected surfaces need coverage")
    report.check("discovery", parsed.discovered_tests > 0 and 0 < parsed.executed_tests <= parsed.discovered_tests,
                 "positive discovered and executed test counts are required")
    report.check("visual_differences", parsed.unresolved_differences == 0,
                 "unresolved differences cannot be approved by an exit code")
    for relative in parsed.evidence:
        try:
            _regular_path(evidence_root, relative)
            report.check(relative, True)
        except (OSError, ValueError) as error:
            report.check(str(relative), False, str(error))
    return report


def _terminate(process):
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)


def run_adapters(run, changed_paths):
    """Retain logs/results/captures and reject missing coverage or source writes."""
    checks, artifacts, failures = [], [], []
    for adapter in run.cfg.defaults.verification_adapters:
        affected = affected_surfaces(changed_paths, adapter.applicable_paths)
        if not affected:
            # Applicability is calculated by the controller, never the adapter.
            continue
        phase = run.phases[-1].seq if run.phases else 0
        parent = Path(run.context_handoff_dir) / 'verification'
        parent.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix=f'{phase:02d}_{adapter.name}_', dir=parent)).resolve()
        result_path = output / 'result.json'
        before = quality.source_fingerprint(run)
        env = operator_env()
        env.update(KH_VERIFICATION_RESULT=str(result_path), KH_CANDIDATE_SHA256=before,
                   KH_CHANGED_PATHS=json.dumps(affected))
        started = time.monotonic()
        process, stdout, stderr, code = None, '', '', 127
        try:
            from .execution_budget import remaining_timeout
            timeout = remaining_timeout(run, adapter.timeout_seconds)
            process = subprocess.Popen(adapter.argv, cwd=run.repo_root, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            stdout, stderr = process.communicate(timeout=timeout)
            code = process.returncode
        except subprocess.TimeoutExpired:
            _terminate(process)
            stdout, stderr = process.communicate(timeout=5)
            code = 124
            stderr += f'\nAdapter timed out after {adapter.timeout_seconds}s'
        except OSError as error:
            stderr = str(error)
        except BaseException:
            _terminate(process)
            if process is not None:
                process.communicate(timeout=5)
            raise
        after = quality.source_fingerprint(run)
        report = GateReport().check('exit', code == 0, f'command exited {code}')
        report.check('source_unchanged', before == after, 'VERIFICATION_SOURCE_CHANGED')
        try:
            path = _regular_path(output, 'result.json')
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('adapter result exceeds 2 MiB')
            data = json.loads(path.read_text())
            validated = validate_result(data, output, before, affected)
            report.checks.extend(validated.checks)
            artifacts.append(str(path))
            # Retain valid captures on failed checks too, so repair/review can
            # inspect the difference instead of losing the failing evidence.
            if isinstance(data, dict) and isinstance(data.get('evidence'), list):
                for relative in data['evidence']:
                    try:
                        artifacts.append(str(_regular_path(output, relative)))
                    except (OSError, ValueError):
                        pass
        except (OSError, ValueError, TypeError) as error:
            report.check('result', False, str(error))
        duration = time.monotonic() - started
        # Controller log is outside the adapter-owned output directory.
        log = parent / (output.name + '.log')
        text = (f'$ {shlex.join(adapter.argv)}\nexit: {code}\nsource_before: {before}\n'
                f'source_after: {after}\n\nstdout:\n{stdout}\nstderr:\n{stderr}\n'
                f'violations:\n' + '\n'.join(report.violations))
        with log.open('x') as stream:
            stream.write(text)
        artifacts.append(str(log))
        passed = report.passed
        check = QualityCheckResult(name=f'verification:{adapter.name}', area='frontend', operation='build',
            command=shlex.join(adapter.argv), returncode=code if code else (0 if passed else 125),
            passed=passed, duration_seconds=duration, output_artifact=str(log),
            output_tail=text[-quality.TAIL_CHARS:])
        checks.append(check)
        if not passed:
            failures.append(f'{check.name}: ' + '; '.join(report.violations))
    return QualityResult(passed=not failures, checks=checks, failures=failures, artifacts=artifacts)
