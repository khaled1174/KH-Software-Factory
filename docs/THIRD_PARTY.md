# Source and attribution

KH Software Factory contains owner-supplied framework code and selected adapted
components. It is not an official release by the upstream maintainers. Original
notices remain where supplied; this package asserts no new license over
third-party source.

## Included components

- [delegate-skills](https://github.com/amElnagdy/delegate-skills), amElnagdy,
  revision b781ee2e23089630e2fbee1cfd6174afe4edeb76: adapted Codex relay with
  explicit sandbox mappings and bounded execution. Its pin, source hashes and
  MIT license are in [vendor/delegate-skills](../vendor/delegate-skills/README.md).
- [task-observer](https://github.com/rebelytics/one-skill-to-rule-them-all),
  Eoghan Henn / Rebelytics, revision 2967fa5f2f16336677d216fe83d9832a52aadc00:
  adapted instructions, local storage and sanitized promotion. The upstream
  [CC BY 4.0 license](../.claude/skills/task-observer/LICENSE.txt) and attribution remain.
- Owner-supplied fable-vs-astra pack: fable-pack-audit and astra-pack-audit retain
  their source identities in component ATTRIBUTION.md files. The supplied pack
  has no license file; no third-party license is invented.
- kh-scope-guard retains its supplied MIT notice, Copyright (c) 2026 Leonxlnx.
- [Matt Pocock skills](https://github.com/mattpocock/skills), revision
  49dd158d1076134a641b33efb035946536778336: selected role-prompt wording with the
  retained [MIT notice](../.claude/skills/kh/references/engineering-guidance/LICENSE.mattpocock).

## Conceptual influences

No code or text from the following sources is bundled; these references do not
claim ownership of those projects or assign them a new license.

- poteto/pstack: explicit model routing, verification principles and prose guidance.
- EricTechPro/super-board, revision 120bc1d: content-bound owner approval,
  sensitive-change categories and reviews that account for earlier findings.
- addyosmani/agent-skills, revision 1401c8b: constraint-driven quality-floor checks.
- UI Verify: modular rules, independent review and actual visual evidence.
- Owner-supplied Tac-7 methodology: current context, conditional documentation
  and scoped closing documentation. No supplied hooks or workflows are activated.

Frontend dependencies remain declared in package.json and bun.lock under their
respective licenses. Python development dependencies are pinned in
requirements-dev.txt. No model binaries or authenticated CLI configuration ship.
