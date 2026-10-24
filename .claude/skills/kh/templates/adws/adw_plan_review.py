#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""Plan and independently review a task without executing implementation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from adw_modules import agents, intent, planning, project_context, routing, scope, session, utils
from adw_modules.data_types import PhaseParams


def main(prompt, config='adws/adw_kh_config/kh.config.yaml', adw_id=None,
         profile=None, scope_file=None, dry_run=False):
    run = None
    try:
        root = Path.cwd()
        accepted_intent = intent.load(root, prompt)
        cfg, decision = routing.resolve(agents.load_config(config), root, requested=profile,
                                        required_roles=['planner', 'plan_reviewer'])
        contract = scope.load_contract(scope_file, prompt) if scope_file else scope.ScopeContract(
            original_request=prompt, deliverables=[prompt], required_checks=list(cfg.defaults.required_quality_checks))
        packet = intent.attach(project_context.build_context_packet(root, prompt, contract.allowed_paths),
                               accepted_intent)
        for role in ('planner', 'plan_reviewer'):
            agents.resolve(cfg, role).writes = []
        agents.validate(cfg, ['planner', 'plan_reviewer'])
        if dry_run:
            print(json.dumps({'dry_run': True, 'routing': decision,
                              'context_digest': packet['digest'], 'scope': contract.model_dump()}, indent=2))
            return 0
        run = session.ensure(cfg, adw_id)
        project_context.sync_commands(root, cfg, repo_lock=run._lock)
        packet = intent.attach(project_context.build_context_packet(root, prompt, contract.allowed_paths),
                               accepted_intent)
        scope.capture_contract(run, contract)
        scope._atomic_json(Path(run.session_dir) / 'routing.json', decision)
        with run.phase(PhaseParams(name='request', kind='engineer', owner=run.engineer,
                                   description='Capture reviewed-planning request')) as ph:
            ph.log(input=prompt, routing=decision, scope=contract.model_dump())
        plan, approval = planning.run_reviewed_plan(run, prompt, contract, packet,
                                                   max_repairs=decision['max_plan_repairs'])
        planning.assert_plan_approval(run, plan, approval, contract, packet)
        run.console.note(f'Reviewed plan only; implementation has not run. Receipt: {planning.approval_path(run)}')
        return run.finish()
    except Exception as error:
        if run is not None:
            return run.finish(accepted=False, reason=str(error))
        print(f'PLAN_BLOCKED: {error}', file=sys.stderr)
        return 1
    except BaseException:
        if run is not None:
            run._finish_trace(False)
            if run._lock:
                run._lock.close()
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prompt')
    parser.add_argument('--config', default='adws/adw_kh_config/kh.config.yaml')
    parser.add_argument('--adw-id')
    parser.add_argument('--profile')
    parser.add_argument('--scope-file')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), args.config, args.adw_id,
                  args.profile, args.scope_file, args.dry_run))
