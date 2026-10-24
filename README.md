# KH Software Factory

KH Software Factory coordinates reviewed plans, scoped implementation, real
checks, independent review and verified source promotion.

Use continuous sequential execution for a recorded plan. The controller accepts
each task after its scoped build, checks and independent review pass.
Costs are informational. Permissions, deadlines, attempt limits and the quality
floor remain acceptance constraints.

Start with [START_HERE.md](START_HERE.md) and
[COMMANDS.md](COMMANDS.md), the catalog of 18 Claude Code slash commands.
[Architecture](docs/ARCHITECTURE.md) explains context, source proofs,
sequential acceptance and failure boundaries. The owner selects each role's
provider, model and effort.

```bash
python3 install.py --project "/absolute/path/to/project" --dry-run
python3 install.py --project "/absolute/path/to/project"
```

For an installed project, read the backup, upgrade and restore instructions
first. Differing project configuration and role prompts are preserved.
User installation provides commands; each target project needs its runtime.
The optional trace UI is local and does not execute or approve work.

[Verification](docs/VERIFICATION.md) states the current measured scope and limits.
A release passes after its extracted ZIP passes the full suite, contract
evaluations and installer smoke checks. Deterministic fixtures measure dispatch
and safety contracts; they do not establish universal model quality or charges.

[CHANGELOG.md](CHANGELOG.md) describes version 1.0.0.
[Source attribution](docs/THIRD_PARTY.md) and the
[integration register](docs/Skills_Integration_Register.md) identify sources,
licenses and component responsibilities.
No credentials, model sessions or dependencies are bundled.
