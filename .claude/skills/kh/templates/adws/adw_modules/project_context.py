"""Reviewed, controller-published project documentation and bounded context packets.

Worker models only emit JSON. All filesystem publication happens here, under the
calling controller's repository lock. The journal is recoverable, not a claim of
multi-file atomicity. Existing owner files are retained verbatim.
"""
from __future__ import annotations
from .utils import digest
from . import execution_budget, scope
import ast
import fnmatch
import hashlib
import posixpath
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import tempfile
from typing import Literal
from types import SimpleNamespace
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator
from . import conditional_docs as cd
from .data_types import AgentCall, ConfigDefaults, EnvelopeBase, PhaseParams

INDEX = '.kh/project-context.json'
JOURNAL = '.kh/context-publication.json'
COMMANDS_PATH = 'docs/agent-guide/commands.md'
MAX_BYTES = 131072
MAX_MAP_BYTES = 32768
MANIFESTS = {'package.json', 'pyproject.toml', 'requirements.txt', 'Cargo.toml', 'go.mod', 'Makefile', 'justfile', 'build.gradle', 'settings.gradle', 'Gemfile', 'pubspec.yaml', 'Package.swift'}
SKIP = {'.git', '.kh', 'adw_data'}
# Framework-owned trees. They are installed, not authored by the project, so the
# architect must not inventory or document them as if they were project source.
FRAMEWORK_PATHS = ('.claude', 'adws', 'vendor/delegate-skills', '.kh-backups')
FRAMEWORK_NOTE = ('Installed KH tooling, not project source: do not read, inventory or document these '
                  'paths. Reading them only fills the context window.')

class ContextBlocked(ValueError):
    pass

class _UnreadableDocument(ContextBlocked):
    pass

class DocumentLifecycle(BaseModel):
    model_config = ConfigDict(extra='forbid')
    purpose: Literal['guide', 'change_record', 'run_evidence', 'external_reference'] = 'guide'
    status: Literal['active', 'historical', 'superseded'] = 'active'
    topics: list[str] = Field(default_factory=list, max_length=16)
    supersedes: list[str] = Field(default_factory=list, max_length=32)
    source_revision: str = Field(default='', max_length=128)
    read_when: list[cd.ReadCondition] = Field(default_factory=list, max_length=16)

    @field_validator('read_when')
    @classmethod
    def unique_conditions(cls, values):
        if len({v.id for v in values}) != len(values):
            raise ValueError('Read conditions require unique IDs per document')
        return values

    @model_serializer(mode='wrap')
    def compatible_dump(self, handler):
        data = handler(self)
        if not self.read_when and 'read_when' not in self.model_fields_set:
            data.pop('read_when', None)
        return data

    @field_validator('topics')
    @classmethod
    def bounded_topics(cls, values):
        if len(set(values)) != len(values) or any(not re.fullmatch(r'[a-z][a-z0-9_-]{0,47}', v) for v in values):
            raise ValueError('Topics must be unique lowercase identifiers')
        return values


class ContextDocument(DocumentLifecycle):
    path: str
    content: str
    applies_to: list[str] = Field(default_factory=lambda: ['**'])

    @model_validator(mode='after')
    def required_instructions(self):
        cd.validate_required(self.path, self.read_when)
        return self


def resolve_document_metadata(document, previous):
    """Preserve omitted existing metadata before review hashes are computed.

    Mutate the typed proposal once so the reviewer, saved proposal and publisher
    all see the same resolved values. Explicit fields remain reviewed changes.
    """
    if previous:
        inherited = {**DocumentLifecycle.model_validate({key: previous[key]
            for key in DocumentLifecycle.model_fields if key in previous}).model_dump(),
            'applies_to': previous.get('applies_to', ['**'])}
        for key, value in inherited.items():
            if key not in document.model_fields_set:
                if key == 'read_when':
                    value = [cd.ReadCondition.model_validate(item) for item in value]
                setattr(document, key, value)


def lifecycle(path, row):
    data = DocumentLifecycle.model_validate({key: row[key] for key in DocumentLifecycle.model_fields if key in row})
    cd.validate_required(path, data.read_when)
    if (PurePosixPath(path).name in {'CLAUDE.md', 'AGENTS.md'} or path == cd.CATALOG_PATH) and (data.purpose != 'guide' or data.status != 'active' or data.supersedes):
        raise ContextBlocked('Instruction documents must remain active guides: ' + path)
    return data.model_dump()


def validate_lifecycles(rows):
    by_path = {row['path']: row for row in rows}
    if len(by_path) != len(rows):
        raise ContextBlocked('Duplicate context document path')
    metadata = {path: lifecycle(path, row) for path, row in by_path.items()}
    for path, data in metadata.items():
        targets = data['supersedes']
        if len(set(targets)) != len(targets):
            raise ContextBlocked('Duplicate supersession target: ' + path)
        for target in targets:
            if target not in metadata or target == path or metadata[target]['status'] != 'superseded':
                raise ContextBlocked('Supersession must name a different indexed superseded document: ' + target)
    # Bounded iterative walk avoids recursion limits on large documentation sets.
    for start in metadata:
        pending, seen = [(start, frozenset())], set()
        while pending:
            path, ancestors = pending.pop()
            if path in ancestors:
                raise ContextBlocked('Document supersession cycle: ' + path)
            if path in seen:
                continue
            seen.add(path)
            pending.extend((target, ancestors | {path}) for target in metadata[path]['supersedes'])
    return metadata

class ProjectContextProposal(EnvelopeBase):
    model_config = ConfigDict(extra='forbid')
    baseline_digest: str
    documents: list[ContextDocument]
    retained_paths: list[str] = Field(default_factory=list)
    unresolved_decisions: list[str] = Field(default_factory=list)

class ContextRevisionCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    finding_id: str = Field(min_length=1, max_length=80)
    disposition: Literal['resolved', 'unresolved', 'superseded']
    evidence: str = Field(min_length=1)

    @field_validator('evidence')
    @classmethod
    def concrete_evidence(cls, value):
        if not value.strip():
            raise ValueError('Historical finding checks require evidence')
        return value


class ContextReviewOutput(EnvelopeBase):
    model_config = ConfigDict(extra='forbid')
    proposal_digest: str
    decision: Literal['approve', 'revise', 'needs_owner']
    findings: list[str] = Field(default_factory=list)
    policy_conflicts: list[str] = Field(default_factory=list)
    revision_context_digest: str = ''
    revision_checks: list[ContextRevisionCheck] = Field(default_factory=list)


def _path(root, relative):
    p = PurePosixPath(relative)
    if not relative or p.is_absolute() or '..' in p.parts or '\\' in relative or p.as_posix() != relative:
        raise ContextBlocked('Unsafe context path: ' + relative)
    current = Path(root)
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise ContextBlocked('Symlink context path refused: ' + relative)
    return current


