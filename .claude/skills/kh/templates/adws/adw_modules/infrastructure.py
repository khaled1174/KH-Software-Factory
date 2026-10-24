"""Infrastructure capabilities, separate from file permissions (SYS-05).

File scope compares file bytes. A coding agent that runs
`ALTER SYSTEM SET max_connections = 300` and restarts the shared test container
changes no file, so file scope cannot see it. This module adds three layers,
each honest about what it proves:

1. Real restriction (the only layer that makes a mutation impossible): the
   agents' environment. `worker_env` gives agents their own credentials (a
   non-superuser test database URL, say) and `withhold_env` removes others
   (an admin URL, DOCKER_HOST). `capability_checks` are run by the controller
   with exactly that environment before planning; the task refuses to start
   when a probe shows the capability is still there (for example the role is
   a superuser). A non-superuser role cannot run ALTER SYSTEM through any path.
2. Prevention where the runtime supports it: executables in `deny_executables`
   are replaced on the agents' PATH by a stub that refuses, and Claude Code
   roles get matching permission deny rules. Absolute paths bypass PATH, so
   this layer is not proof.
3. Detection: every command an agent runs is in the controller's command
   ledger; a match of `forbidden_commands` (absolute paths and `sh -c`
   wrappers included) that actually executed stops the phase with
   INFRA_BOUNDARY_VIOLATION. A refused attempt is recorded as refused; neither
   case claims anything about the server's state.

Controller quality checks keep the operator's environment: they are owner
configuration, not agent actions.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from .command_ledger import DENIED_MARKER

_settings = None
_shim_dir = None
_resource_prefix = None


class InfrastructureBreach(RuntimeError):
    pass


def configure(cfg, run_id: str | None = None) -> None:
    """Apply this task's infrastructure settings process-wide (one controller, one task)."""
    reset()
    global _settings, _shim_dir, _resource_prefix
    settings = cfg.execution.infrastructure
    _settings = settings if settings.enabled else None
    _resource_prefix = (f'{settings.resource_prefix}{re.sub(r"[^a-z0-9]", "_", (run_id or "run").lower())}_'
                        if _settings is not None and settings.resource_prefix else None)
    _shim_dir = None
    if _settings is not None and settings.deny_executables:
        directory = Path(tempfile.mkdtemp(prefix='kh-infra-shim-'))
        for name in settings.deny_executables:
            stub = directory / name
            stub.write_text('#!/bin/sh\n'
                            f'echo "{DENIED_MARKER}: {name} is not available to coding agents in this task '
                            '(owner infrastructure boundary). Ask the owner; do not work around it." >&2\n'
                            'exit 126\n', encoding='utf-8')
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _shim_dir = directory


def reset() -> None:
    global _settings, _shim_dir, _resource_prefix
    if _shim_dir is not None:
        import shutil
        shutil.rmtree(_shim_dir, ignore_errors=True)
    _settings, _shim_dir, _resource_prefix = None, None, None


def _expand(value: str, env: dict) -> str:
    def replacement(match):
        name = match.group(1)
        result = env.get(name)
        if not isinstance(result, str) or not result.strip():
            raise RuntimeError(f'INFRA_WORKER_ENV_NOT_CONFIGURED: required environment variable {name} '
                               'is missing or empty; supply it before applying this worker restriction')
        return result
    return re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', replacement, value)


_phase_env: dict = {}


def set_phase_env(values: dict) -> None:
    """Per-phase variables for the agent being dispatched (e.g. KH_EVIDENCE_DIR for adws/kh_exec.py)."""
    _phase_env.clear()
    _phase_env.update({k: str(v) for k, v in values.items()})


def apply_worker_env(env: dict) -> dict:
    """The coding agents' environment: the operator's, narrowed by the owner's boundary."""
    env.update(_phase_env)
    if _settings is None:
        return env
    source = dict(env)
    for name in _settings.withhold_env:
        env.pop(name, None)
    for name, value in _settings.worker_env.items():
        env[name] = _expand(value, source)
    if _shim_dir is not None:
        env['PATH'] = os.pathsep.join([str(_shim_dir), env.get('PATH', '')])
    if _resource_prefix:
        env['KH_RESOURCE_PREFIX'] = _resource_prefix
    return env


def claude_deny_rules() -> list[str]:
    if _settings is None:
        return []
    return [f'Bash({name}:*)' for name in _settings.deny_executables]


def forbidden_patterns() -> list[str]:
    if _settings is None:
        return []
    patterns = list(_settings.forbidden_commands)
    if _resource_prefix:
        patterns.append(r'(?i)\bdrop\s+(schema|database)\s+(?!(?:if\s+exists\s+)?"?' + re.escape(_resource_prefix) + r')')
    return patterns


