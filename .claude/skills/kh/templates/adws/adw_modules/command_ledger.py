"""Controller-owned record of every shell command a coding agent runs (SYS-01).

Before this module, command evidence existed only when the builder chose to
route a command through a capture helper. A command run directly, a test whose
status was swallowed by `| tail`, or a pipeline captured only at its first
stage left nothing the reviewer could verify, and nothing recorded later could
recover it.

The ledger sits at the agent tool boundary instead: the controller already
reads every coding agent's event stream (Claude Code, pi, Gemini CLI, Codex),
so each shell tool call is recorded from that stream, whatever the agent
intended:

- a start record is written and flushed when the call is announced, before
  its result exists, so an interrupted call still leaves a durable record;
- the completion record carries the exact command text, the runtime-reported
  outcome, authentic timestamps, and the complete output the agent received,
  stored under a unique invocation ID with its SHA-256 (an empty stream is
  recorded as empty, with the empty digest);
- the command is analysed, not trusted: a status hidden by a pipeline without
  pipefail, by `;`/`||`, or by background execution is recorded as unknown; a
  display cut by tail/grep/redirection is recorded as filtered;
- each stage is classified by owner-configured patterns (test, format, lint,
  probe, exploratory compilation, ...). A label the agent gives a command
  plays no part.

Records are a hash chain (each line names the SHA-256 of the previous line), in
the controller session directory, which no worker workspace contains.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Optional

from .utils import now_iso

LEDGER_NAME = 'ledger.jsonl'
STREAMS = 'streams'
EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()
SHELL_TOOLS = {'bash', 'shell', 'run_shell_command', 'exec_command', 'local_shell', 'execute_command', 'run_command'}
FILTERS = {'tail', 'head', 'grep', 'egrep', 'fgrep', 'rg', 'sed', 'awk', 'gawk', 'cut', 'sort', 'uniq', 'wc',
           'less', 'more', 'xcpretty', 'xcbeautify', 'jq', 'tr', 'column', 'fold', 'nl', 'cat'}
WRAPPERS = {'env', 'command', 'time', 'nice', 'nohup', 'exec', 'xcrun', 'stdbuf', 'caffeinate', 'sudo'}
# Options of the wrappers above that take a separate value word.
WRAPPER_VALUES = {'nice': {'-n'}, 'xcrun': {'--sdk', '--toolchain', '-sdk', '-toolchain'}, 'env': {'-u', '-C', '-S'},
                  'sudo': {'-u', '-g', '-C'}, 'stdbuf': set()}
KEYWORDS = {'if', 'then', 'elif', 'else', 'while', 'until', 'do', '!', '{', '}', 'fi', 'done'}
SHELLS = {'sh', 'bash', 'zsh', 'dash', 'ksh'}
DENIED_MARKER = 'KH_INFRA_DENIED'
EXEC_MARKER = 'KH_EXEC_RECEIPT'

_EXIT_PATTERNS = (
    re.compile(r'^\s*(?:Error:\s*)?Exit code[:\s]+(-?\d+)', re.I),          # Claude Code Bash
    re.compile(r'Command exited with code[:\s]+(-?\d+)', re.I),              # pi bash
    re.compile(r'^\s*Exit Code:\s*(-?\d+)', re.I | re.M),                    # Gemini CLI
)
_REFUSED = re.compile(r'(?i)(permission to use \S+.*?(has been )?denied|denied by (the )?(user|permission|policy)|tool call denied by pre-tool hook)')
_TIMEOUT = re.compile(r'(?i)\b(command timed out|timed out after|was killed|terminated by signal)\b')


# ── command analysis ─────────────────────────────────────────────────────────

def _separate_lines(text: str) -> str:
    """Turn unquoted line breaks into `;` so each line is its own list item."""
    out, quote, escape = [], None, False
    for ch in text:
        if escape:
            escape = False
            out.append(' ' if ch == '\n' else ch)
            continue
        if ch == '\\' and quote != "'":
            escape = True
            out.append(ch)
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == '\n':
            previous = ''.join(out).rstrip()
            if previous.endswith(('|', '&&', '||', ';', '(', '{')) or not previous:
                out.append(' ')
                continue
            out.append(' ; ')
            continue
        out.append(ch)
    if escape and out and out[-1] == '\\':
        out.pop()
    return ''.join(out).replace('\\ ', ' ')


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.S)


def _strip_heredocs(text: str) -> str:
    """A heredoc body is data, not commands: `cat > run.sh <<EOF ... EOF` runs only `cat`."""
    return _HEREDOC.sub(lambda m: '< /dev/null', text)


def _pipefail(text: str) -> bool:
    """`set -o pipefail` (or -eo/-euo pipefail) in effect at the end of the text."""
    state = False
    for match in re.finditer(r'\bset\s+([-+])[A-Za-z]*o\s+pipefail\b', text):
        state = match.group(1) == '-'
    return state


def _tokens(text: str):
    lexer = shlex.shlex(text, posix=True, punctuation_chars=';&|<>()')
    lexer.whitespace_split = True
    lexer.commenters = ''
    return list(lexer)


def _strip(words: list[str], *, executable_heads=None) -> list[str]:
    """Drop variable assignments and transparent wrappers in front of a command."""
    words = list(words)
    while words:
        head = words[0]
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', head):
            words.pop(0)
            continue
        if head in KEYWORDS:
            words.pop(0)
            continue
        base = PurePosixPath(head).name
        if executable_heads is not None:
            executable_heads.append(base)
        if base in WRAPPERS:
            words.pop(0)
            while words and words[0].startswith('-'):
                option = words.pop(0)
                if option in WRAPPER_VALUES.get(base, ()) and words:
                    words.pop(0)
            continue
        if base == 'xargs':
            words.pop(0)
            while words and words[0].startswith('-'):
                option = words.pop(0)
                if option in ('-n', '-I', '-P', '-L', '-d', '-s', '-E', '-J', '-R', '-S') and words:
                    words.pop(0)
            continue
        if base == 'timeout' or base == 'gtimeout':
            words.pop(0)
            while words and (words[0].startswith('-') or re.fullmatch(r'\d+[smhd]?', words[0])):
                words.pop(0)
            continue
        break
    if words:
        words[0] = PurePosixPath(words[0]).name    # /usr/local/bin/swift -> swift
    return words


def analyze(text: str, depth: int = 0, *, include_executables: bool = False) -> dict:
    """Structure of one shell command: list items, pipeline stages, operators.

    Returns {'stages': [...], 'parse': 'ok'|'uncertain', 'pipefail': bool}.
    Each stage: {'words', 'text', 'item', 'position', 'last_in_pipeline',
    'after': operators following its pipeline, 'filtered_by', 'redirected'}.
    """
    text = _strip_heredocs(str(text or ''))
    pipefail = _pipefail(text)
    try:
        tokens = _tokens(_separate_lines(text))
        parse = 'uncertain' if '<<' in text else 'ok'
    except ValueError:
        tokens = re.split(r'\s+', text.strip())
        parse = 'uncertain'
    items, current, pipeline, words = [], [], [], []
    redirect_next = False
    redirected = False
    operators = []

    def end_stage():
        nonlocal words, redirected
        if words:
            pipeline.append({'words': words, 'redirected': redirected})
        words, redirected = [], False

    def end_item(op):
        nonlocal pipeline
        end_stage()
        if pipeline:
            items.append({'stages': pipeline, 'op': op})
        pipeline = []

    for token in tokens:
        if redirect_next:
            redirect_next = False
            continue
        if token in ('|', '|&'):
            end_stage()
            continue
        if token in (';', '&&', '||', '&', '\n', ';;'):
            end_item(token)
            continue
        if set(token) <= set('<>&') and token:
            # A redirection: the next token is its target (fd duplications such
            # as 2>&1 keep the output in the same stream and are not a loss).
            if token in ('>', '>>', '&>', '&>>', '>|') :
                redirected = True
                redirect_next = True
                if words and re.fullmatch(r'\d', words[-1]):
                    words.pop()
            elif token in ('>&', '<&'):
                redirect_next = True
                if words and re.fullmatch(r'\d', words[-1]):
                    words.pop()
            elif token in ('<', '<<', '<<<'):
                redirect_next = True
            continue
        if token in ('(', ')'):
            continue
        words.append(token)
    end_item(None)

    stages = []
    executable_heads = [] if include_executables else None
    for index, item in enumerate(items):
        # Operators that follow this item; a trailing `;` changes nothing.
        after = [items[j]['op'] for j in range(index, len(items))
                 if items[j]['op'] and not (j == len(items) - 1 and items[j]['op'] in (';', ';;'))]
        for position, stage in enumerate(item['stages']):
            negated = '!' in stage['words'][:3]
            stripped = _strip(stage['words'], executable_heads=executable_heads)
            routed, program = route_split(stripped)
            if routed:
                stripped = _strip(program, executable_heads=executable_heads) or ['kh_exec.py']
            if not stripped:
                continue
            # bash -lc '...' (Codex wraps every command this way): analyse the inner script.
            if (PurePosixPath(stripped[0]).name in SHELLS and depth < 3 and any(w.startswith('-') and 'c' in w
                    for w in stripped[1:-1])):
                inner = analyze(stripped[-1], depth + 1, include_executables=include_executables)
                if executable_heads is not None:
                    executable_heads.extend(inner['executables'])
                pipefail = pipefail or inner['pipefail']
                if inner['parse'] != 'ok':
                    parse = inner['parse']
                stages.extend(inner['stages'])
                continue
            later = [PurePosixPath(_strip(s['words'])[0]).name for s in item['stages'][position + 1:]
                     if _strip(s['words'])]
            stages.append({'words': stripped, 'text': ' '.join(stripped), 'item': index, 'position': position,
                           'last_in_pipeline': position == len(item['stages']) - 1,
                           'after': after, 'filtered_by': [w for w in later if w in FILTERS],
                           'piped_into': later, 'redirected': stage['redirected'], 'negated': negated,
                           'routed': routed})
    result = {'stages': stages, 'parse': parse, 'pipefail': pipefail}
    if executable_heads is not None:
        result['executables'] = list(dict.fromkeys(executable_heads))
    return result


def classify(stage_text: str, classes) -> tuple[str, bool]:
    """First owner class matching the stage, or the command a wrapper runs after `--`."""
    candidates = [stage_text]
    if ' -- ' in stage_text:
        candidates.append(stage_text.split(' -- ', 1)[1])
    for item in classes:
        for text in candidates:
            if any(re.search(pattern, text) for pattern in item.patterns):
                return item.name, item.strict
    return 'other', False


def judge(command: str, classes) -> dict:
    """Class of the command and whether its recorded status/output can stand for it."""
    shape = analyze(command)
    best = None
    components = []
    for stage in shape['stages']:
        name, strict = classify(stage['text'], classes)
        if strict:
            components.append({'class': name, 'text': stage['text'], 'routed': bool(stage.get('routed'))})
        if best is None or (strict and not best['strict']) or (best['class'] == 'other' and name != 'other'):
            best = {'class': name, 'strict': strict, 'stage': stage}
    if best is None:
        return {'class': 'other', 'strict': False, 'stage_text': '', 'status_authentic': False,
                'status_note': 'empty command', 'output_complete': False, 'parse': shape['parse']}
    stage = best['stage']
    note = ''
    authentic = shape['parse'] == 'ok'
    if not authentic:
        note = 'command could not be parsed reliably'
    if not stage['last_in_pipeline'] and not shape['pipefail']:
        authentic, note = False, ('pipeline without pipefail: the recorded status is that of `'
                                  + ' | '.join(stage['piped_into']) + '`, not of this command')
    if stage.get('negated'):
        authentic, note = False, '`!` inverts the exit status of the command'
    for op in stage['after']:
        if op in (';', '||', '&', '\n'):
            authentic, note = False, f'`{op}` after the command replaces or suppresses its exit status'
            break
    if len(components) > 1:
        authentic, note = False, (f'{len(components)} strict commands in one call: one exit status cannot stand '
                                  'for each; run each on its own or through adws/kh_exec.py')
    ambiguous = authentic and '&&' in stage['after']
    output_complete = not stage['filtered_by'] and not stage['redirected']
    output_note = ''
    if stage['filtered_by']:
        output_note = 'output passed through ' + ', '.join(stage['filtered_by']) + ' before it was recorded'
    elif stage['redirected']:
        output_note = 'output redirected away from the recorded stream'
    return {'class': best['class'], 'strict': best['strict'], 'stage_text': stage['text'],
            'status_authentic': authentic, 'status_note': note,
            'status_ambiguous_if_nonzero': ambiguous,
            'output_complete': output_complete, 'output_note': output_note, 'parse': shape['parse'],
            'strict_components': components}


ROUTE_OPTIONS = {'--class', '--timeout', '--out', '--display-bytes'}


def _route_path(word: str) -> bool:
    text = str(word)
    return text in ('adws/kh_exec.py', './adws/kh_exec.py') or text.endswith('/adws/kh_exec.py')


def route_split(words):
    """(True, program words) when the stage runs the project's adws/kh_exec.py route, else (False, words)."""
    words = list(words)
    index = None
    if words and _route_path(words[0]):
        index = 0
    elif words and PurePosixPath(words[0]).name.startswith('python'):
        position = 1
        while position < len(words) and words[position].startswith('-'):
            position += 2 if words[position] in ('-X', '-W') else 1
        if position < len(words) and _route_path(words[position]):
            index = position
    if index is None:
        return False, words
    rest = words[index + 1:]
    while rest and rest[0].startswith('--') and rest[0] != '--':
        option = rest.pop(0)
        if option in ROUTE_OPTIONS and rest:
            rest.pop(0)
    if rest[:1] == ['--']:
        rest = rest[1:]
    return True, rest


