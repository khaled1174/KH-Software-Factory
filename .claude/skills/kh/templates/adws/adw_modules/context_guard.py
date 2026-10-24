"""Controller-owned context boundary for sessions that accumulate.

Parse retries and gate corrections re-enter the SAME coding-agent session, so a
long repair loop is the one place in this runtime where window occupancy grows
without a bound. The controller already records occupancy after every send; this
module is the part that acts on it, before the next correction is dispatched.

Two outcomes, decided by role:

* rotate — end the session and open a fresh one for the same task. The brief
  that starts it is reconstructed from the repository and the controller's own
  records, never from the previous session's memory, so nothing the model
  believed has to be trusted across the boundary.
* refuse — a role whose verdict depends on having read the evidence itself
  cannot be handed a reconstruction. An independent reviewer that continues
  from a brief is reviewing the brief. Those roles fail the task instead, which
  is a scope signal: the task is too large to review in one window.

Runtime-agnostic on purpose: it reads the occupancy numbers every adapter
already reports, so claude_code, pi and delegate_codex are covered the same
way. In-session provider compaction, where a runtime offers it, is a separate
layer and is not visible here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .data_types import EventRecord


class ContextExhausted(RuntimeError):
    """A role that must not continue from a reconstruction ran out of window."""


@dataclass
class Rotation:
    """An approved session rotation and the evidence its brief is built from."""
    session_id: str
    occupancy_percent: float
    context_tokens: int
    context_window: int
    changed_paths: list[str]
    artifact: Path | None = None

    def brief(self, task_text: str, correction_text: str) -> str:
        """The opening prompt of the fresh session.

        Every line is something the controller can point at: the original task
        it dispatched, the paths it measured on disk, and the correction it is
        issuing now. No claim from the ended session is carried over.
        """
        if self.changed_paths:
            changed = "\n".join(f"- {path}" for path in self.changed_paths)
        else:
            changed = "- (none yet; nothing has been written for this task)"
        return (
            "CONTEXT ROTATION — this is a new session for the same task.\n"
            f"The previous session reached {self.occupancy_percent:.1f}% of its context window "
            f"({self.context_tokens:,} of {self.context_window:,} tokens) and was ended by the "
            "controller before it could be truncated mid-task.\n"
            "Everything below is reconstructed by the controller from the repository and its own "
            "records. Nothing is carried over from what the previous session believed, so read the "
            "files before you change them.\n\n"
            "ORIGINAL TASK\n"
            f"{task_text}\n\n"
            "FILES ALREADY CHANGED ON DISK (measured by the controller, not reported by an agent)\n"
            f"{changed}\n\n"
            "WHAT MUST BE FIXED NOW\n"
            f"{correction_text}\n\n"
            "Do not restart work that the files above already contain, and do not claim any change "
            "you have not made in this session."
        )


def occupancy(result) -> tuple[float | None, int, int]:
    """Percent of the window used by the last send, or None when unknowable.

    A window of 0 means the model registry declares no ceiling for this model;
    that is unknown, not empty, so the guard stays out of the way rather than
    inventing a denominator.
    """
    tokens = int(getattr(result, "context_tokens", 0) or 0)
    window = int(getattr(result, "context_window", 0) or 0)
    if window <= 0 or tokens <= 0:
        return None, tokens, window
    return (tokens / window) * 100.0, tokens, window


def _record(run, phase, agent, name, payload, level="log") -> None:
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type=level, name=name,
                                 payload={"agent": agent.name, **payload}))


def _write_artifact(run, phase, agent, rotation: Rotation) -> Path | None:
    """Persist the rotation as evidence next to the phase's other records."""
    try:
        parent = Path(run.context_handoff_dir) / "context_rotations"
        parent.mkdir(parents=True, exist_ok=True)
        seq = run.phases[-1].seq if getattr(run, "phases", None) else 0
        path = parent / f"{seq:02d}_{agent.name}_{rotation.session_id}.json"
        path.write_text(json.dumps({
            "agent": agent.name,
            "phase_id": phase.phase_id,
            "new_session_id": rotation.session_id,
            "context_tokens": rotation.context_tokens,
            "context_window": rotation.context_window,
            "occupancy_percent": round(rotation.occupancy_percent, 2),
            "changed_paths": rotation.changed_paths,
        }, indent=2, sort_keys=True), encoding="utf-8")
        return path
    except OSError:
        # Evidence is best effort; losing the file must not lose the rotation.
        return None