def _read(root, relative):
    p = _path(root, relative)
    if not p.exists():
        return None
    if not p.is_file():
        raise ContextBlocked('Context file is not a bounded regular file: ' + relative)
    if p.stat().st_size > (MAX_BYTES * 4 if relative == JOURNAL else MAX_BYTES):
        raise _UnreadableDocument('Context file exceeds bounded size: ' + relative)
    try:
        return p.read_text(encoding='utf-8')
    except UnicodeError as exc:
        raise _UnreadableDocument('Context must be UTF-8: ' + relative) from exc


def _source_file_digest(root, relative, remaining):
    """Hash bounded regular source without following links or blocking on pipes."""
    from .scope import _parent_fd
    from .permissions import PermissionBreach
    try:
        with _parent_fd(Path(root) / relative) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ContextBlocked('Context source is not a regular file: ' + relative)
                if before.st_size > remaining:
                    raise ContextBlocked('Context source exceeds bounded byte inventory: ' + relative)
                content, size = hashlib.sha256(), 0
                while block := stream.read(min(1048576, remaining - size + 1)):
                    size += len(block)
                    if size > remaining:
                        raise ContextBlocked('Context source exceeds bounded byte inventory: ' + relative)
                    content.update(block)
                after = os.fstat(stream.fileno())
                identity = lambda info: (info.st_dev, info.st_ino, info.st_size,
                                         info.st_mtime_ns, info.st_ctime_ns)
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if identity(before) != identity(after) or identity(before) != identity(current):
                    raise ContextBlocked('Context source changed while hashing: ' + relative)
                return content.hexdigest(), size
    except (OSError, PermissionBreach) as error:
        raise ContextBlocked('Cannot safely hash context source: ' + relative) from error


def source_digests(root, row, observed):
    """Scoped guides bind source bytes; general navigation keeps structure binding."""
    patterns = _applicability(row['path'], row.get('applies_to'))
    if (row.get('purpose', 'guide') != 'guide' or row.get('status', 'active') != 'active'
            or any(pattern in {'**', '**/*', '*'} for pattern in patterns)):
        return None
    from .permissions import covers
    result, remaining = {}, ConfigDefaults().snapshot_max_bytes
    for path in observed['files']:
        if not _readable_doc(path) and any(covers(path, pattern) for pattern in patterns):
            result[path], size = _source_file_digest(root, path, remaining)
            remaining -= size
    return result


def source_conflicts(root, row, observed):
    current = source_digests(root, row, observed)
    if current is None:
        return []
    previous = row.get('source_digests')
    if previous is None:
        return ['Source baseline missing for guide: ' + row['path']]
    if (not isinstance(previous, dict) or any(not isinstance(path, str)
            or not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)
            for path, value in previous.items())):
        raise ContextBlocked('Invalid guide source baseline: ' + row['path'])
    return ['Guide source changed: ' + row['path'] + ' <- ' + path
            for path in sorted(set(previous) | set(current)) if previous.get(path) != current.get(path)]


def _observed(root, excluded_paths=(), generated_paths=None, dependency_paths=None):
    from .permissions import generated_roots
    defaults = ConfigDefaults()
    if generated_paths is not None:
        defaults.generated_paths = generated_paths
    if dependency_paths is not None:
        defaults.dependency_paths = dependency_paths
    disposable = generated_roots(SimpleNamespace(repo_root=root, cfg=SimpleNamespace(defaults=defaults)))
    files, docs, manifests, warnings = [], {}, {}, []
    excluded = {str(p).strip('/') for p in [*excluded_paths, *disposable, *FRAMEWORK_PATHS, 'CHANGELOG.md'] if p}
    def ignored(path):
        relative = Path(path).relative_to(root).as_posix()
        return any(relative == p or relative.startswith(p + '/') for p in excluded)
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP
                         and not (Path(directory) / d).is_symlink() and not ignored(Path(directory) / d))
        for name in sorted(names):
            if ignored(Path(directory) / name):
                continue
            rel = (Path(directory) / name).relative_to(root).as_posix()
            if name in {'CLAUDE.md', 'AGENTS.md'} or (rel.startswith('docs/') and name.endswith('.md')):
                try:
                    docs[rel] = _read(root, rel)
                except _UnreadableDocument as error:
                    if name in {'CLAUDE.md', 'AGENTS.md'}:
                        raise
                    warnings.append('Document not loaded: ' + str(error))
            if name in MANIFESTS or rel.startswith('scripts/') and Path(name).suffix in {'.py', '.sh', '.js', '.ts', '.mjs'}:
                file = _path(root, rel)
                if file.is_file():
                    manifests[rel] = hashlib.sha256(file.read_bytes()).hexdigest()
            if not name.startswith('.'):
                files.append(rel)
            if len(files) > 20000:
                raise ContextBlocked('Repository inventory exceeds 20000 files; narrow project root')
    return {'files': sorted(files), 'documents': docs, 'manifests': manifests, 'warnings': warnings}


def inspect_context(root, *, excluded_paths=(), generated_paths=None, dependency_paths=None):
    root = Path(root)
    try:
        raw = _read(root, INDEX)
        index = json.loads(raw) if raw is not None else None
        excluded_paths = list(excluded_paths) + (index.get('excluded_paths', []) if isinstance(index, dict) else [])
        if isinstance(index, dict):
            generated_paths = index.get('generated_paths') if generated_paths is None else generated_paths
            dependency_paths = index.get('dependency_paths') if dependency_paths is None else dependency_paths
        observed = _observed(root, excluded_paths, generated_paths, dependency_paths)
        baseline = digest(observed)
        if _path(root, JOURNAL).exists():
            return {'status': 'blocked', 'conflicts': ['Interrupted publication; run /kh-bootstrap --refresh'],
                    'digest': baseline, 'observed': observed}
        if raw is None:
            return {'status': 'missing', 'conflicts': [], 'digest': baseline, 'observed': observed,
                    'warnings': observed['warnings']}
        if index.get('schema_version') != 1 or not isinstance(index.get('documents'), list):
            raise ContextBlocked('Invalid project context schema')
        validate_lifecycles(index['documents'])
        cd.validate_catalog(index['documents'], lambda p: _read(root, p))
        conflicts = []
        if index.get('structure_digest') != digest(observed['files']):
            conflicts.append('Project file structure changed; refresh project navigation')
        if index.get('manifest_digests') != observed['manifests']:
            conflicts.append('Project manifests or guard scripts changed; refresh architecture and checks')
        indexed = set()
        for row in index['documents']:
            rel = row['path']; indexed.add(rel)
            conflicts.extend(source_conflicts(root, row, observed))
            if not _readable_doc(rel):
                raise ContextBlocked('Index contains a non-document path: ' + rel)
            current = _read(root, rel)
            if digest(current) != row['digest']:
                conflicts.append('Context changed: ' + rel)
            elif current is not None:
                for target in _links(rel, current):
                    if not _path(root, target).exists():
                        conflicts.append('Documentation target missing: ' + target)
        for rel in observed['documents']:
            if PurePosixPath(rel).name in {'CLAUDE.md', 'AGENTS.md'} and rel not in indexed:
                conflicts.append('Unindexed instruction file: ' + rel)
        if not {'CLAUDE.md', 'AGENTS.md'} <= indexed:
            conflicts.append('Root navigation files missing from index')
        expected = digest({k: v for k, v in index.items() if k != 'digest'})
        if index.get('digest') != expected:
            conflicts.append('Context index digest mismatch')
        return {'status': 'stale' if conflicts else 'ready', 'conflicts': conflicts,
                'digest': index.get('digest'), 'baseline_digest': baseline,
                'observed': observed, 'managed_paths': sorted(indexed), 'index': index,
                'warnings': observed['warnings']}
    except (ContextBlocked, ValueError, KeyError, TypeError) as exc:
        return {'status': 'blocked', 'conflicts': [str(exc)], 'digest': None}


