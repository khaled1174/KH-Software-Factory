"""A time limit for each shell command a builder runs, stated by the controller.

The builder chooses the timeout of every shell command it starts. Across one
project those choices ranged from 10 s to 7,200 s, while the role itself ends
at 2,400 s. A test that never returned therefore sat for the rest of the role's
time (38 minutes) and was cut by the role limit, not by anything the builder
could observe: it never saw a failed command, so it never learned there was a
defect. The controller's own `test` gate stops the same command after 600 s.

The controller cannot cap a command from outside the agent, so the cap is
stated where the builder reads it, computed from the role's real limit and
never above what the controller itself allows a test (600 s).
"""
from __future__ import annotations

# Equals the controller's own `test` gate limit (quality.py), so a command the
# builder is allowed to run is never longer than the gate that will run it again.
COMMAND_CAP_SECONDS = 600
FLOOR_SECONDS = 30

INSTRUCTION = (
    "Controller command time limit: this attempt has about {role} seconds in total. Give EVERY shell "
    "command an explicit timeout of at most {limit} seconds (the tool's timeout argument) and wrap test "
    "runs as `timeout {limit} <command>`. A command that reaches that limit is a defect in what it runs, "
    "not bad luck: do not run it again unchanged. Find why it does not return (a wait that can never "
    "complete, a lock held while waiting for work that needs the same lock, an await without a timeout) "
    "and fix that, or report a blocker. Never leave one command running for the rest of your time.")


def command_limit(role_timeout_seconds: float) -> int:
    """The longest a single command may run: a quarter of the role, 30 s to 600 s."""
    try:
        quarter = int(float(role_timeout_seconds) / 4)
    except (TypeError, ValueError, OverflowError):
        quarter = COMMAND_CAP_SECONDS
    return max(FLOOR_SECONDS, min(COMMAND_CAP_SECONDS, quarter))


def instruction(role_timeout_seconds: float) -> str:
    return INSTRUCTION.format(role=int(role_timeout_seconds), limit=command_limit(role_timeout_seconds))
