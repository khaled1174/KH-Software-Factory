"""Freeze workflow limits without changing models, scope or mandatory checks."""
from pydantic import BaseModel, ConfigDict
from typing import Literal


class ExecutionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal['full', 'light', 'bootstrap']
    max_plan_repairs: int
    max_repairs: int
    max_substantive_sends: int
    max_output_corrections: int
    max_model_sends: int
    deadline_seconds: int
    recovery_rounds: int = 0


def resolve_execution_policy(cfg, decision, mode='full'):
    if mode not in ('full', 'light'):
        raise ValueError('Unknown execution mode: ' + str(mode))
    plan = decision.get('max_plan_repairs', 2)
    code = decision.get('max_repairs', 2)
    if any(type(n) is not int or not 0 <= n <= 5 for n in (plan, code)):
        raise ValueError('Repair limits must be integers in 0..5')
    if mode == 'light':
        plan, code = min(plan, 1), min(code, 1)
    substantive = 2 * (plan + 1) + (code + 1) * 2
    settings = cfg.execution
    # Reserve a document proposal for each permitted build window when enabled.
    if settings.documentation.enabled:
        substantive += code + 1
    recovery = settings.recovery_rounds if mode == 'full' and code else 0
    # Each recovery reserves a reviewed plan amendment and another complete
    # build/check/review window. An explicit owner send ceiling still wins.
    substantive += recovery * (2 + (code + 1) * (2 + int(settings.documentation.enabled)))
    return ExecutionPolicy(mode=mode, max_plan_repairs=plan, max_repairs=code,
        max_substantive_sends=substantive,
        max_output_corrections=settings.max_output_corrections,
        max_model_sends=settings.max_model_sends or substantive + settings.max_output_corrections,
        deadline_seconds=settings.deadline_seconds or (5400 if mode == 'light' else 10800),
        recovery_rounds=recovery)


def resolve_bootstrap_policy(cfg, decision):
    repairs = decision.get('max_bootstrap_repairs', 2)
    if type(repairs) is not int or not 0 <= repairs <= 5:
        raise ValueError('Bootstrap repairs must be an integer in 0..5')
    substantive = 2 * (repairs + 1)
    return ExecutionPolicy(mode='bootstrap', max_plan_repairs=repairs, max_repairs=0,
        max_substantive_sends=substantive,
        max_output_corrections=cfg.execution.max_output_corrections,
        max_model_sends=cfg.execution.max_model_sends or substantive + cfg.execution.max_output_corrections,
        deadline_seconds=cfg.execution.deadline_seconds or 5400)


def validate_light_paths(cfg, policy, paths):
    if policy.mode != 'light':
        return
    from pathlib import PurePosixPath
    paths = list(dict.fromkeys(paths))
    reason = None
    if not paths or len(paths) > cfg.execution.light_max_files:
        reason = 'requires 1..%s concrete files' % cfg.execution.light_max_files
    for path in paths:
        if (not path or path.endswith('/') or path.startswith('/') or
                any(c in path for c in '*?[]\\') or '..' in PurePosixPath(path).parts or path == '.'):
            reason = 'allowed paths must be concrete relative files'
        from .permissions import covers
        if any(covers(path, pattern) for pattern in cfg.execution.light_risk_paths):
            reason = 'path matches owner light-mode risk rules: ' + path
    if reason:
        raise ValueError('REQUIRES_FULL_MODE: ' + reason + '; use /kh-start')
