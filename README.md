# KH Software Factory

**Claude Code says "done." KH proves it.**

A local controller that takes one ticket through a reviewed plan, a locked file scope, your real test commands and an independent reviewer — and records acceptance only when all of them pass.

[![Verify](https://github.com/khaled1174/KH-Software-Factory/actions/workflows/verify.yml/badge.svg?branch=main)](https://github.com/khaled1174/KH-Software-Factory/actions/workflows/verify.yml) [![Release](https://img.shields.io/github/v/release/khaled1174/KH-Software-Factory?color=0d9488&label=release)](https://github.com/khaled1174/KH-Software-Factory/releases/latest) [![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

[Start here](START_HERE.md) · [18 commands](COMMANDS.md) · [Architecture](docs/ARCHITECTURE.md) · [Download](https://github.com/khaled1174/KH-Software-Factory/releases/latest) · [دليل عربي](START_HERE_AR.md)

<!--
DEMO SLOT — ضع هنا GIF أو asciinema لدورة مهمة واحدة كاملة (٣٠ ثانية):
الخطة المراجَعة ← البناء ← الاختبارات ← المراجع المستقل ← القبول.
مثال: ![Demo](docs/assets/demo.gif)
هذا أهم عنصر مفقود في الصفحة. لا تنشر بدونه إن أمكن.
-->

## The problem

Hand an agent a ticket and three things happen often enough to matter:

- it edits files that had nothing to do with the ticket,
- it reports success without running your tests,
- it reviews its own work and approves it.

So you read the whole diff anyway. You end up supervising the agent instead of shipping the work.

## What KH does about it

| What goes wrong | What the controller does |
| --- | --- |
| The agent edits files outside the ticket | The scope contract is frozen before the build and enforced against the actual changed files. Writes outside approved paths are rejected before they reach your source. |
| "Done" with no tests run | Your configured quality commands are run by the controller, not by the agent. A claim is not evidence. |
| The agent approves its own work | A separate reviewer model receives the diff measured by the controller and has to approve it. |
| A failed run starts over from zero | Finished work is retained. The next run continues from where it stopped, under the same plan. |
| Nothing is left to audit | Source fingerprints, phase traces and an acceptance receipt per task. |

Role models submit proposals and evidence. The controller owns ordering, scope, checks, source promotion and task completion — nothing else can mark a task accepted. [See the roles and their entry points](docs/ARCHITECTURE.md#role-ownership).

![The owner configures role models. Architect, planner, builder and documenter propose work. Plan reviewer and reviewer assess it. The controller owns checks, state and final acceptance.](docs/assets/roles-and-controller.svg)

## Why not just use Claude Code on its own?

For a scratch script, do exactly that — KH would only slow you down.

KH earns its place when the change lands in a codebase you care about and "mostly right" is expensive: a scope violation, a skipped test or a silent regression costs more than the run did. It trades speed for a record you can check afterwards.

## Quick start

Download and unpack the [latest release](https://github.com/khaled1174/KH-Software-Factory/releases/latest), then preview and install:

```bash
python3 install.py --project "/absolute/path/to/project" --dry-run
python3 install.py --project "/absolute/path/to/project"
```

Then:

1. Configure your real quality commands (tests, lint, build) — KH can only verify what you tell it to run.
2. Record each role's model and effort with `/kh-setup-models`.
3. Follow [START_HERE.md](START_HERE.md) to accept an intent, prepare project context and run your first task.

Upgrading an existing install? Read its backup, upgrade and restore instructions first. Your project configuration and role prompts are preserved.

## One guarded cycle per ticket

![Sequential task cycle: reviewed understanding, reviewed planning, isolated build, documentation proposals, checks and independent review, then promotion and acceptance.](docs/assets/task-cycle.svg)

Run a recorded project plan with `/kh-project run-continuous`. Tasks execute sequentially and a dependency unlocks only after the task it depends on is accepted. Corrections keep the same scope, permissions, deadlines and attempt limits as the original run.

## What this is not

- Not a hosted service. It runs on your machine, against your own CLI subscriptions. No credentials, model sessions or dependencies are bundled.
- Not a sandbox. Acceptance is still bounded by your native CLI permissions.
- Not a guarantee of correctness. It guarantees that the checks you configured actually ran and passed, and that an independent reviewer saw the diff.
- Not an approval UI. The optional trace UI observes work and reports tokens and cost; it cannot execute or approve anything. Cost figures are informational only.

## Status

Version 1.0.0 — see [CHANGELOG.md](CHANGELOG.md).

Honest state of things: this is built and used on my own projects, with Claude Code and Codex as the providers. It has not been through an external pilot yet. The release gate runs the full test suite, contract evaluations and installer smoke checks against the extracted release ZIP in CI — the Verify badge above is the live result.

[Verification and known limits](docs/VERIFICATION.md) sets out what those results do and do not establish. Issues and findings are welcome; that is the fastest way this gets better.

## Attribution

[Source attribution](docs/THIRD_PARTY.md) and the [integration register](docs/Skills_Integration_Register.md) identify sources, licenses and responsibilities.

## License

MIT — see [LICENSE](LICENSE).
