"""Stable identities for review findings across rounds and runs (SYS-08).

Before: each blocking item of a rejected review became a new requirement whose
identity was its prose, clipped to 400 characters. A reworded B3 was a second
requirement, a long correction lost its tail, and review ratios from different
rounds had different denominators.

Now:

- A finding's identity is its ID. A reviewer starts a blocking item with an ID
  (`B3: ...`, `B3-D2 — ...`); reusing an earlier ID revises that finding, a new
  ID is a new finding. An item without an ID gets `R<round>.<n>`, and an item
  identical in text to an existing finding is that finding.
- The full text of every revision is kept in the registry (controller session
  and a read-only copy delivered to workers). The requirement line carries the
  ID; coverage is matched by ID, so a reviewer may restate it.
- Each round updates every finding: open, resolved, or regressed (resolved
  earlier, unmet or blocking again). Closed findings stay requirements, so a
  regression is caught.
- Progress is reported on two stable bases: the original requirements (fixed
  denominator) and the finding counts by state.
- Legacy carried lines (`Prior review blocker R1.2 is resolved ...: text`) are
  migrated into the registry with their IDs, so none disappears.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PREFIX = 'Prior review blocker'
INLINE_CHARS = 4000
# An ID is letters then digits (B3, E1, SYS12), optionally with a dashed
# sub-ID (B3-D2), followed by `:` or a long dash. `MD5 - ...` or `E2E: ...`
# are not IDs: a hyphen is not a separator and digits must end the first part.
_ID = re.compile(r'^\s*(?:\[(?P<lens>[a-z]+)\]\s*)?(?:\*\*)?\[?(?P<id>[A-Z]{1,6}\d{1,4}(?:-[A-Z]{1,6}\d{0,4})*)'
                 r'\]?(?:\*\*)?\s*[:\u2014\u2013]\s*(?P<text>.+)$', re.S)
_LINE = re.compile(r'^' + PREFIX + r' (?P<id>\S+) is resolved in the current code \(.*?\): (?P<text>.*)$', re.S)


def _norm(text) -> str:
    return ' '.join(str(text).split())


def parse(item: str, round_number: int, index: int) -> tuple[str, str]:
    """(finding ID, full text) of one blocking item."""
    text = _norm(item)
    match = _ID.match(text)
    if match:
        # Each panel lens numbers its own blockers: the lens is part of the identity.
        lens = match.group('lens')
        return (f"{lens}/{match.group('id')}" if lens else match.group('id')), match.group('text').strip()
    return f'R{round_number}.{index}', text


def line(fid: str, text: str) -> str:
    shown = text if len(text) <= INLINE_CHARS else (text[:INLINE_CHARS].rstrip() +
                                                    f' … (full text: controller findings registry, {fid})')
    return (f"{PREFIX} {fid} is resolved in the current code (met only if fixed, cited by file:line, or no longer "
            f"applicable with the reason; a builder's claim is not evidence): {shown}")


def key(requirement: str):
    """Coverage identity of a requirement or finding line: the finding ID when it has one."""
    match = re.match(r'^' + PREFIX + r' (\S+) ', str(requirement or ''))
    return ('finding', match.group(1)) if match else ('text', str(requirement))


def finding_id(requirement: str):
    k = key(requirement)
    return k[1] if k[0] == 'finding' else None


class Registry:
    def __init__(self, data=None):
        data = data or {}
        self.findings: dict[str, dict] = dict(data.get('findings', {}))
        self.rounds: list[dict] = list(data.get('rounds', []))

    def to_dict(self) -> dict:
        return {'schema_version': 1, 'findings': self.findings, 'rounds': self.rounds}

    def save(self, *paths) -> None:
        text = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        for path in paths:
            if path is None:
                continue
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + '.tmp')
            temporary.write_text(text, encoding='utf-8')
            temporary.replace(path)

    @classmethod
    def load(cls, path) -> 'Registry':
        try:
            return cls(json.loads(Path(path).read_text(encoding='utf-8')))
        except (OSError, ValueError):
            return cls()

    def _by_text(self, text: str):
        for fid, entry in self.findings.items():
            if _norm(entry['revisions'][-1]['text']) == text:
                return fid
        return None

    def add(self, blocking, round_number: int, source: str = '', categories=None) -> list[str]:
        """Register one rejected review's blocking items; returns their IDs in order."""
        ids = []
        this_round = set()
        for index, item in enumerate(blocking or [], 1):
            if not _norm(item):
                continue
            fid, text = parse(item, round_number, index)
            if fid in this_round:
                # The same ID twice in one review names two problems, not one revision.
                fid = f'{fid}~{index}'
            this_round.add(fid)
            if fid not in self.findings:
                same = self._by_text(_norm(text))
                if same and fid.startswith('R') and re.fullmatch(r'R\d+\.\d+', fid):
                    fid = same
            entry = self.findings.setdefault(fid, {'id': fid, 'first_round': round_number, 'state': 'open',
                                                   'revisions': [], 'history': []})
            if not entry['revisions'] or _norm(entry['revisions'][-1]['text']) != _norm(text):
                entry['revisions'].append({'round': round_number, 'text': text, 'source': source})
            if entry['state'] == 'resolved':
                entry['state'] = 'regressed'
            elif entry['state'] != 'regressed':
                entry['state'] = 'open'
            if categories and len(categories) == len(blocking):
                entry['category'] = categories[index - 1]
            entry['history'].append({'round': round_number, 'state': entry['state'], 'via': 'blocking',
                                     'category': entry.get('category', 'code_change')})
            ids.append(fid)
        return list(dict.fromkeys(ids))

    def rule(self, findings, round_number: int, approved: bool, blocking_ids=()) -> None:
        """Apply one completed review's verdicts by ID."""
        verdicts = {}
        for finding in findings or []:
            fid = finding_id(getattr(finding, 'requirement', ''))
            if fid:
                verdicts[fid] = bool(getattr(finding, 'met', False))
        for fid, entry in self.findings.items():
            if fid in blocking_ids:
                continue
            met = verdicts.get(fid)
            if met is None:
                continue
            if met:
                entry['state'] = 'resolved'
            else:
                entry['state'] = 'regressed' if entry['state'] == 'resolved' else 'open'
            entry['history'].append({'round': round_number, 'state': entry['state'], 'via': 'finding'})
        self.rounds.append({'round': round_number, 'approved': approved,
                            'open': self.count('open'), 'resolved': self.count('resolved'),
                            'regressed': self.count('regressed')})

    def count(self, state: str) -> int:
        return sum(1 for e in self.findings.values() if e['state'] == state)

    def requirements(self) -> list[str]:
        return [line(fid, e['revisions'][-1]['text']) for fid, e in self.findings.items()]

    def migrate(self, requirements) -> None:
        """Adopt legacy carried lines so earlier unresolved findings never disappear."""
        for requirement in requirements or []:
            match = _LINE.match(str(requirement))
            if match and match.group('id') not in self.findings:
                self.findings[match.group('id')] = {
                    'id': match.group('id'), 'first_round': 0, 'state': 'open',
                    'revisions': [{'round': 0, 'text': match.group('text'), 'source': 'migrated'}],
                    'history': [{'round': 0, 'state': 'open', 'via': 'migrated'}]}

    def merge_requirements(self, requirements) -> list[str]:
        """Original requirements first (unchanged), then exactly one line per finding ID."""
        self.migrate(requirements)
        base = [r for r in requirements or [] if key(r)[0] != 'finding']
        return [*base, *self.requirements()]


