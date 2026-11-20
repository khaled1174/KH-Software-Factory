#!/usr/bin/env python3
"""Serve a disposable KH review project with deterministic role responses."""
import argparse
from pathlib import Path
import sys
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_visualizer_review_actions import APP, BUN, REPLY, ReviewActionFixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=4600)
    args = parser.parse_args()
    if not BUN or not (APP / 'dist/index.html').is_file():
        parser.exit(2, 'Build the visualizer with Bun before starting the demo.\n')
    if not 0 <= args.port <= 65535:
        parser.error('port must be between 0 and 65535')

    fixture = ReviewActionFixture()
    fixture.setUp()
    try:
        fixture.prepare()
        fixture.failed_checks()
        with fixture.server(port=args.port) as url:
            print(f'Open {url}/#/review/T1/attempts', flush=True)
            print(f'Fixture reply: {REPLY}', flush=True)
            print('Press Ctrl-C to remove the temporary project.', flush=True)
            Event().wait()
    except KeyboardInterrupt:
        return 0
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    sys.exit(main())
