# Delegation integration

KH owns task state, phase retries, acceptance, and promotion. The vendored
delegate-skills relay supplies transport to an installed Codex CLI. It does not
create another orchestrator or share an implicit conversation between models.

## Claude sandbox

Every Claude send supplies mandatory sandbox settings through its generated
`--settings` file, including when user setup is inherited. Startup fails if the
native sandbox is unavailable; unsandboxed retries are disabled. Sandboxed
commands cannot access the network, and cannot read `~/.ssh`, `~/.aws` or
`~/.config/gh`. Matching `Read` deny rules protect the separate file tool.
Sandbox auto-allow is off: the owner's existing command approvals still apply.
The installed Claude Code 2.1.303 schema and a real macOS probe verified these
keys. Linux needs the native sandbox dependencies; unsupported hosts fail closed.
Codex, KH scope enforcement, existing permissions and retry limits are unchanged.

## Claude usage accounting

The Claude adapter attributes `result.modelUsage` as cumulative native-session
counters, including helper models. Each send returns the delta since the last
controller-recorded snapshot. Repeated assistant message IDs are provisional
observations, not additional turns; a valid terminal snapshot supersedes them.
`result.usage` is not the aggregate accounting source. Context occupancy still
uses the last assistant message and the existing reported-window rules.

Controller-owned session state lives beside the native `.created` marker. A
resumed session without a usable baseline reports an explicit partial lower
bound, anchors its first snapshot, and excludes unknown historical totals.
Rejected fresh-session creation cannot establish a zero baseline. Decreasing,
conflicting or malformed counters remain visible in accounting warnings; they
never create permissions, acceptance or a monetary stopping condition.

Each attempt writes `claude-usage.json` and emits `claude_usage_accounting`.
The receipt separates normalized usage, completeness and warnings from the
provider's estimated price. `actual_billed_subscription_cost_usd` remains null;
a query price is not the subscription bill. Cumulative estimated prices are
also differenced across resumes, while interrupted or uncovered tails remain
partial. Existing run-level uncertainty stays conservative across failures.

If session state cannot be saved, that send's charge is deferred and shown in
`unattributed_usage`; a later usable cumulative snapshot can attribute it once.
This prevents stale state from charging the same delta again. Corrupt or unsafe
state is retained and is never replaced with a guessed zero. Meter failures do
not stop source execution or relax any guard. Original reader, callback,
cancellation and cleanup exceptions retain their type and measured partial use.

Thinking is already included in output. Retrospectively disclosed thinking that
cannot be assigned within this send's output is recorded separately with an
incomplete-attribution warning. `reasoning_coverage_complete` distinguishes
reported thinking from an unreported zero. The four billed token components
remain the token-total identity. Snapshot semantics were replay-verified against
Claude Code 2.1.303; provider schema changes still need independent validation.

## Saved choices and task compatibility