# A drop this steep inside one session is not ordinary turn accounting: it is
# the runtime having compacted or truncated the window on its own.
SHRINK_RATIO = 0.75


def note_runtime_compaction(run, phase, agent, before_tokens, same_session, result) -> bool:
    """Record a window that shrank inside a single session.

    Some runtimes compact themselves as they approach the ceiling. That spends
    provider requests the send budget never counts, and discards history the
    controller never chose to discard, so it is reported at the moment it is
    observable instead of being guessed at later from an agent that forgot
    something. The controller does not try to prevent it; it is not reachable
    from out here.
    """
    if not same_session or before_tokens <= 0:
        return False
    tokens = int(getattr(result, "context_tokens", 0) or 0)
    if tokens <= 0 or tokens >= before_tokens * SHRINK_RATIO:
        return False
    _record(run, phase, agent, "runtime_compaction_observed",
            {"context_tokens_before": before_tokens, "context_tokens_after": tokens,
             "context_window": int(getattr(result, "context_window", 0) or 0),
             "note": "the runtime compacted inside the session; those provider requests are not "
                     "counted by the send budget, and the controller did not choose what was "
                     "dropped"})
    return True


def before_correction(run, phase, agent, previous, new_session_id) -> Rotation | None:
    """Decide what happens before a correction re-enters the same session.

    Returns a Rotation when the caller must open `new_session_id` and start it
    with `Rotation.brief(...)`, None when the session may simply continue.
    Raises ContextExhausted for a role configured never to rotate.
    """
    guard = run.cfg.defaults.context_guard
    if not guard.enabled or previous is None:
        return None
    percent, tokens, window = occupancy(previous)
    if percent is None:
        # Said once per phase so an unknown ceiling is visible in the trace
        # rather than looking like a guard that passed.
        if not getattr(run, "_context_guard_unknown", False):
            run._context_guard_unknown = True
            _record(run, phase, agent, "context_guard_unknown",
                    {"context_tokens": tokens, "context_window": window,
                     "note": "no declared context window; occupancy cannot be judged"})
        return None
    if percent < guard.rotate_at_percent:
        return None

    done = getattr(run, "_context_guard_rotations", {}).get(phase.phase_id, 0)
    measured = {"context_tokens": tokens, "context_window": window,
                "occupancy_percent": round(percent, 2),
                "threshold_percent": guard.rotate_at_percent,
                "rotations_used": done}
    if done >= guard.max_rotations:
        # A fresh session carries the task prompt, so crossing again means the
        # task does not fit this model at all. Another rotation would only
        # spend a send to arrive in the same place.
        _record(run, phase, agent, "context_guard_exhausted", measured, level="error")
        raise ContextExhausted(
            f"CONTEXT_DOES_NOT_FIT: {agent.name} crossed {guard.rotate_at_percent}% of its "
            f"{window:,}-token window again after {done} rotation(s), so the task prompt itself "
            f"does not leave room to work. Split the task, shorten the inputs it carries, or give "
            f"this role a model with a larger window.")
    if agent.name in guard.never_rotate:
        _record(run, phase, agent, "context_guard_refused", measured, level="error")
        raise ContextExhausted(
            f"SCOPE_TOO_LARGE_FOR_REVIEW: {agent.name} reached {percent:.1f}% of its "
            f"{window:,}-token window ({tokens:,} tokens) with corrections still pending. "
            f"This role does not continue from a reconstructed brief, because a verdict reached "
            f"from a summary of the evidence is not an independent review. Split the task and "
            f"run it again.")

    rotation = Rotation(session_id=new_session_id, occupancy_percent=percent,
                        context_tokens=tokens, context_window=window,
                        changed_paths=sorted(getattr(run, "actual_changed_paths", None) or []))
    rotation.artifact = _write_artifact(run, phase, agent, rotation)
    counts = getattr(run, "_context_guard_rotations", None)
    if counts is None:
        counts = {}
        run._context_guard_rotations = counts
    counts[phase.phase_id] = done + 1
    _record(run, phase, agent, "context_guard_rotated",
            {**measured, "new_session_id": new_session_id,
             "changed_paths": rotation.changed_paths,
             "artifact": str(rotation.artifact) if rotation.artifact else None})
    return rotation