def is_routed(words) -> bool:
    """A stage that runs the controller execution route (adws/kh_exec.py)."""
    return route_split(words)[0]


_RUNTIME_EXIT = {'claude_code': _EXIT_PATTERNS[0], 'pi': _EXIT_PATTERNS[1], 'gemini_cli': _EXIT_PATTERNS[2]}


def runtime_exit(result_text: str, ok: Optional[bool], exit_code=None, runtime: str = '') -> tuple[Optional[int], str]:
    """(exit code, basis) as the runtime reported it.

    Each runtime states the code in its own place: Claude Code at the start of
    the result, pi and Gemini CLI in a trailer after the output. Only that place
    counts, and for a trailer only its last occurrence, so command output that
    happens to print `Exit code 0` cannot stand in for the real status.
    """
    if isinstance(exit_code, int):
        return exit_code, 'runtime exit_code field'
    text = result_text or ''
    if DENIED_MARKER in text:
        return 126, 'refused by the controller infrastructure boundary'
    patterns = [_RUNTIME_EXIT[runtime]] if runtime in _RUNTIME_EXIT else list(_EXIT_PATTERNS)
    for pattern in patterns:
        found = list(pattern.finditer(text))
        if found:
            return int(found[-1].group(1)), 'exit code stated in the runtime tool result'
    if _TIMEOUT.search(text):
        return None, 'runtime reported a timeout or kill; exit status unknown'
    if ok is True:
        return 0, 'runtime reported the tool call as successful (nonzero exits are reported as errors)'
    if ok is False:
        return None, 'runtime reported an error without an exit code'
    return None, 'no outcome reported'