Saved owner setup applies to mapped standalone workflows such as `/kh-prompt --agent builder`.
Their validation resolves only the roles they require. Guarded workflows retain
the choices resolved before the current run. Persistent setup waits until idle.
See [role setup and task compatibility](model-choice.md#task-tool-compatibility).

Model choices remain fixed during a run. The owner may select per-role
`--use` overrides before a later run or record defaults after the current writer
ends. Scope, quality gates, deadlines and send/repair counters are unchanged.

Role `required_capabilities` is checked against tools, sandboxes and exact native
command grants before dispatch. A file-only builder can write Swift
source while controller quality commands compile it. Legacy Swift/database names
require native shell configuration; executable and database access remain
unverified until actual tool receipts. Role `capability_probes` is retired and
accepted without effect for one compatibility release. Infrastructure worker
restrictions remain active. Compatibility failure never escalates permissions
or silently substitutes the model.

Task plan validation also checks every `required_test_commands` entry against
the configured builder before independent plan approval. The check is read-only:
it runs no host probe, shell command or model call. A native shell tool is required. The
same infrastructure boundary applies, including container and database settings.
The planner receives the incompatible command and reason within the existing
bounded correction loop. Unsupported additions to a rejected proposal may be
corrected or removed; owner requirements and previously validated test obligations
remain mandatory. Planning cannot create grants or prove external native access.

Revised plans and cached approvals repeat this validation.
The existing dispatch-time check remains authoritative when role settings change
later. Planning prompts include the current builder tools and the explicit list
of retained test obligations so a rejected addition does not become permanent.

## Codex supported surface

- Backend: Codex through the pinned standalone `codex-delegate` Node relay.
- Host: POSIX (macOS/Linux); Windows is explicitly rejected pending a dedicated
  shell-quoting and process-tree cancellation pilot.
- Requirements: Python dependencies used by KH, Node, Git, authenticated Codex
  CLI supporting `exec --json -o`, `--ignore-user-config`, explicit sandboxing,
  the `approval_policy` config key, and the configured model.
- Local tests invoke the actual relay against a fake Codex executable, including negative
  outcomes. Real account, model availability, sandbox enforcement and CLI-version
  compatibility must pass the owner's isolated pilot before production use.
- Other providers fail configuration validation rather than silently falling
  back to a different model or permissions model.

## Controller contract

`agent_delegate.run(DelegateRequest(...), on_event=None, on_spawn=None,
on_exit=None)` returns the common `PiResult` transport shape.

Mandatory request context includes original `task_context`, current `prompt`,
`system_prompt`, explicit `model`, controller `session_id`, artifact paths,
`cwd`, source `baseline` (HEAD and workspace snapshot identity), `allowed_paths`,
`evidence_requirements`, and the JSON `output_contract`. Retries supply the
original context plus the correction and relevant prior evidence explicitly.
Every call starts a fresh provider session; provider thread IDs are recorded for
diagnosis but never used for an implicit resume or `resume-last`.

The controller must create the isolated workspace before calling the adapter.
`allowed_paths` is the task's narrow promotion allowlist. Codex's native sandbox
can enforce read-only or workspace-wide writes, not file-glob allowlists.
Therefore writable dispatch requires `sandbox="workspace-write"` and explicit
`sandbox_scope=["**"]` within that isolated workspace. KH checks every actual
delta against `allowed_paths` before promoting changes. These are distinct
controls and neither may be represented as a substitute for the other.
The adapted relay disables implicit `/tmp` and `$TMPDIR` writable roots, extra
writable roots, and sandboxed-command network access. It uses approval policy
`never`, so permission requests fail instead of triggering an escalation. Config
keys are traced to a pinned official Codex schema in `PROVENANCE.json`.

Read-only dispatch requires `sandbox_scope=[]`. Claude tool allowlists and
extensions cannot be mapped faithfully to this relay; any configured `tools`
(including an empty list) or nonempty `extensions` causes a pre-dispatch error.
Use `tools: null` only when the controller has explicitly selected native
sandbox enforcement. Unsupported constraints are not ignored.

## Completion and evidence

Transport succeeds only when all of the following hold:

1. The relay exits zero and emits the recognized receipt schema.
2. Receipt status is `completed`, exitCode is integer zero, no error/signal exists,
   and cwd, model and sandbox match the dispatch.
3. Every provider event is valid JSON with a type; no provider failure event exists.
4. Exactly one `turn.completed` occurs and is the final event.
5. A nonempty final response exists and any supplied usage values are valid.

`terminal_success=true` means only that transport completed. The existing KH
output schema, evidence checks, review and quality gates still decide acceptance.
The adapter writes `normalized.json` with `accepted: false` and the controller as
the acceptance owner. It does not interpret the provider's text claim of success
as proof that the task passed.

Artifacts are placed under the supplied controller session directory in a unique
private per-invocation directory: request, relay receipt, provider JSONL stream,
final response, stdout/stderr, and normalized receipt. `raw_output_path` receives
the latest provider stream; full per-call streams remain in their own directories.
Event callbacks deliver normalized provider records after the relay finishes;
this adapter does not claim real-time tool tracing. Parent process start/end
callbacks expose the relay PID so the controller can cancel it.

Codex token counts are normalized with cached input split from uncached input.
Dollar cost is unavailable from this receipt and is marked `cost_known: false`;
zero in the legacy numeric cost field must not be presented as measured free usage.
Model context capacity/occupancy is not inferred from token totals.

## Isolated environment pilot (still required)

Record the actual OS, Node version, Codex version, configured model, and source
baseline. Run a small read-only task, then a disposable writable-workspace task
with one authorized source path. Verify external and unauthorized writes cannot
reach the source repository, validate evidence, and exercise a cancelled call.
Retain the resulting receipts. Do not enable broader provider permissions to
make a failed pilot pass. Authentication failures, unavailable model, unsupported
CLI flags, malformed streams, timeouts and absent terminal completion all fail
closed and are reported to the controller.

## Register mapping

DEL-01: single controller lifecycle. DEL-02: completion separated from acceptance.
DEL-03: explicit original context, cwd, baseline, scope, evidence and output schema.
DEL-04: typed normalized transport, tokens, errors, terminal state and artifacts.
DEL-05: explicit native sandbox and rejection of unmappable restrictions, with
controller isolation plus allowlist enforcement before promotion.
DEL-06: local fixture verification delivered; real-device/account pilot remains
an explicit external acceptance requirement, not a claimed completed test.

## Reasoning effort

The controller forwards AgentConfig.thinking through DelegateRequest.thinking
to the pinned relay's --effort option. The adapter validates low/medium/high/xhigh
and any narrower declared supported_thinking set before spawning. It checks that
the relay receipt effort agrees and stores requested_effort/effective_effort with
`effort_verification: relay_receipt`. An unsupported or missing receipt setting
cannot be silently reported as applied. This proves relay forwarding; actual
provider interpretation and model-specific support remain part of the live pilot.

Model selection and permitted pre-dispatch fallback belong to the routing
module. The adapter itself never switches providers or relaxes permissions.
