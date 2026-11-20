"""Load a local owner reply for one stopped, tracked task."""
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import delta, evidence, evidence_packet, permissions, project, scope

UUID = r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$'
SHA256 = r'^[a-f0-9]{64}$'


class Anchor(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    artifact: str = Field(pattern=r'^controller_diff_[1-9][0-9]*\.patch$')
    patch_sha256: str = Field(pattern=SHA256)
    path: str = Field(max_length=4096)
    side: Literal['old', 'new']
    line: int = Field(gt=0)
    line_sha256: str = Field(pattern=SHA256)


class Request(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    schema_version: Literal[1]
    request_id: str = Field(pattern=UUID)
    task_id: str
    stopped_run_id: str
    task_definition_sha256: str = Field(pattern=SHA256)
    blocker: str
    action: Literal['request_changes', 'retry']
    text: str = Field(max_length=4000)
    created_at: str
    anchor: Anchor | None = None


def load(root, cfg, request_id, task_id, run_id):
    if not re.fullmatch(UUID, request_id) or not run_id:
        raise ValueError('Review feedback requires a request UUID and a new run ID')
    data = evidence._read(root / '.kh/review-requests' / f'{request_id}.json', limit=65536)
    request = Request.model_validate_json(data)
    if request.request_id != request_id or request.task_id != task_id or request.stopped_run_id == run_id:
        raise ValueError('Review request identity does not match this launch')
    if datetime.fromisoformat(request.created_at.replace('Z', '+00:00')).utcoffset() is None:
        raise ValueError('Review request creation time needs a timezone')
    if '\x00' in request.text or (request.action == 'request_changes' and not request.text.strip()):
        raise ValueError('Request changes needs a nonempty reply without NUL')
    if request.action == 'retry' and request.anchor:
        raise ValueError('Retry accepts task-level feedback only')
    project.check_review_start(root, request, run_id)
    if request.anchor:
        scope._relative_pattern(request.anchor.path)
        prior = root / Path(cfg.defaults.data_dir) / 'sessions' / request.stopped_run_id
        mapping = scope._read_json(prior / 'artifact-map.json')
        if not any(request.anchor.artifact in row.get('published_handoff_files', ())
                   for row in mapping.get('mappings', ())):
            raise ValueError('REVIEW_REQUEST_STALE: the selected patch was not published')
        patch = evidence._read(prior / 'context_handoff' / request.anchor.artifact, limit=2 * 1024 * 1024)
        if sha256(patch).hexdigest() != request.anchor.patch_sha256:
            raise ValueError('REVIEW_REQUEST_STALE: the published patch changed')
    return request, sha256(data).hexdigest()


def deliver(state, feedback, paths):
    request, digest = feedback
    if request.anchor:
        anchor = request.anchor
        current = permissions.snapshot(state.view)
        patch = delta.unified_diff(state.view.agent_baseline, current, paths)
        kept = state.kept or {}
        if (kept.get('run_id') != request.stopped_run_id or sha256(patch.encode()).hexdigest() != anchor.patch_sha256
                or delta.line_digest(state.view.agent_baseline, current, anchor) != anchor.line_sha256):
            raise ValueError('REVIEW_REQUEST_STALE: the selected line no longer matches the kept candidate')
        scope.check_paths(state.view.scope_contract, [anchor.path])
    record = evidence_packet.deliver_json(state.view, 'owner-review-request.json',
        {**request.model_dump(), 'request_sha256': digest}, producer='owner', kind='scoped_review_reply')
    if request.text.strip():
        state.view.owner_review_reply = record.relative_to(state.view.repo_root).as_posix()
