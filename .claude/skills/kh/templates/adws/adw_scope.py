#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""Read the saved task scope; omitted run ID selects the latest saved scope."""
import argparse
import json
import os
from pathlib import Path
import re
from adw_modules import agents, scope


def inspect(config, run_id=None):
    cfg = agents.load_config(config)
    sessions = Path(cfg.defaults.data_dir) / 'sessions'
    if run_id is None:
        candidates = list(sessions.glob('*/scope.json'))
        if not candidates: raise ValueError('No saved task scope found')
        run_id = max(candidates, key=lambda p: p.stat().st_mtime_ns).parent.name
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', run_id):
        raise ValueError('Invalid run ID')
    result = {'run_id': run_id, 'scope': scope.load_contract(sessions / run_id / 'scope.json').model_dump()}
    effective = sessions / run_id / 'effective_scope.json'
    if effective.exists():
        # The boundary actually enforced on builder source writes (owner scope
        # narrowed by the accepted plan, or plan-supplied when the owner gave none).
        result['effective_scope'] = scope.load_contract(effective, result['scope']['original_request']).model_dump()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id', nargs='?')
    parser.add_argument('--config', default=os.environ.get('KH_CONFIG', 'adws/adw_kh_config/kh.config.yaml'))
    args = parser.parse_args()
    print(json.dumps(inspect(args.config, args.run_id), ensure_ascii=False, indent=2))
