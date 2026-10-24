"""Exact capture grants with owner-declared, process-only DB test overrides.

Standard library only: validation and grammar inspection never run a program,
change an environment or confer permission to a parsed command.
"""
from __future__ import annotations

import re
import shlex

CHECK_CLASSES = frozenset({"test", "lint", "typecheck", "build"})
DB_ENV_NAMES = frozenset({"DATABASE_URL", "TEST_DATABASE_URL"})


def validate_overrides(mapping) -> dict[str, list[str]]:
    """Return detached, ordered literal value lists; never expand environment text."""
    if not isinstance(mapping, dict):
        raise ValueError("test_environment_overrides must be a mapping of DB names to explicit value lists")
    result = {}
    for name, values in mapping.items():
        if name not in DB_ENV_NAMES:
            raise ValueError(f"Unsupported test environment override {name!r}; use {sorted(DB_ENV_NAMES)}")
        if not isinstance(values, list) or not values:
            raise ValueError(f"{name}: provide a nonempty explicit list of literal values")
        unique = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name}: override values must be nonempty literal strings")
            if any(ord(char) < 32 or ord(char) == 127 or char in "$`\u0085\u2028\u2029" for char in value):
                raise ValueError(f"{name}: controls, newlines, $ and backticks are forbidden in literal override values")
            if value not in unique:
                unique.append(value)
        result[name] = unique
    return result


def capture_commands(quality_checks: dict, timeout: int, test_environment_overrides=None) -> list[str]:
    """Keep existing capture bytes and add two exact forms per declared DB value.

    Prefixes apply only to test-class captures. Quoting the assignment's VALUE
    separately preserves shell assignment syntax; env receives one literal
    NAME=VALUE argv entry. Neither form changes the caller's environment.
    """
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("Capture timeout must be a positive integer supplied by the command limit")
    if not isinstance(quality_checks, dict):
        raise ValueError("quality_checks must map check names to argv lists")
    overrides = validate_overrides({} if test_environment_overrides is None else test_environment_overrides)
    commands, tests = [], []
    for name, argv in quality_checks.items():
        if name not in CHECK_CLASSES or not argv:
            continue
        if (not isinstance(argv, list) or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
                or not argv[0].strip()):
            raise ValueError(f"{name}: quality check must have a nonempty program and literal string argv")
        base = shlex.join(["python3", "adws/kh_exec.py", "--class", name,
                           "--timeout", str(timeout), "--", *argv])
        if base not in commands:
            commands.append(base)
        if name == "test":
            tests.append(base)
    for base in tests:
        for name, values in overrides.items():
            for value in values:
                commands.append(f"{name}={shlex.quote(value)} {base}")
                commands.append(f"{shlex.join(['env', name + '=' + value])} {base}")
    return list(dict.fromkeys(commands))


def inner_argv(command) -> list[str] | None:
    """Recognize only this module's exact capture grammar; this is not a grant.

    Optional DB prefixes are shape-checked literal forms, not proof that the
    owner declared their values. Callers still require exact grant membership.
    """
    if not isinstance(command, str):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if any("\0" in word for word in words):
        return None
    prefix, captured = "", words
    assignment = None
    env_form = False
    if words and "=" in words[0]:
        assignment, captured = words[0], words[1:]
    elif len(words) >= 2 and words[0] == "env":
        assignment, captured, env_form = words[1], words[2:], True
    if assignment is not None:
        name, sep, value = assignment.partition("=")
        if not sep:
            return None
        try:
            validate_overrides({name: [value]})
        except ValueError:
            return None
        prefix = (shlex.join(["env", assignment]) if env_form else f"{name}={shlex.quote(value)}") + " "
    if (len(captured) < 8 or captured[:3] != ["python3", "adws/kh_exec.py", "--class"]
            or captured[3] not in CHECK_CLASSES or captured[4] != "--timeout"
            or not re.fullmatch(r"[1-9][0-9]*", captured[5]) or captured[6] != "--"
            or not captured[7].strip() or (prefix and captured[3] != "test")):
        return None
    if command != prefix + shlex.join(captured):
        return None
    return captured[7:]