# ── the ledger ───────────────────────────────────────────────────────────────

class CommandLedger:
    """One phase's command records. Thread-safe; every write is flushed and fsynced."""

    def __init__(self, directory: Path, *, phase_id: str, agent: str, runtime: str, cwd: str,
                 classes=(), forbidden=(), on_forbidden=None, inspect=None, exec_dir=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / STREAMS).mkdir(exist_ok=True)
        self.path = self.directory / LEDGER_NAME
        self.path.touch(exist_ok=True)
        self.phase_id, self.agent, self.runtime, self.cwd = phase_id, agent, runtime, cwd
        self.classes = list(classes)
        self.forbidden = [re.compile(p) for p in forbidden]
        self.on_forbidden = on_forbidden
        self.inspect = inspect
        self.exec_dir = Path(exec_dir) if exec_dir else None
        self._claimed: set[str] = set()
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._open: dict[str, dict] = {}
        self._seq = 0
        self._previous = self._last_digest()
        self.records: dict[str, dict] = self._load()

    # persistence
    def _last_digest(self) -> str:
        if not self.path.exists():
            return ''
        lines = self.path.read_bytes().splitlines()
        return hashlib.sha256(lines[-1]).hexdigest() if lines else ''

    def _load(self) -> dict:
        records = {}
        if self.path.exists():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                event = json.loads(line)
                inv = event['invocation_id']
                records[inv] = {**records.get(inv, {}), **event}
                self._seq = max(self._seq, int(inv.rsplit('#', 1)[1]))
        return records

    def _append(self, event: dict) -> None:
        event = {**event, 'prev_sha256': self._previous}
        line = json.dumps(event, ensure_ascii=True, sort_keys=True).encode('ascii')
        with self.path.open('ab') as handle:
            handle.write(line + b'\n')
            handle.flush()
            os.fsync(handle.fileno())
        self._previous = hashlib.sha256(line).hexdigest()
        inv = event['invocation_id']
        self.records[inv] = {**self.records.get(inv, {}), **event}

    # observation
    def opened(self, call_id: str, tool: str, args, timing_basis: str = 'live') -> None:
        try:
            with self._lock:
                self._start(call_id, tool, args, timing_basis)
        except Exception as error:
            self.errors.append(f'{type(error).__name__}: {error}'[:500])

    def _start(self, call_id, tool, args, timing_basis='live'):
        """Write the start record (lock held). Returns the open record or None."""
        command = shell_command(tool, args)
        if command is None:
            return None
        key = str(call_id or f'anon-{self._seq + 1}')
        if key in self._open:
            return self._open[key]
        self._seq += 1
        inv = f'{self.phase_id}#{self._seq}'
        verdict = judge(command, self.classes)
        forbidden = [p.pattern for p in self.forbidden if p.search(command)]
        if self.inspect is not None:
            forbidden += list(self.inspect(command))
        record = {'event': 'start', 'invocation_id': inv, 'tool_call_id': key, 'phase_id': self.phase_id,
                  'agent': self.agent, 'runtime': self.runtime, 'tool': tool, 'command': command,
                  'cwd': self.cwd, 'cwd_basis': 'agent workspace root at session start (a persistent '
                  'shell may have changed directory)', 'started_at': now_iso(),
                  'started_monotonic': time.monotonic(), **{f'class_{k}': v for k, v in verdict.items()},
                  'forbidden': forbidden,
                  'timing_basis': ('observed live from the runtime event stream' if timing_basis == 'live' else
                                   'relayed after the run: times are when the controller read the event, '
                                   'not when the command ran')}
        self._open[key] = record
        self._append(record)
        return record

    def closed(self, call_id: str, tool: str, args, result_text: str, ok: Optional[bool], exit_code=None) -> None:
        # Called from the runtime's stream reader: a recording problem must never
        # kill the agent. It is kept and reported as unavailable evidence instead.
        try:
            self._closed(call_id, tool, args, result_text, ok, exit_code)
        except Exception as error:
            self.errors.append(f'{type(error).__name__}: {error}'[:500])

    def _closed(self, call_id, tool, args, result_text, ok, exit_code=None) -> None:
        with self._lock:
            key = str(call_id or '')
            if key not in self._open:
                started = self._start(key, tool, args)
                if started is None:
                    return
                key = started['tool_call_id']
            start = self._open.pop(key)
            inv = start['invocation_id']
            data = (result_text or '').encode('utf-8', errors='backslashreplace')
            stream = self.directory / STREAMS / (inv.replace('#', '_') + '.out')
            with stream.open('xb') as handle:        # two invocations can never share a capture name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            code, basis = runtime_exit(result_text, ok, exit_code, self.runtime)
            # Refused only when nothing else in the call could have run: one stage,
            # no list operators. `docker x || /usr/local/bin/docker x` is not refused.
            single = len(analyze(start['command'])['stages']) <= 1
            refused = ((DENIED_MARKER in (result_text or '') and single)
                       or (ok is False and bool(_REFUSED.search(result_text or ''))))
            authentic = start.get('class_status_authentic', False)
            background = _background(args)
            if background:
                authentic = False
            routed = (self._kh_exec(start, result_text or '', code)
                      if any(c.get('routed') for c in start.get('class_strict_components') or []) else None)
            if code is not None and code != 0 and start.get('class_status_ambiguous_if_nonzero'):
                authentic = False
            status = ('refused' if refused else 'unknown' if code is None
                      else f'exit:{code}' if authentic else 'unknown')
            record = {'event': 'end', 'invocation_id': inv, 'ended_at': now_iso(),
                      'duration_ms': int((time.monotonic() - start['started_monotonic']) * 1000),
                      'runtime_ok': ok, 'runtime_exit_code': code, 'runtime_exit_basis': basis,
                      'status': status,
                      'status_note': ('started in the background: the result reports the launch, not the '
                                      'command\'s outcome' if background else
                                      '' if authentic or code is None else
                                      start.get('class_status_note') or 'nonzero exit after `&&` is ambiguous'),
                      'stream': {'path': f'{STREAMS}/{stream.name}', 'bytes': len(data),
                                 'sha256': hashlib.sha256(data).hexdigest(), 'empty': not data},
                      'evidence': 'complete' if start.get('class_output_complete') and not background else 'excerpt'}
            if routed is not None:
                # The controller execution route: status and complete streams come from
                # its verified receipt, not from the (possibly shortened) display.
                record['kh_exec'] = routed
                components = start.get('class_strict_components') or []
                verified = [r for r in routed if r.get('verified')
                            and classify(r.get('stage_text', ''), self.classes)[1]]
                # Complete only when every strict component ran through the route and
                # each has its own verified receipt.
                strict_receipts = (verified if components and all(c['routed'] for c in components)
                                   and len(verified) >= len(components) else [])
                if strict_receipts:
                    failed = [r for r in strict_receipts if (r['outcome'] or {}).get('kind') != 'exit'
                              or (r['outcome'] or {}).get('exit_code') != 0]
                    outcome = (failed or strict_receipts)[-1]['outcome']
                    kind = outcome.get('kind')
                    record['status'] = (f"exit:{outcome.get('exit_code')}" if kind == 'exit' else
                                        f"signal:{outcome.get('signal')}" if kind == 'signal' else
                                        'timeout' if kind == 'timeout' else 'interrupted' if kind == 'interrupted'
                                        else 'unknown')
                    record['status_note'] = 'from the verified kh_exec receipt ' + strict_receipts[-1]['id']
                    record['evidence'] = 'complete'
                elif any(not r.get('verified') for r in routed):
                    record['status_note'] = 'kh_exec receipt present but not verified: ' + '; '.join(
                        r.get('problem', '') for r in routed if not r.get('verified'))
            self._append(record)
        if start.get('forbidden') and self.on_forbidden:
            self.on_forbidden(self.records[inv])

    def _kh_exec(self, start: dict, text: str, runtime_code=None) -> list:
        """Receipts of the execution route, verified and copied into the controller session now."""
        receipts = []
        for line in text.splitlines():
            if line.startswith(EXEC_MARKER + ' '):
                try:
                    receipts.append(json.loads(line[len(EXEC_MARKER) + 1:]))
                except ValueError:
                    continue
        if not receipts and self.exec_dir is not None and self.exec_dir.is_dir():
            # The display (and the receipt line) may have been filtered away: find
            # receipts written during this call for the same argv.
            begun = _iso(start.get('started_at'))
            for path in sorted(self.exec_dir.glob('*/receipt.json')):
                try:
                    data = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    continue
                at = _iso(data.get('started_at'))
                # Started during this call (same clock), not claimed by an earlier record.
                if begun is not None and at is not None and at >= begun - 0.001 and \
                        data.get('id') not in self._claimed and \
                        all(str(word) in start['command'] for word in data.get('argv') or []):
                    receipts.append({k: data.get(k) for k in ('id', 'argv', 'outcome', 'stdout', 'stderr',
                                                               'started_at', 'ended_at', 'directory')})
        seen, result = set(), []
        for receipt in receipts:
            ident = str(receipt.get('id') or '')
            if not ident or ident in seen:
                continue
            seen.add(ident)
            self._claimed.add(ident)
            entry = {**receipt, 'stage_text': ' '.join(_strip(list(receipt.get('argv') or []))), 'verified': False}
            directory = Path(str(receipt.get('directory') or ''))
            try:
                target = self.directory / 'exec' / ident
                problems = []
                # Bind the receipt to THIS call: written in this phase's evidence
                # directory, during the call, for words of this command, and agreeing
                # with the runtime's own exit status when the route ran alone.
                if self.exec_dir is not None and not directory.resolve().is_relative_to(self.exec_dir.resolve()):
                    problems.append('receipt directory is outside this phase\'s exec directory')
                begun, ended = _iso(start.get('started_at')), _iso(receipt.get('ended_at'))
                at = _iso(receipt.get('started_at'))
                if begun is None or at is None or at < begun - 0.001 or (ended is not None and ended > time.time() + 1):
                    problems.append('receipt times are outside this call')
                if not all(str(word) in start['command'] for word in receipt.get('argv') or []):
                    problems.append('receipt argv is not part of this command')
                outcome = receipt.get('outcome') or {}
                single = len(start.get('class_strict_components') or []) == 1 and start.get('class_status_authentic')
                if single and runtime_code is not None and outcome.get('exit_code') is not None \
                        and outcome.get('exit_code') != runtime_code:
                    problems.append(f"receipt exit {outcome.get('exit_code')} differs from the runtime's {runtime_code}")
                if outcome.get('streams_complete') is False:
                    problems.append('streams incomplete: ' + str(outcome.get('note', '')))
                for name in ('stdout', 'stderr'):
                    data = (directory / name).read_bytes()
                    if hashlib.sha256(data).hexdigest() != (receipt.get(name) or {}).get('sha256'):
                        problems.append(f'{name} differs from its receipt')
                if problems:
                    entry['problem'] = ', '.join(problems)
                else:
                    target.mkdir(parents=True, exist_ok=False)
                    for name in ('start.json', 'receipt.json', 'stdout', 'stderr'):
                        if (directory / name).is_file() and not (directory / name).is_symlink():
                            (target / name).write_bytes((directory / name).read_bytes())
                    entry['verified'] = True
                    entry['copied_to'] = f'exec/{ident}'
            except OSError as error:
                entry['problem'] = f'records unreadable: {error}'
            result.append(entry)
        return result

    def codex_event(self, event: dict) -> None:
        """Codex `exec --json` items: command_execution carries exit_code itself."""
        item = event.get('item') if isinstance(event, dict) else None
        if not isinstance(item, dict) or item.get('type') != 'command_execution':
            return
        args = {'command': item.get('command', '')}
        if event.get('type') == 'item.started':
            self.opened(item.get('id'), 'exec_command', args, timing_basis='relayed')
        elif event.get('type') == 'item.completed':
            if not item.get('id'):
                item = {**item, 'id': f"codex-{hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:12]}"}
            try:
                with self._lock:
                    if str(item.get('id')) not in self._open:
                        self._start(item.get('id'), 'exec_command', args, 'relayed')
            except Exception as error:
                self.errors.append(f'{type(error).__name__}: {error}'[:500])
                return
            self.closed(item.get('id'), 'exec_command', args, str(item.get('aggregated_output') or ''),
                        item.get('status') == 'completed', item.get('exit_code'))

    def finalize(self, reason: str = 'phase ended before the tool call returned') -> None:
        """Every call still open becomes a durable interrupted/unknown record."""
        with self._lock:
            for key, start in list(self._open.items()):
                self._append({'event': 'end', 'invocation_id': start['invocation_id'], 'ended_at': now_iso(),
                              'duration_ms': int((time.monotonic() - start['started_monotonic']) * 1000),
                              'runtime_ok': None, 'runtime_exit_code': None,
                              'runtime_exit_basis': reason, 'status': 'interrupted',
                              'status_note': 'no result was received; the exit status is unknown',
                              'stream': None, 'evidence': 'none'})
                self._open.pop(key, None)

    # judgement
    def invocations(self) -> list[dict]:
        return sorted(self.records.values(), key=lambda r: int(r['invocation_id'].rsplit('#', 1)[1]))


