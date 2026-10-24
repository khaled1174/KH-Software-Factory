#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""Prepare reviewed project documentation; --dry-run never dispatches or writes."""
import argparse
import json
import os
from pathlib import Path
from adw_modules import agents, routing, session, project_context


def main(config='adws/adw_kh_config/kh.config.yaml', profile=None,
         dry_run=False, refresh=False, adw_id=None):
    root = Path.cwd()
    inspection = project_context.inspect_context(root)
    cfg = agents.load_config(config)
    cfg, decision = routing.resolve(cfg, root, profile,
        available=(lambda agent: True) if dry_run else routing.installed,
        required_roles={'project_architect', 'plan_reviewer'})
    if dry_run:
        print(json.dumps({'inspection': inspection, 'routing': decision,
                          'models_dispatched': False, 'writes': False}, indent=2))
        return 0 if inspection['status'] in {'ready', 'missing'} or refresh else 1
    agents.validate(cfg, ['project_architect', 'plan_reviewer'])
    run = session.ensure(cfg, adw_id, fresh=True)
    try:
        receipt = project_context.bootstrap_with_budget(run, decision, refresh=refresh)
        print(json.dumps(receipt, indent=2))
        return run.finish()
    except Exception as exc:
        run.finish(accepted=False, reason=str(exc))
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('KH_CONFIG', 'adws/adw_kh_config/kh.config.yaml'))
    parser.add_argument('--profile')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--adw-id')
    args = parser.parse_args()
    raise SystemExit(main(**vars(args)))
