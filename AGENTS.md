# KH Software Factory — agent guide

This repository is the KH Software Factory framework distribution: runtime templates,
18 slash commands, supporting skills, the vendored Codex relay, tests and the
release gate. Canonical rules live in the documents below; this file routes
and states repository facts. The controller contract is
`.claude/skills/kh/SKILL.md`.

## Map

- Orientation: `README.md`, `START_HERE.md` (+ `START_HERE_AR.md`), `COMMANDS.md`
- Delegation transport and reasoning-effort forwarding: `.claude/skills/kh/references/delegation.md`
- Current acceptance evidence: `docs/VERIFICATION.md`
- Skill/model integration decisions and open findings:
  `docs/Skills_Integration_Register.md`
- Builder/reviewer judgement rules:
  `.claude/skills/kh/references/verification-principles.md`
- Documentation prose policy: `.claude/skills/kh/references/prose-standard.md`

## Language

Write all new or updated repository content in English, including documentation,
comments, prompts, reports, UI text and file names. Keep conversation replies in
the owner's preferred language. START_HERE_AR.md is the owner-requested
maintained translation; other active Markdown remains English. Represent non-Latin test fixture characters with
Unicode escapes in source files so internationalization coverage is preserved.

## Checks and release gate

- Install dev dependencies once: `pip install -r requirements-dev.txt`
- Full test suite: `python3 -B -m unittest discover -s tests`
- Release: `python3 scripts/package.py` regenerates `MANIFEST.sha256`,
  rebuilds the ZIP, extracts it into a clean folder and reruns the suite plus
  an installer smoke test there. A green working tree is not a verified
  release; the extracted archive is.

Engine line ceiling: 17667 physical lines across Python files in
`.claude/skills/kh/templates/adws/`. The source and
extracted-archive suites enforce this ceiling. Raising it requires explicit
owner approval.

## Facts that constrain edits

- `MANIFEST.sha256` is generated only by `scripts/package.py`; hand edits are
  defects.
- `vendor/delegate-skills` is pinned third-party code; changing it requires
  updating its provenance record and the integration register.
- Tests assert exact contracts: 18 `kh-*` command files, 12 mapped scripts,
  and installer behavior. Changing those surfaces means updating the tests in
  the same change with a recorded reason.
- Shipped framework files carry no model catalog or ranking. The generated
  `kh.config.yaml` carries per-role starter model names as templates only;
  the effective per-role model/effort choices are owner-recorded at project
  start (`/kh-setup-models`) and take precedence over them. The delegate
  adapter forwards requested effort and checks it against the relay receipt.
- New skills follow the adoption checklist in the integration register:
  overlap review first; no scheduler, router or approval authority; reviewed
  text is never activated merely because it exists in source material.