def _iso(value):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def _background(args) -> bool:
    return isinstance(args, dict) and bool(args.get('run_in_background') or args.get('is_background'))


def shell_command(tool: str, args) -> Optional[str]:
    """The shell text of a tool call, or None for a non-shell tool."""
    if str(tool or '').lower() not in SHELL_TOOLS:
        return None
    if isinstance(args, dict):
        value = args.get('command') or args.get('cmd') or args.get('script') or args.get('CommandLine')
        if isinstance(value, list):
            return shlex.join(str(v) for v in value)
        return str(value or '')
    return str(args or '')


def complete(record: dict) -> bool:
    """A strict record that stands on its own: authentic status and full output."""
    return (record.get('status', '').startswith('exit:') and record.get('evidence') == 'complete')


def coverage(records: list[dict], decisions=None) -> list[dict]:
    """Every strict invocation and how its evidence requirement is met.

    complete: its own record is complete. superseded: a later invocation of the
    same command in the same phase has a complete record (the original stays
    recorded as unknown; nothing pretends it was recovered). disposition: an
    owner decision record names this exact invocation. gap: none of these.
    Non-strict invocations are listed as metadata-only; a filtered display of
    them is an excerpt, never acceptance evidence.
    """
    from . import dispositions as owner_dispositions
    rows = []
    ordered = sorted(records, key=lambda r: int(r['invocation_id'].rsplit('#', 1)[1]))
    for index, record in enumerate(ordered):
        row = {'invocation_id': record['invocation_id'], 'class': record.get('class_class', 'other'),
               'strict': bool(record.get('class_strict')), 'command': record.get('command', ''),
               'status': record.get('status', 'open'), 'evidence': record.get('evidence', 'none'),
               'establishes': ['command'] + (['timing'] if str(record.get('timing_basis', '')).startswith('observed live')
                                             else [])}
        if row['status'].startswith('exit:'):
            row['establishes'].append('exit_status')
        if row['evidence'] == 'complete':
            row['establishes'].append('complete_output')
        if not row['strict']:
            row['requirement'] = 'metadata'
            row['resolution'] = 'recorded'
        elif complete(record):
            row['requirement'], row['resolution'] = 'strict', 'complete'
        else:
            row['requirement'] = 'strict'
            later = next((r for r in ordered[index + 1:]
                          if r.get('class_stage_text') == record.get('class_stage_text') and complete(r)), None)
            decision = owner_dispositions.match(decisions, record) if decisions else None
            if decision:
                row['resolution'], row['decision'] = 'disposition', decision['id']
            elif later:
                row['resolution'], row['superseded_by'] = 'superseded', later['invocation_id']
                row['note'] = 'original status/output stays unknown; the later complete record is a new execution'
            else:
                row['resolution'] = 'gap'
                row['why'] = (record.get('status_note') or ('output: ' + (record.get('class_output_note') or ''))
                              if record.get('status') != 'interrupted' else 'interrupted')
        rows.append(row)
    return rows