def _readable_doc(path):
    return (PurePosixPath(path).name in {'CLAUDE.md', 'AGENTS.md'} or
            path.startswith('docs/') and path.endswith('.md')) and '..' not in PurePosixPath(path).parts


def _links(relative, content):
    for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', content):
        target = target.split('#', 1)[0]
        if not target or target.startswith(('https://', 'http://', 'mailto:')):
            continue
        if target.startswith('/') or '\\' in target:
            raise ContextBlocked('Unsafe documentation link: ' + target)
        candidate = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
        if candidate == '..' or candidate.startswith('../'):
            raise ContextBlocked('Documentation link escapes project: ' + target)
        yield candidate


def _applicability(path, proposed=None):
    if PurePosixPath(path).name in {'CLAUDE.md', 'AGENTS.md'}:
        parent = PurePosixPath(path).parent.as_posix()
        return ['**'] if parent == '.' else [parent + '/**']
    return proposed or ['**']


def _overlap(left, right):
    # Prefix comparison handles directory globs; fnmatch handles concrete paths.
    if left in {'**', '**/*', '*'} or right in {'**', '**/*', '*'}:
        return True
    if fnmatch.fnmatchcase(left, right) or fnmatch.fnmatchcase(right, left):
        return True
    def prefix(value):
        head = re.split(r'[*?\[]', value, 1)[0].rstrip('/')
        return head
    a, b = prefix(left), prefix(right)
    return bool(a and b and (a == b or a.startswith(b + '/') or b.startswith(a + '/')))


def validate_proposal(root, proposal, baseline):
    errors = []
    discarded = [d.path for d in proposal.documents if d.path in {cd.CATALOG_PATH, COMMANDS_PATH}]
    proposal.documents = [d for d in proposal.documents if d.path not in discarded]
    warnings = ['Ignored model-supplied controller document: ' + path for path in discarded]
    if proposal.status != 'success' or proposal.baseline_digest != baseline['digest']:
        errors.append('Proposal must succeed and bind current inventory digest')
    if proposal.unresolved_decisions:
        errors.append('Unresolved owner decisions: ' + '; '.join(proposal.unresolved_decisions))
    paths = [d.path for d in proposal.documents]
    if len(paths) != len(set(paths)):
        errors.append('Duplicate document paths')
    existing = baseline['observed']['documents']
    prior_raw = _read(root, INDEX)
    prior_rows = {r['path']: r for r in json.loads(prior_raw).get('documents', [])} if prior_raw else {}
    if 'prior_index_sha256' in baseline and baseline['prior_index_sha256'] != digest(prior_raw):
        errors.append('Context index changed during review; refresh project navigation')
    for document in proposal.documents:
        resolve_document_metadata(document, prior_rows.get(document.path))
    instructions = {p for p in existing if PurePosixPath(p).name in {'CLAUDE.md', 'AGENTS.md'}}
    if not instructions <= set(proposal.retained_paths) | set(paths):
        errors.append('Every existing instruction file must be retained in context')
    if not {'CLAUDE.md', 'AGENTS.md'} <= set(existing) | set(paths):
        errors.append('Both root navigation documents are required')
    for rel in proposal.retained_paths:
        if rel not in existing:
            errors.append('Retained path is not a tracked context document '
                          '(only CLAUDE.md, AGENTS.md and docs/**/*.md are tracked): ' + rel)
    rows = {p: {'path': p, 'applies_to': prior_rows.get(p, {}).get('applies_to', ['**']),
                **lifecycle(p, prior_rows.get(p, {}))} for p in proposal.retained_paths if p in existing}
    rows.update({d.path: d.model_dump() for d in proposal.documents})
    try:
        validate_lifecycles(list(rows.values()))
    except (ValueError, KeyError, TypeError) as error:
        errors.append(str(error))
    # Bind source before independent review and compare again before publication.
    bindings = baseline.setdefault('guide_source_bindings', {})
    for path, row in rows.items():
        try:
            if path in proposal.retained_paths and path not in paths and path in prior_rows:
                errors.extend(source_conflicts(root, prior_rows.get(path, row), baseline['observed']))
                continue
            sources = source_digests(root, row, baseline['observed'])
            if sources is not None:
                value = {'applies_to': _applicability(path, row.get('applies_to')), 'source_digests': sources}
                previous = bindings.get(path)
                if previous and previous['applies_to'] == value['applies_to'] and previous != value:
                    errors.append('Source changed during context review: ' + path)
                else:
                    bindings[path] = value
        except ContextBlocked as error:
            errors.append(str(error))
    # Links may name deterministic outputs; their bodies are derived only after approval.
    if cd.catalog_rows(rows.values()) or cd.CATALOG_PATH in existing:
        paths.append(cd.CATALOG_PATH)
    if 'commands' in baseline:
        paths.append(COMMANDS_PATH)
    total = sum(len(existing[p].encode()) for p in set(proposal.retained_paths) if p in existing)
    for document in proposal.documents:
        rel = document.path
        try:
            _path(root, rel)
            if rel not in {'CLAUDE.md', 'AGENTS.md'} and not (rel.startswith('docs/agent-guide/') and rel.endswith('.md')):
                raise ContextBlocked('Publication path outside documentation allowlist: ' + rel)
            old = _read(root, rel)
            previous = prior_rows.get(rel, {})
            replaceable = previous.get('managed') is True and previous.get('digest') == digest(old)
            if old is not None and old != document.content and not replaceable:
                raise ContextBlocked('Preserve owner documentation verbatim: ' + rel)
            if not document.content.strip() or not document.applies_to:
                raise ContextBlocked('Empty document or applicability: ' + rel)
            validate_library_claims(root, document)
            validate_command_guide(root, document, baseline.get('commands', {}))
            total += len(document.content.encode()) if rel not in proposal.retained_paths else 0
            for candidate in _links(rel, document.content):
                if not _path(root, candidate).exists() and candidate not in paths:
                    raise ContextBlocked('Broken documentation link: ' + candidate)
        except ContextBlocked as exc:
            errors.append(str(exc))
    if total > MAX_BYTES:
        errors.append('Documentation exceeds context budget; curate existing canonical docs first')
    return {'valid': not errors, 'findings': errors, 'warnings': warnings}


