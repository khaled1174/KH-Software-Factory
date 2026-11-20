#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""Prepare a draft task intent and project plan from a versioned KH template."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import date
import json
import os
from pathlib import Path
import re
import sys

import yaml

TEMPLATES = Path(__file__).resolve().parents[1] / 'templates'
sys.path.insert(0, str(TEMPLATES / 'adws'))
from adw_modules import intent, project  # noqa: E402
from adw_modules.data_types import ConfigDefaults  # noqa: E402
from adw_modules.scope import _parent_fd  # noqa: E402
from adw_modules.session import acquire_repo_lock  # noqa: E402


def draft_text(value: str, field: str, *, single_line: bool = False) -> str:
    value = value.strip()
    if (not value or value.startswith('<') or '\x00' in value or '\r' in value
            or (single_line and '\n' in value)
            or re.search(r'(?m)^(?:## |Status:|Accepted digest:)', value)):
        raise ValueError(f'{field} must be plain text without intent headings or fields')
    return value


def check_configuration(path: Path, requested: list[str]) -> tuple[list[str], str]:
    raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict) or not isinstance(raw.get('execution', {}), dict):
        raise ValueError('Expected a project configuration mapping')
    defaults = ConfigDefaults.model_validate(raw.get('defaults', {}))
    checks = list(dict.fromkeys([*defaults.required_quality_checks, *requested]))
    missing = []
    for name in checks:
        argv = defaults.quality_checks.get(name, [])
        if (not argv or not argv[0].strip() or
                any('PLACEHOLDER' in arg or '\x00' in arg for arg in argv)):
            missing.append(name)
    if missing:
        raise ValueError(f"NOT_CONFIGURED: configure real argv in defaults.quality_checks for {', '.join(missing)}")
    return checks, raw.get('execution', {}).get('task_kind', 'generic')


def prepare(args: argparse.Namespace, catalog: dict) -> dict:
    root = args.project.absolute()
    if not root.is_dir():
        raise ValueError(f'Project root is not a directory: {root}')
    title = draft_text(args.title, 'Title', single_line=True)
    author = draft_text(args.author, 'Author', single_line=True)
    request = draft_text(args.request, 'Task request')
    problem = draft_text(args.problem, 'Problem')
    outcome = draft_text(args.outcome, 'Proposed outcome')
    template = catalog['templates'][args.template]
    required_checks = list(dict.fromkeys(['test', *args.check]))
    checks, configured_kind = check_configuration(root / args.config, required_checks)
    plan = project.validate_plan({
        'schema_version': project.SCHEMA_VERSION,
        'project': {'id': args.project_id, 'name': args.project_name or root.name or 'Local project'},
        'milestones': [{'id': 'M1', 'title': title}],
        'tasks': [{'id': args.task_id, 'milestone_id': 'M1', 'title': title, 'request': request,
                   'deliverables': [outcome, *template['deliverables']], 'allowed_paths': args.allow_path,
                   'required_checks': required_checks}],
    })
    version = catalog['schema_version']
    source = (
        f'# Intent: {title}\n\nStatus: draft\nAuthor: {author}\nDate: {date.today().isoformat()}\n\n'
        f'## Task request\n\n{request}\n\n## Problem\n\n{problem}\n\n'
        f'## Proposed outcome\n\n{outcome}\n\n'
        '## Acceptance\n\nAccepted by: <owner name>\nAccepted on: <YYYY-MM-DD>\n'
        'Approved sensitive changes: none\n\n'
        f'<!-- KH task template {args.template} v{version}. -->\n'
    )
    if len(source.encode('utf-8')) > intent.MAX_INTENT_BYTES:
        raise ValueError('Draft exceeds the controller intent size limit')
    return {
        'template': args.template, 'template_version': version, 'task_kind': template['task_kind'],
        'configured_task_kind': configured_kind, 'required_quality_checks': checks,
        'intent_path': f'intents/{args.task_id}.md', 'plan_path': f'project-plan-{args.task_id}.json',
        'intent': source, 'project_plan': plan,
    }


def remove_created(parent: int, name: str, created: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if os.path.samestat(created, current):
            os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass


def publish_new(parent: int, name: str, content: str) -> os.stat_result:
    """Create a new draft without following links or replacing an existing file."""
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
    created = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        remove_created(parent, name, created)
        raise
    return created


def write_draft(root: Path, draft: dict) -> None:
    plan = draft['project_plan']
    request = plan['tasks'][0]['request']
    paths = [draft['intent_path'], draft['plan_path']]
    contents = [draft['intent'], json.dumps(plan, ensure_ascii=False, indent=2) + '\n']
    with acquire_repo_lock(root), ExitStack() as stack:
        for path in paths:
            if os.path.lexists(root / path):
                raise ValueError(f'Draft destination already exists: {path}')
        destinations = [stack.enter_context(_parent_fd(root / path, create=True)) for path in paths]
        for path in sorted((root / 'intents').glob('*.md')):
            if intent._section(intent._read(path), 'Task request') == request:
                raise ValueError(f'An intent already matches the task request: {path.name}')
        parent, name = destinations[0]
        created = publish_new(parent, name, contents[0])
        try:
            publish_new(*destinations[1], contents[1])
        except BaseException:
            remove_created(parent, name, created)
            raise


def main() -> int:
    catalog = json.loads((TEMPLATES / 'task-templates/v1.json').read_text(encoding='utf-8'))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('template', choices=catalog['templates'])
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--config', type=Path,
                        default=Path(os.environ.get('KH_CONFIG', 'adws/adw_kh_config/kh.config.yaml')))
    parser.add_argument('--project-id', default='local-project')
    parser.add_argument('--project-name')
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--author', default='Project author')
    parser.add_argument('--request', required=True)
    parser.add_argument('--problem', required=True)
    parser.add_argument('--outcome', required=True)
    parser.add_argument('--allow-path', action='append', required=True)
    parser.add_argument('--check', action='append', default=[], choices=('test', 'lint', 'typecheck', 'build'))
    parser.add_argument('--write', action='store_true', help='Create the intent and plan as new files')
    args = parser.parse_args()
    try:
        draft = prepare(args, catalog)
        if args.write:
            write_draft(args.project, draft)
    except (OSError, RuntimeError, ValueError, yaml.YAMLError) as error:
        print(f'prepare-task: {error}', file=sys.stderr)
        return 1
    print(json.dumps({**draft, 'written': args.write}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