def gaps(records: list[dict], decisions=None) -> list[dict]:
    return [row for row in coverage(records, decisions) if row['resolution'] == 'gap']


def gate(envelope, run):
    """Builder gate: every strict command has complete evidence before the phase is accepted."""
    from .data_types import GateReport
    report = GateReport()
    ledger = getattr(run, 'command_ledger', None)
    settings = getattr(getattr(getattr(run, 'cfg', None), 'execution', None), 'command_policy', None)
    if ledger is None or settings is None or not settings.enabled or not settings.require_complete_strict_records:
        return report.check('command records', True, 'no command ledger for this phase')
    records = ledger.invocations()
    decisions = getattr(run, 'owner_decisions', None)
    missing = gaps(records, decisions)
    report.check('command records', not missing,
                 f'{len(records)} command(s) recorded by the controller' if not missing else
                 'strict command evidence is incomplete: ' + '; '.join(
                     f"{g['invocation_id']} `{g['command'][:300]}` ({g['class']}): {g.get('why') or 'incomplete'}"
                     for g in missing[:20]) +
                 '. The original status/output cannot be recovered and stays recorded as unknown. Re-run each '
                 'named command so that its exit status and full output reach the tool result unfiltered (no '
                 '`| tail`/`| grep`, no `; echo $?`, no `|| true`; use `set -o pipefail` when a pipeline is '
                 'needed), then re-emit your report. Do not relabel a command: the controller classifies it.')
    return report