def carry_forward(requirements: list[str], blocking: list[str], round_number: int,
                  registry: "Registry | None" = None, categories=None) -> list[str]:
    """Make every later review round rule on each earlier blocker, by stable ID.

    Concept adapted from super-board's "review remembers" (EricTechPro/super-board,
    MIT): a re-review that starts from scratch can accept a blocker the builder
    only *claimed* to fix. Each blocker becomes a controller requirement, so the
    existing verdict_consistent gate demands an evidenced finding for it and
    refuses approval while it is unmet. Requirements are kept for all later
    rounds, which also catches a fix that regresses one round later.

    Identity is the finding ID (SYS-08): a blocker that reuses an earlier ID
    revises that requirement instead of adding a second one; a blocker without
    an ID is `R<round>.<n>`, or the existing finding with the same text.
    """
    registry = registry if registry is not None else Registry()
    registry.migrate(requirements)
    registry.add(blocking, round_number, categories=categories)
    return registry.merge_requirements(requirements)


def progress(registry: Registry, requirements, findings=None) -> dict:
    """Stable-denominator progress: original requirements and finding states, reported apart."""
    originals = [r for r in requirements or [] if key(r)[0] != 'finding']
    met = set()
    for finding in findings or []:
        if getattr(finding, 'met', False):
            met.add(key(getattr(finding, 'requirement', '')))
    return {'original_requirements': {'total': len(originals),
                                      'met_in_last_review': sum(1 for r in originals if key(r) in met)},
            'findings': {'open': registry.count('open'), 'resolved': registry.count('resolved'),
                         'regressed': registry.count('regressed'), 'total': len(registry.findings)}}
