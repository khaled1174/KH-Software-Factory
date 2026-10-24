# Current project guide

All four reports are implemented. See logreports/reports/status_counts.py:5, logreports/reports/latency.py:5, logreports/reports/failures.py:4 and logreports/reports/daily.py:4; corresponding tests are tests/test_status_counts.py:1, tests/test_latency.py:1, tests/test_failures.py:1 and tests/test_daily.py:1.

Service, status, duration and missing-timezone validation branches raise ValueError (logreports/core.py:14,16,19,22). Missing timestamp raises KeyError at logreports/core.py:20; a non-object row can raise AttributeError at logreports/core.py:13. JSONDecodeError is a ValueError subclass (probes/parser_position.py:9).

The loader does not add its own input-record prefix to JSONDecodeError (logreports/core.py:12). The JSON parser still provides lineno and colno; these are parser positions, not a uniform loader-record error prefix (probes/parser_position.py:8).

Current source imports use the standard library (logreports/core.py:2-4; logreports/reports/latency.py:2). This observation is not an import mandate; the fixture owner supplies no standard-library-only restriction (OWNER_RULES.md:3).

Build: python3 -B -m compileall -q logreports, or make build (Makefile:5). Tests can also run through make test (Makefile:1).

Run: python3 -m logreports input.jsonl --report status_counts (README.md:5; logreports/__main__.py:8).

Tests: python3 -B -m unittest discover -s tests (README.md:6).