def _atomic(root, relative, text):
    target = _path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.context-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        _path(root, relative)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def recover_publication(root):
    raw = _read(root, JOURNAL)
    if raw is None:
        return
    data = json.loads(raw)
    entries = data.get('entries', [])
    if data.get('schema_version') != 1 or not entries:
        raise ContextBlocked('Invalid publication journal; preserve it for manual recovery')
    for entry in entries:
        rel = entry['path']
        if rel != INDEX and rel not in {'CLAUDE.md', 'AGENTS.md'} and not (rel.startswith('docs/agent-guide/') and rel.endswith('.md')):
            raise ContextBlocked('Invalid journal publication path')
        current = _read(root, rel)
        if current not in (entry['before'], entry['after']):
            raise ContextBlocked('Owner edit during recovery: ' + rel)
    for entry in reversed(entries):
        if entry['before'] is None:
            _path(root, entry['path']).unlink(missing_ok=True)
        else:
            _atomic(root, entry['path'], entry['before'])
    _path(root, JOURNAL).unlink()


def command_declarations(root, cfg, observed):
    defaults = getattr(cfg, 'defaults', ConfigDefaults())
    scripts = {}
    for path in sorted(observed['manifests']):
        if PurePosixPath(path).name == 'package.json':
            try:
                data = json.loads(_read(root, path))
                declared = data.get('scripts', {})
                if not isinstance(declared, dict) or any(not isinstance(v, str) for v in declared.values()):
                    raise ValueError('scripts must map names to strings')
                scripts[path] = declared
            except (ValueError, TypeError, AttributeError) as error:
                raise ContextBlocked('Cannot read command declarations from ' + path + ': ' + str(error)) from error
    data = {'quality_checks': defaults.quality_checks,
            'required_quality_checks': sorted(set(defaults.required_quality_checks)), 'package_scripts': scripts}
    # Copies remap project-local absolute argv. Keep derived guide bytes identical.
    prefix = json.dumps(str(Path(root).resolve()), ensure_ascii=False)[1:-1]
    return json.loads(json.dumps(data, ensure_ascii=False).replace(prefix, '<project>'))


def _prose(content):
    return re.sub(r'(?ms)^[ ]{0,3}(?=[^\n]*\n)(?P<fence>(?P<mark>`|~)(?P=mark){2,})[^\n]*\n.*?'
                  r'(?:^[ ]{0,3}(?P=fence)(?P=mark)*[ \t]*(?:\n|\Z)|\Z)', lambda m: '\n' * m[0].count('\n'), content.replace('\r\n', '\n'))


def validate_library_claims(root, document):
    """Check supported library statements without executing project source."""
    if (document.purpose, document.status) != ('guide', 'active'):
        return
    prose = _prose(document.content)
    phrase = r'Use only (?:the )?Python standard library'
    rule = r'(?im)(?:\A|(?<=\n\n))' + phrase + r'(?: \((?P<source>[^\n]+):(?P<line>[0-9]+)\))?\.?[ \t]*(?=\n\n|\n?\Z)'
    for claim in re.finditer(rule, prose if document.path.startswith('docs/agent-guide/') else ''):
        info = inspect_context(root)
        rows = {row['path']: row for row in info.get('index', {}).get('documents', [])}
        owners = {p: c for p, c in info.get('observed', {}).get('documents', {}).items()
                  if not rows.get(p, {}).get('managed') or rows[p].get('digest') != digest(c)}
        if owners.get(document.path) == document.content:
            continue
        source = _prose(owners.get(claim['source'], ''))
        line = next((m[0] for m in re.finditer(r'(?im)(?:\A|(?<=\n\n))[ ]{0,3}(?:[-*] )?' + phrase + r'\.?[ \t]*(?=\n\n|\n?\Z)', source)
                     if str(source.count('\n', 0, m.start()) + 1) == claim['line'].lstrip('0')), '')
        if not (claim['source'] and PurePosixPath(claim['source']).name in {'CLAUDE.md', 'AGENTS.md'}
                and line
                and all(p == posixpath.normpath(p) and scope._contains(
                    str(PurePosixPath(claim['source']).parent / '**'), p) for p in _applicability(document.path, document.applies_to))):
            raise ContextBlocked('Unsubstantiated dependency rule: ' + document.path
                                 + '; cite a preserved owner instruction at file:line within the guide scope')
    pattern = (r'(?m)(?:\A|(?<=\n\n))(?:json\.)?JSONDecodeError\s+(?:has\s+no|lacks|does\s+not\s+provide)\s+(?:parser\s+)?'
               r'(?P<properties>(?:line(?:\s+number)?|lineno|column(?:\s+number)?|colno|position)\b[^\n]*?)'
               r'\((?P<path>[\w./-]+\.py):(?P<line>\d+)\)\.[ \t]*(?=\n\n|\n?\Z)')
    for claim in re.finditer(pattern, prose):
        if set(re.findall(r'[A-Za-z_]+', claim['properties'])) - {'line', 'number', 'lineno', 'column', 'colno', 'position', 'parser', 'information', 'or', 'and'}:
            continue
        try:
            tree = ast.parse(_read(root, claim['path']) or '')
        except (SyntaxError, RecursionError):
            continue
        imports = {a for n in tree.body if isinstance(n, ast.Import)
                   for a in n.names if a.name == 'json' and a.asname in {None, 'json'}}
        if any(os.path.lexists(Path(root) / folder / suffix) for folder in {PurePosixPath(), PurePosixPath(claim['path']).parent}
               for suffix in ('json.py', 'json')):
            continue
        dynamic = {'exec', 'eval', '__import__', 'setattr', 'delattr', 'globals', 'locals', 'import_module', 'reload'}
        if not imports or any(n not in imports and (
               getattr(n, 'name', None) in {'json', 'JSONDecodeError', '*'} or getattr(n, 'asname', None) in {'json', 'JSONDecodeError'}
               or isinstance(n, (ast.Name, ast.Attribute)) and not isinstance(n.ctx, ast.Load)
                  and any(isinstance(v, ast.Name) and v.id in {'json', 'JSONDecodeError'} for v in ast.walk(n))
               or isinstance(n, ast.arg) and n.arg in {'json', 'JSONDecodeError'}
               or isinstance(n, ast.Call) and getattr(n.func, 'id', getattr(n.func, 'attr', '')) in dynamic
               or isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'sys' and n.attr in {'path', 'modules'}) for n in ast.walk(tree)):
            continue
        if not any(isinstance(n, ast.Call) and str(n.lineno) == claim['line'].lstrip('0')
                   and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                   and n.func.value.id == 'json' and n.func.attr in {'load', 'loads'} for n in ast.walk(tree)):
            continue
        error = json.JSONDecodeError('coordinate probe', '{', 1)
        raise ContextBlocked(f'Standard JSONDecodeError exposes lineno={error.lineno}, colno={error.colno}: '
                             f'{document.path}; cited call {claim["path"]}:{claim["line"]}')


