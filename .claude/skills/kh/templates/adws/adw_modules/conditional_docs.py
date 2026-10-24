"""Typed, reviewed read conditions. Semantic matching belongs to the planner/reviewer.

This module validates completeness and identity; it neither grants permissions nor
claims that delivery proves comprehension or prevents a worker reading other files.
"""
from __future__ import annotations
import copy
import json
from .utils import digest
from pathlib import PurePosixPath
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CATALOG_PATH = 'docs/agent-guide/conditional_docs.md'


class ReadCondition(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,47}$')
    condition: str = Field(min_length=1, max_length=1000)

    @field_validator('condition')
    @classmethod
    def concrete_text(cls, text):
        if not text.strip() or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise ValueError('Read condition must be a nonblank single line without control characters')
        return text


class ReadDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str
    document_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision: Literal['read', 'skip', 'unresolved']
    condition_ids: list[str] = Field(default_factory=list, max_length=16)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode='after')
    def coherent(self):
        if not self.reason.strip() or len(set(self.condition_ids)) != len(self.condition_ids):
            raise ValueError('Read decisions require unique conditions and a concrete reason')
        if (self.decision == 'read') != bool(self.condition_ids):
            raise ValueError('Only read decisions must name matching condition IDs')
        return self


def validate_required(path, conditions):
    if conditions and (PurePosixPath(path).name in {'CLAUDE.md', 'AGENTS.md'} or path == CATALOG_PATH):
        raise ValueError('Required instructions and conditional_docs.md cannot be conditional: ' + path)


def catalog_rows(rows):
    return sorted([row for row in rows if row.get('purpose', 'guide') == 'guide'
                   and row.get('status', 'active') == 'active' and row.get('read_when')],
                  key=lambda row: row['path'])


def render_catalog(rows):
    lines = ['# Conditional project documentation', '',
             'Evaluate every condition for this task. Read a guide when any condition matches.',
             'Record read/skip, matching condition IDs and a task-specific reason in the plan.',
             'Mandatory instructions remain mandatory. Historical records live in app_docs/.', '']
    for row in catalog_rows(rows):
        lines.extend(['## `' + row['path'] + '`', ''])
        for item in row['read_when']:
            condition = ReadCondition.model_validate(item)
            lines.append('- `' + condition.id + '`: ' + condition.condition)
        lines.append('')
    return '\n'.join(lines) + '\n'


def prepare_catalog(documents, rows, read_existing, *, previous=()):
    """Render a planned target and return its controller-generated provenance."""
    target = next((doc for doc in documents if doc.path == CATALOG_PATH), None)
    has_conditions = bool(catalog_rows(rows))
    indexed = any(row['path'] == CATALOG_PATH for row in rows)
    if target is not None:
        submitted = target.content
        target.content = render_catalog(rows)
        inputs = [{'path': row['path'], 'read_when': row['read_when']}
                  for row in catalog_rows(rows)]
        return {'path': CATALOG_PATH, 'generator': 'conditional_docs.render_catalog',
                'submitted_content_empty': submitted == '',
                'submitted_content_digest': digest(submitted),
                'rendered_content_digest': digest(target.content),
                'read_conditions_digest': digest(inputs)}
    elif has_conditions and (not indexed or read_existing(CATALOG_PATH) != render_catalog(rows)):
        raise ValueError('Changed read conditions require the planned catalog target: ' + CATALOG_PATH)
    elif not has_conditions and (indexed or catalog_rows(previous)
                                or any(row['path'] == CATALOG_PATH for row in previous)):
        # Removing the final condition must not leave stale instructions behind.
        # Dropping the catalog from proposed rows leaves its old file on disk.
        old = read_existing(CATALOG_PATH)
        if old and old.startswith('# Conditional project documentation\n') and (not indexed or old != render_catalog(rows)):
            raise ValueError('Removed read conditions require the planned catalog target: ' + CATALOG_PATH)


def validate_catalog(rows, read):
    # Do not probe a legacy project's unindexed docs tree. Actual indexed or
    # conditional paths still use the caller's strict bounded/symlink-safe reader.
    indexed = any(row['path'] == CATALOG_PATH for row in rows)
    if not indexed and not catalog_rows(rows):
        return
    content = read(CATALOG_PATH)
    if catalog_rows(rows) or content and content.startswith('# Conditional project documentation\n'):
        if not indexed or content != render_catalog(rows):
            raise ValueError('Conditional catalog missing or inconsistent: ' + CATALOG_PATH)


def validate_selection(packet, decisions):
    rows = packet.get('conditional_documents', [])
    if not rows:
        if decisions:
            raise ValueError('Conditional decisions supplied without a catalog')
        return []
    if digest(rows) != packet.get('conditional_catalog_digest'):
        raise ValueError('Conditional catalog digest mismatch')
    by_path = {row['path']: row for row in rows}
    if len(by_path) != len(rows) or decisions is None:
        raise ValueError('Conditional selection must cover the complete catalog')
    choices = [ReadDecision.model_validate(item.model_dump() if isinstance(item, ReadDecision) else item)
               for item in decisions]
    if len({item.path for item in choices}) != len(choices) or {item.path for item in choices} != set(by_path):
        raise ValueError('Conditional selection must cover each catalog path exactly once')
    for choice in choices:
        row = by_path[choice.path]
        ids = {condition['id'] for condition in row['read_when']}
        if choice.document_digest != row['digest'] or not set(choice.condition_ids) <= ids:
            raise ValueError('Stale document digest or unknown read condition: ' + choice.path)
        if choice.decision == 'unresolved':
            raise ValueError('Unresolved conditional selection: ' + choice.path)
    return choices


def resolve_context(packet, decisions, read, limit):
    choices = validate_selection(packet, decisions)
    result = copy.deepcopy(packet)
    if not choices:
        return result
    rows = {row['path']: row for row in packet['conditional_documents']}
    paths = set(rows)
    # Re-resolution is idempotent and cannot duplicate delivered guides.
    result['documents'] = [row for row in result['documents'] if row['path'] not in paths]
    for key in ('selected', 'omitted'):
        result['selection'][key] = [row for row in result['selection'][key] if row['path'] not in paths]
    for choice in choices:
        if choice.decision == 'skip':
            result['selection']['omitted'].append({'path': choice.path, 'reason': 'condition_not_matched'})
            continue
        content = read(choice.path)
        if content is None or digest(content) != choice.document_digest:
            raise ValueError('Selected guide missing or digest changed: ' + choice.path)
        result['documents'].append({**rows[choice.path], 'content': content,
                                    'provenance': 'reviewed-conditional-project-context'})
        result['selection']['selected'].append({'path': choice.path, 'reason': 'condition_match',
                                                'matched_conditions': choice.condition_ids})
    result['conditional_selection'] = {'catalog_digest': packet['conditional_catalog_digest'],
                                        'decisions': [choice.model_dump() for choice in choices]}
    keys = ('documents', 'manifest_digests', 'selection', 'conditional_documents',
            'conditional_catalog_digest', 'conditional_selection')
    used = len(json.dumps({**{key: result[key] for key in keys},
                           'warnings': result.get('warnings', [])}).encode())
    if used > limit:
        raise ValueError('Selected project context exceeds the 128 KiB budget; curate guides or split the task')
    result['budget']['used_bytes'] = used
    return result
