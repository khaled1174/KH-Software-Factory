# KH Software Factory — start here

KH has one acceptance controller. Read this guide for operation, the
[command catalog](COMMANDS.md) for supported entry points, and
[architecture](docs/ARCHITECTURE.md) for runtime boundaries.
Downloading this distribution does not migrate another project.
A [maintained Arabic translation](START_HERE_AR.md) follows the same sections.

## Requirements

- macOS, or Linux with `bubblewrap` and `socat`, or Windows through WSL2 with Ubuntu. WSL1 and native Windows are unsupported.
- Python 3.10 or newer, Git and `uv` in the environment where KH runs.
- Claude Code installed and logged in in that same environment. Install any additional selected provider CLI there too.

### Windows via WSL2

In administrator PowerShell, install Ubuntu. Restart Windows if requested and
complete Ubuntu's initial user setup:

```powershell
wsl --install -d Ubuntu
```

Then select WSL2 and confirm that Ubuntu's `VERSION` column is `2`:

```powershell
wsl --set-version Ubuntu 2
wsl --list --verbose
```

Open the Ubuntu terminal and install the Linux tools there:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv bubblewrap socat curl git
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://claude.ai/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
mkdir -p ~/projects
cd ~/projects
python3 --version
claude
```

Confirm Python is at least 3.10 and complete Claude Code's login. Clone or unpack
both KH and your project under `~/`, such as `~/projects/`, never `/mnt/c`.
Run the remaining installation and workflow commands inside Ubuntu.
See [Microsoft's WSL installation](https://learn.microsoft.com/en-us/windows/wsl/install)
and [filesystem guidance](https://learn.microsoft.com/en-us/windows/wsl/filesystems),
the [Claude Code quickstart](https://code.claude.com/docs/en/quickstart)
and [sandbox prerequisites](https://code.claude.com/docs/en/sandboxing),
and [uv installation](https://docs.astral.sh/uv/getting-started/installation/).

## Install

Unpack the complete distribution. Python 3.10 or newer runs the installer;
the target directory must already exist. Preview before applying:

```bash
python3 install.py --project "/absolute/path/to/project" --dry-run
python3 install.py --project "/absolute/path/to/project"
python3 install.py --user
```

The optional user installation supplies commands across projects only. Each
project needs its own runtime. Open a new Claude Code session and use /kh-help;
it reads that project's installed copy of the canonical command catalog.
Core installation includes skills, runtime, prompts and starter configuration;
it installs no dependencies, authenticates no provider and starts no sessions.

## Upgrade and restore

Different existing files block ordinary installation before any writes. Review
the dry run, then explicitly upgrade when the replacement is intended:

```bash
python3 install.py --project "/absolute/path/to/project" --upgrade --dry-run
python3 install.py --project "/absolute/path/to/project" --upgrade
```

Upgrades preserve existing adws/adw_kh_config/kh.config.yaml and differing
owner role prompts, including configured literal prompt destinations. The report
lists kept_project_files and package alternatives under .kh-backups/ for
explicit comparison and merge. --reset-config deliberately replaces only the
configuration, with backup; it does not reset prompts. Unrelated files and
live .env files are retained. Existing ignore rules remain; runtime rules append.
The installer never stages or commits Git changes.

The report gives the backup ID and exact restore command:

```bash
python3 install.py --restore "/absolute/path/to/project/.kh-backups/BACKUP_ID" --dry-run
python3 install.py --restore "/absolute/path/to/project/.kh-backups/BACKUP_ID"
```

Registered pristine retired files are backed up and removed; modified or unknown
copies stay under kept_retired_files. Restore recovers removed files, refuses
recreated paths and refuses later edits to installed files. Review the backup
manually when refusal protects newer work. Symlinked managed paths are refused.
Run no concurrent installer against the same target. Legacy-framework migration
is retired; use a fresh current KH installation when the owner requests one.

## Configure tools, models and checks

Workflow execution needs uv and the selected provider CLI with its existing
authenticated setup. just is optional for direct commands. Review the project's
adws/adw_kh_config/kh.config.yaml; configure real argv lists in
defaults.quality_checks and select applicable required_quality_checks.
An absent or placeholder command is not a passed check. Profile/scope check
requirements add to existing requirements; they cannot remove them.

Record an explicit runtime, model and effort for each needed role:

```text
/kh-setup-models --interactive
/kh-setup-models --interactive --apply
/kh-models --doctor
/kh-start --dry-run "Exact accepted task request"
```

Needed roles are planner, plan_reviewer, builder and reviewer, plus
project_architect for preparation and documenter when closing documentation is
enabled. Separate role contexts remain independent even when the owner chooses
the same model. KH supplies no model catalog or ranking. Setup previews without
--apply; local diagnostics do not prove login or model availability.
Persistent changes wait until the writer ends. A repeatable
--use ROLE=RUNTIME:MODEL:EFFORT selects an owner choice before one run; running
roles keep their resolved configuration. See the installed
[model-choice rules](.claude/skills/kh/references/model-choice.md).

By default .env entries reach provider adapters and quality commands. Set
defaults.env_allowlist deliberately: a named list admits those file values;
[] withholds all. Differently exported shell values remain, while values equal
to the file count as file-derived. Withheld names are reported. Credential
selection is independent of source scope; never paste secrets into role prompts.
See [configuration](.claude/skills/kh/references/config.md) for exact semantics,
runtime constraints, retries, effective send ceilings and deadlines. Numeric
send defaults have one documentation source there; --dry-run reports the
effective execution configuration.

## Intent, plan and project preparation

Copy the [intent template](.claude/skills/kh/templates/intent.md) into intents/.
Preserve the exact task request, scope, constraints and acceptance. The owner
accepts it before planning. The controller binds that text on first use or checks
an explicit accepted digest. Editing accepted text requires reacceptance; this
detects drift rather than authenticating the human. Follow the installed
[intent rules](.claude/skills/kh/references/intent.md).

/kh-bootstrap --dry-run inspects context readiness; /kh-bootstrap prepares
missing reviewed navigation. Guarded start does so under the same controller.
Use /kh-start --refresh-context for stale single-task context, or
/kh-bootstrap --refresh explicitly. Project batches refresh when required.
Unresolved owner decisions stop preparation without inventing answers. Valid
warm context requires no architect call. Existing owner document edits remain
preserved; readiness does not prove application tests or provider access.

/kh-plan-review TASK produces a reviewed plan without implementation. Guarded
start validates its own exact-version approval; there is no --use-plan shortcut.
The planner receives original requirements and applicable guides. An independent
reviewer checks coverage, order, native test feasibility and previous findings.
Conditional guide selection requires an explicit reviewed read/skip decision.
See [planning rules](.claude/skills/kh/references/reviewed-planning.md) and
[project context](.claude/skills/kh/references/project-context.md).

## Execute a task or recorded project

Use /kh-start TASK for full work or /kh-simple-sdlc TASK for guarded light
work. Light retains real checks, independent plan/code review and scope.
It requires concrete narrow paths and a positive independent eligibility verdict;
it cannot silently expand scope or downgrade evidence. Both keep the complete
candidate staged until accepted.

For a recorded plan, adapt project-plan.example.json, validate/init it once,
and create accepted intents matching its exact task requests:

```text
/kh-project init --plan project-plan.json --dry-run
/kh-project init --plan project-plan.json
/kh-project next
/kh-start --task T1 --dry-run
/kh-start --task T1
/kh-project status
```

The imported plan contains milestones, stable task IDs, deliverables, safe
allowed paths, dependencies, checks and positive weights. Unknown fields,
dependency cycles and unsafe scope fail validation. A tracked task supplies its
request; conflicting positional text or a separate scope file is refused.
Revise pending/blocked tasks with amend or split while idle, using accepted
replacement intents and an explicit reason. Splitting preserves original scope,
constraints, checks and total weight. Accepted tasks cannot be revised.
The installed [project reference](.claude/skills/kh/references/project-tracking.md)
defines the schema, transitions and receipt boundaries.

After plan and ticket review, execute the recorded plan sequentially:

```text
/kh-project run-continuous
```

The controller accepts each task only after planning, implementation, checks,
documentation and independent review succeed. Historical parallel state is
rejected with LEGACY_PARALLEL_STATE; preserve its files and use the prior
release for reconciliation. See [sequential execution](docs/ARCHITECTURE.md#sequential-execution).

## Follow progress, failure and recovery

The SQLite ledger .kh/project.db is authoritative. Local reports and receipts
show accepted work, blockers and evidence; back them up with the project.
Completion counts only checked, independently reviewed and promoted tasks.
Weighted progress measures the agreed plan, not elapsed time. Missing older
history stays unknown. status reads committed state; report regenerates
derived reports after a failed projection. No report or agent can mark completion.

run-continuous accepts one ready task, notifies the owner and then selects
another. It stops on completion, failure, pause or no ready work and does not
automatically retry blocked work. pause-after-current stops between its tasks.
An interrupted active task requires
explicit recover after its process ends; recovery records blocked, not accepted.

Use /kh-sessions, /kh-phases, /kh-tail and /kh-procs to inspect recorded traces.
Historical PIDs are not proof of liveness. Failed attempts, evidence and usage
remain recorded. Cost is cumulative information; unknown subscription prices
stay partial, and no monetary ceiling stops execution. Deadlines, send limits,
failed gates and unconfirmed cleanup remain real technical stops.

## Documentation and enforcement

Keep current guides and conditional_docs.md in docs/agent-guide/; put new
historical change records in app_docs/. Root CLAUDE/AGENTS files route to these
rules. The architect proposes reviewed guides; the controller derives command
facts and the conditional catalog. During implementation, the approved plan names
exact documentation targets. The read-only documenter proposes updates against
the measured candidate. The controller validates/stages them before checks and
independent code review, then publishes the index after promotion and before
task completion. Publication failure requires reconciliation.

### Where enforcement lives

The development hook, .claude/settings.json and .claude/protected-paths.txt
protect this distribution and are not installed into target projects. Installed
defaults.protected_files is a post-change acceptance check; it is not tool-time
blocking. Native provider permissions still apply. An isolated working copy is
not an OS, network or shared-database sandbox. Exact test command grants do not
authorize changing infrastructure; see
[test-command permissions](.claude/skills/kh/references/test-command-permissions.md).

By default accepted tasks report sensitive changes for owner attention after
completion. Configured owner-review hold mode stops before promotion instead.
The quality floor makes lowering tests/checks explicit review requirements.
Reviewers must rule on carried findings with evidence. No workflow implicitly
authorizes commits, pushes, merges, deployments or broader native permissions.

## Optional trace UI

Add UI source from the complete distribution, then use /kh-obs:

```bash
python3 install.py --project "/absolute/path/to/project" --with-visualizer
```

The installer adds no frontend dependencies. The installed recipe needs just
and Bun, installs with bun install --frozen-lockfile, builds dist and serves
UI/API on loopback port 4600. Core-only upgrades preserve existing UI files.
The white dashboard shows accepted project progress, blockers, traces, phase
gates and evidence. It does not dispatch, retry, approve or choose models.
Stale data is marked; incomplete historical metrics/cost stay unknown or partial.
The execution board is retired. The existing bounded builder resume note remains.

## Develop and verify this distribution

```bash
pip install -r requirements-dev.txt
python3 -B -m unittest discover -s tests
python3 -B scripts/run_evals.py
python3 -B scripts/package.py --output /tmp/kh-release.zip
python3 -B tests/test_next_call_counts.py --benchmark-contexts /tmp/kh-bench.json
```

Packaging generates the manifest, extracts the ZIP and reruns tests, evaluations
and installer smoke checks there. CI uploads complete completed-subprocess logs
and benchmark hashes; a green working tree alone is not a verified release.
The optional UI runs bun run build and bun run lint in its source directory;
actual Bun HTTP tests also run in the full suite. Offline contracts and fixture
send counts do not prove general model quality or application correctness.
[Verification](docs/VERIFICATION.md) records actual outcomes and limitations;
[attribution](docs/THIRD_PARTY.md), [adoption decisions](docs/Skills_Integration_Register.md)
and [change history](CHANGELOG.md) have separate canonical sources.