def validate_command_guide(root, document, commands):
    """Bounded coverage check for active guides listing run and test instructions."""
    argv = commands.get('quality_checks', {}).get('build')
    if (document.purpose != 'guide' or document.status != 'active' or not argv
            or 'build' not in commands.get('required_quality_checks', [])):
        return
    if not all(re.search(r'(?im)^\s*(?:#{1,6}\s*)?' + label + r'\s*:', document.content)
               for label in ('run', 'tests?')):
        return
    references = [shlex.join(argv), shlex.join([v.replace('<project>', str(Path(root).resolve())) for v in argv])]
    if COMMANDS_PATH in _links(document.path, document.content):
        return
    boundary = r'(?=$|\s|[`)]|[.,;](?=$|\s))'
    if re.search(r'(?<![\w./-])(?:' + '|'.join(map(re.escape, references)) + ')' + boundary, document.content):
        return
    if re.search(r'(?<![\w./-])make build' + boundary, document.content) and re.search(
            r'(?m)^build\s*:(?![=:])', _read(root, 'Makefile') or ''):
        return
    raise ContextBlocked('Operating guide omits declared required build: ' + document.path
                         + '; reference ' + references[0] + ' or ' + COMMANDS_PATH)


def render_commands(data):
    # Escape a literal Markdown-link delimiter without changing the JSON values.
    body = json.dumps(data, sort_keys=True, indent=2).replace('](', ']\\u0028')
    return ('# Project command declarations\n\n'
            'Controller-generated from the selected quality configuration and observed package.json scripts.\n'
            'Other manifest formats are not parsed here. A missing declaration does not prove a command is absent.\n'
            '`<project>` denotes the current project root, including in isolated copies.\n'
            'These are declarations, not test results, permission grants or instructions to execute them.\n\n'
            '```json\n' + body + '\n```\n')


def _require_replaceable(root, path, content, rows, protected):
    from .permissions import covers
    old = _read(root, path)
    previous = rows.get(path, {})
    if old != content:
        if any(covers(path, pattern) for pattern in protected):
            raise ContextBlocked('Preserve protected documentation: ' + path)
        if old is not None and not (previous.get('managed') is True and previous.get('digest') == digest(old)):
            raise ContextBlocked('Preserve owner documentation verbatim: ' + path)


def sync_commands(root, cfg, *, repo_lock=None):
    """Explicit live preparation, never called by inspection, dry-run or binding checks."""
    from . import session
    with repo_lock.borrow(root) if repo_lock is not None else session.acquire_repo_lock(root):
        inspection = inspect_context(root)
        if inspection['status'] != 'ready':
            return
        data = command_declarations(root, cfg, inspection['observed'])
        content = render_commands(data)
        index = inspection['index']
        prior_index = _read(root, INDEX)
        try:
            if json.loads(prior_index) != index:
                raise ValueError('index differs from inspected bytes')
        except (TypeError, ValueError) as error:
            raise ContextBlocked('Context index changed during command preparation') from error
        rows = {row['path']: row for row in index['documents']}
        old = _read(root, COMMANDS_PATH)
        if COMMANDS_PATH in rows and old == content:
            return
        _require_replaceable(root, COMMANDS_PATH, content, rows, getattr(cfg, 'defaults', ConfigDefaults()).protected_files)
        managed = old is None or rows.get(COMMANDS_PATH, {}).get('managed') is True
        previous = rows.get(COMMANDS_PATH, {})
        rows[COMMANDS_PATH] = {'path': COMMANDS_PATH, 'digest': digest(content),
            **lifecycle(COMMANDS_PATH, previous), 'applies_to': previous.get('applies_to', ['**']), 'managed': managed,
            'provenance': 'controller-generated' if managed else 'owner-preserved',
            **({'source_digests': previous['source_digests']} if 'source_digests' in previous else {})}
        files = sorted(set(inspection['observed']['files']) | {COMMANDS_PATH})
        index.update(documents=[rows[p] for p in sorted(rows)], structure_digest=digest(files),
                     commands_inputs_digest=digest(data))
        index.pop('digest'); index['digest'] = digest(index)
        contents = {COMMANDS_PATH: content}
        _context_packet(root, {'index': index, 'digest': index['digest'],
            'observed': {**inspection['observed'], 'files': files}}, '', [], contents=contents)
        if len(json.dumps(index, indent=2).encode()) + 1 > MAX_BYTES:
            raise ContextBlocked('Context index exceeds bounded size; reduce document metadata')
        _write_context(root, index, contents, {COMMANDS_PATH: old, INDEX: prior_index})


def _publication_content(root, proposal, baseline):
    """Project the exact reviewed documents and index without publishing files."""
    proposal_digest = digest(proposal.model_dump())
    contents = {p: baseline['observed']['documents'][p] for p in proposal.retained_paths}
    contents.update({d.path: d.content for d in proposal.documents})
    previous = _read(root, INDEX)
    prior_rows = {row['path']: row for row in json.loads(previous).get('documents', [])} if previous else {}
    applicability = {path: row.get('applies_to', ['**']) for path, row in prior_rows.items()}
    def managed(path):
        if path not in baseline['observed']['documents']:
            return True
        prior = prior_rows.get(path, {})
        return prior.get('managed') is True and prior.get('digest') == digest(baseline['observed']['documents'][path])
    applicability.update({d.path: d.applies_to for d in proposal.documents})
    metadata = {p: lifecycle(p, next((d.model_dump() for d in proposal.documents if d.path == p), prior_rows.get(p, {})))
                for p in contents}
    derived = {}
    if 'commands' in baseline:
        derived[COMMANDS_PATH] = render_commands(baseline['commands'])
        metadata[COMMANDS_PATH] = lifecycle(COMMANDS_PATH, prior_rows.get(COMMANDS_PATH, {}))
    rows = [{'path': p, **data} for p, data in metadata.items()]
    if cd.catalog_rows(rows) or cd.CATALOG_PATH in prior_rows or cd.CATALOG_PATH in contents:
        derived[cd.CATALOG_PATH] = cd.render_catalog(rows)
    for path, content in derived.items():
        _require_replaceable(root, path, content, prior_rows, baseline.get('protected_files', []))
        contents[path] = content
        metadata[path] = lifecycle(path, prior_rows.get(path, {}))
    index = {'schema_version': 1, 'proposal_digest': proposal_digest,
             'excluded_paths': baseline.get('excluded_paths', []),
             'generated_paths': baseline.get('generated_paths', ConfigDefaults().generated_paths),
             'dependency_paths': baseline.get('dependency_paths', ConfigDefaults().dependency_paths),
             'structure_digest': digest(sorted(set(baseline['observed']['files']) | set(contents))),
             'manifest_digests': baseline['observed']['manifests'],
             'documents': [{'path': p, 'digest': digest(c), 'applies_to': _applicability(p, applicability.get(p)),
                            **metadata[p],
                            'managed': managed(p),
                            'provenance': 'controller-generated' if managed(p) else 'owner-preserved'}
                           for p, c in sorted(contents.items())]}
    proposed = {document.path for document in proposal.documents}
    for row in index['documents']:
        path = row['path']
        if path in proposed or path not in prior_rows:
            sources = baseline.get('guide_source_bindings', {}).get(path, {}).get('source_digests')
        elif path in derived:
            sources = source_digests(root, row, baseline['observed'])
        else:
            sources = prior_rows.get(path, {}).get('source_digests')
        if sources is not None:
            row['source_digests'] = sources
    if 'commands' in baseline:
        index['commands_inputs_digest'] = digest(baseline['commands'])
    index['digest'] = digest(index)
    if len(json.dumps(index, indent=2).encode()) + 1 > MAX_BYTES:
        raise ContextBlocked('Context index exceeds bounded size; reduce document metadata')
    return index, contents


