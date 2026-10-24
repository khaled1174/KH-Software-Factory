"""Small shared helpers. Anything bigger belongs in its own module."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel

_BEFORE_DOTENV = frozenset(os.environ)
_DOTENV_PATH = Path.cwd() / '.env'
_DOTENV_FILE = {k: v for k, v in dotenv_values(_DOTENV_PATH).items() if v is not None} if _DOTENV_PATH else {}
load_dotenv(_DOTENV_PATH)

# The names the project's .env supplies to this process. A name the operator
# exported in their own shell with a different value is theirs, not the file's,
# so it is never withheld on the file's behalf — that distinction is what makes
# a narrowed boundary safe to apply to PATH-adjacent environments at all.
# A name already present with the file's exact value counts as the file's: a
# launcher such as `just` (`set dotenv-load`) exports .env before Python starts,
# and must not be able to switch the boundary off by doing so.
DOTENV_KEYS = (frozenset(os.environ) - _BEFORE_DOTENV) | frozenset(
    name for name, value in _DOTENV_FILE.items() if os.environ.get(name) == value)

# None until a project config is loaded, and None means "hand over every
# .env-sourced name", the default passthrough behavior. `load_config` replaces it
# with the owner's declared allowlist. One controller runs one task, so the
# boundary is process-wide by design and every dispatch path inherits it
# without threading configuration through the adapters.
_dotenv_allowlist: frozenset[str] | None = None


def set_dotenv_allowlist(names) -> list[str]:
    """Apply the owner's declared .env boundary. Returns the withheld names."""
    global _dotenv_allowlist
    _dotenv_allowlist = None if names is None else frozenset(names)
    return withheld_dotenv_keys()


def withheld_dotenv_keys() -> list[str]:
    """.env-sourced names this run will not hand to any child process."""
    if _dotenv_allowlist is None:
        return []
    return sorted(DOTENV_KEYS - _dotenv_allowlist)


def operator_env() -> dict[str, str]:
    """The engineer's own environment, as their shell would hand it over.

    Narrowed by `defaults.env_allowlist` when the owner declares one: a
    credential that only exists because it is written in the project's .env is
    withheld from agents and quality commands unless it is named. Withheld
    names are reported at load time, because an unreachable credential must
    fail visibly rather than look like a provider outage.

    Agents and quality blocks are meant to see exactly what the operator sees:
    their PATH, their toolchains, their globally installed packages. Copying
    os.environ gets almost all the way there — but ADWs launch under `uv run`,
    which prepends its ephemeral venv's bin to PATH and sets VIRTUAL_ENV. That
    venv holds the ADW's OWN dependencies (pydantic, pyyaml), not the
    operator's, so anything a subprocess resolves through it — `python3`,
    `pip`, every globally pip-installed CLI — silently becomes the wrong one.

    Stripping the venv restores parity: `python3` in an agent's bash is the
    same `python3` the engineer gets in their terminal. The ADW's own imports
    are unaffected; this env is only ever handed to child processes.
    """
    env = os.environ.copy()
    for name in withheld_dotenv_keys():
        env.pop(name, None)
    venv = env.pop("VIRTUAL_ENV", "")
    if not venv:
        return env
    venv_bin = str(Path(venv) / "bin")
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p and p != venv_bin]
    env["PATH"] = os.pathsep.join(parts)
    return env


def new_id(length: int = 8) -> str:
    return secrets.token_hex(length // 2)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_prompt(arg: str) -> str:
    """CLI prompt arg: a file path resolves to its contents, else inline text."""
    try:
        p = Path(arg)
        if p.is_file():
            return p.read_text()
    except OSError:
        pass
    return arg


def engineer_name() -> str:
    name = os.environ.get("ENGINEER_NAME", "").strip()
    if name:
        return name
    try:
        out = subprocess.run(["git", "config", "user.name"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except OSError:
        pass
    return os.environ.get("USER", "engineer")


def digest(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode='json')
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()