CONTAINER_TOOLS = {'docker', 'podman', 'nerdctl', 'docker-compose'}
SERVICE_TOOLS = {'systemctl', 'launchctl', 'service'}
SERVICE_ACTIONS = {'restart', 'stop', 'kill', 'unload', 'bootout', 'reload'}
SERVER_TOOLS = {'pg_ctl', 'pg_ctlcluster'}


def _inspect(command: str, settings) -> list[str]:
    """Forbidden infrastructure actions in a command, judged per command stage.

    The executable (any path) and its subcommand decide, not substrings: `cat
    docker/start.sh`, `docker run --rm img` and `pytest tests/docker -k restart`
    are not container control; `/usr/local/bin/docker restart db` is.
    """
    if settings is None or not settings.enabled:
        return []
    from .command_ledger import analyze
    from pathlib import PurePosixPath
    actions = set(settings.forbidden_container_actions)
    found = []
    for stage in analyze(command)['stages']:
        words = stage['words']
        tool = PurePosixPath(words[0]).name if words else ''
        rest = [w for w in words[1:] if not w.startswith('-')]
        if tool in CONTAINER_TOOLS:
            sub = [w for w in rest if w not in ('compose', 'container', 'image', 'volume', 'network', 'system')][:1]
            if sub and sub[0] in actions:
                found.append(f'{tool} {sub[0]}')
        elif tool in SERVICE_TOOLS and SERVICE_ACTIONS & set(rest):
            found.append(f'{tool} {sorted(SERVICE_ACTIONS & set(rest))[0]}')
        elif tool in SERVER_TOOLS:
            found.append(tool)
        elif tool == 'brew' and rest[:1] == ['services']:
            found.append('brew services')
    return found


def inspect(command: str) -> list[str]:
    return _inspect(command, _settings)


def inspect_config(command: str, cfg, run_id: str | None = None) -> list[str]:
    """Check a proposed native command without configuring or running anything.

    Capture helpers execute their argv as a child program, so inspect that
    program too. Owner declarations cannot turn a forbidden infrastructure
    action into a granted test command.
    """
    settings = cfg.execution.infrastructure
    if not settings.enabled:
        return []
    from .command_grants import inner_argv
    from .command_ledger import analyze
    import shlex
    argv = inner_argv(command)
    effective = shlex.join(argv) if argv is not None else command
    found = _inspect(effective, settings)
    patterns = list(settings.forbidden_commands)
    if settings.resource_prefix and run_id is not None:
        prefix = settings.resource_prefix + re.sub(r'[^a-z0-9]', '_', run_id.lower()) + '_'
        patterns.append(r'(?i)\bdrop\s+(schema|database)\s+(?!(?:if\s+exists\s+)?"?' + re.escape(prefix) + r')')
    found += [pattern for pattern in patterns if re.search(pattern, command)]
    heads = analyze(command, include_executables=True)['executables']
    for executable in heads:
        if executable in settings.deny_executables:
            found.append('denied executable: ' + executable)
    return list(dict.fromkeys(found))


def check_record(record: dict) -> None:
    """Called when a forbidden command's call returns: executed is a breach, refused is recorded."""
    if record.get('status') == 'refused' or DENIED_MARKER in str(record.get('runtime_exit_basis', '')):
        return
    raise InfrastructureBreach(
        f"INFRA_BOUNDARY_VIOLATION: {record.get('agent')} ran `{str(record.get('command'))[:300]}` "
        f"({record.get('invocation_id')}), which the owner's infrastructure boundary forbids "
        f"(patterns: {', '.join(record.get('forbidden') or [])}). File scope cannot see this kind of change, so "
        "the phase stops. KH does not claim whether the server or container state changed; inspect it.")


def run_capability_checks(cfg, operator_env: dict, session_dir: Path) -> list[dict]:
    """Probe that the agents' environment lacks each capability the owner removed."""
    settings = cfg.execution.infrastructure
    if not settings.enabled:
        return []
    env = apply_worker_env(dict(operator_env))
    if not settings.capability_checks:
        return []
    results, failures = [], []
    for check in settings.capability_checks:
        argv = [_expand(a, env) for a in check.argv]
        try:
            done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=check.timeout_seconds,
                                  stdin=subprocess.DEVNULL)
            output, code = done.stdout.strip(), done.returncode
        except (OSError, subprocess.TimeoutExpired) as error:
            output, code = f'could not run: {error}', None
        ok = code == 0 and output == check.expect_stdout
        results.append({'name': check.name, 'argv': check.argv, 'exit': code, 'stdout': output[:2000],
                        'expected': check.expect_stdout, 'passed': ok})
        if not ok:
            failures.append(f"{check.name}: expected {check.expect_stdout!r}, got {output[:200]!r} (exit {code})")
    path = Path(session_dir) / 'infrastructure_checks.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    path.write_text(json.dumps(results, indent=2), encoding='utf-8')
    if failures:
        raise RuntimeError('INFRA_CAPABILITY_NOT_RESTRICTED: the coding agents\' environment still has a '
                           'capability the owner removed: ' + '; '.join(failures))
    return results
