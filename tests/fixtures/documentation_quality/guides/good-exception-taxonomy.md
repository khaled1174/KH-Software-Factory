# Current project guide

All four reports are implemented. See logreports/reports/status_counts.py:5, logreports/reports/latency.py:5, logreports/reports/failures.py:4 and logreports/reports/daily.py:4; corresponding tests are tests/test_status_counts.py:1, tests/test_latency.py:1, tests/test_failures.py:1 and tests/test_daily.py:1.

An invalid service raises a line-prefixed ValueError (logreports/core.py:14). That is a targeted validation case, not a promise that all errors are ValueError: missing timestamp can raise KeyError (logreports/core.py:20).

The loader does not add its own input-record prefix to JSONDecodeError (logreports/core.py:12). The JSON parser still provides lineno and colno; these are parser positions, not a uniform loader-record error prefix (probes/parser_position.py:8).

Current source imports use the standard library (logreports/core.py:2-4; logreports/reports/latency.py:2). This observation is not an import mandate; the fixture owner supplies no standard-library-only restriction (OWNER_RULES.md:3).

Build: make build (Makefile:4); it runs python3 -B -m compileall -q logreports.

Run: python3 -m logreports input.jsonl --report status_counts (README.md:5; logreports/__main__.py:8).

Tests: python3 -B -m unittest discover -s tests (README.md:6).
