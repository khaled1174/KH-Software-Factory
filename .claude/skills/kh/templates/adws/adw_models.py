#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""List configured routing profiles and installed CLIs without provider calls."""
import argparse
import json
import os
from pathlib import Path
from adw_modules import agents, routing, agent_delegate

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('KH_CONFIG', 'adws/adw_kh_config/kh.config.yaml'))
    parser.add_argument('--doctor', action='store_true', help='Read-only local prerequisites and relay protocol; no model calls')
    parser.add_argument('--choices', action='store_true',
                        help='Per-role options the controller offers the owner before a run (read-only)')
    parser.add_argument('--profile')
    args = parser.parse_args()
    if args.choices:
        from adw_modules import project_context
        cfg = agents.load_config(args.config)
        roles = set(routing.REVIEWED_ROLES)
        if project_context.inspect_context(Path.cwd())['status'] == 'missing':
            roles.add('project_architect')
        print(json.dumps(routing.choice_menu(cfg, Path.cwd(), args.profile, roles), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    cfg = agents.load_config(args.config)
    report = routing.inventory(cfg, Path.cwd())
    if args.doctor:
        from adw_modules import runtime_compatibility
        from adw_modules.data_types import AgentConfig
        base = {a.name: a for a in cfg.agents}
        report['runtime_compatibility'] = {
            name: {role: [runtime_compatibility.structural(AgentConfig.model_validate({
                **base[role].model_dump(), **{key: item[key] for key in
                    ('coding_agent', 'model', 'thinking', 'supported_thinking', 'tools') if key in item}}), cfg)
                for item in items] for role, items in roles.items() if role in base}
            for name, roles in report['profiles'].items()}
        report["delegate_diagnostics"] = agent_delegate.doctor()
    print(json.dumps(report, ensure_ascii=False, indent=2))
