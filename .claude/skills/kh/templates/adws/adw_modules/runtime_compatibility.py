"""Reject proven role/runtime mismatches before spending a provider call.

Static tool/sandbox and exact native-command checks do not prove executable,
resource or native permission access. Legacy probe settings are accepted without
executing host commands. This module cannot grant tools, change roles/models,
request approval, or infer capabilities from model names.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import uuid


from .data_types import RUNTIMES
TOOL_ALIASES = {'read': 'read', 'read_file': 'read', 'view_file': 'read',
                'grep': 'grep', 'grep_search': 'grep', 'glob': 'find',
                'find': 'find', 'find_by_name': 'find', 'ls': 'ls', 'list_dir': 'ls',
                'bash': 'bash', 'shell': 'bash', 'run_command': 'bash',
                'edit': 'edit', 'multiedit': 'edit', 'replace_file_content': 'edit',
                'multi_replace_file_content': 'edit', 'write': 'write', 'write_file': 'write',
                'write_to_file': 'write'}


class RuntimeCompatibilityError(RuntimeError):
    def __init__(self, report):
        self.report = report
        super().__init__('RUNTIME_INCOMPATIBLE: ' + '; '.join(
            item['capability'] + ': ' + item['reason'] for item in report['blocked']))


def _item(report, section, capability, reason):
    report[section].append({'capability': capability, 'reason': reason})


def _tools(agent):
    value = getattr(agent, 'tools', None)
    if value is None:
        return None
    return {TOOL_ALIASES.get(str(tool).casefold(), str(tool).casefold()) for tool in value}


def _quality(cfg):
    defaults = getattr(cfg, 'defaults', None)
    checks = getattr(defaults, 'quality_checks', {}) or {}
    return {name: argv for name, argv in checks.items()
            if name in {'test', 'lint', 'typecheck', 'build'} and isinstance(argv, list) and argv}


def _native_test_commands(report, agent, cfg, commands, *, origin, run_id=None):
    """Read-only preflight against the same exact grants as the native hook."""
    if getattr(agent, 'name', '') != 'builder':
        return
    tools = _tools(agent)
    has_shell = tools is None or 'bash' in tools
    defaults = getattr(cfg, 'defaults', None)
    quality = _quality(cfg)
    from .command_limits import command_limit
    from .command_grants import capture_commands
    from . import infrastructure
    try:
        grants = capture_commands(quality, command_limit(getattr(agent, 'timeout_seconds', 1800)),
                                  getattr(defaults, 'test_environment_overrides', {}) or {})
    except ValueError as error:
        _item(report, 'blocked', 'native_test_grants', str(error))
        return
    if not isinstance(commands, (list, tuple)) or any(not isinstance(command, str) or not command.strip()
            or any(char in command for char in ('\x00', '\n', '\r')) for command in commands):
        _item(report, 'blocked', 'required_test_command', 'Test requirements must be literal nonempty single-line commands')
        return
    existing = {row['command'] for row in report['native_test_commands']}
    requirements = list(dict.fromkeys(commands))
    report['required_test_commands'] = list(dict.fromkeys([*report['required_test_commands'], *requirements]))
    # Every capture exposed to a native builder must respect the owner's
    # infrastructure boundary even if a plan does not require that capture.
    candidates = [(command, True) for command in requirements]
    if has_shell:
        candidates += [(command, False) for command in grants]
    for command, required in candidates:
        if command in existing:
            row = next(row for row in report['native_test_commands'] if row['command'] == command)
            row['required'] = row['required'] or required
            if required and origin not in row['origins']:
                row['origins'].append(origin)
            continue
        exact = command in grants
        forbidden = infrastructure.inspect_config(command, cfg, run_id=run_id) if has_shell else []
        issues = []
        if required and not has_shell:
            issues.append('Builder has no native shell tool for this required test command')
        if forbidden:
            issues.append('Native command conflicts with the owner infrastructure boundary: ' + '; '.join(forbidden))
        row = {'command': command, 'required': required, 'origins': [origin] if required else ['configured_capture'],
               'shell_tool': has_shell, 'exact_grant': exact, 'forbidden': forbidden,
               'permission_status': 'unverified', 'passed': not issues, 'issues': issues}
        report['native_test_commands'].append(row)
        existing.add(command)
        if issues:
            _item(report, 'blocked', 'required_test_command' if required else 'native_test_grant', '; '.join(issues))
        elif required:
            _item(report, 'supported', 'required_test_command',
                  'Native shell tool is configured; its permission/access remains unverified')


def _structural(agent, cfg=None, *, run_id=None):
    runtime = getattr(agent, 'coding_agent', '')
    required = list(dict.fromkeys(getattr(agent, 'required_capabilities', []) or []))
    report = {'schema_version': 1, 'runtime': runtime, 'role': getattr(agent, 'name', ''),
              'required_capabilities': required, 'status': 'compatible',
              'supported': [], 'blocked': [], 'unverified': [], 'probes': [],
              'required_test_commands': [], 'native_test_commands': []}
    if runtime not in RUNTIMES:
        _item(report, 'blocked', 'runtime', 'No supported KH adapter for the declared runtime')
    tools = _tools(agent)
    native_shell = tools is None or 'bash' in tools
    write_tools = {'write', 'edit', 'bash'}
    native_write = tools is None or bool(tools & write_tools)
    if runtime == 'delegate_codex' and tools is not None:
        _item(report, 'blocked', 'tools', 'Codex relay cannot enforce a role tool allowlist')
    if runtime == 'delegate_codex' and getattr(agent, 'harness_engineering', []):
        _item(report, 'blocked', 'extensions', 'This runtime cannot load or enforce the configured Pi harness extensions')
    sandbox = getattr(agent, 'delegate_sandbox', 'workspace-write')
    read_only = runtime == 'delegate_codex' and sandbox == 'read-only'
    if read_only and ('source_write' in required or getattr(agent, 'writes', None) != []):
        _item(report, 'blocked', 'source_write', 'Codex read-only sandbox cannot write source')
    elif 'source_write' in required:
        if not native_write or getattr(agent, 'writes', None) == []:
            _item(report, 'blocked', 'source_write', 'Role tool/file boundary does not permit source writes')
        else:
            _item(report, 'supported', 'source_write', 'Configured source-write tools and sandbox permit candidate writes')
    quality = _quality(cfg)
    for capability in required:
        if capability == 'source_write':
            continue
        if capability not in {'shell', 'swift', 'database'}:
            _item(report, 'blocked', capability, 'Unknown owner-declared capability')
            continue
        if not native_shell:
            _item(report, 'blocked', capability, 'Role has no native shell tool; host quality execution is separate')
            continue
        _item(report, 'supported', capability, 'Configured native shell tool is available; native permissions remain unverified')
        if capability in {'swift', 'database'}:
            _item(report, 'unverified', 'host_' + capability,
                  'Legacy capability requires native shell configuration only; executable/resource access is unverified')
    _item(report, 'unverified', 'native_permissions',
          'Native permission/access decisions are not proven by configuration; no approval is granted')
    if 'database' in required:
        _item(report, 'unverified', 'native_database_access',
              'Configured shell tools do not prove provider sandbox/network/database access')
    defaults = getattr(cfg, 'defaults', None)
    _native_test_commands(report, agent, cfg, getattr(defaults, 'required_test_commands', []) or [],
                          origin='owner_config', run_id=run_id)
    if report['blocked']:
        report['status'] = 'incompatible'
    return report


def structural(agent, cfg=None, *, required_test_commands=(), run_id=None):
    """Read-only model/plan compatibility; never executes host commands."""
    report = _structural(agent, cfg, run_id=run_id)
    if required_test_commands:
        _native_test_commands(report, agent, cfg, required_test_commands,
                              origin='proposed_plan', run_id=run_id)
    if report['blocked']:
        report['status'] = 'incompatible'
    return report


def _persist(run, report):
    base = getattr(run, 'session_dir', None)
    if base is None:
        return
    def safe(value):
        return re.sub(r'[^a-zA-Z0-9_-]', '_', str(value or 'unknown'))[:100]
    directory = Path(base).resolve() / 'runtime_checks' / safe(report.get('phase_id')) / safe(report['role'])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ('check-' + uuid.uuid4().hex + '.json')
    report['artifact'] = str(path)
    from .scope import _atomic_json
    _atomic_json(path, report)


def check(run, agent, *, required_test_commands=()):
    """Fresh dispatch check; raises only on demonstrated/configured incompatibility."""
    report = _structural(agent, getattr(run, 'cfg', None), run_id=getattr(run, 'adw_id', None))
    report['phase_id'] = getattr(run, 'active_phase_id', '')
    report['checked_at'] = datetime.now(timezone.utc).isoformat()
    _native_test_commands(report, agent, getattr(run, 'cfg', None), required_test_commands,
                          origin='approved_plan', run_id=getattr(run, 'adw_id', None))
    runtime = report['runtime']
    evidence_required = getattr(run, 'runtime_evidence_required',
                                bool(getattr(run, 'context_handoff_dir', None)
                                     or getattr(run, 'agent_output_relative', None)))
    if evidence_required:
        tools = _tools(agent)
        if runtime == 'delegate_codex' and getattr(agent, 'delegate_sandbox', '') == 'read-only':
            _item(report, 'blocked', 'phase_evidence', 'Codex read-only sandbox cannot write mandatory phase artifacts')
        elif tools is not None and not tools & {'write', 'edit', 'bash'}:
            _item(report, 'blocked', 'phase_evidence', 'Role has no native tool able to write mandatory phase artifacts')
    report['status'] = 'incompatible' if report['blocked'] else 'compatible'
    _persist(run, report)
    if report['blocked']:
        raise RuntimeCompatibilityError(report)
    return report