def publish_context(root, proposal, review, baseline):
    prior_index = _read(root, INDEX)
    validation = validate_proposal(root, proposal, baseline)
    if not validation['valid']:
        raise ContextBlocked('; '.join(validation['findings']))
    if review.status != 'success' or review.decision != 'approve' or review.findings or review.policy_conflicts or review.proposal_digest != digest(proposal.model_dump()):
        raise ContextBlocked('Independent context review is missing, unresolved or stale')
    if digest(_observed(Path(root), baseline.get('excluded_paths', []), baseline.get('generated_paths'), baseline.get('dependency_paths'))) != baseline['digest']:
        raise ContextBlocked('Repository changed during bootstrap; rerun /kh-bootstrap --refresh')
    index, contents = _publication_content(root, proposal, baseline)
    _write_context(root, index, contents, {**baseline['observed']['documents'], INDEX: prior_index})
    return {'status': 'ready', 'digest': index['digest'], 'proposal_digest': index['proposal_digest'],
            'managed_paths': sorted(contents),
            'warnings': baseline['observed'].get('warnings', []) + validation['warnings']}


def _write_context(root, index, contents, before):
    entries = [{'path': path, 'before': before.get(path), 'after': content}
               for path, content in sorted(contents.items()) if before.get(path) != content]
    entries.append({'path': INDEX, 'before': before[INDEX], 'after': json.dumps(index, indent=2) + '\n'})
    for path in [*contents, INDEX]:
        if _read(root, path) != before.get(path):
            raise ContextBlocked('Owner changed publication target: ' + path)
    _atomic(root, JOURNAL, json.dumps({'schema_version': 1, 'entries': entries}))
    for entry in entries:
        if _read(root, entry['path']) != entry['before']:
            raise ContextBlocked('Owner changed publication target: ' + entry['path'])
        _atomic(root, entry['path'], entry['after'])
    _path(root, JOURNAL).unlink()


def _context_error(inspection):
    detail = '; '.join(inspection['conflicts'])
    if 'observed' not in inspection:
        return ContextBlocked('Project context is blocked: ' + detail +
                              '; --refresh cannot resolve this conflict; fix the reported cause first')
    return ContextBlocked('Project context is ' + inspection['status'] +
                          '; run /kh-bootstrap --refresh: ' + detail)


def build_context_packet(root, request, allowed_paths):
    inspection = inspect_context(root)
    if inspection['status'] != 'ready':
        raise _context_error(inspection)
    return _context_packet(root, inspection, request, allowed_paths)


def _project_map(files):
    counts = {}
    for path in files:
        directory = '/'.join(PurePosixPath(path).parts[:-1][:2]) or '.'
        counts[directory] = counts.get(directory, 0) + 1
    rows, used = [], 2
    for directory, count in sorted(counts.items()):
        row = {'path': directory, 'area': directory.split('/')[0] if directory != '.' else 'root',
               'file_count': count}
        size = len(json.dumps(row).encode()) + (2 if rows else 0)
        if used + size > MAX_MAP_BYTES:
            break
        rows.append(row); used += size
    return rows, {'used_bytes': used, 'limit_bytes': MAX_MAP_BYTES,
                  'omitted_directories': len(counts) - len(rows),
                  'omitted_files': len(files) - sum(row['file_count'] for row in rows)}


def _context_packet(root, inspection, request, allowed_paths, *, contents=None):
    """Construct a bounded packet from a validated index, including projections."""
    requested = list(allowed_paths or [])
    documents, selected, omitted, conditional = [], [], [], []
    words = set(re.findall(r'[\w-]+', request.casefold()))
    aliases = {'auth': 'authentication', 'login': 'authentication', 'دخول': 'authentication',
               'مصادقة': 'authentication', 'db': 'database', 'sql': 'database',
               'البيانات': 'database', 'واجهة': 'ui', 'الواجهة': 'ui', 'أمان': 'security'}
    topics = words | {aliases[word] for word in words if word in aliases}
    for row in inspection['index']['documents']:
        # Validate indexed guides even when their bodies are not selected. This
        # also protects documentation projection before source promotion.
        content = (contents[row['path']] if contents is not None and row['path'] in contents
                   else _read(root, row['path']))
        data = lifecycle(row['path'], row)
        applies_to = _applicability(row['path'], row.get('applies_to'))
        reason = None
        if data['status'] != 'active':
            reason = data['status']
        elif data['purpose'] != 'guide':
            reason = 'non_guide'
        if reason is None and data.get('read_when'):
            conditional.append({'path': row['path'], 'digest': row['digest'], **data, 'applies_to': applies_to})
            omitted.append({'path': row['path'], 'reason': 'condition_pending'})
            continue
        path_match = row['path'] == cd.CATALOG_PATH or not requested or any(_overlap(pattern, path) for pattern in applies_to for path in requested)
        instruction = PurePosixPath(row['path']).name in {'CLAUDE.md', 'AGENTS.md'}
        topic_match = [] if instruction else sorted(topics & set(data['topics']))
        if reason or not (path_match or topic_match):
            omitted.append({'path': row['path'], 'reason': reason or 'not_applicable'})
            continue
        why = ('root_instruction' if row['path'] in {'CLAUDE.md', 'AGENTS.md'} else
               'directory_instruction' if instruction else 'unscoped' if not requested else
               'path_match' if path_match else 'topic_match')
        selected.append({'path': row['path'], 'reason': why, 'matched_topics': topic_match})
        documents.append({'path': row['path'], 'digest': row['digest'],
                          'content': content, 'provenance': 'reviewed-project-context',
                          **data, 'applies_to': applies_to,
                          'scope_note': 'Apply directory instructions only within their paths; topic hints add guides, never remove required rules.'})
    selection = {'selected': selected, 'omitted': omitted, 'policy': 'active-guides; mandatory directory rules; advisory topic hints'}
    project_map, map_budget = _project_map(inspection['observed']['files'])
    extra = ({'conditional_documents': conditional, 'conditional_catalog_digest': digest(conditional)}
             if conditional else {})
    extra['warnings'] = inspection['observed'].get('warnings', [])
    used = len(json.dumps({**extra, 'documents': documents,
                           'manifest_digests': inspection['observed']['manifests'], 'selection': selection}).encode())
    if used > MAX_BYTES:
        raise ContextBlocked('Required project context exceeds 128 KiB; curate canonical documentation')
    return {**extra, 'digest': inspection['digest'], 'documents': documents, 'selection': selection,
            'budget': {'used_bytes': used, 'limit_bytes': MAX_BYTES},
            'allowed_paths': requested, 'request_digest': digest(request),
            'project_map': project_map,
            'map_budget': map_budget,
            'map_provenance': 'Observed directory areas; no person/team ownership inferred',
            'manifest_digests': inspection['observed']['manifests']}


