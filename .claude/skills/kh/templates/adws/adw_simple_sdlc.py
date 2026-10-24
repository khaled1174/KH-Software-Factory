#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""Alias for guarded light SDLC: reviewed plan, build, checks and code review."""
import adw_modules
import argparse
import os
import sys


def main(prompt=None, config='adws/adw_kh_config/kh.config.yaml', profile=None,
         scope_file=None, dry_run=False, save_profile=False, adw_id=None, task_id=None, use=None):
    from adw_start import main as guarded_start
    return guarded_start(prompt=prompt, config=config, profile=profile,
                         scope_file=scope_file, dry_run=dry_run,
                         save_profile=save_profile, adw_id=adw_id,
                         task_id=task_id, mode='light', use=use)


def cli(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prompt', nargs='?')
    parser.add_argument('--task', dest='task_id')
    parser.add_argument('--config', default=os.environ.get('KH_CONFIG', 'adws/adw_kh_config/kh.config.yaml'))
    parser.add_argument('--profile')
    parser.add_argument('--scope-file')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--save-profile', action='store_true')
    parser.add_argument('--adw-id')
    parser.add_argument('--use', action='append', metavar='ROLE=RUNTIME:MODEL:EFFORT')
    options = vars(parser.parse_args(arguments))
    try:
        return main(**options)
    except (ValueError, RuntimeError, OSError) as error:
        print(f'kh-simple-sdlc: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(cli())
