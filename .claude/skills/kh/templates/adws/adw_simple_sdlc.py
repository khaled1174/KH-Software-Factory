#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""Alias for guarded light SDLC: reviewed plan, build, checks and code review."""
import sys


def main(prompt=None, config='adws/adw_kh_config/kh.config.yaml', profile=None,
         scope_file=None, dry_run=False, save_profile=False, adw_id=None, task_id=None, use=None):
    from adw_start import main as guarded_start
    return guarded_start(prompt=prompt, config=config, profile=profile,
                         scope_file=scope_file, dry_run=dry_run,
                         save_profile=save_profile, adw_id=adw_id,
                         task_id=task_id, mode='light', use=use)


def cli(argv=None):
    from adw_start import guarded_parser
    options = vars(guarded_parser(__doc__).parse_args(argv))
    try:
        return main(**options)
    except (ValueError, RuntimeError, OSError) as error:
        print(f'kh-simple-sdlc: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(cli())