def resolve_conditional_context(root, packet, decisions, *, baseline=None):
    """Resolve a frozen catalog without changing approval inputs or source files."""
    def read(path):
        if baseline is None:
            return _read(root, path)
        entry = baseline.entries.get(path)
        if entry is None or entry.kind != 'file':
            raise ContextBlocked('Selected guide missing from task baseline: ' + path)
        return entry.data.decode('utf-8')
    return cd.resolve_context(packet, decisions, read, MAX_BYTES)


def _revision_context(history, attempt):
    """Prior role output is inspectable evidence, never an approval or grant."""
    findings, summaries = {}, []
    for record in history:
        review = record.get('review') or {}
        sources = [('validation', record['validation']['findings']),
                   ('finding', review.get('findings', [])),
                   ('policy_conflict', review.get('policy_conflicts', []))]
        for kind, texts in sources:
            for text in texts:
                identity = 'ctx-' + digest({'kind': kind, 'text': text})
                row = findings.setdefault(identity, {'id': identity, 'kind': kind,
                                                     'text': text, 'attempts': []})
                if record['attempt'] not in row['attempts']:
                    row['attempts'].append(record['attempt'])
        summaries.append({key: value for key, value in record.items() if key != 'proposal'})
    previous = history[-1] if history else None
    return {'schema_version': 1, 'attempt': attempt,
            'digest_scope': 'controller-canonical-before-workspace-path-mapping',
            'previous_proposal': previous['proposal'] if previous else None,
            'previous_proposal_digest': previous['proposal_digest'] if previous else None,
            'findings': list(findings.values()), 'history': summaries}


def _verify_revision_review(review, revision, revision_digest):
    required = {row['id'] for row in revision['findings']}
    if required or review.revision_context_digest:
        if review.revision_context_digest != revision_digest:
            raise ContextBlocked('Context review does not bind current revision history; '
                                 'set revision_context_digest to ' + revision_digest)
    ids = [check.finding_id for check in review.revision_checks]
    if len(ids) != len(set(ids)) or set(ids) != required:
        raise ContextBlocked('Context review must check every historical finding exactly once; '
                             f'missing={sorted(required - set(ids))}; '
                             f'unknown={sorted(set(ids) - required)}; duplicate IDs are forbidden')
    if review.decision == 'approve' and any(c.disposition == 'unresolved' for c in review.revision_checks):
        raise ContextBlocked('Context approval leaves historical findings unresolved')


def bootstrap_with_budget(run, decision, *, refresh=False):
    from .execution_policy import resolve_bootstrap_policy
    execution_budget.start_budget(run, resolve_bootstrap_policy(run.cfg, decision), stage='bootstrap')
    return bootstrap(run, max_repairs=decision.get('max_bootstrap_repairs', 2), refresh=refresh)


def bootstrap(run, *, max_repairs=2, refresh=False):
    from . import agents
    if type(max_repairs) is not int or not 0 <= max_repairs <= 5:
        raise ContextBlocked('Invalid bootstrap repair budget')
    root = Path(run.repo_root)
    if _path(root, JOURNAL).exists():
        recover_publication(root)
    excluded = []
    defaults = getattr(run.cfg, 'defaults', None)
    observability = getattr(run.cfg, 'observability', None)
    for value in (getattr(defaults, 'data_dir', None), getattr(observability, 'db', None)):
        if value:
            path = Path(value)
            try:
                excluded.append((path if path.is_absolute() else root / path).relative_to(root).as_posix())
            except ValueError:
                pass
    excluded.extend([p + suffix for p in list(excluded) for suffix in ('-wal', '-shm')])
    generated = getattr(defaults, 'generated_paths', ConfigDefaults().generated_paths)
    dependencies = getattr(defaults, 'dependency_paths', ConfigDefaults().dependency_paths)
    inspection = inspect_context(root, excluded_paths=excluded, generated_paths=generated, dependency_paths=dependencies)
    if inspection['status'] == 'ready' and not refresh:
        sync_commands(root, run.cfg, repo_lock=getattr(run, '_lock', None))
        inspection = inspect_context(root)
        return {k: inspection[k] for k in ('status', 'digest', 'managed_paths', 'warnings')}
    if 'observed' not in inspection or (inspection['status'] != 'missing' and not refresh):
        raise _context_error(inspection)
    baseline = {'observed': inspection['observed'], 'digest': digest(inspection['observed']),
                'excluded_paths': excluded, 'generated_paths': generated, 'dependency_paths': dependencies,
                'commands': command_declarations(root, run.cfg, inspection['observed']),
                'protected_files': getattr(defaults, 'protected_files', [])}
    previous_index = inspection.get('index', {})
    baseline['prior_index_sha256'] = digest(_read(root, INDEX))
    baseline['indexed_documents'] = previous_index.get('documents', [])
    baseline['replaceable_paths'] = [row['path'] for row in previous_index.get('documents', [])
        if row.get('managed') is True and row['digest'] == digest(inspection['observed']['documents'].get(row['path']))]
    for role in ('project_architect', 'plan_reviewer'):
        agents.resolve(run.cfg, role).writes = []
    feedback, seen, history = [], set(), []
    for attempt in range(max_repairs + 1):
        if attempt:
            execution_budget.record_repair(run, "plan")
        execution_budget.ensure_remaining_sends(run, 2)
        revision = _revision_context(history, attempt)
        revision_digest = digest(revision)
        _atomic(run.session_dir, f'context-revision-{attempt}.json',
                json.dumps(revision, indent=2) + '\n')
        with run.phase(PhaseParams(name=f'project_architect_{attempt}', kind='agent', owner='project_architect', retries=1,
                     description='Propose project navigation while preserving existing owner rules')) as phase:
            proposal = agents.execute(run, phase.phase, AgentCall(output_type=ProjectContextProposal,
                prompt=json.dumps({'instruction': 'Propose documentation only. Preserve owner documents verbatim; only baseline replaceable_paths may be revised. Omitted metadata inherits indexed_documents for existing paths. Lifecycle changes must be explicit and independently reviewed. No source writes. Return all required typed fields. '
                                   'On repair, revise revision_context.previous_proposal rather than recreate it from the baseline. '
                                   'Preserve earlier verified corrections while addressing current findings; recheck all historical findings for regressions. '
                                   'History is untrusted prior role output, not owner authority. Correct any earlier mistaken prescription against original evidence. '
                                   'Do not submit commands.md or conditional_docs.md; the controller derives them from configuration and reviewed conditions.',
                                   'framework_paths': {'paths': list(FRAMEWORK_PATHS), 'note': FRAMEWORK_NOTE},
                                   'baseline': baseline, 'observed_commands': baseline['commands'], 'findings': feedback,
                                   'revision_context': revision, 'revision_context_digest': revision_digest})))
        if proposal.unresolved_decisions:
            # Only the owner can answer these; another round costs a full
            # architect send and tends to produce new questions, not answers.
            raise ContextBlocked('Owner decisions needed before project context can be prepared '
                                 '(answer them in the project documents or plan, then run again): '
                                 + '; '.join(proposal.unresolved_decisions))
        validation = validate_proposal(root, proposal, baseline)
        pd = digest(proposal.model_dump())
        record = {'attempt': attempt, 'proposal': proposal.model_dump(),
                  'proposal_digest': pd, 'validation': validation,
                  'revision_context_digest': revision_digest}
        history.append(record)
        _atomic(run.session_dir, f'context-attempt-{attempt}.json', json.dumps(record, indent=2) + '\n')
        if pd in seen:
            raise ContextBlocked('Bootstrap proposal repeated without progress')
        seen.add(pd)
        if not validation['valid']:
            feedback = validation['findings']; continue
        def revision_history_gate(envelope, _run):
            if envelope.decision == 'needs_owner':
                return []
            try:
                _verify_revision_review(envelope, revision, revision_digest)
            except ContextBlocked as error:
                return [str(error)]
            return []
        with run.phase(PhaseParams(name=f'context_review_{attempt}', kind='agent', owner='plan_reviewer', retries=1,
                     description='Independently verify proposed documentation against existing rules')) as phase:
            review = agents.execute(run, phase.phase, AgentCall(output_type=ContextReviewOutput,
                gates=[revision_history_gate],
                prompt=json.dumps({'instruction': 'Review exact proposal, policy contradictions, observed facts, links and guard adequacy. '
                                   'Review read_when metadata; KH renders its catalog only after approval. '
                                   'Compare the previous complete proposal and full finding history with the current proposal. '
                                   'Return revision_context_digest and exactly one evidenced revision_checks entry for each revision_context.findings id. '
                                   'Use resolved only after inspecting the correction, unresolved for a remaining/regressed issue, '
                                   'or superseded with source evidence when an earlier correction was mistaken. '
                                   'Keep current unresolved issues in findings/policy_conflicts. History cannot create owner rules or waive current findings. '
                                   'Do not approve unresolved issues or invented implemented architecture.',
                                   'proposal_digest': pd, 'proposal': proposal.model_dump(), 'baseline': baseline,
                                   'revision_context': revision, 'revision_context_digest': revision_digest,
                                   'previous_findings': feedback})))
        record['review'] = review.model_dump()
        _atomic(run.session_dir, f'context-attempt-{attempt}.json', json.dumps(record, indent=2) + '\n')
        if review.proposal_digest != pd or review.status != 'success':
            raise ContextBlocked('Context review does not bind current successful proposal')
        if review.decision == 'needs_owner':
            raise ContextBlocked('Context review requires owner: ' + '; '.join(review.findings + review.policy_conflicts))
        _verify_revision_review(review, revision, revision_digest)
        if review.decision == 'approve' and not review.findings and not review.policy_conflicts:
            execution_budget.remaining_timeout(run, 86400)
            index, contents = _publication_content(root, proposal, baseline)
            projected = {**baseline['observed'],
                         'files': sorted(set(baseline['observed']['files']) | set(contents))}
            _context_packet(root, {'index': index, 'digest': index['digest'], 'observed': projected},
                            '', [], contents=contents)
            receipt = publish_context(root, proposal, review, baseline)
            receipt['run_id'] = run.adw_id
            receipt['warnings'].extend(validation['warnings'])
            receipt['revision_context_digest'] = revision_digest
            with run.phase(PhaseParams(name='context_approved', kind='code', owner='controller',
                           description='Record independently approved project context and loading budget')) as phase:
                packet = build_context_packet(root, '', [])
                if hasattr(phase, 'log'):
                    phase.log(context_receipt=receipt, budget=packet['budget'])
            _atomic(run.session_dir, 'context-approval.json', json.dumps(receipt, indent=2) + '\n')
            write_understanding(run, packet)
            return receipt
        feedback = review.findings + review.policy_conflicts
        if not feedback:
            raise ContextBlocked('Context revision requires actionable findings')
    raise ContextBlocked('Bootstrap repair budget exhausted: ' + '; '.join(feedback))


def write_understanding(run, packet):
    """Save a factual, controller-owned orientation receipt without another model call."""
    from .scope import _atomic_json
    areas = {}
    for row in packet.get('project_map', []):
        areas[row['area']] = areas.get(row['area'], 0) + row.get('file_count', 1)
    defaults = getattr(run.cfg, 'defaults', None)
    record = {'schema_version': 1, 'run_id': run.adw_id, 'context_digest': packet['digest'],
        'source_areas': dict(sorted(areas.items())), 'manifest_paths': sorted(packet.get('manifest_digests', {})),
        'documents': [{key: row.get(key) for key in ('path', 'digest', 'purpose', 'status', 'provenance', 'applies_to')}
                      for row in packet.get('documents', [])],
        'selection': packet.get('selection', {}), 'budget': packet.get('budget', {}),
        'map_budget': packet.get('map_budget', {}),
        'configured_checks': sorted(defaults.quality_checks) if defaults is not None else None,
        'required_checks': list(defaults.required_quality_checks) if defaults is not None else None,
        'blocking_context_decisions': [],
        'runtime_verification': 'not performed by context preparation'}
    if packet.get('conditional_documents'):
        record['conditional_catalog_digest'] = packet['conditional_catalog_digest']
        record['conditional_selection'] = packet.get('conditional_selection', {'status': 'pending'})
    directory = Path(run.session_dir)
    _atomic_json(directory/'project-understanding.json', record)
    lines = ['# Project understanding', '', 'Generated from observed paths and independently reviewed project context.', '',
             'Context digest: `' + packet['digest'] + '`.', '', '## Observed source areas', '',
             *[f'- {area}: {count} observed paths.' for area,count in sorted(areas.items())], '',
             f"Directory map omitted {record['map_budget'].get('omitted_directories', 0)} groups "
             f"({record['map_budget'].get('omitted_files', 0)} observed paths).", '',
             '## Context documents', '', *['- ' + row['path'] for row in record['documents']], '',
             '## Configured verification', '',
             'Configured checks: ' + (', '.join(record['configured_checks'] or []) or ('not recorded' if record['configured_checks'] is None else 'none')) + '.',
             'Required checks: ' + (', '.join(record['required_checks'] or []) or ('not recorded' if record['required_checks'] is None else 'none')) + '.', '',
             'No recorded unresolved bootstrap decision remains. This is not a claim of complete project knowledge.',
             'Context preparation does not execute application checks or prove provider authentication.', '',
             '## Selection receipt', '', '```json', json.dumps(record['selection'],ensure_ascii=False,indent=2), '```', '']
    if 'conditional_selection' in record:
        lines.extend(['## Conditional read decisions', '', '```json',
                      json.dumps(record['conditional_selection'], ensure_ascii=False, indent=2), '```', ''])
    _atomic(directory, 'project-understanding.md', '\n'.join(lines))
    def reference(path):
        resolved = path.resolve()
        try:
            return resolved.relative_to(Path(run.repo_root).resolve()).as_posix()
        except ValueError:
            return str(resolved)
    return {'context_digest': packet['digest'], 'receipt_path': reference(directory/'project-understanding.json'),
            'report_path': reference(directory/'project-understanding.md')}
